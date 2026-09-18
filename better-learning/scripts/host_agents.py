"""Host tool adapters; no model calls are made by Python."""
# 宿主适配表：跨宿主只有四件事不同——如何新建"全新且可写"的执行体、用什么工具
# 写文件、用什么工具看图、如何确认旧成员已停止。其余调度逻辑完全共享。
HOSTS = {
    'codex': {
        'spawn': '用 collaboration.spawn_agent 新建成员并显式传 fork_turns="none"（不继承主对话、不派生）',
        'write': 'apply_patch',
        'read_image': 'view_image',
        'read_text': 'tools.mcp__node_repl__js 里的 node:fs/promises（只读分配路径）',
        'stopped': '用宿主接口确认该成员线程已结束，必要时将其终止',
        # 外部进程无法调用宿主的 send_message；看门狗只写提醒文件，主代理被唤醒后读取。
        'nudge': None,
        'background_process': '用 shell 后台方式启动：POSIX `nohup python -X utf8 watchdog.py … &`；Windows `Start-Process pythonw`',
    },
    'codebuddy': {
        'spawn': '用 Task 工具传 name 与 mode="acceptEdits" 建立团队成员（异步、独立上下文）；不要用只读的 code-explorer',
        'write': 'write_to_file 或 replace_in_file',
        'read_image': 'read_file（可直接读 PNG）',
        'read_text': 'read_file',
        'stopped': '以宿主实际完成/停止回执确认；仅需主动停止时用 send_message 发 shutdown_request 并等待停止确认。脚本 status 与文件写齐不证明成员已停止；禁止长 sleep 后轮询',
        # send_message 是主代理工具，后台进程用不了；提醒经文件投递，主代理每次唤醒即读。
        'nudge': None,
        'background_process': 'execute_command：Start-Process -FilePath pythonw -ArgumentList "…watchdog.py …" -WindowStyle Hidden',
    },
    'claude': {
        'spawn': '用 Agent 工具传 subagent_type（须为具备 Write/Edit 的类型，如 general-purpose 或自定义 agent）；Task 是旧别名。不要用只读的 Explore / Plan',
        'write': 'Write 或 Edit',
        'read_image': 'Read（可直接读图片）',
        'read_text': 'Read',
        'stopped': '用 TaskStop 停止，或确认该 subagent 已返回',
        'nudge': None,
        'background_process': 'Bash：`nohup python -X utf8 watchdog.py … >/dev/null 2>&1 &`',
    },
}
DEFAULT_HOST = 'codex'
