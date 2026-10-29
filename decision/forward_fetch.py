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
        return list(_CACHE[res_id])
    # NapCat 上实测认 {`message_id`: res_id}；标准 OneBot 写的是 {"id": …}，两个都试。
    fwd = await _call(bot, "get_forward_msg", {"message_id": res_id}, timeout)
    if not isinstance(fwd, dict):
        fwd = await _call(bot, "get_forward_msg", {"id": res_id}, timeout)
    if not isinstance(fwd, dict):
        return []
    lines = items_of(fwd.get("messages") or fwd.get("message") or [])
    _remember(res_id, lines)
    return lines
