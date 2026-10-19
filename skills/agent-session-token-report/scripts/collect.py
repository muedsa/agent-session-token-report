#!/usr/bin/env python3
"""采集一个 agent CLI 会话及其子会话的 token 消耗事实,供撰写报告使用。

覆盖 ccusage 支持的各平台(默认一次通用 `ccusage session` 采集即可拿到全部平台的行),
再按平台补本地存储细节:opencode 走 SQLite(会话树/压缩/分时段/工具清单/时间区间),
claude 与 codex/gemini 走 JSONL(subagent 文件、fork 血缘、时间分布)。

stdout 打印的是**精简视图**(去掉与落盘文件重复的大字段、原始 ccusage 行、绝对路径);
完整事实包始终落盘 `agent-report-<id>-facts.json`,需要细节就读那个文件,或用
`--full-output` 让 stdout 打完整包。

用法:
    python collect.py <session-id> [--platform NAME] [--db PATH] [--out-dir DIR]
                      [--offline-only] [--compare-pricing] [--no-crosscheck] [--no-ccusage]
                      [--full-output] [--timeout SEC]

定价默认走实时 LiteLLM(ccusage 自身默认);--offline-only 读本地缓存,缺价时自动升级实时。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crosscheck  # noqa: E402
import jsonl_reader  # noqa: E402
import store_jsonl  # noqa: E402
import store_opencode  # noqa: E402

# 兜底清单(取自 ccusage 20.x 的 --help);只在需要校验显式 --platform 时才去实际探测
PLATFORMS = [
    "claude", "codex", "opencode", "amp", "droid", "codebuff", "hermes", "pi", "goose",
    "kilo", "copilot", "gemini", "antigravity", "kimi", "qwen", "openclaw", "grok", "zcode",
]
GENERIC_COMMANDS = {"daily", "monthly", "weekly", "session", "blocks", "statusline", "help", "version", "update"}
CCUSAGE_PKG = os.environ.get("AGENT_CCUSAGE_PKG", "ccusage@latest")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
SID_RE = re.compile(r"ses_[A-Za-z0-9]{20,34}")

# 支持分级:只有这四个平台有本地适配器,其余平台只能拿到 ccusage 的聚合数字
SUPPORT_TIERS = {
    "opencode": ("full", "SQLite 会话树、按消息类型求和、compaction 归因、分时段对账、工具清单、独立复核"),
    "claude": ("full", "subagents/sidechain 子代理发现、按 message.id 去重求和、独立复核"),
    "codex": ("full", "多 rollout 与 .zst 压缩分片覆盖、fork 血缘、逐轮 usage 去重后独立复核"),
    "gemini": ("partial", "能定位会话文件并给出时间分布,但没有独立复核路径,也未发现子会话机制"),
}
AGGREGATE_ONLY_CAVEATS = [
    "没有该平台的本地存储适配器,无法确认是否存在子会话,报告只能写“未验证”",
    "ccusage 的出行粒度未必等于一个会话(如 codex 按 rollout 文件出行),会话边界无法核对",
    "没有独立复核路径,数字无法与原始存储逐字段对照",
    "缺少会话树、消息类型分布、压缩事件、时间分布等结构性指标",
]


def support_block(platform, adapter_used):
    """给报告直接引用的支持声明,避免把不完整统计说成完整。"""
    tier, guarantees = SUPPORT_TIERS.get(platform, ("aggregate_only", ""))
    caveats = [] if tier == "full" else (
        ["没有独立复核路径,数字未与原始存储逐字段对照"] if tier == "partial" else list(AGGREGATE_ONLY_CAVEATS)
    )
    label = {"full": "完整支持", "partial": "部分支持", "aggregate_only": "仅聚合数字"}[tier]
    parts = [f"**支持范围**:本平台({platform})为{label}。"]
    if guarantees:
        parts.append(f"可保证:{guarantees}。")
    parts.extend(f"\n- 限制:{c}" for c in caveats)
    parts.append(
        "\n- 本技能只对 opencode、claude、codex、gemini 四个平台实现了本地存储适配;"
        "其余 ccusage 平台只能给出 ccusage 的聚合数字,**不保证统计完整**。"
    )
    return {"platform": platform, "tier": tier, "tier_zh": label, "guarantees": guarantees,
            "caveats": caveats, "adapter_used": adapter_used, "report_note_zh": "".join(parts)}


def npx_path():
    return shutil.which("npx") or shutil.which("npx.cmd")


def discover_platforms(timeout):
    """从 `ccusage --help` 的 COMMANDS 段读平台子命令,防止硬编码清单随版本漂移。

    这一步要跑一次 npx(可能联网解析 @latest),所以只在需要校验显式 --platform 时才调用;
    auto 路径直接从 ccusage 输出的 agent 字段得到平台集合。
    """
    npx = npx_path()
    if not npx:
        return list(PLATFORMS), "内置清单(npx 不可用)"
    try:
        proc = subprocess.run([npx, "-y", CCUSAGE_PKG, "--help"], capture_output=True, timeout=timeout,
                              text=True, encoding="utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return list(PLATFORMS), "内置清单(读取 --help 失败)"
    if "COMMANDS:" not in (proc.stdout or ""):
        return list(PLATFORMS), "内置清单(--help 无法解析)"
    body = proc.stdout.split("COMMANDS:", 1)[1].split("For more info", 1)[0]
    found = set()
    for line in body.splitlines():
        m = re.match(r"\s+([a-z][a-z0-9]*)\s{2,}\S", line)
        if m and m.group(1) not in GENERIC_COMMANDS:
            found.add(m.group(1))
    if not found:
        return list(PLATFORMS), "内置清单(--help 里没有平台项)"
    return sorted(found), "运行时读自 ccusage --help"


def emit(payload, code=0, text=None):
    print(text if text is not None else json.dumps(payload, ensure_ascii=False, indent=1))
    sys.exit(code)


def run_ccusage(offline, timeout):
    """跑通用 `ccusage session`;注意其 -i/--id 过滤经实测对 opencode/claude/codex/gemini
    都拿不到行(返回 null 或空数组),因此一律全量取回后在客户端按 period 过滤。
    包名可用 AGENT_CCUSAGE_PKG 固定版本(默认 ccusage@latest)。"""
    npx = npx_path()
    if not npx:
        return None, "npx 不在 PATH 中,无法调用 ccusage"
    cmd = [npx, "-y", CCUSAGE_PKG, "session", "--json", "--offline" if offline else "--no-offline"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True,
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, f"ccusage 超时({timeout}s)"
    except OSError as exc:
        return None, f"ccusage 无法启动: {exc}"
    if proc.returncode != 0:
        return None, f"ccusage 退出码 {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    raw = (proc.stdout or "").strip()
    if not raw or raw == "null":
        return {}, None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"ccusage 输出无法解析: {exc}"
    if not isinstance(payload, dict):
        return None, f"ccusage 输出结构异常:{type(payload).__name__}"
    return payload, None


def row_id(row):
    return row.get("period") or row.get("sessionId")


def row_keys(row):
    """一行可能要用多个键去命中:period 与 sessionId 都要登记,再加最后一段和内嵌的 UUID / ses_ id。

    有些 ccusage 行把会话标识放在 `sessionId` 而 `period` 只是日期;只看 `row_id()`
    会让这种行既进不了索引也进不了兜底扫描,整个会话从聚合里凭空消失。
    opencode 的 `ses_` id 大小写混排,所以同时登记小写形式(`entry_ids` 一律小写查询)。
    """
    keys = set()
    for raw in (row.get("period"), row.get("sessionId")):
        if not raw:
            continue
        text = str(raw)
        keys.update({text, text.rsplit("/", 1)[-1]})
        keys.update(UUID_RE.findall(text))
        keys.update(SID_RE.findall(text))
    keys = {k for k in keys if k}
    return keys | {k.lower() for k in keys}


def row_matches(row, wanted_id):
    """兜底匹配:后缀必须落在分隔符边界上,否则短 id 会误命中无关会话并计入总量。"""
    wanted = str(wanted_id).lower()
    if len(wanted) < 12:
        return False
    for raw in (row.get("period"), row.get("sessionId")):
        rid = (raw or "").lower() if isinstance(raw, str) else str(raw or "").lower()
        if not rid.endswith(wanted):
            continue
        before = rid[-len(wanted) - 1] if len(rid) > len(wanted) else ""
        if before in ("", "/", "-"):
            return True
    return False


class RowIndex:
    """把 ccusage 的全量行按多种可能的键建索引,避免每个会话都全表扫一遍。

    一个会话在 ccusage 里可能对应多行(codex 按 rollout 出行),行的 period
    又可能是 `日期/rollout-...-<uuid>` 这种带前缀的形态,所以同时按完整 period、
    最后一段、内嵌 UUID / ses_ id 建键。
    """

    def __init__(self, rows):
        self.rows = list(rows)
        self.by_key = defaultdict(list)
        for row in self.rows:
            for key in row_keys(row):
                self.by_key[key].append(row)

    def matching(self, wanted):
        """索引命中与后缀兜底都要跑:非 UUID / 非 ses_ 形态的 id 可能只作为长 period 的后缀出现。"""
        hits = list(self.by_key.get(wanted) or self.by_key.get(str(wanted).lower()) or [])
        seen = {id(row) for row in hits}
        for row in self.rows:
            if id(row) not in seen and row_matches(row, wanted):
                seen.add(id(row))
                hits.append(row)
        return hits


def collect_rows(index, wanted_ids):
    """返回这些 id 对应的所有行(按对象去重,保持原顺序)。"""
    picked, seen = [], set()
    for want in wanted_ids:
        for row in index.matching(want):
            if id(row) not in seen:
                seen.add(id(row))
                picked.append(row)
    return picked


_num = store_jsonl.to_number


def _sum(value):
    """求和用的强制转换:缺失按 0 参与,但缺失本身要由调用方单独记录。"""
    return _num(value) or 0


def _field_sum(rows, pick):
    """多行求和:缺失的行不参与,但要说清有几行缺这个字段。

    过去这里把缺失静默当 0,于是同一个字段在单行路径显示 None、
    在多行路径显示一个看起来很精确的数字,两种语义无法区分。
    """
    values = [_num(pick(r)) for r in rows]
    present = [v for v in values if v is not None]
    if not present:
        return None, len(values)
    return sum(present), len(values) - len(present)


def _latest_activity(rows):
    """多行取最近一次活动:先按可解析的时间基准比,解析不了才按字符串比,绝不混类型比较。"""
    values = [(r.get("metadata") or {}).get("lastActivity") for r in rows]
    values = [v for v in values if v not in (None, "")]
    if not values:
        return None
    keyed = []
    for value in values:
        stamp, _ = crosscheck._parse_stamp(value)
        keyed.append((stamp.timestamp() if stamp else 0.0, str(value)))
    return max(keyed)[1]


def summarize(row):
    meta = row.get("metadata") or {}
    return {
        "session_id": row_id(row),
        "platform": row.get("agent"),
        "input": _num(row.get("inputTokens")),
        "output": _num(row.get("outputTokens")),
        "cache_read": _num(row.get("cacheReadTokens")),
        "cache_creation": _num(row.get("cacheCreationTokens")),
        "total": _num(row.get("totalTokens")),
        "cost": _num(row.get("totalCost")),
        "models": row.get("modelsUsed"),
        "reasoning_output_tokens": _num(meta.get("reasoningOutputTokens")),
        "last_activity": meta.get("lastActivity"),
        "missing_pricing": any(b.get("missingPricing") for b in (row.get("modelBreakdowns") or [])),
    }


def aggregate_rows(rows, session_id, platform):
    """把同一会话的多行相加,并说明聚合了几个 rollout。"""
    if not rows:
        return None
    if len(rows) == 1:
        out = summarize(rows[0])
        out["rollouts_aggregated"] = 1
        return out
    spec = (
        ("input", lambda r: r.get("inputTokens")),
        ("output", lambda r: r.get("outputTokens")),
        ("cache_read", lambda r: r.get("cacheReadTokens")),
        ("cache_creation", lambda r: r.get("cacheCreationTokens")),
        ("total", lambda r: r.get("totalTokens")),
        ("cost", lambda r: r.get("totalCost")),
        ("reasoning_output_tokens", lambda r: (r.get("metadata") or {}).get("reasoningOutputTokens")),
    )
    fields, missing = {}, {}
    for out_key, pick in spec:
        value, missing_rows = _field_sum(rows, pick)
        fields[out_key] = value
        if missing_rows:
            missing[out_key] = missing_rows
    return {
        "session_id": session_id,
        "platform": platform,
        "models": sorted({m for r in rows for m in (r.get("modelsUsed") or [])}),
        "last_activity": _latest_activity(rows),
        "aggregated_missing": missing or None,
        "missing_pricing": any(
            b.get("missingPricing") for r in rows for b in (r.get("modelBreakdowns") or [])
        ),
        "rollouts_aggregated": len(rows),
        "rollout_ids": [row_id(r) for r in rows],
        **fields,
    }


def entry_ids(entry):
    """一个会话条目可能同时用会话 id、原始请求 id、各 rollout 文件名里的 id 命中 ccusage 行。"""
    ids = {entry["session_id"]}
    if entry.get("requested_id"):
        ids.add(entry["requested_id"])
    ids.update(u for u in (entry.get("rollout_uuids") or []) if u)
    return {str(i).lower() for i in ids if i}


def attach_ccusage(entry, index, platform, basis=None):
    rows = collect_rows(index, entry_ids(entry))
    entry["ccusage"] = aggregate_rows(rows, entry["session_id"], platform) if rows else None
    if basis:
        entry["ccusage_basis"] = basis
    return entry["ccusage"]


def store_opencode_found(db, session_id):
    """只做存在性探测:不触发 db+wal 复制回退(那可能是上百 MB)。"""
    try:
        con, tmp = store_opencode.open_db(db, copy_fallback=False)
    except Exception:
        return False
    try:
        return store_opencode.exists(con, session_id)
    except Exception:
        return False
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def resolve_platform(requested, session_id, rows, db):
    """判定平台;命中本地适配器时把已构建的 store 一起返回,避免上游重复全量索引。"""
    if requested != "auto":
        return requested, f"显式指定 --platform {requested}", None
    if rows:
        platform = rows[0].get("agent")
        if platform:
            return platform, "取自 ccusage 行的 agent 字段", None
    if session_id.startswith("ses_"):
        return "opencode", "id 形如 ses_", None
    if store_opencode_found(db, session_id):
        return "opencode", "存在于 opencode.db", None
    for platform in ("claude", "codex", "gemini"):
        store = store_jsonl.ADAPTERS[platform](session_id)
        if store.get("files"):
            return platform, f"在 {platform} 本地存储里找到该会话文件", store
    return "unknown", "ccusage 与本地存储都没定位到平台", None


def pricing_verdict_for(rows, offline_only, missing):
    if not rows:
        return "no_ccusage_rows"
    if missing:
        return "unpriced_everywhere"
    return "priced_offline_cache" if offline_only else "priced_online"


def pricing_block(wanted_ids, rows, offline_only, timeout, compare):
    cc = {"pricing_source": "offline_cache" if offline_only else "litellm_online", "primary_rows": rows}
    missing = [r for r in rows if any(b.get("missingPricing") for b in (r.get("modelBreakdowns") or []))]
    escalated = None
    if offline_only and missing:
        other, err = run_ccusage(False, timeout)
        if err:
            cc["escalation_error"] = err
            cc["pricing_verdict"] = "offline_missing_escalation_failed"
        else:
            escalated = collect_rows(RowIndex(other.get("session", [])), wanted_ids)
            if not escalated:
                # 升级实时后一行都没拿到,不能说"已按实时价计价"
                cc["pricing_verdict"] = "offline_missing_escalation_empty"
                cc["escalation_note"] = "实时采集没有返回该会话的任何行,定价状态未知,需重跑或换 --offline-only 对照"
            else:
                still = [r for r in escalated if any(b.get("missingPricing") for b in (r.get("modelBreakdowns") or []))]
                cc["escalated_rows"] = escalated
                cc["pricing_verdict"] = "unpriced_everywhere" if still else "priced_online_only"
    else:
        cc["pricing_verdict"] = pricing_verdict_for(rows, offline_only, missing)
    if compare:
        other, err = run_ccusage(not offline_only, timeout)
        if err:
            cc["compare_error"] = err
        else:
            cc["comparison_rows"] = collect_rows(RowIndex(other.get("session", [])), wanted_ids)
            cc["comparison_source"] = "offline_cache" if not offline_only else "litellm_online"
            if not cc["comparison_rows"]:
                cc["compare_note"] = "另一种定价来源没有返回该会话的行,无法核对缓存是否过期"
            else:
                base_cost = sum(_sum(r.get("totalCost")) for r in rows)
                other_cost = sum(_sum(r.get("totalCost")) for r in cc["comparison_rows"])
                cc["cost_delta_between_sources"] = round(base_cost - other_cost, 6)
    source_rows = escalated or rows
    cc["unpriced_models"] = sorted({
        b.get("modelName") for r in source_rows for b in (r.get("modelBreakdowns") or []) if b.get("missingPricing")
    })
    return source_rows, cc


def _basename_list(paths):
    return [os.path.basename(p) for p in (paths or [])]


def _slim_rows(rows, cap=5):
    """原始行太多时只保留前几行样例,其余给个数 —— 分片明细在落盘文件里仍然完整。

    样例里也去掉 session_id:它基本是把 rollout 文件名再抄一遍,而文件名已经出现在
    sessions[].files_basenames 里了。
    """
    slimmed = []
    for row in rows or []:
        item = summarize(row)
        item.pop("session_id", None)
        slimmed.append(item)
    if len(slimmed) > cap:
        return slimmed[:cap] + [{"omitted_rows": len(slimmed) - cap, "see": "fact_pack_written_to"}]
    return slimmed


def compact_view(facts):
    """stdout 用的视图:只去掉体积大且对成文无用的重复内容,报告要引用的字段一律保留。

    去掉的是:原始 ccusage 行(改为限量样例)、绝对路径(改为 root + 文件名)、
    sessions 与 local_store 之间重复的会话级字段、逐分片的 rollout id 列表
    (报告只需要"聚合了几个分片"这个数和文件名)。落盘文件不受影响。
    """
    slim = {}
    local = facts.get("local_store") or {}
    for key, value in facts.items():
        if key in ("local_store", "ccusage", "sessions"):
            continue
        slim[key] = value
    if local:
        slim["local_store"] = {k: local[k] for k in (
            "store", "root", "session_uuid", "rollout_files_total", "rollout_compressed_total",
            "compression_backend", "compression_warning", "sessions_indexed", "rollouts_unreadable",
            "rollouts_without_session_meta", "children_mechanism", "children_verified",
            "files_attribution_verified", "subagent_markers", "subagent_dirs_scanned",
            "requested_id_form", "session_id", "parent_session_id",
            "multi_rollout_note", "note", "matched_by_full_id",
        ) if k in local}
        if local.get("candidate_files"):
            slim["local_store"]["candidate_files_basenames"] = _basename_list(local["candidate_files"])
    cc = facts.get("ccusage")
    if isinstance(cc, dict):
        slim["ccusage"] = {k: v for k, v in cc.items()
                           if k not in ("primary_rows", "escalated_rows", "comparison_rows")}
        slim["ccusage"]["primary_rows_slim"] = _slim_rows(cc.get("primary_rows"))
        for src, dst in (("escalated_rows", "escalated_rows_slim"), ("comparison_rows", "comparison_rows_slim")):
            if cc.get(src) is not None:
                slim["ccusage"][dst] = _slim_rows(cc[src])
    sessions = []
    for entry in facts.get("sessions", []):
        kept = dict(entry)
        if kept.get("files"):
            kept["files_basenames"] = _basename_list(kept.pop("files"))
        if isinstance(kept.get("ccusage"), dict):
            kept["ccusage"] = {k: v for k, v in kept["ccusage"].items() if k != "rollout_ids"}
        sessions.append(kept)
    slim["sessions"] = sessions
    slim["_stdout_is_compact"] = (
        "stdout 去掉了原始 ccusage 行、逐分片 rollout id、绝对路径和 sessions 与 local_store 的重复字段;"
        "完整内容在 fact_pack_written_to 指向的文件里,也可用 --full-output 重跑"
    )
    return slim


ACTIVE_WINDOW_MIN = 5.0
# 只在本机实测存在的标记上判定;其它 CLI 的环境标记未经本仓库验证,不做猜测性检测
AGENT_PROCESS_MARKERS = ("QODERCN_CLI", "QODER_AGENT_SDK_ENTRYPOINT")


def self_reference_guard(facts):
    """目标会话"此刻仍在写入"时给出自指告警。

    在被分析的会话里运行本脚本会自我污染:那条提问与随后的工具调用先写进会话存储,
    再被同一份脚本统计进去,于是报告统计的是"测量这次测量的开销";同时 fact pack 与
    报告正文还会继续撑大这个会话的上下文。两次快照必然错开,`snapshot_skew_likely`
    这类判定也就失去了意义。
    """
    gap = (facts.get("crosscheck") or {}).get("minutes_since_last_activity")
    if gap is None or gap < 0 or gap > ACTIVE_WINDOW_MIN:
        return
    markers = [name for name in AGENT_PROCESS_MARKERS if os.environ.get(name)]
    facts["self_reference_suspected"] = True
    message = (
        f"目标会话在读取前 {gap:.1f} 分钟内仍在写入。若这次采集正跑在被分析的那个会话里,"
        "报告会把\u201c测量这次测量\u201d的 token 与成本算进该会话,复核偏差也必然来自快照错开;"
        "请改在另一个会话或普通终端里对该会话出报告。确认可接受时加 --allow-live-target 关闭本提示"
    )
    if markers:
        message += f"(当前进程带有 agent CLI 标记:{', '.join(markers)})"
    facts.setdefault("coverage_warnings", []).append(message)
    facts["coverage_warning"] = " | ".join(dict.fromkeys(facts["coverage_warnings"]))


def main():
    parser = argparse.ArgumentParser(description="采集 agent CLI 会话及其子会话的 token 事实",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("session_id")
    parser.add_argument("--platform", default="auto", help="agent 平台名;auto 表示先问 ccusage 再翻本地存储")
    parser.add_argument("--db", default=store_opencode.DEFAULT_DB, help="opencode.db 路径")
    parser.add_argument("--out-dir", default=os.getcwd(), help="保存 fact pack 的目录")
    parser.add_argument("--timeout", type=int, default=420, help="单次 ccusage 调用超时秒数")
    parser.add_argument("--offline-only", action="store_true", help="用本地缓存定价(快、不联网),缺价时自动升级实时")
    parser.add_argument("--compare-pricing", action="store_true", help="额外跑另一种定价来源,核对缓存是否过期")
    parser.add_argument("--no-crosscheck", action="store_true", help="跳过独立复核(默认对 opencode/claude/codex 自己算一份并与 ccusage 对照)")
    parser.add_argument("--no-ccusage", action="store_true", help="只读本地存储,不调用 ccusage")
    parser.add_argument("--full-output", action="store_true", help="stdout 打完整事实包而不是精简视图")
    parser.add_argument("--allow-live-target", action="store_true",
                        help="目标会话仍在写入时不提示自指污染(在别的会话里分析刚结束的会话时使用)")
    args = parser.parse_args()

    sid = args.session_id.strip()
    if args.no_ccusage:
        known_platforms, platforms_source = list(PLATFORMS), "内置清单(本次未调用 ccusage)"
    elif args.platform != "auto":
        known_platforms, platforms_source = discover_platforms(args.timeout)
    else:
        known_platforms, platforms_source = list(PLATFORMS), "内置清单(auto 判定不需要 --help;实际平台集合见 ccusage_rows 的 agent 字段)"
    facts = {
        "captured_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "requested_session_id": sid,
        "platform_requested": args.platform,
        "ccusage_platforms": known_platforms,
        "ccusage_platforms_source": platforms_source,
        "zstd_backend": jsonl_reader.zstd_backend(),
        "adapters": {"opencode": "SQLite 深度适配", "claude": "subagents/sidechain",
                     "codex": "rollout+fork 血缘(.zst 已支持)", "gemini": "chat 文件"},
    }
    if args.platform != "auto" and args.platform not in known_platforms:
        facts["platform_warning"] = f"--platform {args.platform} 不在已知清单({platforms_source}),仍按该值尝试"

    all_rows, target_rows, cc_error = [], [], False
    index = RowIndex([])
    if not args.no_ccusage:
        payload, err = run_ccusage(args.offline_only, args.timeout)
        if err:
            cc_error = True
            facts["ccusage"] = {"error": err, "pricing_verdict": "pricing_unavailable"}
        else:
            all_rows = payload.get("session", [])
            index = RowIndex(all_rows)
            facts["ccusage_rows_scanned"] = len(all_rows)
            facts["ccusage_agents_seen"] = sorted({r.get("agent") for r in all_rows if r.get("agent")})
            target_rows = collect_rows(index, {sid})
            if not all_rows:
                facts["ccusage_warning"] = "ccusage 通用 session 输出为空:本机可能没有该平台的任何用量数据"
            elif not target_rows:
                facts["ccusage_warning"] = (
                    f"全量输出 {len(all_rows)} 行里没有 id 匹配 {sid} 的行;"
                    "确认 id 是否属于本机、或平台是否被 ccusage 支持"
                )

    prebuilt = None
    platform, why, prebuilt = resolve_platform(args.platform, sid, target_rows, args.db)
    facts["platform"] = platform
    facts["platform_resolution"] = why

    sessions = []
    tree_ids = {sid}
    if platform == "opencode":
        try:
            deep = store_opencode.analyze(args.db, sid)
        except Exception as exc:
            emit({"error": f"opencode 存储不可用:{type(exc).__name__}: {exc}", "session_id": sid,
                  "platform": platform, "store_error": True, "db": args.db,
                  "hint": "库被占用/损坏/不是 SQLite 时走这个分支,与「会话不存在」是两回事;"
                          "确认路径与权限后重跑,必要时先复制 db 再 --db 指向副本"}, 2)
        facts["opencode"] = deep
        if not deep.get("found"):
            emit({"error": "opencode.db 中不存在该会话", "session_id": sid, "platform": platform,
                  "hint": "确认 id,或用 --db 指定数据库", "recent_sessions": deep.get("recent_sessions", []),
                  "ccusage_rows": [summarize(r) for r in target_rows]}, 3)
        for entry in deep["sessions"]:
            tree_ids.add(entry["session_id"])
            attach_ccusage(entry, index, "opencode")
            sessions.append(entry)
        facts["has_children"] = deep["has_children"]
        facts["children_verified"] = True
        for entry in deep["sessions"]:
            if entry.get("unparsable_message_rows"):
                facts.setdefault("coverage_warnings", []).append(
                    f"会话 {entry['session_id']} 有 {entry['unparsable_message_rows']} 条消息的 data 无法按 JSON 对象解析,相关汇总可能有缺失"
                )
    elif platform in store_jsonl.ADAPTERS:
        store = prebuilt if prebuilt is not None else store_jsonl.ADAPTERS[platform](sid)
        facts["store_reused_from_platform_probe"] = prebuilt is not None
        facts["local_store"] = store
        extras = store_jsonl.describe(platform, sid, store.get("files"))
        facts["local_store_extras"] = extras
        canonical = store.get("session_id") or sid
        files = store.get("files") or []
        entry = {
            "session_id": canonical, "platform": platform, "depth": 0,
            "requested_id": sid if canonical != sid else None,
            "files": files,
            "bytes": sum(filter(None, (store_jsonl.file_size(f) for f in files))),
            "compressed_files": extras.get("compressed_files"),
            "rollout_uuids": store.get("rollout_uuids"),
            "parent_session_id": store.get("parent_session_id"),
            "session_meta": store.get("session_meta"),
            "subagent_markers": store.get("subagent_markers"),
            "multi_rollout_note": store.get("multi_rollout_note"),
            "timing": extras.get("timing"),
            "lines": extras.get("lines"),
            "unreadable_files": extras.get("unreadable_files"),
        }
        attach_ccusage(entry, index, platform)
        # 分片计数直接取复核用的那份聚合,避免两条路径用不同的 id 集合算出不同的数
        entry["ccusage_rollouts_aggregated"] = (entry.get("ccusage") or {}).get("rollouts_aggregated", 0)
        tree_ids |= entry_ids(entry)
        sessions.append(entry)
        for child in store.get("children", []):
            cid = child.get("session_id") or os.path.basename(child.get("file") or (child.get("files") or [""])[0])
            tree_ids.add(cid)
            c_files = child.get("files") or ([child["file"]] if child.get("file") else [])
            entry = {
                "session_id": cid, "platform": platform, "depth": 1, "kind": child.get("kind"),
                "files": c_files, "bytes": sum(filter(None, (store_jsonl.file_size(f) for f in c_files))),
                "rollout_uuids": child.get("rollout_uuids"),
                "agent_type": child.get("agent_type"), "description": child.get("description"),
                "spawn_depth": child.get("spawn_depth"), "parent_session_id": sid,
                "usage_from_files": child.get("usage"),
                "timing": child.get("timing"),
            }
            attach_ccusage(entry, index, platform)
            # 子会话的 rollout id 也要进树:否则缺价判定看不见子分片,
            # 而一旦升级实时,esc_index 里没有这些行,子会话数字会由有变无 —— 静默少算
            tree_ids |= entry_ids(entry)
            if platform != "claude" and c_files:
                total_bytes = entry["bytes"] or 0
                if total_bytes <= 40_000_000:
                    entry["timing"] = store_jsonl.time_profile_many(c_files)
                else:
                    entry["timing_note"] = f"子会话文件共 {total_bytes:,} 字节,超过 40MB 阈值,未做时间分布扫描"
            if (entry.get("usage_from_files") or {}).get("error"):
                facts.setdefault("coverage_warnings", []).append(
                    f"子会话 {cid} 的文件无法解码,其 token 记为 0,family_totals 因此偏低"
                )
            sessions.append(entry)
        facts["children_verified"] = bool(store.get("children_verified"))
        facts["has_children"] = bool(store.get("children")) if store.get("children_verified") else None
        if facts["has_children"] is None:
            facts.setdefault("scope_notes", []).append(
                f"{platform} 的子会话是否存在没有可核查的依据(gemini 未发现派生机制;"
                "codex 传入的是 rollout id 时按文件名兜底),报告第 3 节必须写「未验证」"
            )
        facts["children_mechanism"] = store.get("children_mechanism")
        if store.get("compression_warning"):
            facts.setdefault("coverage_warnings", []).append(store["compression_warning"])
        if extras.get("unreadable_files"):
            facts.setdefault("coverage_warnings", []).append(
                f"主会话有 {len(extras['unreadable_files'])} 个文件无法读取/解码:{extras['unreadable_files']}"
            )
        if store.get("note"):
            facts.setdefault("scope_notes", []).append(store["note"])
    else:
        entry = {"session_id": sid, "platform": platform, "depth": 0}
        attach_ccusage(entry, index, platform)
        sessions.append(entry)
        facts["has_children"] = None
        facts["children_verified"] = False
        facts["children_note"] = f"{platform} 没有本地存储适配器:只能给出 ccusage 数字,子会话情况未验证,不要声称没有子会话"

    facts["sessions"] = sessions

    if not cc_error and not args.no_ccusage:
        pricing_rows = collect_rows(index, tree_ids) or target_rows
        _, cc = pricing_block(tree_ids, pricing_rows, args.offline_only, args.timeout,
                              args.compare_pricing)
        cc["pricing_rows_for"] = sorted(tree_ids)
        facts["ccusage"] = cc
        if cc.get("escalated_rows"):
            # 升级到实时定价后,整棵树都要用升级后的行重算,否则父行是实时、子行仍是缓存,
            # family_totals 会把两种口径混在一起而 basis 仍显示 "ccusage"
            esc_index = RowIndex(cc["escalated_rows"])
            for entry in sessions:
                attach_ccusage(entry, esc_index, platform, basis="litellm_online")
                # 出行数必须跟着重算:否则同一 entry 里 ccusage_rollouts_aggregated 还是离线口径的数,
                # 与 ccusage.rollouts_aggregated 自相矛盾,少掉的分片没有任何标记 —— 静默少算
                if entry.get("depth") == 0 and "ccusage_rollouts_aggregated" in entry:
                    entry["ccusage_rollouts_aggregated"] = (entry.get("ccusage") or {}).get(
                        "rollouts_aggregated", 0)
            cc["escalation_reapplied_to"] = len(sessions)

    family = {"per_session": [], "mixed_basis": False}
    bases = set()
    for entry in sessions:
        numbers, basis = None, None
        if entry.get("ccusage"):
            numbers, basis = entry["ccusage"], entry.get("ccusage_basis") or "ccusage"
        elif entry.get("usage_from_files") and not entry["usage_from_files"].get("error"):
            u = entry["usage_from_files"]
            numbers = {
                "input": u["input_tokens"], "output": u["output_tokens"],
                "cache_read": u["cache_read_input_tokens"], "cache_creation": u["cache_creation_input_tokens"],
                "total": sum(u.get(k) or 0 for k in store_jsonl.CLAUDE_TOKEN_KEYS), "cost": None,
            }
            basis = "file_dedup_sum"
        if numbers:
            bases.add(basis)
            picked = {k: numbers.get(k) for k in ("input", "output", "cache_read", "cache_creation",
                                                  "total", "cost", "reasoning_output_tokens",
                                                  "rollouts_aggregated")}
            family["per_session"].append({"session_id": entry.get("session_id"),
                                          "depth": entry.get("depth"), "basis": basis, **picked})
    family["mixed_basis"] = len(bases) > 1
    family["bases"] = sorted(bases)
    sums, sum_missing, sum_unavailable = {}, {}, []
    for key in ("input", "output", "cache_read", "cache_creation", "total"):
        values = [_num(item.get(key)) for item in family["per_session"]]
        present = [v for v in values if v is not None]
        sums[key] = sum(present) if present else None
        if not present:
            if family["per_session"]:
                sum_unavailable.append(key)
        elif len(present) < len(values):
            sum_missing[key] = len(values) - len(present)
    if family["per_session"]:
        family["sum"] = sums
        if sum_missing:
            family["sum_missing_sessions"] = sum_missing
            facts.setdefault("coverage_warnings", []).append(
                "family_totals.sum 里有些会话缺该字段,合计只包含有数的会话,报告要写成下限"
            )
        if sum_unavailable:
            family["sum_unavailable_fields"] = sum_unavailable
            facts.setdefault("coverage_warnings", []).append(
                f"字段 {sum_unavailable} 在任何一行里都没有数值,合计是 null 而不是 0,报告要写未知"
            )
    costs = [_num(item.get("cost")) for item in family["per_session"]]
    known_costs = [c for c in costs if c is not None]
    if known_costs:
        family["cost_sum"] = sum(known_costs)
        family["cost_rows"] = len(known_costs)
        if len(known_costs) < len(costs):
            family["cost_missing_rows"] = len(costs) - len(known_costs)
            facts.setdefault("coverage_warnings", []).append(
                f"成本合计只覆盖 {len(known_costs)} 个有成本数值的会话,"
                f"另有 {len(costs) - len(known_costs)} 个没有该字段,cost_sum 是下限"
            )
    else:
        family["cost_sum"] = None
        family["cost_note"] = "所有行都没有成本数值,不是 $0"
    missing_rows = [e.get("session_id") for e in sessions
                    if not e.get("ccusage") and not (e.get("usage_from_files") and not e["usage_from_files"].get("error"))]
    if missing_rows:
        family["sessions_without_numbers"] = missing_rows
        family["sum_is_partial"] = True
        facts.setdefault("coverage_warnings", []).append(
            f"{len(missing_rows)} 个会话既没有 ccusage 行也没有可用的文件用量,合计只覆盖其余会话,报告必须说明总量偏低"
        )
    facts["family_totals"] = family

    facts["support"] = support_block(platform, bool(facts.get("opencode") or facts.get("local_store")))
    facts["supported_platforms"] = {k: v[0] for k, v in SUPPORT_TIERS.items()}
    adapter_failures = store_jsonl.read_failures()
    if adapter_failures:
        facts.setdefault("coverage_warnings", []).extend(
            f"文件 {f['file']} 读取失败:{f['reason']}" for f in adapter_failures[:10]
        )
    if facts.get("coverage_warnings"):
        facts["coverage_warning"] = " | ".join(dict.fromkeys(facts["coverage_warnings"]))

    if not args.no_crosscheck:
        try:
            facts["crosscheck"] = crosscheck.run(facts, platform)
        except Exception as exc:
            facts["crosscheck"] = {"error": f"{type(exc).__name__}: {exc}", "verdict": "failed",
                                   "reason": "复核本身异常失败,报告必须写未复核,并附此错误定位"}
    if not args.allow_live_target:
        self_reference_guard(facts)

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"agent-report-{sid}-facts.json")
    facts["fact_pack_written_to"] = path
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=1)
    if args.full_output:
        emit(facts, 0)
    emit(facts, 0, json.dumps(compact_view(facts), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        sys.exit(1)
