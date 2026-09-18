# 宿主适配

转写阶段需要"新建一个**全新上下文、且能写文件**的执行体"来按批识图。这项能力在不同宿主里有不同的工具名和参数，**其余调度逻辑完全一致**。本文件是唯一记录宿主差异的地方；主流程与 `SKILL.md` 只引用"宿主可写子 Agent"这一抽象，不写死任何宿主的工具名。

## 一、统一抽象（主流程只依赖这五条）

| 能力 | 要求 |
| --- | --- |
| 新建执行体 | 全新上下文：不继承主对话历史、看不到主代理已读过的文件 |
| 可写 | 能把正文落盘到分配的输出路径 |
| 能看图 | 能把指定 PNG 真正交给视觉模型（不是靠文件名推断） |
| 返回 | 只回约定的短状态字段，不回正文与工具清单；写 attempt staging，不直接写正式结果 |
| 可确认停止 | 用于 `recover` 前确认旧成员确实已结束 |

适配模块 `host_agents.py` 提供 `HOSTS` 表，`dispatch --host <名称>` 把对应宿主工具写入 task.json，主代理仅转发 bootstrap，成员自读任务。启动成功须 mark-running 登记真实 Agent ID；确认停止后 collect。

## 二、三宿主映射

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE dispatch \
  --source SRC-001 --batch B01 --host codebuddy --model ACTUAL_MODEL
```

| | **codex** | **codebuddy** | **claude** |
| --- | --- | --- | --- |
| 新建成员 | `collaboration.spawn_agent` + `fork_turns="none"` | `Task` 工具 + `name` + `mode="acceptEdits"`（团队成员模式） | `Agent` 工具 + `subagent_type`（`Task` 是其旧别名） |
| 写文件 | `apply_patch` | `write_to_file` / `replace_in_file` | `Write` / `Edit` |
| 看图 | `view_image` | `read_file`（可直接读 PNG） | `Read`（可直接读图片） |
| 读文本 | `tools.mcp__node_repl__js` 的 `node:fs/promises` | `read_file` | `Read` |
| 确认停止 | 宿主接口确认线程结束或终止 | 宿主完成/停止回执；需主动停止时 `send_message` 发 `shutdown_request` 并等确认 | `TaskStop` 或确认子代理已返回 |
| 只读陷阱 | 内置 `explorer`；`sandbox_mode="read-only"` | 内置 `code-explorer`（工具集仅 search/read/lsp） | 内置 `Explore` / `Plan`（明确拒绝 Write/Edit） |
| 并发控制 | `[agents] max_concurrent_threads_per_session` | 本流程默认4；--host-limit 按宿主可用量覆盖 | `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`（默认 20） |
| 防嵌套 | 成员不派生 | 成员无 `task` 工具 | 从 `tools` 省略 `Agent`，或设 `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1` |

### 2.1 codex

- 内置三种 agent 类型：`default` / `worker` / `explorer`（后者偏只读）；自定义 agent 放在 `~/.codex/agents/` 或 `.codex/agents/`，TOML 必填 `name` / `description` / `developer_instructions`。
- 关键：**Codex 不会自动派生子代理**，必须在提示里明确要求并行；本流程通过 `spawn_agent` 显式派生，符合该约束。
- 权限由 `sandbox_mode` 决定：要能写文件，成员不能是 `read-only`。
- 长篇提示会刷屏的中间产出留在成员线程里，只回摘要——这正是本流程要的形态。

### 2.2 codebuddy

- `Task` 有两种形态：
  - **同步子代理**（`subagent_name` + `prompt`）：本流程**不可用**——内置 `code-explorer` 只有 `search_file` / `search_content` / `read_file` / `read_lints` / `lsp`，**没有写工具**。
  - **团队成员**（`name` + `mode`）：异步、独立上下文、工具集完整（含 `write_to_file` / `replace_in_file` / `execute_command`），**这是本流程要用的形态**。
- `mode` 取 `acceptEdits`（自动接受文件编辑）即可；`bypassPermissions` 更强但不必要。
- 团队管理：`team_delete` 清理；成员可通过 `send_message` 主动回推——因此提示词里的"只回 5 行、不要工具清单"是硬要求。
- 调度接收团队成员的短完成通知与宿主终态回执，这属于元数据，不违反"主代理不读正文"。不要执行 `execute_command` 的 sleep 120/170 秒后再查 status。Python `status` 仅汇总主代理维护的台账，不查询 CodeBuddy 宿主；成员写齐文件也不能证明它已停止。成员正常完成时使用真实完成回执；只有需主动停止或当前宿主要求 shutdown 才发 `shutdown_request`，并等停止确认后 collect，不能刚发请求就声明已停止。

### 2.3 claude

- 调用形式：`Agent(subagent_type=..., description=..., prompt=...)`；v2.1.63 起 `Task` 更名为 `Agent`，`Task(...)` 仍作别名可用。
- 内置类型中 **`general-purpose` 拥有全部 subagent 可用工具**（可写）；`Explore` / `Plan` **只读**，明确拒绝 `Write` / `Edit`。
- 自定义 subagent 放 `.claude/agents/*.md` 或 `~/.claude/agents/*.md`，YAML frontmatter 支持 `tools`（白名单）/ `disallowedTools`（黑名单）/ `permissionMode: acceptEdits` / `maxTurns`。
  为本流程建议的最小定义：

  ```markdown
  ---
  name: vision-transcriber
  description: 逐单元读取分配图片或文本，忠实转写为 Markdown 并落盘，只回短状态。
  tools: Read, Write, Edit
  permissionMode: acceptEdits
  maxTurns: 80
  ---

  只处理被分配的单元。用 Read 看图或读文本，用 Write/Edit 写分配的 output。
  不做摘要、教学改写或解题；不可辨认处写 [待核实]。只回规定的短状态行。
  ```

- 也可以用 `claude --agents '<json>'` 在会话级临时定义，不落盘。
- 防嵌套：从 `tools` 里省略 `Agent`（或用 `disallowedTools: Agent`）。
- subagent **以新鲜隔离上下文启动，看不到主对话历史** —— 与需求完全一致；`isolation: "worktree"` 对本流程不需要（输出路径是绝对路径）。

## 三、如何选择宿主的 `--host`

先看当前会话可用的工具名，再对照：

| 你看到的 | 用 |
| --- | --- |
| `collaboration.spawn_agent` | `--host codex` |
| `Task` 且能传 `name` 与 `mode` | `--host codebuddy` |
| `Agent` 或 `Task` 且能传 `subagent_type` | `--host claude` |

不确定时：先跑 `probe`（见下节），用实际通过视觉转写与写入双能力探针的宿主和模型。

## 四、探针必须验证实际视觉转写

先用 probe-start --host HOST --model ACTUAL_MODEL 生成随机测试图，再用**相同模型与宿主**派新成员，严格限制其只能看图并保存响应；最后 probe --member REAL_MEMBER --host HOST --model ACTUAL_MODEL 校验。具体命令与响应要求见 [图像流程](image-first.md)。

只写 ok、可调用看图工具、声称支持图片，都不能替代实际转写通过。未通过立即停止任务并建议用户换模型；模型改变重新探测。不允许成员读取随机图答案、验证器状态或以目录猜写替代识图。

## 五、三个共同陷阱

1. **只读子代理陷阱**（最常踩）：`explorer`（codex）/ `code-explorer`（codebuddy）/ `Explore`·`Plan`（claude）都能看图，却都写不了文件。若用它做转写，成员会"看完了但存不下"，隔离反而白做。
2. **成员不继承主对话**（claude 与 codebuddy 明确如此，codex 用 `fork_turns="none"` 保证）：bootstrap 必须提供**任务与策略绝对路径**，任务内包含分配资源路径，不能写"上面提到的那张图"。
3. **成员会多说**：实测中即便要求"只回三行"，成员仍可能附上工具清单。因此返回格式必须是**固定字段名 + 禁止附言**的硬约束。

## 六、新增一个宿主

两处改动即可：

1. 在 `scripts/host_agents.py` 的 `HOSTS` 表加一项：`spawn` / `write` / `read_image` / `read_text` / `stopped`。
2. 在本文件第二、三节补一行映射。

不需要改 `prepare` / `collect` / `assemble` / `check`——它们完全宿主无关。

正常调度由 pump 返回 spawn tickets；主代理调用宿主工具并 mark-running。任一完成事件先确认停止再 collect；**不启动 refill 成员**，本批全部收集完才 pump 下一批。转写阶段共用 host_agents / agent_pool 契约；并发上限是业务配置，不代表宿主一定有同等空闲容量。

## 七、完成事件与等待

此规则只用于转写（章级讲义已改为主代理写作，没有子 Agent）。Python 只在调用 pump / collect 时执行，不是后台守护进程；批内不再有 refill，不会在主代理休眠时自行触发。

Codex 的正常宿主循环：

1. pump → 对返回的 tickets 启动成员 → 逐个 mark-running。
2. 先处理已到达的完成通知；通知中的 attempt 与真实 Agent ID 必须匹配台账。成员文字说 DONE 仍需宿主终态确认，不能凭文件存在推断已停止。
3. 每确认一个成员完成就 collect，然后处理下一条通知；本批内不启动新成员。整批 collect 完毕（status 的 `batch.open=false`）后再 pump 下一批。
4. 没有未处理的完成通知、仍有活动成员且无必要本地工作时，调用 `collaboration.wait_agent({"timeout_ms": 30000})` 等待任一成员消息。这是事件等待，消息到达会提前返回，30000 是超时上限，不是必须睡满的间隔。单次等待不超过 60000 毫秒。
5. 返回后先区分完成、普通进度、用户输入和等待超时；必要时用 `collaboration.list_agents` 查询当前任务成员的宿主状态。完成即回到第 3 步；进度/等待超时不释放租约、不算失败、不消耗重试次数。不要在事件已到达后额外 sleep。
6. 队列与活动成员均为空才结束。若队列有任务但没有活动成员，先 pump 或处理明确阻塞，不进入空等。

不要用 `clock.sleep`、`time.sleep`、`Start-Sleep` 或长时间 shell 循环观察输出文件来代替成员通知。不能把通用"等待更久减少轮询"的建议理解为固定休眠；真正能被任一成员完成事件唤醒的等待不会强制睡满超时值。

其他宿主优先使用本会话实际提供的异步完成通知/任一成员等待能力，不猜工具名或参数；查看当前工具 schema。只有确实没有完成事件接口时，才退化为不超过 10 秒的短轮询，每次检查全部当前活动成员并逐个收集（不补位）。没有任何状态变化时不反复运行 pump、读取全台账或正文。

两类 status 不可混淆：`convert_materials.py status` / `lesson_tasks.py status` 查询课程台账，宿主成员状态接口查询实际执行状态。前者不监听子代理，也不会因为 staging 文件已写齐自动把 running 改成完成；因此仅循环"睡眠 → 脚本 status"不能实现整批收口。接收宿主短完成消息、查询成员状态都属于允许的元数据操作。

排查延迟时分别核对"宿主确认完成 → collect 开始 → 本批最后一个成员 collect 结束 → 下一批 pump"的时间。UI 显示等待持续 170 秒不能单独证明延迟；若调用是固定休眠，或完成事件已到达却未收集，才是此循环执行不正确。本流程不再有"补位"环节，批内成员各自独立推进，实际耗时还包括宿主消息投递、工具排队和输出校验。

## 八、看门狗与后台进程

全流程只有转写一个多 Agent 环节，看门狗只覆盖该阶段。启动方式按宿主查表（`host_agents.py` 的 `background_process`）：

| 宿主 | 启动后台进程 |
| --- | --- |
| codex | `nohup python -X utf8 SKILL/scripts/watchdog.py --course COURSE start --host codex &`（Windows 用 `Start-Process pythonw`） |
| codebuddy | `execute_command`：`Start-Process -FilePath pythonw -ArgumentList "…watchdog.py --course COURSE start --host codebuddy" -WindowStyle Hidden` |
| claude | Bash：`nohup python -X utf8 SKILL/scripts/watchdog.py --course COURSE start --host claude >/dev/null 2>&1 &` |

提醒投递：三个宿主的 `nudge` 均为 `null`——外部进程无法调用宿主的 `send_message`/`collaboration` 接口，因此**统一走文件通道**：`_工作区/看门狗/提醒.jsonl` 追加一条、`状态.json` 保存最新一条。主代理每次被唤醒（完成事件到达或 `wait_agent` 超时返回）先读 `状态.json`，这就是"看门狗主动提醒"的落点；不得因为宿主没有推送接口就假装已收到实时消息。

禁止事项（写死在此，任何宿主都一样）：看门狗不得判失败、不得 collect、不得释放租约、不得改台账；主代理不得用它替代宿主完成事件；不得用长 sleep 代替等待。若宿主确实无法启动后台进程，降级为主代理每 90 秒读一次 `status` 自检，并在质量报告里如实说明没有独立进程。
