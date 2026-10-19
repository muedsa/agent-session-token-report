#!/usr/bin/env python3
"""Claude Code / Codex / Gemini CLI 的本地会话存储适配器:定位文件、时间分布、子会话发现。

只读解析。codex 的 rollout 有相当一部分是 .jsonl.zst 压缩存档,必须和解压后的普通
文件一起统计,否则一个会话会少掉大半数据(本机实测 87 个 rollout 里过半是压缩的)。

一次 `scan()` 扫描同时产出时间戳、Claude 去重用量、子代理标记计数,并按
(路径, mtime, 大小) 复用 —— 同一会话的文件过去要被时间分布、标记、复核各扫一遍。
首行 session_meta 走单独的廉价路径(`first_record`),因为索引阶段要覆盖全部 rollout。
无法解码或读取失败的文件一律记进 `read_failures()`,由上游转成覆盖度告警,
绝不静默按 0 计入总量。
"""

import glob
import json
import os
import re
from datetime import datetime

from jsonl_reader import DecodeError, LineSource, zstd_backend

TS_RE = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IDLE_THRESHOLD_MS = 60 * 1000
LONG_GAP_MS = 10 * 60 * 1000
BACKEND = zstd_backend()

ROLLOUT_GLOBS = ("sessions", "archived_sessions")
MARKER_TOKENS = ('"spawn_agent"', '"SubAgentActivity"', '"subagents"')
MAX_CACHED_STAMPS = 200_000

CLAUDE_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_READ_FAILURES = []
_SCAN_CACHE = {}
_FIRST_RECORD_CACHE = {}


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def show(ms):
    sec = int(ms // 1000)
    return f"{sec // 3600}h{(sec % 3600) // 60}m{sec % 60}s"


def read_failures():
    """本进程内累计的文件级失败,调用方必须转成 coverage_warning 而不是忽略。"""
    return list(_READ_FAILURES)


def _note_failure(path, exc):
    _READ_FAILURES.append({"file": os.path.basename(path), "reason": f"{type(exc).__name__}: {exc}"})


def _stat_key(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return (path, None, None)


def reset_caches():
    """测试用:清掉扫描缓存与失败台账。"""
    _SCAN_CACHE.clear()
    _FIRST_RECORD_CACHE.clear()
    del _READ_FAILURES[:]


def iso_to_ms(text):
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError, TypeError):
        return None


def first_record(path):
    """只读第一行(rollout 的 session_meta);索引阶段要覆盖全部文件,必须保持廉价。"""
    key = _stat_key(path)
    if key in _FIRST_RECORD_CACHE:
        return _FIRST_RECORD_CACHE[key]
    record = None
    try:
        line = LineSource(path, BACKEND).first_line()
        record = json.loads(line) if line else None
    except (json.JSONDecodeError, OSError, DecodeError, RuntimeError) as exc:
        _note_failure(path, exc)
        record = None
    if key[1] is not None:
        _FIRST_RECORD_CACHE[key] = record
    return record


def _claude_totals(latest, anonymous, raw_records, agents, models):
    totals = {k: 0 for k in CLAUDE_TOKEN_KEYS}
    for usage in list(latest.values()) + anonymous:
        for field in totals:
            totals[field] += int(usage.get(field) or 0)
    totals.update({
        "usage_records_raw": raw_records,
        "usage_records_deduped": len(latest) + len(anonymous),
        "anonymous_usage_records": len(anonymous),
        "unique_message_ids": len(latest),
        "agent_ids": sorted(agents),
        "models": sorted(models),
    })
    return totals


def scan(path):
    """一次扫描产出该文件的全部派生指标(含按 message.id 保留的末次 usage)。"""
    key = _stat_key(path)
    if key in _SCAN_CACHE:
        return _SCAN_CACHE[key]
    profile = {
        "path": path, "lines": 0, "stamps": [], "markers": {}, "decode_error": None,
        "timestamp_hits": 0, "timestamp_unparsed": 0,
        "usage_latest": {}, "usage_anonymous": [], "usage_raw_records": 0,
        "agent_ids": set(), "models": set(),
    }
    try:
        for line in LineSource(path, BACKEND).lines():
            profile["lines"] += 1
            m = TS_RE.search(line)
            if m:
                profile["timestamp_hits"] += 1
                ms = iso_to_ms(m.group(1))
                if ms is not None:
                    profile["stamps"].append(ms)
                else:
                    profile["timestamp_unparsed"] += 1
            for token in MARKER_TOKENS:
                hit = line.count(token)
                if hit:
                    profile["markers"][token] = profile["markers"].get(token, 0) + hit
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            profile["usage_raw_records"] += 1
            if rec.get("agentId"):
                profile["agent_ids"].add(rec["agentId"])
            if rec.get("model"):
                profile["models"].add(rec["model"])
            mid = message.get("id") or rec.get("uuid")
            if mid:
                profile["usage_latest"][mid] = usage
            else:
                profile["usage_anonymous"].append(usage)
        for token in MARKER_TOKENS:
            profile["markers"].setdefault(token, 0)
    except (OSError, DecodeError, RuntimeError) as exc:
        profile["decode_error"] = f"{type(exc).__name__}: {exc}"
        _note_failure(path, exc)
    profile["claude"] = _claude_totals(profile["usage_latest"], profile["usage_anonymous"],
                                       profile["usage_raw_records"], profile["agent_ids"], profile["models"])
    cacheable = (not profile["decode_error"] and key[1] is not None
                 and len(profile["stamps"]) <= MAX_CACHED_STAMPS
                 and len(profile["usage_latest"]) <= MAX_CACHED_STAMPS)
    if cacheable:
        _SCAN_CACHE[key] = profile
    return profile


def claude_usage_from_files(paths):
    """跨多个主 transcript 求 Claude 用量:按 message.id 全局去重,不是逐文件去重再相加。

    一个 sessionId 在 ~/.claude/projects 下命中多个 project 目录(会话换过 cwd)时,
    同一条 message 会出现在多份文件里;逐文件去重后相加会把它重复计入,本地侧系统性偏高,
    复核就会报出假的 mismatch。
    """
    paths = list(paths or [])
    latest, anonymous, agents, models = {}, [], set(), set()
    raw = 0
    unreadable = []
    for path in paths:
        prof = scan(path)
        if prof["decode_error"]:
            unreadable.append({"file": os.path.basename(path), "reason": prof["decode_error"]})
            continue
        latest.update(prof["usage_latest"])
        anonymous.extend(prof["usage_anonymous"])
        agents |= prof["agent_ids"]
        models |= prof["models"]
        raw += prof["usage_raw_records"]
    out = _claude_totals(latest, anonymous, raw, agents, models)
    out["files_merged"] = len(paths)
    out["files_unreadable"] = unreadable
    if len(paths) == 1 and unreadable:
        out["error"] = unreadable[0]["reason"]
    return out


def _profile(stamps):
    if len(stamps) < 2:
        return {"events_with_timestamp": len(stamps), "note": "时间戳不足,无法计算时间分布"}
    active = 0
    gaps = []
    for prev, nxt in zip(stamps, stamps[1:]):
        delta = nxt - prev
        if delta <= IDLE_THRESHOLD_MS:
            active += delta
        else:
            gaps.append(delta)
    elapsed = stamps[-1] - stamps[0]
    return {
        "events_with_timestamp": len(stamps),
        "first": datetime.fromtimestamp(stamps[0] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "last": datetime.fromtimestamp(stamps[-1] / 1000).strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_human": show(elapsed),
        "active_human": show(active),
        "active_note": f"相邻事件间隔 <= {IDLE_THRESHOLD_MS // 1000}s 记为活跃,更长记为空闲;这是近似值,不等于模型计费时长",
        "idle_human": show(sum(gaps)),
        "idle_events": len(gaps),
        "idle_ge_10min_events": len([g for g in gaps if g >= LONG_GAP_MS]),
        "longest_idle_human": show(max(gaps)) if gaps else None,
    }


def time_profile(path):
    return time_profile_many([path])


def time_profile_many(paths):
    """跨多个文件(含 .zst)合并时间戳。"""
    stamps, unparsed = [], 0
    for path in paths:
        prof = scan(path)
        stamps.extend(prof["stamps"])
        unparsed += prof["timestamp_unparsed"]
    profile = _profile(sorted(stamps))
    if unparsed:
        profile["events_timestamp_unparsed"] = unparsed
        profile["unparsed_note"] = (
            f"{unparsed} 行有 timestamp 字段但无法解析为时间,已从时间分布中剔除;"
            "elapsed/active 因此可能偏小"
        )
    return profile


def usage_sum(path):
    """累加 Claude 格式 JSONL 里 message.usage 的 token 字段(按 message.id 去重取末次)。

    这是全项目唯一的 Claude 用量求和实现:store_jsonl 与 crosscheck 都用它,
    因为复核的独立轴是"本地存储 vs ccusage",不是"两份实现互验"。
    Claude Code 会把同一条 assistant 消息的 usage 重复写入多份(本机实测约 3 份),
    不去重求和会到 ccusage 数字的 3 倍。
    """
    return claude_usage_from_files([path])


def to_number(value):
    """各存储与 ccusage 的数值字段类型不保证:返回数字或 None(缺失),布尔视为缺失。

    None 与 0 在报告里含义不同,所以不用 0 兜底 —— 由求和方决定缺失怎么处理。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if not text:
            return None
        try:
            return float(text) if any(c in text.lower() for c in ".e") else int(text)
        except ValueError:
            return None
    return None


def file_size(path):
    try:
        return os.stat(path).st_size
    except OSError as exc:
        _note_failure(path, exc)
        return None


def rollout_uuid(path):
    found = UUID_RE.findall(os.path.basename(path))
    return found[-1] if found else None


def claude_store(session_id):
    root = home(".claude", "projects")
    main = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    info = {"store": "claude/projects", "root": root, "files": main, "children": [],
            "rollout_uuids": [session_id], "compressed_files": 0, "children_verified": bool(main),
            "files_attribution_verified": bool(main)}
    if not main:
        info["note"] = "在 ~/.claude/projects 下没找到该 session 的 JSONL"
        return info
    subagent_dirs = sorted({os.path.join(os.path.dirname(m), session_id, "subagents") for m in main})
    info["subagent_dirs_scanned"] = len(subagent_dirs)
    info["children_mechanism"] = "<project>/<uuid>/subagents/agent-*.jsonl;sidechain 记录与父会话共用 sessionId,主 transcript 只含 isSidechain:false,故子代理用量不在父行内"
    agent_files = sorted({f for d in subagent_dirs for f in glob.glob(os.path.join(d, "agent-*.jsonl"))})
    for agent_file in agent_files:
        meta_path = agent_file[: -len(".jsonl")] + ".meta.json"
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                _note_failure(agent_file, exc)
                meta = {"note": f"meta.json 无法解析: {exc}"}
        prof = scan(agent_file)
        info["children"].append({
            "kind": "subagent",
            "file": agent_file,
            "bytes": file_size(agent_file),
            "agent_type": meta.get("agentType"),
            "description": meta.get("description"),
            "tool_use_id": meta.get("toolUseId"),
            "spawn_depth": meta.get("spawnDepth"),
            "usage": dict(prof["claude"], **({"error": prof["decode_error"]} if prof["decode_error"] else {})),
            "timing": _profile(sorted(prof["stamps"])),
        })
    return info


def codex_files(root):
    paths = []
    for sub in ROLLOUT_GLOBS:
        paths.extend(glob.glob(os.path.join(root, sub, "**", "*.jsonl"), recursive=True))
        paths.extend(glob.glob(os.path.join(root, sub, "**", "*.jsonl.zst"), recursive=True))
    return sorted(set(paths))


def codex_store(session_id):
    root = home(".codex")
    found = UUID_RE.findall(session_id)
    uuid = (session_id if re.fullmatch(r"[0-9a-fA-F-]{36}", session_id) else (found[-1] if found else ""))
    uuid = uuid.lower()
    paths = codex_files(root)
    info = {
        "store": "codex/sessions + archived_sessions(.jsonl 与 .jsonl.zst)",
        "root": root,
        "session_uuid": uuid,
        "children": [],
        "rollout_files_total": len(paths),
        "rollout_compressed_total": len([p for p in paths if p.endswith(".zst")]),
        "compression_backend": BACKEND,
    }
    if BACKEND is None and any(p.endswith(".zst") for p in paths):
        info["compression_warning"] = "存在 .jsonl.zst rollout 但本机没有可用的 zstd 后端(node/zstd/python-zstandard 均缺),压缩分片未纳入统计"
    sessions = {}
    alias = {}  # rollout 自身的 id -> 会话的 session_id(真实 codex 两者不同)
    failures_before = len(_READ_FAILURES)
    for path in paths:
        rec = first_record(path)
        if not rec:
            continue
        payload = rec.get("payload") or {}
        sid = payload.get("session_id") or payload.get("id")
        if not sid:
            continue
        sid = str(sid).lower()
        rid = payload.get("id")
        if rid and sid and rid != sid:
            alias.setdefault(str(rid).lower(), sid)
        entry = sessions.setdefault(sid, {
            "session_id": sid, "files": [], "rollout_uuids": [], "rollout_ids": [], "compressed": 0,
            "forked_from_id": None, "cwd": payload.get("cwd"), "originator": payload.get("originator"),
            "cli_version": payload.get("cli_version"), "source": payload.get("source"),
            "model_provider": payload.get("model_provider"),
            "first_timestamp": rec.get("timestamp"),
        })
        entry["files"].append(path)
        if rid:
            entry["rollout_ids"].append(rid)
        rid2 = rollout_uuid(path)
        if rid2:
            entry["rollout_uuids"].append(rid2)
        if path.endswith(".zst"):
            entry["compressed"] += 1
        if rec.get("timestamp") and (entry["first_timestamp"] is None or rec["timestamp"] < entry["first_timestamp"]):
            entry["first_timestamp"] = rec["timestamp"]
        # 续跑产生的后续 rollout 会把 forked_from_id 写成本会话自己,只有最早那份的 forked_from_id 才是真正的父
        fork = payload.get("forked_from_id")
        # 血缘里的 id 同样要归一大小写:父会话的 sid 与 rollout id 都已小写化,
        # 这里不归一会让大写 forked_from_id 的子会话被静默丢弃(children_verified 仍是 True)
        fork = str(fork).lower() if fork else None
        if fork and fork != sid:
            stamp = rec.get("timestamp") or ""
            best = entry.get("_fork_best_stamp")
            if entry["forked_from_id"] is None or (best is not None and stamp < best):
                entry["forked_from_id"] = fork
                entry["_fork_best_stamp"] = stamp
    for sid, m in sessions.items():
        if m.get("forked_from_id"):
            m["forked_from_resolved"] = alias.get(m["forked_from_id"], m["forked_from_id"])
    info["sessions_indexed"] = len(sessions)
    info["rollouts_unreadable"] = len(_READ_FAILURES) - failures_before
    info["rollouts_without_session_meta"] = len(paths) - info["rollouts_unreadable"] - sum(
        len(m["files"]) for m in sessions.values()
    )
    if not uuid:
        info["note"] = "给定 id 里没有 UUID,无法在 codex 存储中定位"
        return info
    canonical = alias.get(uuid) or alias.get(uuid.lower())
    here = sessions.get(uuid) or sessions.get(uuid.lower())
    if here is None and canonical:
        here = sessions.get(canonical)
        if here is not None:
            info["requested_id_form"] = "rollout_id"
            info["note"] = (
                f"传入的 id {uuid} 是 rollout 自身的 id(session_meta.payload.id),"
                f"已映射到会话 id {canonical};会话边界按 {canonical} 统计"
            )
    elif here is not None:
        info["requested_id_form"] = "session_id"
    if here:
        sid_key = here["session_id"]
        info["session_id"] = sid_key
        info["files"] = here["files"]
        info["rollout_uuids"] = here["rollout_uuids"]
        info["session_meta"] = {k: v for k, v in here.items() if k != "files" and not k.startswith("_")}
        if here.get("forked_from_id"):
            info["parent_session_id"] = here["forked_from_id"]
            if here.get("forked_from_id") == sid_key:
                info["parent_warning"] = "解析出的父引用指回本会话自己,血缘未确定"
        info["children_mechanism"] = "rollout 首行 session_meta.payload.forked_from_id 指回本会话,即为派生(fork)会话"
        info["children_verified"] = True
        info["files_attribution_verified"] = True
        # forked_from_id 可能指向本会话的 session_id,也可能指向其中任意一份 rollout 的 id;
        # 只比 session_id 会把"从续跑分片派生"的子会话漏掉,而 children_verified 仍是 True。
        parents = {sid_key, uuid} | {r.lower() for r in here.get("rollout_ids") or []} \
                  | {r.lower() for r in here.get("rollout_uuids") or []}
        info["children"] = sorted(
            [{"kind": "forked_session", "session_id": cid, "files": m["files"], "rollout_uuids": m["rollout_uuids"],
              "timestamp": m["first_timestamp"], "cwd": m["cwd"]}
             for cid, m in sessions.items()
             if m.get("forked_from_resolved") in parents and cid != sid_key],
            key=lambda x: x["session_id"],
        )
        markers = {token: 0 for token in MARKER_TOKENS}
        for path in here["files"]:
            for token, count in scan(path)["markers"].items():
                markers[token] = markers.get(token, 0) + count
        info["subagent_markers"] = markers
        info["multi_rollout_note"] = (
            f"该 session_id 对应 {len(here['files'])} 个 rollout 文件(其中 {here['compressed']} 个为 .zst 压缩),"
            "时间分布已跨文件合并;ccusage 按 rollout 文件出行,故 token 总量需按这些文件聚合"
            if len(here["files"]) > 1 else None
        )
    else:
        matched = [p for p in paths if uuid in os.path.basename(p).lower()]
        info["files"] = matched
        info["rollout_uuids"] = [rollout_uuid(p) for p in matched]
        info["children_verified"] = False
        info["files_attribution_verified"] = False
        metas = [first_record(p) for p in matched]
        metas = [m.get("payload") or {} for m in metas if m]
        if metas:
            info["session_meta"] = {
                "session_id": metas[0].get("session_id"), "cwd": metas[0].get("cwd"),
                "originator": metas[0].get("originator"), "cli_version": metas[0].get("cli_version"),
                "model_provider": metas[0].get("model_provider"),
                "rollout_ids": [m.get("id") for m in metas if m.get("id")],
            }
        info["note"] = (
            "只按文件名匹配到候选 rollout,session_meta 里没有这个 id 作为 session_id;"
            "因此派生(fork)子会话是否为零未经验证,且这些文件是否真属于该会话也未核实;"
            "报告第 3 节必须写「未验证」而不是「0 个」,第 4 节的本地数字要标注归属未核实"
        )
    return info


def _id_present(text, session_id):
    """判断文本里是否真的出现了这个会话 id。

    完整 36 位 UUID 不可能是别的 UUID 的一部分,直接子串匹配(文件名里 `rollout-...-<uuid>`
    这种前导连字符是合法形态,不能被边界规则误杀)。只有短 id 才要求两侧不接 id 字符,
    否则 `aaaa-bbbb` 会命中 `aaaa-bbbb-cccc`,把别人的会话算进本会话。
    """
    if UUID_RE.fullmatch(session_id):
        return session_id.lower() in text.lower()
    found = re.finditer(re.escape(session_id), text, re.I)
    hexish = set("0123456789abcdefABCDEF")
    for m in found:
        s_at, e_at = m.start(), m.end()
        before = text[s_at - 1] if s_at > 0 else ""
        before2 = text[s_at - 2] if s_at > 1 else ""
        after = text[e_at] if e_at < len(text) else ""
        after2 = text[e_at + 1] if e_at + 1 < len(text) else ""
        if (before in hexish) or (before in "-_" and before2 in hexish):
            continue
        if (after in hexish) or (after in "-_" and after2 in hexish):
            continue
        return True
    return False


def gemini_store(session_id):
    root = home(".gemini")
    short = session_id.split("-")[0]
    files = glob.glob(os.path.join(root, "tmp", "*", "chats", f"*{short}*.jsonl"))
    matched, unreadable = [], []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                if _id_present(fh.read(4000), session_id):
                    matched.append(path)
        except OSError as exc:
            _note_failure(path, exc)
            unreadable.append(os.path.basename(path))
    info = {
        "store": "gemini/tmp/*/chats",
        "root": root,
        "files": matched,
        "children": [],
        "rollout_uuids": [session_id],
        "compressed_files": 0,
        "matched_by_full_id": bool(matched),
        "children_verified": False,
        "files_attribution_verified": bool(matched),
        "unreadable_files": unreadable,
        "children_mechanism": "未在 ~/.gemini 会话文件里发现父引用或子会话目录;若报告需要子会话,只能依赖 ccusage 行本身",
    }
    if not matched:
        # 只按短 id 命中、文件里又没有完整 session_id 时,不能把这些文件说成本会话的:
        # 它们可能属于同前缀的另一个会话,写进报告会污染数据范围。
        info["candidate_files"] = files
        info["note"] = (
            f"按短 id 前缀 glob 命中 {len(files)} 个 chat 文件,但首 4000 字符里没有完整 session_id;"
            "未确认归属,故不作为本会话文件统计(reported files=0)"
        )
    return info


ADAPTERS = {"claude": claude_store, "codex": codex_store, "gemini": gemini_store}


def describe(agent, session_id, files):
    """补时间分布与行级覆盖度;主会话 token 由 ccusage 提供,时间分布复用同一次扫描。"""
    files = files or []
    profiles = [scan(f) for f in files]
    out = {
        "file_count": len(files),
        "files_bytes": [file_size(f) for f in files],
        "compressed_files": len([f for f in files if f.endswith(".zst")]),
        "compression_backend": BACKEND,
        "lines": sum(p["lines"] for p in profiles),
        "unreadable_files": sorted({os.path.basename(p["path"]) for p in profiles if p["decode_error"]}),
    }
    if files:
        out["timing"] = time_profile_many(files) if len(files) > 1 else time_profile(files[0])
    return out
