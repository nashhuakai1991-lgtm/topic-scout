#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""榜单聚合 + 文案骨架生成 + 金句溯源登记。

本脚本负责「机械可验证」的部分，**不负责编造内容**：

  1. 汇总 collected.json → 生成「参考到的全部自媒体条目」清单（= 报告文末的
     参考来源表），每条带平台、作者、链接、热度指标、抓取时间。
  2. 生成「原创文案骨架」（outline.json + 底稿 md）：把各平台内容线索按
     主题聚类，给出结构与要点占位。
  3. 登记「金句溯源表」（quotes.json）：**只登记能定位到具体出处的内容**
     （标题/简介/字幕原文）。没有原文的**不登记**，由一个 `needs_manual`
     列表标出「需要人工/AI 补充核校后才能引用」的条目。

⚠️ 铁律：本脚本绝不为任何条目编造「他说过……」式的内容。
   拿不到正文的条目，只能进 needs_manual，不得进 quotes。

用法:
    python distill.py --collected ./out/collected.json --out ./out
产出:
    out/references.json   参考来源清单（报告文末表格直接用）
    out/outline.json      文案骨架
    out/draft.md          Markdown 底稿（供人工/AI 续写）
    out/quotes.json       金句溯源表 + needs_manual
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import now_iso  # noqa: E402


# ---------------------------------------------------------------- 内容线索类型

CLUE_RANK = {           # 内容可靠性排序（高→低），决定能不能当「内容总结」用
    "transcript": 4,    # 本地转写（最可靠）
    "subtitle": 3,      # 官方字幕/CC
    "desc": 2,          # 官方简介——通常只有一句，**不足以写内容总结**
    "title-only": 1,    # 只有标题 → 只能做主题标注
    "": 0,              # 什么都没有
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def all_items(collected: dict) -> list[dict]:
    out = []
    for r in collected.get("results", []):
        for it in r.get("items", []):
            it = dict(it)
            it.setdefault("platform_name", r.get("platform_name", ""))
            out.append(it)
    return out


# ---------------------------------------------------------------- 1. 参考来源

def build_references(collected: dict) -> dict:
    """生成文末「参考来源」表。这是用户要求的『标注参考了哪些自媒体内容』。"""
    rows = []
    for r in collected.get("results", []):
        if not r.get("items"):
            continue
        for it in r["items"]:
            rows.append({
                "rank": it.get("rank"),
                "platform": it.get("platform"),
                "platform_name": it.get("platform_name") or r.get("platform_name"),
                "title": it.get("title", ""),
                "author": it.get("author", ""),
                "url": it.get("url", ""),
                "published": it.get("published", ""),
                "metrics": {
                    "views": it.get("views"),
                    "likes": it.get("likes"),
                    "comments": it.get("comments"),
                    "collects": it.get("collects"),
                    "shares": it.get("shares"),
                },
                "hot_score": it.get("hot_score"),
                "content_source": it.get("content_source", ""),
                "title_key": f"P{it.get('rank')}-{it.get('platform')}",
            })
            rows[-1]["title_key"] = (
                f"[{rows[-1]['platform_name']}#{it.get('rank')}]")
    return {
        "topic": collected.get("topic"),
        "generated_at": now_iso(),
        "collected_at": collected.get("generated_at"),
        "total": len(rows),
        "rows": rows,
    }


# ---------------------------------------------------------------- 2. 文案骨架

STOPWORDS = set("的 了 和 与 及 在 是 我 你 他 它 们 这 那 就 都 也 还 又 很 把 被 "
                "一个 什么 怎么 如何 为什么 哪些 可以 需要 这样 那样 视频 分享 "
                "教程 方法 技巧 揭秘 干货 全 最 更 超".split())


def tokenize(text: str, n: int = 3) -> list[str]:
    """非常轻的中文关键词抽取：n-gram + 停用词过滤。够用于聚类打标。"""
    s = re.sub(r"[^\u4e00-\u9fa50-9A-Za-z]+", " ", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    grams: list[str] = []
    for chunk in s.split(" "):
        if not chunk:
            continue
        if len(chunk) <= n:
            grams.append(chunk)
            continue
        for i in range(len(chunk) - n + 1):
            g = chunk[i:i + n]
            if g in STOPWORDS or any(c in STOPWORDS for c in g):
                continue
            grams.append(g)
    return grams


def cluster_items(items: list[dict], max_clusters: int = 6) -> list[dict]:
    """按关键词共现把条目聚成若干主题簇。纯机械，不做语义臆断。"""
    from collections import Counter, defaultdict
    item_grams: list[tuple[dict, set[str]]] = []
    for it in items:
        txt = " ".join(filter(None, [it.get("title"), it.get("summary")]))
        item_grams.append((it, set(tokenize(txt))))

    # 用全局高频 gram 当种子
    freq: Counter = Counter()
    for _, gs in item_grams:
        freq.update(gs)
    seeds = [g for g, c in freq.most_common(max_clusters * 3)
             if c >= 2 and len(g) >= 2][:max_clusters]
    if not seeds:
        return [{"seed": "全部内容", "members": [it for it, _ in item_grams]}]

    clusters: dict[str, list[dict]] = defaultdict(list)
    assigned: set[int] = set()
    for seed in seeds:
        for idx, (it, gs) in enumerate(item_grams):
            if idx in assigned:
                continue
            if seed in gs:
                clusters[seed].append(it)
                assigned.add(idx)
    rest = [it for idx, (it, _) in enumerate(item_grams) if idx not in assigned]
    out = [{"seed": k, "members": v} for k, v in clusters.items() if v]
    if rest:
        out.append({"seed": "其它相关", "members": rest})
    return out


def build_outline(collected: dict, references: dict,
                  topic: str) -> dict:
    items = all_items(collected)
    clusters = cluster_items(items)
    is_tutorial = _looks_tutorial(topic, items)

    sections = []
    for cl in clusters:
        mem = cl["members"]
        citable = [m for m in mem
                   if CLUE_RANK.get(m.get("content_source", ""), 0) >= 2]
        sections.append({
            "heading": cl["seed"],
            "source_count": len(mem),
            "citable_count": len(citable),
            "needs_manual": len(mem) - len(citable),
            "sources": [
                {
                    "key": f"[{m.get('platform_name')}#{m.get('rank')}]",
                    "platform": m.get("platform_name"),
                    "title": m.get("title"),
                    "author": m.get("author"),
                    "url": m.get("url"),
                    "likes": m.get("likes"),
                    "views": m.get("views"),
                    "content_source": m.get("content_source", ""),
                }
                for m in mem
            ],
        })

    steps = []
    if is_tutorial:
        steps = [
            {"step": i + 1, "name": f"步骤 {i + 1}（待据正文整理）",
             "detail": "", "sources": []}
            for i in range(5)
        ]

    return {
        "topic": topic,
        "generated_at": now_iso(),
        "is_tutorial": is_tutorial,
        "structure": {
            "1_开篇": "一句话点出主题价值 + 本内容覆盖范围（须写明参考了多少条自媒体内容）",
            "2_核心内容": [s["heading"] for s in sections],
            "3_教程流程" if is_tutorial else "3_要点小结":
                "按步骤整理清晰流程（教程类）" if is_tutorial
                else "汇总可执行结论",
            "4_交叉验证": "标注哪些结论多源一致、哪些存疑",
            "5_参考来源": "文末列出全部参考条目与金句出处",
        },
        "sections": sections,
        "tutorial_steps": steps,
        "clue_policy": {
            "可作内容总结": ["transcript", "subtitle"],
            "仅可作主题标注": ["desc", "title-only"],
            "禁止": "拿不到正文的条目不得扩写成内容描述",
        },
    }


def _looks_tutorial(topic: str, items: list[dict]) -> bool:
    kw = ("怎么", "如何", "教程", "步骤", "方法", "入门", "教学", "教你",
          "做法", "流程", "how", "tutorial", "guide")
    hay = (topic or "").lower() + " " + " ".join(
        (i.get("title") or "") for i in items).lower()
    return any(k in hay for k in kw)


# ---------------------------------------------------------------- 3. 金句溯源

def build_quotes(collected: dict) -> dict:
    """登记可定位出处的「金句」。没有正文的进 needs_manual，不硬编。"""
    rows, needs = [], []
    for r in collected.get("results", []):
        for it in r.get("items", []):
            src = it.get("content_source", "")
            text = (it.get("summary") or "").strip()
            key = f"[{it.get('platform_name') or r.get('platform_name')}" \
                  f"#{it.get('rank')}]"
            entry = {
                "key": key,
                "platform": it.get("platform_name") or r.get("platform_name"),
                "author": it.get("author", ""),
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "published": it.get("published", ""),
                "quote": text,
                "quote_type": src or "none",
                "usable_as_quote": CLUE_RANK.get(src, 0) >= 3,
            }
            if text and CLUE_RANK.get(src, 0) >= 2:
                rows.append(entry)
            else:
                needs.append({
                    "key": key,
                    "reason": ("仅有标题，无正文——引用前必须拿到字幕/转写，"
                               "否则不得写成『他说过』"),
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "platform": it.get("platform_name") or r.get("platform_name"),
                })
    return {
        "generated_at": now_iso(),
        "note": ("usable_as_quote=true 表示该条正文可直接引用（字幕/转写级）；"
                 "desc 级为官方简介，可引用但须注明『据官方简介』。"),
        "quotes": rows,
        "needs_manual": needs,
        "stats": {"citable": len(rows), "needs_manual": len(needs)},
    }


# ---------------------------------------------------------------- 4. 底稿 md

def build_draft(topic: str, outline: dict, references: dict,
                quotes: dict, status_rows: list[dict]) -> str:
    L: list[str] = []
    L.append(f"# {topic} —— 全网内容蒸馏（底稿）\n")
    L.append(f"> 生成时间：{now_iso()}　|　"
             f"参考自媒体条目：**{references['total']}** 条　|　"
             f"可直接引用：**{quotes['stats']['citable']}** 条，"
             f"待补正文：**{quotes['stats']['needs_manual']}** 条\n")
    L.append("> ⚠️ 本文件是**底稿**：机械部分（来源清单、结构、溯源）已就绪，"
             "正文段落需由人工/AI 依据真实内容线索续写。"
             "**不得为拿不到正文的条目编造内容**。\n")

    L.append("## 数据源状态\n")
    L.append("| 平台 | 可达性 | 状态 | 条数 | 说明 |")
    L.append("| --- | --- | --- | --- | --- |")
    for s in status_rows:
        L.append(f"| {s['platform_name']} | {s['access']} | {s['status']} | "
                 f"{s['count']} | {(s.get('reason') or '')[:80]} |")
    L.append("")

    L.append("## 一、开篇\n")
    L.append("（待写）一句话点出主题价值，并写明参考了多少条自媒体内容。\n")

    L.append("## 二、核心内容\n")
    for sec in outline["sections"]:
        L.append(f"### {sec['heading']}")
        L.append(f"- 来源 {sec['source_count']} 条；其中可引用 "
                 f"{sec['citable_count']} 条，需补正文 {sec['needs_manual']} 条")
        for s in sec["sources"][:12]:
            m = []
            if s.get("likes") is not None:
                m.append(f"赞 {s['likes']}")
            if s.get("views") is not None:
                m.append(f"播 {s['views']}")
            metric = ("（" + " / ".join(m) + "）") if m else ""
            L.append(f"  - {s['key']} {s['title']} —— {s['author']} "
                     f"{metric} `{s['content_source'] or 'none'}`")
        L.append("")

    if outline["is_tutorial"]:
        L.append("## 三、教程流程（图文并茂）\n")
        L.append("> 教程类：需整理出清晰流程。每一步配图（视频截帧或 AI 生图），"
                 "并标注该步骤的参考来源。\n")
        for st in outline["tutorial_steps"]:
            L.append(f"### {st['step']}. {st['name']}")
            L.append("- 操作要点：（待据正文整理）")
            L.append("- 配图：（截帧/生图，待补）")
            L.append("- 参考来源：（待标注）\n")

    L.append("## 四、交叉验证与存疑\n")
    L.append("（待填）列出：多源一致的结论、来源冲突的点、仅单一来源的点。\n")

    L.append("## 五、参考来源\n")
    L.append("| 序号 | 平台 | 标题 | 作者 | 链接 | 互动 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for i, r in enumerate(references["rows"], 1):
        m = r["metrics"]
        parts = []
        for k, lab in (("views", "播"), ("likes", "赞"), ("comments", "评"),
                       ("collects", "藏"), ("shares", "转")):
            if m.get(k) is not None:
                parts.append(f"{lab}{m[k]}")
        L.append(f"| {i} | {r['platform_name']} | {r['title']} | {r['author']} | "
                 f"[原链接]({r['url']}) | {' '.join(parts) or '—'} |")
    L.append("")

    if quotes["quotes"]:
        L.append("### 金句出处\n")
        L.append("| 金句 | 出处 | 平台/作者 | 链接 | 类型 |")
        L.append("| --- | --- | --- | --- | --- |")
        for q in quotes["quotes"]:
            qt = q["quote"][:60] + ("…" if len(q["quote"]) > 60 else "")
            L.append(f"| {qt} | {q['title']} | {q['platform']} / {q['author']} | "
                     f"[原链接]({q['url']}) | {q['quote_type']} |")
        L.append("")

    if quotes["needs_manual"]:
        L.append("### ⚠️ 待补正文（引用前必须拿到字幕/转写）\n")
        for n in quotes["needs_manual"][:30]:
            L.append(f"- {n['key']} {n['title']} —— {n['reason']}")
        L.append("")

    return "\n".join(L)


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="榜单聚合 + 文案骨架 + 金句溯源")
    ap.add_argument("--collected", required=True, help="collect_topics.py 的产出")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--topic", default="", help="覆盖主题名")
    args = ap.parse_args()

    collected = load(Path(args.collected))
    topic = args.topic or collected.get("topic") or "未命名主题"
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    references = build_references(collected)
    outline = build_outline(collected, references, topic)
    quotes = build_quotes(collected)
    status_rows = []

    # 状态行从 collected 里带出来（报告数据源状态表用）
    for r in collected.get("results", []):
        status_rows.append({
            "platform_name": r.get("platform_name"),
            "platform": r.get("platform"),
            "access": r.get("access"),
            "status": r.get("status"),
            "count": len(r.get("items", [])),
            "reason": r.get("reason", ""),
        })

    draft = build_draft(topic, outline, references, quotes, status_rows)

    (out / "references.json").write_text(
        json.dumps(references, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "outline.json").write_text(
        json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "quotes.json").write_text(
        json.dumps(quotes, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "draft.md").write_text(draft, encoding="utf-8")

    print(f"参考来源 {references['total']} 条 → references.json")
    print(f"文案骨架：{len(outline['sections'])} 个主题簇，"
          f"教程类={outline['is_tutorial']} → outline.json")
    print(f"金句：可引用 {quotes['stats']['citable']} 条 / "
          f"待补正文 {quotes['stats']['needs_manual']} 条 → quotes.json")
    print(f"底稿 → {out / 'draft.md'}")
    print("\n⚠️ 下一步：依据真实内容线索续写正文；"
          "needs_manual 里的条目**不得**编造内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
