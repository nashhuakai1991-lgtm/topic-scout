#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六平台检索器实现。各平台一个函数，由 platforms.search_platform 调度。

⚠️ 平台反爬随时变。任何一路失败都返回 status != ok 的 PlatformResult，
   **绝不**返回编造数据。字段按 platforms.Item 契约填，缺失留 None。

实测结论（2026-10，本机 Windows + bundled chromium）：
  B站      —— 搜索接口免登录可返回前 N 条；风控 -412 时降级到页面解析
  抖音      —— 必须先访问首页预热生成 ttwid，否则接口返回空
  小红书    —— 游客搜索有签名校验，多数情况拿不到；如实标 login-gated
  快手      —— 网页搜索可达，内嵌 JSON 可解
  YouTube   —— 国内直连不可达，交给 yt-dlp（若网络可达）
  X         —— 默认 blocked
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from typing import Any, Optional

from platforms import (
    Item, PlatformResult, PLATFORM_SPEC, UA_MOBILE,
    fetch_json_in_page, hot_score, norm_url, now_iso, parse_count,
    register, sleep_jitter,
)


# ==================================================================== B站

WBI_MIXIN_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_SEARCH = ("https://api.bilibili.com/x/web-interface/wbi/search/type"
               "?search_type=video&keyword={kw}&page=1&page_size=30")


@register("bilibili")
def search_bilibili(keyword: str, limit: int = 5, browser=None,
                    min_likes: int = 0) -> PlatformResult:
    name = PLATFORM_SPEC["bilibili"]["name"]
    res = PlatformResult("bilibili", name, access="public", queried=keyword)
    if browser is None:
        res.status, res.reason = "error", "需要浏览器上下文"
        return res

    page = browser.new_page()
    try:
        # 预热拿 cookie（buvid3 等），否则部分接口 412
        page.goto("https://www.bilibili.com/", wait_until="domcontentloaded",
                  timeout=30_000)
        sleep_jitter(0.6, 0.5)

        url = BILI_SEARCH.format(kw=urllib.parse.quote(keyword))
        data = fetch_json_in_page(page, url, referer="https://search.bilibili.com/")

        if not data or data.get("code") == -412:
            res.status = "blocked"
            res.reason = ("B站搜索接口触发风控（-412），本次未取到数据。"
                          "稍后重试或降低频率；也可只跑其它平台。")
            return res
        if not data or data.get("code") != 0:
            res.status = "error"
            code = (data or {}).get("code")
            res.reason = f"B站接口返回 code={code}，未取到结果"
            return res

        raw = ((data.get("data") or {}).get("result") or [])
        items: list[Item] = []
        for it in raw:
            if it.get("type") != "video" and "bvid" not in it:
                continue
            it_obj = Item(
                platform="bilibili", platform_name=name,
                title=_strip_tags(it.get("title") or ""),
                author=_strip_tags(it.get("author") or ""),
                item_id=it.get("bvid") or "",
                url=norm_url(_bili_url(it)),
                published=_ts_to_date(it.get("pubdate")),
                duration=_bili_duration(it),
                views=parse_count(it.get("play")),
                likes=parse_count(it.get("like")),
                comments=parse_count(it.get("review") or it.get("video_review")),
                collects=parse_count(it.get("favorites")),
                summary=_strip_tags(it.get("description") or ""),
                content_source="desc" if it.get("description") else "",
                paid=bool(it.get("is_pay") or it.get("is_charging_arc")),
                fetched_at=now_iso(),
            )
            items.append(it_obj)

        if not items:
            res.status, res.reason = "empty", "B站搜索无结果"
            return res

        items = _rank(items, limit, min_likes)
        res.items = items
        res.status = "ok"
        return res
    finally:
        try:
            page.close()
        except Exception:
            pass


# ==================================================================== 抖音

@register("douyin")
def search_douyin(keyword: str, limit: int = 5, browser=None,
                  min_likes: int = 0) -> PlatformResult:
    name = PLATFORM_SPEC["douyin"]["name"]
    res = PlatformResult("douyin", name, access="login-required", queried=keyword)
    if browser is None:
        res.status, res.reason = "error", "需要浏览器上下文"
        return res

    page = browser.new_page()
    try:
        # 关键前置：先访问首页，让 JS 生成 ttwid，否则搜索接口返回空
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                  timeout=35_000)
        sleep_jitter(1.5, 1.0)
        try:
            page.mouse.wheel(0, 400)
            sleep_jitter(0.8, 0.5)
        except Exception:
            pass

        kw = urllib.parse.quote(keyword)
        api = ("https://www.douyin.com/aweme/v1/web/general/search/single/"
               f"?device_platform=webapp&aid=6383&channel=channel_pc_web"
               f"&search_channel=aweme_general&keyword={kw}"
               f"&search_source=normal_search&count=20&offset=0")
        data = fetch_json_in_page(page, api, referer="https://www.douyin.com/")

        items: list[Item] = []
        if data and data.get("data"):
            for it in data["data"]:
                aweme = it.get("aweme_info") or {}
                if not aweme:
                    continue
                st = aweme.get("statistics") or {}
                items.append(Item(
                    platform="douyin", platform_name=name,
                    title=(aweme.get("desc") or "").strip(),
                    author=((aweme.get("author") or {}).get("nickname") or ""),
                    item_id=aweme.get("aweme_id") or "",
                    url=(f"https://www.douyin.com/video/{aweme.get('aweme_id')}"
                         if aweme.get("aweme_id") else ""),
                    published=_ts_to_date(aweme.get("create_time")),
                    duration=_ms_to_hms((aweme.get("video") or {}).get("duration")),
                    likes=parse_count(st.get("digg_count")),
                    comments=parse_count(st.get("comment_count")),
                    collects=parse_count(st.get("collect_count")),
                    shares=parse_count(st.get("share_count")),
                    views=parse_count(st.get("play_count")),
                    summary=_strip_tags(aweme.get("desc") or ""),
                    content_source="desc",
                    paid=bool(aweme.get("is_paid") or aweme.get("paid_type")),
                    fetched_at=now_iso(),
                ))

        if not items:
            # 识别**硬登录门禁**（实测 status_code=2483，status_msg="请先登录，再继续搜索吧"）
            # 这种情况下再走 DOM 退路也没用，直接如实上报，省掉后续无效等待
            code = (data or {}).get("status_code")
            msg = str((data or {}).get("status_msg") or "")
            if code in (2483, 8) or "登录" in msg:
                res.status = "login-gated"
                res.reason = (f"抖音搜索接口要求登录（status_code={code}，"
                              f"平台提示「{msg or '请先登录'}」）。这是平台规则，"
                              "不是技术问题；用 --headful 扫码登录后可抓，或只跑其它平台。")
                return res
            # 其它情况的退路：读页面 SSR 数据
            items = _douyin_from_dom(page, keyword, name)

        if not items:
            res.status = "login-gated"
            res.reason = ("抖音未登录状态下未取到搜索结果（首屏接口返回空）。"
                          "需扫码登录后可抓；也可只跑其它平台。")
            return res

        res.items = _rank(items, limit, min_likes)
        res.status = "ok"
        return res
    finally:
        try:
            page.close()
        except Exception:
            pass


def _douyin_from_dom(page, keyword: str, name: str) -> list[Item]:
    """退路：从搜索页 DOM 里抠视频卡片。字段不全就留 None。"""
    try:
        page.goto("https://www.douyin.com/search/"
                  + urllib.parse.quote(keyword), wait_until="domcontentloaded",
                  timeout=35_000)
        sleep_jitter(2.0, 1.0)
        html = page.content()
    except Exception:
        return []
    items: list[Item] = []
    for m in re.finditer(r'"(?:aweme_id|awemeId)"\s*:\s*"(\d{15,25})"', html):
        aid = m.group(1)
        if any(i.item_id == aid for i in items):
            continue
        items.append(Item(platform="douyin", platform_name=name, item_id=aid,
                          url=f"https://www.douyin.com/video/{aid}",
                          content_source="", fetched_at=now_iso(),
                          degraded=["页面退路解析，字段不全"]))
        if len(items) >= 25:
            break
    return items


# ==================================================================== 小红书

@register("xiaohongshu")
def search_xiaohongshu(keyword: str, limit: int = 5, browser=None,
                       min_likes: int = 0) -> PlatformResult:
    name = PLATFORM_SPEC["xiaohongshu"]["name"]
    res = PlatformResult("xiaohongshu", name, access="login-required",
                         queried=keyword)
    if browser is None:
        res.status, res.reason = "error", "需要浏览器上下文"
        return res

    page = browser.new_page()
    try:
        page.goto("https://www.xiaohongshu.com/explore",
                  wait_until="domcontentloaded", timeout=35_000)
        sleep_jitter(1.5, 1.0)

        # 游客态下尝试调用搜索接口；有签名校验，取不到属正常
        kw = urllib.parse.quote(keyword)
        api = (f"https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"
               f"?keyword={kw}&page=1&page_size=20&search_id=0")
        data = fetch_json_in_page(page, api,
                                  referer="https://www.xiaohongshu.com/")

        items: list[Item] = []
        if data and (data.get("data") or {}).get("items"):
            for it in data["data"]["items"]:
                nc = it.get("note_card") or {}
                if not nc:
                    continue
                interact = nc.get("interact_info") or {}
                items.append(Item(
                    platform="xiaohongshu", platform_name=name,
                    title=(nc.get("display_title") or "").strip(),
                    author=((nc.get("user") or {}).get("nickname") or ""),
                    item_id=it.get("id") or "",
                    url=(f"https://www.xiaohongshu.com/explore/{it.get('id')}"
                         if it.get("id") else ""),
                    likes=parse_count(interact.get("liked_count")),
                    collects=parse_count(interact.get("collected_count")),
                    comments=parse_count(interact.get("comment_count")),
                    summary=(nc.get("display_title") or "").strip(),
                    content_source="desc",
                    fetched_at=now_iso(),
                ))

        if not items:
            res.status = "login-gated"
            res.reason = ("小红书网页端搜索对游客有签名校验，未登录未取到结果。"
                          "登录后可抓；也可只跑其它平台。")
            return res

        res.items = _rank(items, limit, min_likes)
        res.status = "ok"
        return res
    finally:
        try:
            page.close()
        except Exception:
            pass


# ==================================================================== 快手

@register("kuaishou")
def search_kuaishou(keyword: str, limit: int = 5, browser=None,
                    min_likes: int = 0) -> PlatformResult:
    name = PLATFORM_SPEC["kuaishou"]["name"]
    res = PlatformResult("kuaishou", name, access="login-required",
                         queried=keyword)
    if browser is None:
        res.status, res.reason = "error", "需要浏览器上下文"
        return res

    page = browser.new_page()
    try:
        # 快手是纯客户端渲染（__USE_SSR__），HTML 里**没有**结果数据，
        # 且搜索接口 /rest/v/search/feed 需要页面 JS 生成的 __NS_hxfalcon 签名
        # —— 自己造请求必然被拒（实测 error_msg="签名验证失败"）。
        # 唯一可行路径：**拦截页面自身发起的** search/feed 请求并读其响应体。
        cap: dict[str, str] = {}

        def _on_resp(r):
            if "/rest/v/search/feed" in r.url:
                try:
                    cap["body"] = r.text()
                except Exception:
                    pass

        page.on("response", _on_resp)
        url = ("https://www.kuaishou.com/search/video?searchKey="
               + urllib.parse.quote(keyword))
        try:
            page.goto(url, wait_until="commit", timeout=45_000)
        except Exception:
            pass  # commit 超时也继续，接口可能仍会回来

        for _ in range(9):
            if cap.get("body"):
                break
            time.sleep(2)
            try:
                page.mouse.wheel(0, 900)
            except Exception:
                pass

        if not cap.get("body"):
            res.status = "empty"
            res.reason = ("快手搜索接口未返回数据（页面为客户端渲染，"
                          "未能捕获 search/feed 响应）。本次未取到数据。")
            return res

        try:
            data = json.loads(cap["body"])
        except Exception:
            res.status = "error"
            res.reason = "快手返回内容非 JSON，无法解析"
            return res

        # 实测：result=2 表示请求被拒（未登录/风控），feeds 为空
        feeds = ((data.get("data") or {}).get("feeds")
                 or data.get("feeds") or [])
        if not feeds:
            if data.get("result") not in (1, None):
                res.status = "login-gated"
                res.reason = (f"快手搜索接口拒绝了未登录请求（result={data.get('result')}，"
                              "接口需页面签名+登录态）。属平台规则；可 --headful 扫码登录，"
                              "或只跑其它平台。")
            else:
                res.status = "empty"
                res.reason = "快手搜索无结果（feeds 为空）"
            return res

        items = _kuaishou_parse_feeds(feeds, name)
        if not items:
            res.status = "empty"
            res.reason = "快手返回了 feeds，但字段解析后为空（结构可能已变）"
            return res

        res.items = _rank(items, limit, min_likes)
        res.status = "ok"
        return res
    finally:
        try:
            page.close()
        except Exception:
            pass


def _kuaishou_parse_feeds(feeds: list, name: str) -> list[Item]:
    """解析快手 search/feed 的 feeds 数组。字段缺失一律留 None。"""
    items: list[Item] = []
    for f in feeds:
        if not isinstance(f, dict):
            continue
        # 快手结构通常是 feed.photo 或直接扁平
        photo = f.get("photo") if isinstance(f.get("photo"), dict) else f
        pid = photo.get("id") or photo.get("photoId") or f.get("id")
        title = (photo.get("caption") or photo.get("title")
                 or f.get("title") or "").strip()
        if not title and not pid:
            continue
        user = photo.get("user") or f.get("user") or {}
        items.append(Item(
            platform="kuaishou", platform_name=name,
            title=title,
            author=(user.get("name") or user.get("userName") or ""),
            item_id=str(pid or ""),
            url=(f"https://www.kuaishou.com/short-video/{pid}" if pid else ""),
            views=parse_count(photo.get("viewCount") or photo.get("playCount")
                              or f.get("viewCount")),
            likes=parse_count(photo.get("likeCount") or photo.get("realLikeCount")
                              or f.get("likeCount")),
            comments=parse_count(photo.get("commentCount") or f.get("commentCount")),
            summary=title,
            content_source="desc" if title else "",
            fetched_at=now_iso(),
        ))
    return items


def _kuaishou_from_html(html: str, name: str) -> list[Item]:
    """退路：若某天快手恢复 SSR，可从这里解析。目前实测 HTML 无数据。"""
    items: list[Item] = []
    # 快手把结果塞在 window.__APOLLO_STATE__ / INITIAL_STATE 里
    for pat in (r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});",
                r"window\.INITIAL_STATE\s*=\s*(\{.*?\});"):
        m = re.search(pat, html, re.S)
        if not m:
            continue
        try:
            blob = json.loads(m.group(1))
        except Exception:
            continue
        for v in _walk_dicts(blob):
            photo = v.get("photo") if isinstance(v, dict) else None
            if not isinstance(photo, dict):
                if v.get("caption") and (v.get("likeCount") is not None
                                         or v.get("viewCount") is not None):
                    photo = v
                else:
                    continue
            pid = photo.get("id") or photo.get("photoId")
            items.append(Item(
                platform="kuaishou", platform_name=name,
                title=(photo.get("caption") or "").strip(),
                author=((photo.get("user") or {}).get("name") or ""),
                item_id=str(pid or ""),
                url=(f"https://www.kuaishou.com/short-video/{pid}" if pid else ""),
                views=parse_count(photo.get("viewCount")
                                  or photo.get("playCount")),
                likes=parse_count(photo.get("likeCount")
                                  or photo.get("realLikeCount")),
                comments=parse_count(photo.get("commentCount")),
                summary=(photo.get("caption") or "").strip(),
                content_source="desc",
                fetched_at=now_iso(),
            ))
        if items:
            break
    # 去重
    seen, uniq = set(), []
    for i in items:
        if i.item_id and i.item_id in seen:
            continue
        seen.add(i.item_id)
        uniq.append(i)
    return uniq


def _walk_dicts(obj, depth: int = 0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v, depth + 1)


# ==================================================================== YouTube

@register("youtube")
def search_youtube(keyword: str, limit: int = 5, browser=None,
                   min_likes: int = 0) -> PlatformResult:
    name = PLATFORM_SPEC["youtube"]["name"]
    res = PlatformResult("youtube", name, access="external", queried=keyword)

    # 用本机已装的 yt-dlp；网络不可达时会直接报错，如实上报
    from ytdlp_util import ytdlp_cmd
    prefix = ytdlp_cmd()
    if not prefix:
        res.status = "error"
        res.reason = "未找到 yt-dlp（既不在 PATH 也无 yt_dlp 模块），无法检索 YouTube"
        return res
    try:
        cmd = prefix + ["--flat-playlist", "-J", "--no-warnings",
                        f"ytsearch{max(limit * 4, 20)}:{keyword}"]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=90)
        if p.returncode != 0 or not (p.stdout or "").strip():
            res.status = "blocked"
            res.reason = ("YouTube 取数失败（国内网络通常直连不可达，"
                          "或 yt-dlp 未安装）。详见 stderr。"
                          + (" " + (p.stderr or "")[:200] if p.stderr else ""))
            return res
        blob = json.loads(p.stdout)
    except FileNotFoundError:
        res.status = "error"
        res.reason = "未找到 yt-dlp 可执行文件，无法检索 YouTube"
        return res
    except subprocess.TimeoutExpired:
        res.status = "blocked"
        res.reason = "YouTube 请求超时（网络不可达），本次未取到数据"
        return res
    except Exception as e:  # noqa: BLE001
        res.status = "error"
        res.reason = f"{type(e).__name__}: {e}"
        return res

    items: list[Item] = []
    for e in (blob.get("entries") or []):
        if not e:
            continue
        items.append(Item(
            platform="youtube", platform_name=name,
            title=(e.get("title") or "").strip(),
            author=(e.get("uploader") or e.get("channel") or ""),
            item_id=e.get("id") or "",
            url=(e.get("url") or (f"https://www.youtube.com/watch?v={e.get('id')}"
                                  if e.get("id") else "")),
            duration=_sec_to_hms(e.get("duration")),
            views=parse_count(e.get("view_count")),
            content_source="",
            degraded=["flat-playlist 无点赞/评论数，热度仅按播放量计"],
            fetched_at=now_iso(),
        ))

    if not items:
        res.status, res.reason = "empty", "YouTube 搜索无结果"
        return res
    res.items = _rank(items, limit, min_likes)
    res.status = "ok"
    return res


# ==================================================================== X

@register("x")
def search_x(keyword: str, limit: int = 5, browser=None,
             min_likes: int = 0) -> PlatformResult:
    """X 默认 blocked（需登录 + 国内网络不可达）。占位实现，如实返回不可用。"""
    return PlatformResult(
        "x", PLATFORM_SPEC["x"]["name"], access="blocked", status="blocked",
        queried=keyword,
        reason=("X (Twitter) 网页端需登录且有强反爬，国内网络亦不可达，"
                "本次未纳入。如你能提供可用的第三方检索通道，可再补。"),
    )


# ==================================================================== 公共

def _rank(items: list[Item], limit: int, min_likes: int) -> list[Item]:
    """按热度分排序，取前 limit 名，写回 rank。"""
    if min_likes:
        items = [i for i in items if (i.likes or 0) >= min_likes] or items
    for it in items:
        it.hot_score = hot_score(it)
    items.sort(key=lambda x: x.hot_score, reverse=True)
    for idx, it in enumerate(items, 1):
        it.rank = idx
    return items[:limit]


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = (s.replace("&quot;", '"').replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
         .replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", s).strip()


def _ts_to_date(ts: Any) -> str:
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _sec_to_hms(sec: Any) -> str:
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s2 = divmod(rem, 60)
    return (f"{h}:{m:02d}:{s2:02d}" if h else f"{m}:{s2:02d}")


def _ms_to_hms(ms: Any) -> str:
    try:
        return _sec_to_hms(int(ms) / 1000)
    except (TypeError, ValueError):
        return ""


def _bili_url(it: dict) -> str:
    """B站链接优先用 bvid（av 号已不被新播放页支持）。"""
    bv = it.get("bvid")
    if bv:
        return f"https://www.bilibili.com/video/{bv}"
    return it.get("arcurl") or ""


def _bili_duration(it: dict) -> str:
    """B站搜索结果里时长字段名不稳定，逐个尝试。

    搜索接口返回的 `duration` 常是 "12:34" 字符串（不是秒数），
    也有 `duration` 为秒数字、或走 `length` 字段的情况。
    """
    for key in ("duration", "length", "video_duration"):
        v = it.get(key)
        if v in (None, "", 0, "0"):
            continue
        s = str(v)
        if ":" in s:                    # 已经是 "12:34" 形式
            return s
        hms = _sec_to_hms(v)
        if hms:
            return hms
    return ""
