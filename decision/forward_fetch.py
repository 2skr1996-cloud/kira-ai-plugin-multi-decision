# -*- coding: utf-8 -*-
"""空壳转发的补救：框架只给了「这里有一条合并转发」，没把里面的记录取回来。

根因在框架侧（core/adapter/src/qq/im.py 处理 forward 段的那几行）：它把**外层消息
自己的 message_id** 当成转发的 res_id 传给 get_forward_msg，NapCat 自然取不到东西，
于是构造出 Forward(chains=[])，格式化时退化成 "[Forward message]"。真正的 res_id
在 OneBot 段的 `data.id` 里（形如 jWdbJWOdruke9T6bZWztTd+8Q0SsvozCPjdipGsF2AuUcQCeCFCV95xqi869ITZ8）。

框架目录本机不可写（D: 是只读网络盘），只能在插件侧补一次：
  send_action("get_msg", {"message_id": 外层 id})  → data.message[].data.id
  send_action("get_forward_msg", {"id": res_id})  → data.messages[]
把每条记录拼成一行 `[时间] 昵称: 正文`，塞回元素的 chains，
engine._forward_text 就会照框架的格式吐出完整的 "[Forward …]"，前端照常出卡。
"""

from __future__ import annotations

import asyncio
import re
import time

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

# 同一条转发常常被读两次（进待发一次、结算时再一次），按 res_id 记一份就行。
_CACHE: dict = {}
_CACHE_ORDER: list = []
_CACHE_MAX = 64
# 框架历史里只留得下 "[Forward]" 这个空壳，正文由插件按 message_id 补回来；
# 下一轮 collect 把框架历史并进窗口时要用它换掉那枚空壳（key 是外层 message_id）。
_MID_LINES: dict = {}
_MID_ORDER: list = []
_MID_MAX = 128

_CQ_FWD_RE = re.compile(r"\[CQ:forward,[^\]]*?id=([^,\]]+)")
_LINE_ID_RE = re.compile(r"\[message_id:\s*(-?\d+)\]")


def _seg_text(seg) -> str:
    """把一条 OneBot 消息段压成一行文字。"""
    if not isinstance(seg, dict):
        return ""
    kind = str(seg.get("type") or "")
    data = seg.get("data")
    if not isinstance(data, dict):
        data = {}
    if kind == "text":
        return str(data.get("text") or "")
    if kind == "image":
        return "[图片]"
    if kind in ("face", "mface", "marketface"):
        return "[表情]"
    if kind == "at":
        nm = str(data.get("name") or data.get("qq") or "").strip()
        return "@" + nm if nm else "@"
    if kind == "record":
        return "[语音]"
    if kind == "video":
        return "[视频]"
    if kind == "file":
        nm = str(data.get("file") or data.get("name") or "").strip()
        return "[文件 %s]" % nm if nm else "[文件]"
    if kind == "forward":
        return "[合并转发]"
    if kind == "json":
        return "[卡片]"
    if kind == "xml":
        return "[卡片]"
    if kind == "reply":
        return ""
    return ""


def _when_of(value) -> str:
    try:
        stamp = float(value)
    except Exception:
        return str(value or "").strip()
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except Exception:
        return ""


def _line_of(msg) -> str:
    """一条转发记录 → `[时间] 昵称: 正文`。时间/昵称要自己拼：engine 的
    _fw_time/_fw_name 走 getattr，对 dict 取不到值。"""
    if not isinstance(msg, dict):
        return ""
    segs = msg.get("message")
    if isinstance(segs, list):
        body = "".join(_seg_text(s) for s in segs)
    elif isinstance(segs, str):
        body = segs
    else:
        body = str(msg.get("raw_message") or "")
        body = _CQ_FWD_RE.sub("[合并转发]", body)
        body = re.sub(r"\[CQ:[^\]]+\]", "", body)
    body = body.replace("\r", " ").strip()
    sender = msg.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    nick = str(sender.get("card") or sender.get("nickname") or "").strip()
    if not nick:
        nick = str(msg.get("user_id") or "").strip()
    when = _when_of(msg.get("time"))
    head = ""
    if when:
        head += "[%s]" % when
    if nick:
        head += (" " if head else "") + nick + ":"
    return (head + (" " if (head and body) else "") + body).strip()


#: 转发里面的图（v4.47，用户 m24522 第 3 项）：框架只把转发压成文字，图一律变成
#: 「[图片]」四个字，卡片上就只剩占位。这里在补内容的时候顺手把图也存进插件缓存，
#: 前端按占位顺序认领（web 的 forwardHtml 早就会分图，只差一份 media）。
_PLUGIN_ID = "kira-ai-plugin-multi-decision"
_IMG_MAX = 8 * 1024 * 1024
_MID_MEDIA: dict = {}
_MID_MEDIA_ORDER: list = []
_MID_MEDIA_MAX = 64
# 进程重启就丢，这一份落盘（用户 m25038 第 4 项：老消息/老快照里的转发图还要能认回来）
_MEDIA_DB: dict = {}
_MEDIA_DB_LOADED = False
# 读文件失败时不再把内存清空、也不算「已加载」：下次再读一次，别让一次坏文件
# 把后面保存的映射盖掉。真要在坏文件之上写时，先把它原样另存一份。
_MEDIA_DB_LOAD_FAILED = False
_MEDIA_DB_BACKED_UP = False
_MEDIA_DB_MAX = 300


def _media_cache_dir():
    try:
        from core.utils.path_utils import get_data_path
    except Exception:
        return None
    from pathlib import Path
    try:
        return Path(str(get_data_path())) / "plugin_data" / _PLUGIN_ID / "media_cache"
    except Exception:
        return None


def _media_db_path():
    folder = _media_cache_dir()
    if folder is None:
        return None
    try:
        return folder.parent / "forward_media.json"
    except Exception:
        return None


def _media_db() -> dict:
    global _MEDIA_DB, _MEDIA_DB_LOADED, _MEDIA_DB_LOAD_FAILED
    if _MEDIA_DB_LOADED:
        return _MEDIA_DB
    path = _media_db_path()
    if path is None or not path.is_file():
        _MEDIA_DB_LOADED = True
        _MEDIA_DB_LOAD_FAILED = False
        return _MEDIA_DB
    try:
        import json
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # 坏文件：保留内存里已有的映射，也不标「已加载」，下次调用再试一次。
        _MEDIA_DB_LOAD_FAILED = True
        logger.info("[multi-decision] 转发配图索引读不出来 %s: %s",
                    type(exc).__name__, exc)
        return _MEDIA_DB
    if not isinstance(blob, dict):
        # 解析成对象才算「读到了」；list/null/数字是坏内容，同样按失败处理。
        _MEDIA_DB_LOAD_FAILED = True
        logger.info("[multi-decision] 转发配图索引不是对象，按读取失败处理")
        return _MEDIA_DB
    _MEDIA_DB = {str(k): [x for x in v if isinstance(x, dict)]
                 for k, v in blob.items() if isinstance(v, list)}
    _MEDIA_DB_LOADED = True
    _MEDIA_DB_LOAD_FAILED = False
    return _MEDIA_DB


def _media_db_backup_broken(path) -> bool:
    """坏文件第一次被覆盖前，原样另存一份；存不下就不写，别把旧映射盖掉。"""
    global _MEDIA_DB_BACKED_UP
    if _MEDIA_DB_BACKED_UP:
        return True
    import os
    import uuid
    bak = path.with_name(path.name + ".bad-" + uuid.uuid4().hex[:8])
    try:
        with open(bak, "xb") as fp:
            fp.write(path.read_bytes())
    except FileExistsError:
        _MEDIA_DB_BACKED_UP = True
        return True
    except Exception as exc:
        logger.info("[multi-decision] 坏索引另存失败，先不覆盖 %s: %s",
                    type(exc).__name__, exc)
        return False
    _MEDIA_DB_BACKED_UP = True
    return True


def _media_db_save() -> None:
    global _MEDIA_DB_LOAD_FAILED, _MEDIA_DB_BACKED_UP
    path = _media_db_path()
    if path is None:
        return
    import json
    import os
    import uuid
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        while len(_MEDIA_DB) > _MEDIA_DB_MAX:
            _MEDIA_DB.pop(next(iter(_MEDIA_DB)))
        if _MEDIA_DB_LOAD_FAILED and path.is_file():
            if not _media_db_backup_broken(path):
                return
        # 同目录唯一临时文件 + 替换：写一半断掉不会留下半份索引。
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(_MEDIA_DB, ensure_ascii=False))
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        tmp = None
        _MEDIA_DB_LOAD_FAILED = False
    except Exception as exc:
        logger.info("[multi-decision] 转发配图索引没写成 %s: %s", type(exc).__name__, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _sniff_image_ext(blob: bytes) -> str:
    """按文件头认图片格式，认不出返回 ""（M09）。

    以前按 Content-Type 猜后缀：对方随便回个 image/jpeg 就能把网页存成 .jpg，
    真实 PNG 配错头又会存成 .jpg，前端按 mime 画图就画不出来。文件头是图片
    自己带的，不认服务器怎么说。
    """
    if not blob:
        return ""
    for magic, ext in ((b"\x89PNG\r\n\x1a\n", ".png"),
                       (b"\xff\xd8\xff", ".jpg"),
                       (b"GIF87a", ".gif"),
                       (b"GIF89a", ".gif")):
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return ""


def _mime_of(ext: str) -> str:
    return {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")


def _save_image(url: str) -> dict:
    """把转发里的一张图存进插件缓存，返回 {path, mime}；取不到就返回 {}。"""
    u = str(url or "").strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return {}
    folder = _media_cache_dir()
    if folder is None:
        return {}
    import hashlib
    import urllib.request as _ur

    key = "fw_" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
    for ext in (".jpg", ".png", ".gif", ".webp"):
        hit = folder / (key + ext)
        if hit.is_file():
            return {"path": "plugin_data/%s/media_cache/%s%s" % (_PLUGIN_ID, key, ext),
                    "mime": _mime_of(ext)}
    try:
        req = _ur.Request(u, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": "https://q.qq.com/",
        })
        with _ur.urlopen(req, timeout=6) as resp:
            blob = resp.read(_IMG_MAX + 1)
            ctype = str(resp.headers.get("Content-Type") or "").lower()
        if not blob or len(blob) > _IMG_MAX:
            return {}
        ext = _sniff_image_ext(blob)
        if not ext:
            logger.info("[multi-decision] 转发配图不是图片（按文件头判断）%s", u[:80])
            return {}
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (key + ext)).write_bytes(blob)
        return {"path": "plugin_data/%s/media_cache/%s%s" % (_PLUGIN_ID, key, ext),
                "mime": _mime_of(ext)}
    except Exception as exc:
        logger.info("[multi-decision] 转发配图没取到 %s：%s: %s", u[:80], type(exc).__name__, exc)
        return {}


def media_of(messages) -> list:
    """按转发条目的先后把图存下来，顺序与文本里的 [图片] 占位一一对应。"""
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        segs = msg.get("message")
        if not isinstance(segs, list):
            continue
        for seg in segs:
            if not isinstance(seg, dict) or str(seg.get("type") or "") != "image":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                data = {}
            got = _save_image(str(data.get("url") or data.get("file") or ""))
            if got:
                out.append(got)
    return out


def remember_media_by_mid(mid: str, refs: list) -> None:
    key = str(mid or "").strip()
    if not key or not refs:
        return
    _MID_MEDIA[key] = list(refs)
    _MID_MEDIA_ORDER.append(key)
    while len(_MID_MEDIA_ORDER) > _MID_MEDIA_MAX:
        old = _MID_MEDIA_ORDER.pop(0)
        if old != key:
            _MID_MEDIA.pop(old, None)
    db = _media_db()
    db[key] = list(refs)
    _media_db_save()


def media_for_mid(mid: str) -> list:
    key = str(mid or "").strip()
    got = _MID_MEDIA.get(key)
    if got:
        return list(got)
    return list(_media_db().get(key) or [])


def media_for_text(text: str) -> list:
    """一条历史消息正文里的转发配图：按 [message_id: X] 反查。"""
    t = str(text or "")
    if "[Forward" not in t:
        return []
    hit = _LINE_ID_RE.search(t)
    return media_for_mid(hit.group(1)) if hit else []


def _remember(res_id: str, lines: list) -> None:
    if not res_id or not lines:
        return
    _CACHE[res_id] = list(lines)
    _CACHE_ORDER.append(res_id)
    while len(_CACHE_ORDER) > _CACHE_MAX:
        old = _CACHE_ORDER.pop(0)
        if old != res_id:
            _CACHE.pop(old, None)


def remember_by_mid(mid: str, lines: list) -> None:
    """按外层 message_id 记一份：框架历史里那行空壳要靠它换掉。"""
    key = str(mid or "").strip()
    if not key or not lines:
        return
    _MID_LINES[key] = list(lines)
    _MID_ORDER.append(key)
    while len(_MID_ORDER) > _MID_MAX:
        old = _MID_ORDER.pop(0)
        if old != key:
            _MID_LINES.pop(old, None)


def lines_for_mid(mid: str) -> list:
    return list(_MID_LINES.get(str(mid or "").strip()) or [])


def block_of(lines) -> str:
    """和框架 message_formatter 一致：条目之间空一行。"""
    return "[Forward " + "\n\n".join(str(x) for x in lines) + "]"


def patch_text(text: str) -> str:
    """把一段历史文本里的空壳转发换成真内容。

    只认「同一行既有 [message_id: X] 又有 [Forward]/[Forward message]」的行——
    框架的 user 内容是「[时间] [message_id: X] [user_nickname: …] | 正文」，
    转发正文就是那个空壳。没补到过内容的消息原样留着，不编。
    """
    if not text or "[Forward" not in text:
        return text
    out = []
    changed = False
    for line in text.split("\n"):
        if "[Forward" in line:
            hit = _LINE_ID_RE.search(line)
            lines = lines_for_mid(hit.group(1)) if hit else []
            if lines:
                block = block_of(lines)
                if "[Forward message]" in line:
                    line = line.replace("[Forward message]", block)
                elif "[Forward]" in line:
                    line = line.replace("[Forward]", block)
                changed = True
        out.append(line)
    return "\n".join(out) if changed else text


async def _call(bot, action: str, params: dict, timeout: float):
    try:
        res = await asyncio.wait_for(bot.send_action(action, params, timeout=timeout), timeout=timeout)
    except Exception as exc:
        logger.info("[multi-decision] 转发补取 %s 失败：%s: %s" % (action, type(exc).__name__, exc))
        return None
    if not isinstance(res, dict):
        return None
    if str(res.get("status") or "") == "failed":
        return None
    try:
        if int(res.get("retcode") or 0) != 0:
            return None
    except Exception:
        pass
    return res.get("data")


def _res_id_of(data) -> str:
    """从 get_msg 的返回里抠出转发 res_id：优先进段里的 data.id。"""
    if not isinstance(data, dict):
        return ""
    segs = data.get("message")
    if isinstance(segs, list):
        for seg in segs:
            if not isinstance(seg, dict) or str(seg.get("type") or "") != "forward":
                continue
            box = seg.get("data")
            if not isinstance(box, dict):
                box = {}
            rid = str(box.get("id") or box.get("res_id") or "").strip()
            if rid:
                return rid
    hit = _CQ_FWD_RE.search(str(data.get("raw_message") or ""))
    return hit.group(1) if hit else ""


def items_of(messages) -> list:
    """data.messages[] → [{"message_str": "[时间] 昵称: 正文"}, …]（engine 认 dict）。"""
    out = []
    for msg in messages or []:
        line = _line_of(msg)
        if line:
            out.append({"message_str": line})
    return out


async def _forward_raw(bot, res_id: str, timeout: float):
    """取转发的原始条目（图就在这里面）。NapCat 认 {`message_id`: res_id}，
    标准 OneBot 写的是 {"id": …}，两个都试。"""
    fwd = await _call(bot, "get_forward_msg", {"message_id": res_id}, timeout)
    if not isinstance(fwd, dict):
        fwd = await _call(bot, "get_forward_msg", {"id": res_id}, timeout)
    if not isinstance(fwd, dict):
        return None
    return fwd.get("messages") or fwd.get("message") or []


async def fetch_forward_items(bot, message_id: str, timeout: float = 5.0) -> list:
    """按外层消息 id 反查转发内容；拿不到就返回 []（调用方保持原样）。"""
    mid = str(message_id or "").strip()
    if not mid or bot is None:
        return []
    data = await _call(bot, "get_msg", {"message_id": mid}, timeout)
    if not isinstance(data, dict) and mid.lstrip("-").isdigit():
        # 少数实现只认 int
        data = await _call(bot, "get_msg", {"message_id": int(mid)}, timeout)
    res_id = _res_id_of(data)
    if not res_id:
        return []
    if res_id in _CACHE:
        # 文字命中了缓存，图却可能还没存过（早先只有取文字的那版代码）——补存一次。
        if not media_for_mid(mid):
            raw_again = await _forward_raw(bot, res_id, timeout)
            try:
                refs_again = await asyncio.to_thread(media_of, raw_again or [])
            except Exception:
                refs_again = []
            if refs_again:
                remember_media_by_mid(mid, refs_again)
        return list(_CACHE[res_id])
    raw_msgs = await _forward_raw(bot, res_id, timeout)
    if raw_msgs is None:
        return []
    lines = items_of(raw_msgs)
    _remember(res_id, lines)
    try:
        refs = await asyncio.to_thread(media_of, raw_msgs)
    except Exception:
        refs = []
    if refs:
        remember_media_by_mid(mid, refs)
    return lines
