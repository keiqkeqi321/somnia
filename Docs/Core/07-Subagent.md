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
| `bash` | Shell 命令 |
| `project_scan` | 项目扫描 |
| `tree` | 目录树 |
| `find_symbol` | 符号查找 |
| `glob` | 文件模式匹配 |
| `grep` | 内容搜索 |
| `read_file` | 文件读取 |
| `load_skill` | 技能加载 |

**禁止**：任何文件写入操作。

### general-purpose 模式

在 Explore 模式基础上，额外增加：

| 工具 | 说明 |
|------|------|
| `write_file` | 文件写入 |
| `edit_file` | 文本替换 |

`edit_file` 与主 Agent 保持同一约定：只接受 `edits=[{old_text,new_text}, ...]`，单次替换也必须包装成单元素数组。

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
    
    # 4. 执行 Agent Loop
    for _ in range(max_subagent_rounds):
        if pending_tool_repair_hints:
            messages.append(
                make_user_text_message(
                    render_transient_repair_hint_message(pending_tool_repair_hints)
                )
            )
            pending_tool_repair_hints = []
        turn = self.provider.complete(system_prompt, messages, registry.schemas())
        messages.append(turn.as_message())
        
        if not turn.has_tool_calls():
            # 无工具调用 → 返回文本摘要
            return turn.text or "(no summary)"
        
        # 执行工具调用
        for tool_call in turn.tool_calls:
            ctx = ToolExecutionContext(
                runtime=self.runtime,
                session=None,          # 子代理无会话
                actor="subagent",      # actor 标记为 subagent
                trace_id=f"subagent-{uuid}"
            )
            output = registry.execute(ctx, tool_call.name, tool_call.input)
            repair_hint = extract_transient_repair_hint(output)
            if repair_hint is not None:
                pending_tool_repair_hints.append(repair_hint)
            persisted_output = sanitize_tool_output_for_persistence(output)
            results.append(
                make_tool_result(content=serialize_tool_output(persisted_output), ...)
            )
        
        messages.append(make_tool_result_message(results))
    
    return final_text  # 达到最大轮数时返回最后文本
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
