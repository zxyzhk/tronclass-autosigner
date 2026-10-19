#!/usr/bin/env python3
"""端到端测试：把 changke-daemon 对着「假畅课」跑起来，逐场景断言。

覆盖：
  A number_gate  数字签到 + 门控（3 人时等、满 4 人后提交，并且只提交一次）
  B no_code      两条取码通道都拿不到码 -> 不提交，并提醒人工
  C qr_403       二维码取码被拒 -> 提醒人工
  D outside      课表窗口外 -> 完全不查签到接口
所有场景同时校验：Cookie/x-session-id 有带上、服务端校时生效、事件推送到中继。
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(os.path.dirname(HERE), "changke_daemon.py")
MOCK = os.path.join(HERE, "mock_tronclass.py")
# 测试自带的课表夹具：让用例不依赖任何个人文件
SCHEDULE_FIXTURE = {
    "meta": {"title": "测试课表"},
    "periods": [{"period": 7, "time": "16:40~17:25"},
                {"period": 8, "time": "17:35~18:20"}],
    "courses": [{"id": 1, "name": "测试课程", "day": "星期一",
                 "weeks": {"start": 1, "end": 16}, "periods": [7, 8],
                 "time": "16:40~18:20", "location": "测试楼"}],
}

MON_GATE = "Mon, 19 Oct 2026 08:35:00 GMT"     # 北京 周一 16:35 -> 窗口内（16:30~18:30）
SUN_NIGHT = "Sun, 25 Oct 2026 19:00:00 GMT"    # 北京 周一 03:00 -> 窗口外

SCENARIOS = [
    {"name": "number_gate", "date": MON_GATE, "port": 8891, "seconds": 7},
    {"name": "no_code", "date": MON_GATE, "port": 8892, "seconds": 6, "max_attempts": 2},
    {"name": "outside", "date": SUN_NIGHT, "port": 8894, "seconds": 5},
    {"name": "stale", "date": MON_GATE, "port": 8895, "seconds": 5},
    {"name": "qr_unvalidated", "date": MON_GATE, "port": 8896, "seconds": 7, "qr_api": True},
    {"name": "qr_validated", "date": MON_GATE, "port": 8897, "seconds": 7},
]

results = []


def free_port(port: int) -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


class Sink:
    """假 clawbot：接收 daemon 推来的通知并落盘。"""

    def __init__(self, port: int, path: str):
        self.port = port
        self.path = path
        self.script = os.path.join(os.path.dirname(path), "sink.py")
        with open(self.script, "w", encoding="utf-8") as fh:
            fh.write(
                "import json,sys\n"
                "from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\n"
                "OUT=sys.argv[1];PORT=int(sys.argv[2])\n"
                "class H(BaseHTTPRequestHandler):\n"
                "    def log_message(self,*a):pass\n"
                "    def do_POST(self):\n"
                "        n=int(self.headers.get('Content-Length') or 0)\n"
                "        b=self.rfile.read(n).decode('utf-8','replace')\n"
                "        open(OUT,'a',encoding='utf-8').write(b+'\\n')\n"
                "        r=b'{\"ok\":true}';self.send_response(200)\n"
                "        self.send_header('Content-Length',str(len(r)));self.end_headers();self.wfile.write(r)\n"
                "    def do_GET(self):\n"
                "        open(OUT,'a',encoding='utf-8').write('{\"get\":\"health\"}\\n')\n"
                "        r=b'{\"ok\":true}';self.send_response(200)\n"
                "        self.send_header('Content-Length',str(len(r)));self.end_headers();self.wfile.write(r)\n"
                "ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()\n")
        self.proc = subprocess.Popen([sys.executable, self.script, path, str(port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def read_lines(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [line for line in fh.read().splitlines() if line.strip()]


def run_scenario(scenario: dict) -> None:
    name = scenario["name"]
    workdir = tempfile.mkdtemp(prefix=f"ck-{name}-")
    record_path = os.path.join(workdir, "requests.jsonl")
    notify_path = os.path.join(workdir, "notify.jsonl")
    log_path = os.path.join(workdir, "daemon.log")

    schedule_path = os.path.join(workdir, "课程表.json")
    with open(schedule_path, "w", encoding="utf-8") as fh:
        json.dump(SCHEDULE_FIXTURE, fh, ensure_ascii=False)

    env = dict(os.environ,
               MOCK_SCENARIO=name, MOCK_DATE=scenario["date"],
               MOCK_PORT=str(scenario["port"]), MOCK_RECORD=record_path)
    mock = subprocess.Popen([sys.executable, MOCK], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sink = Sink(scenario["port"] + 100, notify_path)

    config = {
        "base_url": f"http://127.0.0.1:{scenario['port']}",
        "cookies": "sessionid=TESTCOOKIE123; csrftoken=abc",
        "x_session_id": "test-xsid-999",
        "schedule_path": schedule_path,
        "clawbot_url": f"http://127.0.0.1:{scenario['port'] + 100}/notify",
        "notify_enabled": True,
        "gate_min_signed": 4,
        "poll_in_window_s": 1,
        "window_check_s": 1,
        "clock_resync_s": 3600,
        "max_attempts": scenario.get("max_attempts", 20),
        "qr_fetch": True,
        "qr_poll_s": 0.5,
        "timeout_s": 5,
        "state_path": os.path.join(workdir, "state.json"),
        "log_path": log_path,
        "qr_api_enabled": True,
        "qr_api_port": scenario["port"] + 200,
    }
    config_path = os.path.join(workdir, "config.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False)

    daemon_env = dict(os.environ, CHANGKE_CONFIG=config_path)
    daemon = subprocess.Popen([sys.executable, DAEMON, "serve"], env=daemon_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, cwd=os.path.dirname(DAEMON))
    time.sleep(scenario["seconds"])
    api_result = None
    if scenario.get("qr_api"):
        import urllib.request as urlreq
        payload = json.dumps({"rollcallId": 9001, "data": "1792425000" + "a" * 32})
        body = json.dumps({"payload": payload}).encode("utf-8")
        request = urlreq.Request(
            f"http://127.0.0.1:{scenario['port'] + 200}/qr", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urlreq.urlopen(request, timeout=8) as resp:
                api_result = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:
            api_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    time.sleep(1)
    daemon.send_signal(signal.SIGTERM)
    try:
        out, _ = daemon.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        daemon.kill()
        out, _ = daemon.communicate()
    mock.terminate()
    sink.stop()
    try:
        mock.wait(timeout=5)
    except subprocess.TimeoutExpired:
        mock.kill()

    requests = [json.loads(line) for line in read_lines(record_path)]
    notifications = []
    for line in read_lines(notify_path):
        try:
            obj = json.loads(line)
            if obj.get("title"):
                notifications.append(obj)
        except ValueError:
            pass
    logtext = out or ""

    checks = []
    answer_put = [r for r in requests if r["method"] == "PUT"
                  and r["path"].endswith("/answer_number_rollcall")]
    qr_gets = [r for r in requests if r["method"] == "GET" and r["path"].endswith("/qr_code")]
    rollcall_gets = [r for r in requests if r["method"] == "GET"
                     and "/student/" in r["path"] and r["path"].endswith("/rollcalls")]
    with_cookie = [r for r in requests if r.get("cookie")]
    with_xsid = [r for r in requests if r.get("xsid") == "test-xsid-999"]
    titles = [n.get("title", "") for n in notifications]   # 保持列表，勿拼成字符串

    if name == "number_gate":
        checks += [
            ("检测到签到时推送提醒", any("检测到签到" in t for t in titles)),
            ("服务端校时生效（日志含采用偏差且声明未改系统时间）",
             "采用偏差" in logtext and "未改动系统时间" in logtext),
            ("门控生效：先等待（日志含未达 4 人）", "未达 4 人" in logtext),
            ("满 4 人后才提交", len(answer_put) == 1),
            ("提交体含 numberCode=4821", len(answer_put) == 1 and '"numberCode": "4821"' in answer_put[0]["body"]),
            ("提交体含 deviceId", len(answer_put) == 1 and "deviceId" in answer_put[0]["body"]),
            ("去重：同一点名只提交一次", len(answer_put) == 1),
            ("成功后有推送", any("数字签到成功" in t for t in titles)),
        ]
    elif name == "no_code":
        checks += [
            ("两通道无码时不提交", len(answer_put) == 0),
            ("取不到码时提醒人工", any("数字签到我处理不了" in t for t in titles)),
            ("尝试过课表日期取码通道", any("timetable_rollcalls" in r["path"] for r in requests)),
        ]
    elif name == "qr_unvalidated":
        qr_puts = [r for r in requests if r["method"] == "PUT"
                   and r["path"].endswith("/answer_qr_rollcall")]
        checks += [
            ("取不到 token 时自动做了服务端校验探测",
             "校验探测" in logtext and len(qr_puts) >= 1),
            ("识别出服务端不校验 data（发了解锁通知）",
             any("不校验" in t for t in titles)),
            ("不再要求人工处理二维码", not any("需要你出手" in t for t in titles)),
            ("本地载荷接口提交成功", bool(api_result and api_result.get("ok"))),
        ]
    elif name == "qr_validated":
        qr_puts = [r for r in requests if r["method"] == "PUT"
                   and r["path"].endswith("/answer_qr_rollcall")]
        checks += [
            ("做了校验探测", "校验探测" in logtext and len(qr_puts) >= 1),
            ("识别出服务端确实校验（错误码 invalid_create_time_hash）",
             any("确实校验" in t for t in titles)),
            ("探测内容里带上了错误码", "invalid_create_time_hash" in logtext),
            ("没有把被拒当成签到成功", not any("签到成功" in t for t in titles)),
        ]
    elif name == "stale":
        checks += [
            ("历史点名不提交任何签到", len(answer_put) == 0),
            ("历史点名不再取二维码码", len(qr_gets) == 0),
            ("历史点名不发任何提醒（含检测/二维码）", len(notifications) == 0),
        ]
    elif name == "outside":
        checks += [
            ("窗口外不查签到列表", len(rollcall_gets) == 0),
            ("窗口外不提交任何签到", len(answer_put) == 0),
            ("窗口外仍会校时（有 /api/profile）",
             any(r["path"] == "/api/profile" for r in requests)),
        ]

    checks += [
        ("请求带上 Cookie", len(with_cookie) > 0),
        ("请求带上 x-session-id", len(with_xsid) > 0),
    ]

    passed = sum(1 for _, ok in checks if ok)
    results.append((name, passed, len(checks)))
    print(f"\n=== 场景 {name}：{passed}/{len(checks)} ===")
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")
    if passed != len(checks):
        print("  --- daemon 输出（末 12 行）---")
        for line in logtext.splitlines()[-12:]:
            print("   " + line)
        print("  --- 通知 ---")
        for n in notifications:
            print(f"   {n.get('title')}｜{n.get('body')}")

    shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    for scenario in SCENARIOS:
        if not free_port(scenario["port"]):
            print(f"端口 {scenario['port']} 被占用，无法测试")
            return 1
    for scenario in SCENARIOS:
        run_scenario(scenario)

    total_pass = sum(p for _, p, _ in results)
    total_all = sum(a for _, _, a in results)
    print(f"\n================ 总计 {total_pass}/{total_all} ================")
    for name, passed, all_ in results:
        print(f"  {name:<12} {passed}/{all_}{'' if passed == all_ else '  <-- 有失败'}")
    return 0 if total_pass == total_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
