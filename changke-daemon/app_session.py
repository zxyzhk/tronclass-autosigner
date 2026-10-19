#!/usr/bin/env python3
"""从本机畅课 App 的容器里读取已登录会话（**只读**，不改动 App 任何数据）

原理：畅课 App 是 Capacitor 应用，登录态存在 WebView 的 localStorage 里
（`WebsiteData/Default/<hash>/<hash>/LocalStorage/localstorage.sqlite3` 的
`_cap_global` 键，UTF-16 编码的 JSON）。里面包含：

    org.apiUrl              畅课租户域名（自动发现，无需手填）
    sessionID               API 所需的 x-session-id
    identityRefreshToken    Keycloak 刷新令牌（可静默续期）
    org.identityConfigH5    auth-server-url / realm / resource(client_id)
    deviceInfo.uuid         设备 UUID（可作 deviceId）

于是「本机 App 登录 → 本机程序操作」成立：守护进程直接复用这个会话，
既不存密码，也不需要你手动抄 Cookie。

所有函数都不打印敏感值；CLI 输出一律打码。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import netfix   # 同目录：优先 IPv4，避免 IPv6 超时

netfix.prefer_ipv4()

BUNDLE_CANDIDATES = ("com.wisdomgarden.trpc",)
CONTAINERS = os.path.expanduser("~/Library/Containers")


def _decode(raw) -> str:
    if isinstance(raw, str):
        return raw
    data = bytes(raw)
    for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _read_cap_global(db_path: str):
    """复制一份再读（App 运行中会锁库），读完即删临时副本。"""
    tmpdir = tempfile.mkdtemp(prefix="ck-app-")
    local = os.path.join(tmpdir, "ls.sqlite3")
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                shutil.copy2(db_path + suffix, local + suffix)
        con = sqlite3.connect(local)
        try:
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key='_cap_global'").fetchone()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not row:
        return None
    text = _decode(row[0])
    try:
        return json.loads(text)
    except ValueError:
        return None


def _candidate_bundles():
    ids = list(BUNDLE_CANDIDATES)
    try:
        for name in os.listdir(CONTAINERS):
            low = name.lower()
            if ("wisdomgarden" in low or "tronclass" in low or "trpc" in low) \
                    and name not in ids:
                ids.append(name)
    except OSError:
        pass
    return ids


def read() -> dict | None:
    """读本机畅课 App 的会话信息；找不到返回 None。"""
    for bundle in _candidate_bundles():
        base = os.path.join(CONTAINERS, bundle,
                            "Data/Library/WebKit/WebsiteData/Default")
        if not os.path.isdir(base):
            continue
        for outer in sorted(glob.glob(os.path.join(base, "*"))):
            if not os.path.isdir(outer):
                continue
            for inner in sorted(glob.glob(os.path.join(outer, "*"))):
                if not os.path.isdir(inner):
                    continue
                db = os.path.join(inner, "LocalStorage", "localstorage.sqlite3")
                if not os.path.exists(db):
                    continue
                data = _read_cap_global(db)
                if not isinstance(data, dict):
                    continue
                org = data.get("org") or {}
                h5 = (org.get("identityConfigH5") or {})
                app_conf = (org.get("identityConfig") or {})
                api_url = str(org.get("apiUrl")
                              or (data.get("apiPrefix") or {}).get("api") or "").rstrip("/")
                info = {
                    "bundle": bundle,
                    "api_url": api_url,
                    "session_id": str(data.get("sessionID") or ""),
                    "refresh_token": str(data.get("identityRefreshToken") or ""),
                    "user_id": str(data.get("userID") or ""),
                    "device_uuid": str((data.get("deviceInfo") or {}).get("uuid") or ""),
                    "auth_server": str(h5.get("auth-server-url")
                                       or app_conf.get("auth-server-url") or "").rstrip("/"),
                    "realm": str(h5.get("realm") or app_conf.get("realm") or ""),
                    "client_id": str(h5.get("resource") or "TronClassH5"),
                    "client_id_app": str(app_conf.get("resource") or ""),
                    "login_type": str(data.get("loginType") or ""),
                    "is_login": bool(data.get("isLogin")),
                    "org_name": str(org.get("orgName") or ""),
                }
                if info["api_url"] and info["session_id"]:
                    return info
    return None


# ------------------------------------------------------------------ 会话续期

def refresh_token_grant(auth_server: str, realm: str, client_id: str,
                        refresh_token: str, timeout: float = 15.0):
    """用 Keycloak 刷新令牌换新的 access_token。返回 (access_token, error)。"""
    url = f"{auth_server}/realms/{realm}/protocol/openid-connect/token"
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            payload = json.loads(resp.read(65536).decode("utf-8", "replace"))
        token = payload.get("access_token")
        if token:
            return token, ""
        return None, f"响应无 access_token: {str(payload)[:120]}"
    except urllib.error.HTTPError as exc:
        snippet = ""
        try:
            snippet = exc.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        return None, f"HTTP {exc.code} {snippet[:160]}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def exchange_session(api_url: str, access_token: str, org_id: int = 1,
                     timeout: float = 15.0):
    """用 access_token 换 x-session-id（对应 POST /api/login?login=access_token）。"""
    url = f"{api_url}/api/login?login=access_token"
    body = json.dumps({"access_token": access_token, "org_id": org_id}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/plain, */*"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            session = resp.headers.get("x-session-id") or resp.headers.get("X-Session-Id")
            if session:
                return session, ""
            return None, "响应头里没有 x-session-id"
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def renew(info: dict, timeout: float = 15.0):
    """会话失效时用刷新令牌续期，返回 (新的 session_id, 说明)。"""
    if not (info.get("auth_server") and info.get("realm") and info.get("refresh_token")):
        return None, "缺少 refresh_token / auth_server / realm，无法自动续期"
    candidates = [c for c in (info.get("client_id_app"), info.get("client_id"),
                              "TronClassH5", "TronClassApp") if c]
    tried = []
    for client_id in candidates:
        if client_id in tried:
            continue
        tried.append(client_id)
        token, err = refresh_token_grant(info["auth_server"], info["realm"],
                                        client_id, info["refresh_token"], timeout)
        if not token:
            continue
        session, err2 = exchange_session(info["api_url"], token, timeout=timeout)
        if session:
            return session, f"已用 client_id={client_id} 续期成功"
        return None, f"换 session 失败：{err2}"
    return None, "刷新令牌授权失败（" + "; ".join(tried) + "）"


# ------------------------------------------------------------------ 探针

def probe(info: dict, session_id: str, timeout: float = 12.0):
    """只读探针：GET /api/profile 验证会话是否有效。"""
    request = urllib.request.Request(
        f"{info['api_url']}/api/profile",
        headers={"Accept": "application/json, text/plain, */*",
                 "X-Requested-With": "XMLHttpRequest",
                 "x-session-id": session_id})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            body = resp.read(65536).decode("utf-8", "replace")
        return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def mask(value, keep: int = 4) -> str:
    text = str(value or "")
    if not text:
        return "(空)"
    return text[:keep] + "***" + text[-2:]


def summarize(info: dict) -> str:
    lines = [
        f"App 容器      : {info['bundle']}",
        f"平台          : {info['org_name'] or '(未知)'}",
        f"畅课域名      : {info['api_url']}",
        f"登录状态      : {'已登录' if info['is_login'] else '未登录'}"
        f"（loginType={info['login_type'] or '?'}）",
        f"sessionID     : {mask(info['session_id'])}（{len(info['session_id'])} 字符）",
        f"刷新令牌      : {mask(info['refresh_token'])}"
        f"（{len(info['refresh_token'])} 字符）",
        f"认证服务      : {info['auth_server'] or '(无)'} realm={info['realm'] or '?'}"
        f" client_id={info['client_id'] or '?'}",
        f"设备 UUID     : {info['device_uuid'] or '(无)'}",
        f"用户 ID       : {info['user_id']}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="读取本机畅课 App 的已登录会话")
    parser.add_argument("--probe", action="store_true", help="只读探针：验证会话是否有效")
    parser.add_argument("--renew", action="store_true", help="会话失效时尝试续期")
    args = parser.parse_args()

    found = read()
    if not found:
        print("❌ 没找到畅课 App 的登录会话")
        print("   请确认：App 已安装（/Applications/TronClass.app）且已登录过一次")
        raise SystemExit(1)

    print(summarize(found))
    if args.probe or args.renew:
        status, body = probe(found, found["session_id"])
        print(f"\n探针 GET /api/profile -> HTTP {status}")
        if status == 200:
            data = json.loads(body) if body.strip().startswith("{") else {}
            print(f"  ✅ 会话有效，student_id={data.get('id') or data.get('user_id')}")
        else:
            print("  ⚠️ 会话无效，尝试用刷新令牌续期 ...")
            if not args.renew:
                print("  （加 --renew 才会真的续期）")
            else:
                session, detail = renew(found)
                print(f"  {'✅ ' if session else '❌ '}{detail}")
                if session:
                    status2, _ = probe(found, session)
                    print(f"  续期后探针 -> HTTP {status2}"
                          + ("  ✅ 可用" if status2 == 200 else "  ❌ 仍不可用"))
