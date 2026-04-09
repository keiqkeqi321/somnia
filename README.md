# Somnia

Somnia 是一个面向开发者的 AI Agent CLI 工具。名字来源于拉丁语 **somnia**（梦），寓意把复杂的软件任务拆解、协作并自动执行。

> 发布名：`somnia`  
> 内部 Python 模块名：`open_somnia`

## Somnia 是什么

Somnia 提供一个可在终端中运行的智能体工作流，你可以用它：

- 进行交互式对话
- 让 Agent 执行带工具的开发任务
- 管理任务、上下文与协作流程
- 在工作目录内持续工作
- 通过多 Agent 方式拆解复杂问题

它适合用于：

- 代码分析与修改
- 命令行辅助开发
- 多步骤任务执行
- 本地项目自动化处理
- 团队式协作执行复杂目标

## 核心功能

### 1. 任务与 Todo 管理

Somnia 内置任务拆解能力，适合把复杂工作拆成多个可执行步骤。

- 支持短期 Todo 跟踪
- 支持持久化任务管理
- 适合逐步推进长链路工作
- 可与多 Agent 协作结合使用

### 2. 基本工具能力

Somnia 可以在工作区中调用常见工具完成开发任务，例如：

- 读取和编辑文件
- 搜索文件与内容
- 执行命令行操作
- 写入脚本和配置
- 跟踪变更并辅助调试

### 3. 子智能体（Subagent）

Somnia 支持启动子智能体，把某个子问题单独拆出去探索或实现。

适合场景：

- 先让子 Agent 调研一个问题
- 并行探索多个方案
- 把主流程和子任务隔离开

### 4. Skill 机制

Somnia 支持按需加载技能（skill），用于扩展特定任务的处理能力。

适合场景：

- 某类问题需要专门知识
- 某些任务需要固定操作套路
- 让 Agent 在不同任务中切换能力模式

### 5. MCP 工具集成

Somnia 支持 MCP（Model Context Protocol）工具接入，可把外部工具能力纳入同一工作流。

例如：

- 接入外部服务工具
- 统一通过 Agent 访问 MCP tool
- 让 CLI 具备更强扩展性

#### `stdio` MCP 配置示例

Somnia 同时支持 `http` 和 `stdio` 两种 MCP 传输方式。  
如果你要接本地进程型 MCP Server，推荐使用 `stdio`。

工作区配置文件：

```toml
[mcp_servers.minimal]
transport = "stdio"
command = "python"
args = ["-m", "open_somnia.mcp.minimal_stdio_server"]
enabled = true
startup_timeout_sec = 10
timeout_seconds = 30
```

也可以显式指定工作目录和环境变量：

```toml
[mcp_servers.my_stdio_server]
transport = "stdio"
command = "C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python312\\python.exe"
args = ["D:\\tools\\my_mcp_server.py"]
cwd = "D:\\tools"
enabled = true
startup_timeout_sec = 20
timeout_seconds = 30
env = { SystemRoot = "C:\\WINDOWS", TMP = "D:\\temp", TEMP = "D:\\temp" }
```

字段说明：

- `transport = "stdio"`：声明这是子进程标准输入输出通信，不是 HTTP。
- `command`：要启动的可执行程序。
- `args`：传给 `command` 的参数列表，必须写成 TOML 数组。
- `cwd`：可选，子进程启动目录；相对路径按当前工作区解析。
- `env`：可选，只覆盖你指定的环境变量，其余系统环境变量会保留。
- `startup_timeout_sec`：初始化阶段等待时长。
- `timeout_seconds`：普通 MCP 请求超时。
- `enabled`：是否启用该 server。

注意：

- `stdio` server 不需要 `url`。
- 如果全局配置里某个同名 MCP server 之前是 `http`，而工作区把它覆盖成了 `stdio`，Somnia 会忽略残留的 `url`，避免把旧 HTTP 配置混进来。
- 可执行文件路径里有空格时，不要手动加引号，直接把完整路径写进 `command`。
- 启动后可在 REPL 中执行 `/mcp` 查看连接状态和已注册工具。

### 6. 上下文压缩（Context Compact）

长会话会不断累积上下文，Somnia 内置压缩机制，帮助在长时间运行时保持可持续性。

- 适合长链路任务
- 降低上下文膨胀
- 保持任务连续性

### 7. 任务系统与 Agent Team

Somnia 不只是单 Agent CLI，还支持 Agent Team 协作模式。

- 支持持久任务
- 支持消息收发
- 支持 teammate / inbox / 协作执行
- 适合复杂任务拆分、并行推进、回收结果

## 核心命令

### 常用 CLI 命令

```bash
somnia
somnia chat "你好"
somnia run "总结这个仓库"
somnia doctor
somnia tasks
somnia compact
```

说明：

- `somnia`：启动交互式会话
- `somnia chat "..."`：执行一次性对话
- `somnia run "..."`：执行带目标的运行任务
- `somnia doctor`：检查环境与配置
- `somnia tasks`：查看任务相关信息
- `somnia compact`：执行上下文压缩

### REPL 内常用斜杠命令

进入交互式模式后，可使用：

```text
/scan
/symbols
/investigation
/compact
/model
/tasks
/team
/inbox
/mcp
/toollog
/bg
/help
/exit
```

这些命令分别用于：

- `/scan`：扫描当前仓库或子目录，并缓存项目摘要
- `/symbols`：查找符号并预览匹配位置附近源码
- `/investigation`：查看当前探索状态、事实与告警
- `/compact`：压缩当前上下文
- `/model`：查看或切换模型信息
- `/tasks`：查看任务状态
- `/team`：查看或操作协作团队
- `/inbox`：查看消息收件箱
- `/mcp`：查看 MCP 相关状态
- `/toollog`：查看工具调用记录
- `/bg`：查看后台任务
- `/help`：帮助
- `/exit`：退出会话

## 快速开始

### 方式一：pip 安装（推荐）

```bash
pip install somnia
```

安装后直接运行：

```bash
somnia
```

首次启动如果还没有配置 provider，Somnia 会弹出交互引导，先选择兼容模式 `anthropic` 或 `openai`，再填写 `provider_name`，并在同一页填写 `base_url`、`api_key` 和逗号分隔的模型 ID 列表，最后自动写入全局配置 `~/.open_somnia/open_somnia.toml`。

如果当前配置里已经没有任何带 `api_key` 的 provider，Somnia 会先清理残留的无效 `[providers]` 配置，再重新拉起这套引导流程。

常用命令：

```bash
somnia --help
somnia doctor
somnia chat "你好"
```

### 方式二：一键安装脚本

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/keiqkeqi321/learn-claude-code/OpenAgent_by_codex/OpenAgent/scripts/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/keiqkeqi321/learn-claude-code/OpenAgent_by_codex/OpenAgent/scripts/install.ps1 | iex
```

### 方式三：Docker

```bash
docker build -t somnia .
docker run -it somnia
```

## 常见使用方式

### 启动交互式会话

```bash
somnia
```

### 单次对话

```bash
somnia chat "帮我总结当前仓库结构"
```

### 执行任务

```bash
somnia run "分析项目并给出重构建议"
```

### 检查环境

```bash
somnia doctor
```

### 查看帮助

```bash
somnia --help
```

## 环境要求

| 方式 | 需要的环境 |
|------|-----------|
| `pip install somnia` | Python 3.11+ |
| `npx somnia` | Node.js 16+ + Python 3.11+ |
| 一键脚本 | curl / PowerShell |
| Docker | Docker |

如果还没有 Python：

- Windows：`winget install Python.Python.3.12`
- macOS：`brew install python@3.12`
- Ubuntu：`sudo apt update && sudo apt install python3.12`

## 常见问题

### 安装后命令找不到

可先尝试：

```bash
python -m open_somnia --help
```

如果这样能运行，说明是 Python Scripts 路径未加入 PATH。

### 如何升级

```bash
pip install --upgrade somnia
```

### 数据存在哪里

运行数据默认保存在当前工作区的：

```text
.open_somnia/
```

## 文档导航

技术实现、发版流程、CI/CD、PyPI、npm、Docker、版本管理等内容已拆分到 `Docs/`：

- [Docs/README.md](./Docs/README.md)
- [01-项目概述](./Docs/01-项目概述.md)
- [02-版本管理](./Docs/02-版本管理.md)
- [03-发版流程](./Docs/03-发版流程.md)
- [04-CI-CD配置](./Docs/04-CI-CD配置.md)
- [05-PyPI发布](./Docs/05-PyPI发布.md)
- [06-npm发布](./Docs/06-npm发布.md)
- [07-Docker部署](./Docs/07-Docker部署.md)
- [08-用户安装方式](./Docs/08-用户安装方式.md)
- [09-常见问题排查](./Docs/09-常见问题排查.md)
- [10-探索能力](./Docs/10-探索能力.md)
- [11-探索能力优化计划](./Docs/11-探索能力优化计划.md)
- [12-压缩策略](./Docs/12-压缩策略.md)

## 版本与发布

- PyPI：`https://pypi.org/project/somnia/`
- 当前命令：`somnia`
- 版本发布走 GitHub CI 自动化

---

如果你是普通用户，看完本 README 就可以开始使用。  
如果你是维护者或运维人员，请直接查看 `Docs/` 目录。
