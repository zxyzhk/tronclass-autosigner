# clawbot — 把签到事件推到你微信

> ⚠️ **声明**：配套的畅课签到工具针对**厦门大学（XMU）畅课**编写，
> **仅用于快速签到等测试/研究用途，严禁用于旷课**。完整条款见 [`DISCLAIMER.md`](../DISCLAIMER.md)。

本地推送机器人。油猴脚本把事件发给它，它转发到你微信。

```
油猴脚本（畅课自动签到）
   │  POST http://127.0.0.1:8787/notify   （走 GM_xmlhttpRequest，无 CORS 问题）
   ▼
clawbot（本机进程，仅监听 127.0.0.1）
   │  HTTPS
   ▼
推送通道 ──► 你的微信
```

## 配置微信推送通道

先开通下列任一推送通道，再将对应凭据填入本机 `config.json`。

本中继通过企业微信群机器人或第三方公众号推送服务发送通知。

## 通道选哪个

| provider | 怎么拿凭据 | 消息落在哪 |
| --- | --- | --- |
| `serverchan` ⭐ | [sct.ftqq.com](https://sct.ftqq.com) 微信扫码登录 → 复制 SendKey | **直接进微信**（公众号推送） |
| `pushplus` ⭐ | [pushplus.plus](https://www.pushplus.plus) 微信扫码 → 复制 token | **直接进微信** |
| `wxpusher` ⭐ | [wxpusher.zjiecode.com](https://wxpusher.zjiecode.com) 创建应用拿 appToken，关注其公众号拿 UID | **直接进微信** |
| `wecom` | 企业微信 → 任意群 → 群机器人 → 添加 → 复制 Webhook 地址 | 企业微信 App（最稳、免费、无第三方） |
| `generic` | 你自己的 webhook | 看你自建怎么处理 |

**想直接进微信就用前三者**；只有企业微信、且不在意落在企业微信 App，就用 `wecom`。

具体开通流程与服务限制请以各通道的官方说明为准。

## 三步跑起来

```bash
cd clawbot
cp config.example.json config.json
# 编辑 config.json：只填 "provider" 和它对应的那一个凭据
chmod 600 config.json

python3 clawbot.py check   # 看配置是否就绪（密钥一律打码显示）
python3 clawbot.py test    # 发一条测试消息 —— 微信里收到就通了
python3 clawbot.py serve   # 启动中继（前台运行）
```

浏览器那边：油猴脚本面板勾上 **「微信推送」**，然后点 **「测试提醒」**，整条链路（脚本 → clawbot → 微信）就走通了。

## 开机自启（macOS）

```bash
# 1) 先把 plist 里的 /REPLACE/WITH/clawbot 换成实际绝对路径
sed -i '' "s#/REPLACE/WITH/clawbot#$(pwd)#g" com.local.clawbot.plist

# 2) 装进 LaunchAgents
cp com.local.clawbot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.clawbot.plist

# 查看状态 / 停止
launchctl list | grep clawbot
launchctl unload ~/Library/LaunchAgents/com.local.clawbot.plist
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/notify` | body `{"title":"...","body":"..."}` → 推送（带去重与限速） |
| POST | `/test` | 发测试消息 |
| GET | `/health` | 通道是否就绪、上次推送结果、被丢弃条数 |
| GET | `/` | 状态页 |

推送请求示例：

```bash
curl -s http://127.0.0.1:8787/notify \
  -H 'Content-Type: application/json' \
  -d '{"title":"检测到签到 · 高等数学","body":"数字签到已发起，等满 4 人后自动签"}'
```

## 安全

- 默认**只绑定 `127.0.0.1`**，外网访问不到。
- `api_token` 可选：一旦设置，`/notify` 必须带 `X-Clawbot-Token` 头（防止本机其它程序/网页乱发）。
- 密钥**永不打印**：`check` 与日志里一律打码（webhook 的 query 也会被替换成 `***`）。
- `clawbot.log` 只记标题与结果，不记凭据；超过 5MB 自动轮转。
- 去重窗口默认 20 秒（同内容不重复推），限速默认 30 条/分钟，防止脚本 bug 把你微信刷爆。

## 排障

| 现象 | 原因 |
| --- | --- |
| `check` 说缺凭据 | `config.json` 里 `provider` 与对应凭据字段没对上 |
| `/notify` 返回 503 | 通道未配置就绪（同上） |
| 返回 502 且 detail 里有 `errcode`/`code` | 凭据错或已失效，按通道文档重新获取 |
| 脚本日志出现「推送中继不可达」 | clawbot 没在跑，或端口不一致 |
| 微信没收到，但 `test` 返回成功 | 第三方服务侧的问题（如公众号未关注/应用未发布） |

## 自定义通知出口

`clawbot` 是本套件的本地通知中继。使用 `generic` provider 可将事件发送到自建 webhook，由自建服务继续转发。
