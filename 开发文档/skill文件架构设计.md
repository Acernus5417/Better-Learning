# better-learning 文件架构：宿主子 Agent 版本

技能源目录是 better-learning/，安装目录是 C:/Users/win11/.codex/skills/better-learning/。运行依赖为现有文档 Python 与宿主可写子 Agent；模型阶段不使用外部 CLI、本地 OCR 或 API key。

```text
better-learning/
├── SKILL.md                       # 流程入口与按需规范
├── agents/openai.yaml             # 显示名称和默认提示
├── references/
│   ├── intake.md                  # 目标、资料完整性、基础与前置
│   ├── material-processing.md     # 格式路由与图文定位
│   ├── image-first.md             # 探针、派发、收集、恢复和合成
│   ├── knowledge-model.md         # 台账、索引、覆盖和哈希约定
│   ├── learning-path.md           # 先修依赖和学习路径
│   ├── chapter-writing.md         # 独立学习章节规范
│   ├── core-cards.md              # 核心知识卡片规范
│   └── quality-checks.md          # 完整性和教学质量门
├── assets/templates/              # 知识章节、路径、讲义和卡片模板
└── scripts/
    ├── _common.py                 # 路径、哈希、原子写入
    ├── inventory_materials.py     # 盘点与稳定来源 ID
    ├── extract_materials.py       # 机械渲染、文本与图片拆分
    ├── convert_materials.py       # 准备、台账、租约、校验、来源合成
    ├── transcribe_materials.py    # BL-PAGE 兼容格式和完整性检查
    ├── prepare_visual.py          # 局部放大，供成员复核
    ├── assemble_knowledge.py      # 全部转写完成后合并知识分片
    └── validate_package.py        # 来源、覆盖、依赖与链接校验
```

主文件按阶段引用规范，规范引用模板与脚本。convert_materials.py 不调用模型；主代理通过宿主 collaboration.spawn_agent 派发可写成员，fork_turns=none，不继承主对话。每批默认 8 个同来源连续单元，最多 2 个并发，可因密集内容下调。

```text
课程目录/
├── 学习需求.md、资料清单.md
├── 转写拆分计划.md                # JSON 台账的六节可读投影
├── 资料转写/
│   ├── _分片/                    # 批次机械合成
│   └── SRC-ID-名称-哈希-agent-v1.md # 每个输入文件独立转写
├── 知识内容.md                    # 后续学习写作的核心
├── 学习路径.md、核心知识点.md
├── 学习文档/
├── 开始学习.md、学习反馈.md、质量报告.md
├── 待核实问题.md、附件/
└── _工作区/
    ├── 资料索引.json、转写任务.json、转写索引.json
    ├── 生成进度.json
    ├── 提取内容/来源ID/版本哈希/
    │   ├── 定位映射.json
    │   └── U00001/
    │       ├── page.png 或 text-001.md 等资源
    │       ├── transcript-agent.md # 成员逐单元写入
    │       └── meta.json           # 脚本计算状态、字符数、哈希
    ├── 章节知识/、章节索引.json
    └── 知识索引.jsonl、覆盖台账.jsonl、结构检查.json
```

数据流：需求确认 → 输入盘点 → prepare → 可写成员探针 → 启动看门狗 → pump 一批 → 宿主成员 → 整批 collect（批内不补位）→ 循环至无就绪工作 → 停止看门狗 → 全部来源 assemble/check → 按源章节整理分片 → 知识内容.md → 学习路径 → 讲义（主代理按章生成并 commit）→ 卡片（主代理统一写）→ Obsidian 链接复验 → 验收。

成员只写独立单元文件，主代理只读短状态，脚本负责共享台账和合成文件。中断时先确认旧成员终止再 recover，避免并发重复写。来源变化按新哈希重新处理。任何 failed/unresolved 均阻止正式合成，但不阻止同批其他单元完成；明确排除需理由与证据。

这是按批隔离，批内仍积累上下文；只在可用时携带上一批尾文最多 100 字。知识整理按章读取，不模拟清空对话，也不设置 150 字符提交机制。图像转写落盘与语义复核是不同质量层。
