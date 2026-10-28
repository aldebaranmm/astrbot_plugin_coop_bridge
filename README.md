# astrbot_plugin_coop_bridge

AstrBot × Coopanion 桥接插件（开发中，当前 M4，含 M3 双发）。

## 分期

| 版本 | 内容 | 状态 |
| --- | --- | --- |
| M1 | 多身份绑定（协议层 QQ 号 + 一次性确认，KV 存储） | 已完成，QQ 实测通过 |
| M2 | 配对协议（QQ 配对码 ↔ 机器令牌 + 设备管理命令） | 已完成，已部署 |
| M3 | 主动消息双发（挂钩 proactive_chat 发送出口，抄送 inbox） | 已完成，25/25 测试通过 |
| M4 | 独立 HTTP 服务 + 记忆检索（livingmemory / angel_memory / self_learning） | 已完成，47/47 测试通过 |

## M1 使用

- 任意用户私聊 bot → 自动进入绑定流程，bot 复述协议层识别到的 QQ 号；
- 回复「确认」→ 绑定钉住；「不是」→ 取消；流程内其他消息 → 重发提示；
- 已绑定用户发「解绑」→ 二次确认「确认解绑」→ 注销（含后续各期的设备数据）；
- 已绑定用户的普通消息不拦截，放行 AstrBot 主回复链。

## M2 使用（配对电脑）

| QQ 私聊发 | 作用 |
| --- | --- |
| `配对` | 拿一个 8 位配对码（10 分钟有效、只能用一次、最多同时 3 个、每小时最多申请 5 次） |
| `我的设备` | 列出你名下已配对的电脑（名字 + 绑定时间） |
| `取消配对 名字` | 吊销某一台；只带「取消配对」则列出名字让你选 |
| `解绑` → `确认解绑` | 注销身份，同时吊销名下所有设备、作废未用的码 |

电脑上填码：`POST /bridge/pair  {"code":"12345678","device_name":"笔记本"}`
→ 返回 `{"token": "...", "device_id": "...", "name": "...", "identity_label": "276*****27"}`。
令牌按身份隔离，M4 的 recall 只能读到自己身份的数据；QQ 号对外只给打码标签。

## M3 主动消息双发

不动 `astrbot_plugin_proactive_chat` 的任何源码，只在运行期给它的发送出口
`SenderMixin._send_proactive_message` 套一层包装：

- QQ 该怎么发还怎么发，返回值与异常语义完全不变（抄送出错也只记日志）；
- 发送完成后，这条文本抄送进该身份的 outbox，等 Coopanion 来 `GET /bridge/inbox` 拉；
- 只认**私聊**会话且只推给**已绑定**的身份：群聊主动消息、陌生人不外流；
- 纯图片/语音（文本为空）暂不抄送；
- 单身份队列上限 50 条，机器长期离线不会把内存堆爆（重启即清空，当前不做持久化）；
- 插件卸载时自动还原原函数；主动消息插件没装/没启用时静默跳过。

配置项 `proactive_dual_send`（默认开）可一键关掉双发。

## M4 桥接服务

独立 HTTP 服务，默认 `127.0.0.1:6190`（**不走仪表盘**——仪表盘有管理员登录，绝不穿透）。
公网访问靠 frp 隧道；插件配置里 `server_bind` 保持 `127.0.0.1` 最安全。

| 端点 | 鉴权 | 用途 |
| --- | --- | --- |
| `GET /bridge/health` | 无 | 探活 + 各数据源可用性 |
| `POST /bridge/pair` | 无 | 配对码换机器令牌 |
| `GET /bridge/identity` | 令牌 | 该机器所属身份（QQ 号打码）、昵称 |
| `POST /bridge/recall` | 令牌 | 检索记忆：`{"query":"...","limit":20}` |
| `GET /bridge/inbox` | 令牌 | 拉取待推送的主动消息（M3 写入） |

令牌通过 `Authorization: Bearer <token>` 或 `X-Bridge-Token: <token>` 传入。

### 检索数据源

| 源 | 内容 | 归属口径 |
| --- | --- | --- |
| livingmemory（主力） | `graph_entries` 里的事实/主题记忆 | `session_id` 私聊全取；群聊仅取内容提到本人 QQ 号的条目 |
| angel_memory | `memory_records` 的判断类记忆 | `memory_scope` 为 public，或 scope 含本人 QQ 号 |
| self_learning | `memories` 个人记忆 + `jargon` 黑话词典 | `group_id`/`user_id` 等于本人 QQ 号 |

只读访问，任一源失败只影响它自己那一段，其余照常返回。

## 安全口径

身份只取自 `platform_meta.id` / `message_obj.self_id` / `message_obj.sender.user_id`，
消息正文自报的号码不作为身份依据（不可验证）。

## 安装（需用户批准）

将本目录复制到 AstrBot 实例的 `data/plugins/` 下，重载插件。
