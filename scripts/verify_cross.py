#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨平台交叉验证器 —— 把「反复出现的说法」与「孤证」分开。

用户原文要求第 3 条：「所有截取与引用的内容需与互联网信息交叉验证，
避免幻觉或主观臆断。」

本脚本做的是**机械可验证**的那部分：

  ① 多源一致性：同一关键词/说法在几个平台独立出现 → 置信度分级
  ② 孤证标记：只有一个来源的说法一律标 low，交付文档必须写明
  ③ 指标异常检测：互动数据明显不自洽的条目（如赞 > 播放）打 warning
  ④ 时间线检查：发布日期越界（未来/过旧）打 warning
  ⑤ 存疑清单：把所有需要人工确认的点汇成一张表

⚠️ 本脚本**不做网络检索**。真正的「与互联网交叉验证」由 AI 在写作阶段用
   WebSearch 完成（见 references/verification.md 的四路核校法）；
   本脚本负责把**哪些必须去核**列成待办，避免漏核。

用法:
    python verify_cross.py --collected ./out/collected.json \
        --quotes ./out/quotes.json --out ./out
产出:
    out/verification.json  置信度分级 + 存疑清单 + 待核事项
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import now_iso  # noqa: E402


def load(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------- 关键词抽取

STOP = set("的 了 和 与 及 在 是 我 你 他 它 们 这 那 就 都 也 还 又 很 把 被 一个 "
           "什么 怎么 如何 为什么 哪些 可以 需要 这样 那样 视频 分享 教程 方法 "
           "技巧 揭秘 干货 全部 最好 更加 超级 一个 一些".split())


def keywords(text: str) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fa50-9A-Za-z]+", " ", text or "")
    out: set[str] = set()
    for chunk in s.split():
        if len(chunk) < 2:
            continue
        for i in range(len(chunk) - 1):
            g = chunk[i:i + 2]
            if g not in STOP and not any(c in STOP for c in g):
                out.add(g)
    return out


def item_claims(it: dict) -> set[str]:
    """一条内容主张的关键词集合。"""
    txt = " ".join(filter(None, [it.get("title"), it.get("summary")]))
    return keywords(txt)


# ---------------------------------------------------------------- ①② 一致性

def consistency(collected: dict, min_platforms: int = 2) -> dict:
    items = []
    for r in collected.get("results", []):
        for it in r.get("items", []):
            it = dict(it)
            it.setdefault("platform_name", r.get("platform_name"))
            items.append(it)

    # 每个关键词出现在哪些平台
    kw_platforms: dict[str, set[str]] = defaultdict(set)
    kw_items: dict[str, list[str]] = defaultdict(list)
    for it in items:
        key = f"[{it.get('platform_name')}#{it.get('rank')}]"
        for k in item_claims(it):
            kw_platforms[k].add(it.get("platform") or it.get("platform_name"))
            kw_items[k].append(key)

    high, mid, single = [], [], []
    for k, plats in kw_platforms.items():
        n = len(plats)
        rec = {"term": k, "platform_count": n,
               "platforms": sorted(x for x in plats if x),
               "items": sorted(set(kw_items[k]))}
        if n >= 3:
            high.append(rec)
        elif n == min_platforms:
            mid.append(rec)
        else:
            single.append(rec)

    high.sort(key=lambda x: -x["platform_count"])
    mid.sort(key=lambda x: -x["platform_count"])
    return {"high": high[:40], "mid": mid[:40], "single": single[:40],
            "total_items": len(items)}


# ---------------------------------------------------------------- ③ 指标异常

def metric_anomalies(items: list[dict]) -> list[dict]:
    warns = []
    for it in items:
        key = f"[{it.get('platform_name')}#{it.get('rank')}]"
        v, l = it.get("views"), it.get("likes")
        if v is not None and l is not None and v > 0 and l > v:
            warns.append({
                "key": key, "type": "likes_gt_views",
                "detail": f"点赞 {l} > 播放 {v}，指标不自洽，需人工核",
                "url": it.get("url", ""),
            })
        c = it.get("comments")
        if l is not None and c is not None and l > 0 and c > l * 2:
            warns.append({
                "key": key, "type": "comments_gt_likes",
                "detail": f"评论 {c} 远高于点赞 {l}，可能为争议内容，需人工看",
                "url": it.get("url", ""),
            })
        if not it.get("url"):
            warns.append({"key": key, "type": "no_url",
                          "detail": "缺少原链接，无法溯源，报告中不得引用",
                          "url": ""})
    return warns


# ---------------------------------------------------------------- ④ 时间线

def timeline_checks(items: list[dict], ref_year: int) -> list[dict]:
    warns = []
    for it in items:
        d = (it.get("published") or "").strip()
        key = f"[{it.get('platform_name')}#{it.get('rank')}]"
        if not d:
            warns.append({"key": key, "type": "no_date",
                          "detail": "无发布日期，时效性无法判断", "url": it.get("url", "")})
            continue
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d)
        if not m:
            continue
        y = int(m.group(1))
        if y > ref_year:
            warns.append({"key": key, "type": "future_date",
                          "detail": f"发布日期 {d} 晚于当前年份 {ref_year}，可能是解析错误",
                          "url": it.get("url", "")})
        elif y < ref_year - 3:
            warns.append({"key": key, "type": "stale",
                          "detail": f"发布于 {d}，较旧；若主题有时效性需谨慎引用",
                          "url": it.get("url", "")})
    return warns


# ---------------------------------------------------------------- ⑤ 待核事项

def build_todo(collected: dict, quotes: dict,
               consistency_res: dict) -> list[dict]:
    """列出「必须用 WebSearch 去互联网核校」的事项。"""
    todo: list[dict] = []

    # 1) 孤证说法必须核
    for rec in consistency_res["single"][:15]:
        todo.append({
            "type": "孤证核校",
            "claim": rec["term"],
            "why": "仅出现于单一平台，属于孤证；引用前须另行检索复核",
            "seen_in": rec["items"][:3],
            "method": "WebSearch 该说法 + 权威源关键词",
        })

    # 2) 高一致性说法也要核数字
    for rec in consistency_res["high"][:10]:
        todo.append({
            "type": "数字/事实核校",
            "claim": rec["term"],
            "why": f"跨 {rec['platform_count']} 个平台出现，属高频说法，"
                   "但仍须核对具体数字与专名",
            "seen_in": rec["items"][:3],
            "method": "对照官方/权威媒体口径",
        })

    # 3) 待补正文
    for n in (quotes or {}).get("needs_manual", [])[:15]:
        todo.append({
            "type": "补正文",
            "claim": n.get("title", ""),
            "why": n.get("reason", ""),
            "seen_in": [n.get("key", "")],
            "method": "取字幕/转写；拿不到则不得引用",
        })

    return todo


# ---------------------------------------------------------------- 置信度分级

def grade(consistency_res: dict, anomalies: list[dict],
          todo: list[dict]) -> dict:
    counts = {
        "high_confidence_terms": len(consistency_res["high"]),
        "medium_confidence_terms": len(consistency_res["mid"]),
        "single_source_terms": len(consistency_res["single"]),
        "anomalies": len(anomalies),
        "todo_items": len(todo),
    }
    if counts["single_source_terms"] and counts["high_confidence_terms"]:
        verdict = ("存在若干孤证说法，必须在报告中标注为『仅单一来源』，"
                   "不得当作共识陈述。")
    elif counts["single_source_terms"]:
        verdict = "多为一手孤证，注意区分『事实』与『某人观点』。"
    else:
        verdict = "未发现明显孤证，但仍须逐项核校数字与专名。"
    counts["verdict"] = verdict
    return counts


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="跨平台交叉验证 + 存疑清单")
    ap.add_argument("--collected", required=True)
    ap.add_argument("--quotes", default="", help="distill.py 产出的 quotes.json")
    ap.add_argument("--out", default="./out")
    args = ap.parse_args()

    collected = load(Path(args.collected), {}) or {}
    quotes = load(Path(args.quotes), {}) if args.quotes else {}
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    items = []
    for r in collected.get("results", []):
        for it in r.get("items", []):
            it = dict(it)
            it.setdefault("platform_name", r.get("platform_name"))
            items.append(it)

    import time
    ref_year = int(time.strftime("%Y"))

    cons = consistency(collected)
    anomalies = metric_anomalies(items)
    timeline = timeline_checks(items, ref_year)
    todo = build_todo(collected, quotes, cons)
    stats = grade(cons, anomalies, todo)

    payload = {
        "generated_at": now_iso(),
        "reference_year": ref_year,
        "stats": stats,
        "consistency": cons,
        "anomalies": anomalies,
        "timeline_warnings": timeline,
        "todo_verify": todo,
        "policy": {
            "可信度分级": {
                "高": "≥3 个独立平台一致，且数字核校通过",
                "中": "2 个来源支持，或数字口径存在差异",
                "低": "仅单一来源 / UGC / 有社区争议",
            },
            "交付要求": "文档必须含『来源与可信度说明』与『交叉验证与存疑』两章节",
            "禁令": "不得给所有结论都标高置信度（等于没收据）",
        },
    }
    (out / "verification.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"高置信词条 {stats['high_confidence_terms']} / "
          f"中 {stats['medium_confidence_terms']} / "
          f"孤证 {stats['single_source_terms']}")
    print(f"指标异常 {len(anomalies)} · 时间线告警 {len(timeline)} · "
          f"待核事项 {len(todo)}")
    print(f"→ {out / 'verification.json'}")
    print("\n⚠️ 下一步：用 WebSearch 逐条处理 todo_verify；"
          "孤证说法必须标『仅单一来源』。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
