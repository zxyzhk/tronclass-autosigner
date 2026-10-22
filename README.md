# catch me if you can：tronclass-autosigner

厦门大学畅课（XMU TronClass）自动签到套件：**数字码签到、位置签到、二维码签到，以及微信状态通知**。
后台守护进程负责轮询和提交，浏览器脚本提供辅助面板，本地 `clawbot` 中继将事件转发到微信。

> Local-first XMU TronClass check-in toolkit with a Python daemon, a browser userscript,
> and a WeChat notification relay. GPS check-in supports target coordinates from the API,
> a verified local cache, or distance-based estimation when the server provides distances.

**使用范围：仅用于本人实际到场时的快速签到、测试与研究，严禁旷课代签、替他人签到或伪造考勤。**
不使用教师凭据，不包含账号密码。完整说明见 [DISCLAIMER.md](DISCLAIMER.md)。

## 本次更新 · v1.1.0（2026-10-22）

- **位置签到自动取坐标**：读取点名接口中的目标经纬度；没有直接坐标时，可使用已验证缓存，或根据服务端返回的距离计算候选坐标并提交。
- **回查确认**：位置签到提交后查询本人在签到名单中的状态，只有确认 `on_call_fine` 才通知成功。
- **微信运行状态**：新增启动、进入检测窗口和停止通知；窗口消息包含课程、检测时段与门控设置。中继请求失败时有限重试。
- **本地缓存**：计算并签到成功的坐标按课程保存；旧坐标失效时继续尝试其他可用路径。

## 支持范围

| 类型 | 守护进程 | 条件与边界 |
| --- | --- | --- |
| 数字码签到 | 自动取码并提交 | 需登录态有效，且详情或课表接口能返回数字码；取不到会通知人工处理 |
| GPS / 雷达位置签到 | 自动获取或计算目标坐标，再回查结果 | 需接口坐标、有效缓存或服务端距离反馈之一可用；也可手动设置 `coords` |
| 二维码签到 | 自动取码，或提交提供的二维码载荷 | 服务端校验真实载荷时需要有效码；无法保证所有租户都能免扫码自动完成 |
| 普通 / 点击签到 | 提交普通签到接口 | 取决于租户支持的接口和权限 |

**本次 GPS 自动取坐标更新在 `changke-daemon/`。浏览器脚本的 GPS 路径仍需在面板中填写坐标。**

默认仅在课表窗口前后各 10 分钟内查询签到，每 20 秒一轮；窗口外不查签到列表，仍会定期校时。
提交前默认等待已签人数达到 4 人，并设置超时和临近结束的保底规则。

## 快速开始

需要 **Python 3.10+**，仅使用标准库，无需安装 Python 依赖。
macOS 可复用本机已登录的畅课 App；使用浏览器 Cookie 时，在配置中选择 `session_source=config`。

```bash
git clone https://github.com/zxyzhk/tronclass-autosigner.git
cd tronclass-autosigner
```

### 1. 配置微信中继

```bash
cd clawbot
cp config.example.json config.json
chmod 600 config.json
# 编辑本机 config.json，选择 provider 并填写对应凭据
python3 clawbot.py check
python3 clawbot.py test
python3 clawbot.py serve
```

支持 Server酱、PushPlus、WxPusher、企业微信和自定义 webhook。
通道配置、启动方式与排障见 [clawbot/README.md](clawbot/README.md)。
保持中继运行，再用另一个终端启动守护进程。

### 2. 准备课表与守护进程配置

将个人课表放到 `changke-daemon/课程表.json`，结构见 [tools/gen_schedule.py](tools/gen_schedule.py) 开头的示例。
文件也可放在其他位置，并通过本机 `config.json` 的 `schedule_path` 指定。

```bash
cd changke-daemon
cp config.example.json config.json
chmod 600 config.json
# macOS 已登录畅课 App：保留 session_source=auto
# Cookie 模式：填写 base_url、cookies / x_session_id，并设置 session_source=config
python3 changke_daemon.py check
python3 changke_daemon.py once --dry-run
python3 changke_daemon.py serve
```

`--dry-run` 仍会读取接口、记录日志，并可能发送检测提醒，但不会提交签到。
详细配置、后台运行与 macOS 自启见 [changke-daemon/README.md](changke-daemon/README.md)。

### 3. 可选：浏览器辅助面板

在 Tampermonkey 中导入 `changke-autosign.user.js`，打开并登录畅课网页版。
面板支持二维码粘贴、门控与微信推送设置。若守护进程已负责轮询，建议关闭脚本的自动监控，避免重复提交。

要把课表写入浏览器脚本，在仓库根目录运行：

```bash
python3 tools/gen_schedule.py changke-daemon/课程表.json --in-place changke-autosign.user.js
```

生成后的脚本会包含个人课程名称，仅在本机使用，不应作为通用发布文件上传。

## 位置签到如何自动完成

目标是本次签到的**经纬度**，不包含街道地址解析。守护进程依次使用：

1. **手动配置 `coords`**：填写后优先使用；提交失败会报告该坐标的错误，不再覆盖为计算结果。
2. **接口直接坐标**：从点名详情或签到列表读取 `latitude` / `longitude`。
3. **课程缓存**：尝试本课程上次计算成功的坐标；每次仍须回查本人签到状态。
4. **距离定位**：提交两个已知参考点，使用越界响应中的 `distance` 求两圆交点，再尝试候选坐标；必要时有限次精化。

所有成功路径均需“提交成功 + 名单确认”。计算成功的坐标保存在本机 `state.json` 的 `radar_cache` 中。
**没有有效坐标，且服务端不返回 `distance` 时，程序无法自动定位**；会记录原因，有限重试后通过微信提示失败，请手动签到或在本机配置正确坐标。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `coords` | 空 | 手动指定 `纬度,经度`，优先级最高 |
| `radar_auto` | `true` | 启用基于距离的定位；关闭后仍可使用手动、接口或缓存坐标 |
| `radar_probe_coords` | `24.3,118.0;24.6,118.2` | 两个通用示例参考点，可在本机改为学校附近两点；不是教师的真实位置 |
| `radar_tolerance_m` | `5.0` | 求圆交点时允许的距离舍入误差，**不是**签到允许半径 |
| `max_attempts` | `20` | 同一点名最多尝试轮数，每轮可能包含多次接口请求 |

服务端距离可能被舍入、隐藏或受权限限制，因此不承诺固定定位精度，也不承诺所有学校都能自动完成。
原理与接口记录见 [逆向笔记 §12](docs/畅课签到逆向笔记.md#12-位置签到gps雷达自动取坐标2026-10-22)。

## 二维码签到

优先尝试服务端取码；如果服务端校验 `data`，仍需取得有效载荷，无法在客户端自行生成服务端密钥或签名。
现有校验探测按每次运行、每个点名执行，接受结论只在本次运行中复用；详情见逆向笔记 §11。

浏览器面板可粘贴载荷，也可使用守护进程的本地接口：

```bash
curl http://127.0.0.1:8788/qr -H 'Content-Type: application/json' \
  -d '{"payload":"<有效二维码载荷>"}'
# 在 changke-daemon/ 中运行的等价命令：
python3 changke_daemon.py qr-payload '<有效二维码载荷>'
```

本地载荷接口默认仅监听 `127.0.0.1:8788`；可设置 `qr_api_token`。

## 微信通知

通知包括检测到签到、签到成功、失败或需要人工处理，以及守护进程启动、进入检测窗口与停止。
位置签到成功以名单回查为准；数字码和二维码路径保留原有接口响应判据。

守护进程日志中的“已推送”表示**本地中继已接受请求**，实际微信送达还取决于中继、第三方通道和账号配置。
停止通知适用于正常退出、可捕获的停止信号或异常；强制杀死进程、断电等情况无法保证发送。

## 测试

```bash
cd changke-daemon
python3 tests/run_tests.py
```

测试使用临时示例课表、本地假畅课和模拟通知中继，不需要个人登录态，不调用真实签到或微信服务。
覆盖数字码、门控、历史记录跳过、窗口外零签到查询、二维码分支，以及 GPS 直接坐标、距离定位和缺少距离时的处理。
本版本通过 **9 个端到端场景的 53 项检查，以及 9 项内存回归，共 62 项检查**；
内存回归另覆盖手动优先、缓存、名单确认、精化、空跑和通知失败后重试。
测试结果验证实现与模拟协议；实际租户权限和微信最终送达需在自己的配置环境中确认。

## 隐私与文件

| 路径 | 内容 |
| --- | --- |
| `changke-daemon/` | 守护进程、配置示例和测试 |
| `changke-autosign.user.js` | 浏览器辅助脚本，内置示例课表 |
| `clawbot/` | 本地微信通知中继 |
| `docs/畅课签到逆向笔记.md` | 接口与实现记录 |
| `tools/gen_schedule.py` | 将本机课表生成浏览器脚本配置 |

仓库只包含通用代码、示例和测试夹具。真实课表、登录态、微信凭据、设备标识、运行日志与对话记录不随发布上传。
`config.json`、`课程表.json`、`state.json` 和日志均由 `.gitignore` 排除。
运行时的坐标缓存、课程提醒和日志可能包含课程或位置数据，请留在本机，并按自己的需要配置通知通道。

## 许可与参考

[MIT License](LICENSE)。研究线索参考 `spicysuimai/XMU-AC`、`alkali210/XMU-Rollcall-Bot`、
`wilinz/fuck_tronclass_sign`、`CUC-Life-Hack/tronclass-hack`、`hot-YUser/auto-Tronclass-VPS`。
