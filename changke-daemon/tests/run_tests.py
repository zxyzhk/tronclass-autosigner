#!/usr/bin/env python3
"""端到端测试：把 changke-daemon 对着「假畅课」跑起来，逐场景断言。

覆盖：
  A number_gate  数字签到 + 门控（3 人时等、满 4 人后提交，并且只提交一次）
  B no_code      两条取码通道都拿不到码 -> 不提交，并提醒人工
  C outside / stale   窗口外零查询与历史点名跳过
  D qr_unvalidated / qr_validated   二维码校验接受与拒绝
  E radar / radar_coords / radar_nodist   距离推算、接口坐标与缺少距离
另有隔离的内存回归检查：缓存、手动坐标、名单确认、dry-run 与通知重试。
所有场景同时校验：Cookie/x-session-id 有带上、服务端校时生效、事件推送到中继。
"""
from __future__ import annotations

import json
import importlib.util
import math
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

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
    {"name": "radar", "date": MON_GATE, "port": 8898, "seconds": 14},
    {"name": "radar_coords", "date": MON_GATE, "port": 8899, "seconds": 8},
    {"name": "radar_nodist", "date": MON_GATE, "port": 8900, "seconds": 8, "max_attempts": 2},
]

results = []


def clean_environment() -> dict:
    """防止外部 CHANGKE_* 设置把测试指向真实会话、状态文件或通知出口。"""
    return {key: value for key, value in os.environ.items()
            if not key.startswith("CHANGKE_")}



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

    env = dict(clean_environment(),
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
        "session_source": "config",
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

    daemon_env = dict(clean_environment(), CHANGKE_CONFIG=config_path)
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
    state_path = os.path.join(workdir, "state.json")
    saved_state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            saved_state = json.load(fh)

    checks = []
    answer_put = [r for r in requests if r["method"] == "PUT"
                  and r["path"].endswith("/answer_number_rollcall")]
    qr_gets = [r for r in requests if r["method"] == "GET" and r["path"].endswith("/qr_code")]
    rollcall_gets = [r for r in requests if r["method"] == "GET"
                     and "/student/" in r["path"] and r["path"].endswith("/rollcalls")]
    with_cookie = [r for r in requests if r.get("cookie")]
    with_xsid = [r for r in requests if r.get("xsid") == "test-xsid-999"]
    titles = [n.get("title", "") for n in notifications]   # 保持列表，勿拼成字符串
    bodies = " | ".join(n.get("body", "") for n in notifications)

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
    elif name in ("radar", "radar_coords", "radar_nodist"):
        import math as _m
        def _hav(la1, lo1, la2, lo2):
            R = 6371000.0
            p1, p2 = _m.radians(la1), _m.radians(la2)
            dp, dl = p2 - p1, _m.radians(lo2 - lo1)
            a = (_m.sin(dp / 2) ** 2
                 + _m.cos(p1) * _m.cos(p2) * _m.sin(dl / 2) ** 2)
            return 2 * R * _m.asin(_m.sqrt(a))

        answers = []
        for r in requests:
            if r["method"] == "PUT" and r["path"].split("?")[0].endswith("/answer"):
                try:
                    body = json.loads(r.get("body") or "{}")
                    answers.append((float(body["latitude"]), float(body["longitude"])))
                except Exception:
                    pass
        nearest = min((_hav(la, lo, 24.45, 118.09) for la, lo in answers), default=99999)

        if name == "radar":
            checks += [
                ("位置签到成功", any("位置签到成功" in x for x in titles)),
                ("确实走了三角定位（日志有探针距离）", "探针距离" in logtext),
                ("提交了多个候选坐标", len(answers) >= 3),
                ("最终坐标落在老师位置附近",
                 nearest <= 60),
                ("成功后缓存课程坐标", "111" in saved_state.get("radar_cache", {})),
            ]
        elif name == "radar_coords":
            checks += [
                ("接口坐标直接命中", "点名详情坐标" in bodies),
                ("没有多余做三角定位", "探针距离" not in logtext),
            ]
        else:  # radar_nodist
            checks += [
                ("无距离时不误判成功", not any("位置签到成功" in x for x in titles)),
                ("提示服务端未返回距离", "未返回 distance" in logtext),
                ("给出失败提醒", any("位置签到未成功" in x for x in titles)),
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
            ("历史点名不发任何签到类提醒",
             not any(any(k in n.get("title", "") for k in
                         ("检测到签到", "二维码", "签到成功", "签到失败", "位置签到"))
                     for n in notifications)),
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


def run_regressions() -> None:
    """无真实网络的回归：确认成功判据、dry-run、坐标复用与通知重试。"""
    module_dir = os.path.dirname(DAEMON)
    sys.path.insert(0, module_dir)
    try:
        spec = importlib.util.spec_from_file_location("changke_daemon_tested", DAEMON)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)

    checks = []
    instance = module.Daemon.__new__(module.Daemon)
    instance.cfg = dict(module.DEFAULTS, session_source="config")
    instance.dry_run = False
    instance.sid = 12345
    instance.log = Mock()
    instance.device_id = lambda: "test-device"
    instance.state = SimpleNamespace(seen=set(), attempts={}, gate_since={},
                                     radar_cache={}, save=Mock())
    signed_detail = {"student_rollcalls": [
        {"student_id": 12345, "rollcall_status": "on_call_fine"}]}
    absent_detail = {"student_rollcalls": [
        {"student_id": 12345, "rollcall_status": "absent"}]}
    instance.detail = lambda rid: signed_detail
    instance._radar_submit = Mock(return_value=(200, None, "{}"))

    # 合成测试位置；不是任何真实教师或课堂的坐标。
    target = (24.45, 118.09)
    instance.cfg["coords"] = ",".join(map(str, target))
    ok, _ = instance.answer_radar_auto("9001", "111", "测试课程")
    checks.append(("手动坐标优先且仅提交一次",
                   ok and instance._radar_submit.call_args_list == [
                       (("9001", *target), {})]))

    instance.cfg["coords"] = ""
    instance.state.radar_cache["111"] = list(target)
    instance._radar_submit.reset_mock()
    ok, _ = instance.answer_radar_auto("9001", "111", "测试课程")
    checks.append(("课程缓存坐标直接复用，不额外探测",
                   ok and instance._radar_submit.call_count == 1
                   and instance._radar_submit.call_args.args == ("9001", *target)))

    instance.state.radar_cache.clear()
    instance.cfg["radar_auto"] = False
    instance.detail = lambda rid: absent_detail
    instance._radar_submit.reset_mock()
    ok, _ = instance.answer_radar_auto(
        "9001", "111", "测试课程",
        detail={"latitude": target[0], "longitude": target[1]})
    checks.append(("HTTP 200 但名单未确认时不能视为成功",
                   not ok and instance._radar_submit.call_count == 1
                   and not instance.state.radar_cache))

    instance.dry_run = True
    instance._radar_submit.reset_mock()
    ok, reason = instance.answer_radar_auto("9001", "111", "测试课程")
    checks.append(("dry-run 不提交坐标，也不返回真实成功",
                   not ok and reason == "dry-run"
                   and not instance._radar_submit.called))

    instance.dedupe = set()
    instance.qr_targets = {}
    instance.qr_gate = {}
    instance.ensure_identity = lambda: True
    instance.load_courses = lambda: [{"id": 111, "display_name": "测试课程"}]
    instance.rollcalls_of = lambda cid: [
        {"id": 9001, "is_radar": True, "status": "absent", "type": "radar"}]
    instance.detail = lambda rid: dict(absent_detail, status="in_progress")
    instance.gate = lambda rid: (True, "测试门控放行")
    with patch.object(module, "notify", return_value=True) as notified:
        instance.poll()
    checks.append(("GPS 空跑不标为已签，不推送成功",
                   not instance.state.seen and "9001" not in instance.state.attempts
                   and not instance._radar_submit.called
                   and not any("成功" in call.args[2]
                               for call in notified.call_args_list)))

    # 更小的模拟允许半径迫使初解失败，检验实际迭代而非只检查日志文本。
    instance.dry_run = False
    instance.cfg["radar_auto"] = True
    instance.state.radar_cache.clear()
    signed = {"value": False}
    submitted = []

    def distance(point):
        lat1, lon1, lat2, lon2 = map(math.radians, (*point, *target))
        a = (math.sin((lat2 - lat1) / 2) ** 2
             + math.cos(lat1) * math.cos(lat2)
             * math.sin((lon2 - lon1) / 2) ** 2)
        return 2 * module.EARTH_R * math.asin(math.sqrt(a))

    def submit(rid, lat, lon):
        point = (float(lat), float(lon))
        submitted.append(point)
        gap = distance(point)
        if gap <= 0.5:
            signed["value"] = True
            return 200, None, "{}"
        return 400, round(gap, 1), "{}"

    instance._radar_submit = submit
    instance.detail = lambda rid: signed_detail if signed["value"] else absent_detail
    ok, _ = instance.answer_radar_auto("9001", "111", "测试课程")
    checks.append(("距离精化得到已确认且小于 0.5 米的模拟解",
                   ok and len(submitted) >= 4
                   and min(map(distance, submitted)) <= 0.5
                   and "111" in instance.state.radar_cache))

    # 完全拦截通知网络与等待：失败不能污染去重集，后续调用仍能重试。
    notify_cfg = dict(module.DEFAULTS, notify_enabled=True,
                      clawbot_url="http://127.0.0.1:1/notify")
    dedupe = set()
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"{}"
    opener = Mock()
    opener.open.side_effect = [URLError("test failure") for _ in range(3)] + [response]
    with patch.object(module.urllib.request, "build_opener", return_value=opener), \
            patch.object(module.time, "sleep") as sleep:
        failed = module.notify(notify_cfg, Mock(), "测试通知", "模拟正文", dedupe)
        checks.append(("通知失败后不加入去重集", not failed and not dedupe))
        checks.append(("三次通知尝试只等待两次",
                       opener.open.call_count == 3
                       and [call.args[0] for call in sleep.call_args_list] == [2, 5]))
        sent = module.notify(notify_cfg, Mock(), "测试通知", "模拟正文", dedupe)
        calls_after_sent = opener.open.call_count
        repeated = module.notify(notify_cfg, Mock(), "测试通知", "模拟正文", dedupe)
        checks.append(("同一通知失败后可重试，成功后才去重",
                       sent and repeated and len(dedupe) == 1
                       and calls_after_sent == 4
                       and opener.open.call_count == calls_after_sent))

    passed = sum(bool(ok) for _, ok in checks)
    results.append(("regressions", passed, len(checks)))
    print(f"\n=== 内存回归：{passed}/{len(checks)} ===")
    for label, ok in checks:
        print(f"  {'✅' if ok else '❌'} {label}")


def main() -> int:
    for scenario in SCENARIOS:
        for port in (scenario["port"], scenario["port"] + 100, scenario["port"] + 200):
            if not free_port(port):
                print(f"端口 {port} 被占用，无法测试")
                return 1
    for scenario in SCENARIOS:
        run_scenario(scenario)
    run_regressions()

    total_pass = sum(p for _, p, _ in results)
    total_all = sum(a for _, _, a in results)
    print(f"\n================ 总计 {total_pass}/{total_all} ================")
    for name, passed, all_ in results:
        print(f"  {name:<12} {passed}/{all_}{'' if passed == all_ else '  <-- 有失败'}")
    return 0 if total_pass == total_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
