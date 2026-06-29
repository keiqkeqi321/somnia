# Trace Viewer

Somnia Trace Viewer 是一个离线 HTML 对话分析工具，用来排查 provider payload、prompt cache 命中率和相邻请求的前缀稳定性。

## 开启采集

Trace Viewer 读取 runtime 已经支持的 provider payload debug dump：

```bash
somnia trace
```

生成的 JSON 位于：

```text
.open_somnia/logs/provider_payloads/
```

这些 JSON 是实际发送给 provider 前后的调试快照，包含 messages、tools、system prompt sections、context usage、provider request/response、latency 和 usage。

## 生成报告

```bash
somnia trace-viewer
```

默认输出：

```text
.open_somnia/logs/provider_payloads/trace-viewer.html
```

生成后会自动打开浏览器。

只分析一个会话：

```bash
somnia trace-viewer --session <session-id>
```

只看最新 N 条：

```bash
somnia trace-viewer --limit 20
```

指定输出路径：

```bash
somnia trace-viewer --output .open_somnia/logs/provider_payloads/report.html
```

## 报告内容

- 总览：trace 数、session 数、总 input/output/cache read/cache creation token。
- 请求表：每次 provider 请求的时间、会话、provider、model、messages、system prompt、tools、cache hit、context usage、latency 和错误状态。
- 相邻 diff：同一 session 内相邻 payload 的 common message prefix、first diff index、system/tools/model 是否变化。
- 详情面板：消息预览、provider request、provider response、system prompt sections。
- 风险提示：`system changed`、`tools changed`、`message prefix changed at start`、`early message prefix changed`、`transient/runtime notice present` 等。

## 缓存排查重点

OpenAI 兼容链路主要依赖服务端自动前缀缓存，所以重点看相邻请求的稳定前缀：

- `First diff = 0` 通常意味着 message 前缀从第一条就变了，缓存命中风险最高。
- `transient/runtime notice present` 表示本次 payload 中有临时提示，应该确认它是否只出现在尾部。
- `system changed` 或 `tools changed` 表示请求前缀的更早部分变化，会显著影响缓存命中。
- cache hit ratio 使用 `cache_read_input_tokens / (input_tokens + cache_read_input_tokens)`，兼容 provider 将 cached tokens 单独统计的 usage 格式。

Anthropic 链路还可以结合 `cache_creation_input_tokens` 看本轮是否创建了新缓存；OpenAI 链路通常只暴露 cached/read tokens。
