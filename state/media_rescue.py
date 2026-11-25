"""表情包/图片抢救：框架归档失败时自己把图下回来，救不回来就把方括号词换成占位。

背景（用户截图那条 19:27 的 QQ 表情包，message_id 1232530657）：
    框架 core/chat/message_history.py 归档媒体失败时，会把 chain 里的元素写成
    {"type": "image", "mime": "image/gif", "file_type": "unavailable"}——没有 file 键，
    图就彻底丢了；QQ 的商城表情（mface）还会额外带一段纯文本 "[不对不对]"（表情名），
    于是插件页面上只剩一行 "[不对不对]"。

这个模块做两件事：

1. 抢救（rescue_message / rescue_in_background）
   消息刚进来时，对「有 URL 但没落盘」的 image/sticker 元素自己下载一份，写进插件
   媒体缓存，并按 message_id 登记，让 window/manager.py 的 _media_by_ids 下次渲染时
   能反查到（它 misses 不缓存，所以晚一点登记也能被下一次渲染捡到）。

2. 兜底（apply_placeholder）
   这条消息带图、但正文整条只剩一个短方括号词（表情名）时，换成 [表情]/[图片]。
   只在「带图 + 正文除方括号词外没有别的字」时才换，正文里夹着的 [重点] 不动。

两条硬约束：
- 绝不阻塞消息流：下载走后台 task，调用方不 await；失败不抛，只留一条 INFO 日志
  （带状态码和去掉查询串的链接，用户 m18331 图2 之后才查得动）。
- 不新增依赖：只用 httpx（decision/engine.py 已经在用，requirements 里没加东西）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote_to_bytes, urlparse

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

# 一次抢救的总预算（秒）。任务要求 8 秒：超过就放弃，后台 task 不许拖长。
RESCUE_TIMEOUT = 8.0
# 单次 HTTP 尝试的上限；总预算不够时会被压小。
_ATTEMPT_TIMEOUT = 4.0
# 单张图上限，跟 window/manager.py 的 _MEDIA_COPY_LIMIT 对齐（32MB）。
MAX_BYTES = 32 * 1024 * 1024
# 后台 task 的强引用：asyncio 只持弱引用，不存一份会被 GC 掉。
_TASKS: set = set()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# QQ 图床的防盗链：gxh.vip.qq.com 实测对 UA/Referer 完全不敏感（4 组头返回一模一样，
# 见 tmp/_probe_qq_host.py），但 multimedia.nt.qq.com.cn / gchat.qpic.cn 这类会看
# Referer，所以按「最像浏览器」到「最裸」依次试，第一个成功就停。
_HEADER_PROFILES = (
    {
        "User-Agent": _UA,
        "Referer": "https://qq.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    },
    {"User-Agent": _UA, "Referer": "https://im.qq.com/", "Accept": "image/*,*/*;q=0.8"},
    {"User-Agent": _UA, "Referer": "https://qun.qq.com/", "Accept": "image/*,*/*;q=0.8"},
    {"User-Agent": _UA},
    {},
)

# 「整条只剩一个短方括号词」——QQ 商城表情（mface）的表情名就长这样，例如 [不对不对]。
# 上限 12 个字：再长就不像表情名了，宁可不动。
_BRACKET_WORD = re.compile(r"^\[[^\[\]\r\n]{1,12}\]$")
# 框架/插件自己产出的占位，已经是好的，不用再换。
_PLACEHOLDERS = frozenset(
    {"[图片]", "[表情]", "[视频]", "[语音]", "[文件]", "[Image]", "[Sticker]"}
)
# 正文前面可能带的媒体占位（框架 message_str 里 Image/Sticker 的 repr）。
_MEDIA_PREFIX = re.compile(r"^\[(?:Image|Sticker|图片|表情|视频|语音|文件)\]\s*")


class RescueResult:
    """一次抢救的结果。

    has_media  这条消息带 image/sticker 元素
    local      本来就有本地副本（不用联网）的张数
    rescued    抢救成功、已登记进缓存的相对路径
    failed     没救回来的张数
    """

    __slots__ = ("has_media", "local", "rescued", "failed")

    def __init__(self) -> None:
        self.has_media = False
        self.local = 0
        self.rescued: list = []
        self.failed = 0

    def __repr__(self) -> str:
        return "RescueResult(has_media=%r, local=%r, rescued=%r, failed=%r)" % (
            self.has_media,
            self.local,
            self.rescued,
            self.failed,
        )


# ---------------------------------------------------------------- 元素识别


def _chain_items(message) -> list:
    """message.chain 里的元素列表（拿不到就空列表）。"""
    chain = getattr(message, "chain", None)
    if chain is None:
        return []
    try:
        return list(chain)
    except TypeError:
        return []


def _media_kind_of(el) -> str:
    """这个元素是 image / sticker 还是别的。"""
    from core.chat.message_elements import Image, Sticker

    if isinstance(el, Sticker):
        return "sticker"
    if isinstance(el, Image):
        return "image"
    return ""


def _source_of(el) -> str:
    """元素的原始数据（URL / 路径 / base64 / data URL）。

    统一读 .file：Image.image 是构造时拷的一份普通属性，Sticker.sticker 是
    property，只有 .file 两边都靠得住。
    """
    return str(getattr(el, "file", "") or "")


def _needs_network(el) -> bool:
    """这个元素是不是「本地没有、必须联网才能拿到」。"""
    src = _source_of(el)
    ftype = str(getattr(el, "file_type", "") or "")
    if ftype == "path":
        try:
            return not (src and os.path.isfile(src))
        except OSError:
            return True
    if ftype in ("base64", "data_url"):
        return False
    if src.startswith("data:") or src.startswith("base64://"):
        return False
    if ftype == "url" or src.startswith(("http://", "https://")):
        return True
    return True


def _media_parts(message) -> list:
    """[(元素, "image"|"sticker", 是否要联网)]。"""
    out = []
    for el in _chain_items(message):
        kind = _media_kind_of(el)
        if not kind:
            continue
        out.append((el, kind, _needs_network(el)))
    return out


def _message_id(message) -> str:
    for attr in ("message_id", "msg_id", "id"):
        val = getattr(message, attr, None)
        if val is None or val == "":
            continue
        return str(val).strip()
    return ""


def _host(url: str) -> str:
    try:
        return urlparse(str(url or "")).hostname or ""
    except Exception:
        return ""


def _url_for_log(url: str) -> str:
    """日志里用的链接：只留 scheme://host/path，查询串（可能带 rkey）一律丢掉。"""
    try:
        parts = urlparse(str(url or ""))
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return ("%s://%s%s" % (parts.scheme, parts.netloc, parts.path))[:200]


def _url_variants(url: str) -> list:
    """同一个链接的几种写法：原样，以及 http/https 互换。

    QQ 图床有的路径只认 https（http 直接 403），有的老链接只认 http，
    两种都试一遍，代价只是一次请求。
    """
    text = str(url or "").strip()
    if not text:
        return []
    out = [text]
    if text.startswith("http://"):
        out.append("https://" + text[len("http://"):])
    elif text.startswith("https://"):
        out.append("http://" + text[len("https://"):])
    return out


# ---------------------------------------------------------------- 已有副本


def _already_available(mid: str, raw_sid: str = "") -> bool:
    """框架/插件是不是已经有一份了。有就别再下一次（每条图消息都会走这里）。

    顺序跟 window/manager.py 的 _media_by_ids 一致：插件缓存索引 → 框架落盘目录
    → 框架消息库。框架归档成功时前两步就命中，一次 sqlite 都不用开。
    """
    if not mid:
        return False
    try:
        from ..window import manager as wm
    except Exception:
        return False
    try:
        if wm._media_by_mid_index(mid):
            return True
    except Exception:
        pass
    try:
        root = wm._data_root()
        if root is not None:
            base = root / wm._MEDIA_ROOT
            dirs = wm._session_dirs(raw_sid, base)
            if wm._scan_mid_dirs(mid, dirs, base):
                return True
    except Exception:
        pass
    try:
        if wm._media_from_db(mid):
            return True
    except Exception:
        pass
    return False


def needs_rescue(message, raw_sid: str = "") -> bool:
    """这条消息有没有「要联网才拿得到、而且现在一张都没有」的 image/sticker。"""
    try:
        parts = _media_parts(message)
    except Exception:
        return False
    if not parts:
        return False
    if any(not need for _el, _kind, need in parts):
        # 已经有一张本地的了，不折腾
        return False
    return not _already_available(_message_id(message), raw_sid)


def _media_lost(message, raw_sid: str = "") -> bool:
    """这条消息的图是不是「确实没拿到」。apply_placeholder 只在 True 时才动正文。

    两个判据，满足一个就算丢：
    · 强判据：chain 里有 image/sticker 元素带 file_type == "unavailable"——框架
      core/chat/message_history.py:100-110 归档失败时就是这么写的，线上那条
      1232530657 正是这个形态。注意活消息上不会出现这个值：
      serialize_message_chain 只把 file_type 写进序列化后的 dict，不改活元素，
      所以这条判据实际作用于「从库里反序列化出来的 chain」。
    · 退一步：本地/宿主缓存里查不到这张消息的图（_already_available 为 False），
      且至少有一个元素是「必须联网才拿得到」的。

    两条都不满足就一个字都不改——正常归档成功的图片配文 [重点] 必须原样保留。
    """
    try:
        parts = _media_parts(message)
    except Exception:
        return False
    if not parts:
        return False
    for el, _kind, _need in parts:
        if str(getattr(el, "file_type", "") or "") == "unavailable":
            return True
    if not any(need for _el, _kind, need in parts):
        return False
    return not _already_available(_message_id(message), raw_sid)


# ---------------------------------------------------------------- 下载


def _decode_data_url(src: str, mime: str = ""):
    """data:image/gif;base64,xxxx → (bytes, mime)。"""
    text = str(src or "")
    if not text.startswith("data:"):
        return None, ""
    head, _sep, payload = text.partition(",")
    if not payload:
        return None, ""
    head_mime = head[5:].split(";")[0].strip().lower()
    try:
        if ";base64" in head.lower():
            data = base64.b64decode(payload, validate=False)
        else:
            data = unquote_to_bytes(payload)
    except Exception:
        return None, ""
    if not data:
        return None, ""
    return data, (mime or head_mime)


async def _fetch_bytes(url: str, budget: float):
    """按几组头依次试，返回 (bytes, mime)；失败返回 (None, "")，不抛。"""
    import httpx

    remaining = max(0.5, float(budget))
    last: Optional[BaseException] = None
    last_status = None
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for candidate in _url_variants(url):
            for headers in _HEADER_PROFILES:
                if remaining <= 0.5:
                    break
                started = time.monotonic()
                try:
                    async with client.stream(
                        "GET",
                        candidate,
                        headers=headers,
                        timeout=min(_ATTEMPT_TIMEOUT, remaining),
                    ) as resp:
                        resp.raise_for_status()
                        mime = (
                            (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                        )
                        chunks = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_BYTES:
                                return None, ""
                            chunks.append(chunk)
                        data = b"".join(chunks)
                    if not data:
                        return None, ""
                    return data, mime
                except Exception as exc:
                    last = exc
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status is not None:
                        last_status = status
                    # 401/403/404 换一组头可能就过了；429/5xx 是对方临时抽风，换头
                    # 未必有用但值得再试；超时和连接错误换什么都没用，直接放弃。
                    if status is None or not (
                        status in (401, 403, 404, 429) or status >= 500
                    ):
                        break
                finally:
                    remaining -= time.monotonic() - started
            if remaining <= 0.5:
                break
    if last is not None:
        # 用户 m18331 图2：以前只记域名和异常类名，事后完全没法定位是哪条链接、
        # 对方回了什么。现在把状态码和去掉查询串的链接记下来。
        logger.info(
            "[multi-decision] 表情包下载失败（%s）：%s %s",
            _host(url),
            ("HTTP %s" % last_status) if last_status is not None else type(last).__name__,
            _url_for_log(url),
        )
    return None, ""


async def _obtain(el, budget: float):
    """把一个元素变成 (bytes, mime)。本地已有的直接读，不联网。"""
    src = _source_of(el)
    ftype = str(getattr(el, "file_type", "") or "")
    mime = str(getattr(el, "mime", "") or "").split(";")[0].strip().lower()
    if ftype == "path" or (
        src
        and not src.startswith(("http://", "https://", "data:", "base64://"))
        and os.path.isfile(src)
    ):
        try:
            data = Path(src).read_bytes()
        except OSError:
            return None, ""
        return (data, mime) if data else (None, "")
    if ftype == "data_url" or src.startswith("data:"):
        return _decode_data_url(src, mime)
    if ftype == "base64" or src.startswith("base64://"):
        raw = src[len("base64://"):] if src.startswith("base64://") else src
        try:
            data = base64.b64decode(raw, validate=False)
        except Exception:
            return None, ""
        return (data, mime) if data else (None, "")
    if src.startswith(("http://", "https://")):
        return await _fetch_bytes(src, budget)
    return None, ""


def _suffix_for(el, mime: str) -> str:
    """给缓存文件挑个后缀：元素名 → URL 路径 → mime。挑不到就交给 _cache_media_file 嗅探。"""
    name = str(getattr(el, "name", "") or "")
    if name:
        suf = Path(name).suffix
        if suf and len(suf) <= 8:
            return suf
    src = _source_of(el)
    if src.startswith(("http://", "https://")):
        suf = Path(urlparse(src).path).suffix
        if suf and len(suf) <= 8:
            return suf
    if mime:
        suf = mimetypes.guess_extension(mime) or ""
        if suf and len(suf) <= 8:
            return suf
    return ""


def _register(data: bytes, suffix: str, mid: str, raw_sid: str) -> str:
    """把下载到的字节写进插件媒体缓存，并按 message_id 登记，返回相对 data 的路径。

    rel_key 用 session_media/<会话哈希>/<消息哈希>/<文件名>：window/manager.py 的
    _by_mid_index 拿 rel_key 的倒数第二段当 message_id 反查，正好对上。
    """
    from ..window import manager as wm

    mid_hash = hashlib.sha256(str(mid).encode("utf-8")).hexdigest()
    sid_hash = hashlib.sha256(str(raw_sid or "").encode("utf-8")).hexdigest()
    name = hashlib.sha1(data).hexdigest() + (suffix or ".img")
    rel_key = "%s/%s/%s/%s" % (wm._MEDIA_ROOT, sid_hash, mid_hash, name)
    tmp_dir = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="mdp-rescue-")
        tmp = Path(tmp_dir) / name
        tmp.write_bytes(data)
        return wm._cache_media_file(tmp, rel_key, raw_sid) or ""
    except Exception:
        logger.debug("[multi-decision] 表情包登记进缓存失败（已忽略）", exc_info=True)
        return ""
    finally:
        if tmp_dir:
            try:
                for p in Path(tmp_dir).iterdir():
                    p.unlink(missing_ok=True)
                Path(tmp_dir).rmdir()
            except OSError:
                pass


async def _rescue_one(el, kind: str, mid: str, raw_sid: str, budget: float) -> str:
    try:
        data, mime = await _obtain(el, budget)
    except Exception:
        return ""
    if not data:
        return ""
    return await asyncio.to_thread(_register, data, _suffix_for(el, mime), mid, raw_sid)


async def rescue_message(
    message, raw_sid: str = "", *, timeout: float = RESCUE_TIMEOUT
) -> RescueResult:
    """把这条消息里「有 URL 但没落盘」的 image/sticker 自己下回来并登记。

    失败静默（只留 debug 日志），绝不抛；总耗时不超过 timeout 秒。
    """
    result = RescueResult()
    try:
        parts = _media_parts(message)
    except Exception:
        return result
    result.has_media = bool(parts)
    result.local = sum(1 for _el, _kind, need in parts if not need)
    pending = [(el, kind) for el, kind, need in parts if need]
    if not pending:
        return result
    mid = _message_id(message)
    if not mid:
        result.failed = len(pending)
        return result
    budget = max(0.5, float(timeout))
    tasks = [
        asyncio.ensure_future(_rescue_one(el, kind, mid, raw_sid, budget))
        for el, kind in pending
    ]
    try:
        done = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), budget + 1.0
        )
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        result.failed = len(pending)
        return result
    for rel in done:
        if isinstance(rel, str) and rel:
            result.rescued.append(rel)
        else:
            result.failed += 1
    return result


# ---------------------------------------------------------------- 占位兜底


def _placeholder_for(parts) -> str:
    """表情包用 [表情]，普通图用 [图片]。"""
    for _el, kind, _need in parts:
        if kind == "sticker":
            return "[表情]"
    for el, _kind, _need in parts:
        mime = str(getattr(el, "mime", "") or "").lower()
        if "gif" in mime or "webp" in mime:
            return "[表情]"
    return "[图片]"


def _is_bare_bracket_word(text: str) -> bool:
    """整条正文是不是只剩一个短方括号词（剥掉前面的媒体占位之后）。"""
    word = str(text or "").strip()
    if not word:
        return False
    m = _MEDIA_PREFIX.match(word)
    if m:
        word = word[m.end():].strip()
    if not word or word in _PLACEHOLDERS:
        return False
    return bool(_BRACKET_WORD.match(word))


def _replace_bare_bracket_word(raw: str, placeholder: str) -> Optional[str]:
    """把一行里的纯方括号词换成占位；不是这个形状就返回 None（不动）。"""
    text = str(raw or "").strip()
    if not text:
        return None
    head, sep, tail = text.rpartition(" | ")
    if sep:
        body = tail.strip()
        prefix = ""
        m = _MEDIA_PREFIX.match(body)
        if m:
            prefix = m.group(0)
            body = body[m.end():].strip()
        if body and body not in _PLACEHOLDERS and _BRACKET_WORD.match(body):
            return head + sep + prefix + placeholder
        return None
    prefix = ""
    m = _MEDIA_PREFIX.match(text)
    if m:
        prefix = m.group(0)
        text = text[m.end():].strip()
    if text and text not in _PLACEHOLDERS and _BRACKET_WORD.match(text):
        return prefix + placeholder
    return None


def apply_placeholder(message, raw_sid: str = "") -> bool:
    """这条消息带图、图确实没救回来、正文整条只剩一个短方括号词时换成 [表情]/[图片]。

    两处都要改：
    - chain 里的 Text：extract_text 只读这里，模型看到的就是它；
    - message.message_str：raw_line_of 优先用它，不改这里页面还是显示原文。

    raw_sid 用来按 message_id 查这张图在本地/宿主缓存里到底有没有——查不到才算
    「丢」。正常归档成功的图片宿主会落到 session_media/archive/、消息库里是
    file_type "archive"，_already_available 查得到，于是原样保留，不会被误伤。
    """
    try:
        parts = _media_parts(message)
    except Exception:
        return False
    if not parts:
        return False
    if not _media_lost(message, raw_sid):
        return False
    placeholder = _placeholder_for(parts)
    changed = False
    try:
        from core.chat.message_elements import Text

        texts = [el for el in _chain_items(message) if isinstance(el, Text)]
        if texts:
            joined = "".join(str(getattr(t, "text", "") or "") for t in texts).strip()
            if _is_bare_bracket_word(joined):
                texts[0].text = placeholder
                for t in texts[1:]:
                    t.text = ""
                changed = True
    except Exception:
        logger.debug("[multi-decision] 表情名占位改写 chain 失败（已忽略）", exc_info=True)
    try:
        raw = str(getattr(message, "message_str", "") or "")
        if raw:
            new = _replace_bare_bracket_word(raw, placeholder)
            if new is not None and new != raw:
                message.message_str = new
                changed = True
    except Exception:
        logger.debug("[multi-decision] 表情名占位改写 message_str 失败（已忽略）", exc_info=True)
    return changed


# ---------------------------------------------------------------- 后台入口


async def _rescue_later(message, raw_sid: str, delay: float) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        mid = _message_id(message)
        # 框架归档可能比插件钩子晚一点落盘，等一小会儿再确认一次，别白下一遍。
        if _already_available(mid, raw_sid):
            return
        result = await rescue_message(message, raw_sid)
        if result.rescued:
            logger.info(
                "[multi-decision] 表情包/图片抢救成功 mid=%s，%d 张"
                % (mid, len(result.rescued))
            )
        elif result.failed:
            logger.debug(
                "[multi-decision] 表情包/图片抢救失败 mid=%s，%d 张（页面用占位）"
                % (mid, result.failed)
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("[multi-decision] 表情包/图片抢救异常（已忽略）", exc_info=True)


def rescue_in_background(message, raw_sid: str = "", *, delay: float = 0.4):
    """起一个后台 task 去抢救，立刻返回，绝不阻塞消息流。没有事件循环就返回 None。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = loop.create_task(_rescue_later(message, raw_sid, delay))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
