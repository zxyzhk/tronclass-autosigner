#!/usr/bin/env python3
"""测试用「假畅课」服务端：模拟真实接口与可控的签到场景。

用环境变量控制：
  MOCK_SCENARIO   number_gate | no_code | outside | stale | qr_unvalidated |
                  qr_validated | radar | radar_coords | radar_nodist
  MOCK_DATE       HTTP Date 响应头（用来伪造「服务端时间」）
  MOCK_PORT       监听端口
  MOCK_RECORD     请求记录文件（JSON Lines）
"""
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCENARIO = os.environ.get("MOCK_SCENARIO", "number_gate")
FIXED_DATE = os.environ.get("MOCK_DATE", "Mon, 19 Oct 2026 08:35:00 GMT")
PORT = int(os.environ.get("MOCK_PORT", "8891"))
RECORD = os.environ.get("MOCK_RECORD", "mock-requests.jsonl")

T0 = time.time()
STUDENT_ID = 12345
COURSE_ID = 111
ROLLCALL_ID = 9001
NUMBER_CODE = "4821"

# 位置签到测试夹具：合成目标点与允许半径，不对应真实教师或课堂
RADAR_TARGET = (24.45, 118.09)
RADAR_RADIUS = 60.0
RADAR_STATE = {"signed": False}


def haversine(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def record(entry: dict) -> None:
    try:
        with open(RECORD, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def signed_count() -> int:
    """前 2.5 秒只有 3 人已签（用来验证门控），之后 4 人。"""
    return 3 if time.time() - T0 < 2.5 else 4


def roster():
    people = []
    for index in range(signed_count()):
        people.append({"student_id": 20000 + index, "rollcall_status": "on_call_fine"})
    people.append({"student_id": STUDENT_ID, "rollcall_status": "absent"})
    return people


def rollcall_row():
    if SCENARIO in ("radar", "radar_coords", "radar_nodist"):
        return {"id": ROLLCALL_ID, "is_number": False, "is_radar": True, "is_qr": False,
                "type": "radar", "status": "absent",
                "rollcall_time": "2026-10-19T08:40:00Z"}
    if SCENARIO in ("qr_unvalidated", "qr_validated"):
        return {"id": ROLLCALL_ID, "is_number": False, "is_radar": False, "is_qr": False,
                "type": "qr_rollcall", "status": "absent",
                "rollcall_time": "2026-10-19T08:40:00Z"}
    if SCENARIO == "stale":
        # 历史记录：我当年没签（absent），但点名早已结束
        return {"id": ROLLCALL_ID, "is_number": True, "is_radar": False, "is_qr": False,
                "type": "another", "status": "absent",
                "rollcall_time": "2026-10-18T08:05:00Z"}
    return {
        "id": ROLLCALL_ID,
        "is_number": SCENARIO != "qr_403",
        "is_radar": False,
        "is_qr": SCENARIO == "qr_403",
        "type": "qr_rollcall" if SCENARIO == "qr_403" else "number_rollcall",
        "status": "absent",
        "rollcall_time": "2026-10-19T08:40:00Z",
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def date_time_string(self, timestamp=None):
        return FIXED_DATE          # 伪造服务端时间，用于测试校时与窗口判定

    def _json(self, code: int, obj) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):                                    # noqa: N802
        path = self.path.split("?")[0]
        record({"method": "GET", "path": self.path,
                "cookie": bool(self.headers.get("Cookie")),
                "xsid": self.headers.get("x-session-id") or "",
                "at": round(time.time() - T0, 2)})

        if path == "/api/profile":
            self._json(200, {"id": STUDENT_ID, "name": "测试学生"})
        elif path == "/api/current-semester-info":
            self._json(200, {"semester": {"id": 29}, "academic_year": {"id": 12}})
        elif path.startswith("/api/course/") and path.endswith("/rollcalls"):
            if SCENARIO == "outside":
                self._json(200, {"rollcalls": [rollcall_row()]})
            else:
                self._json(200, {"rollcalls": [rollcall_row()]})
        elif path.endswith("/student_rollcalls"):
            if SCENARIO == "stale":
                detail = {"id": ROLLCALL_ID, "status": "finished",
                          "end_time": "2026-10-18T08:50:00Z",
                          "student_rollcalls": roster()}
            else:
                detail = {"id": ROLLCALL_ID, "status": "in_progress",
                          "end_time": "2026-10-19T09:00:00Z",
                          "student_rollcalls": roster()}
            if SCENARIO == "radar_coords":
                detail["latitude"] = RADAR_TARGET[0]
                detail["longitude"] = RADAR_TARGET[1]
            if SCENARIO in ("radar", "radar_coords", "radar_nodist"):
                detail["student_rollcalls"] = [
                    {"student_id": 20000 + i, "rollcall_status": "on_call_fine"}
                    for i in range(signed_count())] + [
                    {"student_id": STUDENT_ID,
                     "rollcall_status": "on_call_fine" if RADAR_STATE["signed"] else "absent"}]
            if SCENARIO == "number_gate":
                detail["number_code"] = None       # 新通道：详情不再下发数字码
            self._json(200, detail)
        elif path == "/api/timetable_rollcalls":
            if SCENARIO == "no_code":
                self._json(200, {"rollcalls": [{"id": ROLLCALL_ID, "number_code": None}]})
            else:
                self._json(200, {"rollcalls": [{"id": ROLLCALL_ID,
                                                "number_code": NUMBER_CODE}]})
        elif "/rollcall/" in path and path.endswith("/qr_code"):
            if SCENARIO in ("qr_403", "qr_unvalidated", "qr_validated"):
                self._json(403, {"error": "forbidden"})
            else:
                token = f"{int(time.time())}" + "a" * 32
                self._json(200, {"data": token})
        elif path == "/":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):                                   # noqa: N802
        self._read_and_record("POST")
        path = self.path.split("?")[0]
        if path == "/api/my-courses":
            self._json(200, {"courses": [{"id": COURSE_ID, "display_name": "测试课程"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_PUT(self):                                    # noqa: N802
        body = self._read_and_record("PUT")
        path = self.path.split("?")[0]
        if path.endswith("/answer_number_rollcall"):
            self._json(200, {"ok": True})
        elif path.endswith("/answer_qr_rollcall"):
            if SCENARIO == "qr_validated":
                self._json(400, {"error": "invalid_create_time_hash"})
            else:
                self._json(200, {"ok": True})
        elif path.endswith("/answer"):
            if SCENARIO not in ("radar", "radar_coords", "radar_nodist"):
                self._json(200, {"ok": True})
                return
            try:
                body = json.loads(body or "{}")
                lat, lon = float(body.get("latitude")), float(body.get("longitude"))
            except Exception:
                self._json(400, {"error": "bad payload"})
                return
            dist = haversine(lat, lon, *RADAR_TARGET)
            if dist <= RADAR_RADIUS:
                RADAR_STATE["signed"] = True
                self._json(200, {"ok": True})
            elif SCENARIO == "radar_nodist":
                self._json(400, {"error": "out_of_range"})   # 不发距离
            else:
                self._json(400, {"distance": round(dist, 1)})
        else:
            self._json(404, {"error": "not found"})
        del body

    def _read_and_record(self, method: str) -> str:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        record({"method": method, "path": self.path, "body": body,
                "cookie": bool(self.headers.get("Cookie")),
                "xsid": self.headers.get("x-session-id") or "",
                "at": round(time.time() - T0, 2)})
        return body


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
