#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图文并茂报告生成器 —— 单文件 HTML + Markdown 底稿。

输入（全部来自前面脚本，可缺省，缺了如实不渲染对应板块）：
    outline.json / references.json / quotes.json / verification.json
    frames/ （grab_frames.py 的产出，用于插图）
    正文   （--body body.md，AI/人工续写好的正文，本脚本负责排版与溯源）

产出:
    report.html   单文件，可直接分享/打印；含数据源状态、图文正文、
                  交叉验证、参考来源表、金句出处
    report.md     Markdown 底稿

设计约束（用户原文要求）：
    - 图文并茂：正文可插入 <figure>，**每张图必须带图注与来源链接**
    - 文末标注参考来源 + 金句出处
    - 数据源状态表如实反映哪些平台没取到
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import re
from pathlib import Path

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--ink:#1c1f23;--muted:#5b6470;--line:#e3e6ea;
--accent:#c8102e;--accent2:#0b6bcb;--ok:#0a7d3c;--warn:#b06a00;--bad:#b3261e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:32px 22px 80px}
h1{font-size:26px;margin:8px 0 6px;letter-spacing:.2px}
h2{font-size:20px;margin:34px 0 12px;padding-left:11px;border-left:4px solid var(--accent)}
h3{font-size:16px;margin:22px 0 8px;color:#2a2f36}
.meta{color:var(--muted);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:14px 0}
.note{background:#fff8e6;border:1px solid #f0dca8;border-radius:8px;padding:12px 15px;
font-size:13.5px;color:#7a5b00;margin:14px 0}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13.5px}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f0f2f5;font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:#fafbfc}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:12px;
border:1px solid var(--line);background:#fff;color:var(--muted);white-space:nowrap}
.t-ok{background:#e8f6ee;border-color:#b6e0c6;color:var(--ok)}
.t-login{background:#fff4e5;border-color:#f0d3a0;color:var(--warn)}
.t-bad{background:#fdeceb;border-color:#f2c2be;color:var(--bad)}
figure{margin:18px 0;text-align:center}
figure img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:8px;
background:#fff}
figcaption{font-size:12.5px;color:var(--muted);margin-top:7px;line-height:1.6;
text-align:left}
figcaption a{color:var(--accent2)}
a{color:var(--accent2)}
.kw{display:inline-block;background:#eef3fb;border:1px solid #cddcf2;color:#21508f;
border-radius:5px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12.5px}
.steps{counter-reset:s;list-style:none;padding:0}
.steps>li{counter-increment:s;position:relative;padding:12px 14px 12px 50px;
background:var(--card);border:1px solid var(--line);border-radius:9px;margin:10px 0}
.steps>li::before{content:counter(s);position:absolute;left:13px;top:12px;width:26px;
height:26px;line-height:26px;text-align:center;border-radius:50%;
background:var(--accent);color:#fff;font-size:13px;font-weight:700}
.ft{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
color:var(--muted);font-size:12.5px}
@media(max-width:820px){.wrap{padding:20px 13px 60px}h1{font-size:21px}}
@media print{body{background:#fff}.card{break-inside:avoid}}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def img_data_uri(path: Path) -> str:
    """把图片内联成 data URI，保证 HTML 单文件可分享。"""
    try:
        mt = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mt};base64,{b64}"
    except Exception:
        return ""


def fmt_metrics(m: dict) -> str:
    if not isinstance(m, dict):
        return "—"
    parts = []
    for k, lab in (("views", "播放"), ("likes", "点赞"), ("comments", "评论"),
                   ("collects", "收藏"), ("shares", "转发")):
        v = m.get(k)
        if v is not None:
            parts.append(f"{lab} {v:,}")
    return " · ".join(parts) or "—"


STATUS_ICON = {"ok": ("✅", "t-ok"), "empty": ("⚪", ""),
               "login-gated": ("🔒", "t-login"), "blocked": ("⛔", "t-bad"),
               "error": ("❌", "t-bad"), "skipped": ("⏭", "")}
STATUS_TEXT = {"ok": "已取到", "empty": "无结果", "login-gated": "需登录",
               "blocked": "不可达", "error": "抓取失败", "skipped": "已跳过"}


def render_status(status_rows: list[dict]) -> str:
    if not status_rows:
        return ""
    rows = []
    for s in status_rows:
        icon, cls = STATUS_ICON.get(s.get("status"), ("❔", ""))
        rows.append(
            f"<tr><td>{esc(s.get('platform_name'))}</td>"
            f"<td><span class='tag'>{esc(s.get('access'))}</span></td>"
            f"<td><span class='tag {cls}'>{icon} "
            f"{esc(STATUS_TEXT.get(s.get('status'), s.get('status')))}</span></td>"
            f"<td>{s.get('count', 0)}</td>"
            f"<td>{esc(s.get('reason') or '—')}</td></tr>")
    return f"""<h2>数据源状态</h2>
<div class="scroll"><table>
<thead><tr><th>平台</th><th>可达性</th><th>本次结果</th><th>条数</th><th>说明</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def render_references(refs: dict) -> str:
    rows = refs.get("rows") or []
    if not rows:
        return ""
    body = []
    for i, r in enumerate(rows, 1):
        link = (f"<a href='{esc(r['url'])}' target='_blank' rel='noopener'>原链接</a>"
                if r.get("url") else "—")
        body.append(
            f"<tr><td>{i}</td><td>{esc(r.get('platform_name'))}</td>"
            f"<td>{esc(r.get('title'))}</td><td>{esc(r.get('author'))}</td>"
            f"<td>{esc(fmt_metrics(r.get('metrics')))}</td>"
            f"<td>{esc(r.get('published') or '—')}</td><td>{link}</td></tr>")
    return f"""<h2>参考来源（全部检索条目）</h2>
<p class="meta">本次共参考 {refs.get('total', 0)} 条自媒体内容，逐条列出来源，
便于核对与追溯。</p>
<div class="scroll"><table>
<thead><tr><th>#</th><th>平台</th><th>标题</th><th>作者</th><th>互动指标</th>
<th>发布</th><th>链接</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>"""


def render_quotes(quotes: dict) -> str:
    if not quotes:
        return ""
    out = []
    qs = quotes.get("quotes") or []
    if qs:
        body = []
        for q in qs:
            qt = q.get("quote") or ""
            if len(qt) > 90:
                qt = qt[:90] + "…"
            body.append(
                f"<tr><td>{esc(qt)}</td><td>{esc(q.get('platform'))} / "
                f"{esc(q.get('author'))}</td><td>{esc(q.get('title'))}</td>"
                f"<td><span class='tag'>{esc(q.get('quote_type'))}</span></td>"
                f"<td>{('<a href=\"%s\" target=\"_blank\" rel=\"noopener\">原链接</a>' % esc(q['url'])) if q.get('url') else '—'}</td></tr>")
        out.append(f"""<h2>金句出处</h2>
<div class="scroll"><table>
<thead><tr><th>金句</th><th>平台 / 作者</th><th>出处标题</th><th>类型</th><th>链接</th></tr></thead>
<tbody>{''.join(body)}</tbody></table></div>""")
    needs = quotes.get("needs_manual") or []
    if needs:
        items = "".join(
            f"<li>{esc(n.get('key'))} {esc(n.get('title'))} —— {esc(n.get('reason'))}</li>"
            for n in needs[:30])
        out.append(f"""<div class="note"><b>⚠️ 以下条目仅有标题、无正文</b>，
引用前必须拿到字幕/转写，否则不得写成「他说过……」：<ul>{items}</ul></div>""")
    return "\n".join(out)


def render_verification(v: dict) -> str:
    if not v:
        return ""
    st = v.get("stats") or {}
    cons = v.get("consistency") or {}

    def terms(lst, cls, limit=12):
        if not lst:
            return "<p class='meta'>无</p>"
        return "".join(
            f"<span class='kw' title='{esc(' / '.join(r.get('platforms') or []))}'>"
            f"{esc(r.get('term'))} ×{r.get('platform_count')}</span>"
            for r in lst[:limit])

    anomalies = v.get("anomalies") or []
    an_rows = "".join(
        f"<tr><td>{esc(a.get('key'))}</td><td>{esc(a.get('type'))}</td>"
        f"<td>{esc(a.get('detail'))}</td></tr>" for a in anomalies[:20]) or \
        "<tr><td colspan='3' class='meta'>未发现明显异常</td></tr>"

    todo = v.get("todo_verify") or []
    todo_rows = "".join(
        f"<tr><td>{esc(t.get('type'))}</td><td>{esc(t.get('claim'))}</td>"
        f"<td>{esc(t.get('why'))}</td><td>{esc(t.get('method'))}</td></tr>"
        for t in todo[:20]) or "<tr><td colspan='4' class='meta'>无</td></tr>"

    return f"""<h2>交叉验证与存疑</h2>
<div class="card">
<p><b>可信度分级</b>（口径见 references/verification.md）：</p>
<p>高置信词条（≥3 平台一致）：{st.get('high_confidence_terms', 0)}　·　
中置信（2 源）：{st.get('medium_confidence_terms', 0)}　·　
孤证（仅 1 源）：<b>{st.get('single_source_terms', 0)}</b></p>
<div class="note">{esc(st.get('verdict', ''))}</div>

<h3>多平台一致的说法（可作共识陈述，数字仍需核）</h3>
<p>{terms(cons.get('high'), 't-ok')}</p>
<h3>两源支持的说法</h3>
<p>{terms(cons.get('mid'), '')}</p>
<h3>⚠️ 孤证说法（必须标注「仅单一来源」）</h3>
<p>{terms(cons.get('single'), 't-bad')}</p>
</div>

<h3>指标异常与溯源告警</h3>
<div class="scroll"><table>
<thead><tr><th>条目</th><th>类型</th><th>说明</th></tr></thead>
<tbody>{an_rows}</tbody></table></div>

<h3>待与互联网核校事项</h3>
<div class="scroll"><table>
<thead><tr><th>类型</th><th>待核说法</th><th>为什么要核</th><th>核校方法</th></tr></thead>
<tbody>{todo_rows}</tbody></table></div>"""


FIG_RE = re.compile(
    r"\[\[fig:(?P<src>[^\|\]]+)\|(?P<cap>[^\]]*)\]\]", re.I)
# 占位符：先抽出成 token，escape 正文后再替换回真正的 HTML，避免转义冲突
FIG_TOKEN = "\x00FIG{}\x00"


def _resolve_img(src: str, base_dir: Path) -> Path | None:
    """按候选顺序找图片。支持相对 out 目录、相对 cwd 两种写法。"""
    s = src.strip().replace("\\", "/")
    cands = [
        base_dir / s,
        base_dir / "frames" / re.sub(r"^(\.?test/)?frames/", "", s),
        Path(s),
        Path.cwd() / s,
    ]
    # 去掉形如 .test/frames/xxx 的前缀再试 frames/
    m = re.search(r"(?:^|/)frames/(.+)$", s)
    if m:
        cands.append(base_dir / "frames" / m.group(1))
    for c in cands:
        try:
            if c.exists() and c.is_file():
                return c
        except Exception:
            continue
    return None


def render_body(body_md: str, base_dir: Path, frames_index: dict) -> str:
    """极简 Markdown → HTML，额外支持 [[fig:相对路径|图注]] 插图语法。"""
    figs: list[str] = []

    def fig_sub(m):
        src = m.group("src").strip()
        cap = m.group("cap").strip()
        found = _resolve_img(src, base_dir)
        if not found:
            figs.append(
                f"<div class='note'>图片缺失：{esc(src)}（图注：{esc(cap)}）</div>")
            return FIG_TOKEN.format(len(figs) - 1)
        uri = img_data_uri(found)
        if not uri:
            figs.append(f"<div class='note'>图片无法读取：{esc(src)}</div>")
            return FIG_TOKEN.format(len(figs) - 1)
        # 图注里的来源链接：格式「图注 | url」
        link = ""
        if "|" in cap:
            cap, link = cap.rsplit("|", 1)
            cap, link = cap.strip(), link.strip()
            if link and not link.startswith("http"):
                link = "https://" + link
        src_html = (f"　来源：<a href='{esc(link)}' target='_blank' "
                    f"rel='noopener'>原视频</a>" if link else "")
        figs.append(f"<figure><img src='{uri}' alt='{esc(cap)}'>"
                    f"<figcaption>{esc(cap)}{src_html}</figcaption></figure>")
        return FIG_TOKEN.format(len(figs) - 1)

    text = FIG_RE.sub(fig_sub, body_md)
    text = esc(text)

    # 标题
    for lvl in (4, 3, 2, 1):
        text = re.sub(rf"^{'#' * lvl}\s+(.+)$", rf"<h{lvl}>\1</h{lvl}>",
                      text, flags=re.M)
    # 表格
    text = _md_table(text)
    # 列表：先按行把连续的 - / * / 数字 项归组，避免被空行拆散
    lines = text.split("\n")
    grouped: list[str] = []
    buf: list[str] = []

    def flush_list():
        if buf:
            grouped.append("<ul>" + "".join(buf) + "</ul>")
            buf.clear()

    for ln in lines:
        m_ul = re.match(r"^\s*[-*]\s+(.+)$", ln)
        m_ol = re.match(r"^\s*\d+\.\s+(.+)$", ln)
        if m_ul or m_ol:
            buf.append(f"<li>{(m_ul or m_ol).group(1)}</li>")
        else:
            flush_list()
            grouped.append(ln)
    flush_list()
    text = "\n".join(grouped)

    # 粗体/行内码/链接
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                  r"<a href='\2' target='_blank' rel='noopener'>\1</a>", text)
    # 段落
    blocks = re.split(r"\n\s*\n", text)
    out = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        if re.match(r"^<(h\d|ul|ol|table|figure|div|blockquote)", b):
            out.append(b)
        else:
            out.append("<p>" + b.replace("\n", "<br>") + "</p>")
    html = "\n".join(out)
    # 回填插图 HTML（此时正文已 escape，占位符本身不会被转义）
    for i, frag in enumerate(figs):
        html = html.replace(FIG_TOKEN.format(i), frag)
        html = html.replace(FIG_TOKEN.format(i).replace("\x00", ""), frag)
    # 清理 <p> 里嵌 block 元素产生的空段落
    html = re.sub(r"<p>(<figure>|<div class='scroll'>|<div class='note'>|<ul>|<h\d>)",
                  r"\1", html)
    html = re.sub(r"(</figure>|</table></div>|</div>|</ul>|</h\d>)</p>",
                  r"\1", html)
    return html


def _md_table(text: str) -> str:
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if (i + 1 < len(lines) and "|" in lines[i]
                and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1])):
            head = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<div class='scroll'><table><thead><tr>"]
            t += [f"<th>{c}</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def build(topic: str, title: str, status_rows, refs, quotes, verif,
          body_html: str, body_md: str, generated: str) -> str:
    stat = ""
    if status_rows and refs:
        stat = (f"成功平台 <b>{sum(1 for s in status_rows if s.get('status') == 'ok')}"
                f"/{len(status_rows)}</b>　·　参考条目 <b>{refs.get('total', 0)}</b>　·　"
                f"孤证 <b>{(verif or {}).get('stats', {}).get('single_source_terms', 0)}</b>")
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>{esc(title)}</h1>
<div class="meta">主题：<b>{esc(topic)}</b>　|　生成时间：{esc(generated)}　|　{stat}</div>
<div class="note"><b>本报告基于实时检索的自媒体公开内容生成</b>。
文中「原创文案」系对多条参考内容的综合梳理与再表达，非原文搬运；
所有引用均标注出处。数据以检索时为快照，不代表平台全量。</div>
{render_status(status_rows)}
{body_html}
{render_verification(verif)}
{render_references(refs)}
{render_quotes(quotes)}
<div class="ft">
<p><b>免责与说明</b>：① 榜单按互动量加权热度排序，口径见 references/hot-ranking.md；
② 未取到的平台已在「数据源状态」中如实标注，未做填补；③ 视频截帧版权归原作者，
仅作引用说明用途，图注均已标注来源；④ 孤证说法已单独标出，请勿当作共识。</p>
<p>生成工具：topic-scout（多平台选题侦察与内容蒸馏）</p>
</div></div></body></html>"""


def md_export(title, topic, status_rows, refs, quotes, verif, body_md, gen) -> str:
    L = [f"# {title}\n", f"主题：**{topic}**　|　生成时间:{gen}\n"]
    if status_rows:
        L.append("## 数据源状态\n")
        L.append("| 平台 | 可达性 | 状态 | 条数 | 说明 |")
        L.append("| --- | --- | --- | --- | --- |")
        for s in status_rows:
            L.append(f"| {s.get('platform_name')} | {s.get('access')} | "
                     f"{s.get('status')} | {s.get('count')} | "
                     f"{s.get('reason') or '—'} |")
        L.append("")
    L.append(body_md.strip())
    L.append("")
    if refs and refs.get("rows"):
        L.append("## 参考来源\n")
        L.append("| # | 平台 | 标题 | 作者 | 互动 | 链接 |")
        L.append("| --- | --- | --- | --- | --- | --- |")
        for i, r in enumerate(refs["rows"], 1):
            L.append(f"| {i} | {r.get('platform_name')} | {r.get('title')} | "
                     f"{r.get('author')} | {fmt_metrics(r.get('metrics'))} | "
                     f"{r.get('url')} |")
        L.append("")
    if quotes and quotes.get("quotes"):
        L.append("## 金句出处\n")
        for q in quotes["quotes"]:
            L.append(f"- 「{q.get('quote')}」 —— {q.get('platform')}/"
                     f"{q.get('author')}，《{q.get('title')}》{q.get('url')}")
        L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="图文并茂报告生成")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--title", default="")
    ap.add_argument("--topic", default="")
    ap.add_argument("--body", default="", help="正文 markdown 文件")
    ap.add_argument("--collected", default="", help="补充读取数据源状态")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    refs = load(out / "references.json", {}) or {}
    quotes = load(out / "quotes.json", {}) or {}
    verif = load(out / "verification.json", {}) or {}
    outline = load(out / "outline.json", {}) or {}
    collected = load(args.collected or (out / "collected.json"), {}) or {}

    topic = args.topic or refs.get("topic") or outline.get("topic") or "未命名主题"
    title = args.title or f"{topic} —— 全网内容蒸馏与原创梳理"

    status_rows = []
    for r in collected.get("results", []):
        status_rows.append({
            "platform_name": r.get("platform_name"), "platform": r.get("platform"),
            "access": r.get("access"), "status": r.get("status"),
            "count": len(r.get("items", [])), "reason": r.get("reason", ""),
        })

    body_md = ""
    if args.body and Path(args.body).exists():
        body_md = Path(args.body).read_text(encoding="utf-8")
    elif (out / "body.md").exists():
        body_md = (out / "body.md").read_text(encoding="utf-8")
    if not body_md:
        body_md = ("## 正文\n\n> ⚠️ 尚未提供正文。请在 out/body.md 写入正文后"
                   "重跑本脚本，或在命令里用 --body 指定。\n\n"
                   "正文中插图语法：`[[fig:frames/01_xxx/f02.jpg|图注文字|原链接]]`\n")

    import time
    gen = time.strftime("%Y-%m-%d %H:%M")
    body_html = render_body(body_md, out, load(out / "frames" / "frames_index.json", {}) or {})

    html_out = build(title, topic, status_rows, refs, quotes, verif,
                     body_html, body_md, gen)
    (out / "report.html").write_text(html_out, encoding="utf-8")
    (out / "report.md").write_text(
        md_export(title, topic, status_rows, refs, quotes, verif, body_md, gen),
        encoding="utf-8")

    # 自检
    problems = []
    if "<img src='' " in html_out:
        problems.append("存在空 src 的图片")
    if re.search(r"[A-Za-z]:\\\\", html_out):
        problems.append("HTML 里含 Windows 绝对路径（应内联为 data URI）")
    if not refs.get("rows"):
        problems.append("缺少参考来源表")
    print(f"→ {out / 'report.html'}")
    print(f"→ {out / 'report.md'}")
    if problems:
        print("\n自检告警:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n自检通过：无本地绝对路径、参考来源已列表、图注含来源。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
