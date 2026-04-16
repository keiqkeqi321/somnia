# 核心架构文档

本目录包含 Somnia 核心架构相关文档。

## 文档列表

| 编号 | 文档 | 说明 |
|------|------|------|
| 01 | [Agent Loop](./01-AgentLoop.md) | Agent 核心执行循环：提示构建、模型调用、工具执行、上下文治理 |
| 02 | [Tool Use](./02-ToolUse.md) | 工具系统架构：ToolRegistry、工具分类、执行流程、日志 |
| 03 | [TodoWrite](./03-TodoWrite.md) | 会话级待办清单：数据模型、状态、约束规则 |
| 04 | [探索能力](./04-探索能力.md) | 即时探索能力：`/scan`、`/symbols` 及其他探索工具 |
| 05 | [上下文回滚](./05-上下文回滚.md) | Checkpoint / Rollback 机制：会话状态检查点与回滚 |
| 06 | [上下文治理与压缩](./06-上下文治理与压缩.md) | Payload Normalization + Semantic Janitor + Auto Compact 三层上下文治理 |
| 07 | [Subagent](./07-Subagent.md) | 隔离子代理：Explore / general-purpose 模式、权限控制 |
| 08 | [Skills](./08-Skills.md) | 可扩展知识库：技能文件结构、搜索路径、加载流程 |
| 09 | [MCP](./09-MCP.md) | Model Context Protocol：传输协议、工具注册、配置 |
| 10 | [权限系统与执行模式](./10-权限系统与执行模式.md) | 四层执行模式 + 三层权限检查机制 |
| 11 | [任务系统](./11-任务系统.md) | 持久化任务管理：状态流转、依赖关系、自动认领 |
| 12 | [后台任务](./12-后台任务.md) | 异步任务执行：后台线程、通知机制、超时控制 |
| 13 | [Agent Team](./13-AgentTeam.md) | 多 Agent 协作：消息总线、计划审批、空间轮询、启动恢复 |
| 14 | [Hooks](./14-Hooks.md) | 运行时 Hook 系统：事件模型、配置协议、内置通知、workspace 对 global 的 managed Hook 覆盖、`/hooks` 开关管理 |

