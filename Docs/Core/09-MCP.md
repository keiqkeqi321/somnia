# MCP (Model Context Protocol)

## 概述

MCP 是 Somnia 与外部工具服务器集成的标准协议实现。通过 MCP，Agent 可以远程调用外部服务提供的工具，扩展自身能力。

---

## 架构

```
Somnia Agent
    │
    └── MCPRegistry
            │
            ├── MCPClient (server A)
            │       │
            │       └── Transport (http / stdio)
            │               │
            │               └── 外部 MCP Server
            │
            └── MCPClient (server B)
                    │
                    └── Transport (http / stdio)
                            │
                            └── 外部 MCP Server
```

---

## 传输协议

### HTTP 传输 (`StreamableHTTPTransport`)

适用场景：远程 MCP 服务

配置项：
| 参数 | 说明 |
|------|------|
| `url` | HTTP 端点地址（必填） |
| `http_headers` | 自定义请求头 |
| `timeout_seconds` | 请求超时 |
| `startup_timeout_seconds` | 启动超时 |

### Stdio 传输 (`StdioTransport`)

适用场景：本地 MCP 服务

配置项：
| 参数 | 说明 |
|------|------|
| `command` | 启动命令（必填） |
| `args` | 命令行参数 |
| `cwd` | 工作目录 |
| `env` | 环境变量 |
| `timeout_seconds` | 超时时间 |

---

## MCP 协议流程

```
1. initialize
   → { protocolVersion, capabilities, clientInfo }
   ← { protocolVersion, capabilities, serverInfo }

2. notifications/initialized
   → 通知服务端客户端已就绪

3. tools/list
   → 获取可用工具列表

4. tools/call
   → { name, arguments }
   ← { content: [{ type: "text", text: "..." }], isError }
```

---

## 工具注册

MCP 工具自动注册到主 ToolRegistry，命名格式：

```
mcp__{server_name}__{tool_name}
```

例如：
```
mcp__github__create_issue
mcp__database__query_users
```

工具描述自动拼接：
```
MCP tool 'create_issue' from server 'github'. {原始描述}
```

---

## 配置方式

在 `open_somnia.toml` 中配置 MCP 服务器：

```toml
[[mcp_servers]]
name = "github"
enabled = true
transport = "http"
url = "http://localhost:3000/mcp"
timeout_seconds = 30

[[mcp_servers]]
name = "filesystem"
enabled = true
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

### 工具级启停（include/exclude）

每个 server 支持可选的工具子集清单（两种 TOML 写法均可）：

```toml
[mcp_servers.playwright]
url = "http://localhost:8931/mcp"
include_tools = ["browser_navigate", "browser_snapshot", "browser_click"]
# 或：exclude_tools = ["browser_run_code_unsafe"]
```

- 两个键都缺省 = 全部启用；同时存在时 exclude 优先。
- `include_tools = []`（显式空列表）= 该 server 工具全部禁用，与键缺失语义不同。
- 被禁用的工具不注册进 ToolRegistry（不进 tools 数组），但仍在管理界面可见。

管理入口（改动即时生效并回写 TOML，行级 upsert 保注释）：

- **CLI**：`/mcp` → 选 server → 选工具 → Enable/Disable。
- **Desktop**：Settings → MCP → 展开 server → 每个工具的开关。
- **API**：`POST /mcp/servers/{name}/tools/{tool}/enabled`（remote RPC：`mcp.set_tool_enabled`）。

---

## 状态管理

### 服务器状态

| 状态 | 说明 |
|------|------|
| `connected` | 成功连接并获取工具列表 |
| `disabled` | 服务器未启用 |
| `error` | 连接失败 |

### 状态查询

```
MCPRegistry.status_lines()
→ [
    "github: connected [http] http://localhost:3000/mcp tools=12",
    "filesystem: error - command not found",
    "disabled_server: disabled [stdio] (unconfigured)"
  ]
```

---

## 错误处理

- **连接失败**：记录到 `self.errors`，不影响其他服务器
- **工具调用错误**：返回 `Error: {错误文本}`
- **结果渲染**：自动提取 MCP 响应中的 `content` 数组文本

---

## 生命周期

- **初始化**：`MCPClient.initialize()` 在首次 `list_tools` 或 `call_tool` 时自动调用
- **关闭**：`MCPRegistry.close()` 关闭所有 MCP 客户端连接

---

## 相关代码

- `open_somnia/mcp/registry.py` — `MCPRegistry`
- `open_somnia/mcp/client.py` — `MCPClient`
- `open_somnia/mcp/transport_http.py` — `StreamableHTTPTransport`
- `open_somnia/mcp/transport_stdio.py` — `StdioTransport`
- `open_somnia/tools/mcp.py` — `register_mcp_tools()`
- `open_somnia/config/models.py` — `MCPServerSettings`
