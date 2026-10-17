#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台适配层 —— 六平台检索器 + 热度打分 + 统一的 source_status 契约。

设计原则（重要，先读）：
  1. **绝不编造**。任何一个平台取不到数据，就写进 source_status 并给出原因，
     绝不返回空壳数据冒充「已检索」。
  2. **真实浏览器优先**。用 bundled chromium 走页面/接口，不用 requests 硬打
     （本机 DNS 有 fake-ip 劫持，部分域名 requests 直接超时；浏览器网络栈可用）。
  3. **免登录优先**。能免登录拿到的平台就免登录；需要登录的平台在
     source_status 里标 `login-gated`，并让上层去问用户。
  4. **打分口径统一**。各平台指标名不同（播放/点赞/收藏/转发），统一映射成
     `views / likes / comments / collects / shares` 后加权算热度分，口径写进
     references/hot-ranking.md。

各平台字段与可达性均以 2026-10 实测为准；平台改版时改本文件顶部的
PLATFORM_SPEC 即可，其它脚本不用动。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ------------------------------------------------------------------ 常量

DEFAULT_CHROME = (
    r"C:\Users\Administrator\AppData\Local\ms-playwright"
    r"\chromium-1234\chrome-win64\chrome.exe"
)
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)

# 热度分权重（口径见 references/hot-ranking.md）
HOT_WEIGHTS = {
    "likes": 1.0,
    "comments": 1.6,
    "collects": 1.4,
    "shares": 2.0,
    "views": 0.02,   # 播放量量级大，压到 1/50 量级避免淹没互动
}

# 平台规格：显式声明每个平台「用什么方式、能否免登录、有哪些指标」
PLATFORM_SPEC: dict[str, dict[str, Any]] = {
    "bilibili": {
        "name": "B站",
        "access": "public",              # public / login / blocked
        "metrics": ["views", "likes", "comments", "collects", "shares"],
        "note": "搜索接口 api.bilibili.com/x/web-interface/wbi/search/type，"
                "通常免登录取前若干条；风控时返回 -412。",
    },
    "douyin": {
        "name": "抖音",
        "access": "login-required",
        "metrics": ["likes", "comments", "collects", "shares"],
        "note": "搜索接口**硬性要求登录**（实测 status_code=2483）。免登录可用的替代："
                "热榜接口 fetch_hotlist.py；以及搜索引擎索引（site:douyin.com）。",
    },
    "xiaohongshu": {
        "name": "小红书",
        "access": "login-required",
        "metrics": ["likes", "collects", "comments"],
        "note": "网页端搜索接口有 x-s/x-t 签名校验，游客拿不到，**需登录**。",
    },
    "kuaishou": {
        "name": "快手",
        "access": "login-required",
        "metrics": ["views", "likes", "comments"],
        "note": "纯客户端渲染（__USE_SSR__），HTML 无数据；搜索接口需页面签名 + 登录态"
                "（未登录 result=2）。免登录的榜单亦不可得（走 GraphQL + 签名）。",
    },
    "youtube": {
        "name": "YouTube",
        "access": "external",
        "metrics": ["views", "likes", "comments"],
        "note": "国内网络直连不可达。若用户已配置代理/可访问，用 yt-dlp "
                "（本机已装）走 ytsearchN 取结果，避免自建解析。",
    },
    "x": {
        "name": "X (Twitter)",
        "access": "blocked",
        "metrics": ["likes", "comments", "shares"],
        "note": "网页端需登录且有强反爬；国内网络亦不可达。默认标 unavailable，"
                "只有用户明确提供可用的第三方检索通道时才纳入。",
    },
}

# ------------------------------------------------------------------ 数据契约


@dataclass
class Item:
    """一条检索结果。字段缺失一律 None，绝不用 0 冒充缺失。"""
    platform: str
    platform_name: str = ""
    rank: int = 0
    title: str = ""
    author: str = ""
    url: str = ""
    published: str = ""
    duration: str = ""
    item_id: str = ""
    views: Optional[int] = None
    likes: Optional[int] = None
    comments: Optional[int] = None
    collects: Optional[int] = None
    shares: Optional[int] = None
    hot_score: float = 0.0
    # 内容线索（用于后续蒸馏；没有就是没有）
    summary: str = ""
    content_source: str = ""        # transcript / subtitle / desc / title-only
    paid: bool = False              # 充电/付费/会员专属
    degraded: list[str] = field(default_factory=list)
    fetched_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlatformResult:
    platform: str
    platform_name: str
    access: str = ""
    status: str = "ok"        # ok / empty / login-gated / blocked / error / skipped
    reason: str = ""
    queried: str = ""
    items: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "platform_name": self.platform_name,
            "access": self.access,
            "status": self.status,
            "reason": self.reason,
            "queried": self.queried,
            "count": len(self.items),
            "items": [i.to_dict() if isinstance(i, Item) else i for i in self.items],
        }


# ------------------------------------------------------------------ 工具


def parse_count(v: Any) -> Optional[int]:
    """把 '1.2万' / '3.4亿' / '1234' / '12.3w' / '—' 解析成整数。

    无法解析或空值 -> None（**不是 0**）。缺失与 0 必须区分。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace(" ", "")
    if not s or s in {"-", "—", "暂无", "无", "N/A", "n/a"}:
        return None
    m = re.match(r"^([\d.]+)\s*([万亿wWkK]?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    mult = {"万": 10_000, "w": 10_000, "W": 10_000,
            "亿": 100_000_000, "k": 1_000, "K": 1_000}.get(unit, 1)
    return int(num * mult)


def hot_score(it: Item) -> float:
    """按 HOT_WEIGHTS 加权求和。缺失指标按 0 计入求和项，但会记 degraded。"""
    total = 0.0
    missing = []
    for key, w in HOT_WEIGHTS.items():
        val = getattr(it, key, None)
        if val is None:
            missing.append(key)
            continue
        total += val * w
    if missing:
        it.degraded.append("热度缺指标:" + ",".join(missing))
    return round(total, 2)


def norm_url(u: str) -> str:
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def is_available(platform: str) -> bool:
    return PLATFORM_SPEC.get(platform, {}).get("access") != "blocked"


def platform_name(platform: str) -> str:
    return PLATFORM_SPEC.get(platform, {}).get("name", platform)


# ------------------------------------------------------------------ 浏览器


class Browser:
    """bundled chromium 的轻量包装。上下文管理器用法：

        with Browser(headless=True) as b:
            page = b.new_page()

    两种进阶用法（用于需要登录的平台）：
      1. **持久化 profile**：`Browser(persistent_dir=..., headless=False)`
         登录态落盘，后续同 profile 复用，不必反复扫码。
      2. **导入已有 cookie**：`Browser(cookies_file="cookies.json")`
         把从本机浏览器导出的 cookie 注入上下文（见 import_login.py）。
    """

    def __init__(self, chrome: str = "", headless: bool = True, proxy: str = "",
                 persistent_dir: str = "", cookies_file: str = ""):
        self.chrome = chrome or DEFAULT_CHROME
        self.headless = headless
        self.proxy = proxy
        self.persistent_dir = persistent_dir
        self.cookies_file = cookies_file
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        kw: dict[str, Any] = {"headless": self.headless}
        ctx_kw = {
            "user_agent": UA_DESKTOP,
            "locale": "zh-CN",
            "viewport": {"width": 1440, "height": 900},
        }
        if self.proxy:
            ctx_kw["proxy"] = {"server": self.proxy}

        if self.persistent_dir:
            # 持久化上下文：登录态存盘，复用免重扫
            import os
            os.makedirs(self.persistent_dir, exist_ok=True)
            try:
                self._context = self._pw.chromium.launch_persistent_context(
                    self.persistent_dir, executable_path=self.chrome, **kw,
                    **ctx_kw)
            except Exception:
                self._context = self._pw.chromium.launch_persistent_context(
                    self.persistent_dir, **kw, **ctx_kw)
            self._browser = None
        else:
            try:
                self._browser = self._pw.chromium.launch(
                    executable_path=self.chrome, **kw)
            except Exception:
                # 退路：用 playwright 自带的 chromium（不指定 executable_path）
                self._browser = self._pw.chromium.launch(**kw)
            self._context = self._browser.new_context(**ctx_kw)

        self._context.set_default_timeout(30_000)

        # 注入外部 cookie（若有）
        if self.cookies_file:
            self._load_cookies(self.cookies_file)
        return self

    def _load_cookies(self, path: str):
        """支持两种格式：
           - {name: value}            → 需配合 domains 参数（见 import_login.py 输出带 domains 的格式）
           - [{"name","value","domain","path"}...]  → 标准 Playwright cookie 格式
        纯 {name:value} 无法确定 domain，此处按常见平台域名兜底。
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] cookie 文件读取失败: {e}")
            return
        cookies = []
        if isinstance(raw, list):
            cookies = raw
        elif isinstance(raw, dict):
            domains = raw.get("domains") or []
            vals = raw.get("cookies") or {k: v for k, v in raw.items()
                                          if k != "domains"}
            if not domains:
                print("[warn] cookie 为 name:value 形式但缺 domains，无法注入")
                return
            for d in domains:
                for name, value in vals.items():
                    cookies.append({"name": name, "value": str(value),
                                    "domain": d, "path": "/"})
        if cookies:
            try:
                self._context.add_cookies(cookies)
                print(f"[info] 已注入 {len(cookies)} 个 cookie")
            except Exception as e:
                print(f"[warn] cookie 注入失败: {e}")

    def new_page(self):
        return self._context.new_page()

    @property
    def context(self):
        return self._context

    def __exit__(self, *exc):
        for obj, close in ((self._context, "close"), (self._browser, "close"),
                           (self._pw, "stop")):
            if obj is None:
                continue
            try:
                getattr(obj, close)()
            except Exception:
                pass


def fetch_json_in_page(page, url: str, timeout: int = 25, retries: int = 2,
                       referer: str = "") -> Any:
    """在浏览器上下文里 fetch 一个 JSON 接口（自动带 cookie/UA，绕过 requests 限制）。

    返回解析后的 dict/list；失败返回 None（绝不返回伪造结构）。
    """
    for attempt in range(retries + 1):
        try:
            res = page.evaluate(
                """async ([u, ref]) => {
                    const h = {};
                    if (ref) h['Referer'] = ref;
                    const r = await fetch(u, {headers: h, credentials: 'include'});
                    const t = await r.text();
                    return {status: r.status, body: t};
                }""",
                [url, referer],
            )
            if not res:
                continue
            if res.get("status") != 200:
                time.sleep(1.2 * (attempt + 1))
                continue
            body = (res.get("body") or "").strip()
            if not body:
                continue
            return json.loads(body)
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return None


def sleep_jitter(base: float = 1.0, spread: float = 0.8) -> None:
    """站间/页间限速。不要为快而加并发，会触发风控。"""
    import random
    time.sleep(base + random.random() * spread)


# ------------------------------------------------------------------ 检索器注册表

REGISTRY: dict[str, Any] = {}   # platform -> 检索函数（由 searchers 注册）


def register(platform: str):
    """装饰器：把某个平台的检索函数登记进 REGISTRY。

    searchers.py 里用 @register("bilibili") 挂在各函数上；
    collect_topics.py 只需 `import searchers` 即可完成注册。
    """
    def deco(fn):
        REGISTRY[platform] = fn
        return fn
    return deco


def search_platform(platform: str, keyword: str, limit: int = 5,
                    browser: "Browser | None" = None,
                    min_likes: int = 0) -> PlatformResult:
    """统一入口：调度到具体平台的检索函数。

    返回 PlatformResult —— 无论成功失败都返回（失败时 status != ok 且 reason 有值）。
    """
    spec = PLATFORM_SPEC.get(platform)
    if not spec:
        return PlatformResult(platform, platform, status="error",
                              reason=f"未知平台 {platform}")
    if spec["access"] == "blocked":
        return PlatformResult(platform, spec["name"], access=spec["access"],
                              status="blocked",
                              reason=spec["note"])
    fn = REGISTRY.get(platform)
    if fn is None:
        return PlatformResult(platform, spec["name"], access=spec["access"],
                              status="error",
                              reason=f"平台 {platform} 无检索实现（需先 import "
                                     f"searchers 完成注册；改版后需补实现）")
    try:
        return fn(keyword=keyword, limit=limit, browser=browser,
                  min_likes=min_likes)
    except Exception as e:  # noqa: BLE001 — 采集失败必须如实上报，不能崩掉整轮
        return PlatformResult(platform, spec["name"], access=spec["access"],
                              status="error",
                              reason=f"{type(e).__name__}: {e}")
