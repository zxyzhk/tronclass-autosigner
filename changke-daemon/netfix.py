#!/usr/bin/env python3
"""强制优先 IPv4 —— 绕过"IPv6 路由不通"导致的每次请求 ~10 秒连接超时。

背景：
    目标服务的 DNS 可能同时返回 AAAA（IPv6）与 A（IPv4）记录。
    当本机 IPv6 出网不通时，urllib 会先连 IPv6、等超时，
    再回落 IPv4 —— 每个请求白等约 10 秒。轮询一次要查多门课，累积起来会把节奏拖垮。

做法：
    优先只取 IPv4 结果；若该主机确实没有 A 记录，再退回原始解析（不做硬性禁用）。
    只影响本进程，不改动系统网络设置。
"""
from __future__ import annotations

import socket

_ORIGINAL = socket.getaddrinfo
_APPLIED = False


def prefer_ipv4() -> bool:
    """把 socket.getaddrinfo 换成"IPv4 优先"版本；重复调用无副作用。"""
    global _APPLIED
    if _APPLIED:
        return True

    def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
        try:
            results = _ORIGINAL(host, port, socket.AF_INET, type, proto, flags)
            if results:
                return results
        except (socket.gaierror, OSError):
            pass
        return _ORIGINAL(host, port, family, type, proto, flags)

    socket.getaddrinfo = _ipv4_first
    _APPLIED = True
    return True


def restore() -> None:
    """还原原始解析（测试或排障用）。"""
    global _APPLIED
    socket.getaddrinfo = _ORIGINAL
    _APPLIED = False


def address_family_used(host: str, port: int = 443):
    """诊断：打印某主机当前会优先使用的地址族与地址。"""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if not infos:
        return None, None
    family = infos[0][0]
    name = {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}.get(family, str(family))
    return name, infos[0][4][0]


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "courses.example.edu.cn"
    print("=== 未修补 ===")
    print("  优先地址族:", *address_family_used(host))
    print("=== 修补后 ===")
    prefer_ipv4()
    print("  优先地址族:", *address_family_used(host))
