# 宿主适配

转写阶段需要"新建一个**全新上下文、且能写文件**的执行体"来按批识图。这项能力在不同宿主里有不同的工具名和参数，**其余调度逻辑完全一致**。本文件是唯一记录宿主差异的地方；主流程与 `SKILL.md` 只引用"宿主可写子 Agent"这一抽象，不写死任何宿主的工具名。

## 一、统一抽象（主流程只依赖这五条）

| 能力 | 要求 |
| --- | --- |
| 新建执行体 | 全新上下文：不继承主对话历史、看不到主代理已读过的文件 |
| 可写 | 能把正文落盘到分配的输出路径 |
| 能看图 | 能把指定 PNG 真正交给视觉模型（不是靠文件名推断） |
| 返回 | 只回约定的短状态字段，不回正文与工具清单 |
| 可确认停止 | 用于 `recover` 前确认旧成员确实已结束 |

调度脚本 `convert_materials.py` 内置 `HOSTS` 表，`dispatch --host <名称>` 会把对应宿主的写工具名、看图工具名注入成员提示词。

## 二、三宿主映射

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE dispatch \
  --source SRC-001 --batch B01 --host codebuddy
```

| | **codex** | **codebuddy** | **claude** |
| --- | --- | --- | --- |
| 新建成员 | `collaboration.spawn_agent` + `fork_turns="none"` | `Task` 工具 + `name` + `mode="acceptEdits"`（团队成员模式） | `Agent` 工具 + `subagent_type`（`Task` 是其旧别名） |
| 写文件 | `apply_patch` | `write_to_file` / `replace_in_file` | `Write` / `Edit` |
| 看图 | `view_image` | `read_file`（可直接读 PNG） | `Read`（可直接读图片） |
| 读文本 | `tools.mcp__node_repl__js` 的 `node:fs/promises` | `read_file` | `Read` |
| 确认停止 | 宿主接口确认线程结束或终止 | `send_message` 发 `shutdown_request` | `TaskStop` |
| 只读陷阱 | 内置 `explorer`；`sandbox_mode="read-only"` | 内置 `code-explorer`（工具集仅 search/read/lsp） | 内置 `Explore` / `Plan`（明确拒绝 Write/Edit） |
| 并发控制 | `[agents] max_concurrent_threads_per_session` | 本流程限制 `--max-concurrent 2` | `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`（默认 20） |
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

不确定时：先跑 `probe`（见下节），用能成功写出 `ok` 的那个宿主。

## 四、探针仍然必须做

`prepare` 之后、`dispatch` 之前，必须用**目标宿主**建一个最小成员写出 `_工作区/probe.txt` 内容为 `ok`，然后：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE probe --member REAL_MEMBER
```

脚本会**实际读取磁盘内容**判定，工具清单或口头成功一律不算。这一步正是用来提前发现"选成了只读子代理"——三个宿主都有只读探索型子代理，名字不同但都不能写。

## 五、三个共同陷阱

1. **只读子代理陷阱**（最常踩）：`explorer`（codex）/ `code-explorer`（codebuddy）/ `Explore`·`Plan`（claude）都能看图，却都写不了文件。若用它做转写，成员会"看完了但存不下"，隔离反而白做。
2. **成员不继承主对话**（claude 与 codebuddy 明确如此，codex 用 `fork_turns="none"` 保证）：提示词必须自带**全部绝对路径**，不能写"上面提到的那张图"。
3. **成员会多说**：实测中即便要求"只回三行"，成员仍可能附上工具清单。因此返回格式必须是**固定字段名 + 禁止附言**的硬约束。

## 六、新增一个宿主

两处改动即可：

1. 在 `scripts/convert_materials.py` 的 `HOSTS` 表加一项：`spawn` / `write` / `read_image` / `read_text` / `stopped`。
2. 在本文件第二、三节补一行映射。

不需要改 `prepare` / `collect` / `assemble` / `check`——它们完全宿主无关。
