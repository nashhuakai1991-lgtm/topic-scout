#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频截帧配图 —— 从采集到的视频里抽关键帧，供图文并茂使用。

用户选择「视频截帧」作为配图策略，所以本脚本是配图主通道。

流程:
    1) 用 yt-dlp 下载目标视频（走用户选定平台的直链；本机已装 yt-dlp）
    2) ffmpeg 按比例抽 N 帧 + 拼 contact sheet
    3) 输出 frames_index.json，供 AI 用 Read 工具看 sheet 挑帧
    4) 挑好帧后写进报告，并在图注标注来源条目（承 users 要求：图也要能溯源）

⚠️ 版权与署名:
    截帧来自他人作品，**必须**在图注标注来源（平台 + 作者 + 链接）。
    不得声称截帧为原创。若用户计划商用，须提醒其自行确认授权。

用法:
    # 抽帧
    python grab_frames.py --url "https://www.bilibili.com/video/BV1xx" \
        --out ./out/frames --per-video 5
    # 批量（从 collected.json 里按 rank 选）
    python grab_frames.py --collected ./out/collected.json --pick 1,2 \
        --out ./out/frames
产出:
    出片目录：<out>/<slug>/*.jpg  +  <out>/sheets/sheet_N.jpg
    <out>/frames_index.json
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def have(cmd: str) -> bool:
    return bool(shutil.which(cmd))


def slugify(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", s or "video").strip("_")
    return (s[:n] or "video")


def duration(path: Path) -> float:
    p = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", p.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def download(url: str, dest_dir: Path, slug: str) -> tuple[Path | None, str]:
    """yt-dlp 下载。返回 (文件路径, 说明)。失败返回 (None, 原因)。"""
    from ytdlp_util import ytdlp_cmd
    prefix = ytdlp_cmd()
    if not prefix:
        return None, "未找到 yt-dlp（既不在 PATH 也无 yt_dlp 模块）"
    tmpl = str(dest_dir / f"{slug}.%(ext)s")
    cmd = prefix + ["-f", "bv*[height<=720]+ba/b[height<=720]/b",
                    "--merge-output-format", "mp4", "--no-warnings",
                    "-o", tmpl, url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=300)
    except subprocess.TimeoutExpired:
        return None, "下载超时（可能需登录或网络受限）"
    if p.returncode != 0:
        tail = (p.stderr or "")[-300:]
        return None, f"yt-dlp 失败: {tail.strip()[:200]}"
    for f in sorted(dest_dir.glob(f"{slug}.*")):
        if f.suffix.lower() in {".mp4", ".mkv", ".webm", ".flv", ".m4a"}:
            return f, "ok"
    return None, "下载完成但未找到视频文件"


def grab(video: Path, out_dir: Path, per_video: int = 5) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = duration(video)
    outs: list[str] = []
    ratios = [(i + 0.5) / per_video for i in range(per_video)]
    for j, r in enumerate(ratios):
        t = max(1.0, dur * r)
        op = out_dir / f"f{j:02d}.jpg"
        subprocess.run(
            [FFMPEG, "-y", "-ss", f"{t:.1f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=960:-2", "-q:v", "3", str(op)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if op.exists() and op.stat().st_size > 3000:
            outs.append(str(op))
    return outs


def contact_sheet(entries: list[dict], out_dir: Path, per_video: int,
                  cols: int = 5, rows_per_sheet: int = 4) -> list[str]:
    if not HAS_PIL or not entries:
        return []
    sheets_dir = out_dir / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    TH_W, TH_H = 300, 170
    made = []
    for pg, start in enumerate(range(0, len(entries), rows_per_sheet)):
        rows = entries[start:start + rows_per_sheet]
        W = TH_W * cols + 12
        H = (TH_H + 44) * len(rows) + 12
        sheet = Image.new("RGB", (W, H), (24, 26, 32))
        d = ImageDraw.Draw(sheet)
        y = 6
        for e in rows:
            d.text((10, y + 3),
                   f"# {e['key']}  {e['title'][:52]}",
                   fill=(255, 210, 120))
            y += 22
            for j in range(cols):
                x = 6 + j * TH_W
                ims = e.get("frames", [])
                if j < len(ims) and ims[j] and Path(ims[j]).exists():
                    try:
                        im = Image.open(ims[j]).convert("RGB")
                        im.thumbnail((TH_W - 8, TH_H - 8))
                        sheet.paste(im, (x + 4, y + 2))
                    except Exception:
                        pass
                else:
                    d.rectangle([x + 4, y + 2, x + TH_W - 4, y + TH_H - 4],
                                outline=(110, 60, 60))
                d.text((x + 6, y + TH_H - 13), f"f{j:02d}", fill=(180, 220, 255))
            y += TH_H + 6
        sp = sheets_dir / f"sheet_{pg + 1}.jpg"
        sheet.save(sp, "JPEG", quality=88)
        made.append(str(sp))
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description="视频截帧配图")
    ap.add_argument("--url", action="append", default=None, help="视频链接，可重复")
    ap.add_argument("--collected", default="", help="从 collected.json 按 --pick 选")
    ap.add_argument("--pick", default="", help="rank 列表，如 1,2,3（配合 --collected）")
    ap.add_argument("--out", default="./out/frames")
    ap.add_argument("--per-video", type=int, default=5)
    args = ap.parse_args()

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    targets: list[dict] = []
    if args.collected:
        data = json.loads(Path(args.collected).read_text(encoding="utf-8"))
        picks = {int(x) for x in args.pick.split(",") if x.strip().isdigit()}
        for r in data.get("results", []):
            for it in r.get("items", []):
                if not picks or it.get("rank") in picks:
                    targets.append({
                        "key": f"[{it.get('platform_name') or r.get('platform_name')}"
                               f"#{it.get('rank')}]",
                        "title": it.get("title", ""),
                        "url": it.get("url", ""),
                        "platform": it.get("platform_name") or r.get("platform_name"),
                        "author": it.get("author", ""),
                    })
    for u in (args.url or []):
        targets.append({"key": f"[直链#{len(targets) + 1}]", "title": u,
                        "url": u, "platform": "", "author": ""})

    if not targets:
        print("没有目标视频。用 --url 或 --collected+--pick 指定。", file=sys.stderr)
        return 2

    entries: list[dict] = []
    for i, t in enumerate(targets, 1):
        slug = slugify(f"{i:02d}_{t['title']}")
        vdir = out / slug
        vdir.mkdir(parents=True, exist_ok=True)
        if not t["url"]:
            print(f"  [{i}/{len(targets)}] {t['key']} ⛔ 无链接，跳过")
            entries.append({**t, "frames": [], "status": "no-url"})
            continue
        print(f"  [{i}/{len(targets)}] {t['key']} 下载中…")
        vp, msg = download(t["url"], vdir, slug)
        if vp is None:
            print(f"        ⛔ {msg}")
            entries.append({**t, "frames": [], "status": "download-failed",
                            "reason": msg})
            continue
        frames = grab(vp, vdir, args.per_video)
        print(f"        ✅ 抽帧 {len(frames)}/{args.per_video}")
        entries.append({**t, "frames": frames, "status": "ok",
                        "video": str(vp)})

    sheets = contact_sheet(entries, out, args.per_video)
    (out / "frames_index.json").write_text(
        json.dumps({"entries": entries, "sheets": sheets},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for e in entries if e["status"] == "ok")
    print(f"\n完成：{ok}/{len(entries)} 条成片，contact sheet {len(sheets)} 张")
    print(f"→ {out / 'frames_index.json'}")
    if sheets:
        print("下一步：用 Read 看 sheets/sheet_N.jpg 挑帧，"
              "并把选中帧写进报告，**图注须标注来源条目**。")
    print("\n⚠️ 截帧来自他人作品，图注必须标注「平台+作者+原链接」，"
          "不得声称为原创。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
