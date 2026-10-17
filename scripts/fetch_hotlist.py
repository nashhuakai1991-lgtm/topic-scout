#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""免登录热榜抓取 —— 抖音 / 微博 / B站 / 百度。

**为什么有这个脚本**：抖音 / 快手 / 小红书的**搜索**要登录，但**热榜是公开的**。
本脚本把各平台热榜做成**免登录**可用的稳定输出，用于：
  - 看当下什么话题在火（选题参考）
  - 判断某主题是否正在热榜上（可顺势跟进）
  - 作为「免登录信息源」的一部分（见 references/no-login-sourcing.md）

**实测（2026-10-14）**：
  ✅ 抖音 `aweme/v1/web/hot/search/list` → 50 条（带 hot_value）
  ✅ 微博 `ajax/side/hotSearch`
  ✅ B站  `x/web-interface/ranking/v2`
  ✅ 百度  `top.baidu.com/api/board`
  ⚠️ 知乎 需登录/风控，本次不可用
  ⛔ 快手的榜单走 GraphQL 且需签名，免登录不可得（如实标注）

**要点**：每个域名的接口必须**从该域名自己的页面上下文**里 fetch，
否则会被 CORS 拦（这是踩过的坑）。

用法:
    python fetch_hotlist.py --platform douyin --platform weibo --top 20 --out ./out/hot
    python fetch_hotlist.py --all --top 30 --out ./out/hot
产出:
    <out>/hotlist.json   结构化榜单
    <out>/hotlist.md     Markdown 表格（可直接贴进报告）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import Browser, fetch_json_in_page, now_iso, sleep_jitter  # noqa: E402

# 每个平台：home（预热域名）+ api + 说明
HOT_SPECS = {
    "douyin": {
        "name": "抖音", "home": "https://www.douyin.com/",
        "api": ("https://www.douyin.com/aweme/v1/web/hot/search/list/"
                "?device_platform=webapp&aid=6383"),
        "note": "免登录可用（word_list，含 hot_value）",
    },
    "weibo": {
        "name": "微博", "home": "https://weibo.com/",
        "api": "https://weibo.com/ajax/side/hotSearch",
        "note": "免登录可用",
    },
    "bilibili": {
        "name": "B站", "home": "https://www.bilibili.com/",
        "api": ("https://api.bilibili.com/x/web-interface/ranking/v2"
                "?rid=0&type=all"),
        "note": "免登录可用（全站排行）",
    },
    "baidu": {
        "name": "百度", "home": "https://top.baidu.com/",
        "api": "https://top.baidu.com/api/board?platform=wise&tab=realtime",
        "note": "免登录可用",
    },
    "zhihu": {
        "name": "知乎", "home": "https://www.zhihu.com/",
        "api": ("https://www.zhihu.com/api/v3/feed/topstory/hot-lists/total"
                "?limit=30"),
        "note": "需登录/风控，多数情况拿不到",
    },
    "kuaishou": {
        "name": "快手", "home": "https://www.kuaishou.com/",
        "api": "",  # 榜单走 GraphQL + 签名，免登录不可得
        "note": "榜单走 GraphQL 且需签名，免登录不可得",
    },
}


def parse(platform: str, d) -> list[dict]:
    """把各平台不同的结构统一成 [{rank,title,hot,extra}]。"""
    out: list[dict] = []
    if not isinstance(d, dict):
        return out

    if platform == "douyin":
        wl = (d.get("data") or {}).get("word_list") or []
        for i, w in enumerate(wl, 1):
            out.append({"rank": i, "title": w.get("word", ""),
                        "hot": w.get("hot_value"),
                        "extra": ""})
    elif platform == "weibo":
        rt = (d.get("data") or {}).get("realtime") or []
        for i, w in enumerate(rt, 1):
            out.append({"rank": i, "title": w.get("word", ""),
                        "hot": w.get("num"),
                        "extra": w.get("label_name") or ""})
    elif platform == "bilibili":
        lst = (d.get("data") or {}).get("list") or []
        for i, w in enumerate(lst, 1):
            st = w.get("stat") or {}
            out.append({"rank": i, "title": w.get("title", ""),
                        "hot": st.get("view"),
                        "extra": (f"赞{st.get('like')} "
                                  f"{((w.get('owner') or {}).get('name') or '')}").strip()})
    elif platform == "baidu":
        cards = (d.get("data") or {}).get("cards") or []
        content = (cards[0].get("content") if cards else []) or []
        for i, w in enumerate(content, 1):
            out.append({"rank": i, "title": w.get("word", ""),
                        "hot": w.get("hotScore"),
                        "extra": (w.get("desc") or "")[:40]})
    elif platform == "zhihu":
        lst = d.get("data") or []
        for i, w in enumerate(lst, 1):
            tgt = w.get("target") or {}
            out.append({"rank": i, "title": tgt.get("title", ""),
                        "hot": (w.get("detail_text") or ""),
                        "extra": ""})
    return out


def fetch(platform: str, browser, top: int) -> dict:
    spec = HOT_SPECS[platform]
    res = {"platform": platform, "platform_name": spec["name"],
           "note": spec["note"], "status": "ok", "reason": "",
           "items": []}
    if not spec["api"]:
        res["status"] = "blocked"
        res["reason"] = spec["note"]
        return res

    # ⚠️ 必须从该域名自己的页面发起请求，否则 CORS 拦
    page = browser.new_page()
    try:
        page.goto(spec["home"], wait_until="domcontentloaded", timeout=35_000)
        sleep_jitter(1.5, 1.0)
        d = fetch_json_in_page(page, spec["api"], referer=spec["home"])
        if d is None:
            res["status"] = "error"
            res["reason"] = "接口无响应（可能被风控/改版）"
            return res
        items = parse(platform, d)
        if not items:
            res["status"] = "empty"
            res["reason"] = "返回结构里没解析到条目（可能已改版）"
            return res
        res["items"] = items[:top]
        return res
    except Exception as e:  # noqa: BLE001
        res["status"] = "error"
        res["reason"] = f"{type(e).__name__}: {e}"
        return res
    finally:
        try:
            page.close()
        except Exception:
            pass


def to_md(data: dict) -> str:
    L = [f"# 平台热榜（免登录）\n", f"> 抓取时间：{data['generated_at']}\n"]
    for r in data["results"]:
        L.append(f"## {r['platform_name']}"
                 f"（{r['status']}）\n")
        if r["items"]:
            L.append("| # | 标题 | 热度 | 备注 |")
            L.append("| --- | --- | --- | --- |")
            for it in r["items"]:
                hot = it.get("hot")
                hot = f"{hot:,}" if isinstance(hot, int) else (hot or "—")
                L.append(f"| {it['rank']} | {it['title']} | {hot} | "
                         f"{it.get('extra') or '—'} |")
        else:
            L.append(f"（未取到：{r.get('reason') or r.get('note')}）")
        L.append("")
    L.append("> ⚠️ 热榜均为公开数据、免登录可得；"
             "但**平台搜索**仍需登录，两者不是一回事。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="免登录抓取各平台热榜")
    ap.add_argument("--platform", action="append", default=None,
                    help="平台：douyin/weibo/bilibili/baidu/zhihu/kuaishou")
    ap.add_argument("--all", action="store_true", help="抓全部平台")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default="./out/hot")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()

    plats = (list(HOT_SPECS) if args.all
             else (args.platform or ["douyin", "weibo", "bilibili", "baidu"]))
    unknown = [p for p in plats if p not in HOT_SPECS]
    if unknown:
        ap.error(f"未知平台: {unknown}；可选 {list(HOT_SPECS)}")

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    results = []
    with Browser(headless=not args.headful) as browser:
        for i, p in enumerate(plats, 1):
            r = fetch(p, browser, args.top)
            icon = {"ok": "✅", "empty": "⚪", "error": "❌",
                    "blocked": "⛔"}.get(r["status"], "❔")
            print(f"  [{i}/{len(plats)}] {r['platform_name']:<6} {icon} "
                  f"{r['status']:<8} {len(r['items']):>2} 条"
                  + (f"  └─ {r['reason']}" if r["reason"] else ""))
            results.append(r)
            if i < len(plats):
                sleep_jitter(1.2, 0.8)

    data = {"generated_at": now_iso(), "top": args.top, "results": results,
            "note": "热榜免登录可得；平台搜索仍需登录（见 no-login-sourcing.md）"}
    (out / "hotlist.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "hotlist.md").write_text(to_md(data), encoding="utf-8")
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\n完成：{ok}/{len(plats)} 个平台取到")
    print(f"→ {out / 'hotlist.json'}\n→ {out / 'hotlist.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
