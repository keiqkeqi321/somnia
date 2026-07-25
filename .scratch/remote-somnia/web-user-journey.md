# Somnia Web 远程聊天用户链路

**范围:** 用户在浏览器访问 `somnia.top`，控制自己电脑上的 Somnia Project 并聊天。

## 参与者和边界

| 参与者 | 作用 | 持有的数据 |
| --- | --- | --- |
| 浏览器 Web | 登录、选设备/项目、会话和消息交互 | 浏览器本地草稿、提示历史、短期访问令牌 |
| Relay | 认证、配对、在线状态和双向转发 | 账户、Device 公钥、Project 名称/在线元数据；不保存会话内容 |
| Connector | 用户电脑上的常驻连接器、Project Runtime 所有者 | Device 私钥、Project 注册表、内存事件重放窗口 |
| Sidecar/Runtime | 本机执行工具和模型调用 | 本机 Session、transcript、Provider 配置和工具结果 |

关键边界：浏览器不直接访问用户电脑端口；Connector 只向 Relay 发起出站 WSS；Relay 不拥有 Runtime，也不构成离线消息队列。

## 当前首次使用链路

### A. 启动本机服务

用户运行本机启动脚本。当前脚本会启动 Relay、Web、Sidecar，并在用户完成配对码后启动 Connector。生产环境中 Relay/Web 由云端提供，用户电脑只需要 Connector 和本机 Runtime/Sidecar。

### B. 首次配对

1. 浏览器打开 Web，输入账号密码登录 Relay。
2. Web 创建一个短期 Device pairing code，并显示过期时间。
3. 用户在电脑终端运行 `somnia-connector pair --relay ... --code ...`。
4. Connector 生成或读取本机 Device 密钥，使用配对码向 Relay 注册公钥。
5. Relay 返回 Device identity；私钥只写入用户电脑。
6. 浏览器重新拉取 Device 列表，用户选择刚配对的 Device。

### C. 本机上线

1. 用户启动 Connector，并指定已注册 Project。
2. Connector 启动/连接本机 Sidecar，取得 Project Runtime 的 loopback 地址。
3. Connector 通过 `wss://somnia.top/ws/connector/{device_id}` 连接 Relay。
4. Relay 发 challenge，Connector 用 Device 私钥签名，认证成功后发送 Project 名称和在线状态。
5. Web 刷新 Device/Project 列表，显示该电脑在线。

### D. 建立聊天连接

1. 用户在 Web 选择 Device，再选择 Project。
2. Web 通过浏览器 WSS 连接 Relay，并校验短期访问令牌。
3. Web 请求 Project 的 Session 列表；Relay 将请求转给 Connector。
4. Connector 将请求转发到本机 Sidecar，Sidecar 从本机存储返回 Session 元数据。
5. 用户新建或打开 Session，Web 加载权威历史和 Runtime 快照。

### E. 发送一条消息

1. 用户输入文本，可附加图片、slash command 或 `@path` 项目路径提示。
2. Web 生成唯一 `request_id`，发送 `turn.start`。
3. Relay 只在内存中转发请求；Connector 校验 Device/Project，执行去重后转给 Sidecar。
4. Sidecar 调用本机 Runtime、Provider、工具和本地文件。
5. Runtime 事件按序经过 `Sidecar -> Connector -> Relay -> Web` 流式返回。
6. Web 渲染思考、工具、Todo、子代理、团队活动和增量文本。
7. Turn 完成后，Web 从本机重新加载权威 Session，更新列表和上下文用量。

## 日常使用链路

配对只做一次。日常流程应当是：

1. 电脑开机后 Connector 自动运行并显示在线。
2. 用户打开 `https://somnia.top`，登录（已有浏览器会话则跳过）。
3. Web 自动选择最近使用且在线的 Device/Project。
4. Web 自动恢复上次 Session；用户直接输入消息。

切换电脑只改变 Device；切换工作区只改变 Project。Session 内容始终从对应电脑读取，不在云端同步。

## 异常和恢复链路

- **Device 离线:** Web 显示离线原因，内容命令立即失败；草稿保留在浏览器，恢复在线后由用户显式重试。
- **浏览器断线/手机切后台:** Web 带上最后确认的 sequence 请求 replay；重放不可用时请求 Session/Runtime snapshot，再继续监听。
- **Connector 重启:** Relay 标记设备重新连接；浏览器等待在线后自动重连，未完成 Turn 以本机快照为准。
- **Relay 重启:** 浏览器和 Connector 都重连；短期令牌失效时只要求重新登录，不要求重新配对。
- **请求重复到达:** Connector 依据 `request_id` 和参数指纹返回原结果，避免同一问题执行两次。
- **本地授权等待:** Web 显示“等待电脑确认”，不能在云端绕过本地权限或 Yolo 确认。

## 当前主要摩擦

1. 首次流程需要用户理解 Relay、Web、Sidecar、Connector 四个进程。
2. 配对码需要在浏览器和终端之间手工复制，且配对后要重新登录/刷新设备。
3. Web 当前仍可让用户填写 Relay URL；正式域名场景不应暴露该配置。
4. Project 注册、Connector 启动、Connector 错误没有统一的用户向导和诊断入口。
5. Device 在线但 Project/Runtime 未就绪时，状态容易被理解为“连接失败”。
6. 断线、等待本地确认、重同步、离线拒绝等状态需要更明确的下一步操作。

## 推荐优化方案（按收益和依赖排序）

### P0：把首次使用压缩成一个向导

- Web 提供“添加电脑”向导：显示名称、短码、二维码和剩余时间。
- Connector 提供 `somnia-connector setup`，完成登录地址、配对、Project 选择和自检；二维码优先，短码作为备用。
- 配对成功通过 Relay presence 事件实时通知浏览器，自动选择新 Device，不再要求重新登录。
- 向导最后只显示一个结果：`电脑在线 -> Project 已就绪 -> 开始聊天`。

### P1：减少本机运维动作

- 提供安装器或托盘/后台服务，让 Connector 和 Sidecar 随系统启动。
- `somnia-connector doctor` 检查私钥、Relay 可达性、认证、Project 注册、Sidecar 和 Provider 配置，并返回可操作的修复建议。
- Connector 支持多个本地 Project 自动发现已批准注册项；Web 只显示名称，不显示路径。

### P1：让 Web 默认“打开就能聊”

- 生产构建固定同源地址：`/`、`/api/*`、`/ws/*`，隐藏 Relay URL 输入框。
- 最近 Device、Project、Session 仅保存在浏览器本地；登录后自动恢复。
- 首次选择在线 Device 后自动连接，只有多设备时才展示选择器。
- 无 Session 时直接展示“新建会话”主按钮和最近会话，不让用户先理解协议概念。

### P1：状态设计以行动为中心

统一状态文案和动作：

| 状态 | 用户看到的内容 | 主动作 |
| --- | --- | --- |
| 未配对 | “还没有电脑” | 添加电脑 |
| 配对未上线 | “已配对，等待电脑上线” | 查看启动命令/重新生成码 |
| 在线但 Project 未就绪 | “电脑在线，工作区启动中” | 查看诊断 |
| 已连接 | “可聊天” | 新建/继续会话 |
| 断线重连 | “网络中断，正在恢复（不重复执行）” | 等待或重新连接 |
| 等待本地确认 | “请在电脑上确认此操作” | 查看确认提示 |
| 离线 | “电脑离线，草稿已保留” | 重试/查看诊断 |

### P2：聊天中的易用性

- 发送按钮旁显示当前 Device、Project 和连接状态，避免发错电脑。
- 活跃 Turn 时保留现有排队语义，但显示队列数量、取消单条和“停止当前 Turn”。
- 长响应或移动端切后台后，恢复时显示“已恢复 N 个事件/已从本机快照同步”。
- 错误信息提供用户动作和诊断编号；诊断内容默认不包含提示词、响应或工具参数。

## 推荐实现顺序

1. P0 配对向导和 `setup` 命令，打通首次使用闭环。
2. P1 Connector 后台自启动和 `doctor`，降低本机启动成本。
3. P1 Web 自动选择/自动恢复，覆盖日常使用主路径。
4. P1 状态模型和文案统一，补齐离线、重连、本地确认等边界。
5. P2 聊天细节优化，并用 Playwright 覆盖新用户、手机切后台和多设备切换。

## 成功标准

- 新用户不看开发文档，仅按向导可在 3 分钟内完成“添加电脑并发送第一条消息”。
- 日常用户打开 Web 后最多一次点击即可进入上次 Project/Session。
- 任何失败状态都明确说明数据是否已执行、草稿是否保留、下一步做什么。
- Relay、日志、缓存和浏览器网络面板均不持久化会话内容；优化不改变这一约束。
