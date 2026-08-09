# Subagent

## 概述

Subagent（子代理）是 Somnia 的隔离执行单元，允许主 Agent 在独立的上下文中执行探索或实现任务，保持主会话上下文整洁。

---

## 架构

```
主 Agent (lead)
    │
    ├── 调用 subagent 工具
    │       │
    │       ▼
    │   SubagentRunner.run_subagent(prompt, agent_type)
    │       │
    │       ├── 构建独立 ToolRegistry
    │       ├── 构建独立系统提示
    │       ├── 独立消息列表
    │       │
    │       └── 执行 Agent Loop（最多 max_subagent_rounds 轮）
    │               │
    │               ├── provider.complete()
    │               ├── ToolRegistry.execute()
    │               └── 返回文本摘要
    │
    └── 接收摘要 → 主会话继续
```

---

## Agent 类型

### Explore 模式（默认）

**只读工具集**：

| 工具 | 说明 |
|------|------|
| `bash` | Shell 命令（受只读门控，禁止写入类命令） |
| `tree` | 目录树 |
| `find_symbol` | 符号查找 |
| `glob` | 文件模式匹配 |
| `grep` | 内容搜索 |
| `read_file` | 文件读取 |
| `read_image` | 图片读取 |
| `web_fetch` | 网页抓取 |
| `load_skill` | 技能加载 |
| `submit_summary` | **完成信号**（见下） |

**禁止**：任何文件写入操作。

### general-purpose 模式

在 Explore 模式基础上，额外增加：

| 工具 | 说明 |
|------|------|
| `write_file` | 文件写入 |
| `edit_file` | 文本替换 |

`edit_file` 与主 Agent 保持同一约定：只接受 `edits=[{old_text,new_text}, ...]`，单次替换也必须包装成单元素数组。

> 两种模式都注册了 `submit_summary`，它是子代理**唯一**的完成方式。

---

## 完成协议（重要）

子代理的循环判定：**只有调用 `submit_summary` 才算完成**。这对应 `round_runner` 的 `should_stop_after_round` 机制（与 teammate 的 `idle` 工具同一套模式）。

- 模型调用 `submit_summary(summary="...")` → 循环结束，`summary` 字段成为返回给 lead 的摘要。
- **纯文本轮（没有任何工具调用）不是完成**。循环会注入一条提醒用户消息并继续，直到模型要么调用工具继续工作、要么正式调用 `submit_summary`。

这样设计是为了堵住"过早退出"的漏洞：旧逻辑把"本轮无工具调用"当作完成信号，导致模型在第 1 轮凭空吐一段总结、或在没做完工作时输出澄清/计划文本就被当作 `completed`。现在完成是一个**显式动作**，子代理不可能"无意中"结束。

> 这也补齐了 lead 主循环早有的"空响应/退化轮修复"保护（`agent.py` 的 `EMPTY_ASSISTANT_RESPONSE_REPAIR_TEXT`），之前 subagent 循环没有对称的保护。

---

## 权限控制

在 `PermissionManager._authorize_subagent_call()` 中：

| 执行模式 | Explore 模式 | general-purpose 模式 |
|----------|-------------|---------------------|
| `accept_edits` / `yolo` | ✅ 允许 | ✅ 允许 |
| `shortcuts` / `plan` | ❌ 阻断（需 `request_authorization`） | ❌ 阻断（需切换模式或用 Explore） |

阻断时返回友好提示，引导用户使用 Explore 模式或提升执行模式。

---

## 执行流程

```python
def run_subagent(self, prompt: str, agent_type: str = "Explore") -> str:
    # 1. 构建独立工具注册表
    registry = self._build_registry(agent_type)
    
    # 2. 构建独立系统提示
    system_prompt = (
        f"You are an isolated subagent working in {workspace_root}. "
        "Keep the main context clean. Do the work, then return a concise summary."
    )
    
    # 3. 初始消息
    messages = [make_user_text_message(prompt)]
    pending_tool_repair_hints = []

    # 4. 执行 Agent Loop（完成只能由 submit_summary 触发）
    while rounds_used < max_rounds:
        result = runner.run_round(system_prompt, messages, registry, ...)
        rounds_used += 1

        if result.stop_after_round:      # 模型调用了 submit_summary
            return SubagentResult(status="completed",
                                  summary=submit_summary 捕获的 summary)

        if not result.has_tool_calls:    # 纯文本轮 ≠ 完成
            messages.append(make_user_text_message(SUBAGENT_NO_TOOL_NUDGE))
            continue                      # 注入提醒后继续

    return SubagentResult(status="truncated", summary=final_text)
```

---

## 特点

| 特性 | 说明 |
|------|------|
| **隔离上下文** | 子代理有独立的消息列表，不影响主会话 |
| **独立工具集** | 根据 agent_type 配置不同的可用工具 |
| **无会话状态** | `session=None`，不维护 todo、undo 等状态 |
| **最大轮数限制** | 由 `settings.runtime.max_subagent_rounds` 控制 |
| **返回摘要** | 最终只返回文本摘要到主会话 |
| **权限隔离** | 子代理调用不受主会话权限直接约束，但受执行模式限制 |

---

## 工具错误与自修复

Subagent 与主 Agent 共用同一套工具错误协议：

- 工具失败统一收敛为结构化错误外壳，而不是裸 `KeyError` 或 `"Error: ..."`
- 只有 `missing_required_params`、`invalid_arguments` 这类可自修复错误才会生成 `repair_hint`
- `repair_hint` 不直接塞进当前轮 `tool_result`
- 下一轮开始前，Runner 会把累计的提示渲染为一次性的 `<tool-repair-hints>` 用户消息注入到子代理消息流
- 注入完成后立即清空，不会在后续轮次反复重复

子代理内部消息历史中保留的是**去掉 `repair_hint` 的精简结构化错误**。因此即使一次性提示已经消费完，后续轮次仍然能看到简单错误信息，而不是完全丢失上下文。

---

## 相关代码

- `open_somnia/runtime/subagent_runner.py` — `SubagentRunner`
- `open_somnia/tools/subagent.py` — `register_subagent_tool()`
- `open_somnia/runtime/permissions.py` — `_authorize_subagent_call()`
- `open_somnia/runtime/agent.py` — `run_subagent()` 入口
- `open_somnia/tools/tool_errors.py` — 统一错误外壳、修复提示提取与渲染
