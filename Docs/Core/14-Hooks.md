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

内置通知 Hook 默认开启。

- 同名事件的用户 Hook 默认不会替代内置通知 Hook
- 如果需要关闭内置通知，直接把对应内置 Hook 的 `enabled` 设为 `false`
- 如果工作区配置里声明了同 `event` 且同 `managed_by` 的 Hook，则它会覆盖全局共享配置里的对应 managed Hook
- 默认内置通知 Hook 同时启用 `background = true`，因此不会阻塞 REPL 主链路

### Hook 开关管理

Somnia 现在提供 REPL 命令：

```text
/hooks
```

该命令用于浏览和切换 Hook 开关，交互流程分两层：

- 第一层先显示事件列表
- 每个事件显示：
  - 该事件下 Hook 总数
  - 已开启 Hook 数
- 第二层显示该事件下的全部 Hook
- 每个 Hook 显示：
  - 是否开启
  - 是否内置
  - 对应命令摘要

在 Hook 详情页中可以直接切换 `enabled` 状态。切换后会写回对应配置文件，并立即刷新当前运行时的 Hook 配置。

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

异步通知类 Hook 示例：

```toml
[[hooks]]
event = "AssistantResponse"
command = "python"
args = ["hooks/notify.py"]
background = true
enabled = true
on_error = "continue"
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
| `enabled` | 是否启用，默认 `true` |
| `background` | 是否后台执行，默认 `false`；仅非 `PreToolUse` 事件支持 |
| `managed_by` | 可选，标识该 Hook 由哪个管理方维护；工作区中同 `event` 且同 `managed_by` 的 Hook 会覆盖全局对应 Hook |
| `matcher.tool_name` | 可选，仅匹配某个工具 |
| `matcher.actor` | 可选，仅匹配某个 actor |

内置通知 Hook 也使用同一个 `enabled` 字段，不再需要单独的抑制字段。

`managed_by` 主要用于“托管 Hook”的覆盖规则，而不是普通自定义 Hook 的命名标签。

`background = true` 时：

- Somnia 只负责启动 Hook 进程，不等待其完成
- 该 Hook 不能改变主流程执行结果
- 当前不支持与 `PreToolUse` 组合使用
- 当前不支持 `on_error = "fail"`

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
  "session_path": "E:/GitRepository/somnia/.open_somnia/sessions/session-123.json",
  "transcript_path": "E:/GitRepository/somnia/.open_somnia/transcripts/session-123.jsonl",
  "snapshot_path": "E:/GitRepository/somnia/.open_somnia/transcripts/session-123.snapshot.json",
  "tool_name": "bash",
  "tool_input": {
    "command": "git status"
  }
}
```

不同事件会附加额外字段。

其中 `session_path`、`transcript_path`、`snapshot_path` 是给 Hook 脚本按需读取上下文的轻量引用，不会把整段历史直接塞进 JSON。

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

对于 `background = true` 的 Hook，Somnia 不解析 stdout 决策，stdout/stderr 仅用于日志与排障。

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
- 状态：`queued`、`ok`、`error`
- 事件名
- hook 标识
- actor
- session_id
- trace_id
- 执行耗时
- action
- 错误信息
- 是否后台执行
- 进程 pid（后台 Hook 启动成功时）

---

## Python SDK

Somnia 现在提供一个轻量 Python SDK：

- `open_somnia/hooks/sdk.py`

它提供：

- `HookHandler`
- `HookPayload`
- `HookResponse`
- `continue_response()`
- `deny_response()`
- `replace_input_response()`
- `run()`

示例：

```python
from open_somnia.hooks.sdk import HookHandler, replace_input_response, run


class RewriteHandler(HookHandler):
    def handle(self, payload):
        if payload.event == "PreToolUse":
            return replace_input_response({"command": "git status --short"})
        return None


if __name__ == "__main__":
    raise SystemExit(run(RewriteHandler()))
```

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
- `open_somnia/hooks/sdk.py`
- `open_somnia/config/models.py`
- `open_somnia/config/settings.py`
- `open_somnia/tools/registry.py`
- `open_somnia/runtime/agent.py`
- `open_somnia/runtime/permissions.py`
- `tests/test_hook_system.py`
