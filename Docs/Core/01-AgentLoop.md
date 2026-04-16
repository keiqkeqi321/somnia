# Agent Loop

## 概述

Agent Loop 是 Somnia 的核心执行循环，驱动 Agent 从接收用户指令到产出结果的完整过程。它管理系统提示构建、模型调用、工具执行、上下文治理和状态持久化。

---

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `OpenAgentRuntime` | `runtime/agent.py` | 运行时总控：提供者、工具、会话、压缩、权限等 |
| `SystemPromptBuilder` | `runtime/system_prompt.py` | 动态构建系统提示 |
| `CompactManager` | `runtime/compact.py` | 上下文压缩与语义治理 |
| `SessionManager` | `runtime/session.py` | 会话创建、加载、持久化、检查点 |
| `ToolRegistry` | `tools/registry.py` | 工具注册与执行调度 |
| `PermissionManager` | `runtime/permissions.py` | 工具调用权限检查 |
| `SubagentRunner` | `runtime/subagent_runner.py` | 子代理隔离执行 |
| `TeammateRuntimeManager` | `runtime/teammate.py` | 持久团队成员生命周期管理 |

---

## Agent Loop 流程

```
用户输入 (REPL / 任务 / 消息)
        │
        ▼
┌─────────────────────────────┐
│  1. build_system_prompt     │  动态构建系统提示
│     - 角色/名称注入          │  - 工具 schema 注入
│     - 执行模式指引           │  - 环境指引
│     - 技能/上下文辅助        │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  2. _messages_for_model     │  构建 payload
│     - deep clone messages   │  - 注入语义治理(janitor)
│     - 计算 token 使用量      │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│  3. provider.complete()     │  调用 LLM
│     - Anthropic / OpenAI    │  - 返回 Turn 对象
└──────────────┬──────────────┘
               ▼
       ┌───────┴───────┐
       ▼               ▼
  无 tool_calls    有 tool_calls
       │               │
       ▼               ▼
  文本响应        ┌─────────────────────┐
  结束本轮        │  4. 执行工具调用     │
                  │  - 权限检查          │
                  │  - 工具执行          │
                  │  - 结果记录          │
                  └──────────┬──────────┘
                             ▼
                  ┌─────────────────────┐
                  │  5. 捕获文件变更     │
                  │  - undo_stack 更新   │
                  │  - pending_file_*    │
                  └──────────┬──────────┘
                             ▼
                  ┌─────────────────────┐
                  │  6. 上下文治理检查   │
                  │  - semantic janitor │  (60% 阈值)
                  │  - auto compact     │  (82% 阈值)
                  └──────────┬──────────┘
                             ▼
                  继续下一轮循环 (回到步骤 1)
```

---

## 核心方法

### `complete(system_prompt, messages, tools, ...)`

`OpenAgentRuntime.complete()` 是 Agent Loop 的核心入口：

```python
turn = self.provider.complete(
    system_prompt=system_prompt,
    messages=messages,
    tools=tools,
    max_tokens=self.settings.provider.max_tokens,
)
```

### `_messages_for_model(...)`

负责构建发送给模型的消息 payload，包含：

1. **Deep clone** 原始消息
2. **Strip metadata**（移除 `raw_output` / `log_id`）
3. **Semantic Janitor**（当使用率 >= 60%）：对历史工具结果执行语义脱水
4. **返回最终 payload**

### 权限检查

工具执行前通过 `PermissionManager.authorize_tool_call()` 检查：

- **workspace 授权**：`permissions.json` 中已授权的工具
- **一次性授权**：用户通过 `request_authorization` 批准的单次调用
- **执行模式**：根据 `shortcuts / plan / accept_edits / yolo` 决定允许范围

### 文件变更追踪

每轮工具执行后调用 `_capture_turn_file_changes()`：

```python
session.undo_stack.append({
    "turn_id": session.latest_turn_id,
    "files": [{"path": "...", "previous_content": "...", "existed_before": True}, ...]
})
```

支持最多 10 轮 undo 记录（`MAX_UNDO_TURNS = 10`）。

---

## 会话状态

`AgentSession` 数据类维护以下状态：

| 字段 | 说明 |
|------|------|
| `id` | 会话唯一标识 |
| `messages` | 对话消息列表 |
| `token_usage` | 累计 token 用量 |
| `todo_items` | 当前待办清单 |
| `rounds_without_todo` | 无 todo 的轮数计数器 |
| `latest_turn_id` | 最新轮次 ID |
| `last_turn_file_changes` | 上一轮文件变更摘要 |
| `undo_stack` | 文件修改撤销栈（最多 10 轮） |

---

## 上下文治理集成

在 Agent Loop 中，上下文治理是自动触发的：

| 触发条件 | 动作 | 阈值 |
|----------|------|------|
| `usage_ratio >= 0.60` | Semantic Janitor | 语义脱水 |
| `usage_ratio >= 0.82` | Auto Compact | 整体摘要压缩 |

---

## 相关代码

- `open_somnia/runtime/agent.py` — `OpenAgentRuntime`
- `open_somnia/runtime/compact.py` — `CompactManager`
- `open_somnia/runtime/session.py` — `SessionManager`, `AgentSession`
- `open_somnia/runtime/system_prompt.py` — `SystemPromptBuilder`
- `open_somnia/runtime/permissions.py` — `PermissionManager`
- `open_somnia/tools/registry.py` — `ToolRegistry`
