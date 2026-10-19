#!/usr/bin/env python3
"""clawbot — 本地微信推送机器人（通知转发中继）

链路：
    油猴脚本 / 任意本地程序
        --(POST http://127.0.0.1:8787/notify)-->
    clawbot（本进程）
        --(HTTPS)-->
    推送通道 --> 你的微信

支持的通道（provider）：
    wecom       企业微信群机器人 webhook（最稳，免费；消息落在企业微信 App）
    serverchan  Server酱 SendKey（经微信公众号推送，**直接进微信**）
    pushplus    PushPlus token（经微信公众号推送，**直接进微信**）
    wxpusher    WxPusher appToken + uid（经微信公众号推送，**直接进微信**）
    generic     任意 webhook，POST JSON（自建服务 / 其它推送平台）

特性：仅标准库；默认只绑定 127.0.0.1；可选共享令牌；去重；限速；不打印任何密钥。

用法：
    python3 clawbot.py check          # 查看解析后的配置（密钥打码）
    python3 clawbot.py test           # 发一条测试消息，验证通道是否打通
    python3 clawbot.py serve          # 启动中继服务（默认）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_BODY_BYTES = 64 * 1024
LOG_MAX_BYTES = 5 * 1024 * 1024

SECRET_FIELDS = (
    "wecom_webhook", "serverchan_key", "pushplus_token",
    "wxpusher_app_token", "api_token", "generic_url",
)

DEFAULTS = {
    "provider": "generic",
    "host": DEFAULT_HOST,
    "port": DEFAULT_PORT,
    "api_token": "",            # 可选：非空则 /notify 必须带 X-Clawbot-Token
    "dedupe_window_s": 20,      # 同内容在该秒数内只推一次
    "rate_per_min": 30,         # 每分钟最多推送条数
    "timeout_s": 10,
    # --- 各通道凭据（只填你实际用的那个）---
    "wecom_webhook": "",        # https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
    "serverchan_key": "",       # SCTxxxxxx
    "pushplus_token": "",       # xxxxxxxx
    "wxpusher_app_token": "",   # AT_xxxxxx
    "wxpusher_uid": "",         # UID_xxxxxx
    "generic_url": "",          # 任意 webhook
    "generic_headers": {},      # 可选附加头
    "prefix": "【畅课】",        # 消息前缀，便于你在微信里识别
}


# --------------------------------------------------------------------- 配置

def config_path() -> str:
    return os.environ.get(
        "CLAWBOT_CONFIG",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
    )


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    cfg["generic_headers"] = dict(DEFAULTS["generic_headers"])
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key, value in data.items():
                    if key in cfg and value not in (None, ""):
                        cfg[key] = value
        except (OSError, ValueError) as exc:
            print(f"[clawbot] 警告：读取 {path} 失败（{exc}），改用默认值", file=sys.stderr)
    # 环境变量覆盖：CLAWBOT_PROVIDER / CLAWBOT_PORT / CLAWBOT_WECOM_WEBHOOK ...
    for key in DEFAULTS:
        env_key = "CLAWBOT_" + key.upper()
        if env_key in os.environ and os.environ[env_key] != "":
            raw = os.environ[env_key]
            if isinstance(DEFAULTS[key], int):
                try:
                    cfg[key] = int(raw)
                except ValueError:
                    pass
            else:
                cfg[key] = raw
    return cfg


def mask(value) -> str:
    """把密钥打码，避免日志/回显泄露。"""
    text = str(value or "")
    if not text:
        return "(未设置)"
    if text.startswith("http"):
        parts = urllib.parse.urlsplit(text)
        query = urllib.parse.parse_qs(parts.query)
        for key in list(query):
            query[key] = ["***"]
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(query, doseq=True), ""))
    if len(text) <= 8:
        return text[:2] + "***"
    return text[:4] + "***" + text[-2:]


def active_provider_ready(cfg: dict) -> tuple[bool, str]:
    provider = cfg["provider"]
    if provider == "wecom":
        return bool(cfg["wecom_webhook"]), "wecom_webhook"
    if provider == "serverchan":
        return bool(cfg["serverchan_key"]), "serverchan_key"
    if provider == "pushplus":
        return bool(cfg["pushplus_token"]), "pushplus_token"
    if provider == "wxpusher":
        return bool(cfg["wxpusher_app_token"] and cfg["wxpusher_uid"]), "wxpusher_app_token + wxpusher_uid"
    if provider == "generic":
        return bool(cfg["generic_url"]), "generic_url"
    return False, f"未知 provider: {provider}"


# ------------------------------------------------------------------ HTTP 发送

def http_post_json(url: str, payload: dict, headers: dict, timeout: float):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        req.add_header(str(key), str(value))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")


def http_post_form(url: str, fields: dict, timeout: float):
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(MAX_BODY_BYTES).decode("utf-8", "replace")


def push(cfg: dict, title: str, body: str) -> dict:
    """把一条消息推送到当前通道。返回 {ok, provider, detail}。"""
    provider = cfg["provider"]
    text = f"{cfg.get('prefix', '')}{title}\n{body}".strip()
    timeout = float(cfg["timeout_s"])
    result = {"ok": False, "provider": provider, "detail": ""}

    try:
        if provider == "wecom":
            status, resp = http_post_json(
                cfg["wecom_webhook"],
                {"msgtype": "text", "text": {"content": text}},
                {}, timeout)
            ok = status == 200 and '"errcode":0' in resp.replace(" ", "")
            result.update(ok=ok, detail=f"HTTP {status} {resp[:180]}")

        elif provider == "serverchan":
            url = f"https://sctapi.ftqq.com/{cfg['serverchan_key']}.send"
            status, resp = http_post_form(
                url, {"title": f"{cfg.get('prefix','')}{title}", "desp": body}, timeout)
            ok = status == 200 and '"code":0' in resp.replace(" ", "")
            result.update(ok=ok, detail=f"HTTP {status} {resp[:180]}")

        elif provider == "pushplus":
            status, resp = http_post_json(
                "https://www.pushplus.plus/send",
                {"token": cfg["pushplus_token"],
                 "title": f"{cfg.get('prefix','')}{title}",
                 "content": body, "template": "txt"},
                {}, timeout)
            ok = status == 200 and '"code":200' in resp.replace(" ", "")
            result.update(ok=ok, detail=f"HTTP {status} {resp[:180]}")

        elif provider == "wxpusher":
            status, resp = http_post_json(
                "https://wxpusher.zjiecode.com/api/send/message",
                {"appToken": cfg["wxpusher_app_token"],
                 "content": f"{cfg.get('prefix','')}{title}\n{body}",
                 "summary": title[:99],
                 "contentType": 1,
                 "uids": [cfg["wxpusher_uid"]]},
                {}, timeout)
            ok = status == 200 and '"code":1000' in resp.replace(" ", "")
            result.update(ok=ok, detail=f"HTTP {status} {resp[:180]}")

        elif provider == "generic":
            status, resp = http_post_json(
                cfg["generic_url"],
                {"title": title, "body": body, "text": text,
                 "source": "clawbot", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
                cfg.get("generic_headers") or {}, timeout)
            result.update(ok=200 <= status < 300, detail=f"HTTP {status} {resp[:180]}")

        else:
            result["detail"] = f"未知 provider: {provider}"

    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        result["detail"] = f"HTTP {exc.code} {snippet[:180]}"
    except Exception as exc:  # 网络/超时等
        result["detail"] = f"{type(exc).__name__}: {exc}"

    return result


# ------------------------------------------------------------- 状态：去重/限速

class State:
    def __init__(self, cfg: dict):
        self.lock = threading.Lock()
        self.cfg = cfg
        self.dedupe: dict[str, float] = {}
        self.sent_times: list[float] = []
        self.last_result: dict | None = None
        self.last_at = ""
        self.dropped = 0

    def accept(self, title: str, body: str) -> tuple[bool, str]:
        now = time.time()
        with self.lock:
            window = float(self.cfg["dedupe_window_s"])
            key = f"{title}|{body}"
            last = self.dedupe.get(key)
            if last and now - last < window:
                self.dropped += 1
                return False, "重复内容，已忽略"
            self.dedupe[key] = now
            if len(self.dedupe) > 500:
                cutoff = now - max(window, 60)
                self.dedupe = {k: v for k, v in self.dedupe.items() if v >= cutoff}

            per_min = int(self.cfg["rate_per_min"])
            self.sent_times = [t for t in self.sent_times if now - t < 60]
            if len(self.sent_times) >= per_min:
                self.dropped += 1
                return False, f"超过限速（{per_min}/分钟），已忽略"
            self.sent_times.append(now)
        return True, ""

    def note(self, result: dict) -> None:
        with self.lock:
            self.last_result = result
            self.last_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_log(line: str) -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clawbot.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ HTTP 服务

def make_handler(cfg: dict, state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"clawbot/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        def _send(self, code: int, obj: dict) -> None:
            payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self) -> bool:
            token = cfg.get("api_token") or ""
            if not token:
                return True
            return self.headers.get("X-Clawbot-Token", "") == token

        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                ready, need = active_provider_ready(cfg)
                self._send(200, {
                    "ok": True, "version": VERSION, "provider": cfg["provider"],
                    "ready": ready, "needs": need if not ready else "",
                    "last": state.last_result, "last_at": state.last_at,
                    "dropped": state.dropped,
                })
                return
            if path == "/":
                ready, need = active_provider_ready(cfg)
                html = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>clawbot</title>"
                    "<body style='font:14px/1.6 -apple-system,PingFang SC,sans-serif;padding:24px'>"
                    f"<h2>clawbot {VERSION}</h2>"
                    f"<p>provider: <b>{cfg['provider']}</b>，"
                    f"凭据: {'已配置' if ready else '未配置（需要 ' + need + '）'}</p>"
                    f"<p>上次推送: {state.last_at or '（暂无）'}</p>"
                    "<p>POST /notify  body: {\"title\":\"...\",\"body\":\"...\"}</p>"
                    "</body>")
                data = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/notify", "/test"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"ok": False, "error": "bad token"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._send(400, {"ok": False, "error": "bad length"})
                    return
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(data, dict):
                    raise ValueError("body must be an object")
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "error": f"bad json: {exc}"})
                return

            title = str(data.get("title") or ("clawbot 测试" if path == "/test" else "通知"))
            body = str(data.get("body") or ("如果你看到这条，说明通道已打通。" if path == "/test" else ""))
            title = title[:120]
            body = body[:2000]

            if path == "/test":
                ok_ready, need = active_provider_ready(cfg)
                if not ok_ready:
                    self._send(503, {"ok": False, "provider": cfg["provider"],
                                     "error": f"通道未配置，缺少 {need}"})
                    return
                result = push(cfg, title, body)
                state.note(result)
                append_log(json.dumps({"at": state.last_at, "title": title,
                                       "test": True, **result}, ensure_ascii=False))
                self._send(200 if result["ok"] else 502, result)
                return

            allowed, why = state.accept(title, body)
            if not allowed:
                self._send(200, {"ok": False, "skipped": why})
                return

            ready, need = active_provider_ready(cfg)
            if not ready:
                self._send(503, {"ok": False, "provider": cfg["provider"],
                                 "error": f"通道未配置，缺少 {need}"})
                return

            result = push(cfg, title, body)
            state.note(result)
            append_log(json.dumps({"at": state.last_at, "title": title, **result},
                                  ensure_ascii=False))
            self._send(200 if result["ok"] else 502, result)

    return Handler


# ----------------------------------------------------------------------- CLI

def cmd_check(cfg: dict) -> int:
    ready, need = active_provider_ready(cfg)
    print(f"clawbot {VERSION}")
    print(f"配置文件      : {config_path()}  {'（已存在）' if os.path.exists(config_path()) else '（不存在，使用默认值）'}")
    print(f"provider      : {cfg['provider']}  ->  {'✅ 凭据已配置' if ready else '❌ 缺少 ' + need}")
    print(f"监听          : http://{cfg['host']}:{cfg['port']}")
    print(f"去重窗口      : {cfg['dedupe_window_s']}s    限速: {cfg['rate_per_min']}/分钟")
    print(f"共享令牌      : {'已设置' if cfg['api_token'] else '未设置（仅本机可访问）'}")
    print("凭据状态：")
    for field in SECRET_FIELDS:
        if field in cfg:
            print(f"  {field:<20} {mask(cfg[field])}")
    return 0 if ready else 1


def cmd_test(cfg: dict) -> int:
    ready, need = active_provider_ready(cfg)
    if not ready:
        print(f"❌ 通道未配置：provider={cfg['provider']}，需要在 config.json 填 {need}")
        return 1
    print(f"→ 通过 {cfg['provider']} 发送测试消息 ...")
    result = push(cfg, "clawbot 测试", "如果你在微信里看到这条，说明通道已打通。")
    print(("✅ 发送成功  " if result["ok"] else "❌ 发送失败  ") + result["detail"])
    append_log(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "title": "clawbot 测试", "test": True, **result},
                          ensure_ascii=False))
    return 0 if result["ok"] else 1


def cmd_serve(cfg: dict) -> int:
    ready, need = active_provider_ready(cfg)
    state = State(cfg)
    handler = make_handler(cfg, state)
    try:
        server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), handler)
    except OSError as exc:
        print(f"❌ 无法监听 {cfg['host']}:{cfg['port']} — {exc}", file=sys.stderr)
        return 1

    print(f"clawbot {VERSION} 已启动: http://{cfg['host']}:{cfg['port']}")
    print(f"通道: {cfg['provider']}  " + ("✅ 凭据已配置" if ready else f"⚠️ 缺少 {need}（先配好再发消息）"))
    print("状态页: GET /   健康检查: GET /health   推送: POST /notify")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="clawbot — 本地微信推送机器人")
    parser.add_argument("action", nargs="?", default="serve",
                        choices=["serve", "test", "check"])
    args = parser.parse_args()
    cfg = load_config()
    if args.action == "check":
        return cmd_check(cfg)
    if args.action == "test":
        return cmd_test(cfg)
    return cmd_serve(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
