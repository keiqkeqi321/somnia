# Agent Team

## 概述

Agent Team 是 Somnia 的多 Agent 协作系统，允许主 Agent（Lead）Spawn 持久化团队成员（Teammate），通过消息总线进行异步通信，实现并行工作、任务自动认领和计划审批。

---

## 架构

```
┌──────────────────────────────────────────────────────┐
│                      Lead Agent                      │
│  (主线程，完整工具集)                                  │
│                                                      │
│  ├── spawn_teammate("explorer", "researcher", "...") │
│  ├── send_message("explorer", "调查 API 设计")       │
│  ├── read_inbox()                                    │
│  ├── plan_approval(request_id, approve=True)         │
│  └── shutdown_request("explorer")                    │
└────────────────────┬─────────────────────────────────┘
                     │
              MessageBus (基于 InboxStore)
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│explorer  │ │builder   │ │reviewer  │
│(线程 A)  │ │(线程 B)  │ │(线程 C)  │
│          │ │          │ │          │
│ Worker   │ │ Worker   │ │ Worker   │
│ ToolReg  │ │ ToolReg  │ │ ToolReg  │
└──────────┘ └──────────┘ └──────────┘
```

---

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `TeammateRuntimeManager` | `runtime/teammate.py` | 团队成员生命周期管理 |
| `MessageBus` | `collaboration/bus.py` | 消息传递 |
| `RequestTracker` | `collaboration/protocols.py` | 关机请求和计划审批追踪 |
| `TeamStore` | `storage/team.py` | 团队配置和活动日志持久化 |
| `InboxStore` | `storage/inbox.py` | 每个 Agent 的收件箱 |

---

## 工具集

### Lead 团队工具

| 工具 | 说明 |
|------|------|
| `spawn_teammate` | 创建持久化团队成员 |
| `list_teammates` | 列出所有团队成员 |
| `send_message` | 发送消息给指定成员 |
| `read_inbox` | 读取并清空收件箱 |
| `broadcast` | 广播消息给所有成员 |
| `shutdown_request` | 请求成员关闭 |
| `plan_approval` | 审批/拒绝成员的计划请求 |

### Worker 本地工具（Teammate 可用）

| 工具 | 说明 |
|------|------|
| `send_message` | 发送消息给 Lead 或其他成员 |
| `idle` | 进入空闲状态 |
| `submit_plan` | 提交计划供 Lead 审批 |

---

## 消息类型

| 类型 | 说明 |
|------|------|
| `message` | 普通消息 |
| `broadcast` | 广播消息 |
| `shutdown_request` | 关机请求 |
| `shutdown_response` | 关机响应 |
| `plan_request` | 计划审批请求 |
| `plan_approval_response` | 计划审批响应 |

---

## Teammate 生命周期

```
spawn_teammate(name, role, prompt)
        │
        ▼
┌──────────────────────────┐
│  1. 创建成员记录         │
│     status = "starting"  │
│     activity = "booting" │
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│  2. 启动独立线程         │
│     _loop(name, role,    │
│           prompt)        │
└──────────┬───────────────┘
           ▼
┌──────────────────────────┐
│  3. 构建 Worker Registry │
│     构建系统提示          │
│     初始消息 = [prompt]  │
└──────────┬───────────────┘
           ▼
┌──────────────────────────────┐
│  4. 主工作循环               │
│  while True:                 │
│    ├── 检查关机请求          │
│    ├── 读取收件箱            │
│    ├── 注入 repair hint      │
│    ├── provider.complete()   │
│    ├── 执行工具调用          │
│    └── idle 阶段:            │
│        ├── 轮询收件箱        │
│        ├── 检查所属任务      │
│        └── 自动认领任务      │
└──────────────┬───────────────┘
               ▼
        shutdown / error / idle_timeout
```

---

## Teammate 状态

| 状态 | 说明 |
|------|------|
| `starting` | 正在启动 |
| `working` | 工作中 |
| `idle` | 空闲等待 |
| `shutdown` | 已关闭 |

### 活动状态

`activity` 字段记录更细粒度的当前活动：

| 活动 | 说明 |
|------|------|
| `checking_inbox` | 检查收件箱 |
| `waiting_for_model` | 等待模型响应 |
| `running_tool:{name}` | 执行工具 |
| `idle_polling` | 空闲轮询 |
| `idle_waiting_on_owned_task` | 等待自己认领的任务 |
| `auto_claimed_task` | 自动认领了任务 |
| `interrupt_requested` | 收到中断请求 |
| `shutdown_request` | 收到关机请求 |
| `idle_timeout` | 空闲超时自动关闭 |
| `runtime_error` | 运行时错误 |

---

## 空闲与自动任务认领

Teammate 进入 idle 后会周期性轮询：

```python
for _ in range(poll_total // poll_interval):
    # 1. 检查收件箱 → 有新消息则恢复工作
    inbox = bus.read_inbox(name)
    if inbox:
        resume = True
        break
    
    # 2. 检查自己拥有的开放任务 → 继续等待
    if has_open_task(name):
        continue
    
    # 3. 自动认领可执行任务 → 开始工作
    claimable = task_store.list_claimable_for(name)
    if claimable:
        task_store.claim(task["id"], name)
        # 注入任务到消息
        resume = True
        break

# 轮询超时后自动关闭
if not resume:
    shutdown(shutdown_reason="idle_timeout")
```

---

## 计划审批流程

```
Teammate                    Lead
    │                         │
    ├── submit_plan(plan) ──▶│
    │                         ├── 创建 PlanRequest
    │                         ├── 记录到 RequestTracker
    │                         └── 通知 Lead
    │                         │
    │                         ├── plan_approval(
    │                         │      request_id,
    │                         │      approve=True/False)
    │                         │
    ├── 收到审批响应 ◀────────┤
    │    (通过 inbox)         │
```

---

## 关机流程

```
shutdown_request("explorer")
        │
        ├── RequestTracker.create_shutdown_request()
        ├── MessageBus.send(sender, "explorer", 
        │                   "Please shut down.", 
        │                   "shutdown_request")
        │
        └── explorer 线程收到 shutdown_request
            ├── _handle_control_message() → return True
            ├── _request_stop(name, "shutdown_request")
            ├── 更新状态 → shutdown
            └── 发送 shutdown_response 给 Lead
```

---

## 启动恢复

Somnia 重启时会自动恢复活跃 Teammate：

```python
def _restore_state(self) -> None:
    for member in config.get("members", []):
        if member.get("status") in {"starting", "working", "idle"}:
            # 从日志恢复 prompt 和消息历史
            prompt, messages = self._restore_prompt_and_messages(name)
            if prompt:
                member["status"] = "starting"
                self._start_thread(name, role, prompt,
                                   initial_messages=messages,
                                   resumed=True)
```

---

## 工具错误与修复提示

Teammate Worker 与 Lead/Subagent 共享同一套工具错误协议：

- `ToolRegistry.execute()` 返回统一结构化错误外壳
- 只有 `missing_required_params`、`invalid_arguments` 这类可自修复错误会提取 `repair_hint`
- Worker 不会把 `repair_hint` 塞回当前轮 `tool_result_message`
- 下一轮调用模型前，会把待处理提示渲染成一次性的 `<tool-repair-hints>` 消息注入 `messages`

与 Lead 会话不同，Teammate 会把这条一次性提示额外记录成一条：

```python
{
    "type": "user_message",
    "source": "tool_repair_hint",
    "content": "<tool-repair-hints>...</tool-repair-hints>",
}
```

这样做的结果是：

- Worker 给模型看的下一轮提示仍然是临时注入，只消费一次
- `tool_result_message` 和工具日志里保留的是去掉 `repair_hint` 的精简结构化错误
- 团队活动日志仍能复盘“这一轮曾注入过什么修复提示”

---

## 活动日志

每个 Teammate 的活动记录在 `team_log` 中：

```
.open_somnia/data/team/{name}.log.json
```

日志事件类型：
- `session_started` — 会话启动
- `session_resumed` — 启动恢复
- `user_message` — 用户消息（含来源）
- `assistant_message` — Agent 回复
- `tool_call` — 工具调用记录
- `tool_result_message` — 工具结果
- `runtime_error` — 运行时错误

当出现一次性修复提示时，会额外记录一条 `user_message(source="tool_repair_hint")`。对应的 `tool_result_message` 仍然只保存精简后的结构化错误，避免把大 schema 或重复提示持续堆进工作历史。

---

## 配置超时

| 配置项 | 默认行为 |
|--------|---------|
| `max_agent_rounds` | 每轮最大 Agent Loop 次数 |
| `teammate_idle_timeout_seconds` | 空闲超时时间 |
| `teammate_poll_interval_seconds` | 轮询间隔 |

---

## 相关代码

- `open_somnia/runtime/teammate.py` — `TeammateRuntimeManager`
- `open_somnia/collaboration/bus.py` — `MessageBus`
- `open_somnia/collaboration/protocols.py` — `RequestTracker`
- `open_somnia/tools/team.py` — 团队工具注册
- `open_somnia/storage/team.py` — `TeamStore`
- `open_somnia/storage/inbox.py` — `InboxStore`
- `open_somnia/tools/tool_errors.py` — 统一错误外壳、修复提示提取与渲染
