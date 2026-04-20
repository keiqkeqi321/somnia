# TodoWrite

## 概述

`TodoWrite` 是 Somnia 的会话级待办清单工具，允许 Agent 在当前会话中维护一个结构化的短期任务清单。

---

## 数据模型

每个 todo 项包含以下字段：

```python
{
    "content": "任务描述",          # 必填
    "status": "pending",           # 必填: pending | in_progress | completed | cancelled
    "activeForm": "正在重构 auth",  # 必填: 当前活跃状态的描述
    "cancelledReason": "方案A已废弃" # 当 status=cancelled 时必填
}
```

---

## 状态与标记

| 状态 | 标记 | 含义 |
|------|------|------|
| `pending` | ☐ | 待处理 |
| `in_progress` | ⏳ | 进行中（同一时刻只能有一项） |
| `completed` | ✔ | 已完成 |
| `cancelled` | ✖ | 已取消（需要 `cancelledReason`） |

---

## 渲染

Todo 清单渲染时**不显示 cancelled 项**，可见状态为 `pending / in_progress / completed`。

输出格式：
```
⏳ 重构 auth 模块 <- 正在重构 auth
☐ 编写单元测试
☐ 更新文档
✔ 分析现有代码

(1/4 completed)
```

其中 `in_progress` 项会追加 ` <- activeForm` 标注。

---

## 约束规则

`TodoManager.update()` 执行以下验证：

| 规则 | 说明 |
|------|------|
| `content` 必填 | 空内容会报错 |
| `activeForm` 必填 | 空 activeForm 会报错 |
| 状态合法性 | 必须是四种状态之一 |
| `cancelled` 需要原因 | `cancelledReason` 必填 |
| **最多 20 项** | 超过上限会报错 |
| **仅一项 in_progress** | 同时只能有一个进行中的任务 |

---

## 工具 Schema

```json
{
    "name": "TodoWrite",
    "description": "Update the short-lived todo checklist for the current session.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                        "activeForm": {"type": "string"},
                        "cancelledReason": {"type": "string"}
                    },
                    "required": ["content", "status", "activeForm"],
                    "allOf": [{
                        "if": {"properties": {"status": {"const": "cancelled"}}},
                        "then": {"required": ["cancelledReason"]}
                    }]
                }
            }
        },
        "required": ["items"]
    }
}
```

---

## 运行时提醒机制

只要会话里仍有 **open todo**（`pending` 或 `in_progress`），运行时就会在**每一轮发给模型的 payload** 里临时追加一条 reminder：

```text
<reminder>If any todo changed, call TodoWrite now. Do not just say you will. If nothing changed, ignore this and continue.</reminder>
```

这条 reminder 的规则是：

- 仅注入到当前轮次的模型 payload
- **不会**写入 `session.messages`
- **不会**写入 transcript snapshot
- 当所有 todo 都进入 closed 状态（`completed` 或 `cancelled`）后立即停止注入

这样做的目的不是强制每轮都调用 `TodoWrite`，而是约束 Agent：

- 如果 todo 状态确实发生变化，就先调用 `TodoWrite`
- 不要只口头说“我来更新 todo”
- 如果 todo 状态没有变化，就忽略 reminder 并继续当前任务

因此：

- reminder 本身**不是**“有 open todo 就禁止结束”的硬门禁
- 但如果 Agent Loop 达到 `max_agent_rounds`（默认 100）时仍有 open todo，runtime 会返回显式状态 `stopped_with_open_todos`
- 上层 REPL / CLI 应把它视为“停在未完成任务上”，而不是正常 done

---

## 与上下文治理的关系

Todo 在上下文治理（Semantic Janitor）中仅作为**弱锚点**使用：

- 命中 `in_progress` todo：提高保留概率
- 仅服务于 `completed` todo：提高压缩概率
- 无 todo 命中：不直接判定为无价值

参见 [14-上下文治理优化方案](./14-上下文治理优化方案.md)。

---

## 存储位置

Todo 项存储在会话数据中（`AgentSession.todo_items`），随会话持久化到：

```
.open_somnia/sessions/{session_id}.json
```

---

## 相关代码

- `open_somnia/tools/todo.py` — `TodoManager`, `register_todo_tool()`
- `open_somnia/runtime/session.py` — `AgentSession.todo_items`
