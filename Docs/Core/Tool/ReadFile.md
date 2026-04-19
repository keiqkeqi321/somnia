# ReadFile

## 概述

`read_file` 是 Somnia 的工作区文件读取工具，适合在 `glob`、`grep`、`find_symbol` 之后做**局部证据读取**。

这次行为更新后，它不再只支持“从文件头开始按 `limit` 截取”，而是支持按行范围读取，并且在输出被字符上限裁切时给出显式提示。

---

## 输入参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `path` | 是 | 工作区内文件路径 |
| `start_line` | 否 | 起始行号，**1-based**，默认 `1` |
| `end_line` | 否 | 结束行号，**1-based 且包含该行** |
| `limit` | 否 | 从 `start_line` 开始最多返回多少行 |

规则：

- `start_line`、`end_line`、`limit` 都必须是 `>= 1` 的整数
- 同时提供 `end_line` 和 `limit` 时，优先使用 `end_line`
- `start_line` 超过文件结尾会直接返回错误
- 如果路径不存在但附近只有一个同名候选文件，会自动解析并在输出前加一条提示

---

## 输出行为

### 1. 范围读取

当读取的不是整文件时，工具会明确标出被省略的前后文：

```text
... (2449 lines omitted before line 2450)
<selected lines>
... (87 more lines after line 2560)
```

这能让模型知道自己拿到的是**局部片段**，而不是完整文件。

### 2. 显式截断提示

`read_file` 的最终输出仍然会受 `runtime.max_tool_output_chars` 限制，但现在不再静默硬截断。  
当结果过长时，尾部会附带类似提示：

```text
... [read_file output truncated at 50000 chars; use start_line/end_line to narrow the range]
```

看到这条提示，应该继续缩小读取范围，而不是假设当前结果就是完整文件。

### 3. Active Working File Cache

`read_file` 会继续把**完整文件内容**写入 runtime 的 active working file cache，同时把本次返回片段作为 snippet。  
这能帮助系统提示告诉模型：“当前正围绕哪个文件工作”，减少对同一区域的重复读取。

---

## 推荐用法

### 常规流程

1. 用 `project_scan` / `tree` 建立目录地图
2. 用 `find_symbol`、`grep` 或报错栈定位文件和行号
3. 用 `read_file` 只读必要范围

### 示例

读取文件头：

```json
{
  "path": "open_somnia/runtime/agent.py",
  "limit": 120
}
```

读取中段局部：

```json
{
  "path": "open_somnia/runtime/agent.py",
  "start_line": 2440,
  "end_line": 2515
}
```

按起始行加窗口读取：

```json
{
  "path": "open_somnia/runtime/agent.py",
  "start_line": 2440,
  "limit": 80
}
```

---

## 与上下文治理的关系

- `read_file` 的 tool result 仍会进入会话历史
- 发送给模型前，payload 构建会移除 `raw_output` / `log_id`
- 对于**较大的完全重复 `read_file` 结果**，payload 构建会折叠旧副本，只保留最新完整副本，旧副本会被替换成：

```text
[Duplicate tool result omitted | read_file] Identical output appears later.
```

- 如果**最新一轮 tool result 里出现了 `read_file`**，payload 构建还会只针对这一轮读过的路径，逆向裁剪更早的同文件重叠区间：

```text
[Overlapping read_file result omitted | demo.txt:3-8] Covered by later read(s) of the same file.
```

- 完全覆盖时会替换成上面的占位
- 部分重叠时只移除重叠行，保留前后独有片段，并插入显式 overlap marker
- 如果最新一轮没有 `read_file`，这一步会完全跳过

这只是 payload 级别的去重与重叠抑制，不会改写原始 `session.messages`。

换句话说，去重能减少重复上下文污染，但它不是鼓励整文件反复重读的替代品。正确做法仍然是优先使用范围读取。

---

## 相关代码

- `open_somnia/tools/filesystem.py` — `read_file`
- `open_somnia/runtime/compact.py` — `build_payload_messages`
- `open_somnia/runtime/system_prompt.py` — active working file cache 注入
- `tests/test_filesystem_tool.py`
- `tests/test_runtime_tool_output.py`
