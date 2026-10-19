"""测试用的合成存储:假的 HOME + 真的 opencode.db 结构,全部数据由本文件生成。

不用任何真实会话数据 —— 这些 fixture 就是被测代码的输入契约,
需要覆盖:coding agent 的真实写法语义(Claude 重复 usage、codex 逐轮用量、
opencode 的 compaction 行、.zst 压缩分片),而不是随便造个能跑的文件。
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

CODEX_UUID = "11111111-2222-3333-4444-555555555555"
CLAUDE_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def dumps(obj):
    """真实客户端写出的 JSONL 是紧凑分隔符,fixture 必须一致,否则时间戳正则会空跑。"""
    return json.dumps(obj, separators=(",", ":"))


def zst_bytes(text):
    import zstandard

    cctx = zstandard.ZstdCompressor()
    return cctx.compress(text.encode("utf-8"))


def codex_session_meta(sid, cwd="/work", forked_from=None, rollout_id=None):
    payload = {"session_id": sid, "cwd": cwd, "cli_version": "0.9", "originator": "cli",
               "source": "cli", "forked_from_id": forked_from, "model_provider": "openai"}
    if rollout_id:
        payload["id"] = rollout_id
    return json.dumps({"type": "session_meta", "timestamp": "2026-10-19T10:00:00.000Z", "payload": payload},
                      separators=(",", ":"))


PARENT_SESSION = "a1a1a1a1-1111-4111-8111-111111111111"
PARENT_ROLLOUTS = ["f1f1f1f1-2222-4222-8222-222222222222", "f2f2f2f2-3333-4333-8333-333333333333"]
FORKED_SESSION = "b2b2b2b2-4444-4444-8444-444444444444"


def build_codex_two_level(root: Path):
    """真实 codex 形态:文件名与 payload.id 是 rollout 自身的 id,payload.session_id 才是会话 id;
    一个会话跨两个 rollout,另一个会话的 forked_from_id 指回这个会话。"""
    d = root / ".codex" / "sessions" / "2026" / "10" / "19"
    d.mkdir(parents=True, exist_ok=True)
    for i, rid in enumerate(PARENT_ROLLOUTS):
        text = "\n".join([
            codex_session_meta(PARENT_SESSION, rollout_id=rid),
            codex_turn(1000, 800, 100, 40, 1140, f"2026-10-19T1{i}:00:00.000Z"),
            codex_turn(2000, 1500, 300, 90, 3890, f"2026-10-19T1{i}:10:00.000Z"),
        ])
        (d / f"rollout-2026-10-19T1{i}-00-00-{rid}.jsonl").write_text(text + "\n", encoding="utf-8")
    fork = "\n".join([
        codex_session_meta(FORKED_SESSION, forked_from=PARENT_SESSION,
                           rollout_id="c3c3c3c3-5555-4555-8555-555555555555"),
        codex_turn(500, 400, 50, 10, 560, "2026-10-19T12:00:00.000Z"),
    ])
    (d / "rollout-2026-10-19T12-00-00-c3c3c3c3-5555-4555-8555-555555555555.jsonl").write_text(
        fork + "\n", encoding="utf-8")
    return d


def build_codex_flat(root: Path, sid, compressed=False):
    """旧的扁平形态:文件名 uuid 就是 session_id。"""
    d = root / ".codex" / "sessions" / "2026" / "10" / "16"
    d.mkdir(parents=True, exist_ok=True)
    first = "\n".join([
        codex_session_meta(sid),
        codex_turn(1000, 800, 100, 40, 1140, "2026-10-19T10:00:05.000Z"),
        codex_turn(1000, 800, 100, 40, 1140, "2026-10-19T10:00:06.000Z"),
        codex_turn(2000, 1500, 300, 90, 3890, "2026-10-19T10:10:00.000Z"),
    ])
    second = "\n".join([
        codex_session_meta(sid),
        codex_turn(4000, 3000, 500, 120, 7500, "2026-10-19T11:00:00.000Z"),
    ])
    if compressed:
        (d / f"rollout-2026-10-19T10-00-00-{sid}.jsonl.zst").write_bytes(zst_bytes(first + "\n"))
    else:
        (d / f"rollout-2026-10-19T10-00-00-{sid}.jsonl").write_text(first + "\n", encoding="utf-8")
    (d / f"rollout-2026-10-19T11-00-00-{sid}.jsonl").write_text(second + "\n", encoding="utf-8")
    return d


def codex_turn(input_tokens, cached, output, reasoning, total, timestamp):
    usage = {"input_tokens": input_tokens, "cached_input_tokens": cached,
             "cache_write_input_tokens": 0, "output_tokens": output,
             "reasoning_output_tokens": reasoning, "thinking_output_tokens": 0,
             "total_tokens": total}
    return json.dumps({"type": "event", "timestamp": timestamp,
                       "payload": {"type": "token_count",
                                   "info": {"last_token_usage": dict(usage),
                                            "total_token_usage": dict(usage)}}},
                      separators=(",", ":"))


def claude_record(message_id, usage, timestamp, agent_id=None, model="claude-test", repeats=1):
    rec = {"type": "assistant", "timestamp": timestamp, "model": model,
           "isSidechain": bool(agent_id), "message": {"id": message_id, "usage": dict(usage)}}
    if agent_id:
        rec["agentId"] = agent_id
    return "\n".join([dumps(rec)] * repeats)


CLAUDE_USAGE = {"input_tokens": 1000, "output_tokens": 200,
                "cache_read_input_tokens": 500, "cache_creation_input_tokens": 50}


def build_fake_home(root: Path, codex_compressed=False, claude_repeats=3):
    """返回 {codex_dir, claude_dir, claude_subagents, db} 路径;不创建 opencode.db。"""
    claude_dir = root / ".claude" / "projects" / "-work"
    claude_sub = claude_dir / CLAUDE_UUID / "subagents"
    claude_sub.mkdir(parents=True, exist_ok=True)

    codex_dir = build_codex_flat(root, CODEX_UUID, compressed=codex_compressed)

    main_lines = [claude_record(f"msg_{i}", CLAUDE_USAGE, f"2026-10-19T09:{i:02d}:00.000Z", repeats=claude_repeats)
                  for i in range(4)]
    (claude_dir / f"{CLAUDE_UUID}.jsonl").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    child_lines = [claude_record(f"sub_{i}", CLAUDE_USAGE, f"2026-10-19T09:{i:02d}:30.000Z",
                                 agent_id="agent-1", repeats=claude_repeats) for i in range(2)]
    (claude_sub / f"agent-{CLAUDE_UUID}.jsonl").write_text("\n".join(child_lines) + "\n", encoding="utf-8")
    (claude_sub / f"agent-{CLAUDE_UUID}.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "probe", "spawnDepth": 1}), encoding="utf-8")
    return {"codex_dir": codex_dir, "claude_dir": claude_dir, "claude_subagents": claude_sub,
            "root": root}


SESSION_V2_COLUMNS = [
    ("id", "TEXT PRIMARY KEY"), ("parent_id", "TEXT"), ("slug", "TEXT"), ("title", "TEXT"),
    ("agent", "TEXT"), ("model", "TEXT"), ("directory", "TEXT"), ("version", "TEXT"),
    ("fork_session_id", "TEXT"), ("fork_boundary", "TEXT"), ("metadata", "TEXT"),
    ("cost", "REAL"), ("tokens_input", "INTEGER"), ("tokens_output", "INTEGER"),
    ("tokens_reasoning", "INTEGER"), ("tokens_cache_read", "INTEGER"), ("tokens_cache_write", "INTEGER"),
    ("time_created", "INTEGER"), ("time_updated", "INTEGER"), ("time_idle", "INTEGER"),
]


def build_opencode_db(path: Path, parent_id="ses_" + "p" * 26, child_id="ses_" + "c" * 26):
    """按适配器使用的列建一个最小 opencode.db,写入 assistant / compaction / 无法解析的行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    cols = ", ".join(f"{name} {decl}" for name, decl in SESSION_V2_COLUMNS)
    con.execute(f"CREATE TABLE session_v2 ({cols})")
    con.execute("CREATE TABLE session_message (id INTEGER PRIMARY KEY, session_id TEXT, type TEXT, "
                "time_created INTEGER, data TEXT)")
    base = 1_760_000_000_000
    con.execute("INSERT INTO session_v2 (id, title, agent, model, directory, version, cost, tokens_input, "
                "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, time_updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (parent_id, "父会话", "build", "model-x", "/work", "1.0", 1.5, 6000, 600, 200, 5300, 0,
                 base, base + 60_000))
    con.execute("INSERT INTO session_v2 (id, parent_id, title, agent, model, directory, version, time_created) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (child_id, parent_id, "子会话", "explore", "model-x", "/work", "1.0", base + 5_000))

    def assistant(inp, cache_read, output, reasoning, created, completed, tool_name=None, spacing=False):
        data = {"type": "assistant", "tokens": {"input": inp, "output": output, "reasoning": reasoning,
                                                 "cache": {"read": cache_read}},
                "time": {"created": created, "completed": completed}}
        if tool_name:
            part = {"type": "tool", "id": "t1", "name": tool_name}
            data["parts"] = [part]
            text = json.dumps(data)
            if spacing:
                text = text.replace('":', '": ').replace(",", ", ")
            return text
        return json.dumps(data)

    rows = [
        ("assistant", base, assistant(1000, 800, 100, 40, base, base + 4000, tool_name="read")),
        # 键序/空白不同的一份:正则版本会漏,递归解析版本必须命中
        ("assistant", base + 5000, assistant(2000, 1500, 300, 90, base + 5000, base + 9000,
                                             tool_name="task", spacing=True)),
        ("compaction", base + 20_000, json.dumps({"type": "compaction",
                                                  "tokens": {"input": 500, "output": 50, "reasoning": 0,
                                                             "cache": {"read": 100}}})),
        ("user", base + 1000, json.dumps({"type": "user", "text": "hi"})),
        ("assistant", base + 30_000, "{not json at all"),
    ]
    for rtype, created, data in rows:
        con.execute("INSERT INTO session_message (session_id, type, time_created, data) VALUES (?,?,?,?)",
                    (parent_id, rtype, created, data))
    con.execute("INSERT INTO session_message (session_id, type, time_created, data) VALUES (?,?,?,?)",
                (child_id, "assistant", base + 6000, assistant(300, 100, 20, 5, base + 6000, base + 7000)))
    con.commit()
    con.close()
    return {"db": path, "parent_id": parent_id, "child_id": child_id, "base": base}


def temp_dir():
    return Path(tempfile.mkdtemp(prefix="agent-report-test-"))


def cc_row(agent, period, inp, out, cache_read, total, cost=1.0, reasoning=None, last_activity=None):
    metadata = {"lastActivity": last_activity, "reasoningOutputTokens": reasoning}
    return {"agent": agent, "period": period, "inputTokens": inp, "outputTokens": out,
            "cacheReadTokens": cache_read, "cacheCreationTokens": 0, "totalTokens": total,
            "totalCost": cost, "modelsUsed": ["model-x"], "metadata": metadata,
            "modelBreakdowns": [{"modelName": "model-x", "missingPricing": False}]}
