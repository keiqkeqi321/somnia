# Provider 缓存命中优化

本文记录 Somnia Prompt Cache 策略。目标是把通用缓存命中优化尽量前置到 runtime 层，同时把 provider 私有协议字段留在对应 adapter 内处理。

## 设计原则

- Runtime 负责稳定通用请求前缀：system prompt 分层、工具 schema 顺序、消息 payload 的前置形状。
- Provider adapter 负责协议翻译：Anthropic 才生成 `cache_control`，OpenAI 兼容链路不注入未知字段。
- Usage 解析要保留 cache token 字段，便于后续 debug payload、CLI 或桌面端展示真实命中率。

## 已实现内容

### Runtime 前置优化

`open_somnia/runtime/agent.py` 在进入 provider 调用前做两件事：

- 对主 agent 的工具 schema 做 canonical sort，排序 key 为工具 `name` + schema 稳定 JSON 表示。
- Anthropic provider 调用前，通过 `cache_optimized_system_prompt()` 把已渲染的 `## A/B/C...` system prompt 还原成结构化 sections。

这样一来，MCP 或本地工具注册顺序变化不会轻易改变 tools 前缀；Anthropic 也不需要在 adapter 内猜 Somnia 的 prompt 分层。

### System Prompt 分层

`open_somnia/runtime/prompt_sections.py` 新增：

- `parse_rendered_prompt_sections(text)`
- `cache_optimized_system_prompt(system_prompt)`

当前规则：

- `A. Core System Prompt` 视为稳定层。
- Runtime Injection、Skill、MCP、Repo Prompt 等后续段视为动态层。
- OpenAI 链路继续使用纯字符串 system prompt。
- Anthropic 链路在 provider 调用前拿到结构化 section 列表。

### Anthropic Prompt Cache

`open_somnia/providers/anthropic_provider.py` 负责把通用 section 结构翻译为 Anthropic text blocks，并放置最多两个 breakpoint：

1. 第一个 breakpoint 放在最后一个稳定 system block 上。
2. 如果 system 为空，则第一个 breakpoint 退回放在最后一个 tool schema 上。
3. 第二个 breakpoint 放在最后一条非 transient 消息的最后一个 content block 上。

Runtime 生成的 `<runtime-notice>` 会带 `transient=true` 元数据。Anthropic adapter 只用它选择 cache breakpoint，发送给 provider 前会剥离该字段，避免动态提醒成为缓存断点。


生成字段为：

```json
{"cache_control": {"type": "ephemeral"}}
```

### OpenAI 兼容链路

OpenAI、DeepSeek、MiMo 等兼容链路主要依赖服务端自动前缀缓存，不支持 Anthropic 风格的显式 breakpoint。Somnia 对 OpenAI 链路的优化重点是：

- 保持 system/instructions 字节级稳定。
- 保持 tools 顺序稳定。
- 不回放不必要的 reasoning 内容。
- 不注入 `cache_control`，避免兼容接口因未知字段返回 400。
- 不再为 open todo 每轮注入 reminder；动态提醒只在必要事件上合并为尾部 `<runtime-notice>`。

### Cache Usage 诊断

Usage 解析已补充 cache token 字段：

- Anthropic: `cache_read_input_tokens`、`cache_creation_input_tokens`
- OpenAI Chat Completions: `prompt_tokens_details.cached_tokens`
- OpenAI Responses API: `input_tokens_details.cached_tokens`

这些字段会进入 Somnia 标准 usage dict，后续可以直接用于 hit rate 统计。

## 当前边界

- Anthropic 的 `cache_control` 是 provider 私有字段，不能在通用 payload 中无条件加入。
- OpenAI 兼容链路没有显式 breakpoint，缓存命中主要依赖稳定前缀、稀疏动态尾部和服务端自动匹配。
- 压缩或语义清理如果频繁重写较早历史，仍会破坏后续前缀命中。

## 验证

覆盖测试位于 `tests/test_runtime_tool_output.py`：

- Anthropic system/message/tool breakpoint 形状。
- Runtime 在 provider 调用前准备 Anthropic 结构化 system prompt。
- OpenAI 链路保持 system prompt 字符串。
- 主 agent tools schema canonical sort。
- Anthropic/OpenAI cache usage 字段解析。

关键回归命令：

```bash
python -m unittest tests.test_cli_resume tests.test_process_output tests.test_repl_todo tests.test_runtime_tool_output
```

## 后续方向

1. 在 provider payload dump 中展示 cache hit rate。
2. 为 DeepSeek、MiniMax、MiMo 等兼容厂商补充 cache usage 字段映射和价格统计。
3. 让压缩策略显式考虑“前缀稳定性”，尽量只改写固定摘要块。
4. 继续细分 system prompt 中稳定规则层和动态运行态层。
