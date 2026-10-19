#!/usr/bin/env python3
"""从课程表 JSON 生成油猴脚本里的 SCHEDULE 常量。

课程表 JSON 期望结构（可由网页课表整理，或截图后用任何工具转写）：
{
  "periods": [{"period": 1, "time": "08:00~08:45"}, ...],
  "courses": [{"id":1,"name":"课程名","day":"星期一","weeks":{"start":1,"end":16},
               "periods":[1,2],"time":"08:00~09:40","location":"..."}]
}

用法：
    python3 tools/gen_schedule.py 课程表.json            # 打印可直接粘贴的 JS 片段
    python3 tools/gen_schedule.py 课程表.json --in-place changke-autosign.user.js
"""
from __future__ import annotations

import argparse
import json
import re
import sys

DAY = {"星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4,
       "星期五": 5, "星期六": 6, "星期日": 0}


def minutes(text: str) -> int:
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def build(data: dict) -> str:
    courses = []
    for course in data.get("courses") or []:
        day = DAY.get(str(course.get("day", "")))
        span = str(course.get("time", "")).split("~")
        if day is None or len(span) != 2:
            continue
        weeks = course.get("weeks") or {}
        courses.append({
            "day": day, "s": minutes(span[0]), "e": minutes(span[1]),
            "w": [int(weeks.get("start", 1)), int(weeks.get("end", 16))],
            "n": course.get("name", ""),
        })
    courses.sort(key=lambda c: (c["day"], c["s"]))
    periods = {str(p["period"]): [minutes(p["time"].split("~")[0]),
                                  minutes(p["time"].split("~")[1])]
               for p in (data.get("periods") or [])}

    lines = ["  const SCHEDULE = {",
             "    tzOffsetMin: 480,",
             "    padMin: 10,",
             "    mergeGapMin: 25,",
             "    periods: " + json.dumps(periods, ensure_ascii=False,
                                          separators=(",", ":")) + ",",
             "    courses: ["]
    for c in courses:
        lines.append(
            "      {day:%d,s:%4d,e:%4d,w:[%2d,%2d],n:%s},"
            % (c["day"], c["s"], c["e"], c["w"][0], c["w"][1],
               json.dumps(c["n"], ensure_ascii=False)))
    lines += ["    ],", "  };"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("schedule_json")
    parser.add_argument("--in-place", help="直接替换该文件里的 SCHEDULE 常量")
    args = parser.parse_args()

    with open(args.schedule_json, encoding="utf-8") as fh:
        data = json.load(fh)
    block = build(data)
    if not args.in_place:
        print(block)
        return 0

    text = open(args.in_place, encoding="utf-8").read()
    pattern = re.compile(r"  const SCHEDULE = \{.*?\n  \};", re.S)
    new_text, count = pattern.subn(block, text, count=1)
    if count != 1:
        print("未找到 SCHEDULE 常量", file=sys.stderr)
        return 1
    open(args.in_place, "w", encoding="utf-8").write(new_text)
    print(f"已写入 {args.in_place}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
