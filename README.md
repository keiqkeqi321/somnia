<p align="center">
  <img src="./desktop/ui/src-tauri/icons/128x128.png" alt="Somnia Desktop icon" width="96" height="96">
</p>

<h1 align="center">Somnia</h1>

> 面向开发者的本地 AI Agent 运行时：终端、桌面端与可组合的 Agent 工作流，覆盖工具调用、持久会话、MCP 集成、任务管理、Hooks、Skills 与多 Agent 协作。

<p align="center">
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://pypi.org/project/somnia/"><img src="https://img.shields.io/badge/package-somnia-black" alt="Package"></a>
</p>

Somnia 是一个本地运行的 AI Agent 运行时。它把模型调用、工具权限、MCP Server、持久化会话、后台任务、Hooks、Skills 和多 Agent 协作统一在同一个 runtime 之下，既能在终端里作为开发助手使用，也能通过桌面端以图形界面操作。所有状态默认保存在本地工作区，适合跨多轮持续推进复杂任务。

## 为什么是 Somnia

Somnia 的核心特点是**上下文自治**：Agent 不只是逐轮调用工具，还会持续管理自己的工作记忆，让长任务始终保持连贯、关键信息不被噪音淹没。

**Context Janitor · 语义化三态裁剪**

Janitor 不是简单截断或粗暴压缩，而是对每条历史工具结果做语义化三态判定：

- **original（保留）**：与当前任务强相关，原样保留。
- **condensed（摘要）**：仍有参考价值但不是焦点，压缩成一句事实摘要 + `log_id`。
- **evicted（驱逐）**：已与当前任务无关，从 payload 移除，仅留 `log_id`。

**以当前话题为锚**：Janitor 从最近对话中抽取活跃文件、活跃符号和关键词作为「当前关注点」，据此判断每条旧结果的相关性；同一个文件的多次读写只保留最新快照，避免陈旧内容污染上下文。

**Todo 作为弱信号**：进行中的 todo 会提升相关结果的优先级，已完成的 todo 在话题无关时反而降权——上下文随任务推进自动演化。

**可恢复驱逐**：被驱逐的结果不是丢失，Agent 可用 `request_original_context` 工具按 `log_id` 随时取回原文。

**低收益熔断**：当连续自动运行的压缩率过低，Janitor 会自我禁用，避免无意义消耗。

**Context Compact · 兜底压缩**：当 Janitor 的日常治理不足以支撑时，Compact 对整体会话历史做摘要压缩，作为上下文膨胀的最后一道防线。

**会话、任务、协作消息全部持久化**到本地工作区，随时可用 `-r` 恢复并继续推进。

**统一 runtime**：工具运行时、MCP、Hooks、Skills、Subagent、Agent Team 统一在同一套权限和 runtime 下工作，无需拼接多个外部进程。

**CLI 与桌面端**两端的会话、配置、任务和日志共享同一份本地数据。

## 快速开始

### 1. 安装

```bash
pip install somnia
```

### 2. 配置 Provider

首次运行时，如果还没有可用 Provider，Somnia 会启动交互式配置流程：

```bash
somnia
```

也可以随时打开 Provider 管理：

```bash
somnia providers
```

全局配置默认写入：

```text
~/.open_somnia/open_somnia.toml
```

工作区级配置位于：

```text
<workspace>/.open_somnia/open_somnia.toml
```

### 3. 启动交互式会话

```bash
somnia
```

指定工作区：

```bash
somnia --workspace /path/to/project
```

继续最近会话或从会话列表恢复：

```bash
somnia -c
somnia -r
```

### 4. 执行一次性任务

```bash
somnia run "总结这个仓库的模块结构，并指出主要入口"
```

## 功能一览

| 能力 | 说明 |
| --- | --- |
| 交互式 REPL | 面向开发任务的终端会话，支持历史恢复和快捷命令 |
| Somnia Desktop | Tauri + React 桌面端，复用同一 runtime，提供图形化会话与状态管理 |
| Provider 管理 | 支持 Anthropic 与 OpenAI-compatible Provider，多模型切换 |
| 工具运行时 | 文件读写、Shell、Todo、任务、后台作业、工具日志等内置工具 |
| MCP 集成 | 支持 `stdio` / `http` MCP Server，并在 REPL 中查看连接状态 |
| 持久化状态 | 会话、transcript、任务、inbox、team、job、log 写入 `.open_somnia/` |
| 执行模式 | 从只读、计划模式到接受编辑和全自动模式，按风险分层 |
| Context Compact | 长会话上下文压缩，降低上下文膨胀对连续工作的影响 |
| Context Janitor | 语义化上下文治理，按当前目标裁剪低价值工具输出，延缓完整压缩 |
| Skills | 按任务加载专门工作流，可使用用户级、项目级和内置技能 |
| Hooks | 在 Agent 事件前后运行本地命令，适合集成通知、审计和自动化 |
| Agent Team | teammate、inbox、message bus 与子任务协作原语 |

## Somnia Desktop

Somnia Desktop 是 Somnia 的桌面端应用，安装即用。它不是一套独立运行时，而是复用 CLI 同一套 runtime、Provider 配置、会话存储、MCP、Hooks、任务和工具日志，只是以图形界面呈现，无需手动配置后端。

桌面端适合需要图形界面的工作流：

- 多项目和多会话管理。
- 聊天、工具调用、后台运行状态和 runtime event 的可视化。
- Provider、模型、vision model、reasoning level 的界面化切换。
- 工作区级配置编辑，包括 Provider、runtime、MCP、Hooks 和 system prompt。
- 会话压缩、janitor、归档会话、工具日志、team/task 状态查看。

## CLI 用法

```bash
somnia [--workspace PATH] [--provider NAME] [--model MODEL]
somnia chat
somnia run "prompt"
somnia tasks list
somnia tasks get <task_id>
somnia compact
somnia doctor
somnia providers
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--workspace PATH` | 指定 Agent 工作区，默认当前目录 |
| `--provider NAME` | 本次调用临时覆盖默认 Provider |
| `--model MODEL` | 本次调用临时覆盖默认模型 |
| `-c`, `--continue` | 继续当前工作区最近会话 |
| `-r`, `--resume` | 打开会话选择器并恢复历史会话 |
| `--version` | 输出 Somnia 版本 |

## REPL 快捷命令

进入 `somnia` 交互式会话后可使用：

| 命令 | 说明 |
| --- | --- |
| `/scan` | 扫描当前仓库或子目录 |
| `/symbols` | 查找符号并预览源码位置 |
| `/compact` | 手动压缩当前会话上下文 |
| `/janitor` | 运行语义化上下文治理，清理低价值工具结果 |
| `/model` | 查看或切换当前模型 |
| `/reasoning` | 调整 reasoning 偏好 |
| `/vision` | 设置图像输入模型 |
| `/providers` | 查看或管理 Provider |
| `/tasks` | 查看持久任务 |
| `/team` | 查看 teammate 状态与日志 |
| `/inbox` | 查看协作消息 |
| `/mcp` | 查看 MCP Server 和工具状态 |
| `/hooks` | 查看或切换 Hooks |
| `/toollog` | 查看最近工具调用日志 |
| `/bg` | 查看后台任务 |
| `/undo` | 撤销最近一轮变更 |
| `/checkpoint` | 创建会话检查点 |
| `/rollback` | 回滚到检查点 |
| `/skills` | 查看可用 Skills |
| `/help` | 查看帮助 |
| `/exit` | 退出会话 |

## 配置示例

Somnia 会先加载全局配置，再加载工作区配置；工作区配置可以覆盖项目相关行为。

```toml
[providers]
default = "openai"

[providers.openai]
provider_type = "openai"
models = ["gpt-4.1", "gpt-4.1-mini"]
default_model = "gpt-4.1"
api_key = "${OPENAI_API_KEY}"
base_url = "https://api.openai.com/v1"
max_tokens = 8000
timeout_seconds = 120

[runtime]
command_timeout_seconds = 120
max_tool_output_chars = 50000
max_agent_rounds = 100
```

Anthropic 示例：

```toml
[providers]
default = "anthropic"

[providers.anthropic]
provider_type = "anthropic"
models = ["claude-sonnet-4-5"]
default_model = "claude-sonnet-4-5"
api_key = "${ANTHROPIC_API_KEY}"
base_url = "https://api.anthropic.com"
```

## MCP Server

Somnia 支持 table form 和 legacy array form。新配置推荐 table form。

`stdio` 示例：

```toml
[mcp_servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
cwd = "."
enabled = true
startup_timeout_sec = 30
timeout_seconds = 30

[mcp_servers.filesystem.env]
DEBUG = "1"
```

`http` 示例：

```toml
[mcp_servers.remote]
transport = "http"
url = "https://example.com/mcp"
enabled = true
startup_timeout_sec = 30
timeout_seconds = 30

[mcp_servers.remote.http_headers]
Authorization = "Bearer ${TOKEN}"
```

启动 REPL 后可用 `/mcp` 查看连接状态和已注册工具。

## 执行模式

Somnia 的交互式 REPL 将执行权限按风险分层，`Shift+Tab` 可循环切换：

| 模式 | 说明 |
| --- | --- |
| `? shortcuts` | 只读工作区访问 |
| `plan mode` | 只读 + 先规划 |
| `accept edits` | 允许文件编辑、任务变更和协作操作 |
| `Yolo` | 全自动执行 |

需要越权的工具会触发授权请求；选择允许工作区后，权限会写入：

```text
.open_somnia/permissions.json
```

## 数据目录

Somnia 的工作区状态默认保存在：

```text
.open_somnia/
```

常见子目录：

| 路径 | 内容 |
| --- | --- |
| `sessions/` | 会话状态 |
| `transcripts/` | 对话 transcript |
| `tasks/` | 持久任务 |
| `inbox/` | 协作消息 |
| `team/` | teammate 状态与日志 |
| `jobs/` | 后台任务 |
| `logs/` | runtime 和工具日志 |
| `permissions.json` | 工作区工具授权 |

## 文档

更多专题文档在 `Docs/` 目录：

- [Core 文档索引](./Docs/Core/README.md)
- [Agent Loop](./Docs/Core/01-AgentLoop.md)
- [Tool Use](./Docs/Core/02-ToolUse.md)
- [TodoWrite](./Docs/Core/03-TodoWrite.md)
- [探索能力](./Docs/Core/04-探索能力.md)
- [上下文治理与压缩](./Docs/Core/06-上下文治理与压缩.md)
- [Subagent](./Docs/Core/07-Subagent.md)
- [Skills](./Docs/Core/08-Skills.md)
- [MCP](./Docs/Core/09-MCP.md)
- [权限系统与执行模式](./Docs/Core/10-权限系统与执行模式.md)
- [任务系统](./Docs/Core/11-任务系统.md)
- [Agent Team](./Docs/Core/13-AgentTeam.md)
- [Hooks](./Docs/Core/14-Hooks.md)
- [运维文档索引](./Docs/运维/README.md)
- [发版流程](./Docs/运维/03-发版流程.md)
- [Docker 部署](./Docs/运维/07-Docker部署.md)

## 状态与许可

Somnia 当前处于 Alpha 阶段，公开 API 和存储结构仍可能变化。

本项目使用 [MIT License](./LICENSE)。
