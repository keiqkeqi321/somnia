# Hooks

## 概述

Somnia 的 Hooks 系统是在运行时生命周期关键节点上触发的外部扩展机制。

第一阶段目标不是把主循环改造成插件平台，而是提供一组稳定、最小、可观测的事件插口，满足以下场景：

- 工具执行前做拦截、改写或审计
- 工具执行后做通知或日志收集
- 会话启动时做初始化动作
- 智能体输出最终自然语言回复后做通知
- 系统即将阻塞等待用户选择时做提醒

Hooks 与权限系统分层：

- 权限系统负责 gate
- Hooks 系统负责 extend

也就是说，工具调用仍然先经过权限检查，再进入 Hook 扩展流程。

---

## 当前事件

第一阶段已支持 5 个事件：

| 事件名 | 触发时机 | 是否允许改变执行 |
|------|------|------|
| `SessionStart` | 新会话创建后 | 否 |
| `PreToolUse` | 工具通过权限检查、执行前 | 是 |
| `PostToolUse` | 工具执行完成后，无论成功或异常 | 否 |
| `AssistantResponse` | 一轮回复结束且没有工具调用，准备返回最终文本前后链路 | 否 |
| `UserChoiceRequested` | 系统即将进入等待用户选择 | 否 |

### 默认内置通知 Hook

Somnia 现在默认内置两条通知 Hook：

- `AssistantResponse`
- `UserChoiceRequested`

它们不需要工作区额外配置。

Somnia 会在构建或加载公共配置时，把这两条内置通知 Hook 写入全局共享配置：

```text
~/.open_somnia/open_somnia.toml
```

同时把通知脚本安装到共享目录：

```text
~/.open_somnia/Hooks/builtin_notify/
```

如果用户在 `open_somnia.toml` 中显式配置了同名事件的 Hook，则该事件不会再重复注入默认通知 Hook，避免双重通知。

### `UserChoiceRequested`

当前会在以下两种场景触发：

- `request_authorization`
- `request_mode_switch`

该事件适合：

- 桌面通知
- 手机推送
- 外部状态灯
- 审计记录

---

## 核心结构

```
HookManager
├── on_session_start(...)
├── before_tool_use(...)
├── after_tool_use(...)
├── on_assistant_response(...)
└── on_user_choice_requested(...)

HookRunner
└── run(hook, context)   # 启动外部进程并交换 JSON
```

### 主要模块

- `open_somnia/hooks/models.py`
  - Hook 事件名、上下文模型、决策模型
- `open_somnia/hooks/runner.py`
  - 外部命令执行、stdin/stdout JSON 协议、超时处理
- `open_somnia/hooks/manager.py`
  - 事件匹配、执行顺序、日志记录

---

## 配置格式

Hooks 通过 `open_somnia.toml` 中的 `[[hooks]]` 配置。

示例：

```toml
[[hooks]]
event = "PreToolUse"
command = "python"
args = ["hooks/pre_bash.py"]
timeout_seconds = 10
on_error = "continue"

[hooks.matcher]
tool_name = "bash"
actor = "lead"
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `event` | Hook 事件名 |
| `command` | 要执行的命令 |
| `args` | 命令参数列表 |
| `cwd` | 可选工作目录，支持相对工作区路径 |
| `env` | 额外环境变量 |
| `timeout_seconds` | Hook 超时秒数 |
| `on_error` | `continue` 或 `fail` |
| `enabled` | 是否启用 |
| `matcher.tool_name` | 可选，仅匹配某个工具 |
| `matcher.actor` | 可选，仅匹配某个 actor |

---

## 上下文协议

Somnia 会把事件上下文以 JSON 形式通过 stdin 传给外部 Hook。

常见字段：

```json
{
  "event": "PreToolUse",
  "session_id": "session-123",
  "trace_id": "session-123-abc",
  "actor": "lead",
  "execution_mode": "accept_edits",
  "workspace_root": "E:/GitRepository/somnia",
  "tool_name": "bash",
  "tool_input": {
    "command": "git status"
  }
}
```

不同事件会附加额外字段。

### `PostToolUse`

可能附带：

- `tool_result`
- `tool_error`

### `AssistantResponse`

附带：

- `assistant_message`
- `text`

### `UserChoiceRequested`

附带：

- `choice_type`
- `choice_payload`
- `options`

示例：

```json
{
  "event": "UserChoiceRequested",
  "choice_type": "authorization",
  "choice_payload": {
    "tool_name": "bash",
    "reason": "Inspect repo",
    "argument_summary": "command=pwd"
  },
  "options": ["allow_once", "allow_workspace", "deny"]
}
```

---

## 返回协议

大多数事件默认只需要退出码为 0，不要求返回内容。

如果 Hook 向 stdout 输出 JSON，则 Somnia 会解析该对象。

### `PreToolUse`

`PreToolUse` 是当前唯一允许改变执行结果的事件。

支持以下 `action`：

#### 1. `continue`

```json
{
  "action": "continue"
}
```

#### 2. `deny`

```json
{
  "action": "deny",
  "message": "blocked by policy"
}
```

#### 3. `replace_input`

```json
{
  "action": "replace_input",
  "replacement_input": {
    "command": "git status --short"
  }
}
```

### 其他事件

除 `PreToolUse` 外，其余事件如果返回 `deny` 或 `replace_input` 会被视为协议错误。

---

## 执行顺序

### 工具执行链路

```
模型返回 tool_call
        ↓
ToolRegistry.execute()
        ↓
PermissionManager.authorize_tool_call()
        ↓
PreToolUse
        ↓
tool handler
        ↓
PostToolUse
        ↓
返回 tool_result
```

### 最终自然语言回复链路

```
模型返回 end_turn 且无 tool_call
        ↓
assistant message 写入 session / transcript
        ↓
保存 session
        ↓
AssistantResponse
        ↓
返回最终文本
```

### 用户选择链路

```
request_authorization / request_mode_switch
        ↓
UserChoiceRequested
        ↓
进入等待用户选择
```

---

## 错误策略

Hook 失败时由 `on_error` 控制：

- `continue`
  - 记录错误日志
  - 忽略本次 Hook 失败
  - 主流程继续
- `fail`
  - 抛出错误
  - 中断当前流程

第一阶段推荐：

- 通知类 Hook 使用 `continue`
- 策略类 `PreToolUse` Hook 视需要使用 `fail`

---

## 日志

Hooks 执行日志写入：

```text
.open_somnia/logs/hooks.jsonl
```

每条日志包含：

- 时间
- 事件名
- hook 标识
- actor
- session_id
- trace_id
- 执行耗时
- action
- 错误信息

---

## 当前接点

### 会话创建

- `open_somnia/runtime/agent.py`
- `OpenAgentRuntime.create_session()`

### 工具执行

- `open_somnia/tools/registry.py`
- `ToolRegistry.execute()`

### 最终回复

- `open_somnia/runtime/agent.py`
- `OpenAgentRuntime._agent_loop()`

### 用户选择请求

- `open_somnia/runtime/permissions.py`
- `PermissionManager.request_authorization()`
- `PermissionManager.request_mode_switch()`

---

## 设计边界

第一阶段刻意保持收敛，不支持以下能力：

- 直接修改 session 持久化结构
- 直接改写系统提示
- 直接拦截 REPL UI 渲染
- 在非 `PreToolUse` 事件上控制执行流
- 任意注入新的内部 runtime 状态

这样做的原因是先稳定事件边界和协议，再逐步扩展事件面。

---

## 相关代码

- `open_somnia/hooks/models.py`
- `open_somnia/hooks/runner.py`
- `open_somnia/hooks/manager.py`
- `open_somnia/config/models.py`
- `open_somnia/config/settings.py`
- `open_somnia/tools/registry.py`
- `open_somnia/runtime/agent.py`
- `open_somnia/runtime/permissions.py`
- `tests/test_hook_system.py`
