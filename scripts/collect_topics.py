#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多平台选题侦察采集器（主入口）。

用法:
    # 1) 全平台默认全开，每平台取 Top5
    python collect_topics.py --topic "在家做面包" --top 5 --out ./out

    # 2) 指定平台
    python collect_topics.py --topic "装修避坑" --platform bilibili --platform douyin --out ./out

    # 3) 先看各平台可达性与原因（不抓取，秒回）
    python collect_topics.py --check-platforms

    # 4) 有头浏览器（需要登录态时用，用户可手动扫码）
    python collect_topics.py --topic "牛排怎么煎" --headful --out ./out

产出:
    out/collected.json   六平台 Top5 + 每条 source_status（如实记录可用性）
    out/source_status.json  仅状态摘要（供报告「数据源状态表」直接用）

设计要点:
    - 拿到几次失败就如实记录，绝不编造
    - 每平台之间 sleep 限速，不加并发
    - 断点续跑：out/collected.json 存在且非空则跳过，除非 --refresh
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platforms import (  # noqa: E402
    Browser, PLATFORM_SPEC, PlatformResult, now_iso, platform_name,
    search_platform, sleep_jitter,
)
import searchers  # noqa: F401,E402 — 副作用：把各平台检索函数注册进 REGISTRY

ALL_PLATFORMS = ["bilibili", "douyin", "xiaohongshu", "kuaishou",
                 "youtube", "x"]
DEFAULT_ON = ["bilibili", "douyin", "xiaohongshu", "kuaishou",
              "youtube", "x"]


def print_platform_table() -> None:
    print(f"{'平台':<10} {'键名':<13} {'可达性':<16} 说明")
    print("-" * 100)
    for key, spec in PLATFORM_SPEC.items():
        print(f"{spec['name']:<10} {key:<13} {spec['access']:<16} "
              f"{spec['note'][:60]}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="多平台选题侦察：按热度取每平台 TopN 并如实记录可用性")
    ap.add_argument("--topic", "-t", help="主题或内容关键词")
    ap.add_argument("--platform", action="append", default=None,
                    help="限定平台，可重复；不传=全平台默认全开")
    ap.add_argument("--top", type=int, default=5, help="每平台取前 N 名，默认 5")
    ap.add_argument("--min-likes", type=int, default=0,
                    help="过滤低于该点赞量的条目（默认不过滤）")
    ap.add_argument("--out", default="./out", help="输出目录")
    ap.add_argument("--headful", action="store_true",
                    help="有头浏览器（需要用户扫码登录时用）")
    ap.add_argument("--profile", default="",
                    help="持久化浏览器 profile 目录；登录态存盘、后续复用（配 --headful 扫码一次）")
    ap.add_argument("--cookies", default="",
                    help="cookie 文件（由 import_login.py 从本机浏览器导出），注入以复用登录")
    ap.add_argument("--chrome", default="", help="chromium 可执行文件路径")
    ap.add_argument("--proxy", default="", help="浏览器代理，如 http://127.0.0.1:7890")
    ap.add_argument("--refresh", action="store_true", help="忽略已有结果重抓")
    ap.add_argument("--check-platforms", action="store_true",
                    help="只打印平台可达性表，不抓取")
    ap.add_argument("--json-only", action="store_true", help="只输出 JSON，不打日志")
    args = ap.parse_args()

    if args.check_platforms:
        print_platform_table()
        return 0

    if not args.topic:
        ap.error("必须提供 --topic（或用 --check-platforms 只看平台表）")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    collected_path = out / "collected.json"

    if collected_path.exists() and not args.refresh:
        try:
            old = json.loads(collected_path.read_text(encoding="utf-8"))
            if old.get("results"):
                print(f"[skip] 已存在结果 {collected_path}（--refresh 可重抓）")
                return 0
        except Exception:
            pass

    platforms = args.platform or DEFAULT_ON
    unknown = [p for p in platforms if p not in PLATFORM_SPEC]
    if unknown:
        print(f"[warn] 忽略未知平台: {unknown}", file=sys.stderr)
        platforms = [p for p in platforms if p in PLATFORM_SPEC]

    log = (lambda *a: None) if args.json_only else print
    log(f"主题: {args.topic}  |  平台: {', '.join(platform_name(p) for p in platforms)}"
        f"  |  每平台 Top{args.top}")

    # blocked 平台直接短路，不必开浏览器
    need_browser = [p for p in platforms
                    if PLATFORM_SPEC[p]["access"] != "blocked"]

    results: list[PlatformResult] = []
    browser = None
    try:
        if need_browser:
            browser = Browser(chrome=args.chrome, headless=not args.headful,
                              proxy=args.proxy,
                              persistent_dir=args.profile,
                              cookies_file=args.cookies).__enter__()
        for idx, p in enumerate(platforms, 1):
            spec = PLATFORM_SPEC[p]
            if spec["access"] == "blocked":
                results.append(PlatformResult(
                    p, spec["name"], access=spec["access"], status="blocked",
                    queried=args.topic, reason=spec["note"]))
                log(f"  [{idx}/{len(platforms)}] {spec['name']:<10} ⛔ 已跳过"
                    f"（{spec['access']}）")
                continue
            t0 = time.time()
            r = search_platform(p, args.topic, limit=args.top, browser=browser,
                                min_likes=args.min_likes)
            dt = time.time() - t0
            icon = {"ok": "✅", "empty": "⚪", "login-gated": "🔒",
                    "blocked": "⛔", "error": "❌", "skipped": "⏭"}.get(
                        r.status, "❔")
            log(f"  [{idx}/{len(platforms)}] {spec['name']:<10} {icon} "
                f"{r.status:<12} {len(r.items):>2} 条  ({dt:.1f}s)")
            if r.status != "ok" and r.reason:
                log(f"        └─ {r.reason}")
            results.append(r)
            if idx < len(platforms):
                sleep_jitter(1.2, 1.0)   # 站间限速
    finally:
        if browser is not None:
            try:
                browser.__exit__(None, None, None)
            except Exception:
                pass

    payload = {
        "topic": args.topic,
        "generated_at": now_iso(),
        "top_n": args.top,
        "platforms_requested": platforms,
        "results": [r.to_dict() for r in results],
        "stats": {
            "ok": sum(1 for r in results if r.status == "ok"),
            "total_items": sum(len(r.items) for r in results),
        },
    }
    collected_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    status = {
        "topic": args.topic,
        "generated_at": payload["generated_at"],
        "rows": [
            {
                "platform": r.platform,
                "platform_name": r.platform_name,
                "access": r.access,
                "status": r.status,
                "reason": r.reason,
                "count": len(r.items),
            }
            for r in results
        ],
    }
    (out / "source_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"\n完成：成功平台 {payload['stats']['ok']}/{len(platforms)}，"
        f"共 {payload['stats']['total_items']} 条")
    log(f"→ {collected_path}")
    log(f"→ {out / 'source_status.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
