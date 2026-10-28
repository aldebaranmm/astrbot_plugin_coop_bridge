# astrbot_plugin_coop_bridge

AstrBot × Coopanion 桥接插件（**服务端**）。

把 AstrBot 的记忆、主动消息、活动感知接到 Coopanion 桌宠上。
当前版本 `0.9.3`：身份绑定 + 机器配对 + 主动消息双发 + 记忆检索 + 对话回传 + 叫不应就凑过来 + 活动感知（M8），另含 M13 远程推送。

**配套客户端**（装在同一套里的另外两个）：

- [cortico-world-astrbot](https://github.com/aldebaranmm/cortico-world-astrbot) —— 桌宠扩展主体
- [cortico-world-web](https://github.com/aldebaranmm/cortico-world-web) —— 网页/接口取数
- [总览 + 完整安装步骤](https://github.com/aldebaranmm/coopanion-astrbot-bridge)

> 消息是**客户端主动来拉**的（`GET /bridge/inbox`，默认 20 秒一次），
> 不是本插件主动推给客户端。所以本插件**不需要、也没有**"客户端地址"这类配置。

## 配套的 AstrBot 插件

本插件**不是独立工作的**，下面这些决定它能发挥多少功能。

### 强烈建议装：主动消息

| 插件 | 不装会怎样 |
| --- | --- |
| [astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) | **M3 双发静默跳过** —— 主动消息只发到 QQ，桌宠不会冒泡。其余功能（绑定 / 配对 / 检索 / 远程推送）照常能用 |

装了之后还要配两处，否则桌宠仍然不会开口：

1. 在它的配置里把要收到主动消息的人加进私聊白名单 `friend_settings.session_list`
2. 客户端的 `wakeMode` 设为 `flush`（`piggyback` 会攒着，等你先跟桌宠说话才冒出来）

### 记忆数据源（至少装一个）

`POST /bridge/recall` 从下面三个插件读记忆，**装哪个读哪个，可以都装**：

| 插件 | 提供什么 |
| --- | --- |
| [astrbot_plugin_livingmemory](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory) | 事实 / 主题记忆（主力） |
| [astrbot_plugin_angel_memory](https://github.com/kawayiYokami/astrbot_plugin_angel_memory) | 判断类记忆 |
| [astrbot_plugin_self_learning](https://github.com/NickCharlie/astrbot_plugin_self_learning) | 个人记忆 + 黑话词典 |

一个都不装的话，`recall` 只会返回空 —— **插件不报错，就是查不到东西**。
用 `GET /bridge/health` 能看到每个源是否 `available`。

### 参考配置（作者实机）

```
astrbot_plugin_proactive_chat   v1.2.6
astrbot_plugin_livingmemory     2.8.2
astrbot_plugin_angel_memory     2.0.4
astrbot_plugin_self_learning    4.4.3
```

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
| `POST /bridge/push` | 令牌 | 往该身份队列塞一条（远程下发指令/手动测试） |
| `POST /bridge/chat` | 令牌 | 客户端回传对话：`{"items":[{"user":"...","assistant":"..."}]}` |
| `GET` / `POST /bridge/activity` | 令牌 | 读取 / 上报「主人在干嘛」（前台窗口、空闲时长） |
| `POST /bridge/video` | 令牌 | 刷视频结果上报。**卡片必须带 `text` 字段**，否则会静默丢弃（返回 `accepted:0`） |
| `POST /bridge/douyin` | 令牌 | 抖音结果上报 |

令牌通过 `Authorization: Bearer <token>` 或 `X-Bridge-Token: <token>` 传入。

### 怎么判断插件加载成功

```bash
curl http://127.0.0.1:6190/bridge/health      # 200 = 服务在跑
curl http://127.0.0.1:6190/bridge/identity    # 401 = 端点已注册（正常）；404 = 插件没加载
```

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

将本目录复制到 AstrBot 实例的 `data/plugins/` 下，**重启 AstrBot 实例**才生效
（仪表盘里的"重载"对新装的代码无效）。

装好后用上面的 `/bridge/health` 确认。默认只监听 `127.0.0.1:6190`，
别的机器要连请用 frp 穿透，别把端口直接暴露到公网。

---

## 许可与商用限制

作者：**Kaiser**

### ✅ 可以自由使用

- 个人自用、学习研究
- 非营利的修改与二次分发（须保留署名和本声明）

### ❌ 未经书面授权，禁止任何形式的商业使用

包括但不限于：

- 打包进收费产品或服务出售
- 用于经营性业务（付费机器人、商业客服、企业内部系统等）
- 二次分发收费，或去除/篡改署名后转售
- 作为付费部署、代搭建服务的一部分提供

### 商业授权

确有商业使用需求的，请联系作者取得 **书面授权**。

### 📮 联系方式

有问题、建议、bug 反馈，或需要商业授权，都欢迎联系作者：

- **QQ 邮箱：2760546027@qq.com**

（作者即本项目的 QQ 机器人使用者，反馈时说明一下你用的是哪个组件、版本号，会更快定位。）

### 法律声明

对未经授权的商业使用，作者 **保留一切追诉权利**，
包括但不限于要求立即停止侵害、消除影响、赔偿损失，以及提起诉讼。

---

代码许可以仓库根目录的 [LICENSE](LICENSE) 文件为准：**PolyForm Noncommercial License 1.0.0**
（源码公开可读、可改、可分发，但**仅限非商业用途**）。
商业使用须另行取得作者的书面许可，联系方式见上。
