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
│  0. turn-boundary janitor   │  只在用户发问后检查
│     - 用户消息已入列         │  - 必要时语义脱水
└──────────────┬──────────────┘
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
│     - deep clone messages   │  - strip tool metadata
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
                  │  - auto compact     │  (82% 阈值)
                  └──────────┬──────────┘
                             ▼
                  继续下一轮循环 (回到步骤 1，最多 `max_agent_rounds`，默认 100)
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
2. **Strip metadata**（移除 payload 副本里的 `raw_output` / `log_id`）
3. **Strip stale media/tool blocks**（清理过期图片块和旧工具结果的富内容块）
4. **返回最终 payload**

`_messages_for_model()` 现在是**无副作用**的。自动 `Semantic Janitor` 不在这里执行，而是在新 user message 入列后的 turn boundary 先完成。

在进入 `_messages_for_model()` 之前，调用方还可能临时扩展消息列表。例如 Todo reconcile、空响应修复、工具修复提示或探索预算提醒会被合并成一条**仅当前轮次可见**的 `<runtime-notice>`，再把扩展后的列表送入 `_messages_for_model()`。这条 notice 不会写回会话历史。

另外，`_messages_for_model()` 做的是**payload 级别**的归一化，而不是会话历史改写。为保持 provider prompt cache 前缀稳定，它不再对旧工具结果做 duplicate dedupe，也不再根据后续 `read_file` 结果裁剪早期 `read_file` 内容。

### 探索预算

Agent Loop 会把 `tree`、`glob`、`grep`、`read_file`、`find_symbol` 以及只读型 shell 查询识别为探索工具：

- 达到 `runtime.exploration_soft_limit`（默认 10）后，下一轮 payload 会临时注入总结提醒，要求先给出阶段性结论
- 如果模型在被提醒后输出了可见阶段性结论，这会被视为一次探索段落边界：`exploration_streak` 会清零，后续 `read/search` 从新的连续探索段重新计数；`exploration_total` 不清零，但默认不启用总量硬停
- 如果模型在阶段性结论后同时继续发起必要的 `read/search`，这些工具会按新的 `exploration_streak` 计数，避免刚总结完又立刻因为旧连续计数触发第二次提醒
- 超过 `runtime.exploration_hard_streak_limit`（默认 14）个连续探索调用时，runtime 不再执行下一个探索工具
- `runtime.exploration_hard_total_limit` 默认为 `0`，表示关闭本轮总探索次数硬停；显式设为正数后，超过该总量时同样会拦截下一个探索工具
- 硬停止以 `exploration_budget_exceeded` 工具错误返回给模型，并结束当前工具批次，让模型总结已有证据或向用户请求继续
- 三个阈值都可在 `[runtime]` 中配置；硬阈值设为 `0` 可关闭对应硬限制

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

### 结束状态

Agent Loop 不只有“正常完成”这一种退出方式：

- 模型返回纯文本且没有 `tool_calls` 时，返回 `status="completed"`
- 如果达到 `max_agent_rounds` 仍未结束，返回显式停止状态，而不是把它伪装成正常完成
- 当达到 `max_agent_rounds` 且会话里仍有 open todo 时，返回 `status="stopped_with_open_todos"`
- 当达到 `max_agent_rounds` 但没有 open todo 时，返回 `status="stopped_after_max_rounds"`

这样上层 REPL / CLI 可以区分：

- 正常 done
- 轮次耗尽但还有未完成工作
- 轮次耗尽但没有 open todo

---

## 会话状态

`AgentSession` 数据类维护以下状态：

| 字段 | 说明 |
|------|------|
| `id` | 会话唯一标识 |
| `messages` | 对话消息列表 |
| `token_usage` | 累计 token 用量 |
| `todo_items` | 当前待办清单 |
| `rounds_without_todo` | 自上次 `TodoWrite` 以来的轮数计数器 |
| `latest_turn_id` | 最新轮次 ID |
| `last_turn_file_changes` | 上一轮文件变更摘要 |
| `undo_stack` | 文件修改撤销栈（最多 10 轮） |

---

## 上下文治理集成

上下文治理的触发点现在拆成两层：

| 触发条件 | 动作 | 阈值 |
|----------|------|------|
| 新 user message 入列后，且 `usage_ratio >= runtime.janitor_trigger_ratio`（默认 `0.60`） | Semantic Janitor | 语义脱水 |
| Agent Loop 内，且 `usage_ratio >= 0.82` | Auto Compact | 整体摘要压缩 |

---

## 相关代码

- `open_somnia/runtime/agent.py` — `OpenAgentRuntime`
- `open_somnia/runtime/compact.py` — `CompactManager`
- `open_somnia/runtime/session.py` — `SessionManager`, `AgentSession`
- `open_somnia/runtime/system_prompt.py` — `SystemPromptBuilder`
- `open_somnia/runtime/permissions.py` — `PermissionManager`
- `open_somnia/tools/registry.py` — `ToolRegistry`
