#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从本机浏览器导出 cookie，供采集时复用登录态。

**用途**：抖音 / 快手 / 小红书 搜索都要求登录。若用户已在 Chrome/Edge/Quark
登录过，可把 cookie 导出成文件，采集时用 `--cookies` 注入，免去重新扫码。

**原理**（Windows）：
  1. 读 `Local State` 里的 `os_crypt.encrypted_key` → DPAPI 解出 AES 主密钥
  2. 读 `Cookies` SQLite 库的 `encrypted_value`（v10/v11 = AES-256-GCM）
  3. 按域名过滤后导出

⚠️ **已知限制**：
  - Chrome **正在运行时** cookie 库被独占锁，无法直接读 →
    脚本会明确提示「请先完全退出 Chrome 再重试」
  - 新版 Chrome 若启用 **App-Bound Encryption**（`app_bound_encrypted_key`），
    本脚本会识别并如实告知无法解密（不硬凑）

用法:
    # 1) 导出（Chrome 需先关闭）
    python import_login.py --browser chrome --domain douyin.com --out ./out/cookies

    # 2) 采集时注入
    python collect_topics.py --topic "…" --platform douyin --cookies ./out/cookies/douyin.com.json

    # 3) 只想看能不能读
    python import_login.py --browser chrome --domain douyin.com --check
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

BROWSERS = {
    "chrome": r"AppData\Local\Google\Chrome\User Data",
    "edge": r"AppData\Local\Microsoft\Edge\User Data",
    "quark": r"AppData\Local\Quark\User Data",
    "brave": r"AppData\Local\BraveSoftware\Brave-Browser\User Data",
}


def user_data_dir(browser: str) -> Path | None:
    home = Path(os.environ.get("USERPROFILE", r"C:\Users\Administrator"))
    rel = BROWSERS.get(browser)
    if not rel:
        return None
    p = home / rel
    return p if p.exists() else None


def _read_locked(path: Path) -> bytes | None:
    """读取可能被占用的文件：先常规尝试，再试 Win32 共享读。"""
    try:
        return path.read_bytes()
    except Exception:
        pass
    try:
        import win32con
        import win32file
        h = win32file.CreateFile(
            str(path), win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
            | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING, 0, None)
        buf = b""
        while True:
            _, chunk = win32file.ReadFile(h, 1024 * 1024)
            if not chunk:
                break
            buf += chunk
        h.Close()
        return buf
    except Exception:
        return None


def _aes_key(local_state: Path) -> tuple[bytes | None, str]:
    """返回 (key, 说明)。"""
    try:
        d = json.loads(local_state.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"无法读取 Local State: {e}"
    oc = d.get("os_crypt", {}) or {}
    if oc.get("app_bound_encrypted_key"):
        return None, ("该浏览器启用了 App-Bound Encryption（app_bound_encrypted_key），"
                      "本脚本无法解密。请改用「持久化 profile + 扫码登录」方式。")
    ek = oc.get("encrypted_key")
    if not ek:
        return None, "Local State 里没有 encrypted_key"
    try:
        import win32crypt
        raw = base64.b64decode(ek)
        if raw[:5] == b"DPAPI":
            raw = raw[5:]
        key = win32crypt.CryptUnprotectData(raw, None, None, None, 0)[1]
        return key, "ok"
    except Exception as e:
        return None, f"DPAPI 解密失败: {e}"


def _decrypt(value: bytes, key: bytes) -> str:
    if not value:
        return ""
    if value[:3] in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            return AESGCM(key).decrypt(value[3:15], value[15:], None).decode(
                "utf-8", "ignore")
        except Exception:
            return ""
    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(value, None, None, None, 0)[1].decode(
            "utf-8", "ignore")
    except Exception:
        return ""


def export(browser: str, domain: str, out: Path, check_only: bool) -> int:
    ud = user_data_dir(browser)
    if not ud:
        print(f"⛔ 未找到 {browser} 的 User Data 目录")
        return 2
    profile = ud / "Default"
    ls = ud / "Local State"
    ck = profile / "Network" / "Cookies"
    if not ck.exists():
        ck = profile / "Cookies"
    if not ck.exists():
        print(f"⛔ 未找到 {browser} 的 Cookies 文件（路径: {ck}）")
        return 2

    key, msg = _aes_key(ls)
    if key is None:
        print(f"⛔ {msg}")
        return 3
    print(f"✅ 拿到 AES 主密钥（{browser}，长度 {len(key)}）")

    # 复制一份再读（避免锁 + 避免破坏原库）
    raw = _read_locked(ck)
    if raw is None:
        print("⛔ Cookies 库被独占锁定，无法读取。")
        print("   → 请**完全退出**该浏览器（含后台进程）后重试；")
        print("   → 或改用持久化 profile：collect_topics.py --profile ./profile --headful")
        return 4
    tmp = out / f".{browser}_Cookies.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(raw)

    try:
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "SELECT host_key,name,encrypted_value FROM cookies "
            "WHERE host_key LIKE ?", (f"%{domain}%",)).fetchall()
    except Exception as e:
        print(f"⛔ 读取 Cookies 库失败: {e}")
        return 5

    cookies = {}
    for host, name, ev in rows:
        v = _decrypt(ev, key)
        if v:
            cookies[name] = v

    print(f"   匹配 {domain} 的 cookie：{len(cookies)} 个")
    for n in list(cookies)[:12]:
        val = cookies[n]
        print(f"     {n} = {val[:36]}{'…' if len(val) > 36 else ''}")

    if check_only:
        try:
            tmp.unlink()
        except Exception:
            pass
        return 0 if cookies else 6

    if not cookies:
        print(f"⛔ 没有 {domain} 的 cookie —— 说明该浏览器里未登录此站点")
        try:
            tmp.unlink()
        except Exception:
            pass
        return 6

    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"{domain}.json"
    dst.write_text(json.dumps({
        "source_browser": browser, "domain": domain,
        "domains": [f".{domain}", domain],
        "cookies": cookies,
        "note": "由 import_login.py 从本机浏览器导出，仅供本地采集复用登录态",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.unlink()
    except Exception:
        pass
    print(f"\n✅ 已导出 → {dst}")
    print(f"   采集时用：--cookies \"{dst}\"")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从本机浏览器导出 cookie 以复用登录态")
    ap.add_argument("--browser", default="chrome",
                    choices=list(BROWSERS), help="目标浏览器，默认 chrome")
    ap.add_argument("--domain", required=True, help="域名，如 douyin.com / kuaishou.com")
    ap.add_argument("--out", default="./cookies", help="输出目录")
    ap.add_argument("--check", action="store_true", help="只检测能否读取，不落盘")
    args = ap.parse_args()
    return export(args.browser, args.domain, Path(args.out).resolve(), args.check)


if __name__ == "__main__":
    raise SystemExit(main())
