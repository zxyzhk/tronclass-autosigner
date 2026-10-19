# catch me if you can：tronclass-autosigner

厦门大学畅课（XMU TronClass）自动签到工具套件。

**已实现 XMU 数字码自动签到、二维码载荷解析与提交，以及微信签到完成状态通知。**
二维码自动完成取决于取码权限与服务端校验；需要有效载荷时，可通过浏览器面板或本地接口提供。
微信通知覆盖签到成功、失败和需要人工处理的状态，使用前需配置推送通道。

一个**本地优先**的畅课（TronClass，`lnt.xmu.edu.cn`）签到自动化套件：后台守护进程 + 微信推送机器人 + 浏览器脚本。

> Local-first auto check-in toolkit for **XMU's TronClass**: a background daemon,
> a WeChat push relay, and a browser userscript. Reverse-engineering notes included.

## ⚠️ 声明（务必先读）

1. **本项目针对厦门大学（XMU）畅课（TronClass，`https://lnt.xmu.edu.cn`）编写与验证**；
   其他学校的租户接口语义大体一致，但权限与字段可能不同，需自行核对。
2. **仅用于快速签到等测试、研究与个人效率用途** —— 例如本人在场时，老师发起签到你快速完成。
3. **严禁用于旷课**：不得在本人未实际到场时代签；不得伪造二维码/数字码；不得替他人签到、
   批量代签或任何形式的考勤造假。伪造考勤属违反校纪校规，后果自负。
4. 不处理账号密码，不使用教师凭据，不访问他人账号。
5. 完整条款见 [`DISCLAIMER.md`](DISCLAIMER.md)。

## 能力范围（实测口径）

| 签到类型 | 能否全自动 | 依据 / 说明 |
| --- | --- | --- |
| **数字签到** | ✅ **完全自动** | 轮询发现 → 取数字码（点名详情 `number_code`，退路 `GET /api/timetable_rollcalls`）→ `PUT /api/rollcall/{id}/answer_number_rollcall`。已实现并有端到端测试 |
| **普通 / 点击签到** | ✅ 自动 | `PUT /api/rollcall/{id}/answer?api_version=1.76`（客户端接口表实测存在） |
| **GPS / 雷达签到** | ✅ 自动（需坐标） | `PUT /api/rollcall/{id}/answer`；需在配置里填教室坐标 `coords` |
| **二维码签到** | ⚠️ **条件性自动**（自动判定） | 见下节：能否全自动取决于该租户服务端是否校验 `data`，由守护进程在**首次真实二维码点名时自动判定** |

### 二维码：为什么是"条件性自动"

- QR 载荷 = `create_time`（10 位 Unix 秒）+ `create_time_hash`（32 位 hex）。
  **服务端生成、服务端校验**（客户端内置错误码 `invalid_create_time_hash` / `qr_code_expired` 等）；
  客户端**没有编码器、没有密钥**，128 bit 无法枚举 ⇒ **不可自行推导**。
- 学生会话调用未公开取码端点 `GET /api/course/{cid}/rollcall/{rid}/qr_code`，
  在实测中返回 **HTTP 403**（学生会话无权限）。
- 因此本套件的二维码流程是 —— **取码 → 取不到则自动做一次判定实验**：
  发一条"形状合法、`hash` 伪造"的 `data`：
  - 服务端返回 **200**（不校验 `data`）⇒ 判定成立，此后**所有二维码点名自动签到**，无需人工；
  - 返回 `invalid_create_time_hash` 等（确实校验）⇒ **无法自动**，会提醒你人工提供码内容，
    再用本地载荷接口 `POST /qr`（或面板粘贴）提交。
- **每次运行中，每个点名最多探测一次**；接受结果在本次运行的后续二维码点名中复用，重启后重新判定。

> 一句话：**数字码可全自动；二维码能全自动与否，由服务端是否校验 `data` 决定，
> 本套件会自动判定并据此执行。**

---

## 它解决什么

| 痛点 | 做法 |
| --- | --- |
| 浏览器标签页在后台被降频、关掉即停摆 | **后台守护进程**常驻轮询，不依赖浏览器 |
| 全天轮询浪费请求、也容易引人注意 | 读课程表，**只在上课/课间窗口前后 10 分钟**内查询（默认 20 秒一次），窗口外零请求 |
| 本机时钟不准、时区不对 | 以服务端 `Date` 响应头校时（含半程时延补偿上限），**不修改系统时间** |
| 总是全班第一个签到 | **门控**：已签 ≥ N 人（默认 4）才提交，并有防漏签保底 |
| 不知道有没有签上 | 结果推送到**微信**（Server酱 / PushPlus / WxPusher / 企业微信机器人） |
| 二维码无法自动化 | 先试服务端取码；取不到则做**一次性实验**：服务端到底校不校验 `data` |

## 组成

| 路径 | 说明 |
| --- | --- |
| `changke-daemon/` | 后台轮询签到守护进程（纯标准库，无需 pip） |
| `clawbot/` | 本地通知中继 → 转发到你的微信（纯标准库） |
| `changke-autosign.user.js` | 浏览器脚本：面板、日志、二维码人工粘贴、权限/校验探测 |
| `docs/畅课签到逆向笔记.md` | 逆向笔记：接口口径、二维码编码格式、客户端静态取证结论 |
| `tools/gen_schedule.py` | 从你的课程表 JSON 生成脚本里的 `SCHEDULE` 常量 |

```
本机 App 登录态 ─┐
                 ├─► changke-daemon ──► clawbot ──► 微信
你的课程表 JSON ─┘        │
                          └─ 窗口内 20s 轮询 → 满 N 人 → 提交签到
```

## 快速开始

### 1) 微信推送（clawbot）

```bash
cd clawbot
cp config.example.json config.json && chmod 600 config.json
# 填 provider 与对应凭据（推荐 Server酱 / PushPlus / WxPusher，直达微信）
python3 clawbot.py check
python3 clawbot.py test      # 微信里收到即打通
python3 clawbot.py serve     # 常驻
```

### 2) 守护进程（changke-daemon）

**零配置路径**：只要本机装有畅课 App 且**登录过一次**，会话可自动复用
（读 App 容器里的 `localStorage`，**只读**：自动发现域名、`sessionID`、刷新令牌、设备 UUID）。

```bash
cd changke-daemon
python3 changke_daemon.py app-session    # 只读验证会话
python3 changke_daemon.py check          # 会话/校时/课表/clawbot 体检
python3 changke_daemon.py once --dry-run # 空跑一轮，不提交
python3 changke_daemon.py serve          # 常驻
```

不想用 App 时，改用浏览器 Cookie：`cp config.example.json config.json`，
填 `base_url` + `cookies`（DevTools → Network → 任意 `/api` 请求 → Request Headers → Cookie），
并把 `session_source` 设为 `"config"`。

### 3) 课程表

把课表整理成 JSON（结构见 `tools/gen_schedule.py` 头部注释），放到
`changke-daemon/课程表.json`（或在配置中指定 `schedule_path`），再生成浏览器脚本配置：

```bash
python3 tools/gen_schedule.py 课程表.json --in-place changke-autosign.user.js
```

未生成时脚本里是**示例课表**，务必替换，否则窗口判定不对。

### 4) 浏览器脚本

Chrome/Edge 安装 Tampermonkey → 导入 `changke-autosign.user.js` → 打开畅课网页版并登录。
面板可调门控人数、轮询间隔、GPS 坐标、微信推送；并提供「探测取码权限」「探测服务端校验」。

## 二维码：这一环的真实边界

逆向结论（详见 `docs/畅课签到逆向笔记.md` §3、§9–§11）：

- QR `data` = `create_time`（10 位 Unix 秒）+ `create_time_hash`（32 位 hex）。
  **服务端生成、服务端校验**（客户端内置错误码 `invalid_create_time_hash` / `qr_code_expired` 等），
  客户端**没有编码器、没有密钥**，128 bit 无法枚举 ⇒ **不可推导**。
- 但客户端**本身已实现"不扫码直接提交"**：把载荷（JSON / 紧凑串 / `/j?p=`、`_wg_p` 深链）喂给它即提交
  ⇒ 技术障碍不在客户端，而在**载荷从哪来**。
- 因此本套件提供：

```bash
# 任何来源的载荷都能立即提交
curl -s http://127.0.0.1:8788/qr -H 'Content-Type: application/json' \
  -d '{"payload":"<JSON / 紧凑串 / /j?p= / _wg_p>"}'
python3 changke_daemon.py qr-payload '<载荷>'
```

- 守护进程在取码失败时会**自动做一次有界实验**：发一条"形状合法、hash 伪造"的 `data`；
  `200` 说明服务端不校验 → 此后二维码点名自动签到；返回校验类错误码 → 提示你人工提供码内容。

## 测试

```bash
cd changke-daemon && python3 tests/run_tests.py
```

用本地假畅课服务端跑端到端：数字签到+门控、取不到码、历史点名跳过、窗口外零查询、
二维码校验探测的两个分支（不校验→自动签 / 校验→提示人工）等；并校验请求确实带上会话头。
本次发布通过 **6 个模拟场景、37 项检查**；通知检查验证到达本地模拟中继，实际微信送达需配置后自行验证。
测试通过伪造 `Date` 头验证校时 —— **故意让本机时钟偏差数小时，仍能落进正确窗口**。

## 隐私与本地配置

发布内容仅包含通用代码、示例配置和测试夹具，不包含个人课表、登录态、微信凭据、运行日志或对话记录。
`config.json`、`课程表.json`、`state.json`、设备标识和日志由 `.gitignore` 排除；请仅在本机填写真实数据。

## 网络注意事项

`force_ipv4`（默认开启）：当本机 IPv6 路由不通时，`urllib` 会先连 IPv6 等超时再回落 IPv4，
实测**每个请求白等约 30 秒**；开启后单次请求由 30s 降到 0.4s。若你的网络 IPv6 正常，可关掉。

## 免责声明

本项目仅供学习与技术研究。多数院校的考勤规则与平台条款**不允许**代签或自动化签到，
使用可能被认定为考勤违纪，风险由使用者自行承担。

本套件**不包含**任何账号密码处理，也不使用教师凭据；请勿用它访问他人账号或课堂。

## 许可与致谢

代码以 **MIT** 许可发布（见 `LICENSE`）。

本仓库为**独立实现**：二维码紧凑编码是对照厂商客户端 bundle 逐行验证后重写的；
接口口径来自对各校公开实测记录的复核。研究线索参考了以下公开项目（**未复制其代码**）：
`spicysuimai/XMU-AC`、`alkali210/XMU-Rollcall-Bot`、`wilinz/fuck_tronclass_sign`、
`CUC-Life-Hack/tronclass-hack`、`hot-YUser/auto-Tronclass-VPS`。
