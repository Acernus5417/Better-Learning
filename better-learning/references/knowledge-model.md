# 数据约定与断点恢复

所有 JSON/JSONL 使用 UTF-8，`schema_version` 为 1。结构路径相对课程根目录并使用 `/`，禁止 `..` 逃出目录。材料原路径可为绝对路径。所有 ID 分配后保持稳定，显示顺序单独记录。

## 提取层（脚本维护）

`_工作区/资料索引.json`：`sources` 列表，每项含 `id`、`path`、`name`、`sha256`、`size`、`format`。重新盘点需传入完整本次范围；同一路径保留 ID，变更内容更新哈希，不删除旧提取缓存。`资料清单.md` 是可读视图。

提取版本放在 `_工作区/提取内容/SRC-ID/完整哈希/`。`定位映射.json` 包含 `source_id`、`source_hash`、`total_units`、`units`。每单元含 `id`（如 `U00001`）、`ordinal`、`locator`、`status`、`chunks`、`images`、`visual_review_required`、`warnings`。status 为 pending、extracted、needs_review 或 blocked。`chunks/images` 相对课程根目录。blocked 文件仍有一个阻塞单元，不会消失。

提取状态与覆盖状态独立：extracted 只表示工具提取成功。需要额外工具补读时，在覆盖台账中记录方法和证据路径；不能篡改源哈希来掩盖提取失败。

所有 PDF/图片标记 content_mode: visual_first。提取层只保存机械结果，转写另有台账，不能把 extracted 当作 done。

## 转写台账与合成（脚本维护）

`_工作区/转写任务.json` 是转写状态的唯一来源；`转写拆分计划.md` 是可重建投影，包含来源清单、单元清单、批次分配、划分规则、状态图例、更新规则六节。不手工同时维护两个状态表。

台账记录 batch_size、max_concurrent、来源 ID/路径/完整哈希/格式、单元 id/kind/locator/assets/batch/member/output/status 及字符数和哈希。单元通过 source_id + unit_id 唯一定位，各来源可从 U00001 开始。每批同来源、连续编号，默认 8 个，最多 2 个并发；章界已知时不跨章。

单元输出为版本提取目录中的 `U00001/transcript-agent.md`。成员只写分配的正文，collect 维护 `meta.json`、资源与结果哈希、负责人和台账。pending 未开始，dispatched 有活动租约，done 已机械校验，failed 失败，unresolved 含待核实内容。non_teaching/duplicate 只能经 resolve 记录理由与课程内证据，duplicate 还必须有实际去向。

探针记录证明成员实际在课程目录写入过 ok；不是工具列表声明。没有有效探针不能派发。dispatched 批次不能重复派发，先确认原成员已经结束，才可 recover 收集落盘成果并释放租约。重试使用新成员，仅分配未完成单元。成员未完成、空文件、源或资源哈希变化、结果被改写，均不得保留未经复核的完成标记。

源文件变化时旧版本成果失效，不复用旧 OCR/CLI 转写；重新盘点和 prepare，保留旧文件作历史，不冒充当前来源。继续任务以台账、源哈希和实际文件共同验证，不以旧 Markdown 计划或成员消息恢复完成状态。

全部来源的所有单元 done 或有证据明确排除后，assemble 生成 `资料转写/_分片/` 和逐来源 `*-agent-v1.md`，按原顺序写 BL-PAGE vision 块、正文哈希和总文件哈希，维护转写索引。failed/unresolved 不阻止同批剩余页继续，但阻止正式合成和知识汇总；不通过部分合并选项绕过。

完成的转写允许按章进入语义阅读，不强制每页再抄区域笔记。覆盖台账 evidence 说明实际阅读与核查方法；visual_reviewed 仅在实际看图时为 true，不把主代理机械收集说成看图。疑难页可交成员读局部图再修订单元，重新 collect；提供了人工区域清单时须校验清单与笔记。详见 [子 Agent 转写](image-first.md)。

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
- 知识分片正文包含稳定锚点 `<a id="K-000001"></a>`。合并后该锚点仍有效。
- `prerequisites` 引用知识 ID。相互依赖概念合理合并；真的存在循环时重构教学拆分，不能任意排序后忽略。
- `lesson_ids` 至少一个。相应讲义含 `<a id="K-000001"></a>` 供反向定位。补学知识同样登记。
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

只合并标记 complete 的章节；默认遇到未完成章节拒绝写完整汇编。`--allow-partial` 仅用于显式的中间检查，输出标记“未完成”，不能据此交付完整课程。知识正文完整保留，不再次摘要；只将普通 Markdown 相对链接调整到总文件所在目录，生成章节目录。分片使用普通 Markdown 文件/图片链接，跨知识链接使用稳定 ID 锚点；不使用 HTML 相对 href/src 或 Wikilink。分片是维护来源，`知识内容.md` 是可重建的完整视图；修改知识先改分片和索引，再重建总文件。

## 进度与反馈

`_工作区/生成进度.json`：

```json
{"schema_version":1,"confirmations":{"goal":false,"materials":false,"baseline":false},"stage":"intake","completed_chapters":[],"completed_lessons":[],"blockers":[],"next_action":"确认学习目标和资料范围"}
```

首次盘点创建进度，后续不覆盖用户回答。资料范围或哈希变化时脚本将 materials 设为 false，stage 设为 needs_update；模型重新确认并记录受影响章节。更新 JSON 时先写临时文件再替换，完成标记后置。

`学习反馈.md` 保存实际作答、复测、错因和历史证据。卡片只存当前快照。不得重建文件时覆盖用户填写内容，也不能把生成进度当作学习进度。

长文默认按章保存。只有实际发生容量不足或中断时，才在既有 `生成进度.json` 的 `next_action` 中记录未完成章/节和续写位置；不另建写作分段状态系统。
