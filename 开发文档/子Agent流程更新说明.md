# 子 Agent 流程更新说明

本次按用户《子Agent转写方案-完整计划》迁移转写阶段，保留后续知识整理、路径、讲义和核心卡片流程。

- 执行体为当前宿主可写子 Agent，使用 collaboration.spawn_agent 和 fork_turns=none；不照搬其他宿主的 Task 接口。
- 先机械生成单元和转写拆分计划，再用真实磁盘 ok 探针验证成员写权限。工具列表与成员口头成功不算通过。
- 默认每批 8 个同来源连续单元，最多并发 2 批，可下调批大小；已知章节边界按章切批。
- 成员逐单元识图或读取文本，立即写 transcript-agent.md，只回短状态；主代理不看图、不接收转写全文。
- 转写任务.json 为唯一状态来源，MD 计划是六节可读投影。脚本收集哈希与状态，不由成员修改台账。
- 失败只重派未完成单元；活动批次须先确认旧成员终止，再恢复租约，防止两个成员写同一文件。
- 全部来源 done 或有证据明确排除后，按原位置合成每来源 agent-v1 Markdown，再按源章节整理知识内容.md。

原计划同时给出含 unresolved 的合成示例和严格来源门，本实现采用严格来源门：待核实不阻止同批后续处理，但阻止正式合成与知识汇总。非教学或重复排除必须有理由、证据和实际去向。

隔离单位改为批次，不能再声称每页无状态。批内图像仍会积累，密集页应减少批量；上一批末尾最多 100 字仅在可用时附带。模型阶段不使用外部 CLI、本地 OCR、API key 或联网工具。

操作细节见 [转写流程](better-learning/references/image-first.md)，目录职责见 [文件架构](skill文件架构设计.md)。测试与实际宿主验证结果应以本次运行记录为准，不沿用旧版 29 项测试或 CLI 实测结论。

## 宿主适配：支持 codex / codebuddy / claude

原先宿主特定的实现只支持 Codex。现已把宿主差异收敛为一张表，其余调度逻辑保持共享：

- `scripts/convert_materials.py` 新增 `HOSTS` 表与 `dispatch --host codex|codebuddy|claude`（默认 `codex`，向后兼容）。派发提示词里的「读文本工具 / 看图工具 / 写文件工具」按宿主注入，不再写死 `apply_patch` / `view_image`。
- 新增 [宿主适配](better-learning/references/host-adapter.md)：统一抽象、三宿主映射表、`--host` 选择方法、共同陷阱、新增宿主的两步改法。
- `SKILL.md` 与 `references/image-first.md` 中的宿主特定工具名已移除，改为引用该文档；`转写拆分计划.md` 的批次表增加「宿主」列。
- 全仓检索确认：`collaboration.spawn_agent` / `view_image` / `apply_patch` / `node_repl` / `fork_turns` 只出现在 `convert_materials.py` 的 `HOSTS` 表和 `host-adapter.md` 两个文件中。

| 宿主 | 新建成员 | 写文件 | 看图 | 只读陷阱 |
| --- | --- | --- | --- | --- |
| codex | `collaboration.spawn_agent` + `fork_turns="none"` | `apply_patch` | `view_image` | 内置 `explorer` |
| codebuddy | `Task` + `name` + `mode="acceptEdits"`（团队成员） | `write_to_file` / `replace_in_file` | `read_file` | 内置 `code-explorer` |
| claude | `Agent` + `subagent_type`（`Task` 为旧别名） | `Write` / `Edit` | `Read` | 内置 `Explore` / `Plan` |

实测：`--host codebuddy` 与 `--host claude` 生成的提示词分别注入了 `read_file`/`write_to_file` 与 `Read`/`Write`；非法宿主被 argparse 拒绝。

**只读子代理陷阱在三个宿主都存在**，因此 probe 仍是唯一可靠的准入检查，且必须用目标宿主执行。

## 收尾与交付结构

新增两步收尾，解决「过程文件占 98% 体积」的问题：

1. **转写完成后停止子 Agent**：`assemble` 成功后不再有成员在写文件，主代理按 [宿主适配](better-learning/references/host-adapter.md) 的 `stopped` 方式确认成员结束（codebuddy 发 `shutdown_request` 后 `team_delete`；codex 确认线程结束；claude 用 `TaskStop`）。
2. **`package` 打包**：新增 `convert_materials.py package [--dry-run]`。
   - 删除 `资料转写/_分片/`、`_工作区/派发提示/`、`_工作区/提取内容/`（体积主体）；
   - 把 8 个报告类 Markdown（学习需求、学习路径、资料清单、知识内容、质量报告、待核实问题、学习反馈、转写拆分计划）归入 `课程文档/`；
   - 重算所有学习者可见 Markdown 的相对链接，跳过围栏代码块、行内代码与外部链接。

最终结构：

```text
课程包/
├── 开始学习.md        核心知识点.md
├── 学习文档/
├── 资料转写/          （仅整合后的 agent-v1 完整版）
├── 课程文档/          （报告与说明 8 份）
└── _工作区/           （仅台账与结构检查）
```

实测（参考包 60 页扫描教材）：`_工作区/` **34.0 MB**，全部交付物 **0.6 MB**，过程文件占 **98.3%**；`package` 后包体积约为原来的 1/50。端到端测试覆盖跨目录链接、锚点、外部链接、行内代码与未移动文件，重写结果全部正确。

新增规范 [交付结构](better-learning/references/delivery-layout.md)；`SKILL.md`、`image-first.md`、`quality-checks.md` 已同步。

注意：`package` 是终态动作——之后 `check` 直接返回通过（结构检查结果已存 `_工作区/结构检查.json`），不要再运行 `assemble`；需要增补资料时重新走 `inventory` → `prepare`。
