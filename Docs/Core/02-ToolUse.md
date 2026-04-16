# Tool Use

## 概述

Somnia 的工具系统采用 **ToolRegistry** 架构，所有工具通过统一的注册表进行注册、权限检查、执行和结果渲染。

---

## 核心架构

```
ToolRegistry
├── _tools: dict[str, ToolDefinition]
├── register(tool: ToolDefinition)
├── schemas() -> list[dict]          # 生成发送给模型的 tool schema
└── execute(ctx, name, payload)       # 执行工具（含权限检查）
```

### ToolDefinition

```python
@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler  # Callable[[ctx, payload], Any]
```

### ToolExecutionContext

```python
@dataclass(slots=True)
class ToolExecutionContext:
    runtime: OpenAgentRuntime   # 运行时引用
    session: AgentSession | None  # 当前会话
    actor: str                   # 调用者（lead / subagent / teammate名）
    trace_id: str                # 追踪 ID
```

---

## 工具分类

### Lead 工具（主 Agent 可用）

注册于 `_register_core_tools()`：

| 类别 | 工具 | 说明 |
|------|------|------|
| 文件系统 | `project_scan` | 项目结构扫描 |
| | `tree` | 目录树渲染 |
| | `find_symbol` | 符号查找 |
| | `glob` | 文件模式匹配 |
| | `grep` | 内容搜索 |
| | `read_file` | 读取文件 |
| | `write_file` | 写入文件 |
| | `edit_file` | 替换文件文本 |
| Shell | `bash` | 执行 shell 命令 |
| Todo | `TodoWrite` | 更新待办清单 |
| 任务 | `task_create` / `task_get` / `task_update` / `task_list` / `claim_task` | 持久任务管理 |
| 子代理 | `subagent` | 隔离子代理执行 |
| 后台 | `background_run` / `check_background` | 后台任务管理 |
| 团队 | `spawn_teammate` / `list_teammates` / `send_message` / `read_inbox` / `broadcast` / `shutdown_request` / `plan_approval` | Agent 团队协作 |
| 本地 | `load_skill` / `request_authorization` / `request_mode_switch` / `compress` / `request_original_context` | 运行时本地工具 |
| MCP | `mcp__{server}__{tool}` | MCP 远程工具 |

### Worker 工具（子 Agent / Teammate 可用）

注册于 `register_worker_tools()`：

| 工具 | 说明 |
|------|------|
| `bash` | Shell 命令 |
| `read_file` / `write_file` / `edit_file` | 文件操作 |
| `task_create` / `task_get` / `task_update` / `task_list` / `claim_task` | 任务管理 |
| `send_message` / `idle` / `submit_plan` | 本地协作工具 |

### 差异

Worker 工具**不包含**：子代理、后台任务、团队管理、MCP、技能加载、权限请求等高级能力。

---

## 文件编辑工具约定

`edit_file` 现在只支持批量编辑格式，即使只改一处也必须传 `edits`：

```json
{
  "path": "open_somnia/runtime/tool_events.py",
  "edits": [
    {
      "old_text": "return normalized",
      "new_text": "return display_name"
    }
  ]
}
```

约束：

- 顶层不再接受 `old_text` / `new_text`
- `edits` 必须是非空数组
- 每个 edit 项必须包含 `old_text` 和 `new_text`
- 每个 edit 项可单独带 `path`；未提供时回退到顶层 `path`

这样可以统一模型的调用心智，避免单编辑和多编辑两套 schema 混用。

---

## 工具执行流程

```
模型返回 tool_calls
        │
        ▼
┌─────────────────────────────┐
│  ToolRegistry.execute()     │
│                             │
│  1. 查找工具定义            │
│  2. 调用 authorize_tool_call│ ← PermissionManager
│     - workspace 授权检查     │
│     - 一次性授权检查         │
│     - 执行模式限制          │
│     - subagent 特殊规则     │
│                             │
│  3. 被阻断 → 返回阻断消息   │
│  4. 通过 → 执行 handler     │
└──────────────┬──────────────┘
               ▼
        返回结果 → 记录到 ToolLogStore
```

---

## 工具结果渲染

`ToolEventRenderer` 负责将工具调用和结果渲染为人类可读的输出：

- **工具名称前缀**：`● tool_name`
- **输入预览**：关键参数摘要
- **输出渲染**：支持 ANSI 彩色输出
- **文件变更摘要**：每轮文件修改的行数统计
- **工具日志**：每次调用持久化到 `.open_somnia/logs/tool_logs/`

---

## 静默工具

以下工具的执行结果不会打印到终端（`SILENT_TOOL_NAMES`）：

- `TodoWrite`

---

## 工具日志

所有工具调用都通过 `ToolLogStore` 持久化：

```
.open_somnia/logs/tool_logs/
├── {log_id}.json    # 每次工具调用的完整记录
```

支持通过 `request_original_context(log_id)` 恢复已压缩的原始工具输出。

---

## 相关代码

- `open_somnia/tools/registry.py` — `ToolRegistry`, `ToolDefinition`
- `open_somnia/tools/filesystem.py` — 文件系统工具
- `open_somnia/tools/shell.py` — Shell 工具
- `open_somnia/tools/todo.py` — TodoWrite 工具
- `open_somnia/tools/tasks.py` — 任务工具
- `open_somnia/tools/subagent.py` — 子代理工具
- `open_somnia/tools/background.py` — 后台任务工具
- `open_somnia/tools/team.py` — 团队工具
- `open_somnia/tools/mcp.py` — MCP 工具注册
- `open_somnia/runtime/events.py` — `ToolExecutionContext`
- `open_somnia/runtime/tool_events.py` — `ToolEventRenderer`
- `open_somnia/storage/tool_logs.py` — `ToolLogStore`
