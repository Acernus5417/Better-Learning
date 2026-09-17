# 讲义并发与核心卡片收口

入口条件：全部资料转写完成、知识分片完整且知识条目 verified，知识内容.md 已通过 assemble_knowledge.py --check；学习路径已落盘，章节索引的 lessons 列出稳定 ID、title、order、path、status 和 path_excerpt（或路径中带该 lesson ID 的独立段落）。路径片段应包含本章目标、先修、顺序、过关标准，不是整份路径副本。

## 章级任务

```text
python -X utf8 SKILL/scripts/lesson_tasks.py --course COURSE prepare --max-concurrent 4
python -X utf8 SKILL/scripts/lesson_tasks.py --course COURSE pump --host HOST --model ACTUAL_MODEL
```

每个 ticket 是一章、一个 fresh context。只转发 bootstrap；成员自读 task.json、分配的知识片段/路径片段/需求、[章节规范](chapter-writing.md)、模板和 [讲义成员策略](lesson-worker-policy.md)。不预读整本课程或全部 references，不看教材图片，不生成最终卡片。任务同时给出 canonical link 字面量、预留 EX ID 和本事务的 graph policy / registry revision；模型只能消费 manifest 里给出的身份与路径。

v2 manifest（`schema_version: 2`）的成员权限是**只读正文、只写片段**：`agent_can_write_system_regions: false`；`sections[].anchor_literal` 与 `system_slots` 只说明脚本会插入什么，成员不得自己写 `BL-*`、`ID ^ID` 或 `^K/^EX`。任务按矩阵只分发可用关系：分配知识的 canonical K 链接、前置知识 canonical K 链接、本章学习步骤链接，以及已登记题源练习的 `exercise_sources`；不提供本讲义自链接、来源文件整篇链接或“相关”类链接。

任务包含本章直接依赖的前置知识片段与路径步骤片段，不携带整套课程。EX ID 预留数量默认为 `max(24, 6×本章知识数+12)`，可在章节索引设置 exercise_count；它是可用 ID 池，不是练习数量要求；未使用的预留 ID 保持 reserved，不构成有效关系目标。

宿主启动成功后登记；任一成员完成并确认停止后收集：

```text
python -X utf8 SKILL/scripts/lesson_tasks.py --course COURSE mark-running --attempt L-ATTEMPT --agent-id REAL_ID
python -X utf8 SKILL/scripts/lesson_tasks.py --course COURSE collect --attempt L-ATTEMPT --stopped-agent-id REAL_ID
```

collect 返回 refill，主代理立即启动新 ticket，不等其他章结束。默认并发 4，支持 1..8；pump 的 --host-limit 可按实际宿主可用槽位降低。失败恢复命令与转写一致：fail-attempt --kind SPAWN_FAILURE|TEMP_TOOL_FAILURE|WRITE_FAILURE|TIMED_OUT|INVALID_OUTPUT；运行中失败必须先确认停止并传 --stopped-agent-id。recover 等同于恢复收集，旧成员未停止不得释放租约。

等待章节完成遵循 [宿主事件循环](host-adapter.md#七完成事件与等待)：等待任一成员事件，终态确认后立即 collect / refill；不固定休眠、不按章序等待。章节顺序决定教学路径，不决定结果收集顺序。

pump 返回中断时先运行 status，从 active 的 member/task_manifest 找回预约。必须先核实宿主是否已经启动该成员；确认未启动才按已有 manifest 启动，已启动则补记真实 ID，不能盲目再次 spawn。

新任务采用 parts-v1 片段协议：成员按分配的 sections 写 `_工作区/讲义尝试/<attempt>/parts/S001/001.md` 等片段，逐片更新各节 section.json，最后写 `<lesson>.result.json`。回执只含节 ID、partial/complete、连续成功片段文件名；不要求按固定字符数切分。一章仍由一个成员负责，正式交付仍是一个 Markdown。

collect 分四步，且没有任何一步可以被“看起来差不多”绕过：

1. **原始校验**：确认宿主停止、核对输入与受保护片段哈希、逐节检查回执/文件/围栏与公式边界/允许链接；片段中出现 `BL-*`、`ID ^ID` 锚点或 `^K/^EX` 直接报 `RESERVED_SYSTEM_MARKUP`。
2. **装配**：按 section 清单合并片段，插入 `BL-L`、`BL-TEACH`、`BL-EX` 边界与该实体的入口锚点；练习区间由 result 的 `exercises[].heading` 定位，标题缺失或不唯一即 `INVALID_OUTPUT`。
3. **关系渲染与阶段性校验**：在候选文件集（虚拟完整文件集）上推导关系、渲染 `BL-REL`，检查结构、链接权限、投影相等、KP/SRC 约束与 DEP 无环。
4. **提交**：写入事务日志后按文件原子替换；任一步失败按备份回滚，attempt 记为 collected，已完成片段保留供续写。

重复 collect 不重复合并或追加；幂等由“相同输入得到逐字节相同输出”保证。字节、分隔符和链接检查不证明数学或教学解释正确，仍须按章节规范复核内容。

成员提前结束、工具写入失败、最后 result 缺失或 status 为 partial 时，collect 保存有效片段的哈希与进度，整章不标记完成；失败节与缺失节保留待补。自动 refill 创建新 attempt，复制经验证的片段，记录 parent_attempt_id，并生成续写摘要（提纲、已用块 ID、未完节末尾）。新成员只补缺失片段，不覆盖复制的旧片段；续写仍占普通章级槽位，不嵌套派代理、不重置重试计数。

使用相同 collect/recover 命令恢复；即使没有最终 result，只要已确认旧成员停止也可收集回执。parts-v1 的 fail-attempt（超时/工具失败）先尝试收集成果再补位。普通等待超时不等于任务失败。连续失败最多 max-attempts，超限 needs_review；文件存在不代表完整，未获回执确认的孤立片段不自动复用。输入或已保存片段哈希改变时拒绝旧片段续写，须先复核处理。未完成整章时仍禁止生成核心卡片。

旧任务缺少 write_mode 时保留单文件收集分支；规范/快照变化会使旧任务失效，不直接修改正在执行的 task.json。先确认旧成员停止并保留成果，再 prepare 生成新契约；不能把旧半篇正文冒充有完整分段证据的新成果。

知识正文、分片、路径、需求、分配附件、graph policy 或注册表结构版本变化会使旧讲义失效；status/check 可定位，prepare 重建受影响任务。快照只比较**教学内容哈希**：`semantic()` 会排除脚本管理的关系区与身份锚点，因此渲染关系、锚点移位或重命名不会触发无限重写；教学正文真的改了才会失效。失败最多 max-attempts（默认 3），超限 needs_review，不自动无限重置。

## 主代理统一生成卡片

全部讲义通过 `lesson_tasks.py check` 后，主代理按 lesson order 每次读取一章与对应知识索引，及时将候选写入 `_工作区/核心候选.jsonl`。不要一次读取全部章节，不宣称能清除已读历史。

```json
{"concept_key":"经主代理归一的概念键","knowledge_ids":["K-01","K-02"],"reason":"为何核心，引用实际目标或覆盖证据",
 "exercise_ids":["EX-L01-003"],"exercise_reason":"该题直接考查本卡覆盖的知识"}
```

同义归一是主代理的语义工作；脚本只按已确认的 concept_key 合并候选，不能自动证明两个术语同义。同一知识不得进入不同概念组。多个章节对应相同概念可合成一卡，保留所有知识去向。`exercise_ids` 必须指向 `_工作区/练习索引.jsonl` 中状态为 used、属于本卡任一知识所在讲义、且 `assesses` 与本卡知识有交集的练习；否则 `RELATION_PROVENANCE_MISSING`。

```text
python -X utf8 SKILL/scripts/core_cards.py --course COURSE prepare
```

脚本确认全部讲义有效，统一分配稳定 KP-章-序-01 ID，保留已有 concept_key 的 ID；输出 `_工作区/核心卡片计划.json`（含 knowledge_ids、exercise_ids 与 canonical 字面量），回写知识索引的 core/card 元数据，重建知识内容.md 并重新渲染关系区。主代理根据计划、[卡片规范](core-cards.md) 和模板统一写 `核心知识点.md`，不派卡片子 Agent。脚本不会再往知识分片里塞关系块——关系区是派生视图，不是知识正文。

```text
python -X utf8 SKILL/scripts/core_cards.py --course COURSE finalize
python -X utf8 SKILL/scripts/core_cards.py --course COURSE check
```

finalize 验证全局 ID、KP 边界与入口锚点、KP 出边只连 canonical K 与 active EX、关系投影键级一致，并记录全部讲义教学内容哈希、知识索引/知识库哈希、练习映射哈希、graph policy 与卡片哈希。之后任一输入或卡片变化都使快照失效。最终 validate_package 同时检查讲义任务、输入新鲜度和卡片快照。

运行时 attempt/staging 不作为知识图谱节点；最终 package 清理讲义尝试目录。不要在学习包仍需修订时提前 package。
