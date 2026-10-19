# agent-session-token-report

Qoder skill：给定一个 agent CLI 会话 ID，统计该会话及其子会话的 token 消耗与成本，独立复核 `ccusage` 的数字，并产出 markdown 报告。

完整支持 **opencode / claude / codex / gemini** 四个平台（各有本地存储适配器与交叉复核）。其余 `ccusage` 平台（amp、droid、kimi、qwen、copilot、goose、kilo 等）只能给出 `ccusage` 的聚合数字，无法确认子会话、无法独立复核，报告必须据此声明。

## 仓库布局

```
.
├── skills/agent-session-token-report/   # 纯净 skill 包（整目录拷走即安装）
│   ├── SKILL.md                         # 唯一入口：frontmatter + 操作指令
│   ├── scripts/                         # 采集与复核实现（按需执行，不必读进上下文）
│   └── references/rationale.md          # 口径的设计依据与实测记录（按需加载）
├── tests/                               # 合成 fixture 回归测试（开发资产，不属于 skill 包）
├── README.md
└── LICENSE
```

`skills/<name>/` 里只放 skill 需要的东西，符合 Qoder skill 规范：安装时拷这一个目录即可，测试与仓库文档不会跟着进包。

## 安装

把 `skills/agent-session-token-report/` 整个目录拷到任一处，然后重启会话或执行 `/skills reload`：

- 用户级：`~/.qoder-cn/skills/agent-session-token-report/`
- 项目级：`<项目>/.qoder/skills/agent-session-token-report/`

用 `/skills list` 确认已加载，可用 `/agent-session-token-report <session-id>` 调用。

## 运行前提

| 依赖 | 必需 | 说明 |
| --- | --- | --- |
| Python 3.8+ | 是 | 只用标准库，`sqlite3` 需在编译时启用 |
| `npx` + 网络 | 取 ccusage 时 | 默认调用 `npx -y ccusage@latest`；设 `AGENT_CCUSAGE_PKG=ccusage@x.y.z` 可锁版本 |
| `zstandard` / Node / `zstd` CLI | 三选一 | 读 codex 的 `.jsonl.zst` 分片；都没有时压缩分片不计入统计，脚本会给出覆盖度告警 |

本地数据只读：`~/.local/share/opencode/opencode.db`（默认 `mode=ro`）、`~/.claude/projects`、`~/.codex/sessions`、`~/.gemini`。

## 使用

```bash
cd skills/agent-session-token-report
python scripts/collect.py <session-id> [--platform NAME] [--offline-only] [--compare-pricing] \
                         [--no-crosscheck] [--no-ccusage] [--full-output] [--allow-live-target] \
                         [--db PATH] [--out-dir DIR]
```

输出两份：stdout 是精简事实视图；完整事实包落盘 `agent-report-<session-id>-facts.json`，报告由 agent 依据这两者撰写。

退出码：`0` 成功、`1` 未捕获异常（事实包未落盘）、`2` opencode 存储不可用、`3` 会话不在 `opencode.db`（附最近会话候选）。

## 自测

```bash
python -m unittest discover -s tests -v
```

70 项测试全部跑在合成 fixture 上，**不读取任何真实会话数据**。守住的是三条承重口径（Claude 重复 usage 去重、codex 逐轮 usage 去重且不用文件累计值、opencode 的 compaction 差额与分时段对账）以及所有"不许静默少算"的分支。改动取数逻辑后必须先让它变绿。

`tests/` 自己按"哪个目录同时有 `SKILL.md` 与 `scripts/collect.py`"来定位被测代码，所以它放在仓库根还是在 skill 包里、以及从任意工作目录执行，都能跑（`python -m unittest discover -s tests -t tests`）。

## 注意事项

- **不要在正在被分析的会话里分析它自己**：触发分析的那条提问与本次工具调用会先写进会话存储、再被同一份脚本统计进去，报告就成了「测量这次测量的开销」，复核偏差也必然被错归为快照错开。脚本在目标会话 5 分钟内仍在写入时会置 `self_reference_suspected` 并输出必须转述的告警；确认已从别处运行可用 `--allow-live-target` 关闭。
- 报告只会覆盖目标会话及其子会话；同库里的兄弟会话不写入。
- 成本为 0 不等于免费：以事实包里的 `pricing_verdict` 为准。
- 会话存储会被客户端清理（opencode 数小时内就会删历史会话），事实包是可复现凭证，不要删。
- 版本号与变更说明记录在本文件，不写进 `SKILL.md` 的 frontmatter（规范字段表未包含 `metadata`）。

## 许可

[MIT](LICENSE)。
