#!/usr/bin/env python3
"""独立复核:自己从原始存储算一份 token 总量,与 ccusage 的行对照,给出差异与解释。

各平台的"真值"来源不同:
- opencode:SQLite 里按消息类型求和(可精确解释 ccusage 少掉的 compaction 部分)。
- claude:主 transcript 按 message.id 去重求和(与 store_jsonl 共用同一份实现,
  复核的独立轴是"本地存储 vs ccusage",不是"两份实现互验";这一条要在报告里如实说明)。
- codex:逐轮累加 token_count.info.last_token_usage(与 ccusage 同口径),并按会话合并多个 rollout 分片。

本模块的任何一步都不允许因为异常而丢掉已经算好的对账数据:时间戳解析、
字段类型、空文件,全部降级成可见的说明字段。
"""

import json
import os
from datetime import datetime, timezone

import store_jsonl
from jsonl_reader import DecodeError, LineSource, zstd_backend

BACKEND = zstd_backend()

TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
                "reasoning_output_tokens", "thinking_output_tokens", "total_tokens")


def _parse_stamp(raw):
    """把 ccusage 的 lastActivity 归一为 aware datetime,并说明用了哪种假设。

    支持:epoch 秒/毫秒、带时区的 ISO、不带时区的 ISO(按本机本地时间解释)。
    解析不了返回 (None, 原因),绝不抛异常 —— 过去这里抛 TypeError 会把整个复核章节一起吞掉。
    """
    if raw is None or raw == "":
        return None, "ccusage 行没有 lastActivity"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        seconds = raw / 1000 if raw > 1e11 else raw
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc), "epoch"
        except (OverflowError, OSError, ValueError):
            return None, f"epoch 数值越界: {raw}"
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"无法解析的时间格式: {text[:40]}"
    if parsed.tzinfo is None:
        try:
            return parsed.astimezone(), "naive ISO,按本机本地时间解释"
        except (OSError, OverflowError, ValueError):
            # 1970 年以前的 naive 时间在 Windows 上 astimezone() 会抛 OSError,
            # 这里必须降级而不是让它把整份 fact pack 带走
            return None, f"naive 时间无法换算到本地时区: {text[:40]}"
    return parsed, "带时区 ISO"


def _gap_minutes(last_activity):
    """会话距上次活动多久,用于判断偏差能否归因于快照错开。"""
    stamp, basis = _parse_stamp(last_activity)
    if stamp is None:
        return None, basis
    try:
        minutes = round((datetime.now(timezone.utc) - stamp).total_seconds() / 60, 1)
    except (TypeError, OSError, OverflowError, ValueError):
        return None, f"时间基准不可比: {basis}"
    return minutes, basis


def codex_file_tokens(path):
    """按文件累加 token_count.info.last_token_usage,并按 usage 值去重。

    同一轮的 usage 有时会被重复写入(时间戳不同、数值相同),ccusage 只计一次;
    不去重会系统性偏高(本机某个文件 +22%)。info.total_token_usage 是文件内累计值,
    不能用它跨文件相加(续跑分片会把历史累计带进来)。
    """
    final_total = None
    seen_values = set()
    per_turn_deduped = {k: 0 for k in TOKEN_FIELDS}
    per_turn_raw = {k: 0 for k in TOKEN_FIELDS}
    turns_raw = turns_deduped = duplicates = 0
    try:
        stream = LineSource(path, BACKEND).lines()
        for line in stream:
            if '"token_count"' not in line and '"total_token_usage"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = rec.get("payload") or {}
            info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
            node = info.get("total_token_usage")
            if isinstance(node, dict) and any(k in node for k in TOKEN_FIELDS):
                final_total = node
            last = info.get("last_token_usage")
            if isinstance(last, dict):
                key = json.dumps(last, sort_keys=True)
                turns_raw += 1
                for field in TOKEN_FIELDS:
                    per_turn_raw[field] += int(last.get(field) or 0)
                if key in seen_values:
                    duplicates += 1
                    continue
                seen_values.add(key)
                turns_deduped += 1
                for field in TOKEN_FIELDS:
                    per_turn_deduped[field] += int(last.get(field) or 0)
    except (OSError, DecodeError, RuntimeError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    detail = {
        "turns_raw": turns_raw,
        "turns_deduped": turns_deduped,
        "duplicate_records": duplicates,
        "per_turn_raw": per_turn_raw,
        "per_turn_sum": per_turn_deduped,
    }
    return final_total, detail, None


def codex_check(files):
    total = {k: 0 for k in TOKEN_FIELDS}
    counted = 0
    per_turn_total = {k: 0 for k in TOKEN_FIELDS}
    raw_total = {k: 0 for k in TOKEN_FIELDS}
    turns_raw = turns_deduped = duplicates = cumulative_count = 0
    unreadable = []
    no_token_records = []
    for path in files:
        node, extra, error = codex_file_tokens(path)
        if error:
            unreadable.append({"file": os.path.basename(path), "reason": error})
            continue
        if node:
            cumulative_count += 1
            for key, value in node.items():
                if key in total and isinstance(value, (int, float)) and not isinstance(value, bool):
                    total[key] += int(value)
        if not extra or not extra.get("turns_raw"):
            no_token_records.append(os.path.basename(path))
            continue
        counted += 1
        turns_raw += extra.get("turns_raw", 0)
        turns_deduped += extra.get("turns_deduped", 0)
        duplicates += extra.get("duplicate_records", 0)
        for key in TOKEN_FIELDS:
            per_turn_total[key] += extra.get("per_turn_sum", {}).get(key, 0)
            raw_total[key] += extra.get("per_turn_raw", {}).get(key, 0)
    # 与 ccusage 对齐的口径是"逐轮 last_token_usage 去重后求和";
    # 每文件累计 total_token_usage 相加会重复计入续跑文件带回的历史量(实测 total 差 9%、input 差 33%)。
    aligned = {
        "input_tokens": per_turn_total["input_tokens"] - per_turn_total["cached_input_tokens"],
        "cache_read_input_tokens": per_turn_total["cached_input_tokens"],
        "cache_creation_input_tokens": per_turn_total["cache_write_input_tokens"],
        "output_tokens": per_turn_total["output_tokens"],
        "reasoning_output_tokens": per_turn_total["reasoning_output_tokens"],
        "total_tokens": per_turn_total["total_tokens"],
    }
    return {
        "method": "逐条累加 token_count.info.last_token_usage 并按 usage 值去重(与 ccusage 同口径);"
                  "input 已扣除 cached 以对齐 ccusage 的 input/cacheRead 分列",
        "files_scanned": len(files),
        "files_with_token_count": counted,
        "files_with_cumulative_total": cumulative_count,
        "files_unreadable": unreadable,
        "files_without_token_count": sorted(set(no_token_records)),
        "turns_raw": turns_raw,
        "turns_deduped": turns_deduped,
        "duplicate_records_skipped": duplicates,
        "aligned_to_ccusage": aligned,
        "per_file_cumulative_sum": total,
        "per_turn_without_dedup": raw_total,
        "cumulative_vs_per_turn_note": "若改用每文件最后一条 total_token_usage 相加,会把续跑分片带回的历史累计重复计入",
        "backend": BACKEND,
    }


def claude_check(main_files):
    """主 transcript 去重求和(唯一实现来自 store_jsonl.usage_sum),子会话文件另算。"""
    totals = store_jsonl.claude_usage_from_files(main_files)
    totals["method"] = ("主 transcript 跨文件按 message.id(缺失时退到记录 uuid)全局去重取末次 usage 后求和,"
                        "不含 subagents 文件;与适配器共用同一份实现")
    return totals


def compare(mine, ccusage_row, mapping):
    """mine/ccusage 字段名不同,按 mapping 逐项比对并给百分比差。

    数值判定接受 int 与 float —— ccusage 的 token 字段不保证是整数,
    过去只认 int,一旦是 float 就所有 delta 静默变 null,复核章节等于没做。
    """
    rows = []
    if not ccusage_row:
        return [{"field": "ccusage", "mine": None, "ccusage": None, "note": "没有可对照的 ccusage 行"}]

    num = store_jsonl.to_number
    for label, mine_key, cc_key in mapping:
        a, b = num(mine.get(mine_key)), num(ccusage_row.get(cc_key))
        delta = round(a - b, 6) if (a is not None and b is not None) else None
        if delta == 0:
            pct = 0.0  # 两侧同为 0 是"逐字段一致",不是"没有可比数值"
        elif delta is not None and b:
            pct = round(100 * delta / b, 2)
        else:
            pct = None
        row = {"field": label, "mine": a, "ccusage": b, "delta": delta, "delta_pct": pct}
        if a is None or b is None:
            row["note"] = "至少一侧没有可比数值"
        elif delta and b == 0:
            # 一侧为 0 另一侧非零:百分比无定义,但这是最大的不一致,不能被当成"没有可比数据"丢掉
            row["unbounded_delta"] = True
            row["note"] = "一侧为 0 而另一侧非零,无法用百分比表达,按不一致处理"
        rows.append(row)
    return rows


MAPPING_OPENCODE = [
    ("input", "input", "input"),
    ("output", "output", "output"),
    ("cache_read", "cache_read", "cache_read"),
    ("reasoning", "reasoning", "reasoning_output_tokens"),
]
MAPPING_CLAUDE = [
    ("input", "input_tokens", "input"),
    ("output", "output_tokens", "output"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("cache_creation", "cache_creation_input_tokens", "cache_creation"),
]
MAPPING_CODEX = [
    ("input", "input_tokens", "input"),
    ("output", "output_tokens", "output"),
    ("cache_read", "cache_read_input_tokens", "cache_read"),
    ("cache_creation", "cache_creation_input_tokens", "cache_creation"),
    ("reasoning", "reasoning_output_tokens", "reasoning_output_tokens"),
    ("total", "total_tokens", "total"),
]


def _finalize(out, gap_minutes=None, gap_basis=None, verdict=None):
    """给复核结论:最大偏差百分比 + 一致性判定 + 与判定相称的 verdict。"""
    if out.get("agreement") == "not_applicable":
        # 结构性无法复核:补齐报告要引用的字段,并且不给"距上次活动"这种无关数值
        out.setdefault("worst_field", None)
        out.setdefault("numeric_fields_compared", 0)
        out.setdefault("comparison", [])
        out.setdefault("delta_interpretation", "本次没有可比的两边,不产出偏差判定")
        return out
    out["minutes_since_last_activity"] = gap_minutes
    if gap_basis:
        out["last_activity_basis"] = gap_basis
    if gap_minutes is not None and gap_minutes < 0:
        out["clock_skew_suspected"] = True
        out["clock_skew_note"] = (
            f"lastActivity 换算后比快照时间还晚 {-gap_minutes:.1f} 分钟,"
            "多半是时区假设问题;不能据此判定会话仍活跃,1%–5% 偏差不该归因于快照错开"
        )
    rows = out.get("comparison") or []
    pct_rows = [r for r in rows if isinstance(r.get("delta_pct"), (int, float))]
    hard = [r for r in rows if r.get("unbounded_delta")]
    if hard:
        worst = max(hard, key=lambda r: abs(r["delta"]))
        out["agreement"] = "mismatch"
        out["max_abs_delta_pct"] = None
        out["worst_field"] = worst["field"]
        out["numeric_fields_compared"] = len(pct_rows) + len(hard)
        out["delta_interpretation"] = (
            f"字段 {worst['field']} 一侧是 {worst['mine']}、另一侧是 {worst['ccusage']},"
            "其中一方为 0 而另一方非零:百分比无定义,但这是不可忽略的不一致,"
            "必须定位(该平台是否被 ccusage 计入、会话边界是否取错、去重口径是否不同)后才能定论"
        )
        out["unbounded_delta_fields"] = [r["field"] for r in hard]
        out["verdict"] = verdict or "needs_attribution"
        return out
    if not pct_rows:
        out["agreement"] = "no_numeric_overlap"
        out["max_abs_delta_pct"] = None
        out["worst_field"] = None
        out["numeric_fields_compared"] = 0
        out["delta_interpretation"] = (
            "两侧没有任何一组可比的数值字段(字段缺失、为 null 或类型不可比)。"
            "这不代表一致,只代表无法复核 —— 报告必须如实写未对齐,不能引用为交叉验证通过"
        )
        out["verdict"] = verdict or "unverifiable"
        return out
    magnitude = max(abs(r["delta_pct"]) for r in pct_rows)
    worst = max(pct_rows, key=lambda r: abs(r["delta_pct"]))
    out["max_abs_delta_pct"] = round(magnitude, 2)
    out["worst_field"] = worst["field"]
    out["numeric_fields_compared"] = len(pct_rows)
    recent = gap_minutes is not None and 0 <= gap_minutes <= 5
    out["agreement"] = ("exact" if magnitude == 0
                        else "close" if magnitude <= 1
                        else "snapshot_skew_likely" if magnitude <= 5 and recent
                        else "divergent_unexplained" if magnitude <= 25 else "mismatch")
    if magnitude > 1:
        out["delta_interpretation"] = (
            ("会话刚在读取前活动过,1%–5% 偏差可按快照错开解释" if recent and magnitude <= 5 else
             "会话早已停止写入,1%–5% 的偏差不能算快照错开,属于口径差(逐轮 vs 累计、边界记录去重等),需定位后才能定论")
            if magnitude <= 5 else
            "超过 5% 先查口径:累计值 vs 逐轮值、是否含 compaction/sidechain、input 是否含 cached,再下结论"
        )
    out["verdict"] = verdict or ("ok" if out["agreement"] in ("exact", "close", "snapshot_skew_likely")
                                 else "needs_attribution")
    return out


def _not_applicable(out, reason):
    """没有可比路径时也要把报告要引用的字段补齐,并明确标注不适用。"""
    out["agreement"] = "not_applicable"
    out["max_abs_delta_pct"] = None
    out["minutes_since_last_activity"] = None
    out.setdefault("worst_field", None)
    out.setdefault("numeric_fields_compared", 0)
    out.setdefault("comparison", [])
    out.setdefault("delta_interpretation", "本次没有可比的两边,不产出偏差判定")
    out["reason"] = reason
    return out


def _local_side_coverage(out, mine, platform, cc):
    """区分两种"本地这边不完整",不要一律把整段复核弃掉。

    - 一条用量都没读到:两边都是 0 不能算复核通过 → not_applicable。
    - 读到了一部分但有文件解码失败:仍然比对,但把 mine 标成下限,
      并给出必须转述的告警(弃掉比对反而会让报告丢掉整节)。
    """
    unreadable = mine.get("files_unreadable") or []
    counted = mine.get("usage_records_deduped")
    if counted is None:
        counted = mine.get("turns_deduped")
    if counted is None:
        counted = mine.get("rows")
    if counted in (None, 0):
        out["coverage_warning"] = (
            f"{platform} 本地存储这边没有读到任何用量记录"
            + (f";{len(unreadable)} 个文件读取或解码失败:{[u['file'] for u in unreadable][:5]}" if unreadable else "")
            + "。这一边是空的,拿它跟 ccusage 比对没有意义,不能因为两边都显示 0 就声称复核通过"
        )
        out["verdict"] = "local_read_failed"
        return _not_applicable(out, "本地存储没有读到任何用量记录,复核无法进行")
    if unreadable:
        out["partial_local_read"] = True
        out["coverage_warning"] = (
            f"{len(unreadable)} 个文件读取或解码失败({[u['file'] for u in unreadable][:5]});"
            "本地一边只统计了成功读取的部分,因此 mine 是下限。"
            "比对仍然有效,但 mine 小于 ccusage 时优先考虑这里的缺失,而不是口径差"
        )
    return None


def run(facts, platform, db_path=None):
    """返回复核结果;没有可信真值来源时明确说明,不猜。"""
    target = next((s for s in facts.get("sessions", []) if s.get("depth") == 0), None)
    if not target:
        return {"verdict": "not_possible", "reason": "fact pack 里没有主会话条目"}
    cc = target.get("ccusage") or {}
    out = {"platform": platform,
           "ccusage_row": {k: cc.get(k) for k in ("input", "output", "cache_read", "cache_creation",
                                                  "total", "cost", "rollouts_aggregated", "last_activity")}}
    if not cc:
        out["verdict"] = "unverified_no_ccusage"
        return _not_applicable(out, "没有可对照的 ccusage 行(采集失败、id 不匹配或该平台无输出);"
                                     "报告只能给本地存储数字并标注未与 ccusage 对齐")
    gap, gap_basis = _gap_minutes(cc.get("last_activity"))

    if platform == "opencode":
        assistant = target.get("assistant_tokens") or {}
        compaction = (target.get("compaction_summary") or {}).get("tokens") or {}
        rollup = target.get("db_rollup") or {}
        mine = {"input": (assistant.get("input") or 0), "output": (assistant.get("output") or 0),
                "cache_read": (assistant.get("cache_read") or 0), "reasoning": (assistant.get("reasoning") or 0)}
        out["mine"] = mine
        out["comparison"] = compare(mine, cc, MAPPING_OPENCODE)
        # opencode 这边没有"文件"概念,读不到 assistant 行同样等于空对空,不能判 exact
        blocked = _local_side_coverage(out, {"rows": assistant.get("rows"),
                                             "files_unreadable": []}, "opencode", cc)
        if blocked is not None:
            return blocked
        out["compaction_excluded_from_ccusage"] = compaction
        out["db_rollup"] = {k: rollup.get(k) for k in ("input", "output", "reasoning", "cache_read", "total_including_reasoning")}
        out["note"] = "mine 只含 assistant 行,应与 ccusage 基本一致(差异来自两次快照之间新增的请求)"
        return _finalize(out, gap, gap_basis)

    files = target.get("files") or []
    if platform == "claude":
        if not files:
            out["verdict"] = "not_possible"
            return _finalize(_not_applicable(out, "没有主 transcript 文件,本地没有可比的一边"), gap, gap_basis)
        mine = claude_check(files)
        out["mine"] = mine
        out["comparison"] = compare(mine, cc, MAPPING_CLAUDE)
        blocked = _local_side_coverage(out, mine, "claude", cc)
        if blocked is not None:
            return blocked
        children = [s for s in facts.get("sessions", []) if s.get("depth") == 1]
        out["subagents_total"] = {
            key: sum((c.get("usage_from_files") or {}).get(key) or 0 for c in children)
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        }
        out["note"] = ("主 transcript 只含 isSidechain:false,故 subagents 用量需另加,不能从主行里扣;"
                       "复核的独立轴是本地存储 vs ccusage,两侧共用同一份去重实现")
        return _finalize(out, gap, gap_basis)

    if platform == "codex":
        if not files:
            out["verdict"] = "not_possible"
            return _finalize(_not_applicable(out, "没有 rollout 文件,本地没有可比的一边"), gap, gap_basis)
        mine = codex_check(files)
        out["mine"] = mine
        out["comparison"] = compare(mine["aligned_to_ccusage"], cc, MAPPING_CODEX)
        blocked = _local_side_coverage(out, mine, "codex", cc)
        if blocked is not None:
            return blocked
        if mine["files_unreadable"]:
            out["coverage_warning"] = (
                f"{len(mine['files_unreadable'])} 个 rollout 文件无法解码,复核只统计了解读成功的部分;"
                "报告必须转述并说明总量可能偏低"
            )
        return _finalize(out, gap, gap_basis,
                         verdict=None if mine["files_with_token_count"] else "no_token_records")

    out["verdict"] = "unsupported"
    return _not_applicable(out, f"{platform} 没有已验证的独立求值路径")
