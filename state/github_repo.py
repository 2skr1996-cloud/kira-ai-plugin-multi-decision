"""GitHub 链接卡片的数据（用户 m18492）。

群里丢一个 github.com 的裸链接，光一行网址看不出是啥：是不是仓库、哪个 issue、
多少 star、主语言是什么都不知道。这里向后端要一次仓库信息——卡片先画骨架，
数据到了再填，跟推文卡一套做法。

只认 github.com / gist.github.com，别的地址一律不认，这个接口不会被当成任意地址的代理。
仓库根走 /repos/{o}/{r}，issue / pull 走 /repos/{o}/{r}/issues/{n}；
blob / tree / releases / commit / 用户主页不发请求，直接拿路径当标题——它们本来也
没什么可展示的，省一次 GitHub 的限流额度。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_GH_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/?#\s]+)/([^/?#\s]+)([^?#\s]*)", re.I)
_GH_USER_RE = re.compile(r"^https?://(?:www\.)?github\.com/([^/?#\s]+)/?$", re.I)
_GIST_RE = re.compile(r"^https?://gist\.github\.com/(?:[^/?#\s]+/)?([0-9a-fA-F]{5,})", re.I)

_ALLOWED_HOSTS = ("github.com", "gist.github.com")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_API = "https://api.github.com"

# 主语言色点只覆盖常见的，没列到的给中性灰，卡片不会因此变形
_LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#f1e05a", "TypeScript": "#3178c6",
    "Rust": "#dea584", "Go": "#00ADD8", "C": "#555555", "C++": "#f34b7d",
    "C#": "#178600", "Java": "#b07219", "Kotlin": "#A97BFF", "Swift": "#F05138",
    "Ruby": "#701516", "PHP": "#4F5D95", "Shell": "#89e051", "Lua": "#000080",
    "Vue": "#41b883", "HTML": "#e34c26", "CSS": "#563d7c", "Zig": "#ec915c",
    "Dart": "#00B4AB", "Elixir": "#6e4a7e", "Haskell": "#5e5086", "Nim": "#ffc200",
    "GDScript": "#355570", "Jupyter Notebook": "#DA5B0B", "Markdown": "#083fa1",
}
_LANG_FALLBACK = "#8b949e"

_MD_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s*)")
_SPACE_RE = re.compile(r"\s+")


def _one_line(raw, limit: int = 140) -> str:
    """把仓库描述 / issue 正文收拾成卡片上能放的一行。"""
    text = str(raw or "").replace("\r", "\n").split("\n")[0]
    text = _MD_PREFIX_RE.sub("", text)
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _lang_color(name) -> str:
    return _LANG_COLORS.get(str(name or ""), _LANG_FALLBACK)


class GitHubRepos:
    """按 URL 缓存 GitHub 链接的展示数据；只认 github.com 与 gist.github.com。"""

    def __init__(self, data_dir, filename: str = "github_repos.json", *,
                 ttl_days: float = 7, fail_ttl: float = 900,
                 limit: int = 500, timeout: float = 6.0,
                 max_bytes: int = 400000, fetcher=None):
        self.dir = Path(str(data_dir or "."))
        self.path = self.dir / filename
        self.ttl = max(600.0, float(ttl_days) * 86400.0)
        self.fail_ttl = max(60.0, float(fail_ttl))
        self.limit = max(20, int(limit))
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
                for key, ent in got.items():
                    if isinstance(ent, dict) and isinstance(ent.get("data"), dict):
                        items[str(key)] = {"t": float(ent.get("t") or 0),
                                           "fail": bool(ent.get("fail")),
                                           "data": ent["data"]}
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.debug("[multi-decision] 读 GitHub 卡片缓存失败：%s: %s", type(exc).__name__, exc)
        self._items = items
        return items

    def _save(self, items: dict) -> None:
        now = time.time()
        alive = {k: e for k, e in items.items()
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
            logger.debug("[multi-decision] 写 GitHub 卡片缓存失败：%s: %s", type(exc).__name__, exc)
        self._items = alive

    def _entry(self, key: str):
        ent = self._load().get(str(key or ""))
        if not ent:
            return None
        age = time.time() - float(ent.get("t") or 0)
        ttl = self.fail_ttl if ent.get("fail") else self.ttl
        if age > ttl:
            return None
        return ent

    def cached(self, url: str) -> dict:
        """缓存里已有的卡片数据；没有或已过期返回空 dict。"""
        ent = self._entry(self._key(url))
        if not ent or ent.get("fail"):
            return {}
        return dict(ent.get("data") or {})

    # ---------- 解析 ----------
    def allowed(self, url: str) -> bool:
        from urllib.parse import urlparse
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

    def _key(self, url: str) -> str:
        """缓存键：去掉协议差异、结尾斜杠与大小写，同一个仓库只抓一次。"""
        u = str(url or "").strip()
        u = re.sub(r"^https?://(?:www\.)?", "https://", u, flags=re.I)
        return u.rstrip("/").lower()

    def _local(self, url: str):
        """不需要发请求就能画完的链接（路径 / 提交 / 用户主页）；认不出返回 None。"""
        m = _GH_USER_RE.match(url)
        if m and not _GH_REPO_RE.match(url):
            owner = m.group(1)
            return {"kind": "user", "owner": owner, "repo": "", "repo_full": owner,
                    "title": owner, "desc": "GitHub 用户",
                    "url": "https://github.com/" + owner}
        m = _GH_REPO_RE.match(url)
        if not m:
            return None
        owner, repo, rest = m.group(1), m.group(2), (m.group(3) or "")
        if repo.endswith(".git"):
            repo = repo[:-4]
        segs = [s for s in rest.split("/") if s]
        if not segs:
            return None                      # 仓库根要发请求
        head = segs[0].lower()
        if head in ("issues", "pull") and len(segs) > 1 and segs[1].isdigit():
            return None                      # issue / pull 要发请求
        base = "https://github.com/%s/%s" % (owner, repo)
        if head in ("blob", "tree") and len(segs) > 1:
            return {"kind": "path", "owner": owner, "repo": repo,
                    "repo_full": owner + "/" + repo, "title": segs[-1],
                    "desc": "/".join(segs[1:-1]) or repo,
                    "url": base + rest}
        if head == "releases":
            return {"kind": "releases", "owner": owner, "repo": repo,
                    "repo_full": owner + "/" + repo, "title": "Releases",
                    "desc": repo, "url": base + "/releases"}
        if head == "commit" and len(segs) > 1:
            return {"kind": "commit", "owner": owner, "repo": repo,
                    "repo_full": owner + "/" + repo, "title": segs[1][:7],
                    "desc": repo, "url": base + rest}
        return {"kind": "path", "owner": owner, "repo": repo,
                "repo_full": owner + "/" + repo, "title": "/".join(segs),
                "desc": repo, "url": base + rest}

    # ---------- 对外 ----------
    def info_of(self, url: str) -> dict:
        """抓（或读缓存）一个 GitHub 链接的卡片数据。永远返回 dict，不抛异常。"""
        url = str(url or "").strip()
        if not self.allowed(url):
            return {"ok": False, "detail": "只支持 GitHub 链接"}
        local = self._local(url)
        if local is not None:
            local["ok"] = True
            local["cached"] = True
            return local
        key = self._key(url)
        # 失败也进短缓存：不然页面每次重绘都会把同一个死链再打一遍
        hit = self._entry(key)
        if hit is not None:
            if hit.get("fail"):
                return {"ok": False, "detail": "上次没取到"}
            data = dict(hit.get("data") or {})
            if data:
                data["ok"] = True
                data["cached"] = True
                return data
        try:
            data = self._fetch(url)
        except Exception as exc:
            data = {}
            logger.debug("[multi-decision] 取 GitHub 信息失败 %s：%s: %s", url, type(exc).__name__, exc)
        with self._lock:
            items = dict(self._load())
            items[key] = {"t": time.time(), "fail": not bool(data), "data": data or {}}
            self._save(items)
        if not data:
            return {"ok": False, "detail": "没有取到仓库信息"}
        out = dict(data)
        out["ok"] = True
        out["cached"] = False
        return out

    # ---------- 抓取 ----------
    def _get_json(self, api: str) -> dict:
        req = Request(api, headers={
            "User-Agent": _UA,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read(self.max_bytes)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _fetch(self, url: str) -> dict:
        if self._fetcher is not None:
            return self._fetcher(url) or {}
        m = _GIST_RE.match(url)
        if m:
            data = self._get_json(_API + "/gists/" + m.group(1))
            if not data:
                return {}
            files = data.get("files") or {}
            gid = str(data.get("id") or m.group(1))
            return {"kind": "gist", "repo_full": "", "title": _one_line(data.get("description")) or gid[:8],
                    "desc": "%d 个文件" % len(files) if files else "",
                    "url": str(data.get("html_url") or url)}
        m = _GH_REPO_RE.match(url)
        if not m:
            return {}
        owner, repo, rest = m.group(1), m.group(2), (m.group(3) or "")
        if repo.endswith(".git"):
            repo = repo[:-4]
        segs = [s for s in rest.split("/") if s]
        base = "https://github.com/%s/%s" % (owner, repo)
        if segs and segs[0].lower() in ("issues", "pull") and len(segs) > 1 and segs[1].isdigit():
            data = self._get_json(_API + "/repos/%s/%s/issues/%s" % (owner, repo, segs[1]))
            if not data or not data.get("title"):
                return {}
            is_pr = bool(data.get("pull_request"))
            state = str(data.get("state") or "")
            if is_pr and (data.get("pull_request") or {}).get("merged_at"):
                state = "merged"
            return {"kind": "pull" if is_pr else "issue",
                    "owner": owner, "repo": repo, "repo_full": owner + "/" + repo,
                    "no": int(segs[1]), "state": state,
                    "title": _one_line(data.get("title"), 120),
                    "desc": _one_line(data.get("body")),
                    "comments": int(data.get("comments") or 0),
                    "url": base + ("/pull/" if is_pr else "/issues/") + segs[1]}
        data = self._get_json(_API + "/repos/%s/%s" % (owner, repo))
        if not data or not data.get("full_name"):
            return {}
        topics = [str(t) for t in (data.get("topics") or []) if str(t)][:6]
        return {"kind": "repo", "owner": owner, "repo": repo,
                "repo_full": str(data.get("full_name") or (owner + "/" + repo)),
                "title": str(data.get("full_name") or (owner + "/" + repo)),
                "desc": _one_line(data.get("description")),
                "stars": int(data.get("stargazers_count") or 0),
                "forks": int(data.get("forks_count") or 0),
                "lang": str(data.get("language") or ""),
                "color": _lang_color(data.get("language")),
                "topics": topics,
                "archived": bool(data.get("archived")),
                "url": str(data.get("html_url") or base)}
