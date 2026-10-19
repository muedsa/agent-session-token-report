#!/usr/bin/env python3
"""opencode 的本地存储适配器:SQLite 里的会话树、按类型 token 求和、压缩明细、分时段、工具清单、时间区间。

只读打开 opencode.db;默认 mode=ro,打不开时(仅 analyze)把 db+wal 复制到临时目录再读,
探测性的查询不走复制回退,避免为了"这个 id 在不在库里"就复制上百 MB。

每个会话只做一次 session_message 扫描:同一批行同时产出去向分类型汇总、compaction 明细、
分时段、峰值、工具清单和消息时间区间 —— 过去这些是 7 条各自扫全表的 SQL,
每条还要对每行重复做 json_extract。
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime

TOOL_RE = re.compile(r'"type":"tool","id":"[^"]*","name":"([^"]+)"')
SPAWN_RE = re.compile(r'"name":"(task|agents|agent|subagent|execute)"')
SID_RE = re.compile(r"ses_[A-Za-z0-9]{20,34}")
DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
LONG_GAP_MS = 10 * 60 * 1000
SPAWN_NAMES = ("task", "agents", "agent", "subagent", "execute")


def ts(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def show(ms):
    sec = int(ms // 1000)
    return f"{sec // 3600}h{(sec % 3600) // 60}m{sec % 60}s"


def open_db(path, copy_fallback=True):
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 opencode 数据库: {path}")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("SELECT count(*) FROM session_v2")
        return con, None
    except sqlite3.Error as first_err:
        if not copy_fallback:
            raise
        tmp = tempfile.mkdtemp(prefix="opencode-ro-")
        try:
            for suffix in ("", "-wal", "-shm"):
                src = path + suffix
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(tmp, "oc.db" + suffix))
            con = sqlite3.connect(os.path.join(tmp, "oc.db"))
            con.execute("PRAGMA query_only=ON")
            con.execute("SELECT count(*) FROM session_v2")
            return con, tmp
        except sqlite3.Error as second_err:
            shutil.rmtree(tmp, ignore_errors=True)
            raise sqlite3.Error(f"{path} 只读失败({first_err}),副本也失败({second_err})")


def fetch_tree(con, session_id):
    return con.execute(
        """
        WITH RECURSIVE tree(id, depth) AS (
            SELECT ?, 0
            UNION ALL
            SELECT s.id, t.depth + 1 FROM session_v2 s JOIN tree t ON s.parent_id = t.id
        )
        SELECT s.id, t.depth, s.slug, s.title, s.agent, s.model, s.directory, s.version,
               s.parent_id, s.fork_session_id, s.fork_boundary, s.metadata,
               s.cost, s.tokens_input, s.tokens_output, s.tokens_reasoning,
               s.tokens_cache_read, s.tokens_cache_write,
               s.time_created, s.time_updated, s.time_idle
        FROM tree t JOIN session_v2 s ON s.id = t.id
        ORDER BY t.depth, s.time_created
        """,
        (session_id,),
    ).fetchall()


def exists(con, session_id):
    return bool(con.execute("SELECT 1 FROM session_v2 WHERE id=? LIMIT 1", (session_id,)).fetchone())


def _walk_tools(node, out):
    """递归找 {"type":"tool", "name": ...};不依赖键序与序列化空白。"""
    if isinstance(node, dict):
        if node.get("type") == "tool" and isinstance(node.get("name"), str):
            out.append(node["name"])
        for value in node.values():
            _walk_tools(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_tools(item, out)


def session_scan(con, sid):
    """一次扫描该会话的全部 session_message,产出所有派生指标。"""
    by_type_all, by_type_tokens = {}, {}
    hourly = defaultdict(lambda: {"rows": 0, "input": 0, "cache_read": 0, "output": 0})
    peak_values = defaultdict(list)
    compaction_events, intervals, tools, refs, spawnish = [], [], Counter(), Counter(), Counter()
    unparsable = 0
    row_count = 0
    for rtype, created, data in con.execute(
        "SELECT type, time_created, data FROM session_message WHERE session_id=? ORDER BY time_created", (sid,)
    ):
        row_count += 1
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed, unparsable = None, unparsable + 1
        if not isinstance(parsed, dict):
            # data 是数组/裸字符串等合法 JSON 时同样不能当成对象取字段,否则会 AttributeError
            # 并把整条采集链崩掉;这里计入 unparsable 并保持该行只贡献行数。
            if parsed is not None:
                unparsable += 1
            parsed = None
        tokens = parsed.get("tokens") if isinstance(parsed, dict) else None
        if not isinstance(tokens, dict):
            tokens = {}
        has_tokens = isinstance(tokens.get("input"), (int, float))
        buckets = [by_type_all] + ([by_type_tokens] if has_tokens else [])
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        for bucket in buckets:
            agg = bucket.setdefault(rtype, {"rows": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0})
            agg["rows"] += 1
            for key in ("input", "output", "reasoning", "cache_read"):
                value = cache.get("read") if key == "cache_read" else tokens.get(key)
                if isinstance(value, (int, float)):
                    agg[key] += value
        if rtype == "compaction":
            compaction_events.append({
                "at": ts(created), "input": tokens.get("input") or 0, "output": tokens.get("output") or 0,
                "reasoning": tokens.get("reasoning") or 0, "cache_read": cache.get("read") or 0,
            })
        if rtype == "assistant" and has_tokens:
            hour = datetime.fromtimestamp(created / 1000).strftime("%H") if created else "??"
            slot = hourly[hour]
            slot["rows"] += 1
            slot["input"] += tokens.get("input") or 0
            slot["output"] += tokens.get("output") or 0
            slot["cache_read"] += cache.get("read") or 0
            for key in ("cache_read", "input", "reasoning", "output"):
                value = cache.get("read") if key == "cache_read" else tokens.get(key)
                if isinstance(value, (int, float)):
                    peak_values[key].append(value)
        if rtype == "assistant":
            if parsed is not None:
                names = []
                _walk_tools(parsed, names)
            else:
                names = TOOL_RE.findall(data or "")
                spawnish.update(SPAWN_RE.findall(data or ""))
            tools.update(names)
            spawnish.update(name for name in names if name in SPAWN_NAMES)
            refs.update(SID_RE.findall(data or ""))
        if isinstance(parsed, dict):
            when = parsed.get("time") or {}
            start = when.get("created", created)
            end = when.get("completed") or when.get("updated") or start
            if isinstance(start, int) and isinstance(end, int) and end > start:
                intervals.append([start, end])
    return {
        "rows": row_count,
        "unparsable_rows": unparsable,
        "tokens_by_type": by_type_tokens,
        "tokens_by_type_all": by_type_all,
        "hourly": [
            {"hour": key, **hourly[key]} for key in sorted(hourly)
        ],
        "peaks": {
            "max_cache_read": max(peak_values["cache_read"], default=None),
            "avg_cache_read": round(sum(peak_values["cache_read"]) / len(peak_values["cache_read"])) if peak_values["cache_read"] else 0,
            "max_input": max(peak_values["input"], default=None),
            "avg_input": round(sum(peak_values["input"]) / len(peak_values["input"])) if peak_values["input"] else 0,
            "max_reasoning": max(peak_values["reasoning"], default=None),
            "max_output": max(peak_values["output"], default=None),
            "sampled_rows": len(peak_values["input"]),
        },
        "compaction_events": compaction_events,
        "tools": dict(tools.most_common()),
        "refs": dict(refs),
        "spawnish": dict(spawnish.most_common()),
        "intervals": intervals,
    }


def timing_profile(intervals):
    if not intervals:
        return {"note": "没有可用的消息时间区间"}
    intervals = sorted(intervals)
    merged = [intervals[0][:]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = [(nxt[0] - prev[1], nxt[0]) for prev, nxt in zip(merged, merged[1:]) if nxt[0] > prev[1]]
    elapsed = merged[-1][1] - merged[0][0]
    active = sum(e - s for s, e in merged)
    return {
        "first_interval": f"{ts(merged[0][0])} -> {ts(merged[0][1])}",
        "last_interval": f"{ts(merged[-1][0])} -> {ts(merged[-1][1])}",
        "elapsed_human": show(elapsed),
        "active_human": show(active),
        "active_note": "取每条消息 created->completed 的区间并集,含模型响应与工具执行",
        "active_pct_of_elapsed": round(100 * active / elapsed, 1) if elapsed else None,
        "idle_human": show(sum(g[0] for g in gaps)),
        "gap_count": len(gaps),
        "long_gaps_ge_10min": [{"starts_at": ts(g[1]), "duration": show(g[0])} for g in sorted(gaps, reverse=True)[:5]],
    }


def analyze(db_path, session_id):
    """返回 {found, sessions[], has_children};会话不存在时 found=False 并附最近会话。"""
    con, tmp = open_db(db_path)
    try:
        rows = fetch_tree(con, session_id)
        if not rows:
            near = con.execute(
                "SELECT id, title, datetime(time_created/1000,'unixepoch','localtime') "
                "FROM session_v2 ORDER BY time_updated DESC LIMIT 12"
            ).fetchall()
            return {
                "found": False,
                "db": os.path.abspath(db_path),
                "recent_sessions": [{"id": i, "title": t, "created": c} for i, t, c in near],
            }
        ids = {r[0] for r in rows}
        sessions = []
        for r in rows:
            sid = r[0]
            scan = session_scan(con, sid)
            with_tokens = scan["tokens_by_type"]
            empty = {"rows": 0, "input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
            assistant = with_tokens.get("assistant", empty)
            compaction = with_tokens.get("compaction", empty)
            hour_rows = scan["hourly"]
            entry = {
                "session_id": sid,
                "platform": "opencode",
                "depth": r[1],
                "slug": r[2],
                "title": r[3],
                "agent": r[4],
                "model_raw": r[5],
                "directory": r[6],
                "version": r[7],
                "parent_id": r[8] or None,
                "fork_session_id": r[9] or None,
                "fork_boundary": r[10] or None,
                "metadata": r[11] or None,
                "created": ts(r[18]),
                "updated": ts(r[19]),
                "idle": ts(r[20]),
                "message_rows": scan["rows"],
                "unparsable_message_rows": scan["unparsable_rows"],
                "db_rollup": {
                    "cost": r[12], "input": r[13], "output": r[14], "reasoning": r[15],
                    "cache_read": r[16], "cache_write": r[17],
                    "total_including_reasoning": sum(filter(None, [r[13], r[14], r[15], r[16], r[17]])),
                },
                "message_type_counts": [{"type": k, **v} for k, v in scan["tokens_by_type_all"].items()],
                "assistant_rows_with_tokens": assistant["rows"],
                "assistant_tokens": assistant,
                "compaction_summary": {"events": compaction["rows"], "tokens": compaction},
                "compaction_events": scan["compaction_events"],
                "per_message_peaks": scan["peaks"],
                "hourly": hour_rows,
                "hourly_reconciles": sum(h["input"] for h in hour_rows) == assistant["input"]
                and sum(h["cache_read"] for h in hour_rows) == assistant["cache_read"],
            }
            if r[1] == 0:
                entry["tool_call_counts"] = scan["tools"]
                entry["agent_like_tool_mentions"] = scan["spawnish"]
                entry["tool_names_resolved"] = bool(scan["tools"]) or entry["message_rows"] == 0
                entry["referenced_session_ids_in_messages"] = {k: v for k, v in scan["refs"].items() if k not in ids}
                entry["timing"] = timing_profile(scan["intervals"])
            sessions.append(entry)
        return {
            "found": True,
            "db": os.path.abspath(db_path),
            "has_children": len(sessions) > 1,
            "sessions": sessions,
        }
    finally:
        con.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
