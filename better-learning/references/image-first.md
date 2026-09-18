# 宿主子 Agent 隔离式转写

主代理只处理元数据，不看教材图像，不接收转写正文。转写完成后才按章节读取文本。成员使用全新上下文，只写本次尝试的暂存文件；脚本是台账、正式输出与合成文件的唯一写入者。

## 1. 盘点与前置探针

```text
python -X utf8 SKILL/scripts/inventory_materials.py --course COURSE INPUT...
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE probe-start --host codex --model ACTUAL_MODEL
```

探针独立保存在 `_工作区/能力探针/current.json`，无需 prepare。PDF、图片或可能有图形的 Office/电子书先做探针，再进行大量渲染。仅有可靠 TXT/CSV/无图 Markdown 的课程可直接 prepare，不需要视觉模型。若后续路由发现视觉任务，必须在 dispatch 前通过探针。

主代理将探针提示词交给相同宿主和实际模型的新成员，不打开测试图、不读取预期答案。成员实际看随机测试图并保存三行转写 JSON。结束后：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE probe --member REAL_MEMBER --host codex --model ACTUAL_MODEL
```

宿主/模型改变必须重新探测；旧成员需先确认停止并释放租约。只写 ok、声称支持图片或能调用看图工具均不能替代真实挑战。失败立即停止，说明原因并建议更换支持视觉转写的模型，不得从目录、文件名或常识编写课程。用户明确授权 AI 补全时仍须先生成知识内容.md，并标明原件未读。探针通过只证明基础能力，不保证复杂教材识别正确。

## 2. 增量准备与路由

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE prepare --max-concurrent 4 --strict-cost-threshold 12000
```

默认普通视觉页 bounded，每个成员最多 5 页；默认并发 4（即一批同时派发的成员数），可配置 1..8，并按当前宿主实际可用容量下调。每批按目标宿主新建**全新上下文**的成员，批内仍累积历史，不承诺固定 Token 成本。

strict 是单元级异常升级：既往 unresolved、unreadable、机械复杂度高、明确要求精细复核、单页调度权重超过阈值、成员 sidecar 返回 uncertain。成本权重不是计费 Token。元数据中的多图、原生图形整页覆盖、降采样、小字警告等可触发升级；主代理不得看图做判断。普通扫描页不因“扫描件”这个类别整体变 strict。

strict 每次只给一页，机械生成 overview + tiles，成员逐 tile 精读，collect 核对全部 tile ID 和资产哈希。工具故障、启动失败、超时、写入失败保持原 profile 重试，不自动升级。`--force-strict-unit SRC-001/U00042` 可在 prepare 显式指定；`reopen --strict` 可重开已完成页。旧 `--mode strict` 仅保留兼容，不作为正常流程。

默认 `--batch-size 5`（允许降低到 1..5）、`--max-attempts 3`；batch-budget 是异常规模安全阀，默认 100000。`--chapter-boundaries` 仍可提供章节起始单元列表，没有可靠目录不猜。

提取缓存绑定来源哈希、提取/布局版本、渲染器版本、比例、像素上限和文本分块参数。缓存资产哈希一致才复用；未变来源再次 prepare 不重新渲染。活跃成员未收尾时禁止 prepare。

提取器明确声明 deterministic_text 的可靠文本由脚本直接落盘，不调用模型；PDF、图片及嵌入图片走 vision。媒体缺失、未知图文顺序、未渲染图表等标记 blocked，不能将 kind=text 当作豁免依据。

Office chart、SmartArt、组合形状、连接线、矢量几何、浮动图形和原生公式触发完整视觉覆盖单元。缺少渲染时阻塞。可用 Office/兼容工具导出对应完整页 PNG 后注册：

```json
{"SRC-001":{"ppt/slides/slide1.xml":"附件/slide1.png"},"SRC-002":{"word/document.xml":["附件/doc-page1.png","附件/doc-page2.png"]}}
```

将映射保存到课程内，并传 `--office-renders 相对路径.json`。DOCX 必须列全该 part 的页面，列表会拆为逐页视觉单元；脚本校验文件与哈希，实际页面覆盖仍需人工/成员核查。映射不能使用裁掉图表的文字截图。也可导出完整 PDF，保留原件与导出对应关系，重新确认资料范围。脚本不承诺在未安装 Office 渲染器的环境自动完成导出。

## 3. 整批派发与整批收口

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE pump --host codex --model ACTUAL_MODEL
```

调度以**批次**为单位：一次 pump 预约一整批，批内不补位，本批全部成员确认停止并 collect 后才允许下一次 pump。

pump 在调度锁内按优先级预约：strict 升级 → 基础设施故障重试 → 普通 bounded。批大小 = `min(max_concurrent, host_limit, 剩余可派发批数)`，可用 `--max-fill N` 下调；宿主可用容量较小时传 `--host-limit N`。并发上限仍是 `max_concurrent`，默认 4，可配 1..8。

若当前批仍有 reserved/running/produced 的 attempt，pump 直接返回空 tickets 并给出 `batch_open: true` 与本批活动 attempt 列表——这是预期行为，不是故障。Python 不调用模型或宿主 Agent API。

主代理立即对每个 ticket 的 bootstrap 新建可写、fresh-context 成员（Codex 显式 fork_turns="none"），再登记真实 ID：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE mark-running --attempt A-ID --agent-id REAL_ID
```

主代理不读 task.json、不转发完整任务清单，不看图；成员自读任务和 [转写策略](worker-policy.md)。成员按单元落盘 Markdown 与 `.result.json` sidecar，状态与教材原文分离。strict 必须列全 manifest 的 tile ID，不能只读 overview 就完成。正式文件仍只由 collect 提交。

任一成员返回并通过宿主确认停止后：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE collect --attempt A-ID --stopped-agent-id REAL_ID
```

collect 验证本次输出、sidecar、来源及资源哈希，只将 complete 原子提交。uncertain/unreadable 保留暂存证据、升级单页 strict，记录 parent_attempt_id。**collect 不再返回 refill tickets**：失败与未完成单元进入下一批，由下一次 pump 统一预约。

主代理的循环是：pump 一批 → 逐个建成员并 mark-running → 收到完成通知即确认停止并 collect（不启动新成员）→ 本批 `batch.open=false` 后再 pump 下一批 → 直到没有 ready work 和活动 attempt。

参数中的停止声明需要主代理先查询宿主，脚本不会替你确认。已完成 attempt 重复 collect 不重复提交；丢失返回消息时通过 status 查询已有预约并按 task_manifest 恢复启动，不能重复创建。

成员运行期间使用 [宿主事件循环](host-adapter.md#七完成事件与等待) 接收完成通知；Codex 用 wait_agent 等待任一成员，收到终态就 collect。禁止固定长休眠后才检查，禁止按派发顺序逐个等到结束。

`dispatch --source ... --batch ...` 保留为手工诊断接口，不作为正常调度主循环。

## 3.5 看门狗进程（防死锁）

派发首批之前，主代理启动一个**独立于主代理的进程**：

```text
python -X utf8 SKILL/scripts/watchdog.py --course COURSE start --host codex --interval 90
python -X utf8 SKILL/scripts/watchdog.py --course COURSE status
python -X utf8 SKILL/scripts/watchdog.py --course COURSE stop
```

- 每 90 秒（1 分 30 秒，`--interval` 可配）向主代理投递一条提醒：**`检查子agent是否正常工作`**，后附纯元数据（活动成员数、待处理单元数、本批编号、每个 attempt 的已运行秒数与静默秒数、疑似无进展的 attempt）。
- 投递方式见 [宿主适配](host-adapter.md#八看门狗与后台进程)。文件通道（`_工作区/看门狗/提醒.jsonl` 与 `状态.json`）对所有宿主可用；主代理每次被唤醒（完成事件或等待超时返回）先读一次。
- 看门狗**只体检、不处置**：不 `fail-attempt`、不 `collect`、不释放租约。是否失败、是否重派必须由主代理经宿主确认成员停止后决定。
- 自停安全阀：连续 3 个周期无活动成员且无待处理单元，或超过 `--max-runtime`（默认 720 分钟）即自行退出，并在 `状态.json` 写入退出原因。
- 主代理收到提醒后的动作：`status` 核对 → 对长期静默的 attempt 查宿主成员真实状态 → **确认已停止**才 `recover --stopped-agent-id` / `collect`；成员仍在运行就继续等待（等待超时不是失败）。
- 转写收口判定：`pump` 无票 + `active_count=0` + `check` 通过 + `assemble` 完成 → 立即 `stop`。`package` 会拒绝在看门狗仍在运行时执行。

## 4. 失败与恢复

明确宿主未创建成员时释放预约：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE fail-attempt --attempt A-ID --kind SPAWN_FAILURE
```

若启动结果不确定，不得假称启动失败；先查询宿主，找到成员就登记其 ID，再确认停止。运行中的故障必须携带已确认停止的 ID，例如：

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE fail-attempt --attempt A-ID --kind TEMP_TOOL_FAILURE --stopped-agent-id REAL_ID
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE recover --attempt A-ID --stopped-agent-id REAL_ID
```

有已落盘成果优先 recover/collect，避免丢掉成功单元。重派只处理未完成项，不重复处理成功页。MEMBER_UNCERTAIN / UNREADABLE_CONTENT 的重试强制 strict 精细单页；默认每单元最多 3 次，超限 needs_review，停止自动重试。WRITE_FAILURE/INVALID_OUTPUT/临时工具故障可有限重试；ASSET_CHANGED 需重新盘点/prepare。

VISION_UNAVAILABLE 立即使探针失效，pump 不再派发新批次；ASSET_CHANGED 同样阻止新批次，需重新盘点和准备。停止下游课程生成；主代理还须终止其他活动成员，确认停止后保留成果。不能通过无限重试、目录代写或把不可读教学页标为空白来绕过。更换模型并通过新探针后可重新 prepare；已经耗尽次数的单元必须由用户决定修复方式，不隐式重置计数。

非教学/重复项只有经实际复核并保存独立证据才可 resolve。duplicate 必须指向同来源已完成正文单元。原有命令保留：`resolve --source ID --unit ID --status non_teaching|duplicate --reason 理由 --evidence 课程相对路径`，重复项另传 `--duplicate-of UNIT`。

已完成单元需要纠错时，运行 `reopen --source ID --unit ID --reason 具体问题` 后重新派发；旧正式正文保留，但不再被视为当前完成结果。耗尽重试次数后只有用户明确要求继续才加 `--reset-attempts`，不会自行无限重置。

旧 schema 1/2 台账不能作为新 attempt 执行。若仍有旧活跃成员，先确认全部停止，再运行 `migrate-legacy --stopped-member 旧成员名`（每个旧活跃成员重复一次参数），脚本备份并释放旧租约，然后 prepare；不得直接将旧 canonical 冒充新输出。有效旧成果经证据校验可复用。

## 5. 合成、报告与知识库

```text
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE report
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE assemble
python -X utf8 SKILL/scripts/convert_materials.py --course COURSE check
```

JSON 台账始终是实时状态；Markdown 计划/问题报告在 prepare、阶段收尾、失败或显式 report 时更新，不在每次派发和正常收集时重写。不再生成批次分片。assemble 直接按单元顺序生成每个来源独立 Markdown，可靠文本标记 text，视觉转写标记 vision；合成器添加稳定的 `^SRC-ID-U00001` 块标识。

所有来源单元必须完成或有证据明确排除，且没有活跃租约，才能合成。最终检查在单次命令中共享哈希与转写解析缓存；每个未变文件仅完整 hash 一次，每来源转写只解析一次，未取消任何来源、覆盖、结构或链接检查。

转写完成后按章阅读，保存章节知识分片/索引与覆盖台账，再生成完整知识内容.md。规划学习路径、讲义和卡片之前必须通过 `assemble_knowledge.py --course COURSE --check`。全部学习文件验收后才可 package，不能转写结束就打包。详见 [交付结构](delivery-layout.md)。
