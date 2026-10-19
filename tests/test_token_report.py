#!/usr/bin/env python3
"""承重口径的回归测试:全部跑在合成 fixture 上,不读取任何真实会话数据。

运行(仓库根):
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SKILL_NAME = "agent-session-token-report"


def _find_skill_dir():
    """定位被测 skill 目录,不假定 tests 放在仓库根还是 skill 包内。

    判据是"同时含 SKILL.md 与 scripts/collect.py"的目录,所以搬动 tests 不需要改这里。
    """
    for cand in (ROOT / "skills" / SKILL_NAME, ROOT, HERE / SKILL_NAME, HERE):
        if (cand / "SKILL.md").is_file() and (cand / "scripts" / "collect.py").is_file():
            return cand
    for skill_md in sorted(ROOT.glob("**/SKILL.md")):
        if (skill_md.parent / "scripts" / "collect.py").is_file():
            return skill_md.parent
    raise RuntimeError(f"找不到 skill 目录(应含 SKILL.md 与 scripts/collect.py);搜索根: {ROOT}")


SKILL_DIR = _find_skill_dir()
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(HERE))

import collect  # noqa: E402
import crosscheck  # noqa: E402
import helpers  # noqa: E402
import jsonl_reader  # noqa: E402
import store_jsonl  # noqa: E402
import store_opencode  # noqa: E402


class AdapterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.temp_dir()
        self.addCleanup(self._cleanup)
        self._old_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
        os.environ["USERPROFILE"] = str(self.tmp)
        os.environ["HOME"] = str(self.tmp)
        store_jsonl.reset_caches()

    def _cleanup(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)
        store_jsonl.reset_caches()

    def run_collect(self, sid, rows, extra_args=(), db=None, build_home=True, cc_func=None):
        """在合成 HOME 里跑一次完整采集,返回 (退出码, stdout 精简视图, 落盘完整包)。

        cc_func 用于需要区分定价来源(离线/实时)的测试;省略时固定返回 rows。
        """
        if build_home:
            helpers.build_fake_home(self.tmp)
        cc_rows = {"session": rows}
        call = cc_func or (lambda offline, timeout: (cc_rows, None))
        with mock.patch.object(collect, "discover_platforms",
                               lambda timeout: (["codex", "claude", "opencode"], "stub")), \
             mock.patch.object(collect, "run_ccusage", call):
            stdout = io.StringIO()
            argv = ["collect.py", sid, "--platform", "auto",
                    "--db", db or str(self.tmp / "no-opencode.db"),
                    "--out-dir", str(self.tmp)] + list(extra_args)
            code = 0
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                try:
                    collect.main()
                except SystemExit as exc:
                    code = exc.code
            pack_path = self.tmp / f"agent-report-{sid}-facts.json"
            full = json.loads(pack_path.read_text(encoding="utf-8")) if pack_path.exists() else None
            view_text = stdout.getvalue()
            try:
                view = json.loads(view_text)
            except json.JSONDecodeError:
                view = {"_not_json": view_text[:400]}
            return code, view, full


class ClaudeEmptyGuardTest(AdapterTestBase):
    """`counted` 的取值顺序必须认 anonymous-only:否则只有匿名 usage 的会话会被降级成未复核。"""

    def test_anonymous_only_usage_is_not_treated_as_empty(self):
        sid = helpers.CLAUDE_UUID
        d = self.tmp / ".claude" / "projects" / "-work" / sid
        d.mkdir(parents=True)
        lines = []
        for _ in range(3):
            rec = {"type": "assistant", "timestamp": "2026-10-19T09:00:00.000Z",
                   "message": {"usage": dict(helpers.CLAUDE_USAGE)}}
            lines.append(json.dumps(rec, separators=(",", ":")))
        main = self.tmp / ".claude" / "projects" / "-work" / f"{sid}.jsonl"
        main.write_text("\n".join(lines) + "\n", encoding="utf-8")
        rows = [helpers.cc_row("claude", sid, 3000, 600, 1500, 5100,
                               last_activity="2026-10-19T09:00:00.000Z")]
        rows[0]["cacheCreationTokens"] = 150  # CLAUDE_USAGE 每条 50 × 3 条匿名记录
        code, view, full = self.run_collect(sid, rows, build_home=False)
        check = full["crosscheck"]
        self.assertEqual(check["verdict"], "ok", "有 anonymous usage 记录就不算本地为空")
        self.assertEqual(check["agreement"], "exact")
        self.assertEqual(check["mine"]["unique_message_ids"], 0)
        self.assertEqual(check["mine"]["anonymous_usage_records"], 3)


class ClaudeDedupTest(AdapterTestBase):
    def test_repeated_usage_counted_once(self):
        helpers.build_fake_home(self.tmp, claude_repeats=3)
        main = self.tmp / ".claude" / "projects" / "-work" / f"{helpers.CLAUDE_UUID}.jsonl"
        usage = store_jsonl.usage_sum(main)
        self.assertEqual(usage["usage_records_raw"], 12)
        self.assertEqual(usage["usage_records_deduped"], 4)
        self.assertEqual(usage["input_tokens"], 4000)
        self.assertEqual(usage["output_tokens"], 800)
        self.assertEqual(usage["cache_read_input_tokens"], 2000)
        self.assertEqual(usage["cache_creation_input_tokens"], 200)

    def test_dedup_rule_is_shared_not_duplicated(self):
        helpers.build_fake_home(self.tmp, claude_repeats=3)
        main = self.tmp / ".claude" / "projects" / "-work" / f"{helpers.CLAUDE_UUID}.jsonl"
        check = crosscheck.claude_check([main])
        usage = store_jsonl.usage_sum(main)
        for key in store_jsonl.CLAUDE_TOKEN_KEYS:
            self.assertEqual(check[key], usage[key], f"适配器与复核的 {key} 不一致")

    def test_anonymous_usage_records_are_kept_and_reported(self):
        path = self.tmp / "anon.jsonl"
        lines = []
        for _ in range(2):
            rec = {"type": "assistant", "timestamp": "2026-10-19T09:00:00.000Z",
                   "message": {"usage": {"input_tokens": 100, "output_tokens": 10}}}
            lines.append(json.dumps(rec))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        usage = store_jsonl.usage_sum(path)
        self.assertEqual(usage["anonymous_usage_records"], 2)
        self.assertEqual(usage["input_tokens"], 200)


class CodexRoundUsageTest(AdapterTestBase):
    def test_duplicate_turn_usage_skipped_and_cumulative_not_used(self):
        helpers.build_fake_home(self.tmp)
        files = sorted(str(x) for x in (self.tmp / ".codex" / "sessions").rglob("*.jsonl"))
        self.assertEqual(len(files), 2)
        mine = crosscheck.codex_check(files)
        self.assertEqual(mine["turns_raw"], 4)
        self.assertEqual(mine["turns_deduped"], 3)
        self.assertEqual(mine["duplicate_records_skipped"], 1)
        aligned = mine["aligned_to_ccusage"]
        self.assertEqual(aligned["input_tokens"], 1700)
        self.assertEqual(aligned["cache_read_input_tokens"], 5300)
        self.assertEqual(aligned["output_tokens"], 900)
        self.assertEqual(aligned["reasoning_output_tokens"], 250)
        self.assertEqual(aligned["total_tokens"], 12530)
        self.assertEqual(mine["per_turn_without_dedup"]["total_tokens"], 13670)
        self.assertEqual(mine["per_file_cumulative_sum"]["total_tokens"], 11390)

    def test_compressed_rollouts_are_included(self):
        helpers.build_fake_home(self.tmp, codex_compressed=True)
        if jsonl_reader.zstd_backend() is None:
            self.skipTest("本机没有 zstd 后端")
        store = store_jsonl.codex_store(helpers.CODEX_UUID)
        self.assertEqual(len(store["files"]), 2)
        self.assertEqual(store["rollout_compressed_total"], 1)
        mine = crosscheck.codex_check(store["files"])
        self.assertEqual(mine["aligned_to_ccusage"]["total_tokens"], 12530)

    def test_missing_zstd_backend_surfaces_as_failure_not_zero(self):
        helpers.build_fake_home(self.tmp, codex_compressed=True)
        if jsonl_reader.zstd_backend() is None:
            self.skipTest("本机没有 zstd 后端,无法构造对照")
        with mock.patch.object(store_jsonl, "BACKEND", None):
            path = next(iter((self.tmp / ".codex" / "sessions").rglob("*.zst")))
            profile = store_jsonl.scan(path)
            self.assertTrue(profile["decode_error"])
            self.assertIn(".zst", profile["decode_error"])
            failures = store_jsonl.read_failures()
            self.assertTrue(any(f["file"].endswith(".zst") for f in failures))
            with mock.patch.object(crosscheck, "BACKEND", None):
                node, extra, error = crosscheck.codex_file_tokens(path)
                self.assertIsNone(node)
                self.assertTrue(error)

    def test_store_indexes_all_rollouts_for_one_session(self):
        helpers.build_fake_home(self.tmp)
        store = store_jsonl.codex_store(helpers.CODEX_UUID)
        self.assertEqual(store["sessions_indexed"], 1)
        self.assertEqual(store["rollout_files_total"], 2)
        self.assertIn("2 个 rollout 文件", store["multi_rollout_note"])
        self.assertEqual(store["children"], [])

    def test_rollout_id_resolves_to_session_id_and_verifies_children(self):
        """真实 codex 形态:文件名/payload.id 是 rollout 自身 id,session_id 才是会话 id。"""
        helpers.build_codex_two_level(self.tmp)
        rollout_id = helpers.PARENT_ROLLOUTS[1]
        store = store_jsonl.codex_store(rollout_id)
        self.assertEqual(store["session_id"], helpers.PARENT_SESSION)
        self.assertEqual(store["requested_id_form"], "rollout_id")
        self.assertTrue(store["children_verified"])
        self.assertEqual(len(store["files"]), 2)
        self.assertEqual([c["session_id"] for c in store["children"]], [helpers.FORKED_SESSION])

        direct = store_jsonl.codex_store(helpers.PARENT_SESSION)
        self.assertEqual(direct["requested_id_form"], "session_id")
        self.assertEqual(sorted(direct["files"]), sorted(store["files"]))
        self.assertEqual(len(direct["children"]), 1)

    def test_filename_fallback_does_not_claim_zero_children(self):
        d = helpers.build_codex_two_level(self.tmp)
        stray = d / "rollout-2026-10-19T13-00-00-09d9de99-1111-2222-3333-444455556666.jsonl"
        stray.write_text(helpers.codex_session_meta("09d9de99-1111-2222-3333-444455556666",
                                                    rollout_id="0ee0ee00-1111-2222-3333-444455556666")
                         + "\n", encoding="utf-8")
        # 传入一个只存在于文件名、不存在于任何 session_id 的 id:只能按文件名兜底
        store = store_jsonl.codex_store("1f2e3d4c-aaaa-bbbb-cccc-ddddeeeeffff")
        self.assertFalse(store.get("children_verified"))
        self.assertEqual(store["files"], [])
        self.assertIn("未经验证", store["note"])


class GeminiScopeTest(AdapterTestBase):
    def test_unmatched_candidates_are_not_attributed_to_the_session(self):
        chats = self.tmp / ".gemini" / "tmp" / "proj" / "chats"
        chats.mkdir(parents=True)
        sid = "11111111-2222-3333-4444-555555555555"
        other = chats / f"{sid.split('-')[0]}-other.jsonl"
        other.write_text(json.dumps({"parts": "another session here"}), encoding="utf-8")
        store = store_jsonl.gemini_store(sid)
        self.assertEqual(store["files"], [])
        self.assertEqual(len(store["candidate_files"]), 1)
        self.assertFalse(store["matched_by_full_id"])


class OpenCodeScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = helpers.temp_dir()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.spec = helpers.build_opencode_db(self.tmp / "opencode.db")

    def analyze(self):
        return store_opencode.analyze(str(self.spec["db"]), self.spec["parent_id"])

    def test_tree_and_totals(self):
        deep = self.analyze()
        self.assertTrue(deep["found"])
        self.assertTrue(deep["has_children"])
        parent = next(s for s in deep["sessions"] if s["depth"] == 0)
        self.assertEqual(parent["assistant_tokens"],
                         {"rows": 2, "input": 3000, "output": 400, "reasoning": 130, "cache_read": 2300})
        self.assertEqual(parent["compaction_summary"]["events"], 1)
        self.assertEqual(parent["compaction_summary"]["tokens"]["input"], 500)
        self.assertTrue(parent["hourly_reconciles"], "分时段求和应等于 assistant 求和")
        self.assertEqual(parent["unparsable_message_rows"], 1)

    def test_tool_inventory_survives_key_order_and_spacing(self):
        parent = [s for s in self.analyze()["sessions"] if s["depth"] == 0][0]
        self.assertEqual(parent["tool_call_counts"].get("read"), 1)
        self.assertEqual(parent["tool_call_counts"].get("task"), 1,
                         "带空格的序列化必须仍能识别工具调用,否则会被当成「没有派生调用」的证据")
        self.assertEqual(parent["agent_like_tool_mentions"].get("task"), 1)

    def test_peaks_and_timing(self):
        parent = [s for s in self.analyze()["sessions"] if s["depth"] == 0][0]
        self.assertEqual(parent["per_message_peaks"]["max_input"], 2000)
        self.assertEqual(parent["per_message_peaks"]["avg_input"], 1500)
        self.assertEqual(parent["per_message_peaks"]["sampled_rows"], 2)
        self.assertEqual(parent["timing"]["elapsed_human"], "0h0m9s")
        self.assertEqual(parent["timing"]["active_human"], "0h0m8s")
        self.assertEqual(parent["timing"]["gap_count"], 1)

    def test_missing_session_lists_recent(self):
        deep = store_opencode.analyze(str(self.spec["db"]), "ses_" + "x" * 26)
        self.assertFalse(deep["found"])
        self.assertEqual(len(deep["recent_sessions"]), 2)

    def test_probe_does_not_copy_the_database(self):
        broken = self.tmp / "locked.db"
        broken.write_text("not a sqlite file", encoding="utf-8")
        self.assertFalse(collect.store_opencode_found(str(broken), "ses_x"))


class StampAndVerdictTest(unittest.TestCase):
    def test_naive_iso_does_not_kill_the_crosscheck(self):
        minutes, basis = crosscheck._gap_minutes("2026-10-19T10:59:00")
        self.assertIsInstance(minutes, float)
        self.assertIn("naive", basis)

    def test_aware_iso_epoch_and_garbage(self):
        self.assertIsNotNone(crosscheck._gap_minutes("2026-10-19T10:59:00+00:00")[0])
        self.assertIsNotNone(crosscheck._gap_minutes(1_760_000_000_000)[0])
        self.assertIsNotNone(crosscheck._gap_minutes(1_760_000_000)[0])
        minutes, basis = crosscheck._gap_minutes("not a time")
        self.assertIsNone(minutes)
        self.assertIn("无法解析", basis)
        minutes, basis = crosscheck._gap_minutes(None)
        self.assertIsNone(minutes)

    def test_compare_accepts_floats_and_reports_missing_overlap(self):
        rows = crosscheck.compare({"input_tokens": 1000.0, "output_tokens": 200},
                                  {"input": 1000, "output": None}, crosscheck.MAPPING_CLAUDE)
        by_field = {r["field"]: r for r in rows}
        self.assertEqual(by_field["input"]["delta"], 0)
        self.assertIsNone(by_field["output"]["delta"])
        out = crosscheck._finalize({"comparison": [
            {"field": "input", "mine": None, "ccusage": None, "delta": None, "delta_pct": None}]}, 999)
        self.assertEqual(out["agreement"], "no_numeric_overlap")
        self.assertEqual(out["verdict"], "unverifiable")

    def test_agreement_tiers(self):
        def verdict(mine, cc, gap):
            rows = crosscheck.compare({"input_tokens": mine}, {"input": cc}, [("input", "input_tokens", "input")])
            return crosscheck._finalize({"comparison": rows}, gap)["agreement"]

        self.assertEqual(verdict(1000, 1000, None), "exact")
        self.assertEqual(verdict(1005, 1000, None), "close")
        self.assertEqual(verdict(1030, 1000, 2), "snapshot_skew_likely")
        self.assertEqual(verdict(1030, 1000, 600), "divergent_unexplained")
        self.assertEqual(verdict(2000, 1000, None), "mismatch")


class PricingVerdictTest(unittest.TestCase):
    def test_no_rows_is_not_reported_as_priced(self):
        rows, cc = collect.pricing_block({"ses_x"}, [], True, 5, False)
        self.assertEqual(cc["pricing_verdict"], "no_ccusage_rows")
        self.assertEqual(rows, [])

    def test_escalation_without_rows_is_not_priced_online_only(self):
        row = {"agent": "codex", "period": "ses_x", "inputTokens": 1, "outputTokens": 1, "totalTokens": 2,
               "modelBreakdowns": [{"modelName": "new-model", "missingPricing": True}], "metadata": {}}
        with mock.patch.object(collect, "run_ccusage", lambda offline, timeout: ({"session": []}, None)):
            rows, cc = collect.pricing_block({"ses_x"}, [row], True, 5, False)
            self.assertEqual(cc["pricing_verdict"], "offline_missing_escalation_empty")
            self.assertNotIn("escalated_rows", cc)
            self.assertEqual(cc["unpriced_models"], ["new-model"])

    def test_escalation_with_priced_rows(self):
        missing = dict(json.loads(json.dumps({
            "agent": "codex", "period": "ses_x", "inputTokens": 1, "outputTokens": 1, "totalTokens": 2,
            "modelBreakdowns": [{"modelName": "new-model", "missingPricing": True}]})))
        priced = {"agent": "codex", "period": "ses_x", "inputTokens": 1, "outputTokens": 1, "totalTokens": 2,
                  "totalCost": 3.0, "modelBreakdowns": [{"modelName": "new-model", "missingPricing": False}],
                  "metadata": {}}

        def fake(offline, timeout):
            return ({"session": [priced]}, None)

        with mock.patch.object(collect, "run_ccusage", fake):
            rows, cc = collect.pricing_block({"ses_x"}, [missing], True, 5, False)
            self.assertEqual(cc["pricing_verdict"], "priced_online_only")
            self.assertEqual(len(cc["escalated_rows"]), 1)


class RowIndexTest(unittest.TestCase):
    def test_uuid_and_rollout_period_forms(self):
        rows = [helpers.cc_row("codex", "2026-10-19/rollout-a-11111111-2222-3333-4444-555555555555", 1, 1, 0, 2),
                helpers.cc_row("codex", "2026-10-19/rollout-b-11111111-2222-3333-4444-555555555555", 2, 2, 0, 4),
                helpers.cc_row("codex", "2026-10-19/rollout-c-99999999-2222-3333-4444-555555555555", 8, 8, 0, 16)]
        index = collect.RowIndex(rows)
        got = collect.collect_rows(index, {"11111111-2222-3333-4444-555555555555"})
        self.assertEqual(len(got), 2)
        agg = collect.aggregate_rows(got, "sid", "codex")
        self.assertEqual(agg["rollouts_aggregated"], 2)
        self.assertEqual(agg["input"], 3)

    def test_short_suffix_does_not_match_unrelated_rows(self):
        rows = [helpers.cc_row("codex", "2026-10-19/rollout-x-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 1, 1, 0, 2)]
        index = collect.RowIndex(rows)
        self.assertEqual(collect.collect_rows(index, {"eeee"}), [])
        self.assertEqual(len(collect.collect_rows(index, {"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"})), 1)

    def test_length_guard_rejects_even_on_clean_boundary(self):
        """只靠长度守卫拦住的情形:后缀落在 '/' 边界上但 id 短于 12 位。"""
        rows = [helpers.cc_row("kimi", "2026-10-19/workspace/abcdef12345", 1, 1, 0, 2)]
        index = collect.RowIndex(rows)
        self.assertEqual(len(collect.collect_rows(index, {"abcdef12345"})), 1, "整段后缀(11 位)不该被长度守卫误杀整行")
        self.assertEqual(collect.collect_rows(index, {"cdef12345"}), [],
                         "5 位后缀即使前面是分隔符也不能命中,否则短 id 会把无关行吸进来")


class PipelineTest(AdapterTestBase):
    """整条采集链路的形状:平台判定、会话装配、stdout 精简、覆盖度告警。"""

    def test_codex_pipeline_aggregates_rollouts(self):
        sid = helpers.CODEX_UUID
        # 两份 rollout 对应的 ccusage 行,数值直接取 fixture 的逐轮去重口径,
        # 这样复核应当逐字段 exact:file1 = 1000+2000 输入扣掉 800+1500 缓存
        spec = [("a", 700, 400, 2300, 5030, 130), ("b", 1000, 500, 3000, 7500, 120)]
        rows = [helpers.cc_row("codex", f"2026-10-19/rollout-{c}-{sid}", inp, out, cache, total,
                               reasoning=reas, last_activity="2026-10-19T11:00:00+00:00")
                for c, inp, out, cache, total, reas in spec]
        code, view, full = self.run_collect(sid, rows)
        self.assertEqual(code, 0)
        main = full["sessions"][0]
        self.assertEqual(main["ccusage"]["rollouts_aggregated"], 2)
        self.assertEqual(main["ccusage_rollouts_aggregated"], 2)
        self.assertGreater(full["local_store_extras"]["timing"]["events_with_timestamp"], 0,
                           "fixture 的时间戳必须真的被正则取到,否则时间分布测试是空断言")
        check = full["crosscheck"]
        self.assertEqual(check["agreement"], "exact", f"复核应逐字段相同:{check['comparison']}")
        self.assertEqual(check["max_abs_delta_pct"], 0)
        self.assertEqual(check["mine"]["aligned_to_ccusage"]["total_tokens"], 12530)
        self.assertEqual(check["mine"]["duplicate_records_skipped"], 1)
        self.assertNotIn("files", view["sessions"][0])
        self.assertIn("files_basenames", view["sessions"][0])
        self.assertNotIn("primary_rows", view["ccusage"])
        self.assertIn("primary_rows", full["ccusage"])
        self.assertLess(len(json.dumps(view, ensure_ascii=False)), len(json.dumps(full, ensure_ascii=False)))

    def test_adapter_is_not_scanned_twice(self):
        calls = []
        original = store_jsonl.ADAPTERS["codex"]

        def counting(sid):
            calls.append(sid)
            return original(sid)

        helpers.build_fake_home(self.tmp)
        with mock.patch.dict(store_jsonl.ADAPTERS, {"codex": counting}), \
             mock.patch.object(collect, "discover_platforms", lambda timeout: (["codex"], "stub")), \
             mock.patch.object(collect, "run_ccusage", lambda offline, timeout: ({"session": []}, None)), \
             mock.patch.object(sys, "argv", ["collect.py", helpers.CODEX_UUID, "--platform", "auto",
                                             "--db", str(self.tmp / "none.db"), "--out-dir", str(self.tmp)]):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    collect.main()
                except SystemExit:
                    pass
        self.assertEqual(len(calls), 1, f"回落探测之后不应再跑一次全量索引,实际 {len(calls)} 次")

    def test_claude_family_totals_include_sidechain(self):
        sid = helpers.CLAUDE_UUID
        rows = [helpers.cc_row("claude", sid, 4000, 800, 2000, 6800,
                               last_activity="2026-10-19T09:03:00+00:00")]
        code, view, full = self.run_collect(sid, rows)
        self.assertEqual(code, 0)
        self.assertTrue(full["has_children"])
        family = full["family_totals"]
        self.assertEqual(family["bases"], ["ccusage", "file_dedup_sum"])
        self.assertTrue(family["mixed_basis"])
        self.assertEqual(family["sum"]["input"], 4000 + 2000)
        self.assertEqual(full["crosscheck"]["subagents_total"]["input_tokens"], 2000)
        self.assertEqual(full["family_totals"]["cost_missing_rows"], 1,
                         "子会话来自文件求和、没有成本数值,合计必须标注是下限")
        self.assertEqual(full["family_totals"]["cost_rows"], 1, "cost_rows 只应数有成本数值的行")
        self.assertIn("cost_sum 是下限", full["coverage_warning"])

    def test_opencode_pipeline_explains_compaction_gap(self):
        spec = helpers.build_opencode_db(self.tmp / "oc.db")
        rows = [helpers.cc_row("opencode", spec["parent_id"], 3000, 400, 2300, 3700, reasoning=130,
                               last_activity="2026-10-19T10:00:00+00:00"),
                helpers.cc_row("opencode", spec["child_id"], 300, 20, 100, 420, reasoning=5,
                               last_activity="2026-10-19T10:00:30+00:00")]
        code, view, full = self.run_collect(spec["parent_id"], rows, db=str(spec["db"]))
        self.assertEqual(code, 0)
        self.assertEqual(full["platform"], "opencode")
        self.assertTrue(full["has_children"])
        self.assertEqual(full["support"]["tier"], "full")
        check = full["crosscheck"]
        self.assertEqual(check["agreement"], "exact", check.get("comparison"))
        self.assertEqual(check["compaction_excluded_from_ccusage"]["input"], 500)
        family = full["family_totals"]
        self.assertEqual(family["sum"]["input"], 3300)
        self.assertFalse(family["mixed_basis"])
        parent_view = next(s for s in view["sessions"] if s["depth"] == 0)
        self.assertTrue(parent_view["hourly"], "分时段是报告第 8 节的必需字段,stdout 不应裁掉")
        self.assertTrue(parent_view["hourly_reconciles"])

    def test_stdout_stays_small_for_many_rollouts(self):
        sid = helpers.CODEX_UUID
        d = self.tmp / ".codex" / "sessions" / "2026" / "11"
        d.mkdir(parents=True, exist_ok=True)
        spec = []
        for i in range(8):
            text = "\n".join([helpers.codex_session_meta(sid),
                              helpers.codex_turn(1000, 800, 100, 40, 1140,
                                                 f"2026-11-01T{i % 24:02d}:00:00.000Z")])
            (d / f"rollout-2026-11-01T{i:02d}-00-00-{sid}.jsonl").write_text(text + "\n", encoding="utf-8")
            spec.append((200, 100, 800, 1100, 40))
        rows = [helpers.cc_row("codex", f"2026-11-01/rollout-{i:02d}-00-00-{sid}", i_, o, cr, t, reasoning=r,
                               last_activity="2026-11-01T07:00:00+00:00")
                for i, (i_, o, cr, t, r) in enumerate(spec)]
        code, view, full = self.run_collect(sid, rows)
        self.assertEqual(code, 0)
        self.assertEqual(view["sessions"][0]["ccusage"]["rollouts_aggregated"], 8)
        self.assertNotIn("rollout_ids", view["sessions"][0]["ccusage"])
        self.assertIn("rollout_ids", full["sessions"][0]["ccusage"])
        self.assertEqual(len(view["ccusage"]["primary_rows_slim"]), 6, "5 行样例 + 1 行省略计数")
        packed = len(json.dumps(view, ensure_ascii=False, indent=1))
        full_size = len(json.dumps(full, ensure_ascii=False, indent=1))
        self.assertLess(packed, full_size * 0.7, f"stdout {packed} 应明显小于落盘 {full_size}")
        for required in ("support", "crosscheck", "family_totals", "sessions", "captured_at"):
            self.assertIn(required, view)

    def test_codex_fork_children_and_rollout_id_alias_reach_report(self):
        helpers.build_codex_two_level(self.tmp)
        rid_a, rid_b = helpers.PARENT_ROLLOUTS
        rows = [
            helpers.cc_row("codex", f"2026-10-19/rollout-2026-10-19T10-00-00-{rid_a}", 700, 400, 2300, 5030,
                           reasoning=130, last_activity="2026-10-19T10:10:00.000Z"),
            helpers.cc_row("codex", f"2026-10-19/rollout-2026-10-19T11-00-00-{rid_b}", 700, 400, 2300, 5030,
                           reasoning=130, last_activity="2026-10-19T11:10:00.000Z"),
            helpers.cc_row("codex", "2026-10-19/rollout-2026-10-19T12-00-00-c3c3c3c3-5555-4555-8555-555555555555",
                           100, 50, 400, 560, reasoning=10, last_activity="2026-10-19T12:00:00.000Z"),
        ]
        code, view, full = self.run_collect(rid_b, rows)
        self.assertEqual(code, 0)
        self.assertTrue(full["children_verified"])
        self.assertTrue(full["has_children"])
        self.assertEqual(len(full["sessions"]), 2)
        main = full["sessions"][0]
        self.assertEqual(main["session_id"], helpers.PARENT_SESSION)
        self.assertEqual(main["requested_id"], rid_b)
        self.assertEqual(main["ccusage"]["rollouts_aggregated"], 2)
        child = full["sessions"][1]
        self.assertEqual(child["kind"], "forked_session")
        self.assertEqual(child["ccusage"]["input"], 100)
        self.assertEqual(full["family_totals"]["sum"]["input"], 1500)
        self.assertEqual(full["crosscheck"]["agreement"], "exact", full["crosscheck"]["comparison"])

    def test_unreadable_child_surfaces_coverage_warning(self):
        helpers.build_fake_home(self.tmp)
        rows = [helpers.cc_row("claude", helpers.CLAUDE_UUID, 1000, 200, 500, 1700,
                               last_activity="2026-10-19T09:00:00.000Z")]
        real_lines = jsonl_reader.LineSource.lines

        def failing(self):
            # 子代理文件名一定是 agent- 前缀;按 basename 判定,避免临时目录名 agent-report-test-* 造成误伤
            if os.path.basename(str(self.path)).startswith("agent-"):
                raise jsonl_reader.DecodeError("模拟压缩分片解码失败")
            return real_lines(self)

        with mock.patch.object(jsonl_reader.LineSource, "lines", failing):
            code, view, full = self.run_collect(helpers.CLAUDE_UUID, rows)
        self.assertEqual(code, 0)
        self.assertTrue(full.get("coverage_warning"), "解码失败必须进入覆盖度告警")
        self.assertIn("模拟压缩分片解码失败", full["coverage_warning"])
        family = full["family_totals"]
        self.assertEqual(family["sessions_without_numbers"], [f"agent-{helpers.CLAUDE_UUID}.jsonl"])
        self.assertTrue(family["sum_is_partial"])
        child = next(s for s in full["sessions"] if s["depth"] == 1)
        self.assertIn("error", child["usage_from_files"])


class HardeningTest(AdapterTestBase):
    """独立复核轮次发现的问题:0 值缺口、混类型、非法 JSON、目录漏扫、截断风险。"""

    def test_zero_on_one_side_is_mismatch_not_exact(self):
        rows = crosscheck.compare({"input_tokens": 500_000, "output_tokens": 100},
                                  {"input": 0, "output": 100},
                                  [("input", "input_tokens", "input"), ("output", "output_tokens", "output")])
        out = crosscheck._finalize({"comparison": rows}, 999)
        self.assertEqual(out["agreement"], "mismatch", "一侧为 0 另一侧非零不能被判成一致")
        self.assertIn("input", out["unbounded_delta_fields"])

    def test_mixed_last_activity_types_do_not_crash(self):
        rows = [{"agent": "codex", "period": "a", "inputTokens": 1, "metadata": {"lastActivity": 1_760_000_000_000}},
                {"agent": "codex", "period": "b", "inputTokens": 1,
                 "metadata": {"lastActivity": "2026-10-19T04:07:55.118Z"}}]
        agg = collect.aggregate_rows(rows, "s", "codex")
        self.assertEqual(agg["rollouts_aggregated"], 2)
        self.assertIsNotNone(agg["last_activity"])

    def test_string_token_values_are_coerced(self):
        rows = [{"agent": "codex", "period": "a", "inputTokens": "1234", "outputTokens": 10,
                 "cacheReadTokens": None, "cacheCreationTokens": 0, "totalTokens": "1244",
                 "totalCost": "0.5", "metadata": {}, "modelsUsed": [], "modelBreakdowns": []},
                {"agent": "codex", "period": "b", "inputTokens": 6, "outputTokens": 1, "totalTokens": 7,
                 "totalCost": 0.1, "metadata": {}, "modelsUsed": [], "modelBreakdowns": []}]
        agg = collect.aggregate_rows(rows, "s", "codex")
        self.assertEqual(agg["input"], 1240)
        self.assertEqual(agg["total"], 1251)
        self.assertAlmostEqual(agg["cost"], 0.6)
        self.assertIsNone(collect.summarize({"agent": "x", "period": "p", "metadata": {}})["input"])

    def test_family_totals_keep_session_labels(self):
        sid = helpers.CODEX_UUID
        row = helpers.cc_row("codex", f"2026-10-19/rollout-00-{sid}", 700, 400, 2300, 5030, reasoning=130,
                             last_activity="2026-10-19T10:10:00.000Z")
        code, view, full = self._run_with_rows(sid, [row], extra=("--platform", "codex"))
        entry = full["family_totals"]["per_session"][0]
        self.assertEqual(entry["session_id"], sid, "标签必须是会话 id,不能被 ccusage 的 period 盖掉")
        self.assertNotIn("period", entry)

    def test_corrupt_opencode_db_exits_with_store_error_not_not_found(self):
        bad = self.tmp / "corrupt.db"
        bad.write_text("this is not a sqlite database", encoding="utf-8")
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", ["collect.py", "ses_" + "a" * 26, "--platform", "opencode",
                                             "--db", str(bad), "--out-dir", str(self.tmp), "--no-ccusage"]), \
             contextlib.redirect_stdout(stdout):
            try:
                collect.main()
            except SystemExit as exc:
                code = exc.code
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2, "存储不可用要区别于会话不存在(退出码 3)")
        self.assertTrue(payload["store_error"])

    def test_non_object_message_data_does_not_kill_the_scan(self):
        spec = helpers.build_opencode_db(self.tmp / "oc.db")
        import sqlite3

        con = sqlite3.connect(str(spec["db"]))
        con.execute("INSERT INTO session_message (session_id, type, time_created, data) VALUES (?,?,?,?)",
                    (spec["parent_id"], "assistant", spec["base"] + 40_000, json.dumps([1, 2, 3])))
        con.execute("INSERT INTO session_message (session_id, type, time_created, data) VALUES (?,?,?,?)",
                    (spec["parent_id"], "assistant", spec["base"] + 41_000, '"just a string"'))
        con.commit()
        con.close()
        deep = store_opencode.analyze(str(spec["db"]), spec["parent_id"])
        parent = [s for s in deep["sessions"] if s["depth"] == 0][0]
        self.assertEqual(parent["unparsable_message_rows"], 3, "1 条非法 JSON + 2 条非对象 JSON")
        self.assertEqual(parent["assistant_rows_with_tokens"], 2)

    def test_claude_subagents_found_in_every_matching_project_dir(self):
        first = self.tmp / ".claude" / "projects" / "-a"
        second = self.tmp / ".claude" / "projects" / "-b"
        sid = helpers.CLAUDE_UUID
        for d in (first, second):
            (d / sid / "subagents").mkdir(parents=True, exist_ok=True)
            (d / f"{sid}.jsonl").write_text(
                helpers.claude_record("m1", helpers.CLAUDE_USAGE, "2026-10-19T09:00:00.000Z") + "\n",
                encoding="utf-8")
            (d / sid / "subagents" / f"agent-{d.name}-1.jsonl").write_text(
                helpers.claude_record("s1", helpers.CLAUDE_USAGE, "2026-10-19T09:01:00.000Z",
                                      agent_id="a1") + "\n", encoding="utf-8")
        store = store_jsonl.claude_store(sid)
        self.assertEqual(store["subagent_dirs_scanned"], 2)
        self.assertTrue(store["files_attribution_verified"], "按 <sessionId>.jsonl 精确命中,归属是核实过的")
        self.assertEqual(len(store["children"]), 2, "两个 project 目录下的 subagents 都要计入")

    def test_gemini_id_match_requires_boundary(self):
        self.assertTrue(store_jsonl._id_present('{"session_id":"aaaa-bbbb"}', "aaaa-bbbb"))
        self.assertFalse(store_jsonl._id_present('{"session_id":"aaaa-bbbb-cccc"}', "aaaa-bbbb"),
                         "短 id 命中更长的 id 会把别人的会话算进来")

    def test_unparsed_timestamps_are_counted(self):
        path = self.tmp / "odd.jsonl"
        lines = [helpers.claude_record("m1", helpers.CLAUDE_USAGE, "2026-10-19T09:00:00.000Z"),
                 json.dumps({"type": "x", "timestamp": "not-a-time"}),
                 json.dumps({"type": "x", "timestamp": "2026-13-45T99:99:99Z"})]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        profile = store_jsonl.time_profile_many([path])
        self.assertEqual(profile["events_with_timestamp"], 1)
        self.assertEqual(profile["events_timestamp_unparsed"], 2)
        self.assertIn("偏小", profile["unparsed_note"])

    def test_long_first_line_in_compressed_file_is_not_truncated(self):
        sid = helpers.CODEX_UUID
        d = self.tmp / ".codex" / "sessions" / "2026" / "10" / "20"
        d.mkdir(parents=True)
        meta = json.loads(helpers.codex_session_meta(sid, cwd="x" * 300_000))
        (d / f"rollout-2026-10-20T00-00-00-{sid}.jsonl.zst").write_bytes(
            helpers.zst_bytes(json.dumps(meta) + "\n" + helpers.codex_turn(1, 0, 1, 0, 2,
                                                                          "2026-10-20T00:00:01.000Z") + "\n"))
        rec = store_jsonl.first_record(next(d.glob("*.zst")))
        self.assertIsNotNone(rec, "首行超过预读窗口时必须回退到完整解压,不能截断")
        self.assertEqual((rec.get("payload") or {}).get("session_id"), sid)

    def test_codex_filename_fallback_flags_attribution(self):
        helpers.build_codex_two_level(self.tmp)
        store = store_jsonl.codex_store("1f2e3d4c-aaaa-bbbb-cccc-ddddeeeeffff")
        self.assertFalse(store["files_attribution_verified"])

    def test_unsupported_platform_still_has_report_fields(self):
        facts = {"sessions": [{"depth": 0, "session_id": "x",
                               "ccusage": collect.summarize(helpers.cc_row("amp", "x", 1, 1, 0, 2))}]}
        out = crosscheck.run(facts, "amp")
        self.assertEqual(out["verdict"], "unsupported")
        self.assertEqual(out["agreement"], "not_applicable")
        self.assertIsNone(out["max_abs_delta_pct"])
        self.assertIsNone(out["minutes_since_last_activity"])

    def _run_with_rows(self, sid, rows, extra=()):
        helpers.build_fake_home(self.tmp)
        with mock.patch.object(collect, "discover_platforms", lambda timeout: (["codex"], "stub")), \
             mock.patch.object(collect, "run_ccusage", lambda offline, timeout: ({"session": rows}, None)):
            stdout = io.StringIO()
            argv = ["collect.py", sid, "--db", str(self.tmp / "none.db"), "--out-dir", str(self.tmp), *extra]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
                try:
                    collect.main()
                except SystemExit as exc:
                    code = exc.code
            full = json.loads(Path(self.tmp, f"agent-report-{sid}-facts.json").read_text(encoding="utf-8"))
            return code, json.loads(stdout.getvalue()), full


class ThirdRoundTest(AdapterTestBase):
    """第三轮独立审查发现的问题:坏压缩文件、越界时间、0=0、verdict 与判定相称、跨文件去重。"""

    def test_corrupt_zst_becomes_decode_error_not_a_crash(self):
        d = self.tmp / ".codex" / "sessions" / "2026" / "10" / "21"
        d.mkdir(parents=True)
        (d / "rollout-2026-10-21T00-00-00-deadbeef-0000-0000-0000-000000000000.jsonl.zst").write_bytes(
            b"\x28\xb5\x2f\xfd" + os.urandom(400))  # 合法 magic,内容是垃圾
        path = next(d.glob("*.zst"))
        with self.assertRaises(jsonl_reader.DecodeError):
            list(jsonl_reader.LineSource(path).lines())
        profile = store_jsonl.scan(path)
        self.assertTrue(profile["decode_error"])
        store = store_jsonl.codex_store("deadbeef-0000-0000-0000-000000000000")
        self.assertEqual(store["rollouts_unreadable"], 1)

    def test_pre_1970_naive_timestamp_does_not_raise(self):
        stamp, basis = crosscheck._parse_stamp("1960-01-01T00:00:00")
        self.assertIsNone(stamp)
        self.assertIn("无法换算", basis)
        minutes, why = crosscheck._gap_minutes("1960-01-01T00:00:00")
        self.assertIsNone(minutes)

    def test_both_zero_field_counts_as_exact_not_no_overlap(self):
        rows = crosscheck.compare({"input_tokens": 0, "output_tokens": 5},
                                  {"input": 0, "output": 5},
                                  [("input", "input_tokens", "input"), ("output", "output_tokens", "output")])
        out = crosscheck._finalize({"comparison": rows}, 900)
        self.assertEqual(out["agreement"], "exact")
        self.assertEqual(out["numeric_fields_compared"], 2)

    def test_verdict_matches_the_agreement_tier(self):
        rows = crosscheck.compare({"input_tokens": 2000}, {"input": 1000},
                                  [("input", "input_tokens", "input")])
        out = crosscheck._finalize({"comparison": rows}, 900)
        self.assertEqual(out["agreement"], "mismatch")
        self.assertEqual(out["verdict"], "needs_attribution", "verdict 不能停留在 ok")
        good = crosscheck._finalize({"comparison": crosscheck.compare({"input_tokens": 1000}, {"input": 1000},
                                                                     [("input", "input_tokens", "input")])}, 900)
        self.assertEqual(good["verdict"], "ok")

    def test_not_applicable_keeps_cited_fields_and_no_fake_gap(self):
        facts = {"sessions": [{"depth": 0, "session_id": "x",
                               "ccusage": {**collect.summarize(helpers.cc_row(
                                   "codex", "x", 1, 1, 0, 2,
                                   last_activity="2030-01-01T00:00:00+00:00")), "input": 1}}]}
        out = crosscheck.run(facts, "codex")  # 没有 files -> not_possible -> not_applicable
        self.assertEqual(out["agreement"], "not_applicable")
        self.assertIsNone(out["minutes_since_last_activity"], "无法复核时不该编造距上次活动的分钟数")
        for field in ("worst_field", "numeric_fields_compared", "comparison", "delta_interpretation"):
            self.assertIn(field, out)

    def test_future_timestamp_flags_clock_skew_and_is_not_recent(self):
        rows = crosscheck.compare({"input_tokens": 1030}, {"input": 1000},
                                  [("input", "input_tokens", "input")])
        out = crosscheck._finalize({"comparison": rows}, -2.0)
        self.assertTrue(out["clock_skew_suspected"])
        self.assertEqual(out["agreement"], "divergent_unexplained", "未来时间戳不能当作活会话的借口")

    def test_aggregate_missing_rows_are_visible(self):
        rows = [{"agent": "codex", "period": "a", "inputTokens": 100, "outputTokens": 5,
                 "totalTokens": 105, "totalCost": 1.0, "metadata": {}},
                {"agent": "codex", "period": "b", "inputTokens": None, "outputTokens": 1,
                 "totalTokens": 1, "totalCost": None, "metadata": {}}]
        agg = collect.aggregate_rows(rows, "s", "codex")
        self.assertEqual(agg["input"], 100)
        self.assertEqual(agg["aggregated_missing"]["input"], 1)
        self.assertEqual(agg["aggregated_missing"]["cost"], 1)
        self.assertIsNone(agg["cache_read"], "整个字段一行都没有时是 None,不是 0")
        self.assertEqual(agg["aggregated_missing"]["cache_read"], 2)
        self.assertEqual(agg["rollouts_aggregated"], 2)

    def test_mixed_case_session_id_still_matches_ccusage_rows(self):
        sid = "ses_Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2"
        index = collect.RowIndex([helpers.cc_row("opencode", sid, 10, 1, 0, 11)])
        self.assertEqual(len(collect.collect_rows(index, {sid.upper()})), 1)
        self.assertEqual(len(collect.collect_rows(index, {sid.lower()})), 1)

    def test_claude_message_repeated_across_project_files_counted_once(self):
        sid = helpers.CLAUDE_UUID
        one = self.tmp / ".claude" / "projects" / "-a" / f"{sid}.jsonl"
        two = self.tmp / ".claude" / "projects" / "-b" / f"{sid}.jsonl"
        one.parent.mkdir(parents=True, exist_ok=True)
        two.parent.mkdir(parents=True, exist_ok=True)
        text = helpers.claude_record("msg_shared", helpers.CLAUDE_USAGE, "2026-10-19T09:00:00.000Z", repeats=3)
        one.write_text(text + "\n", encoding="utf-8")
        two.write_text(text + "\n", encoding="utf-8")
        usage = store_jsonl.claude_usage_from_files([one, two])
        self.assertEqual(usage["usage_records_raw"], 6)
        self.assertEqual(usage["input_tokens"], 1000, "跨文件必须按 message.id 全局去重,不能逐文件去重后相加")
        self.assertEqual(usage["unique_message_ids"], 1)
        self.assertEqual(crosscheck.claude_check([one, two])["input_tokens"], 1000)

    def test_fork_from_a_continuation_rollout_is_found(self):
        """子会话的 forked_from_id 指向父会话某一份续跑 rollout 的 id,而不是 session_id。"""
        d = self.tmp / ".codex" / "sessions" / "2026" / "10" / "22"
        d.mkdir(parents=True)
        parent = "1a000000-0000-0000-0000-000000000001"
        r2 = "1a000000-0000-0000-0000-000000000099"
        (d / f"rollout-a-{parent}.jsonl").write_text(
            helpers.codex_session_meta(parent, rollout_id="1a000000-0000-0000-0000-000000000098") + "\n",
            encoding="utf-8")
        (d / f"rollout-b-{parent}.jsonl").write_text(
            helpers.codex_session_meta(parent, rollout_id=r2) + "\n", encoding="utf-8")
        (d / "rollout-c-2b000000-0000-0000-0000-000000000002.jsonl").write_text(
            helpers.codex_session_meta("2b000000-0000-0000-0000-000000000002",
                                       forked_from=r2,
                                       rollout_id="2b000000-0000-0000-0000-0000000000ff") + "\n",
            encoding="utf-8")
        store = store_jsonl.codex_store(parent)
        self.assertEqual([c["session_id"] for c in store["children"]], ["2b000000-0000-0000-0000-000000000002"],
                         "只比 session_id 会漏掉从续跑分片派生的子会话")
        self.assertTrue(store["children_verified"])

    def test_id_present_accepts_rollout_prefixed_uuid(self):
        sid = "f1f1f1f1-2222-4222-8222-222222222222"
        self.assertTrue(store_jsonl._id_present(f'file: rollout-2026-10-19T12-07-22-{sid}.jsonl', sid))
        self.assertTrue(store_jsonl._id_present('"sid":"%s"' % sid, sid))
        self.assertFalse(store_jsonl._id_present('"sid":"aaaa-bbbb-cccc"', "aaaa-bbbb"))


    def test_family_totals_distinguish_zero_from_missing(self):
        """$0 是真值、null 是缺失:两者在合计里必须走不同路径。"""
        helpers.build_codex_two_level(self.tmp)
        rid_a, rid_b = helpers.PARENT_ROLLOUTS
        parent = helpers.cc_row("codex", f"2026-10-19/rollout-2026-10-19T10-00-00-{rid_a}", 700, 400, 2300, 5030,
                                cost=0.0, reasoning=130, last_activity="2026-10-19T10:10:00.000Z")
        missing_total = {"agent": "codex",
                         "period": "2026-10-19/rollout-2026-10-19T12-00-00-c3c3c3c3-5555-4555-8555-555555555555",
                         "inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 400,
                         "cacheCreationTokens": None, "totalTokens": None, "totalCost": None,
                         "modelsUsed": [], "metadata": {"lastActivity": "2026-10-19T12:00:00.000Z"},
                         "modelBreakdowns": []}
        code, view, full = self.run_collect(helpers.PARENT_SESSION, [parent, missing_total])
        family = full["family_totals"]
        self.assertEqual(family["cost_sum"], 0.0, "真实计价的 $0 必须保持 0,不能变成 null")
        self.assertEqual(family["cost_missing_rows"], 1)
        self.assertEqual(family["sum"]["total"], 5030)
        self.assertEqual(family["sum_missing_sessions"]["total"], 1)
        self.assertEqual(family["sum"]["input"], 800)
        self.assertIn("下限", full["coverage_warning"])

    def test_unpriced_everywhere_yields_no_cost_number(self):
        sid = helpers.CLAUDE_UUID
        rows = [helpers.cc_row("claude", sid, 1000, 200, 500, 1700, cost=0.0,
                               last_activity="2026-10-19T09:00:00.000Z")]
        rows[0]["modelBreakdowns"] = [{"modelName": "brand-new-model", "missingPricing": True}]
        rows[0]["totalCost"] = None
        code, view, full = self.run_collect(sid, rows)
        self.assertEqual(full["ccusage"]["pricing_verdict"], "unpriced_everywhere")
        self.assertEqual(full["family_totals"]["cost_sum"], None)
        self.assertIn("不是 $0", full["family_totals"]["cost_note"])


class FourthRoundTest(AdapterTestBase):
    """第四轮:字符串成本、升级后计数、大写血缘、sessionId 形态的行、空本地侧不得判 exact。"""

    def test_string_cost_in_compare_pricing_does_not_crash(self):
        offline = {"agent": "codex", "period": "ses_x", "inputTokens": 1, "outputTokens": 1,
                   "totalTokens": 2, "totalCost": "1.50", "metadata": {}, "modelBreakdowns": [], "modelsUsed": []}
        online = dict(offline, totalCost="1.25")
        with mock.patch.object(collect, "run_ccusage", lambda offline_flag, timeout: ({"session": [online]}, None)):
            rows, cc = collect.pricing_block({"ses_x"}, [offline], False, 5, True)
        self.assertAlmostEqual(cc["cost_delta_between_sources"], 0.25)

    def test_escalation_refreshes_rollout_count(self):
        sid = helpers.CODEX_UUID
        helpers.build_codex_flat(self.tmp, sid)
        offline_rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 700, 400, 2300, 5030,
                                       last_activity="2026-10-19T10:10:00.000Z")]
        offline_rows[0]["modelBreakdowns"] = [{"modelName": "new", "missingPricing": True}]
        online_rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 700, 400, 2300, 5030,
                                      last_activity="2026-10-19T10:10:00.000Z"),
                       helpers.cc_row("codex", f"2026-10-19/rollout-b-{sid}", 100, 50, 400, 560,
                                      last_activity="2026-10-19T10:20:00.000Z")]
        calls = {"n": 0}

        def fake(offline_flag, timeout):
            calls["n"] += 1
            return ({"session": online_rows}, None) if not offline_flag else ({"session": offline_rows}, None)

        with mock.patch.object(collect, "run_ccusage", fake):
            code, view, full = self.run_collect(sid, offline_rows, extra_args=("--offline-only",),
                                                build_home=False, cc_func=fake)
        main = full["sessions"][0]
        self.assertEqual(full["ccusage"]["pricing_verdict"], "priced_online_only")
        self.assertEqual(main["ccusage"]["rollouts_aggregated"], 2)
        self.assertEqual(main["ccusage_rollouts_aggregated"], 2, "升级实时后出行数必须跟着重算")

    def test_uppercase_forked_from_id_still_finds_children(self):
        d = self.tmp / ".codex" / "sessions" / "2026" / "10" / "1"
        d.mkdir(parents=True)
        parent = "1a000000-0000-0000-0000-000000000001"
        (d / "rollout-a.jsonl").write_text(
            helpers.codex_session_meta(parent, rollout_id="1a000000-0000-0000-0000-000000000098") + "\n",
            encoding="utf-8")
        (d / "rollout-c.jsonl").write_text(
            helpers.codex_session_meta("2b000000-0000-0000-0000-000000000002",
                                       forked_from=parent.upper(),
                                       rollout_id="2B000000-0000-0000-0000-0000000000FF") + "\n",
            encoding="utf-8")
        store = store_jsonl.codex_store(parent)
        self.assertEqual([c["session_id"] for c in store["children"]], ["2b000000-0000-0000-0000-000000000002"])

    def test_row_identified_by_sessionId_field_is_indexed(self):
        sid = "d4d4d4d4-6666-4666-8666-666666666666"
        row = {"agent": "claude", "period": "2026-10-19", "sessionId": sid,
               "inputTokens": 10, "outputTokens": 1, "totalTokens": 11, "metadata": {}, "modelBreakdowns": []}
        index = collect.RowIndex([row])
        self.assertEqual(len(collect.collect_rows(index, {sid})), 1,
                         "period 只是日期、真实 id 在 sessionId 的行不能消失")
        self.assertEqual(len(collect.collect_rows(index, {sid.upper()})), 1)

    def test_empty_local_side_cannot_be_reported_as_exact(self):
        sid = helpers.CLAUDE_UUID
        main_name = f"{sid}.jsonl"
        real_lines = jsonl_reader.LineSource.lines

        def failing(self):
            # 只针对主 transcript:子代理文件名是 agent-<sid>.jsonl,
            # 而临时目录本身就叫 agent-report-test-*,所以必须按 basename 精确判定
            if os.path.basename(str(self.path)) == main_name:
                raise jsonl_reader.DecodeError("模拟主 transcript 解码失败")
            return real_lines(self)

        rows = [helpers.cc_row("claude", sid, 0, 0, 0, 0, last_activity="2026-10-19T09:00:00.000Z")]
        with mock.patch.object(jsonl_reader.LineSource, "lines", failing):
            code, view, full = self.run_collect(sid, rows)
        check = full["crosscheck"]
        self.assertEqual(check["verdict"], "local_read_failed")
        self.assertEqual(check["agreement"], "not_applicable", "两边都是 0 不能算复核通过")
        self.assertIn("不能因为两边都显示 0", check["coverage_warning"])

    def test_no_numeric_overlap_fills_cited_fields(self):
        out = crosscheck._finalize({"comparison": [{"field": "input", "mine": None, "ccusage": None,
                                                    "delta": None, "delta_pct": None}]}, 10)
        self.assertEqual(out["agreement"], "no_numeric_overlap")
        for field in ("worst_field", "numeric_fields_compared", "max_abs_delta_pct"):
            self.assertIn(field, out)

    def test_family_field_missing_everywhere_is_null_not_zero(self):
        sid = helpers.CODEX_UUID
        helpers.build_codex_flat(self.tmp, sid)
        rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 1000, 200, 500, None,
                               last_activity="2026-10-19T09:00:00.000Z")]
        rows[0]["totalTokens"] = None
        code, view, full = self.run_collect(sid, rows, build_home=False)
        family = full["family_totals"]
        self.assertIsNone(family["sum"]["total"])
        self.assertIn("total", family["sum_unavailable_fields"])
        self.assertEqual(family["sum"]["input"], 1000)
        self.assertIn("null 而不是 0", full["coverage_warning"])


class FifthRoundTest(AdapterTestBase):
    """第五轮:部分读取不能弃整段复核、opencode 空对空、子分片进树、后缀兜底匹配。"""

    def test_partial_local_read_keeps_the_comparison(self):
        """主 transcript 有多份、其中一份读不到:必须继续比对并标注 mine 是下限。"""
        sid = helpers.CLAUDE_UUID
        for tag, mid in (("-a", "msg_a"), ("-b", "msg_b")):
            d = self.tmp / ".claude" / "projects" / tag / sid
            d.mkdir(parents=True, exist_ok=True)
            (self.tmp / ".claude" / "projects" / tag / f"{sid}.jsonl").write_text(
                helpers.claude_record(mid, helpers.CLAUDE_USAGE, "2026-10-19T09:00:00.000Z", repeats=2) + "\n",
                encoding="utf-8")
        real = jsonl_reader.LineSource.lines

        def failing(self_):
            if "-b" in str(self_.path).split("projects")[1].split(sid)[0]:
                raise jsonl_reader.DecodeError("模拟其中一份主 transcript 解码失败")
            return real(self_)

        rows = [helpers.cc_row("claude", sid, 2000, 400, 1000, 3400,
                               last_activity="2026-10-19T09:04:00.000Z")]
        with mock.patch.object(jsonl_reader.LineSource, "lines", failing):
            code, view, full = self.run_collect(sid, rows, build_home=False)
        check = full["crosscheck"]
        self.assertEqual(len(check["mine"]["files_unreadable"]), 1)
        self.assertEqual(check["mine"]["input_tokens"], 1000, "只统计到读成功的那一份,是下限")
        self.assertTrue(check.get("partial_local_read"))
        self.assertNotEqual(check["agreement"], "not_applicable", "部分读取仍要给出判定")
        self.assertGreaterEqual(check["numeric_fields_compared"], 3)
        self.assertEqual(check["agreement"], "mismatch", "少读一份主文件应暴露成 50% 缺口")
        self.assertIn("下限", check["coverage_warning"])

    def test_opencode_empty_assistant_rows_is_not_exact(self):
        import sqlite3

        spec = helpers.build_opencode_db(self.tmp / "oc.db")
        con = sqlite3.connect(str(spec["db"]))
        con.execute("DELETE FROM session_message WHERE session_id=?", (spec["parent_id"],))
        con.commit()
        con.close()
        rows = [helpers.cc_row("opencode", spec["parent_id"], 0, 0, 0, 0,
                               last_activity="2026-10-19T10:00:00.000Z")]
        code, view, full = self.run_collect(spec["parent_id"], rows, db=str(spec["db"]))
        check = full["crosscheck"]
        self.assertEqual(check["verdict"], "local_read_failed")
        self.assertEqual(check["agreement"], "not_applicable", "本地一行都没读到,0 对 0 不算复核通过")

    def test_escalation_keeps_child_rollout_numbers(self):
        helpers.build_codex_two_level(self.tmp)
        rid_a, _ = helpers.PARENT_ROLLOUTS
        child_period = ("2026-10-19/rollout-2026-10-19T12-00-00-"
                        "c3c3c3c3-5555-4555-8555-555555555555")
        offline_parent = helpers.cc_row("codex", f"2026-10-19/rollout-2026-10-19T10-00-00-{rid_a}",
                                        700, 400, 2300, 5030, reasoning=130,
                                        last_activity="2026-10-19T10:10:00.000Z")
        offline_child = helpers.cc_row("codex", child_period, 100, 50, 400, 560, reasoning=10,
                                       last_activity="2026-10-19T12:00:00.000Z")
        unpriced = [{"modelName": "new", "missingPricing": True}]
        priced = [{"modelName": "new", "missingPricing": False}]
        offline_parent["modelBreakdowns"] = unpriced
        offline_child["modelBreakdowns"] = unpriced
        online_parent = dict(offline_parent, modelBreakdowns=priced)
        online_child = dict(offline_child, modelBreakdowns=priced)

        def fake(offline_flag, timeout):
            if offline_flag:
                return {"session": [offline_parent, offline_child]}, None
            return {"session": [online_parent, online_child]}, None

        code, view, full = self.run_collect(helpers.PARENT_SESSION, [offline_parent, offline_child],
                                            extra_args=("--offline-only",), build_home=False, cc_func=fake)
        self.assertEqual(full["ccusage"]["pricing_verdict"], "priced_online_only")
        child = next(s for s in full["sessions"] if s["depth"] == 1)
        self.assertIsNotNone(child["ccusage"], "升级实时后子分片不能由有变无")
        self.assertEqual(child["ccusage_basis"], "litellm_online")
        self.assertEqual(full["family_totals"]["bases"], ["litellm_online"],
                         "升级后整棵树必须是同一口径,不能父实时子缓存")
        self.assertEqual(full["family_totals"]["sum"]["input"], 800)

    def test_non_uuid_id_matching_falls_back_to_suffix(self):
        row = {"agent": "kimi", "period": "2026-10-19/workspace-myproject-tail-9f8e7d6a",
               "inputTokens": 6, "outputTokens": 1, "cacheReadTokens": 0, "cacheCreationTokens": 0,
               "totalTokens": 7, "totalCost": 0.1, "metadata": {}, "modelsUsed": [], "modelBreakdowns": []}
        index = collect.RowIndex([row])
        self.assertEqual(len(collect.collect_rows(index, {"tail-9f8e7d6a"})), 1,
                         "非 UUID 形态的 id 只作为长 period 的后缀出现时也要命中")

    def test_missing_cost_rows_produce_a_warning(self):
        sid = helpers.CODEX_UUID
        helpers.build_codex_flat(self.tmp, sid)
        rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 700, 400, 2300, 5030,
                               last_activity="2026-10-19T10:10:00.000Z"),
                helpers.cc_row("codex", f"2026-10-19/rollout-b-{sid}", 100, 50, 400, 560,
                               last_activity="2026-10-19T10:20:00.000Z")]
        rows[1]["totalCost"] = None
        code, view, full = self.run_collect(sid, rows, build_home=False)
        # 两行同属一个会话 -> 缺失记在聚合层;跨会话缺失才记 cost_missing_rows
        main = [s for s in full["sessions"] if s["depth"] == 0][0]
        self.assertEqual(main["ccusage"]["aggregated_missing"]["cost"], 1)
        self.assertEqual(main["ccusage"]["cost"], 1.0)
        family = full["family_totals"]
        self.assertEqual(family["cost_sum"], 1.0)
        self.assertNotIn("cost_missing_rows", family, "同一会话内的缺失不该记成跨会话缺失")


class SelfReferenceTest(AdapterTestBase):
    """在被分析的会话里跑采集会自我污染:必须给出可见告警,而不是安静地把开销算进去。"""

    def _rows_for_live_session(self):
        sid = helpers.CODEX_UUID
        helpers.build_codex_flat(self.tmp, sid)
        rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 200, 100, 800, 1100, reasoning=40,
                               last_activity=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"))]
        return sid, rows

    def test_live_target_raises_self_reference_warning(self):
        sid, rows = self._rows_for_live_session()
        code, view, full = self.run_collect(sid, rows, build_home=False)
        self.assertTrue(full["self_reference_suspected"])
        self.assertIn("测量这次测量", full["coverage_warning"])
        self.assertIn("另一个会话", full["coverage_warning"])

    def test_allow_live_target_suppresses_it(self):
        sid, rows = self._rows_for_live_session()
        code, view, full = self.run_collect(sid, rows, extra_args=("--allow-live-target",), build_home=False)
        self.assertNotIn("self_reference_suspected", full)
        self.assertNotIn("测量这次测量", json.dumps(full, ensure_ascii=False))

    def test_finished_session_is_not_flagged(self):
        sid = helpers.CODEX_UUID
        helpers.build_codex_flat(self.tmp, sid)
        rows = [helpers.cc_row("codex", f"2026-10-19/rollout-a-{sid}", 200, 100, 800, 1100, reasoning=40,
                               last_activity="2020-01-01T00:00:00.000Z")]
        code, view, full = self.run_collect(sid, rows, build_home=False)
        self.assertNotIn("self_reference_suspected", full)

    def test_guard_tolerates_missing_and_future_timestamps(self):
        for gap in (None, -3.0, 900.0):
            facts = {"crosscheck": {"minutes_since_last_activity": gap}, "coverage_warnings": []}
            collect.self_reference_guard(facts)
            self.assertNotIn("self_reference_suspected", facts)


if __name__ == "__main__":
    unittest.main()
