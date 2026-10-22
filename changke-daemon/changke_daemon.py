#!/usr/bin/env python3
"""畅课后台轮询签到（不依赖浏览器）

为什么需要它：浏览器标签页在后台会被降频（Chrome 约 1 次/分钟），可能错过短时限签到；
关掉浏览器就完全停摆。本进程常驻后台，用你的会话 Cookie 直接轮询，不受浏览器限制。

特性
  · 复用浏览器导出的 Cookie（**不存账号密码**，不碰登录流程）
  · 以服务端 Date 响应头校时：**不修改系统时间**，所有时间判断走校时后的 now()
  · 只在课表窗口内轮询（上课/课间前后 10 分钟，窗口内 20 秒一次），窗口外零签到请求
  · 门控：已签人数 >= N（默认 4）才提交；保底规则防漏签
  · 数字签到全自动；GPS 优先取接口/缓存坐标，支持基于距离的定位；二维码支持载荷提交
  · 所有事件推送本地 clawbot -> 你的微信
  · 仅标准库，无需 pip 安装

用法
  python3 changke_daemon.py check         # 体检：会话/课表/clawbot 是否就绪
  python3 changke_daemon.py once          # 立即跑一轮（忽略窗口，便于测试）
  python3 changke_daemon.py test-notify   # 发一条测试推送到微信
  python3 changke_daemon.py serve         # 常驻（默认）
  加 --dry-run 只记录不提交，适合先空跑观察
"""
from __future__ import annotations

import argparse
import email.utils
import math
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

import app_session   # 同目录模块：读取本机畅课 App 的已登录会话（只读）
import netfix        # 同目录模块：优先 IPv4，避开不通的 IPv6 路由

VERSION = "1.1.0"
MAX_BODY = 512 * 1024
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

DEFAULTS = {
    "base_url": "",                 # 必填：你学校的畅课域名，如 https://courses.xxx.edu.cn
    "cookies": "",                  # 可选：从浏览器复制的 Cookie 头（整串）
    "session_source": "auto",       # auto/app/config；auto=配置留空时自动复用本机 App 登录态
    "x_session_id": "",             # 可选：站点若校验该头，一并复制过来
    "schedule_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "课程表.json"),
    "clawbot_url": "http://127.0.0.1:8787/notify",
    "clawbot_token": "",            # clawbot 设了 api_token 时必填
    "notify_enabled": True,
    "tz_offset_min": 480,           # 学校时区 UTC+8
    "pad_min": 10,                  # 窗口前后放宽
    "merge_gap_min": 25,            # 同一天内间隔 <= 该值的课合并为一段
    "gate_min_signed": 4,           # 已签人数 >= 该值才提交
    "gate_max_wait_s": 120,         # 保底：等待超过该秒数直接签
    "gate_min_remain_s": 30,        # 保底：距结束不足该秒数立即签
    "poll_in_window_s": 20,         # 窗口内轮询间隔
    "window_check_s": 5,            # 调度器检查间隔
    "clock_resync_s": 1800,         # 校时间隔
    "max_attempts": 20,             # 同一点名最多尝试次数
    "qr_fetch": True,               # 尝试从服务端取二维码 token
    "qr_probe_validation": True,    # 取不到 token 时，探测服务端是否真的校验 data（决定性实验）
    "qr_api_enabled": True,         # 本地载荷接口：任何来源的二维码载荷都能喂进来直接提交
    "qr_api_port": 8788,
    "qr_api_token": "",             # 可选：本地载荷接口的共享令牌
    "qr_poll_s": 0.7,               # 二维码取码频率（token 只活几秒）
    "coords": "",                   # 手动指定位置签到坐标 "纬度,经度"（优先级最高）
    "radar_auto": True,             # 位置签到：用"两圆交点"自动反推老师坐标
    "radar_probe_coords": "24.3,118.0;24.6,118.2",  # 通用示例探针；可在本机配置为学校附近两点
    "radar_tolerance_m": 5.0,       # 两圆不相交/包含时的误差容忍（米）
    "force_ipv4": True,             # 优先 IPv4：避开 IPv6 路由不通导致的每次 ~10s 超时
    "timeout_s": 10,
    "state_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
    "log_path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "changke.log"),
    "log_max_bytes": 5 * 1024 * 1024,
}

STOP = threading.Event()


# --------------------------------------------------------------------- 工具

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = os.environ.get("CHANGKE_CONFIG",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key, value in data.items():
                    if key in cfg and value not in (None, ""):
                        cfg[key] = value
        except (OSError, ValueError) as exc:
            print(f"[warn] 读取 {path} 失败：{exc}", file=sys.stderr)
    for key in DEFAULTS:
        env_key = "CHANGKE_" + key.upper()
        if os.environ.get(env_key):
            raw = os.environ[env_key]
            try:
                cfg[key] = int(raw) if isinstance(DEFAULTS[key], int) else raw
            except ValueError:
                pass
    cfg["_config_path"] = path
    return cfg


def jload(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def mask(value) -> str:
    text = str(value or "")
    if not text:
        return "(未设置)"
    return text[:6] + "***" + text[-2:] if len(text) > 10 else "***"


def as_list(value, keys=()):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
    return []


LOGLOCK = threading.Lock()


def make_logger(cfg: dict):
    path = cfg["log_path"]
    limit = int(cfg["log_max_bytes"])

    def log(msg: str, bad: bool = False) -> None:
        # 用学校当地时间打戳：避免把 UTC 误读成"早了 8 小时"
        stamp = datetime.fromtimestamp(
            (now_ms(cfg) + int(cfg["tz_offset_min"]) * 60000) / 1000,
            tz=timezone.utc).strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        print(line, flush=True)
        try:
            with LOGLOCK:
                if os.path.exists(path) and os.path.getsize(path) > limit:
                    os.replace(path, path + ".1")
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass

    return log


# ------------------------------------------------------------------ 时钟

_CLOCK = {"offset_ms": 0.0, "synced_at": 0.0, "rtt_ms": None}


def now_ms(cfg: dict) -> float:
    return time.time() * 1000.0 + _CLOCK["offset_ms"]


def sync_clock(http, cfg: dict, log, force: bool = False) -> bool:
    if not force and _CLOCK["synced_at"] and \
            time.time() - _CLOCK["synced_at"] < int(cfg["clock_resync_s"]):
        return True
    t0 = time.time()
    status, _, headers = http.get("/api/profile")
    t1 = time.time()
    raw = headers.get("Date") or headers.get("date")
    if not raw:
        status, _, headers = http.get("/")
        raw = headers.get("Date") or headers.get("date")
    if not raw:
        return False
    try:
        server = email.utils.parsedate_to_datetime(raw).timestamp() * 1000.0
    except (TypeError, ValueError):
        return False
    rtt = (t1 - t0) * 1000.0
    raw_skew = server - t1 * 1000.0
    # 半程时延补偿只在链路快时可信；RTT 很大时上限 500ms，避免用假偏差污染判定
    correction = min(rtt / 2.0, 500.0)
    _CLOCK["offset_ms"] = raw_skew + correction
    _CLOCK["synced_at"] = time.time()
    _CLOCK["rtt_ms"] = rtt
    log(f"[校时] 本机原始偏差 {raw_skew/1000:+.1f}s，时延补偿 +{correction/1000:.2f}s"
        f"（RTT {rtt:.0f}ms）→ 采用偏差 {_CLOCK['offset_ms']/1000:+.1f}s；未改动系统时间")
    return True


# ------------------------------------------------------------------ 二维码载荷
# 客户端静态取证：新版 scanner-jumper 直接 JSON.parse(p)；
# 老版是 !k~v! 紧凑编码（逆向笔记 §3）。两种形态都要认。

QR_FIELD_ORDER = ["courseId", "activityId", "activityType", "data", "rollcallId",
                  "groupSetId", "accessCode", "action", "enableGroupRollcall",
                  "createUser", "joinCourse"]
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(number: int) -> str:
    if number < 36:
        return _B36[number]
    out = ""
    while number:
        number, rem = divmod(number, 36)
        out = _B36[rem] + out
    return out


def _as_json_payload(text: str):
    raw = str(text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and "rollcallId" in obj else None


def _as_compact_payload(text: str):
    raw = str(text or "")
    if "~" not in raw:
        return None
    na, ra, ea, ta = chr(26), chr(16), chr(31), chr(30)
    keymap = {_b36(index): name for index, name in enumerate(QR_FIELD_ORDER)}
    valmap = {na + _b36(index + 2): name
              for index, name in enumerate(["classroom-exam", "feedback", "vote"])}
    out = {}
    for part in [piece for piece in raw.split("!") if piece]:
        if "~" not in part:
            continue
        key, value = part.split("~", 1)
        name = keymap.get(key, key)
        if value.startswith(na):
            if value == na + "1":
                decoded = True
            elif value == na + "0":
                decoded = False
            else:
                decoded = valmap.get(value, value)
        elif value.startswith(ra):
            numbers = []
            for piece in value[1:].split("."):
                try:
                    numbers.append(int(piece, 36))
                except ValueError:
                    pass
            decoded = (float(f"{numbers[0]}.{numbers[1]}") if len(numbers) > 1
                       else (numbers[0] if numbers else value))
        else:
            decoded = value.replace(ea, "~").replace(ta, "!")
        out[name] = decoded
    return out if "rollcallId" in out else None


def parse_qr_payload(text: str):
    """解析任意形态的二维码载荷：JSON / 紧凑串 / URL 或深链（_wg_p、p、_p）。"""
    if not text:
        return None
    raw = str(text).strip()
    for parser in (_as_json_payload, _as_compact_payload):
        found = parser(raw)
        if found:
            return found
    for key in ("_wg_p", "p", "_p"):
        for match in re.finditer(r"[?&]" + key + r"=([^&]+)", raw):
            candidate = urllib.parse.unquote(match.group(1)).strip()
            for parser in (_as_json_payload, _as_compact_payload):
                found = parser(candidate)
                if found:
                    return found
    return None


# ------------------------------------------------------------ 位置签到几何
# 开源做法（XMU-Rollcall-Bot / XMU-AC）：越界提交会返回与目标点的 distance，
# 用两次探针的距离求两圆交点，即可反推出老师的位置；再用候选点逐个提交验证。
EARTH_R = 6371000.0


def _latlon_to_xy(lat, lon, lat0, lon0):
    x = math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * EARTH_R
    return x, y


def _xy_to_latlon(x, y, lat0, lon0):
    lat = lat0 + math.degrees(y / EARTH_R)
    lon = lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def circle_intersections(x1, y1, d1, x2, y2, d2, tolerance=5.0):
    """两圆交点；在 tolerance 米内不相交/包含时微调半径使其相切。"""
    d1, d2 = float(d1), float(d2)
    dist = math.hypot(x2 - x1, y2 - y1)
    if dist < 1e-9:
        return None
    if dist > d1 + d2:
        # 两圆分离：仅在 GPS 误差容忍内接受，按"相切"处理
        if dist - (d1 + d2) > tolerance:
            return None
    elif dist < abs(d1 - d2):
        # 一圆包含另一圆：同上
        if abs(d1 - d2) - dist > tolerance:
            return None
    a = (d1 ** 2 - d2 ** 2 + dist ** 2) / (2 * dist)
    # 关键：不人为放大半径。相切/近似相切时 h 截断为 0 —— 唯一解就是切点，
    # 上游代码用 +0.1 放大半径会凭空偏移十几米。
    h = math.sqrt(max(0.0, d1 ** 2 - a ** 2))
    xm = x1 + a * (x2 - x1) / dist
    ym = y1 + a * (y2 - y1) / dist
    rx = -(y2 - y1) * (h / dist)
    ry = (x2 - x1) * (h / dist)
    return (xm + rx, ym + ry), (xm - rx, ym - ry)


def solve_target(lat1, lon1, lat2, lon2, d1, d2, tolerance=5.0):
    lat0, lon0 = (lat1 + lat2) / 2, (lon1 + lon2) / 2
    x1, y1 = _latlon_to_xy(lat1, lon1, lat0, lon0)
    x2, y2 = _latlon_to_xy(lat2, lon2, lat0, lon0)
    sols = circle_intersections(x1, y1, d1, x2, y2, d2, tolerance)
    if not sols:
        return None
    return (_xy_to_latlon(sols[0][0], sols[0][1], lat0, lon0),
            _xy_to_latlon(sols[1][0], sols[1][1], lat0, lon0))


def school_local(cfg: dict, ts_ms: float = None) -> datetime:
    ms = now_ms(cfg) if ts_ms is None else ts_ms
    return datetime.fromtimestamp((ms + int(cfg["tz_offset_min"]) * 60000) / 1000,
                                  tz=timezone.utc)


DOW_NAME = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

# 我的状态里属于"已终态"的取值（签上/请假/已结束/已关闭）
TERMINAL_STATUSES = {"finished", "expired", "closed", "on_call_fine", "on_personal_leave"}

# 点名"正在进行"的可能取值
ACTIVE_ROLLCALL_STATUSES = {"in_progress", "on_going", "active", "started", "waiting"}


def parse_time_ms(text):
    """把接口时间统一解析成 epoch 毫秒。

    实测：Date 响应头是 RFC 2822（"Mon, 19 Oct 2026 08:35:00 GMT"），
    而 end_time / rollcall_time 是 ISO 8601（"2026-10-16T02:55:27Z"）——
    parsedate_to_datetime 对后者会抛 TypeError，必须两种都支持。
    解析失败返回 None（调用方据此跳过"距结束"判断，而不是当成已结束）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw).timestamp() * 1000.0
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ------------------------------------------------------------------ 课表

class Schedule:
    """读课程表 JSON，编译成 {星期: [[起,止]]} 分钟窗口（不判断学期周次）"""

    def __init__(self, cfg: dict, log):
        self.log = log
        self.cfg = cfg
        self.windows = {}
        self.used = 0
        self.load()

    def load(self) -> None:
        path = self.cfg["schedule_path"]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            self.log(f"[课表] 读取失败 {path}：{exc}", True)
            self.windows = {}
            return

        day_map = {"星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4,
                   "星期五": 5, "星期六": 6, "星期日": 0}
        by_day: dict[int, list] = {}
        by_day_courses: dict[int, list] = {}
        merged_by_day: dict[int, list] = {}
        for course in data.get("courses") or []:
            day = day_map.get(str(course.get("day", "")))
            span = str(course.get("time", "")).split("~")
            if day is None or len(span) != 2:
                continue
            try:
                start = int(span[0][:2]) * 60 + int(span[0][3:5])
                end = int(span[1][:2]) * 60 + int(span[1][3:5])
            except ValueError:
                continue
            by_day.setdefault(day, []).append([start, end])
            by_day_courses.setdefault(day, []).append(
                {"s": start, "e": end, "name": str(course.get("name") or "")})

        pad = int(self.cfg["pad_min"])
        gap = int(self.cfg["merge_gap_min"])
        windows = {}
        for day, spans in by_day.items():
            spans.sort()
            merged = []
            for start, end in spans:
                if merged and start - merged[-1][1] <= gap:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            windows[day] = [[s - pad, e + pad] for s, e in merged]
            merged_by_day[day] = merged
            self.used += len(spans)
        self.windows = windows
        # 供窗口通知使用：课程名 + 未加宽的合并课段
        self.courses = by_day_courses
        self.sessions = {day: list(spans) for day, spans in merged_by_day.items()}

    def names_for(self, day: int, win_start: int, win_end: int) -> list:
        """窗口内涉及的课程名（按开课时间排序，去重）。"""
        pad = int(self.cfg["pad_min"])
        lo, hi = win_start + pad, win_end - pad
        seen, names = set(), []
        for course in self.courses.get(day, []):
            if course["s"] < hi and course["e"] > lo:
                name = course["name"]
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def state(self, ts_ms: float = None):
        cfg = self.cfg
        local = school_local(cfg, ts_ms)
        dow = (local.weekday() + 1) % 7          # 周一=1 ... 周日=0
        minutes = local.hour * 60 + local.minute
        today = self.windows.get(dow, [])
        for start, end in today:
            if start <= minutes <= end:
                return {"in": True, "win": (start, end), "dow": dow, "minutes": minutes}
        nxt = None
        for offset in range(0, 8):
            if nxt:
                break
            day = (dow + offset) % 7
            for start, end in self.windows.get(day, []):
                if offset == 0 and start <= minutes:
                    continue
                nxt = {"delta": offset * 1440 + start - minutes, "at": start,
                       "end": end, "day": day}
                break
        return {"in": False, "next": nxt, "dow": dow, "minutes": minutes}


# ------------------------------------------------------------------ 状态

class QrApi:
    """本地载荷接口：任何来源的二维码载荷都能喂进来直接提交。

    POST /qr   {"payload": "<任意形态载荷>"}  或  {"rollcallId": "...", "data": "..."}
      payload 支持：JSON、紧凑串（!k~v!）、URL/深链（/j?p=、scanner-jumper、_wg_p）
    仅监听 127.0.0.1；可用 qr_api_token 加共享令牌。
    """

    def __init__(self, daemon, log, port: int, token: str = ""):
        self.daemon = daemon
        self.log = log
        self.server = None
        self.thread = None
        self.port = int(port)
        self.token = token or ""

    def _handler(self):
        import http.server

        daemon, log, token = self.daemon, self.log, self.token

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path.split("?")[0] == "/health":
                    self._send(200, {"ok": True, "service": "changke-qr-api"})
                else:
                    self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self.path.split("?")[0] != "/qr":
                    self._send(404, {"ok": False, "error": "not found"})
                    return
                if token and self.headers.get("X-QR-Token", "") != token:
                    self._send(401, {"ok": False, "error": "bad token"})
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"ok": False, "error": "bad json"})
                    return
                result = daemon.submit_qr_payload(
                    payload_text=body.get("payload"),
                    rid=body.get("rollcallId"),
                    data=body.get("data"))
                log(f"[载荷接口] {json.dumps(result, ensure_ascii=False)}", not result.get("ok"))
                self._send(200 if result.get("ok") else 502, result)

        return Handler

    def start(self) -> bool:
        import http.server
        try:
            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port),
                                                          self._handler())
        except OSError as exc:
            self.log(f"[载荷接口] 无法监听 127.0.0.1:{self.port} —— {exc}", True)
            return False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="qr-api", daemon=True)
        self.thread.start()
        self.log(f"[载荷接口] 已启动 http://127.0.0.1:{self.port}/qr"
                 "（POST {'payload': ...} 即可直接提交二维码）")
        return True

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class State:
    def __init__(self, path: str):
        self.path = path
        self.seen = set()
        self.attempts = {}
        self.gate_since = {}
        self.radar_cache = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.seen = set(str(x) for x in (data.get("seen") or []))
            self.attempts = {str(k): int(v) for k, v in (data.get("attempts") or {}).items()}
            self.radar_cache = {str(k): [float(v[0]), float(v[1])]
                                for k, v in (data.get("radar_cache") or {}).items()
                                if isinstance(v, (list, tuple)) and len(v) == 2}
        except (OSError, ValueError, TypeError):
            pass

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"seen": sorted(self.seen), "attempts": self.attempts,
                           "radar_cache": self.radar_cache}, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ------------------------------------------------------------------ HTTP

class HTTP:
    def __init__(self, cfg: dict, on_unauthorized=None):
        self.base = str(cfg["base_url"]).rstrip("/")
        self.cfg = cfg
        self.on_unauthorized = on_unauthorized
        # ProxyHandler({}) 强制直连：避免失效的系统代理把请求拦成 ProxyError
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.timeout = int(cfg["timeout_s"])

    def req(self, method: str, path: str, payload=None, extra_headers=None,
            _retry: bool = True):
        url = path if path.startswith("http") else self.base + path
        data = None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": UA,
        }
        if self.cfg["cookies"]:
            headers["Cookie"] = str(self.cfg["cookies"])
        if self.cfg["x_session_id"]:
            headers["x-session-id"] = str(self.cfg["x_session_id"])
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=utf-8"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                body = resp.read(MAX_BODY).decode("utf-8", "replace")
                return resp.status, body, {k: v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(MAX_BODY).decode("utf-8", "replace")
            except Exception:
                pass
            headers = {k: v for k, v in (exc.headers or {}).items()}
            if exc.code == 401 and _retry and self.on_unauthorized \
                    and self.on_unauthorized():
                return self.req(method, path, payload, extra_headers, _retry=False)
            return exc.code, body, headers
        except Exception as exc:
            return 0, f"{type(exc).__name__}: {exc}", {}

    def get(self, path): return self.req("GET", path)
    def post(self, path, payload=None): return self.req("POST", path, payload)
    def put(self, path, payload=None): return self.req("PUT", path, payload)


# ------------------------------------------------------------------ 通知

def notify(cfg: dict, log, title: str, body: str, dedupe: set,
           retries: int = 3) -> bool:
    """推送一条通知；失败时有界重试（网络抖动很常见，不能一次失败就丢消息）。

    返回本地中继是否接受。仅接受成功后标记去重，实际微信送达取决于推送通道。
    """
    if not cfg["notify_enabled"] or not cfg["clawbot_url"]:
        return False
    key = f"{title}|{body}"
    if key in dedupe:
        return True                     # 已通知过
    payload = {"title": title, "body": body, "source": "changke-daemon",
               "event": "changke", "at": datetime.fromtimestamp(
                   now_ms(cfg) / 1000, tz=timezone.utc).isoformat()}
    headers = {}
    if cfg["clawbot_token"]:
        headers["X-Clawbot-Token"] = str(cfg["clawbot_token"])
    url = str(cfg["clawbot_url"])
    # 仅在剩余重试前等待：默认三次尝试，间隔 2s / 5s。
    delays = [2, 5, 10]
    last_error = ""
    attempts = max(1, retries)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", **headers})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=8) as resp:
                resp.read(4096)
            dedupe.add(key)
            log(f"[微信] 已推送：{title}" + (f"（第 {attempt + 1} 次尝试成功）" if attempt else ""))
            return True
        except Exception as exc:
            last_error = type(exc).__name__
            if attempt + 1 < attempts:
                delay = delays[min(attempt, len(delays) - 1)]
                log(f"[微信] 推送失败（{last_error}），{delay}s 后重试…", True)
                time.sleep(delay)
    log(f"[微信] 推送最终失败（{last_error}）——请检查网络与 clawbot", True)
    return False


# ------------------------------------------------------------------ 核心

def resolve_app_session(cfg: dict, log) -> dict | None:
    """按 session_source 决定是否复用本机畅课 App 的登录态。

    auto（默认）：config 未提供 base_url/cookies 时才用 App 会话 —— 你只要在
    本机 App 里登录过，守护进程就能直接用，不必手抄 Cookie。
    """
    source = str(cfg.get("session_source") or "auto").lower()
    if source == "config":
        return None
    if source == "auto" and cfg.get("base_url") and cfg.get("cookies"):
        return None
    info = app_session.read()
    if not info:
        if source == "app":
            log("[会话] session_source=app，但没找到本机 App 的登录会话", True)
        return None
    # 日志里不写出租户域名与设备号，避免把可识别信息落盘
    log(f"[会话] 已复用本机畅课 App 的登录态"
        f"（sessionID {len(info['session_id'])} 字符，域名已自动识别）")
    cfg["base_url"] = info["api_url"]
    cfg["x_session_id"] = info["session_id"]
    cfg["_device_uuid"] = info["device_uuid"]
    cfg["_app_session"] = info
    return info


class Daemon:
    def __init__(self, cfg: dict, log, dry_run: bool = False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run
        self.app = resolve_app_session(cfg, log)
        self.started_at = now_ms(cfg)      # 停止通知里要报"本次运行时长"
        self.renewing = False
        self.last_renew = 0.0

        def on_unauthorized() -> bool:
            """会话失效时用 App 的刷新令牌续期；返回 True 表示已换新会话、可重试。"""
            if not self.app or self.renewing:
                return False
            if time.time() - self.last_renew < 300:
                return False            # 冷却：5 分钟内至多续期一次
            self.renewing = True
            self.last_renew = time.time()
            try:
                session, detail = app_session.renew(self.app)
                if not session:
                    self.log(f"[会话] 续期失败：{detail}", True)
                    return False
                self.app["session_id"] = session
                self.cfg["x_session_id"] = session
                self.http.cfg["x_session_id"] = session
                self.log(f"[会话] {detail}")
                return True
            finally:
                self.renewing = False

        self.http = HTTP(cfg, on_unauthorized=on_unauthorized)
        self.schedule = Schedule(cfg, log)
        self.state = State(cfg["state_path"])
        self.dedupe: set = set()
        self.sid = None
        self.courses = []
        self.courses_at = 0.0
        self.qr_targets: dict[str, dict] = {}
        self.qr_gate: dict[str, dict] = {}
        self.qr_probe: dict[str, bool] = {}   # rid -> 是否已做过"服务端是否校验"探测
        self.win_notified: set = set()        # 已推送过"开始检测"的窗口
        self.win_retry_at = 0.0               # 窗口通知失败后的重试冷却
        self.state_lock = threading.Lock()
        self.last_poll = 0.0
        self.active = False

    # -- 身份与课程 ------------------------------------------------------
    def ensure_identity(self) -> bool:
        if self.sid:
            return True
        status, body, _ = self.http.get("/api/profile")
        data = jload(body)
        if status == 200 and isinstance(data, dict):
            sid = data.get("id") or data.get("user_id")
            if sid:
                self.sid = str(sid)
                self.log(f"[身份] student_id={self.sid}")
                return True
        self.log(f"[身份] 无法读取 /api/profile（HTTP {status}）：Cookie 可能已过期，"
                 "请重新从浏览器复制", True)
        return False

    def load_courses(self, force: bool = False) -> list:
        if self.courses and not force and time.time() * 1000 - self.courses_at < 600000:
            return self.courses
        sem = {}
        status, body, _ = self.http.get("/api/current-semester-info")
        data = jload(body)
        if status == 200 and isinstance(data, dict):
            semester = data.get("semester") or {}
            year = data.get("academic_year") or {}
            if semester.get("id") is not None:
                sem = {"semester_id": [str(semester["id"])],
                       "academic_year_id": [str(year.get("id"))] if year.get("id") else []}
        conditions = {"keyword": "", "classify_type": "recently_started",
                      "display_studio_list": False, **sem}
        payload = {"conditions": conditions, "fields": "id,name,display_name",
                   "page": 1, "page_size": 30, "showScorePassedStatus": False}
        status, body, _ = self.http.post("/api/my-courses", payload)
        courses = as_list(jload(body), ("courses", "data"))
        if not courses:
            status, body, _ = self.http.post(
                "/api/my-courses", {"fields": "id,name,display_name", "page": 1, "page_size": 30})
            courses = as_list(jload(body), ("courses", "data"))
        seen = set()
        self.courses = [c for c in courses
                        if isinstance(c, dict) and c.get("id") and
                        not (str(c["id"]) in seen or seen.add(str(c["id"])))]
        self.courses_at = time.time() * 1000
        if self.courses:
            self.log(f"[课程] 已加载 {len(self.courses)} 门")
        return self.courses

    # -- 点名信息 --------------------------------------------------------
    def rollcalls_of(self, cid) -> list:
        status, body, _ = self.http.get(
            f"/api/course/{cid}/student/{self.sid}/rollcalls?page=1&page_size=20")
        if status != 200:
            return []
        return as_list(jload(body), ("rollcalls", "data"))

    def detail(self, rid):
        status, body, _ = self.http.get(f"/api/rollcall/{rid}/student_rollcalls")
        if status != 200:
            return None
        data = jload(body)
        return data if isinstance(data, dict) else None

    def signed_stats(self, detail):
        roster = None
        if isinstance(detail, dict):
            for key in ("student_rollcalls", "students"):
                if isinstance(detail.get(key), list):
                    roster = detail[key]
                    break
        if not roster:
            return None
        signed = sum(1 for s in roster
                     if isinstance(s, dict) and s.get("rollcall_status") == "on_call_fine")
        me = None
        for student in roster:
            if not isinstance(student, dict):
                continue
            for key in ("student_id", "user_id", "id"):
                if student.get(key) is not None and str(student[key]) == str(self.sid):
                    me = student
                    break
            if me:
                break
        if me and me.get("rollcall_status") == "on_call_fine":
            return None                     # 已签上，无需门控
        return {"signed": signed, "total": len(roster), "end_time": detail.get("end_time")}

    def number_code_from_timetable(self, rid, cid, rollcall_time):
        date = datetime.fromtimestamp(now_ms(self.cfg) / 1000,
                                      tz=timezone.utc).strftime("%Y-%m-%d")
        if rollcall_time:
            stamp = parse_time_ms(rollcall_time)
            if stamp is not None:
                date = datetime.fromtimestamp(stamp / 1000.0,
                                              tz=timezone.utc).strftime("%Y-%m-%d")
        status, body, _ = self.http.get(
            f"/api/timetable_rollcalls?course_ids={urllib.parse.quote(str(cid))}"
            f"&rollcall_date={date}")
        if status != 200:
            return None
        items = as_list(jload(body), ("rollcalls",))
        for item in items:
            if isinstance(item, dict) and str(item.get("id")) == str(rid):
                code = item.get("number_code")
                return str(code) if code else None
        return None

    # -- 提交 ------------------------------------------------------------
    def answer_number(self, rid, code) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] 本应提交数字签到 rid={rid} code={code}")
            return True
        status, body, _ = self.http.put(f"/api/rollcall/{rid}/answer_number_rollcall",
                                        {"deviceId": self.device_id(), "numberCode": str(code)})
        ok = status == 200
        self.log(f"[提交] 数字签到 rid={rid} code={code} -> HTTP {status}" + ("" if ok else f" {body[:120]}"), not ok)
        return ok

    def answer_plain(self, rid) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] 本应提交普通签到 rid={rid}")
            return True
        status, body, _ = self.http.put(
            f"/api/rollcall/{rid}/answer?api_version=1.76", {"deviceId": self.device_id()})
        ok = status == 200
        self.log(f"[提交] 普通/其他 rid={rid} -> HTTP {status}", not ok)
        return ok

    def answer_radar(self, rid, lat, lon) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] 本应提交 GPS 签到 rid={rid} ({lat},{lon})")
            return True
        status, body, _ = self.http.put(f"/api/rollcall/{rid}/answer", {
            "accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
            "deviceId": self.device_id(), "heading": None,
            "latitude": str(lat), "longitude": str(lon), "speed": None})
        ok = status == 200
        self.log(f"[提交] GPS 签到 rid={rid} -> HTTP {status}", not ok)
        return ok

    def answer_qr(self, rid, token) -> bool:
        if self.dry_run:
            self.log(f"[dry-run] 本应提交二维码签到 rid={rid}")
            return True
        status, body, _ = self.http.put(f"/api/rollcall/{rid}/answer_qr_rollcall",
                                        {"data": token, "deviceId": self.device_id()})
        ok = status == 200
        self.log(f"[提交] 二维码 rid={rid} -> HTTP {status}", not ok)
        return ok

    def device_id(self) -> str:
        app_uuid = str(self.cfg.get("_device_uuid") or "")
        if app_uuid:
            return app_uuid          # 与 App 自身一致，避免设备 ID 异常
        path = os.path.join(os.path.dirname(self.cfg["state_path"]), "device-id")
        try:
            if os.path.exists(path):
                value = open(path, encoding="utf-8").read().strip()
                if value:
                    return value
        except OSError:
            pass
        value = str(uuid.uuid4())
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(value)
        except OSError:
            pass
        return value

    # -- 位置签到（GPS/雷达）---------------------------------------------
    def _radar_payload(self, lat, lon):
        return {"accuracy": 35, "altitude": 0, "altitudeAccuracy": None,
                "deviceId": self.device_id(), "heading": None,
                "latitude": str(lat), "longitude": str(lon), "speed": None}

    def _radar_submit(self, rid, lat, lon):
        """提交一次位置签到；返回 (status, distance, body)。越界时服务端会给 distance。"""
        status, body, _ = self.http.put(f"/api/rollcall/{rid}/answer",
                                        self._radar_payload(lat, lon))
        distance = None
        data = jload(body)
        if isinstance(data, dict):
            distance = data.get("distance")
            if distance is None and isinstance(data.get("data"), dict):
                distance = data["data"].get("distance")
        return status, distance, body

    def _radar_confirmed(self, rid) -> bool:
        """回查名单确认本人已签上。"""
        detail = self.detail(rid)
        roster = detail.get("student_rollcalls") if isinstance(detail, dict) else None
        if not roster:
            return False
        for student in roster:
            if str(student.get("student_id")) == str(self.sid):
                return student.get("rollcall_status") == "on_call_fine"
        return False

    def _probe_coords(self):
        points = []
        for part in str(self.cfg.get("radar_probe_coords") or "").split(";"):
            if "," in part:
                left, right = part.split(",", 1)
                try:
                    points.append((float(left), float(right)))
                except ValueError:
                    pass
        if len(points) < 2:
            points = [(24.3, 118.0), (24.6, 118.2)]
        return points[0], points[1]

    def answer_radar_auto(self, rid, cid, name, detail=None, rc=None):
        """位置签到：四级优先级取坐标。

        0) config.coords 显式指定
        1) 接口直接给的 latitude/longitude
        2) 本课程上次解出的缓存坐标
        3) 三角定位（两次探针 distance → 两圆交点 → 逐个候选验证）
        返回 (是否成功, 说明)。
        """
        cfg = self.cfg
        if self.dry_run:
            self.log(f"[dry-run] 本应做位置签到 rid={rid}")
            return False, "dry-run"

        # 0) 显式配置
        raw = str(cfg.get("coords") or "")
        if "," in raw:
            left, right = raw.split(",", 1)
            try:
                lat, lon = float(left), float(right)
            except ValueError:
                lat = lon = None
            if lat is not None:
                status, _, _ = self._radar_submit(rid, lat, lon)
                if status == 200 and self._radar_confirmed(rid):
                    return True, f"配置坐标({lat}, {lon})"
                return False, f"配置坐标提交 HTTP {status}"

        # 1) 接口里直接带坐标
        for src, label in ((detail, "点名详情"), (rc, "签到列表")):
            if isinstance(src, dict):
                lat, lon = src.get("latitude"), src.get("longitude")
                if lat not in (None, "", 0, "0") and lon not in (None, "", 0, "0"):
                    status, _, _ = self._radar_submit(rid, lat, lon)
                    if status == 200 and self._radar_confirmed(rid):
                        return True, f"{label}坐标({lat}, {lon})"
                    self.log(f"[位置] {label}坐标提交 HTTP {status}", True)

        # 2) 缓存坐标
        cached = self.state.radar_cache.get(str(cid))
        if cached:
            status, _, _ = self._radar_submit(rid, cached[0], cached[1])
            if status == 200 and self._radar_confirmed(rid):
                return True, f"缓存坐标({cached[0]:.6f}, {cached[1]:.6f})"

        # 3) 三角定位
        if not cfg.get("radar_auto", True):
            return False, "未开启三角定位，且无可用坐标"
        (lat1, lon1), (lat2, lon2) = self._probe_coords()
        status1, d1, _ = self._radar_submit(rid, lat1, lon1)
        if status1 == 200 and self._radar_confirmed(rid):
            return True, "探针 1 直接命中（未限制范围）"
        status2, d2, _ = self._radar_submit(rid, lat2, lon2)
        if status2 == 200 and self._radar_confirmed(rid):
            return True, "探针 2 直接命中（未限制范围）"

        if d1 is None or d2 is None:
            return False, ("服务端未返回 distance，三角定位不可用"
                           f"（HTTP {status1}/{status2}）")
        self.log(f"[位置] 探针距离 d1={d1}m d2={d2}m")
        solutions = solve_target(lat1, lon1, lat2, lon2, float(d1), float(d2),
                                 float(cfg.get("radar_tolerance_m", 5.0)))
        if not solutions:
            return False, f"两圆无交点（d1={d1}, d2={d2}）"
        def _haversine(p, q):
            la1, lo1 = math.radians(p[0]), math.radians(p[1])
            la2, lo2 = math.radians(q[0]), math.radians(q[1])
            dlat, dlon = la2 - la1, lo2 - lo1
            a = (math.sin(dlat / 2) ** 2
                 + math.cos(la1) * math.cos(la2) * math.sin(dlon / 2) ** 2)
            return 2 * EARTH_R * math.asin(math.sqrt(a))

        tolerance = float(cfg.get("radar_tolerance_m", 5.0))
        for index, cand in enumerate(solutions):
            current = (float(cand[0]), float(cand[1]))
            # 第一个候选做迭代精化：把当前估计当作新探针，用服务端返回的
            # distance 反解一次 —— 估计点离目标越近，几何条件越好，误差收敛很快。
            for step in range(3 if index == 0 else 1):
                status, dist, _ = self._radar_submit(rid, current[0], current[1])
                if status == 200 and self._radar_confirmed(rid):
                    self.state.radar_cache[str(cid)] = [current[0], current[1]]
                    self.state.save()
                    extra = f"·精化{step}次" if step else ""
                    return True, f"三角定位({current[0]:.6f}, {current[1]:.6f}){extra}"
                if dist is None or step >= 2:
                    break
                refined = solve_target(lat1, lon1, current[0], current[1],
                                       float(d1), float(dist), tolerance)
                if not refined:
                    break
                current = min(refined, key=lambda p: _haversine(p, current))
        return False, "候选坐标均未确认成功"

    # -- 门控 ------------------------------------------------------------
    def gate(self, rid):
        cfg = self.cfg
        detail = self.detail(rid)
        stats = self.signed_stats(detail)
        if not stats:
            return True, "查不到签到人数，直接签"
        need = max(1, int(cfg["gate_min_signed"]))
        since = self.state.gate_since.get(str(rid))
        if since is None:
            since = now_ms(cfg)
            self.state.gate_since[str(rid)] = since
        waited = (now_ms(cfg) - since) / 1000.0
        msg = f"已签 {stats['signed']}/{stats['total']}"
        if stats["signed"] >= need:
            return True, f"{msg}，已达 {need} 人，开签"
        end_time = stats.get("end_time")
        remain = None
        if end_time:
            stamp = parse_time_ms(end_time)
            if stamp is not None:
                remain = (stamp - now_ms(cfg)) / 1000.0
        if waited >= int(cfg["gate_max_wait_s"]):
            return True, f"{msg}，等待超 {cfg['gate_max_wait_s']}s，直接签"
        if remain is not None and remain <= int(cfg["gate_min_remain_s"]):
            return True, f"{msg}，剩余 {remain:.0f}s，直接签"
        return False, f"{msg}，未达 {need} 人，继续等"

    # -- 只读诊断 --------------------------------------------------------
    def diagnose(self) -> None:
        if not self.ensure_identity():
            print("会话无效，无法诊断")
            return
        courses = self.load_courses()
        print(f"课程 {len(courses)} 门；下列为课程签到列表的原始情况（本地时间：学校时区）")
        total = 0
        for course in courses:
            cid = course.get("id")
            name = course.get("display_name") or course.get("name") or str(cid)
            rows = self.rollcalls_of(cid)
            if not rows:
                continue
            print(f"\n— {name}（{len(rows)} 条）")
            for rc in rows:
                rid = rc.get("id") or rc.get("rollcall_id")
                detail = self.detail(rid)
                dstatus = detail.get("status") if isinstance(detail, dict) else None
                end = detail.get("end_time") if isinstance(detail, dict) else None
                code = detail.get("number_code") if isinstance(detail, dict) else None
                total += 1
                print(f"   rid={rid}  我的状态={rc.get('status')}  type={rc.get('type')}"
                      f"  num={bool(rc.get('is_number'))} radar={bool(rc.get('is_radar'))}"
                      f" qr={bool(rc.get('is_qr'))}")
                print(f"        点名状态={dstatus}  end_time={end}  number_code={code}"
                      f"  rollcall_time={rc.get('rollcall_time')}")
        print(f"\n合计 {total} 条签到记录")


    # -- 生命周期通知 ----------------------------------------------------
    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        total = int(max(0, seconds))
        hours, rem = divmod(total, 3600)
        minutes, secs = divmod(rem, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

    def window_brief(self) -> str:
        """一句话概括当前窗口状态与下一个窗口还有多久。"""
        state = self.schedule.state()
        if state["in"]:
            return (f"当前【窗口内】{hhmm(state['win'][0])}~{hhmm(state['win'][1])}"
                    f"（{DOW_NAME[state['dow']]}），{self.cfg['poll_in_window_s']} 秒轮询中")
        nxt = state["next"]
        if not nxt:
            return "当前【窗口外】，近 7 天无课"
        away = self._fmt_duration(nxt["delta"] * 60)
        return (f"当前【窗口外】；下一个窗口 {DOW_NAME[nxt['day']]} "
                f"{hhmm(nxt['at'])}~{hhmm(nxt['end'])}（约 {away} 后开始）")

    def notify_start(self) -> None:
        local = school_local(self.cfg)
        body = "\n".join([
            f"启动时间：{local.strftime('%m-%d %H:%M')}（北京时间）",
            self.window_brief(),
            f"门控：满 {self.cfg['gate_min_signed']} 人后自动签到",
            f"轮询：仅在窗口内，每 {self.cfg['poll_in_window_s']} 秒一次",
            "收到停止信号时会再推一条（含本次运行时长）",
        ])
        notify(self.cfg, self.log, "🟢 畅课守护进程已启动", body, set())

    def notify_window_start(self, state: dict) -> None:
        """进入检测窗口时推送：哪节课、窗口区间、本次持续多久、门控设置。"""
        day, (win_start, win_end) = state["dow"], state["win"]
        now_minutes = state["minutes"]
        local = school_local(self.cfg)
        key = f"win:{local.strftime('%Y-%m-%d')}:{day}:{win_start}"
        if key in self.win_notified:
            return
        if now_ms(self.cfg) - self.win_retry_at < 60000:
            return                          # 失败后 60s 内不重复打（窗口内每轮都会被调用）

        names = self.schedule.names_for(day, win_start, win_end)
        remain = max(0, win_end - now_minutes)
        body = "\n".join(filter(None, [
            f"课程：{'、'.join(names)}" if names else "",
            f"检测窗口：{hhmm(win_start)}~{hhmm(win_end)}（{DOW_NAME[day]}，"
            f"课程时段前后各放宽 {self.cfg['pad_min']} 分钟）",
            f"本次持续：约 {self._fmt_duration(remain * 60)}（到 {hhmm(win_end)} 为止）",
            f"轮询：每 {self.cfg['poll_in_window_s']} 秒一次",
            f"门控：满 {self.cfg['gate_min_signed']} 人后自动签",
        ]))
        sent = notify(self.cfg, self.log,
                      f"🟢 开始 {self.cfg['poll_in_window_s']} 秒检测 · {DOW_NAME[day]}",
                      body, set())
        if sent:
            self.win_notified.add(key)      # 只在送达后标记；失败则下轮再试
        else:
            self.win_retry_at = now_ms(self.cfg)
            self.log("[微信] 窗口开始通知未送达，60s 后重试", True)

    def notify_stop(self, reason: str = "") -> None:
        local = school_local(self.cfg)
        body = "\n".join([
            f"停止时间：{local.strftime('%m-%d %H:%M')}（北京时间）",
            f"本次运行：{self._fmt_duration((now_ms(self.cfg) - self.started_at) / 1000.0)}",
            f"原因：{reason or '正常退出'}",
        ])
        notify(self.cfg, self.log, "🔴 畅课守护进程已停止", body, set())

    def _skip(self, rid: str) -> None:
        """标记为已处理：不再重复查询、也不再提醒。"""
        self.state.seen.add(rid)
        self.state.attempts.pop(rid, None)
        self.state.gate_since.pop(rid, None)
        self.qr_targets.pop(rid, None)
        self.qr_gate.pop(rid, None)

    def _ended(self, end_time) -> bool:
        stamp = parse_time_ms(end_time)
        return stamp is not None and stamp <= now_ms(self.cfg)


    # -- 一轮 ------------------------------------------------------------
    def poll(self) -> None:
        if not self.ensure_identity():
            return
        courses = self.load_courses()
        if not courses:
            return
        cfg = self.cfg
        for course in courses:
            cid = course.get("id")
            name = course.get("display_name") or course.get("name") or str(cid)
            for rc in self.rollcalls_of(cid):
                if not isinstance(rc, dict):
                    continue
                rid = str(rc.get("id") or rc.get("rollcall_id") or "")
                if not rid or rid in self.state.seen:
                    continue

                is_number = bool(rc.get("is_number")) and not rc.get("is_radar")
                is_radar = bool(rc.get("is_radar"))
                is_qr = "qr" in str(rc.get("type") or "").lower()

                # 所有类型都取详情：用「点名本身」的状态判断是否正在进行。
                # 实测：课程签到列表里会混着大量历史记录（我的状态=absent 但点名早已 finished），
                # 若不加这道门就会把它们当成待签，既浪费时间还会误发提醒。
                detail = self.detail(rid)
                detail_status = detail.get("status") if isinstance(detail, dict) else None
                end_time = detail.get("end_time") if isinstance(detail, dict) else None

                if rc.get("status") in TERMINAL_STATUSES:
                    self._skip(rid)                 # 我已签上/请假 → 静默跳过
                    continue
                if detail is None:
                    continue                        # 详情拿不到：本轮不下判断，下轮再试
                if detail_status not in ACTIVE_ROLLCALL_STATUSES:
                    self._skip(rid)                 # 点名不在进行中 → 历史记录，静默跳过
                    continue
                if end_time and self._ended(end_time):
                    self._skip(rid)                 # 结束时间已过 → 同上
                    continue

                if is_number or is_radar:
                    notify(cfg, self.log, f"🎯 检测到签到 · {name}",
                           f"{'数字签到' if is_number else 'GPS/雷达签到'}已发起，"
                           f"等满 {cfg['gate_min_signed']} 人后自动签", self.dedupe)
                    ok, msg = self.gate(rid)
                    if not ok:
                        if int(self.state.attempts.get(rid, 0)) == 0:
                            self.log(f"[等待] {name}：{msg}")
                        continue

                if is_radar and self.dry_run:
                    self.log(f"[dry-run] 本应做位置签到 rid={rid}")
                    continue

                self.state.attempts[rid] = int(self.state.attempts.get(rid, 0)) + 1
                attempts = self.state.attempts[rid]

                if is_number:
                    code = None
                    if isinstance(detail, dict) and detail.get("number_code"):
                        code = str(detail["number_code"])
                    if not code:
                        code = self.number_code_from_timetable(rid, cid, rc.get("rollcall_time"))
                    if not code:
                        if attempts >= int(cfg["max_attempts"]):
                            self.state.seen.add(rid)
                            self.log(f"[放弃] {name} 取不到数字码（尝试 {attempts} 次）", True)
                            notify(cfg, self.log, f"❌ 数字签到我处理不了 · {name}",
                                   "取不到数字码，请手动签到", self.dedupe)
                        elif attempts == 1:
                            self.log(f"[取码] {name} 数字码尚未下发，将重试", True)
                        continue
                    if self.answer_number(rid, code):
                        self.state.seen.add(rid)
                        self.state.gate_since.pop(rid, None)
                        notify(cfg, self.log, f"✅ 数字签到成功 · {name}",
                               f"已自动提交，码={code}", self.dedupe)
                    elif attempts >= int(cfg["max_attempts"]):
                        self.state.seen.add(rid)
                        notify(cfg, self.log, f"❌ 数字签到提交失败 · {name}",
                               f"已重试 {attempts} 次，请手动签到", self.dedupe)
                    continue

                if is_radar:
                    ok, reason = self.answer_radar_auto(rid, cid, name,
                                                        detail=detail, rc=rc)
                    if ok:
                        self._skip(rid)
                        notify(cfg, self.log, f"✅ 位置签到成功 · {name}", reason, self.dedupe)
                    elif attempts >= int(cfg["max_attempts"]):
                        self._skip(rid)
                        notify(cfg, self.log, f"❌ 位置签到未成功 · {name}",
                               f"{reason}；可在 config.json 填 coords 手动指定", self.dedupe)
                    elif attempts == 1:
                        self.log(f"[位置] {name}：{reason}，将重试", True)
                    continue

                if is_qr:
                    if cfg.get("_qr_unvalidated"):
                        # 已验证服务端不校验 data ⇒ 直接提交占位 data，全自动
                        self.submit_qr_payload(
                            rid=rid, data=str(int(now_ms(cfg) // 1000)) + "f" * 32)
                        continue
                    self.qr_targets[rid] = {"cid": cid, "name": name}
                    notify(cfg, self.log, f"📷 二维码签到 · {name}",
                           "已尝试自动取码；取不到会做一次服务端校验探测", self.dedupe)
                    continue

                if self.answer_plain(rid):
                    self.state.seen.add(rid)
                elif attempts >= int(cfg["max_attempts"]):
                    self.state.seen.add(rid)
                    notify(cfg, self.log, f"⚠️ 需要你处理 · {name}",
                           "该签到类型无法自动处理，请手动签到", self.dedupe)

        self.state.save()

    def submit_qr_payload(self, payload_text=None, rid=None, data=None):
        """提交二维码签到：可直接给 {rollcallId, data}，也可给任意形态的载荷文本。

        返回 dict；调用方（本地接口/快循环）据此告知结果。
        """
        if payload_text:
            parsed = parse_qr_payload(payload_text)
            if not parsed:
                return {"ok": False, "error": "unparsed_payload"}
            rid = str(parsed.get("rollcallId"))
            data = str(parsed.get("data") or "")
        if not rid or not data:
            return {"ok": False, "error": "missing_rollcallId_or_data"}
        if self.dry_run:
            self.log(f"[dry-run] 本应提交二维码载荷 rid={rid}")
            return {"ok": True, "dry_run": True, "rollcallId": str(rid)}
        status, body, _ = self.http.put(
            f"/api/rollcall/{rid}/answer_qr_rollcall",
            {"data": data, "deviceId": self.device_id()})
        code = ""
        payload = jload(body)
        if isinstance(payload, dict):
            for key in ("error", "code", "message", "error_code"):
                if payload.get(key):
                    code = str(payload[key])
                    break
        if status == 200:
            with self.state_lock:
                self.state.seen.add(str(rid))
                self.state.save()
            self.log(f"[提交] 二维码载荷提交成功 rid={rid}")
            notify(self.cfg, self.log, "✅ 二维码签到成功（载荷提交）",
                   f"rid={rid}", set())
        else:
            self.log(f"[提交] 二维码载荷被拒 rid={rid} -> HTTP {status} {code}", True)
        return {"ok": status == 200, "http": status, "errorCode": code, "rollcallId": str(rid)}

    def probe_qr_validation(self, rid):
        """决定性实验：服务端到底有没有真的校验 data？

        发一条"形状合法（10 位秒 + 32 位 hex）但 hash 伪造"的 data：
          · 200 → 服务端不校验 ⇒ 免扫码提交可行，之后所有二维码点名都可自动签
          · invalid_create_time_hash / no_request_data / qr_code_expired → 确实校验
        """
        fake = str(int(now_ms(self.cfg) // 1000)) + "f" * 32
        if self.dry_run:
            self.log(f"[dry-run] 本应做校验探测 rid={rid}")
            return False, 0, "dry-run"
        status, body, _ = self.http.put(
            f"/api/rollcall/{rid}/answer_qr_rollcall",
            {"data": fake, "deviceId": self.device_id()})
        code = ""
        payload = jload(body)
        if isinstance(payload, dict):
            for key in ("error", "code", "message", "error_code"):
                if payload.get(key):
                    code = str(payload[key])
                    break
        if status == 200:
            self.cfg["_qr_unvalidated"] = True
            self.log("[校验探测] 服务端返回 200：**它不校验 data** ⇒ 二维码可全自动提交")
            notify(self.cfg, self.log, "🔓 服务端不校验二维码 data",
                   "伪造 data 也被接受，之后二维码点名将自动签到", set())
            with self.state_lock:
                self.state.seen.add(str(rid))
                self.state.save()
        else:
            self.log(f"[校验探测] 服务端拒绝伪造 data：HTTP {status} 错误码={code or '(未返回)'}"
                     " ⇒ 确实在校验，无法自行构造", True)
            notify(self.cfg, self.log, "🔒 服务端确实校验二维码 data",
                   f"错误码 {code or '(未返回)'}：只能人工提供真实码内容", set())
        return status == 200, status, code


    # -- 二维码快循环 ----------------------------------------------------
    def qr_tick(self) -> None:
        cfg = self.cfg
        for rid, target in list(self.qr_targets.items()):
            if cfg["qr_fetch"]:
                cached = self.qr_gate.get(rid)
                if not cached or now_ms(cfg) - cached["at"] > 5000:
                    ok, msg = self.gate(rid)
                    self.qr_gate[rid] = {"ok": ok, "at": now_ms(cfg)}
                    if not ok:
                        continue
                status, body, _ = self.http.get(f"/api/course/{target['cid']}/rollcall/{rid}/qr_code")
                data = jload(body)
                token = ""
                if isinstance(data, dict):
                    token = str(data.get("data") or data.get("qr_code") or "")
                if not token:
                    if status in (403, 404):
                        self.qr_targets.pop(rid, None)
                        self.state.seen.add(rid)
                        self.log(f"[权限] {target['name']} 取码接口 HTTP {status}：读不到 token", True)
                        # 免扫码的最后一次机会：服务端到底校验不校验 data（每个点名最多一次）
                        if cfg.get("qr_probe_validation", True) and rid not in self.qr_probe:
                            self.qr_probe[rid] = True
                            self.probe_qr_validation(rid)   # 内部会按结果发通知
                        else:
                            notify(cfg, self.log, f"📷 二维码需要你出手 · {target['name']}",
                                   f"取码接口 HTTP {status}，需人工提供码内容", self.dedupe)
                    continue
                try:
                    age = now_ms(cfg) - int(token[:10]) * 1000
                except ValueError:
                    age = 0
                if age > 3000:
                    continue                       # 已过期，下一轮重取
                if self.answer_qr(rid, token):
                    self.qr_targets.pop(rid, None)
                    self.state.seen.add(rid)
                    notify(cfg, self.log, f"✅ 二维码签到成功 · {target['name']}",
                           "已自取码提交（未拍照）", self.dedupe)

    # -- 调度 ------------------------------------------------------------
    def loop(self) -> None:
        cfg = self.cfg
        sync_clock(self.http, cfg, self.log, force=True)
        self.log(f"[启动] 畅课后台轮询 v{VERSION}｜窗口内 {cfg['poll_in_window_s']}s｜"
                 f"门控 {cfg['gate_min_signed']} 人" + ("（dry-run）" if self.dry_run else ""))
        self.notify_start()                  # 启动即推送（含窗口/持续时间信息）
        api = None
        if cfg.get("qr_api_enabled", True):
            api = QrApi(self, self.log, int(cfg["qr_api_port"]),
                        str(cfg.get("qr_api_token") or ""))
            if not api.start():
                api = None
        last_qr = 0.0
        while not STOP.is_set():
            state = self.schedule.state()
            if not state["in"]:
                if self.active:
                    self.active = False
                    nxt = state["next"]
                    self.log("[课表] 离开窗口，暂停轮询"
                             + (f"（下一个：{DOW_NAME[nxt['day']]} {hhmm(nxt['at'])}~{hhmm(nxt['end'])}）"
                                if nxt else "（近 7 天无课）"))
                sync_clock(self.http, cfg, self.log)
                self._sleep(int(cfg["window_check_s"]))
                continue

            if not self.active:
                self.active = True
                local = school_local(cfg)
                self.log(f"[课表] 进入窗口 {hhmm(state['win'][0])}~{hhmm(state['win'][1])}"
                         f"（{DOW_NAME[state['dow']]} {local.strftime('%H:%M')}）——开始轮询")
                sync_clock(self.http, cfg, self.log, force=True)

            # 窗口内每轮都尝试（内部有"已送达"与 60s 冷却双重判断）：
            # 这样即使通知发送时网络抖动，也会在窗口内自动补发
            self.notify_window_start(state)

            if (now_ms(cfg) - self.last_poll) / 1000.0 >= int(cfg["poll_in_window_s"]):
                self.last_poll = now_ms(cfg)
                try:
                    self.poll()
                except Exception as exc:                 # 单轮异常不拖垮常驻进程
                    self.log(f"[错误] 轮询异常：{type(exc).__name__}: {exc}", True)

            if self.qr_targets:
                qr_poll = float(cfg["qr_poll_s"]) * 1000
                if now_ms(cfg) - last_qr >= qr_poll:
                    last_qr = now_ms(cfg)
                    try:
                        self.qr_tick()
                    except Exception as exc:
                        self.log(f"[错误] 取码异常：{type(exc).__name__}: {exc}", True)

            self._sleep(1)

    def _sleep(self, seconds: float) -> None:
        STOP.wait(max(0.2, float(seconds)))

    def once(self) -> None:
        sync_clock(self.http, self.cfg, self.log, force=True)
        self.log("[单轮] 忽略课表窗口，立即执行一轮")
        self.poll()
        if self.qr_targets:
            self.qr_tick()


# ------------------------------------------------------------------ CLI

def cmd_check(cfg: dict, log) -> int:
    print(f"畅课后台轮询 v{VERSION}")
    print(f"配置文件    : {cfg['_config_path']}"
          f"{'（已存在）' if os.path.exists(cfg['_config_path']) else '（不存在，用默认值）'}")
    ok = True

    # 先解析会话来源：session_source=auto 且未填 Cookie 时，直接复用本机 App 的登录态
    app = resolve_app_session(cfg, log)
    if app:
        print(f"会话来源    : 本机畅课 App（{app['org_name'] or app['bundle']}）")
        print(f"App 续期    : "
              + ("可用（refresh_token 已就绪）" if app['refresh_token'] else "不可用"))
    else:
        print("会话来源    : config.json 里的 Cookie")
    print(f"站点        : {cfg['base_url'] or '（未设置）'}")
    print(f"Cookie      : {mask(cfg['cookies'])}")
    print(f"x-session-id: {mask(cfg['x_session_id'])}")
    if not cfg["base_url"] or not (cfg["cookies"] or cfg["x_session_id"]):
        print("❌ 缺少会话：请在 config.json 填 base_url + cookies，"
              "或先在本机畅课 App 里登录一次")
        return 1
    http = HTTP(cfg)
    if sync_clock(http, cfg, log, force=True):
        local = school_local(cfg)
        print(f"校时        : 已对齐；学校时间 {local.strftime('%Y-%m-%d %H:%M:%S')}"
              f"（本机偏差 {_CLOCK['offset_ms']/1000:+.1f}s）")
    else:
        print("⚠️ 校时失败：将用本机时间判断窗口")
        ok = False
    status, body, _ = http.get("/api/profile")
    data = jload(body)
    if status == 200 and isinstance(data, dict) and (data.get("id") or data.get("user_id")):
        print(f"会话        : ✅ 有效，student_id={data.get('id') or data.get('user_id')}")
    else:
        print(f"会话        : ❌ 无效（HTTP {status}）——Cookie 过期或站点不对")
        return 1
    schedule = Schedule(cfg, log)
    if schedule.windows:
        print(f"课表        : ✅ {cfg['schedule_path']}（{schedule.used} 门课）")
        state = schedule.state()
        if state["in"]:
            print(f"当前窗口    : 窗口内 {hhmm(state['win'][0])}~{hhmm(state['win'][1])}"
                  f"（{DOW_NAME[state['dow']]}）")
        else:
            nxt = state["next"]
            print("当前窗口    : 窗口外，下一个 "
                  + (f"{DOW_NAME[nxt['day']]} {hhmm(nxt['at'])}~{hhmm(nxt['end'])}" if nxt else "近 7 天无课"))
    else:
        print(f"课表        : ❌ 读取失败 {cfg['schedule_path']}")
        ok = False
    if cfg["notify_enabled"] and cfg["clawbot_url"]:
        try:
            parts = urllib.parse.urlsplit(str(cfg["clawbot_url"]))
            req = urllib.request.Request(f"{parts.scheme}://{parts.netloc}/health")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=5) as resp:
                health = jload(resp.read(8192).decode("utf-8", "replace")) or {}
            ready = health.get("ready")
            print(f"clawbot     : {'✅ 就绪' if ready else '⚠️ 通道凭据未配置'}"
                  f"（provider={health.get('provider')}）")
            if not ready:
                ok = False
        except Exception as exc:
            print(f"clawbot     : ❌ 不可达（{type(exc).__name__}）")
            ok = False
    else:
        print("clawbot     : 已关闭推送")
    print("结论        : " + ("✅ 可以启动 serve" if ok else "⚠️ 先修上面标 ❌/⚠️ 的项"))
    return 0 if ok else 1


def cmd_clock(cfg: dict, log) -> int:
    resolve_app_session(cfg, log)
    http = HTTP(cfg)
    status, _body, headers = http.get("/api/profile")
    now_local = datetime.now().astimezone()
    print(f"本机显示时间  : {now_local.strftime('%Y-%m-%d %H:%M:%S')}  {now_local.tzname()}  (epoch {now_local.timestamp():.0f})")
    raw = headers.get("Date") or headers.get("date")
    if raw:
        server = email.utils.parsedate_to_datetime(raw)
        drift = (datetime.now(timezone.utc) - server.astimezone(timezone.utc)).total_seconds()
        print(f"畅课服务端时间: {server.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}  UTC")
        print(f"本机时钟偏差  : {drift:+.1f} s（不改它，只在内存里补偿）")
    else:
        print("畅课服务端时间: (响应未带 Date 头)")
    sync_clock(http, cfg, log, force=True)
    local = school_local(cfg)
    print(f"学校当地时间  : {local.strftime('%Y-%m-%d %H:%M:%S')}  UTC+{int(cfg['tz_offset_min']) // 60}"
          "   <- 窗口判定用的就是它")
    schedule = Schedule(cfg, log)
    state = schedule.state()
    if state["in"]:
        print(f"窗口状态      : 窗口内 {hhmm(state['win'][0])}~{hhmm(state['win'][1])}"
              f"（{DOW_NAME[state['dow']]}）→ 正在 20s 轮询")
    else:
        nxt = state["next"]
        print("窗口状态      : 窗口外，下一个 "
              + (f"{DOW_NAME[nxt['day']]} {hhmm(nxt['at'])}~{hhmm(nxt['end'])}"
                 if nxt else "近 7 天无课"))
    print("说明          : 上述判断均基于服务端时间；本机时钟与系统设置全程未被改动")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="畅课后台轮询签到")
    parser.add_argument("action", nargs="?", default="serve",
                        choices=["serve", "once", "check", "test-notify", "app-session",
                                 "clock", "rollcalls", "qr-payload"])
    parser.add_argument("extra", nargs="*", help="qr-payload 的载荷文本")
    parser.add_argument("--dry-run", action="store_true", help="只记录，不真的提交签到")
    args = parser.parse_args()

    cfg = load_config()
    if cfg.get("force_ipv4", True):
        netfix.prefer_ipv4()
    log = make_logger(cfg)

    if args.action == "check":
        return cmd_check(cfg, log)
    if args.action == "clock":
        return cmd_clock(cfg, log)
    if args.action == "qr-payload":
        text = " ".join(args.extra).strip()
        if not text and not sys.stdin.isatty():
            text = sys.stdin.read().strip()
        if not text:
            print("用法: python3 changke_daemon.py qr-payload '<载荷>'  （也可用管道喂入）")
            return 1
        resolve_app_session(cfg, log)
        daemon = Daemon(cfg, log, dry_run=args.dry_run)
        result = daemon.submit_qr_payload(payload_text=text)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    if args.action == "rollcalls":
        resolve_app_session(cfg, log)
        Daemon(cfg, log, dry_run=True).diagnose()
        return 0
    if args.action == "app-session":
        info = app_session.read()
        if not info:
            print("❌ 未找到本机畅课 App 的登录会话（请先在 App 里登录一次）")
            return 1
        print(app_session.summarize(info))
        status, body = app_session.probe(info, info["session_id"])
        print(f"\n只读探针 GET {info['api_url']}/api/profile -> HTTP {status}")
        if status == 200:
            print("✅ 会话有效：守护进程可直接用它操作，无需 Cookie")
            return 0
        print("⚠️ 会话无效，尝试续期 …")
        session, detail = app_session.renew(info)
        print(("✅ " if session else "❌ ") + detail)
        return 0 if session else 1
    if args.action == "test-notify":
        notify(cfg, log, "changke-daemon 测试", "如果你在微信看到这条，说明推送链路已打通。", set())
        return 0

    daemon = Daemon(cfg, log, dry_run=args.dry_run)

    reason = {"text": ""}

    def on_signal(signum, frame):
        reason["text"] = f"收到信号 {signum}"
        log(f"[停止] 收到信号 {signum}，正在退出")
        STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    if args.action == "once":
        daemon.once()
        return 0

    try:
        daemon.loop()
    except Exception as exc:                      # 异常也要让微信知道
        log(f"[错误] 主循环异常退出：{type(exc).__name__}: {exc}", True)
        reason["text"] = f"异常退出（{type(exc).__name__}）"
        raise
    finally:
        daemon.notify_stop(reason.get("text") or "正常退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
