---
name: agent-session-token-report
description: 统计 agent CLI 会话及其子会话的 token 消耗与成本,独立复核 ccusage 的数字,并输出 markdown 报告。完整支持 opencode、claude、codex、gemini 四个平台(各有本地存储适配器与交叉复核);其他 ccusage 平台(amp、droid、kimi、qwen、copilot、goose、kilo 等)只能给出 ccusage 的聚合数字,不保证统计完整。当用户给出会话 ID(opencode 形如 ses_xxx,其余平台多为 UUID)要求查 token、用量、成本、消耗明细,提到 ccusage,或要求"分析某会话及其子会话并出报告"时使用。输入为会话 ID,输出为当前工作目录下的报告文件。
user-invocable: true
argument-hint: <session-id> [--platform NAME]
---

# agent 会话 Token 报告

## Overview

从一个会话 ID 出发:判定平台 → 找出子会话 → 取 ccusage 的 token 与成本 → 自己从原始存储再算一份做交叉复核 → 产出只含该会话树的 markdown 报告。取数与复核由 `scripts/collect.py` 完成,判读与成文由你完成。

## 支持范围(必须如实转述)

本技能只对 **opencode、claude、codex、gemini** 实现了本地存储适配器,只有这四个平台的会话树、子会话与总量完整性是可验证的。其余 ccusage 平台(amp、droid、codebuff、hermes、pi、goose、kilo、copilot、antigravity、kimi、qwen、openclaw、grok、zcode 等)**只能给出 ccusage 的聚合数字**:无法确认是否存在子会话、无法核对一个会话对应几行输出、没有独立复核。fact pack 的 `support.tier` 给出 `full` / `partial` / `aggregate_only`,`support.report_note_zh` 是可直接粘贴的声明文本 —— 报告第一节必须原样引用它,不能只在文末备注里提。

## 工作流

1. **先确认目标会话不是你所处的这个会话**，然后**立刻运行采集脚本**（会话存储会被客户端清理，opencode 数小时内就会删历史会话，晚一步 ID 就不在库里）。在被分析的会话里跑采集会自我污染：那条提问与随后的工具调用先写进会话存储、再被同一份脚本统计进去，报告统计的就是"测量这次测量的开销"；fact pack 与报告正文还会继续撑大这个会话的上下文，`snapshot_skew_likely` 一类判定同时失效。用户让你分析**当前会话**时，先说明这一点并请他换到另一个会话或普通终端跑，**不要直接执行**。

   ```bash
   python <skill-dir>/scripts/collect.py <session-id>
   ```

   参数:`--platform NAME`(默认 auto:先取 ccusage 行的 `agent`,再翻本地存储)、`--db PATH`(opencode.db)、`--out-dir DIR`(默认当前目录)、`--offline-only`(用本地缓存定价,缺价时自动升级实时)、`--compare-pricing`(再跑另一种定价来源核对缓存是否过期)、`--no-crosscheck`(跳过独立复核,默认开启)、`--no-ccusage`(只读本地存储)、`--full-output`(stdout 打完整包而不是精简视图)、`--allow-live-target`(目标会话仍在写入时不再提示自指污染,仅在确认换过会话跑之后用)、`--timeout SEC`。环境变量 `AGENT_CCUSAGE_PKG` 可把包名锁成 `ccusage@固定版本`。

   退出码:0 成功;**2** opencode 存储不可用(库被占用/损坏/不是 SQLite,stdout 带 `store_error`,与"会话不存在"是两回事);**3** 会话在 opencode.db 中不存在(stdout 附 `recent_sessions`);**1** 未捕获异常,此时 fact pack **没有落盘**,stdout 只有一个 `error` 字段。出现 2 或 1 时按 stdout 的说明向用户报告,不要伪造任何数字。出现 3 或 `ccusage_warning` 时,把候选/告警原样转述给用户确认,**不要猜别的会话**。

2. **读两份数据**。stdout 是**精简视图**(已去掉原始 ccusage 行、逐分片 rollout id、绝对路径、`local_store` 与 `sessions` 的重复字段);完整事实包在 `fact_pack_written_to` 指向的 `agent-report-<id>-facts.json`。报告第 7、8 节要用的分片明细、compaction 逐条时间、原始行只在那个文件里 —— 需要就按 key 去读它,不要凭 stdout 猜,也不必为凑细节重跑采集。

3. 写报告 `agent-report-<id>.md` 到当前工作目录。

4. 按下面的检查清单自查后交付。

## 输出契约(读这些字段,不要自己再算)

| 字段 | 用途 |
| ---- | ---- |
| `support.tier` / `support.report_note_zh` | 第 1 节支持范围声明,原样引用 |
| `platform` / `platform_resolution` / `store_reused_from_platform_probe` | 平台与判定依据 |
| `ccusage.pricing_verdict` | 第 5 节成本措辞 |
| `ccusage.unpriced_models` / `sessions[].ccusage.missing_pricing` | 点名列不出价的模型 |
| `crosscheck.verdict` / `agreement` / `max_abs_delta_pct` / `worst_field` / `minutes_since_last_activity` / `last_activity_basis` / `comparison` / `unbounded_delta_fields` | 第 6 节复核 |
| `family_totals.sum` / `bases` / `mixed_basis` / `sum_is_partial` / `sessions_without_numbers` / `per_session[]` / `sum_missing_sessions` | 第 4 节合计与其完整性 |
| `family_totals.cost_sum` / `cost_rows` / `cost_missing_rows` / `cost_note` | `cost_sum` 为 `0` 是"计过价且确实 0",为 `null` + `cost_note` 是"没有成本数值" |
| `crosscheck.partial_local_read` / `crosscheck.coverage_warning` | 本地只读到一部分时 `mine` 是**下限**:比对照旧,但 mine 小于 ccusage 要先归因于这里的缺失,不要说成口径差 |
| `sessions[].ccusage.aggregated_missing` / `family_totals.sum_missing_sessions` / `family_totals.sum_unavailable_fields` | 缺失是 `null` 不是 0:有这些字段时,对应的数是"已知行的下限"或"未知",不能当确切值引用 |
| `has_children` / `children_verified` / `children_mechanism` / `children_note` / `scope_notes` | 第 3 节子会话结论。`has_children` 只有在 `children_verified=true` 时才是核查过的 `true/false`;为 `null` 或 `children_verified=false` 时一律写"未验证" |
| `local_store.files_attribution_verified` / `local_store.subagent_dirs_scanned` | 主会话文件归属是否核实;claude 扫了几个 project 目录 |
| `sessions[].requested_id` / `local_store.requested_id_form` | 传入 id 的形态(rollout id 会被映射到会话 id),第 2 节要说明用的是哪个 |
| `coverage_warning`(单数,拼接串)/ `coverage_warnings`(明细列表) | **必须转述**,决定总量是否偏低 |
| `self_reference_suspected` | 为真说明目标会话此刻仍在写入,**极可能就是你正在使用的这个会话**:抬头必须先向用户声明测量自指风险,不能直接给结论 |
| `local_store_extras.timing.events_timestamp_unparsed` / `unparsed_note` | 有 timestamp 但解析不出来的行数,时间分布因此偏小 |
| `local_store.rollouts_unreadable` / `local_store_extras.unreadable_files` / `crosscheck.mine.files_unreadable` / `opencode.sessions[].unparsable_message_rows` | 覆盖度证据的具体数字 |
| `sessions[].ccusage.rollouts_aggregated` / `multi_rollout_note` / `compressed_files` | 分片聚合说明 |

## 平台能力矩阵

| 平台     | 会话 ID 形态              | 子会话机制                                                             | 本地适配器          | 独立复核                                     |
| -------- | ------------------------- | ---------------------------------------------------------------------- | ------------------- | -------------------------------------------- |
| opencode | `ses_...`                 | `session_v2.parent_id` 独立子会话                                      | `store_opencode.py` | 有:按消息类型求和,可精确解释 compaction 差额 |
| claude   | UUID                      | `<project>/<uuid>/subagents/agent-*.jsonl`(sidechain,共用父 sessionId) | `store_jsonl.py`    | 有:主 transcript 按 `message.id` 去重求和    |
| codex    | UUID(可传 rollout 路径尾) | `session_meta.payload.forked_from_id` 指回本会话即派生                 | `store_jsonl.py`    | 有:逐轮 `last_token_usage` 累加              |
| gemini   | UUID                      | 未发现父引用或子会话目录                                               | `store_jsonl.py`    | 无:只报 ccusage 数字                         |
| 其余平台 | 依 ccusage                | 未实现                                                                 | 无                  | 无 —— **仅聚合数字,不保证统计完整**          |

`crosscheck.verdict = unsupported`,或 `has_children` 为 `null`,或 `children_verified` 为 `false`(gemini 没有已发现的派生机制、codex 只按文件名兜底命中),报告必须写"子会话情况未验证",**不能写成"没有子会话"**。`has_children` 只有配上 `children_verified=true` 才是核查过的结论。

## 措辞红线

只列会影响你怎么写报告的规则；字段语义一律以上面的输出契约表为准，**不要自己从原始数据重算或推断**。设计依据和实测记录在 `references/rationale.md`，只在需要解释某条口径由来时读它。

- **不要绕过脚本自己调 ccusage**：`ccusage session -i <id>` 在这四个平台都拿不到行（返回 `null` 或空数组），看起来像"没有用量"。要用就全量取回后按 `period` 匹配，而脚本已经做过这件事。
- **codex 有两个 id**：以 `sessions[].session_id` 为会话边界，`requested_id` 只是用户传入的原始形式（可能是某份 rollout 的 id）。第 2、3 节要说清用的是哪个，别把 rollout id 当会话 id。
- **total 与 reasoning 的含法两个平台相反**：`total` 引用 ccusage 的值；reasoning 是否已含在 output 内，看 `crosscheck.mine.method` 的说明，不要按另一个平台的直觉写。
- **家族合计只认 `family_totals`**：claude 的父行与子代理天然来自两种 basis，逐项相加而不是取父行。
- **复核的独立轴是"本地存储 vs ccusage"**：Claude 的求和在适配器与复核之间是同一份实现，不得写成"两套代码互相印证"。
- **`aggregated_missing` / `sum_missing_sessions` / `sum_unavailable_fields` 出现时**，对应的数写成下限或未知，不能当确切值。

## 报告结构

标题用中文,顺序固定:

1. **抬头**:先原样引用 `support.report_note_zh`,再写快照时间(`captured_at`)、平台与判定依据、统计工具与参数、数据来源、范围声明(只含该会话及其子会话)。`coverage_warning` 非空时必须在抬头列出。
2. **会话身份**:ID、标题/slug、agent 或模型与 variant、工作目录、客户端版本、消息数或事件数、(codex)rollout 文件数与压缩分片数。
3. **会话树与子会话结论**:有子会话就列全(kind、agentType、文件字节数、各自 token);没有就写"子会话:0 个"并给核查依据。依据:opencode 递归 `parent_id`、`fork_session_id`/`metadata` 为空、`tool_call_counts` 无派生类调用、正文无外部 `ses_` 引用;codex 全量 rollout 首行里没有任何会话的 `forked_from_id` 指向本会话;claude `subagents/` 目录为空。`has_children=null` 或无适配器时只能写"未验证"。
4. **Token 消耗**:逐会话一行(input / output / cache read / cache creation / total / cost)+ 合计(`family_totals.sum`)。`rollouts_aggregated`、`mixed_basis`、`sum_is_partial`/`sessions_without_numbers` 必须在表下说明 —— 有缺失时合计是**下限**而不是总量。有子会话时补占比分析。
5. **成本**:按 `ccusage.pricing_verdict` 措辞 —— `priced_online` / `priced_offline_cache`(标注缓存可能滞后)/ `priced_online_only`(说明离线会得到 `$0`)/ `unpriced_everywhere`(写"无法计价",点名 `unpriced_models`)/ `offline_missing_escalation_failed` 或 `offline_missing_escalation_empty` 或 `no_ccusage_rows` 或 `pricing_unavailable`(标注待定并给重跑命令)。后四种**不允许**给出任何金额结论。用了 `--no-ccusage` 时 fact pack 根本没有 `ccusage` 键,写"本次未取 ccusage 定价,金额未知"。
6. **独立复核(必须有)**:引用 `crosscheck.agreement`、`max_abs_delta_pct`、`minutes_since_last_activity`(含 `last_activity_basis` 说明时间戳按什么时区解释)、`comparison` 明细。判定档位:`exact`(0)/ `close`(≤1%)/ `snapshot_skew_likely`(≤5% 且会话在读取前 5 分钟内仍在写入)/ `divergent_unexplained`(1%–25% 且不能用错开解释)/ `mismatch`(>25%,**或一侧为 0 另一侧非零** —— 后者百分比无定义,记在 `unbounded_delta_fields`,绝不可当成"没有可比数据"放过)/ `no_numeric_overlap`(两侧没有可比数值,**等于没复核,不能当通过**)/ `not_applicable`(该平台或这次采集结构性无法复核,`verdict` 会写 `unsupported` / `unverified_no_ccusage` / `not_possible`)。偏差能否归因于快照错开**必须用 `minutes_since_last_activity` 判断**;会话早已停止写入时的小偏差是口径差,要列出可疑点并说明尚未定位。不得取较大值当结论。`verdict` 与判定相称:`ok`(exact/close/snapshot_skew_likely)/ `needs_attribution`(偏差已确认但原因未定位,必须列可疑点)/ `no_token_records` / `local_read_failed`(本地一边根本没读到用量,这时**两边都是 0 不算复核通过**)/ `unverifiable` / `not_possible` / `unsupported` / `unverified_no_ccusage` / `failed`。后六种要在报告里原样附原因,并写"未复核"。看到 `clock_skew_suspected` 时 `minutes_since_last_activity` 为负,说明时间戳换算不可信,**不得据此把偏差归因成快照错开**。
7. **ccusage 与本地存储的差额归因**:opencode 落到 compaction 事件并列出每次压缩时间;codex 给出 rollout 文件数与两种口径的对照;claude 给出主文件与 subagents 的分项。这些明细只在落盘的 fact pack 里。
8. **消耗结构**:缓存读取占比、单条平均与峰值(`per_message_peaks`)、分时段表(仅当 `hourly_reconciles` 为 true)。
9. **时间分布**:`elapsed` / `active` / `idle` 分开写,并保留 fact pack 里的 `active_note` 口径原文(opencode 用消息区间并集,JSONL 用相邻事件阈值,两者不可互相比较);有 `events_timestamp_unparsed` 时要说明时间分布可能偏小。
10. **复现命令**:采集命令 + 关键 SQL 或文件路径查询。
11. **口径备注**:reasoning 是否单列、`totalTokens` 含 reasoning、金额来源与可对账性、快照性质、该平台适配器与复核是否验证过。

## 硬性规则

- **不要在正在被分析的会话里分析它自己**：`self_reference_suspected` 为真时，抬头必须写明"本次采集跑在被分析的会话里，数字含测量自身的开销、复核无法对齐"，并建议换会话重跑；不得直接给成本结论。分析当前会话的需求一律先解释污染，再让用户决定是否在非会话环境里重跑（`--allow-live-target` 只用于你确实已经换开跑的情况）。
- **支持范围放最前,不能藏在文末**:`support.tier` 不是 `full` 时,第一节就写清"部分支持/仅聚合数字"及 `support.caveats`;`aggregate_only` 还要说明子会话是否存在无法确认。
- 报告里**只能出现目标会话及其子会话**。兄弟会话即使在库里也不写;要提就在对话里一句话带过。gemini 的 `local_store.candidate_files`(stdout 里是 `candidate_files_basenames`)只是"按短 id 前缀撞到、未确认归属"的候选,`local_store.files` 为空时不得拿它们当本会话的 token 来源。
- **`coverage_warning` / `coverage_warnings` 必须逐条转述**。文件解码失败、消息 JSON 解析失败、zstd 后端缺失都会让总量偏低;不能只报总数。
- **不得只贴 ccusage 数字**:复核结论与其偏差解释是必需章节,否则报告不可信。
- 复核不一致时不能"取较大的那个数"当结论,要先判定是口径差异还是快照错开。
- 成本为 0 时绝不输出 `$0` 或"免费",按 `pricing_verdict` 处理。
- 口径差异必须给出具体归因和数字,不许写"统计口径略有不同"。
- 跨天运行说成 `elapsed_time`,同时给出 `active` 与 `idle`,不得暗示整段都在调用模型。
- 数字取 fact pack 原值并加千分位;不要二次四舍五入后当精确值引用。
- **`null` 与 `0` 不是一回事**:`null` 是"没有这个数值",写"未知/未提供";`0` 才是量。带 `aggregated_missing` 的合计要写成"至少/下限",不能当确切总量。
- 活会话(`last_activity`/`updated` 接近快照时间)要标注数字仍在变化,且复核偏差可能只是错开。
- 交付前核对报告中只出现本树 ID:`grep -oE 'ses_[A-Za-z0-9]{20,34}|[0-9a-f-]{36}' 报告 | sort -u`。**例外**:fork 血缘的父会话 ID 可以作为证据引用,但只能出现在说明"本会话由谁派生"的一句里,不得展开它的数据。
- 有子代理但子代理不单独成行的平台(codex 的 `spawn_agent`、claude 的 sidechain),要说清 token 是否已含在主行里,不能简单写"无子会话"了事。

## 已知坑

- **会话会被清理**:分析过的会话可能几小时内就消失,落盘的 fact pack 是唯一可复现凭证,别删。
- **`.zst` 后端缺失时压缩分片不进统计**:`zstd_backend` 字段是 `null` 且本机有 `.zst` rollout 时,`coverage_warning` 会说这件事 —— 必须转述,否则报的是偏小的总数。
- **报告第 4 节的合计可能是下限**:`family_totals.sum_is_partial` 为真时,先说清哪些会话没数,再给合计。
- **活会话**:慢就加 `--no-crosscheck`,但报告要注明未复核。

## 改动后自测

本包只含成文所需内容;回归测试在源码仓库根的 `tests/`,安装包里不含。改动 `scripts/` 下任何取数逻辑后,在仓库根运行下面这条并让它变绿——70 项测试全部跑在合成 fixture 上,不读取任何真实会话:

```bash
python -m unittest discover -s tests -v
```

守的是三条承重口径不变量(Claude 重复 usage 去重、codex 逐轮 usage 去重且不用文件累计值、opencode 的 compaction 差额与分时段对账),以及所有"不许静默少算"的路径。

## Resources

日常成文只需要下面两份:`SKILL.md`(本文件,怎么做)与 fact pack(数据)。其余是按需加载的深度内容。

- `references/rationale.md` —— **按需读**:每个口径为什么这样定、错了会错多少、由哪个测试守住。仅在需要解释口径来源、或要改动取数逻辑时读。
- `scripts/collect.py` —— 入口:平台探测与判定、ccusage 采集与定价判读、按 rollout 聚合、子会话装配、家族合计、fact pack 落盘与 stdout 精简视图。
- `scripts/store_opencode.py` —— opencode SQLite 适配器(会话树、单次扫描出全部派生指标、工具清单、消息区间)。
- `scripts/store_jsonl.py` —— claude / codex / gemini 适配器(文件定位、fork 血缘、subagents、单文件一次扫描出时间分布/去重用量/标记)。
- `scripts/jsonl_reader.py` —— 透明读取 `.jsonl` 与 `.jsonl.zst`(三级后端回退 + 进程内解压缓存 + 解码失败显式抛错)。
- `scripts/crosscheck.py` —— 独立复核:各平台自己算一份,与 ccusage 逐项比对并给一致性判定。
