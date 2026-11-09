"""链接卡片的真实标题（v4.87，用户 m11237 第 4 条）。

对方只发一个裸链接时，QQ 的 ark 里没有标题，卡片上只能写「哔哩哔哩视频」这种
通用文案。这里按 URL 抓一次页面标题并落盘缓存：抓到就换成真标题；抓不到、超时、
站点不给标题都保持通用文案，绝不让页面卡住。

只放行 B 站域名（bilibili.com / b23.tv），这个接口不会被当成任意地址的代理。
"""
from __future__ import annotations

import gzip
import html as _html
import json
import logging
import os
import re
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_TAG_RE = re.compile(r"<meta[^>]+property=[\"']og:title[\"'][^>]*>", re.I)
_OG_CONTENT_RE = re.compile(r"content=[\"']([^\"']*)[\"']", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SITE_SUFFIX_RE = re.compile(r"\s*[_\-–—|]\s*(?:哔哩哔哩|bilibili).*$", re.I)
_SPACE_RE = re.compile(r"\s+")

_ALLOWED_HOSTS = ("bilibili.com", "b23.tv")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _decompress(raw: bytes) -> bytes:
    """B 站不管有没有要求都会回 gzip，这里兜一下；读不全也要尽量给出前面那截。"""
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except Exception:
            pass
        try:
            # 流被截断时 gzip.decompress 会整段放弃，decompressobj 会把能解的先给出来
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            return d.decompress(raw) or raw
        except Exception:
            return raw
    if raw[:1] == b"x" and raw[1:2] in (b"\x01", b"\x9c", b"\xda"):
        try:
            return zlib.decompress(raw)
        except Exception:
            try:
                return zlib.decompressobj().decompress(raw)
            except Exception:
                return raw
    return raw


def _clean_title(raw: str, limit: int = 120) -> str:
    """把页面标题收拾成卡片上能显示的一行。"""
    text = _TAG_RE.sub("", str(raw or ""))
    text = _html.unescape(text)
    text = _SPACE_RE.sub(" ", text).strip()
    text = _SITE_SUFFIX_RE.sub("", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


class LinkTitles:
    """按 URL 缓存网页标题；只认 B 站。"""

    def __init__(self, data_dir, filename: str = "link_titles.json", *,
                 ttl_days: float = 60, fail_ttl: float = 600,
                 limit: int = 2000, timeout: float = 6.0,
                 max_bytes: int = 300000, fetcher=None):
        self.dir = Path(str(data_dir or "."))
        self.path = self.dir / filename
        self.ttl = max(60.0, float(ttl_days) * 86400.0)
        self.fail_ttl = max(30.0, float(fail_ttl))
        self.limit = max(10, int(limit))
        self.timeout = max(1.0, float(timeout))
        self.max_bytes = max(4096, int(max_bytes))
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._items = None

    # ---------- 缓存 ----------
    def _load(self) -> dict:
        if self._items is not None:
            return self._items
        items = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            got = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(got, dict):
                for url, ent in got.items():
                    if isinstance(ent, dict) and isinstance(ent.get("title"), str):
                        items[str(url)] = {"t": float(ent.get("t") or 0), "title": ent["title"],
                                           "fail": bool(ent.get("fail"))}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.debug("[multi-decision] 读链接标题缓存失败：%s: %s", type(exc).__name__, exc)
        self._items = items
        return items

    def _save(self, items: dict) -> None:
        now = time.time()
        alive = {u: e for u, e in items.items()
                 if now - float(e.get("t") or 0) <= (self.fail_ttl if e.get("fail") else self.ttl)}
        if len(alive) > self.limit:
            keep = sorted(alive.items(), key=lambda kv: float(kv[1].get("t") or 0), reverse=True)[: self.limit]
            alive = dict(keep)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"v": 1, "items": alive}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            logger.debug("[multi-decision] 写链接标题缓存失败：%s: %s", type(exc).__name__, exc)
        self._items = alive

    def _entry(self, url: str):
        """缓存条目 -> (title, fail)；没有或已过期返回 None。"""
        ent = self._load().get(str(url or ""))
        if not ent:
            return None
        age = time.time() - float(ent.get("t") or 0)
        ttl = self.fail_ttl if ent.get("fail") else self.ttl
        if age > ttl:
            return None
        return (str(ent.get("title") or ""), bool(ent.get("fail")))

    def cached(self, url: str) -> str:
        """缓存里已有的标题；没有、已过期、或上次没抓到都返回空串。"""
        ent = self._entry(url)
        if not ent or ent[1]:
            return ""
        return ent[0]

    def allowed(self, url: str) -> bool:
        try:
            u = urlparse(str(url or ""))
        except Exception:
            return False
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower()
        if not host:
            return False
        return any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)

    def title_of(self, url: str) -> dict:
        """抓（或读缓存）一个链接的标题。永远返回 dict，不抛异常。"""
        url = str(url or "").strip()
        if not self.allowed(url):
            return {"ok": False, "detail": "只支持 B 站链接"}
        # 失败也进短缓存：不然页面每次重绘都会把同一个死链再打一遍
        hit = self._entry(url)
        if hit is not None:
            if hit[1]:
                return {"ok": False, "detail": "上次没抓到标题"}
            if hit[0]:
                return {"ok": True, "title": hit[0], "cached": True}
        try:
            title = self._fetch(url)
        except Exception as exc:
            title = ""
            logger.debug("[multi-decision] 抓链接标题失败 %s：%s: %s", url, type(exc).__name__, exc)
        with self._lock:
            items = dict(self._load())
            items[url] = {"t": time.time(), "title": title, "fail": not title}
            self._save(items)
        if not title:
            return {"ok": False, "detail": "没有取到标题"}
        return {"ok": True, "title": title, "cached": False}

    # ---------- 抓取 ----------
    def _fetch(self, url: str) -> str:
        if self._fetcher is not None:
            return _clean_title(self._fetcher(url))
        req = Request(url, headers={"User-Agent": _UA, "Referer": "https://www.bilibili.com/",
                                     "Accept-Encoding": "gzip, deflate"})
        with urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(self.max_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
        text = _decompress(raw).decode(charset, errors="replace")
        m = _OG_TAG_RE.search(text)
        if m:
            c = _OG_CONTENT_RE.search(m.group(0))
            if c:
                title = _clean_title(c.group(1))
                if title:
                    return title
        m = _TITLE_RE.search(text)
        return _clean_title(m.group(1)) if m else ""
