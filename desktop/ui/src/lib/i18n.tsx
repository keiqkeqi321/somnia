import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

export const LANGUAGE_STORAGE_KEY = "somnia.desktop.language";

export const SUPPORTED_LOCALES = ["zh-CN", "en-US"] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
export type TranslationKey = keyof typeof translations["en-US"];

const translations = {
  "en-US": {
    /* ── App title ── */
    "app.title": "Somnia Desktop",

    /* ── Title bar ── */
    "titlebar.minimize": "Minimize",
    "titlebar.maximize": "Maximize",
    "titlebar.restore": "Restore",
    "titlebar.close": "Close",

    /* ── Settings ── */
    "settings.title": "Settings",
    "settings.back": "Back to app",
    "settings.section.archived": "Archived threads",
    "settings.language.label": "Language",
    "settings.language.en-US": "English",
    "settings.language.zh-CN": "中文",

    /* ── Settings / Configuration panel ── */
    "settings.config.user": "User",
    "settings.config.project": "Project",
    "settings.config.reload": "Reload",
    "settings.config.provider": "Provider",
    "settings.config.mcp": "MCP",
    "settings.config.hooks": "Hooks",
    "settings.config.systemPrompt": "System Prompt",
    "settings.config.providerTitle": "Provider Profiles",
    "settings.config.mcpTitle": "MCP Servers",
    "settings.config.hooksTitle": "Hooks",
    "settings.config.systemPromptTitle": "System Prompt",
    "settings.config.configLabel": "config",
    "settings.config.openFile": "Open file",
    "settings.config.openFolder": "Open folder",
    "settings.config.editorHint": "Edit the TOML snippet for the current category. Saving writes to the selected scope's config file.",
    "settings.config.saving": "Saving",
    "settings.config.save": "Save",
    "settings.config.runtimeMcpServers": "Runtime MCP Servers",
    "settings.config.debugSuccess": "Successfully fetched {count} tool(s) from {name}.",
    "settings.config.mcpEnabled": "Enabled {name}; fetched {count} tool(s) for chat.",
    "settings.config.mcpDisabled": "Disabled {name}; removed its MCP tools from chat.",
    "settings.config.runtimeMcpHint": "Click Debug to inspect registered tools from the running sidecar.",
    "settings.config.noMcpServers": "No MCP servers are active in this sidecar.",
    "settings.config.tools": "tools",
    "settings.config.on": "On",
    "settings.config.off": "Off",
    "settings.config.debug": "Debug",
    "settings.config.fetching": "Fetching",
    "settings.config.toolsButton": "Tools",
    "settings.config.hide": "Hide",
    "settings.config.disableMcp": "Disable this MCP server for chat",
    "settings.config.enableMcp": "Enable this MCP server for chat",
    "settings.config.target": "Target",
    "settings.config.unconfigured": "(unconfigured)",
    "settings.config.noToolsRegistered": "No tools registered for this server.",
    "settings.config.noDescription": "(no description)",
    "settings.config.visionModel": "Shared vision model",
    "settings.config.visionProvider": "Vision provider",
    "settings.config.activeProvider": "Active provider",
    "settings.config.noVisionModel": "No shared vision model",
    "settings.config.saveVisionModel": "Save vision model",
    "settings.config.skills": "Skills",
    "settings.config.noSkills": "No skills found for this scope.",
    "settings.config.loading": "Loading configuration...",
    "settings.config.unavailable": "Configuration unavailable.",
    "settings.providerProfiles.title": "Provider Profiles",
    "settings.providerProfiles.tomlPreview": "TOML preview / fallback",
    "settings.providerProfiles.add": "Add provider",
    "settings.providerProfiles.empty": "No provider profiles configured.",
    "settings.providerProfiles.noDefault": "no default model",
    "settings.providerProfiles.modelCount": "{count} models",
    "settings.providerProfiles.name": "Name",
    "settings.providerProfiles.type": "Type",
    "settings.providerProfiles.baseUrl": "Base URL",
    "settings.providerProfiles.apiKey": "API key",
    "settings.providerProfiles.models": "Models",
    "settings.providerProfiles.defaultModel": "Default model",
    "settings.providerProfiles.reasoning": "Reasoning level",
    "settings.providerProfiles.organization": "Organization",
    "settings.providerProfiles.timeoutSeconds": "Timeout seconds",
    "settings.providerProfiles.contextTokens": "Context tokens",
    "settings.providerProfiles.maxTokens": "Max tokens",
    "settings.providerProfiles.supportsReasoning": "Supports reasoning",
    "settings.providerProfiles.adaptiveReasoning": "Adaptive reasoning",
    "settings.providerProfiles.auto": "Auto",
    "settings.providerProfiles.yes": "Yes",
    "settings.providerProfiles.no": "No",
    "settings.providerProfiles.test": "Test",
    "settings.providerProfiles.testing": "Testing...",
    "settings.providerProfiles.defaultProvider": "Default provider",
    "settings.providerProfiles.setDefault": "Set default",
    "settings.providerProfiles.remove": "Remove",

    /* ── Settings / Archived ── */
    "settings.archived.selectAll": "Select all",
    "settings.archived.restoreSelected": "Restore selected",
    "settings.archived.deleteSelected": "Permanently delete selected",
    "settings.archived.empty": "No archived sessions.",
    "settings.archived.session": "session",
    "settings.archived.sessions": "sessions",
    "settings.archived.emptySession": "(empty session)",
    "settings.archived.restore": "Restore",
    "settings.archived.delete": "Delete",

    /* ── Sidebar ── */
    "sidebar.projects": "Projects",
    "sidebar.total": "{count} total",
    "sidebar.newProject": "New Project",
    "sidebar.projectLimitReached": "You can add up to {count} projects. Remove one project before adding another.",
    "sidebar.noProjects": "No projects yet.",
    "sidebar.noProjectsHint": "Choose a folder to add a project and run its own Somnia sidecar.",
    "sidebar.connecting": "Connecting...",
    "sidebar.startingSidecar": "Starting sidecar...",
    "sidebar.projectOptions": "Project options",
    "sidebar.newSession": "New",
    "sidebar.removeProject": "Remove",
    "sidebar.archiveSession": "Archive",
    "sidebar.sessionOptions": "Session options",
    "sidebar.waitingDecision": "Waiting for your decision",
    "sidebar.agentResponding": "Agent is responding",
    "sidebar.resizePanel": "Resize projects panel",

    /* ── Conversation panel ── */
    "conversation.newConversation": "New conversation",
    "conversation.workspaceUnavailable": "Workspace unavailable",
    "conversation.hideDetails": "Hide details",
    "conversation.showDetails": "Show details",
    "conversation.startSession": "Start a session",
    "conversation.startSessionHint": "Connect to a sidecar, choose a session, then send a prompt. Streaming output lands here.",
    "conversation.waitingAssistant": "Waiting for assistant response",
    "conversation.agentResponding": "Agent is responding",

    /* ── Composer ── */
    "composer.placeholder": "Ask Somnia to inspect, plan, or implement against the current workspace.",
    "composer.attachImage": "Attach image",
    "composer.removeImage": "Remove {name}",
    "composer.provider": "Provider",
    "composer.model": "Model",
    "composer.visionModel": "Vision fallback",
    "composer.visionModelId": "Vision model ID",
    "composer.visionModelNone": "None",
    "composer.apply": "Apply",
    "composer.reasoningLevel": "Reasoning level",
    "composer.send": "Send",
    "composer.queueForSession": "Queue for this session",
    "composer.projectTurnLimit": "This project already has two sessions running",
    "composer.interrupt": "Interrupt",

    /* ── Context panel ── */
    "context.kicker": "Context",
    "context.sessionDetails": "Session Details",
    "context.collapse": "Collapse",
    "context.session": "Session",
    "context.updated": "Updated",
    "context.messages": "Messages",
    "context.currentMode": "Current mode",
    "context.preview": "Preview",
    "context.noSession": "No session selected.",
    "context.noSessionHint": "Choose a session from the sidebar to inspect its details.",
    "context.resizePanel": "Resize session details panel",
    "context.compactContext": "Compact context",
    "context.semanticJanitor": "Semantic janitor",

    /* ── CTX popover ── */
    "ctx.windowDetails": "Context window details",
    "ctx.used": "Used",
    "ctx.window": "Window",
    "ctx.ratio": "Ratio",
    "ctx.usageUnavailable": "Context usage unavailable",

    /* ── TaskGraph ── */
    "taskGraph.title": "TaskGraph",
    "taskGraph.empty": "No persistent tasks in this session.",
    "taskGraph.summary": "{completed}/{total} completed · {inProgress} active · {pending} pending",
    "taskGraph.expand": "Expand",
    "taskGraph.hint": "Use task_create to build a task graph for longer work.",
    "taskGraph.close": "Close",
    "taskGraph.untitledTask": "Untitled task",
    "taskGraph.owner": "Owner",
    "taskGraph.preferred": "Preferred",
    "taskGraph.blockedBy": "Blocked by",
    "taskGraph.blocks": "Blocks",
    "taskGraph.unassigned": "unassigned",
    "taskGraph.none": "none",
    "taskGraph.inProgress": "in progress",
    "taskGraph.panelLabel": "TaskGraph panel",

    /* ── Execution activity ── */
    "activity.executionActivity": "Execution Activity",
    "activity.subagents": "Subagents",
    "activity.agentTeam": "Agent Team",

    /* ── Interaction decision card ── */
    "decision.authorizationRequest": "Authorization request",
    "decision.modeSwitchRequest": "Mode switch request",
    "decision.allowOnce": "Allow once",
    "decision.allowWorkspace": "Allow workspace",
    "decision.deny": "Deny",
    "decision.switchNow": "Switch now",
    "decision.stayHere": "Stay here",
    "decision.approveTool": "Approve {toolName}",
    "decision.switchToMode": "Switch to {targetMode}",
    "decision.noReason": "No reason provided.",
    "decision.allowOnceReason": "Allowed once from desktop UI.",
    "decision.allowWorkspaceReason": "Allowed in this workspace from desktop UI.",
    "decision.denyReason": "Denied from desktop UI.",

    /* ── Prompt queue ── */
    "queue.queuedPrompts": "Queued prompts",
    "queue.nextLoop": "Next loop",
    "queue.injectNextLoop": "Inject next loop",
    "queue.waitingNextLoop": "Waiting for the next agent loop",
    "queue.injectOnNextLoop": "Inject on next agent loop",

    /* ── Mermaid ── */
    "mermaid.title": "Mermaid",
    "mermaid.renderFailed": "Render failed",
    "mermaid.graph": "Graph",
    "mermaid.code": "Code",
    "mermaid.fullscreen": "Fullscreen",
    "mermaid.rendering": "Rendering diagram...",
    "mermaid.diagram": "Mermaid diagram",
    "mermaid.reset": "Reset",
    "mermaid.close": "Close",
    "mermaid.openFullscreen": "Open Mermaid diagram fullscreen",
    "mermaid.closeFullscreen": "Close Mermaid diagram fullscreen",

    /* ── Tool call card ── */
    "toolCall.input": "Input",
    "toolCall.output": "Output",
    "toolCall.images": "Images",
    "toolCall.changes": "Changes",
    "toolCall.changesFor": "Changes for {path}",
    "toolCall.fileUpdated": "File updated.",
    "toolCall.delete": "Delete",
    "toolCall.create": "Create",
    "toolCall.write": "Write",
    "toolCall.update": "Update",
    "toolCall.running": "(running)",
    "toolCall.noOutput": "(no output)",

    /* ── Todo status bar ── */
    "todo.title": "Todo",
    "todo.progress": "{completed}/{total} done",
    "todo.inProgress": "In progress",
    "todo.next": "Next",
    "todo.hide": "Hide",
    "todo.showAll": "Show all",
    "todo.untitled": "(untitled todo)",
    "todo.progressLabel": "Todo progress",

    /* ── Path picker ── */
    "pathPicker.folder": "folder",
    "pathPicker.file": "file",

    /* ── Common ── */
    "common.workspace": "workspace",
    "common.executionModeUnavailable": "Execution mode unavailable",
    "common.lookAtThisImage": "Look at this image.",
    "common.oneImageAttached": "[1 image attached]",
    "common.imagesAttached": "[{count} images attached]",
    "common.emptySession": "(empty session)",
    "common.unknownPath": "(unknown path)",
    "common.working": "working",
    "common.active": "active",
    "common.prefers": "prefers {name}",
    "common.unknown": "unknown",
    "common.connectFirst": "Connect to a sidecar first.",

    /* ── Command specs ── */
    "cmd.init": "Generate AGENTS.md project instructions",
    "cmd.scan": "Scan the repo or a subdirectory",
    "cmd.symbols": "Find symbols and inspect matching source locations",
    "cmd.image": "Send a local image to the active multimodal model",
    "cmd.pasteImage": "Read an image from the system clipboard",
    "cmd.model": "Choose the active provider and model",
    "cmd.vision": "Choose the image understanding model",
    "cmd.reasoning": "Set the active provider reasoning level",
    "cmd.providers": "Add or edit shared provider profiles",
    "cmd.hooks": "Browse hooks by event and toggle them on or off",
    "cmd.undo": "Undo the most recent file change set",
    "cmd.checkpoint": "Save a named checkpoint of the current session state",
    "cmd.rollback": "Roll back to a previous checkpoint",
    "cmd.compact": "Compact the current session context",
    "cmd.janitor": "Run semantic janitor on the current payload",
    "cmd.skills": "Choose a skill to apply to the next prompt",
    "cmd.tasks": "Show persistent tasks",
    "cmd.team": "Show teammate roster and states",
    "cmd.mcp": "Browse configured MCP servers and tools",
    "cmd.bg": "Show background jobs",
    "cmd.help": "Show available REPL commands",
    "cmd.exit": "Exit chat mode",

    /* ── Execution mode options ── */
    "mode.shortcuts.title": "? for shortcuts",
    "mode.shortcuts.description": "Read-only shortcuts and lightweight inspection.",
    "mode.plan.title": "⏸ plan mode on",
    "mode.plan.description": "Read-only planning before edits.",
    "mode.acceptEdits.title": "⏵⏵ accept edits on",
    "mode.acceptEdits.description": "Allow file edits and task updates.",
    "mode.yolo.title": "! Yolo",
    "mode.yolo.description": "Full autonomy for this workspace.",

    /* ── Banner messages ── */
    "banner.initial": "Point the UI at a running sidecar and start a session.",
    "banner.connecting": "Connecting to sidecar...",
    "banner.connectingTo": "Connecting to {path}...",
    "banner.connectedTo": "Connected to {url}",
    "banner.connectedBundled": "Connected to bundled sidecar at {url}",
    "banner.activeProject": "Active project: {path}",
    "banner.disconnected": "Sidecar event stream disconnected.",
    "banner.streamFailed": "Sidecar event stream failed.",
  },

  "zh-CN": {
    /* ── App title ── */
    "app.title": "Somnia Desktop",

    /* ── Title bar ── */
    "titlebar.minimize": "最小化",
    "titlebar.maximize": "最大化",
    "titlebar.restore": "还原",
    "titlebar.close": "关闭",

    /* ── Settings ── */
    "settings.title": "设置",
    "settings.back": "返回应用",
    "settings.section.archived": "已归档线程",
    "settings.language.label": "语言",
    "settings.language.en-US": "English",
    "settings.language.zh-CN": "中文",

    /* ── Settings / Configuration panel ── */
    "settings.config.user": "用户",
    "settings.config.project": "项目",
    "settings.config.reload": "重新加载",
    "settings.config.provider": "Provider",
    "settings.config.mcp": "MCP",
    "settings.config.hooks": "Hooks",
    "settings.config.systemPrompt": "系统提示词",
    "settings.config.providerTitle": "Provider 配置",
    "settings.config.mcpTitle": "MCP 服务器",
    "settings.config.hooksTitle": "Hooks",
    "settings.config.systemPromptTitle": "系统提示词",
    "settings.config.configLabel": "配置",
    "settings.config.openFile": "打开文件",
    "settings.config.openFolder": "打开文件夹",
    "settings.config.editorHint": "编辑当前类别的 TOML 片段。保存会写入所选 scope 的配置文件。",
    "settings.config.saving": "保存中",
    "settings.config.save": "保存",
    "settings.config.runtimeMcpServers": "运行中 MCP 服务器",
    "settings.config.debugSuccess": "成功获取 {name} 的 {count} 个工具。",
    "settings.config.mcpEnabled": "已启用 {name}；获取了 {count} 个工具用于聊天。",
    "settings.config.mcpDisabled": "已禁用 {name}；移除了其 MCP 工具。",
    "settings.config.runtimeMcpHint": "点击 Debug 查看运行中 sidecar 注册的工具。",
    "settings.config.noMcpServers": "当前 sidecar 没有激活的 MCP 服务器。",
    "settings.config.tools": "工具",
    "settings.config.on": "开",
    "settings.config.off": "关",
    "settings.config.debug": "Debug",
    "settings.config.fetching": "获取中",
    "settings.config.toolsButton": "工具",
    "settings.config.hide": "隐藏",
    "settings.config.disableMcp": "在聊天中禁用此 MCP 服务器",
    "settings.config.enableMcp": "在聊天中启用此 MCP 服务器",
    "settings.config.target": "目标",
    "settings.config.unconfigured": "(未配置)",
    "settings.config.noToolsRegistered": "此服务器未注册任何工具。",
    "settings.config.noDescription": "(无描述)",
    "settings.config.visionModel": "公共视觉模型",
    "settings.config.visionProvider": "视觉 provider",
    "settings.config.activeProvider": "当前 provider",
    "settings.config.noVisionModel": "不使用公共视觉模型",
    "settings.config.saveVisionModel": "保存视觉模型",
    "settings.config.skills": "Skills",
    "settings.config.noSkills": "此 scope 未发现 skills。",
    "settings.config.loading": "正在加载配置…",
    "settings.config.unavailable": "配置不可用。",
    "settings.providerProfiles.title": "Provider Profiles",
    "settings.providerProfiles.tomlPreview": "TOML 预览 / 备选",
    "settings.providerProfiles.add": "添加 provider",
    "settings.providerProfiles.empty": "尚未配置 provider profile。",
    "settings.providerProfiles.noDefault": "未设置默认模型",
    "settings.providerProfiles.modelCount": "{count} 个模型",
    "settings.providerProfiles.name": "名称",
    "settings.providerProfiles.type": "类型",
    "settings.providerProfiles.baseUrl": "Base URL",
    "settings.providerProfiles.apiKey": "API Key",
    "settings.providerProfiles.models": "模型",
    "settings.providerProfiles.defaultModel": "默认模型",
    "settings.providerProfiles.reasoning": "推理级别",
    "settings.providerProfiles.organization": "Organization",
    "settings.providerProfiles.timeoutSeconds": "超时时间",
    "settings.providerProfiles.contextTokens": "Context Tokens",
    "settings.providerProfiles.maxTokens": "Max Tokens",
    "settings.providerProfiles.supportsReasoning": "支持推理",
    "settings.providerProfiles.adaptiveReasoning": "自适应推理",
    "settings.providerProfiles.auto": "自动",
    "settings.providerProfiles.yes": "是",
    "settings.providerProfiles.no": "否",
    "settings.providerProfiles.test": "调试",
    "settings.providerProfiles.testing": "调试中…",
    "settings.providerProfiles.defaultProvider": "默认 provider",
    "settings.providerProfiles.setDefault": "设为默认",
    "settings.providerProfiles.remove": "删除",

    /* ── Settings / Archived ── */
    "settings.archived.selectAll": "全选",
    "settings.archived.restoreSelected": "恢复所选",
    "settings.archived.deleteSelected": "彻底删除所选",
    "settings.archived.empty": "没有已归档会话。",
    "settings.archived.session": "个会话",
    "settings.archived.sessions": "个会话",
    "settings.archived.emptySession": "(空会话)",
    "settings.archived.restore": "恢复",
    "settings.archived.delete": "彻底",

    /* ── Sidebar ── */
    "sidebar.projects": "项目",
    "sidebar.total": "共 {count} 个",
    "sidebar.newProject": "新项目",
    "sidebar.projectLimitReached": "最多只能添加 {count} 个项目。请先移除一个项目后再添加新项目。",
    "sidebar.noProjects": "暂无项目。",
    "sidebar.noProjectsHint": "选择一个文件夹以添加项目，并运行其独立的 Somnia sidecar。",
    "sidebar.connecting": "连接中…",
    "sidebar.startingSidecar": "正在启动 sidecar…",
    "sidebar.projectOptions": "项目选项",
    "sidebar.newSession": "新建",
    "sidebar.removeProject": "移除",
    "sidebar.archiveSession": "归档",
    "sidebar.sessionOptions": "会话选项",
    "sidebar.waitingDecision": "等待你的决定",
    "sidebar.agentResponding": "Agent 正在回复",
    "sidebar.resizePanel": "调整项目面板大小",

    /* ── Conversation panel ── */
    "conversation.newConversation": "新会话",
    "conversation.workspaceUnavailable": "工作区不可用",
    "conversation.hideDetails": "隐藏详情",
    "conversation.showDetails": "显示详情",
    "conversation.startSession": "开始会话",
    "conversation.startSessionHint": "连接 sidecar，选择会话，然后发送 prompt。流式输出将显示在此处。",
    "conversation.waitingAssistant": "等待助手回复",
    "conversation.agentResponding": "Agent 正在回复",

    /* ── Composer ── */
    "composer.placeholder": "让 Somnia 检查、规划或实现当前工作区。",
    "composer.attachImage": "附加图片",
    "composer.removeImage": "移除 {name}",
    "composer.provider": "Provider",
    "composer.model": "Model",
    "composer.visionModel": "视觉 fallback",
    "composer.visionModelId": "视觉模型 ID",
    "composer.visionModelNone": "无",
    "composer.apply": "应用",
    "composer.reasoningLevel": "Reasoning level",
    "composer.send": "发送",
    "composer.queueForSession": "排队此会话",
    "composer.projectTurnLimit": "此项目已有两个会话正在运行",
    "composer.interrupt": "中断",

    /* ── Context panel ── */
    "context.kicker": "上下文",
    "context.sessionDetails": "会话详情",
    "context.collapse": "收起",
    "context.session": "会话",
    "context.updated": "更新时间",
    "context.messages": "消息数",
    "context.currentMode": "当前模式",
    "context.preview": "预览",
    "context.noSession": "未选择会话。",
    "context.noSessionHint": "从侧边栏选择一个会话以查看详情。",
    "context.resizePanel": "调整详情面板大小",
    "context.compactContext": "压缩上下文",
    "context.semanticJanitor": "语义脱水",

    /* ── CTX popover ── */
    "ctx.windowDetails": "上下文窗口详情",
    "ctx.used": "已使用",
    "ctx.window": "窗口大小",
    "ctx.ratio": "占比",
    "ctx.usageUnavailable": "上下文用量不可用",

    /* ── TaskGraph ── */
    "taskGraph.title": "任务图",
    "taskGraph.empty": "此会话没有持久任务。",
    "taskGraph.summary": "{completed}/{total} 已完成 · {inProgress} 进行中 · {pending} 待处理",
    "taskGraph.expand": "展开",
    "taskGraph.hint": "使用 task_create 构建任务图来管理较长的工作。",
    "taskGraph.close": "关闭",
    "taskGraph.untitledTask": "无标题任务",
    "taskGraph.owner": "负责人",
    "taskGraph.preferred": "首选",
    "taskGraph.blockedBy": "阻塞于",
    "taskGraph.blocks": "阻塞",
    "taskGraph.unassigned": "未分配",
    "taskGraph.none": "无",
    "taskGraph.inProgress": "进行中",
    "taskGraph.panelLabel": "任务图面板",

    /* ── Execution activity ── */
    "activity.executionActivity": "执行活动",
    "activity.subagents": "子代理",
    "activity.agentTeam": "Agent 团队",

    /* ── Interaction decision card ── */
    "decision.authorizationRequest": "授权请求",
    "decision.modeSwitchRequest": "模式切换请求",
    "decision.allowOnce": "允许一次",
    "decision.allowWorkspace": "允许此工作区",
    "decision.deny": "拒绝",
    "decision.switchNow": "立即切换",
    "decision.stayHere": "保持当前",
    "decision.approveTool": "批准 {toolName}",
    "decision.switchToMode": "切换到 {targetMode}",
    "decision.noReason": "未提供原因。",
    "decision.allowOnceReason": "已从桌面端允许一次。",
    "decision.allowWorkspaceReason": "已从桌面端允许此工作区。",
    "decision.denyReason": "已从桌面端拒绝。",

    /* ── Prompt queue ── */
    "queue.queuedPrompts": "排队中的提示",
    "queue.nextLoop": "下一轮循环",
    "queue.injectNextLoop": "插入下一轮循环",
    "queue.waitingNextLoop": "等待下一个 agent 循环",
    "queue.injectOnNextLoop": "在下一个 agent 循环时插入",

    /* ── Mermaid ── */
    "mermaid.title": "Mermaid",
    "mermaid.renderFailed": "渲染失败",
    "mermaid.graph": "图表",
    "mermaid.code": "代码",
    "mermaid.fullscreen": "全屏",
    "mermaid.rendering": "正在渲染图表…",
    "mermaid.diagram": "Mermaid 图表",
    "mermaid.reset": "重置",
    "mermaid.close": "关闭",
    "mermaid.openFullscreen": "全屏打开 Mermaid 图表",
    "mermaid.closeFullscreen": "关闭 Mermaid 图表全屏",

    /* ── Tool call card ── */
    "toolCall.input": "输入",
    "toolCall.output": "输出",
    "toolCall.images": "图片",
    "toolCall.changes": "变更",
    "toolCall.changesFor": "{path} 的变更",
    "toolCall.fileUpdated": "文件已更新。",
    "toolCall.delete": "删除",
    "toolCall.create": "创建",
    "toolCall.write": "写入",
    "toolCall.update": "更新",
    "toolCall.running": "(运行中)",
    "toolCall.noOutput": "(无输出)",

    /* ── Todo status bar ── */
    "todo.title": "待办",
    "todo.progress": "{completed}/{total} 完成",
    "todo.inProgress": "进行中",
    "todo.next": "下一个",
    "todo.hide": "收起",
    "todo.showAll": "显示全部",
    "todo.untitled": "(无标题待办)",
    "todo.progressLabel": "待办进度",

    /* ── Path picker ── */
    "pathPicker.folder": "文件夹",
    "pathPicker.file": "文件",

    /* ── Common ── */
    "common.workspace": "工作区",
    "common.executionModeUnavailable": "执行模式不可用",
    "common.lookAtThisImage": "看看这张图片。",
    "common.oneImageAttached": "[已附加 1 张图片]",
    "common.imagesAttached": "[已附加 {count} 张图片]",
    "common.emptySession": "(空会话)",
    "common.unknownPath": "(未知路径)",
    "common.working": "工作中",
    "common.active": "活跃",
    "common.prefers": "首选 {name}",
    "common.unknown": "未知",
    "common.connectFirst": "请先连接 sidecar。",

    /* ── Command specs ── */
    "cmd.init": "生成 AGENTS.md 项目说明",
    "cmd.scan": "扫描仓库或子目录",
    "cmd.symbols": "查找符号并检查匹配的源码位置",
    "cmd.image": "发送本地图片给多模态模型",
    "cmd.pasteImage": "从系统剪贴板读取图片",
    "cmd.model": "选择当前 provider 和 model",
    "cmd.vision": "选择图片理解模型",
    "cmd.reasoning": "设置当前 provider 的推理级别",
    "cmd.providers": "添加或编辑共享的 provider 配置",
    "cmd.hooks": "按事件浏览 hooks 并启用或禁用",
    "cmd.undo": "撤销最近的文件变更集",
    "cmd.checkpoint": "保存当前会话状态的命名检查点",
    "cmd.rollback": "回滚到之前的检查点",
    "cmd.compact": "压缩当前会话上下文",
    "cmd.janitor": "对当前载荷执行语义脱水",
    "cmd.skills": "选择一个 skill 应用到下一个 prompt",
    "cmd.tasks": "显示持久任务",
    "cmd.team": "显示团队成员和状态",
    "cmd.mcp": "浏览已配置的 MCP 服务器和工具",
    "cmd.bg": "显示后台任务",
    "cmd.help": "显示可用的 REPL 命令",
    "cmd.exit": "退出聊天模式",

    /* ── Execution mode options ── */
    "mode.shortcuts.title": "? 快捷键",
    "mode.shortcuts.description": "只读快捷键和轻量检查。",
    "mode.plan.title": "⏸ 规划模式",
    "mode.plan.description": "编辑前的只读规划。",
    "mode.acceptEdits.title": "⏵⏵ 允许编辑",
    "mode.acceptEdits.description": "允许文件编辑和任务更新。",
    "mode.yolo.title": "! Yolo",
    "mode.yolo.description": "此工作区的完全自主模式。",

    /* ── Banner messages ── */
    "banner.initial": "将 UI 连接到运行中的 sidecar 并开始一个会话。",
    "banner.connecting": "正在连接 sidecar…",
    "banner.connectingTo": "正在连接 {path}…",
    "banner.connectedTo": "已连接到 {url}",
    "banner.connectedBundled": "已连接到内置 sidecar {url}",
    "banner.activeProject": "当前项目：{path}",
    "banner.disconnected": "Sidecar 事件流已断开。",
    "banner.streamFailed": "Sidecar 事件流失败。",
  },
} as const;

type I18nContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: TranslationKey, params?: Record<string, string | number>) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => readStoredLocale());

  function setLocale(nextLocale: Locale) {
    setLocaleState(nextLocale);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(LANGUAGE_STORAGE_KEY, nextLocale);
    }
  }

  const value = useMemo<I18nContextValue>(
    () => ({
      locale,
      setLocale,
      t: (key, params) => interpolate(translations[locale][key] ?? translations["en-US"][key] ?? key, params),
    }),
    [locale],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nContextValue {
  const value = useContext(I18nContext);
  if (!value) {
    throw new Error("useI18n must be used inside I18nProvider.");
  }
  return value;
}

function readStoredLocale(): Locale {
  if (typeof window === "undefined") {
    return "zh-CN";
  }
  const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY);
  return isSupportedLocale(stored) ? stored : defaultLocale();
}

function defaultLocale(): Locale {
  if (typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("zh")) {
    return "zh-CN";
  }
  return "en-US";
}

function isSupportedLocale(value: unknown): value is Locale {
  return typeof value === "string" && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

function interpolate(value: string, params: Record<string, string | number> | undefined): string {
  if (!params) {
    return value;
  }
  return value.replace(/\{(\w+)\}/g, (match, key) => (key in params ? String(params[key]) : match));
}
