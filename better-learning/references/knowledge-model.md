# 数据约定与断点恢复

所有 JSON/JSONL 使用 UTF-8，`schema_version` 除转写台账为 3 外均沿用 1。结构路径相对课程根目录并使用 `/`，禁止 `..` 逃出目录。材料原路径可为绝对路径。所有 ID 分配后保持稳定，显示顺序单独记录。

v2 课程的 `_工作区/课程配置.json` 至少含：`link_mode: obsidian`、`link_schema_version: 2`、`graph_policy_version: bl-typed-links-v2`、`course_vault_prefix`、`layout: working|packaged`。缺 `link_schema_version` 的旧包按 v1 验收，不静默升级。

## 提取层（脚本维护）

`_工作区/资料索引.json`：`sources` 列表，每项含 `id`、`path`、`name`、`sha256`、`size`、`format`。重新盘点需传入完整本次范围；同一路径保留 ID，变更内容更新哈希，不删除旧提取缓存。`资料清单.md` 是可读视图。

提取版本放在 `_工作区/提取内容/SRC-ID/完整哈希/`。`定位映射.json` 包含 `source_id`、`source_hash`、`total_units`、`units`。每单元含 `id`（如 `U00001`）、`ordinal`、`locator`、`status`、`chunks`、`images`、`visual_review_required`、`warnings`。status 为 pending、extracted、needs_review 或 blocked。`chunks/images` 相对课程根目录。blocked 文件仍有一个阻塞单元，不会消失。

提取状态与覆盖状态独立：extracted 只表示工具提取成功。需要额外工具补读时，在覆盖台账中记录方法和证据路径；不能篡改源哈希来掩盖提取失败。

所有 PDF/图片标记 content_mode: visual_first。提取层只保存机械结果，转写另有台账，不能把 extracted 当作 done。

## 转写台账与合成（脚本维护）

`_工作区/转写任务.json` 是转写状态的唯一来源；`转写拆分计划.md` 是可重建投影，包含来源清单、单元清单、批次分配、划分规则、状态图例、更新规则六节。不手工同时维护两个状态表。

台账 schema 3 记录 policy、max_attempts、max_concurrent、current_batch_no、sources、attempts 和 probe_ref。调度按批次：一次 pump 预约一整批（`current_batch_no` 递增，attempt 记录 `dispatch_batch_no`），批内不补位，本批全部收集完才开下一批。单元通过 source_id + unit_id 唯一定位，包含 processing_route、assets、asset_hashes、output、meta、status、current_attempt、attempt_count。普通页默认 bounded 最多 5 页；异常页按单元升级 strict 并生成 overview/tiles。默认并发 4，可配 1..8，按宿主容量下调。unit 保存 next_profile、strict_reasons、complexity_flags、last_attempt_id、last_result；attempt 保存 profile、parent_attempt_id、result_contract。

正式输出仍是提取目录的 `U00001/transcript-agent.md`，但只能由脚本提交。成员只写 `_工作区/转写尝试/<attempt_id>/<source_id>/<unit_id>.md`。attempt 保存 source_id、batch_id、unit_ids、logical_member、host_agent_id、host/model、state、task_manifest、staging_dir、时间、失败类别和提交日志。reserved 不代表宿主已启动；mark-running 绑定实际 ID 后才是 running。collect 须声明已确认停止的匹配宿主 ID，检查本次 staging 后才原子提交；重复收集终态 attempt 不重复追加。collect 不再回填空闲槽，失败与未完成单元留给下一批 pump。

状态包括 pending、reserved、running、done、failed、unresolved、needs_review。默认最多 3 次尝试；疑难重试单独派发，超限转用户复核。non_teaching/duplicate 只能经 resolve 保存理由与独立证据，duplicate 必须有实际完成去向。成员不能写台账、meta 或正式输出。

视觉探针独立存储于 `_工作区/能力探针/current.json`，在重型 prepare 前可执行。随机图的实际转写与写入证据、宿主和模型绑定均须通过；只写 ok 不算通过。可靠纯文本全部直写时无需视觉探针；视觉能力失效立即阻塞后续任务，不得目录代写。

源文件变化时旧版本成果失效；重新盘点和 prepare，旧提取不冒充当前来源。缓存依据来源哈希、完整提取配置和资产哈希验证，成功输出另有 meta 证据。继续任务以 JSON 台账、源哈希及实际文件共同验证；Markdown 计划是按需报告，不必实时更新。

所有来源单元完成或有证据排除后，assemble 直接从单元正式输出生成逐来源 `*-agent-v1.md`；不生成批次分片。BL-PAGE 中可靠文本标记 text，视觉单元标记 vision，仍保留正文与总文件哈希。failed/unresolved/needs_review 或活动租约均阻止合成与知识汇总。

成员/人工发现语义错误时登记实际问题与依据，通过 `reopen` 登记原因并重开对应单元，重新派发和收集；不能只编辑最终来源汇总文件。提供人工区域清单时仍核查清单与笔记。完整操作见 [子 Agent 转写](image-first.md)。

转写状态与知识覆盖独立：文件存在和哈希正确不证明公式、图表和概念正确。所有转写完成后才逐章复核知识，仍要保留未确定的知识表述。

## 知识分片及教学索引（模型维护）

`_工作区/章节索引.json` 示例：

```json
{
  "schema_version": 1,
  "chapters": [
    {"id": "KC-001", "title": "源章节标题", "order": 1,
     "path": "_工作区/章节知识/KC-001.md", "status": "complete"}
  ],
  "lessons": [
    {"id": "LC-001", "title": "教学章节标题", "order": 1,
     "path": "学习文档/01-章节标题.md", "status": "complete"}
  ]
}
```

`chapters` 是知识分片，`lessons` 是重新编排的教学章节。两者通过知识条目关联，不要求一一对应。章节还可增加 `source_ranges`，记录源文件 ID 及起止单元。

`_工作区/知识索引.jsonl` 每行一个知识条目：

```json
{"id":"K-000001","title":"知识名称","chapter_id":"KC-001","anchor":"K-000001","kind":"material","status":"verified","source_refs":[{"source_id":"SRC-001","unit_id":"U00001"}],"prerequisites":[],"lesson_ids":["LC-001"],"core":true,"card_id":"KP-01-01-01","card_anchor":"KP-01-01-01","core_reason":"本课程的关键前置概念"}
```

- `kind`：material 或 ai_supplement。材料知识必须有源定位；补充内容说明补充范围和依据，禁止伪装成原材料。
- `status`：verified 或 unresolved。verified 表示已复核表述及来源，不代表学习者已掌握。
- v2 知识分片用 `<!-- BL-K:BEGIN K-000001 --> … <!-- BL-K:END K-000001 -->` 包围每个知识条目，标题后第一行写入口锚点 `K-000001 ^K-000001`。**不要**在段落末尾写 `^K-000001`；边界与锚点是程序判定范围与落点的唯一依据，缺失即 `SECTION_BOUNDARY_INVALID`，不会退化按标题猜测。
- 分片与 `知识内容.md` 是两处独立落点：分片是维护来源，`知识内容.md` 由 `assemble_knowledge.py` 重建（追加 `BL-CHAPTER` 章节容器与 `IDX-KNOWLEDGE` 目录容器，并渲染关系区）。v2 的 K canonical location 只有 `知识内容.md` 一处；讲义中的同名块是 teaching occurrence，属于另一个已登记落点。
- `prerequisites` 引用知识 ID。相互依赖概念合理合并；真的存在循环时重构教学拆分，不能任意排序后忽略。
- `lesson_ids` 至少一个。相应讲义含 `^K-000001` 供反向定位。补学知识同样登记。
- `core` 明确布尔值。核心候选记录 `core_reason`，例如对目标关键、是关键前置、资料反复强调或题型有覆盖证据。非核心知识仍需教学去向。
- 核心条目填卡片 ID 和锚点；多条紧密关联知识可共用一卡，卡片必须列出所覆盖知识 ID。卡片 ID 初次按章-节-序分配，重排学习路径不改 ID。

## 覆盖台账（模型维护）

`_工作区/覆盖台账.jsonl` 每个源单元一条：

```json
{"source_id":"SRC-001","source_hash":"源文件完整哈希","unit_id":"U00001","status":"covered","knowledge_ids":["K-000001"],"reason":"","visual_reviewed":true,"evidence":"已读全部文本块，并核对页面中的公式和图例"}
```

status：covered、duplicate、non_teaching、unreadable、unresolved。covered/duplicate 必须关联知识 ID；duplicate/non_teaching 必须写 reason；unreadable/unresolved 必须写问题及影响。视觉标记为 true 必须源于实际看图。

若提取单元为 blocked/pending，但通过其他工具已经完成读取，可增加 `manual_read_evidence`（课程内证据文件路径）和 `evidence`，例如转换版本映射及逐页阅读记录。只有实际覆盖完成才能写该字段，不是绕过未读内容的开关。

## 完整合并

```text
python SKILL/scripts/assemble_knowledge.py --course COURSE
```

只合并标记 complete 的章节；默认遇到未完成章节拒绝写完整汇编。`--allow-partial` 仅用于显式的中间检查，输出标记“未完成”，不能据此交付完整课程。知识正文完整保留，不再次摘要。v2 合并会校验分片中的 `BL-K` 集合与知识索引一致（缺失或多出都拒绝），再追加 `BL-CHAPTER` 与 `IDX-KNOWLEDGE` 容器，最后重新渲染关系区；分片是维护来源，`知识内容.md` 是可重建的完整视图；修改知识先改分片和索引，再重建总文件。附件用 Obsidian embed，知识位置使用稳定块 ID 而非 heading。脚本运行后会同步刷新注册表与 `_工作区/链接映射.json`。

v2 派生与位置数据都由脚本维护，模型不手改：

- `_工作区/路径索引.json`：`{path_id, title, steps:[{id, order, title, lessons}]}`；缺省时按 lesson order 推导，一旦显式登记就以文件为准。
- `_工作区/练习索引.jsonl`：每行 `{id, owner_lesson_id, status: reserved|used, assesses:[K-ID], source_refs:[{source_id, unit_id}], title}`。只有 `used` 且被卡片计划引用才成为关系目标。
- `_工作区/管理文档索引.json`：`{documents:[{id, path, title, entry, links:[{target_id, subtype, reason}]}]}`，管理文档的出边只来自这里登记的记录。
- `_工作区/开始入口.json`：`{entries:[{target_id, subtype}], management:[M-ID], index_entry: bool}`，控制 START 的 NAV.entry / NAV.management。
- `_工作区/实体注册表.json`：实体身份、canonical/teaching/unit 位置、状态、墓碑（合并去向）与结构版本；交付后仍保留，供 packaged 复验使用，不当作临时产物删除。
- `_工作区/链接映射.json`：schema 2，含 `registry_revision`、`locations[]` 与旧适配键（knowledge/lessons/sources/cards）。
- `_工作区/事务记录.jsonl`：多文件提交日志与备份索引；`_工作区/事务备份/` 保存提交前内容，中断可回滚。

## 进度与反馈

`_工作区/生成进度.json`：

```json
{"schema_version":1,"confirmations":{"goal":false,"materials":false,"baseline":false},"stage":"intake","completed_chapters":[],"completed_lessons":[],"blockers":[],"next_action":"确认学习目标和资料范围"}
```

首次盘点创建进度，后续不覆盖用户回答。资料范围或哈希变化时脚本将 materials 设为 false，stage 设为 needs_update；模型重新确认并记录受影响章节。更新 JSON 时先写临时文件再替换，完成标记后置。

`学习反馈.md` 保存实际作答、复测、错因和历史证据。卡片只存当前快照。不得重建文件时覆盖用户填写内容，也不能把生成进度当作学习进度。

长文默认按章保存。只有实际发生容量不足或中断时，才在既有 `生成进度.json` 的 `next_action` 中记录未完成章/节和续写位置；不另建写作分段状态系统。

## 用户明确要求 AI 补全的例外路径

视觉能力失败时默认停止并建议换模型，不擅自降级为目录扩写。只有用户直接要求 AI 补全/根据目录重新编写时，才保存其原话和范围到课程内请求记录。此路径的产物必须如实标注未读原件及覆盖限制，不能声明原资料已全部处理。

先建立章节知识分片与知识索引（本例外路径的条目全部 kind=ai_supplement），再运行：

```text
python -X utf8 SKILL/scripts/assemble_knowledge.py --course COURSE --ai-request _工作区/用户AI补全请求.md
python -X utf8 SKILL/scripts/assemble_knowledge.py --course COURSE --check
```

--ai-request 只是记录已经获得的用户明确指令，不能自行创建请求文件冒充授权。它不解锁原件转写、不清除待核实状态，也不让原资料完整性验收变为通过。脚本保存请求版本用于写作前核查。必须以生成的知识内容.md为核心，再规划路径、写讲义与卡片；没有用户授权、知识分片未完成或知识库未生成时不得继续。正常资料路径不使用该选项。

章级任务、核心卡片快照和输入失效见 [讲义流水线](lesson-pipeline.md)。身份、路径映射、双链与迁移见 [Obsidian 规范](obsidian-links.md)。
