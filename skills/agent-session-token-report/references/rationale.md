# 设计依据与实测记录

`SKILL.md` 只写"该怎么做"。这份文件记录**为什么这样定**：每个口径当初是怎么发现的、错了会错多少、以及是哪段代码和哪个测试在守住它。只有在需要解释某个字段为什么是现在这个语义、或打算改动取数逻辑时才读它。

文中的比例与量级来自开发机上的真实会话；账号级的绝对数字（具体金额、具体条数）不写在这里，避免把使用者的用量数据带进发布文件。

## ccusage 调用面

- **通用 `ccusage session --json` 是骨干**：一次返回本机所有平台的行，`agent` 是平台名，`period` 是会话标识。平台专属子命令（`ccusage opencode session` 等）字段更少，例如不输出 reasoning，所以只作为备用。
  → 实现：`collect.run_ccusage`；防线：`FourthRoundTest.test_escalation_keeps_child_rollout_numbers`。
- **`ccusage session -i <id>` 不可信**：对 opencode / gemini / codex 返回字面量 `null`，对 claude 返回空数组，而全量输出里这些会话明明存在。因此一律全量取回、在客户端按 `period` 匹配。任何"用 -i 省事"的改动都会得到空结果而不是报错，属于最容易被静默吞掉的坑。
  → 实现：`collect.run_ccusage` 的 docstring；`RowIndex` 负责把一次全量结果建成索引。
- **定价默认走实时 LiteLLM**（`--offline` 默认 false）。本地缓存缺条目时 ccusage 给出 `$0`，看起来像"免费"，实时却有价；也存在两边都无价的模型。这就是 `pricing_verdict` 必须存在、且 `cost=0` 不能直接写进报告的原因。
  → 实现：`collect.pricing_block`、`pricing_verdict_for`；防线：`PricingVerdictTest`、`FourthRoundTest.test_unpriced_everywhere_yields_no_cost_number`。

## codex

- **一个会话跨多个 rollout 文件**，其中相当比例是 `.jsonl.zst` 压缩存档。只取单行会少算约一个数量级——分片数与真实总量的差距是"看起来像测试会话的几十倍"这种量级。这是整个适配器存在的理由。
  → 实现：`store_jsonl.codex_files` + `codex_store` 按 session_id 归并；防线：`CodexRoundUsageTest.test_store_indexes_all_rollouts_for_one_session`、`PipelineTest.test_codex_pipeline_aggregates_rollouts`。
- **`session_meta.payload.id` 与 `payload.session_id` 是两个不同的 id**：前者是这份 rollout 自己的 id（通常等于文件名里那段），后者才是会话 id。真机验证时发现：适配器早期只认 `session_id`，用户传入 rollout id 时会掉进"按文件名兜底"分支，结果是**把 20 个分片的会话当成 1 个**上报，成本和 token 都只剩零头。现在按 `payload.id → session_id` 做别名映射，并在 `scope_notes` / `requested_id` 里保留原始输入。
  → 实现：`codex_store` 的 `alias` 与 `requested_id_form`；防线：`CodexRoundUsageTest.test_rollout_id_resolves_to_session_id_and_verifies_children`、`test_fork_from_a_continuation_rollout_is_found`。
- **续跑分片会把 `forked_from_id` 写成本会话自己**，所以只有最早那份的 `forked_from_id` 才是真实父引用；另外派生可能指向任意一份 rollout 的 id，父集合必须把本会话所有 rollout id 都算进去，否则会漏子会话而 `children_verified` 仍为 true。
  → 实现：`codex_store` 的 `_fork_best_stamp` 与 `parents`；防线：`FifthRoundTest` 的血缘用例。
- **token 语义**：`token_count.info.input_tokens` 已包含 `cached_input_tokens`，而 ccusage 把缓存单列，所以复核要先做 `input - cached` 的拆分对齐。ccusage 的口径是**逐轮 `last_token_usage` 累加并按 usage 数值去重**（同一轮的用量有时被写两份，时间戳不同、数值相同）。按这条规则，`input/output/cache_read/reasoning` 与 ccusage 逐字段完全相同；只有记录自带的 `total_tokens` 比 `input+output+cache_read` 高约 0.1%。
- **不能用每文件末尾的 `total_token_usage` 相加**：续跑分片会把历史累计重新带进来，实测把 input 抬高约三分之一、total 抬高约一成。`crosscheck.mine.cumulative_vs_per_turn_note` 就是这个提醒。
  → 实现：`crosscheck.codex_file_tokens` / `codex_check`；防线：`CodexRoundUsageTest.test_duplicate_turn_usage_skipped_and_cumulative_not_used`。
- **`.zst` 需要多后端回退**（python `zstandard` → Node `zlib.zstdDecompress` → `zstd` CLI）。索引阶段只为拿首行而全量解压所有分片曾是最贵的一步，现在 `python-zstandard` 走前缀预读，首行超出预读窗口时自动回退完整解压——截断的首行会让会话被误判为"没有 session_meta"。
  → 实现：`jsonl_reader._decompress_prefix` / `LineSource.first_line`；防线：`HardeningTest.test_long_first_line_in_compressed_file_is_not_truncated`。

## claude

- **每条 assistant 消息的 usage 被重复写约 3 份**，必须按 `message.id` 去重取末次；不去重求和会稳定地得到 ccusage 的约 3 倍。
- **主 transcript 只含 `isSidechain:false`**，子代理用量不在父行里，家族总量必须把 `subagents/agent-*.jsonl` 另加，于是同一棵树里天然存在两种 basis（`ccusage` 与 `file_dedup_sum`），这就是 `mixed_basis` 字段存在的理由。
- **一个 sessionId 可能在多个 project 目录下都有 transcript**（会话换过工作目录）。逐文件去重再相加会把同一条 message 重复计入，导致本地侧系统性偏高、复核报出假 mismatch。因此去重作用域是"跨全部主文件"，不是"单文件内"。
  → 实现：`store_jsonl.claude_usage_from_files`（唯一实现，适配器与复核共用）；防线：`ThirdRoundTest.test_claude_message_repeated_across_project_files_counted_once`、`ClaudeEmptyGuardTest`。

## opencode

- **ccusage 不统计 `compaction` 行**，这部分实测可占整会话 input 的几个百分点。`assistant_tokens` 与 `compaction_summary` 的差就是它——所以复核按消息类型求和，并把 compaction 单独列出解释差额，而不是含糊地说"口径略有不同"。
  → 实现：`store_opencode.session_scan` 的分类型汇总；防线：`PipelineTest.test_opencode_pipeline_explains_compaction_gap`。
- **`totalTokens` 会把 reasoning 再加一次**（与 codex 相反），所以两边的 total 不可直接比较。
- **SQLite 里按小时分组**必须用 `strftime('%H', time_created/1000,'unixepoch','localtime')`；写成对 `datetime(...)` 的结果再取小时会静默丢行。现在改为在 Python 侧按本地时间取小时，并保留 `hourly_reconciles` 作为自检位——分时段之和必须等于 assistant 之和。
- **消息 `data` 列可能不是 JSON 对象**（数组、裸字符串、损坏）。这类行计入 `unparsable_message_rows` 而不是崩掉整次采集。

## 复核的独立性

复核的独立轴是**"本地存储 vs ccusage"**，不是"两份实现互相印证"。Claude 的去重求和在适配器与复核之间是同一份实现——曾有过两份各写各的版本，结果两条规则悄悄分叉（匿名记录与 uuid 回退的处理不同），比"没有复核"更危险。共用一份实现后，`crosscheck.mine.method` 里明确写了这一点，报告也据此措辞，不得声称"两套代码互相验证"。

## 为什么禁止"在被分析的会话里跑采集"

观测者效应在这里是具体的、可算的：用户那句"分析这个会话"和随后这次工具调用会先写进会话存储，再被同一份脚本统计进去，于是报告统计的对象包含"测量这次测量"自身的开销；fact pack 与报告正文还要写回同一个会话的上下文，成本随报告长度继续上涨。更要紧的是复核环节失去意义——`snapshot_skew_likely` 的定义是"两次快照之间有新增请求"，而自指场景下这必然成立，任何偏差都会被错误地归因成快照错开。

判据上**没有**可靠信号可用：本机实测 Qoder agent 子进程的环境里只有 `QODERCN_CLI=1`、`QODER_AGENT_SDK_ENTRYPOINT` 这类标记，**不含当前会话 id**，所以无法把"目标 id"与"我自己"做精确比较。退而求其次用两个可验证信号的组合：目标会话在读取前 5 分钟内仍在写入（`crosscheck.minutes_since_last_activity`，本来就算好了）+ 采集进程带有 agent CLI 标记。命中就置 `self_reference_suspected` 并给出必须转述的告警；确认已经从别处跑的，用 `--allow-live-target` 关掉。这是个偏保守的判定（刚结束 5 分钟内的正常会话也会提示），换来的代价是一行告警，而漏掉的代价是一份看起来精确、实际被自身污染的报告。

  → 实现：`collect.self_reference_guard`；防线：`SelfReferenceTest`（活会话命中、`--allow-live-target` 抑制、已结束会话不误报、时间戳缺失/未来值不崩）。

## 一次采集的 I/O 预算

同一份文件过去被时间分布、标记计数、独立复核各扫一遍；现在一次 `scan()` 同时产出时间戳、去重用量、标记计数，并按 `(路径, mtime, 大小)` 复用，`.zst` 的解压结果另有进程内字节预算缓存。开发机上的实测：codex 会话从 24 秒降到 5 秒，170MB 级 claude 主 transcript 单遍约 7.5 秒。改动取数路径后应重新测这两条，性能回退通常意味着又出现了重复扫描。
