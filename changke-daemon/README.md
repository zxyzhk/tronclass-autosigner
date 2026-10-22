# changke-daemon — 厦门大学畅课（XMU TronClass）后台轮询签到

> ⚠️ **声明**：本项目针对**厦门大学（XMU）畅课**编写，**仅用于快速签到等测试/研究用途，严禁用于旷课**。
> 完整条款见仓库根目录 [`DISCLAIMER.md`](../DISCLAIMER.md)。

**为什么需要它**：浏览器标签页在后台会被降频（Chrome 约 1 次/分钟），关掉浏览器就完全停摆。
本进程常驻后台直接轮询畅课，**不受浏览器限制**。

**v1.1.0（2026-10-22）**：位置签到支持接口坐标、课程缓存和基于距离的定位；
提交后回查本人签到状态。新增运行生命周期通知与通知失败重试。

```
本机畅课 App（你在 App 里登录一次）
   │  守护进程只读它的 localStorage，取出 sessionID / 刷新令牌 / 设备 UUID
   ▼
changke-daemon（常驻，无浏览器）
   │  1) 读课表 → 只在窗口内轮询（20s）
   │  2) 服务端 Date 头校时（不改系统时间）
   │  3) 检测签到 → 满 4 人 → 提交
   │  4) 会话过期 → 用刷新令牌静默续期
   ▼
clawbot（127.0.0.1:8787）  ──►  你的微信
```

## 跑起来（macOS App 会话方式）

**前提**：`/Applications/TronClass.app` 已安装，且**你在 App 里登录过一次**。
需要 Python 3.10+。将本机课表放到本目录的 `课程表.json`，或在 `config.json` 中指定 `schedule_path`。

```bash
cd changke-daemon
python3 changke_daemon.py app-session      # 只读：读出 App 会话并验证（会打印打码摘要）
python3 changke_daemon.py check            # 会话 / 校时 / 课表 / clawbot 逐项体检
python3 changke_daemon.py once --dry-run   # 忽略窗口空跑一轮，只记录不提交（安全观察）
python3 changke_daemon.py serve            # 正式常驻
```

仅使用默认选项时可以不创建 `config.json`。`session_source=auto`（默认）且未填 Cookie 时，守护进程会：

| 自动获取 | 来源（App 容器的 localStorage） |
| --- | --- |
| 畅课域名 | `org.apiUrl`（例：`https://courses.example.edu.cn`） |
| API 会话 | `sessionID` → 作为 `x-session-id` 请求头 |
| 续期能力 | `identityRefreshToken`（Keycloak）→ 过期时静默换新 |
| 设备 ID | `deviceInfo.uuid` → 作为 `deviceId` |

**只读保证**：只读 App 容器里的 `localstorage.sqlite3`（复制副本后读取，读完即删），
**不修改 App 任何数据**；不存账号密码，也不碰登录接口。

## 备选：用浏览器 Cookie

不想用 App 的话，复制上面的示例配置并填写：

```bash
cp config.example.json config.json && chmod 600 config.json
```

1. 浏览器登录畅课 → `F12` → **Network** → 刷新 → 点任意 `/api/...` 请求
2. 复制 **Request Headers** 里 `Cookie:` 的完整值 → 填 `cookies`
3. 若有 `x-session-id:` 也一并复制 → 填 `x_session_id`
4. 填 `base_url`（你学校的畅课域名）
5. 把 `session_source` 设为 `"config"`

## 与油猴脚本的关系

两者**逻辑同源**，定位不同：

| | 油猴脚本 | 守护进程（本目录） |
| --- | --- | --- |
| 运行位置 | 浏览器标签页 | 后台常驻进程 |
| 后台可用性 | 会被降频，关浏览器即停 | ✅ 不受影响 |
| 凭据来源 | 浏览器里的登录态 | 本机 App 的登录态（自动） |
| 二维码人工载荷提交 | ✅ 有面板 | ✅ 本地 `/qr` 接口或 `qr-payload` 命令 |
| GPS 坐标来源 | 手动填写 | 手动 / 接口 / 缓存 / 距离定位 |

**建议**：守护进程负责轮询签到；浏览器脚本把「启用」取消勾选，只留面板做二维码人工处理。
两者同时轮询会让同一点名被提交两次（无害但不必要）。

## 行为细节

- **窗口**：读 `课程表.json`，同一天间隔 ≤25 分钟的课合并为一段，两端各放宽 **10 分钟**；
  窗口内 **20 秒**一次，窗口外**零签到请求**（只做每 30 分钟一次的校时）。
  > **不判断学期周次**：所有课程都参与窗口计算，已结课的课程也可能在其时段产生查询。
- **校时**：读畅课响应的 `Date` 头算偏差（含 RTT/2 校正），`now() = 本机时间 + 偏差`。
  **不修改系统时间**；窗口判定、门控计时、token 时效判定全部用它。
- **门控**：已签人数 ≥ `gate_min_signed`（默认 4）才提交；
  保底放行：拿不到名单 / 等待超 `gate_max_wait_s` / 距结束不足 `gate_min_remain_s`。
- **签到类型**：数字签到自动取码；GPS 按下节获取坐标，并以名单回查确认成功；
  二维码先试服务端取码接口，再按校验结果自动处理或提醒提供有效载荷。
- **会话续期**：仅当请求返回 **401**（会话真失效）→ 用刷新令牌换新会话 → 自动重试一次；
  5 分钟冷却，避免打爆认证服务。403 属于权限问题，不做续期。
- **只处理"正在进行"的点名**：课程签到列表里会混着大量**历史记录**（你当年没签、但点名早已结束）。
  守护进程会取点名详情，要求 `status=in_progress` 且 `end_time` 未过才动作，其余静默跳过 —— 既不误提交也不误提醒。
- **优先 IPv4**（`force_ipv4`）：本机 IPv6 路由不通时，`urllib` 会先连 IPv6 等超时再回落，
  实测**每个请求白等 30 秒**。开启后单次请求从 30.45s 降到 0.44s（快约 69 倍）。
- **状态持久化**：`state.json` 记录已处理点名和计算成功的课程坐标；重启后复用，且每次仍须验证结果。
- **微信状态**：通知启动、进入检测窗口、签到结果和可捕获的停止事件。中继失败时默认尝试三次，
  重试间隔为 2 秒、5 秒；“已推送”仅表示本地中继接受请求，实际微信送达由通道决定。
- **二维码三件事**（详见逆向笔记 §11）：
  1. **载荷解析**支持三种形态：JSON、紧凑串（`!k~v!`）、URL/深链（`/j?p=`、`scanner-jumper`、`_wg_p`）。
  2. **服务端校验探测**（`qr_probe_validation`）：取码失败时自动做一次有界实验 ——
     发一条"形状合法、hash 伪造"的 data；返回 200 说明服务端**不校验**，此后二维码点名自动签到；
     返回 `invalid_create_time_hash` 等错误码说明确实在校验，则提醒你人工提供码内容。
  3. **本地载荷接口**（`qr_api_enabled`，默认 `127.0.0.1:8788`）：任何来源的载荷都能立即提交 ——
     `curl -s http://127.0.0.1:8788/qr -H 'Content-Type: application/json' -d '{"payload":"<载荷>"}'`
     或 `python3 changke_daemon.py qr-payload '<载荷>'`。

## 位置签到配置

坐标来源按 `coords` → 点名详情/列表 → 本课程缓存 → 距离定位的顺序尝试。
填入 `coords` 后会优先使用，失败时不切换其他坐标；留空才会自动查找。

```json
{
  "coords": "",
  "radar_auto": true,
  "radar_probe_coords": "24.3,118.0;24.6,118.2",
  "radar_tolerance_m": 5.0,
  "max_attempts": 20
}
```

上面是可合入本机 `config.json` 的配置片段。探针为通用示例，可改为学校附近两点；
`radar_tolerance_m` 是几何计算容差，不是签到半径。
`radar_auto=false` 只关闭距离定位，仍会使用手动、接口或缓存坐标。

没有有效坐标且响应不含 `distance` 时无法自动定位，达到最大尝试次数后通知人工处理。
只有 HTTP 提交成功且本人名单状态为 `on_call_fine` 才通知位置签到成功；空跑不会提交或标记成功。
缓存保存在本机 `state.json` 的 `radar_cache`，不随代码发布。详见[根目录位置签到说明](../README.md#位置签到如何自动完成)。

## 开机自启（macOS）

```bash
sed -i '' "s#/REPLACE/WITH/changke-daemon#$(pwd)#g" com.local.changkedaemon.plist
cp com.local.changkedaemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.local.changkedaemon.plist
launchctl list | grep changke
launchctl unload ~/Library/LaunchAgents/com.local.changkedaemon.plist   # 停止
```

## 测试

```bash
python3 tests/run_tests.py     # 本地模拟服务、临时课表和通知中继
```

覆盖数字签到与门控、取不到码、历史点名跳过、窗口外零签到查询、二维码分支，以及位置签到的接口坐标、
距离定位和缺少距离反馈。测试通过伪造服务端 `Date` 验证校时，并检查会话头和模拟通知。
所有坐标与课表均为测试数据，不需要个人文件，也不向真实畅课或微信发送请求。

## 排障

| 现象 | 处理 |
| --- | --- |
| `app-session` 说找不到会话 | 先在 `/Applications/TronClass.app` 里登录一次 |
| `check` 说会话无效且续期失败 | 刷新令牌可能已失效 → 重新在 App 登录一次 |
| `check` 说校时失败 | 站点响应被 CDN 剥了 `Date` 头；窗口判定退回本机时间 |
| 推送失败 | `clawbot` 没在跑 → `cd ../clawbot && python3 clawbot.py serve` |
| 一直「窗口外」 | 看 `check` 打印的学校时间与下一个窗口；确认 `schedule_path` |
| 每个请求都很慢（数十秒） | IPv6 路由不通 —— 确认 `force_ipv4` 为 `true`（默认开） |
| 收到历史点名的误报提醒 | 升级到当前版本；已加"只处理 in_progress"的门槛与回归测试 |
| 取不到数字码 | 该租户可能未开放 `/api/timetable_rollcalls`；会提醒你手动签到 |
| 位置签到提示缺少 `distance` | 自动定位依赖服务端反馈；没有有效接口/缓存坐标时，请手动签到或在本机配置 `coords` |
| 位置提交 HTTP 200 仍未成功 | 程序还会回查本人签到状态；未确认时不会推送成功 |

## 安全

- **不存账号密码**，不调用登录接口；会话来自本机 App 的本地存储。
- 备选的 Cookie 模式请 `chmod 600 config.json` 且不要提交到 git。
- 日志（`changke.log`）只记事件与结果，**不记会话凭据**；超过 5MB 自动轮转。
- 请求强制直连（`ProxyHandler({})`），避免失效的系统代理把请求拦成 ProxyError。
