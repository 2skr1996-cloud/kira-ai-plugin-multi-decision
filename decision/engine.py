"""决策引擎：计数 → 判定 → 抖动合并 → 队列串行 → 结算。

设计见框架文档第 3 章。本模块管「要不要回」和「什么时候回」，
并在 ON_LLM_REQUEST 处把窗口快照交给窗口模块（第 4 章）。
"""

from __future__ import annotations

import asyncio
import copy
from collections import deque
import inspect
import json
import random
import re
import time
import uuid
from typing import Optional

from core.chat.message_elements import Emoji, File, Image, Record, Sticker, Text, Video
from core.logging_manager import get_logger

try:
    from core.prompt_manager import Prompt
except Exception:  # 测试桩 / 老版本框架没有这个模块
    Prompt = None

from .forward_fetch import fetch_forward_items, remember_by_mid
from ..state.session_state import SessionState
from ..state.sids import normalize_sid
from ..state.retry_history import RETRY_META
from ..window.buffer import ids_of
from ..window.split import _looks_image, mark_mention, mark_messages, split_user_content

# 「不触发短语」比对前先剥掉这些：句尾标点、空白、常见装饰符。
# 只比「整条消息 == 短语」，不做包含匹配——否则清单里加个「好」字，
# 所有带「好」的话都会被吞掉。
_PHRASE_STRIP = ' \t\r\n，。！？!?~～、,.;；:：…·•—-_*#@\'"“”‘’'


def _is_dm_sid(sid: str) -> bool:
    """这个 sid 是不是私聊（认 dm:1 / dm_1 / 桃华:dm:1 三种写法）。

    v3.70（用户 m21533 第 3 条）：曲线与动态调整不对私聊开放，判定前先看这里。
    """
    for part in str(sid or "").split(":"):
        p = part.strip().lower()
        if p == "dm" or p.startswith("dm_") or p.startswith("dm-"):
            return True
    return False


def _slot_hhmm(slot) -> str:
    """把 48 格里的槽位换成 HH:MM（v3.73，用户 m24640 第 5 条）。"""
    try:
        n = max(0, min(48, int(slot)))
    except (TypeError, ValueError):
        return ""
    return "%02d:%02d" % (n // 2, (n % 2) * 30)


def _band_target(raw, high: float):
    """解析曲线段里的目标值（tr = 目标回复次数/天、tp = 目标回复概率 %）。

    v3.71（用户 m23596 第 5/8 条）：坏值、负数、0 一律当「没设目标」（回 0），
    这样老配置（段里只有 s/e/v）读出来就是「这一段不自动调」。
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0
    if val <= 0:
        return 0
    val = min(float(high), val)
    if high > 1000:
        return int(round(val))
    return round(val, 1)


def _band_day_target(raw, idx: int, high: float):
    """从「按星期分」的 7 元数组里取第 idx 天（0 = 周一）的目标值。

    v3.81（用户 m02645 图3）：开了「使用星期数据」后，每段的目标不再只有一个统一值，
    而是周一..周日各一个（段上的 trw / tpw）。哪天没单独设（0 或空）就回 0，
    调用方再回落到统一的 tr / tp。
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 7:
        return 0
    try:
        return _band_target(raw[int(idx)], high)
    except (IndexError, TypeError, ValueError):
        return 0


def _norm_phrase(text: str) -> str:
    return str(text or "").strip(_PHRASE_STRIP).lower()
from ..window.manager import (
    clean_content,
    sanitize_native_refs,
    _media_from_db,
    shrink_content,
    shrink_timestamp,
    strip_group_tags,
    is_decision_note,
    text_of_content,
)

logger = get_logger("multi_decision", "orange")

#: 一轮 agent 超过这个秒数还没结算，视为卡死，强制释放忙碌标记
BUSY_TIMEOUT = 300.0
# 投递后多久还没结算就强制解锁。正常一轮 5 步以内几十秒，180s 足够宽裕。
BUSY_WATCHDOG = 180.0
# 看门狗放掉的那一轮如果没重试成功，后面同一会话的一条成功回复就算「接续补上」，
# 但只认这段时间之内的：隔了一夜再回，跟那一轮已经没关系了。
WATCHDOG_RECOVER_SECONDS = 3600.0
# 请求出错后自动重试的次数与间隔是设置项（retry_max / retry_delay，v2.72），
# 别再在这儿写常量：写了也不生效，以后按常量改会以为改错地方。

_MEDIA_CLASSES = (Image, Record, Sticker, Video, File)

#: 框架的 format 模板里引用了两个没被传入的变量，渲染会直接抛
#: UndefinedError: 'message_types' is undefined。assemble_prompt 里是
#: "".join(p.to_string() ...)，一抛整段 system 就没了，模型看不到输出格式要求。
#: 这里给一份兜底文本，渲染失败时把占位符替换掉、降级成静态文本。
#: 框架认定「动态」、组装时会往前插的段名（见 core/provider/llm_model.py）
_DYNAMIC_TAIL_NAMES = ("sessions", "chat_env", "time")

#: 动态段里这两段不再注入（m27316）。系统提示词已写清身份与账号，
#: 群里这两段还会把群名带进来，跨群前缀就此分叉，纯属白占 token。
_NOISY_PROMPT_NAMES = ("sessions", "chat_env")

_TEMPLATE_FALLBACK = {
    "message_types": (
        "<text> 文本内容\n"
        "<emoji> 表情 ID\n"
        "<poke> 戳一戳目标用户 ID\n"
        "<img> 生图提示词（可带 path 属性引用参考图）\n"
        # v3.54（用户 m14082「早上的几条消息 bot 会重复发送」）：模型拿不准表情
        # 该怎么写，就一版写标签、一版在正文里内联 [Emoji 128]，两条都发出去。
        # 这条规矩写进输出格式后，2026-10-25 早间那种成对重复的写法不会再被当成
        # 「两种都可以」来发挥（真跑偏了还有 main.py 的回复收口兜底）。
        "<emoji> 只能用这个标签写，不要写成 [Emoji 128] 这样的文字；"
        "同一条内容只写一遍。"
    ),
    "root_tags": "",
}


def emoji_repr(element) -> str:
    """把一个表情元素还原成框架历史里那种文字：[Emoji 汪汪 (ID: 277)]。

    QQ 表情在框架里既没有图片也没有本地文件，只有一个 id（大表情甚至只剩
    数字）。框架自己把它写成 "[Emoji 427]" 存进记忆，页面要显示真图就得走
    同一条渲染链路，所以这里照原样拼回去（v3.25，用户 m46351 图1/图2）。
    """
    for getter in ("repr",):
        try:
            rep = getattr(element, getter, None)
        except Exception:
            rep = None
        if isinstance(rep, str) and rep.startswith("[Emoji"):
            return rep
    try:
        raw = str(element)
    except Exception:
        raw = ""
    if raw.startswith("[Emoji"):
        return raw
    eid = ""
    for attr in ("id", "emoji_id", "emojiId", "face_id", "face", "code"):
        try:
            val = getattr(element, attr, "")
        except Exception:
            val = ""
        if val is None or val == "":
            continue
        eid = str(val).strip()
        if eid:
            break
    name = ""
    for attr in ("name", "nickname", "summary", "desc"):
        try:
            val = getattr(element, attr, "")
        except Exception:
            val = ""
        if isinstance(val, str) and val.strip():
            name = val.strip()
            break
    if not eid:
        return "[Emoji]" if name else ""
    if name:
        return "[Emoji %s (ID: %s)]" % (name, eid)
    return "[Emoji %s]" % eid

# ---------------------------------------------------------------------------
# 合并转发：ON_MESSAGE 收到的是一条独立元素（repr 只有 "[Forward]"），
# extract_text 只认 Text/At/Emoji，media_kind 也认不出它，正文就是空串；
# push_pending 的判据「正文/媒体/原文全空就丢」于是把整条扔掉，待发列表里
# 永远看不到转发（用户 2026-10-24 图1/图2）。
# 框架历史里转发长这样（requests/*.json 的 user 内容）：
#   [Forward [2026-10-23 19:11:44] 昵称: 正文\n\n[2026-10-23 19:12:09] 昵称: …]
# 这里按「类名/repr 像转发 + 元素里挂着消息列表」把它拼回同款格式，前端照常出卡。
# 不能 import 框架的 Forward 类：core 目录在本机读不了，只能按形状认。
_FORWARD_LIST_ATTRS = (
    # chains 放最前：框架 qq 适配器建的就是 Forward(chains=[MessageChain, …])，
    # 每个 chain 的 message_list 开头是一段 Text「[时间] 昵称: 」（im.py _process_forward_message）。
    "chains", "content", "messages", "message_list", "node_list", "nodes",
    "records", "items", "forward_messages", "msg_list", "list", "data",
)
# 嵌套转发只写一行摘要，不递归展开：一层卡片足够看清「这里有转发」，
# 全展开会让上下文翻几倍，还会和框架历史里的空尾巴对不上。
_FW_NEST_MAX = 80
# 只在第一次遇到转发时打一条结构日志，方便以后按真机字段校对（不刷屏）
_FORWARD_DEBUG_DONE = False


def _looks_forward(element) -> bool:
    try:
        name = type(element).__name__ or ""
    except Exception:
        name = ""
    if "forward" in name.lower():
        return True
    try:
        rep = str(element)
    except Exception:
        rep = ""
    return str(rep).strip().startswith("[Forward")


def _fw_time(obj) -> str:
    for attr in ("timestamp", "time", "ts", "send_time", "datetime", "date"):
        try:
            val = getattr(obj, attr, None)
        except Exception:
            val = None
        if val is None or val == "":
            continue
        if isinstance(val, (int, float)):
            try:
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(val)))
            except Exception:
                continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _fw_name(obj) -> str:
    try:
        sender = getattr(obj, "sender", None) or getattr(obj, "from_user", None)
    except Exception:
        sender = None
    for holder in (sender, obj):
        if holder is None:
            continue
        for attr in ("card", "nickname", "name", "display_name", "user_name", "title"):
            try:
                val = getattr(holder, attr, None)
            except Exception:
                val = None
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _fw_is_chain(obj) -> bool:
    """MessageChain 不是 list 子类，只能按「挂着 message_list」认（message_utils.py:366）。"""
    try:
        lst = getattr(obj, "message_list", None)
    except Exception:
        return False
    return isinstance(lst, (list, tuple))


def _fw_elements_text(chain, depth: int = 0) -> str:
    """把一串元素拍平成文本：正文、艾特、表情、媒体占位；嵌套转发只留一行摘要。"""
    out = []
    try:
        seq = list(chain or [])
    except Exception:
        return ""
    for el in seq:
        if el is None:
            continue
        try:
            if isinstance(el, Text):
                out.append(el.text or "")
                continue
            if type(el).__name__ == "At":
                nm = str(getattr(el, "nickname", "") or getattr(el, "pid", "") or "")
                if nm:
                    out.append("@" + nm)
                continue
            if isinstance(el, Image):
                out.append("[Image attached]")
                continue
            if isinstance(el, Sticker):
                out.append("[Sticker attached]")
                continue
            if isinstance(el, Record):
                out.append("[Record attached]")
                continue
            if isinstance(el, Video):
                out.append("[Video attached]")
                continue
            if isinstance(el, File):
                out.append("[File attached]")
                continue
            if isinstance(el, Emoji):
                out.append(emoji_repr(el))
                continue
            if _looks_forward(el):
                if depth >= 1:
                    out.append(_forward_brief(el))
                else:
                    out.append(_forward_text(el, depth + 1) or "[Forward]")
                continue
            rep = getattr(el, "repr", None)
            if not isinstance(rep, str):
                rep = str(el)
            if rep and rep != "[Forward]":
                out.append(rep)
        except Exception:
            continue
    return "".join(out).strip()


def _fw_is_message(obj) -> bool:
    if isinstance(obj, str):
        return True
    if _fw_is_chain(obj):
        return True
    if isinstance(obj, dict):
        return any(k in obj for k in ("text", "message_str", "content", "nickname", "chain"))
    for attr in ("chain", "message_chain", "message_str", "content", "sender"):
        try:
            if getattr(obj, attr, None) is not None:
                return True
        except Exception:
            continue
    return False


def _fw_items(element) -> list:
    """找出转发元素里挂的那串「被转发的消息」。按属性名优先，再扫所有列表属性。"""
    for attr in _FORWARD_LIST_ATTRS:
        try:
            val = getattr(element, attr, None)
        except Exception:
            val = None
        if isinstance(val, (list, tuple)) and val and _fw_is_message(val[0]):
            return list(val)
    try:
        fields = vars(element)
    except Exception:
        fields = {}
    for val in list(fields.values()):
        if isinstance(val, (list, tuple)) and val and _fw_is_message(val[0]):
            return list(val)
    return []


def _fw_body(item, depth: int = 0) -> str:
    if _fw_is_chain(item):
        # MessageChain：message_list 第一段就是「[时间] 昵称: 」，正文直接带上它，
        # 外层不要再补时间昵称，否则会写出「[time] 名: [time] 名: 正文」。
        return _fw_elements_text(getattr(item, "message_list", None), depth)
    if isinstance(item, dict):
        for key in ("message_str", "text", "content", "message"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        chain = item.get("chain") or item.get("message_chain") or item.get("message_list")
        return _fw_elements_text(chain, depth)
    if isinstance(item, str):
        return item.strip()
    for attr in ("message_str", "text"):
        try:
            val = getattr(item, attr, None)
        except Exception:
            val = None
        if isinstance(val, str) and val.strip():
            return val.strip()
    for attr in ("chain", "message_chain", "message_list"):
        try:
            chain = getattr(item, attr, None)
        except Exception:
            chain = None
        if chain:
            return _fw_elements_text(chain, depth)
    try:
        content = getattr(item, "content", None)
    except Exception:
        content = None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, (list, tuple)):
        return _fw_elements_text(content, depth)
    return ""


def _fw_plain(text: str) -> str:
    """摘要里去掉 [Image attached] 这类占位——中括号会把 [合并转发 …] 的配平搞坏。"""
    try:
        return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", str(text or ""))).strip()
    except Exception:
        return ""


def _forward_brief(element) -> str:
    """嵌套转发的单行摘要：[合并转发 4 条：君墨: 我的大肥鱼… / …]。"""
    items = _fw_items(element)
    if not items:
        return "[合并转发]"
    preview = []
    for item in items:
        body = _fw_plain(_fw_body(item, 1))
        if not body:
            continue
        if _fw_is_chain(item):
            preview.append(body)
        else:
            nm = _fw_name(item)
            preview.append(nm + ": " + body if nm else body)
        if len(" / ".join(preview)) >= _FW_NEST_MAX:
            break
    tail = " / ".join(preview)
    if len(tail) > _FW_NEST_MAX:
        tail = tail[:_FW_NEST_MAX].rstrip() + "…"
    head = "[合并转发 %d 条" % len(items)
    return head + ("：" + tail if tail else "") + "]"


def _forward_text(element, depth: int = 0) -> str:
    """把转发元素拼成框架历史同款的 [Forward …] 块；认不出来就返回空串。"""
    global _FORWARD_DEBUG_DONE
    items = _fw_items(element)
    if not items:
        # 空转发（上游只给了 id、没给 content）——每次空都留证，便于定位能不能补取
        try:
            logger.info(
                "[multi-decision] 空转发探测：cls=%s vars=%s mid=%s chains=%r data=%r repr=%s"
                % (
                    type(element).__name__,
                    sorted(vars(element).keys()) if hasattr(element, "__dict__") else [],
                    str(getattr(element, "message_id", "")),
                    getattr(element, "chains", None),
                    getattr(element, "data", None),
                    repr(element)[:300],
                )
            )
        except Exception as _e:
            logger.info("[multi-decision] 空转发探测失败：%s" % _e)
    if not _FORWARD_DEBUG_DONE:
        _FORWARD_DEBUG_DONE = True
        try:
            attrs = sorted(vars(element).keys()) if hasattr(element, "__dict__") else []
        except Exception:
            attrs = []
        logger.info(
            "[multi-decision] 首次遇到转发的元素探测：type=%s attrs=%s items=%d"
            % (type(element).__name__, attrs, len(items))
        )
    rows = []
    for item in items:
        body = _fw_body(item, depth + 1)
        if _fw_is_chain(item):
            # chain 自带的「[时间] 昵称: 」已经在行首，别再补一遍
            if body:
                rows.append(body)
            continue
        name = _fw_name(item)
        when = _fw_time(item)
        head = ""
        if when:
            head += "[%s]" % when
        if name:
            head += (" " if head else "") + name + ":"
        line = (head + (" " if (head and body) else "") + body).strip()
        if line:
            rows.append(line)
    if rows:
        return "[Forward " + "\n\n".join(rows) + "]"
    for attr in ("message_str", "text", "content"):
        try:
            val = getattr(element, attr, None)
        except Exception:
            val = None
        if isinstance(val, str) and "[Forward" in val:
            return val.strip()
    return ""


def _fill_forward_shell(message, raw: str) -> str:
    """把 raw 里的空壳转发标记换成真内容。

    框架的 message_str 在拿到 chains 之前就写成了 "[Forward message]"，而
    raw_line_of 又优先用 message_str —— 补到内容之后不换掉，窗口里永远是
    「合并转发 · 内容未获取」（用户 m12494 的截图）。
    """
    if "[Forward message]" not in raw and "[Forward]" not in raw:
        return raw
    try:
        chain = getattr(message, "chain", None) or []
    except Exception:
        chain = []
    for el in chain:
        try:
            if not _looks_forward(el):
                continue
            txt = _forward_text(el)
        except Exception:
            continue
        if not txt:
            continue
        if "[Forward message]" in raw:
            return raw.replace("[Forward message]", txt)
        return raw.replace("[Forward]", txt)
    return raw



# ---------------------------------------------------------------------------
# Json 卡片（位置分享 com.tencent.map / 小程序 / 分享卡）：ON_MESSAGE 收到的是
# 一个 Json 元素，extract_text 只认 Text/At/Emoji/转发，media_kind 也认不出它，
# 正文是空串；raw_line_of 又「正文空就返回空串」，push_pending 的判据
# 「正文/媒体/原文全空就丢」于是把整条扔掉 —— 待发列表里看不到，要等下一轮
# 请求框架把历史补进来才出现在窗口里（用户 m11820 第 1 条：图1 图2 待发不显示）。
# 框架历史里它长这样（requests/*.json 的 user 内容）：
#   [Json card {"app": "com.tencent.map", "prompt": "[位置]日本神奈川横浜市", "view": "LocationShare"}]
# 这里按「类名/repr 像 Json 卡 + 元素里挂着 dict」把它拼回同款格式，前端照常出卡。
# 不能 import 框架的 Json 类：core 目录在本机读不了，只能按形状认。
def _looks_json_card(element) -> bool:
    try:
        name = type(element).__name__ or ""
    except Exception:
        name = ""
    low = name.lower()
    if "json" in low or "card" in low:
        return True
    try:
        rep = str(element)
    except Exception:
        rep = ""
    return str(rep).strip().startswith("[Json card")


def _json_card_text(element) -> str:
    """把 Json 卡片元素拼成框架历史里的 [Json card {...}] 一行。"""
    try:
        rep = str(element).strip()
    except Exception:
        rep = ""
    if rep.startswith("[Json card"):
        return rep if rep.endswith("]") else rep + "]"
    payload = None
    for attr in ("data", "json", "content", "card", "payload", "raw"):
        try:
            val = getattr(element, attr, None)
        except Exception:
            val = None
        if isinstance(val, dict):
            payload = val
            break
        if isinstance(val, str) and val.strip().startswith("{"):
            payload = val.strip()
            break
    if payload is None:
        return "[Json card]" if rep else ""
    if isinstance(payload, dict):
        try:
            payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            payload = str(payload)
    return "[Json card %s]" % payload


def extract_text(message) -> str:
    """取出消息里所有文本片段拼成一个字符串。"""
    try:
        chain = getattr(message, "chain", None) or []
    except Exception:
        return ""
    parts = []
    for element in chain:
        if isinstance(element, Text):
            try:
                parts.append(element.text or "")
            except Exception:
                continue
        elif type(element).__name__ == "At":
            # 艾特本身也是内容：群里只发一个 @ 不写字时，只收 Text 会得到空正文，
            # 待发卡片就只剩一个「(空)」（用户 m33778）。
            try:
                nm = str(getattr(element, "nickname", "") or getattr(element, "pid", "") or "")
            except Exception:
                nm = ""
            if nm:
                parts.append("@" + nm)
        elif isinstance(element, Emoji):
            # 表情也是内容：只收 Text 的话，纯表情消息正文为空，待发卡片只剩
            # 一个空气泡、下一轮采集还会被当成「没有正文」直接跳过（v3.25）。
            parts.append(emoji_repr(element))
        elif _looks_forward(element):
            # 合并转发（用户 2026-10-24 图1/图2）：元素不是 Text，正文原本是空串，
            # push_pending 会把整条当「没有内容」丢掉，待发列表永远少这条。
            # 拼成框架历史同款的 [Forward …] 块，前端直接渲染成转发卡。
            parts.append(_forward_text(element) or "[Forward]")
        elif _looks_json_card(element):
            # 位置分享 / 小程序 / 分享卡（用户 m11820 第 1 条）：同上，正文是空串，
            # 待发里整条消失，只有请求后框架补历史才看得到。拼成同款 Json card 行。
            parts.append(_json_card_text(element) or "[Json card]")
    return "".join(parts)


def chain_text(chain) -> str:
    """把 ON_MESSAGE_SENT 的 action 拍平成纯文本（v2.52）。

    框架传进来的是 MessageChain，它只有 __repr__、没有 to_text，直接 str()
    会得到 MessageChain([Text(...)]) 这种对象字面量（用户 m26251）。
    这里按元素拍平：Text 取正文，Reply 丢掉（那是引用不是内容），
    图片/贴纸/表情等保留框架给的可读 repr。
    """
    if chain is None:
        return ""
    if isinstance(chain, str):
        return chain.strip()
    items = getattr(chain, "message_list", None)
    if items is None:
        raw = getattr(chain, "text", None)
        if isinstance(raw, str):
            return raw.strip()
        return str(chain).strip()
    out = []
    try:
        seq = list(items)
    except Exception:
        seq = []
    for el in seq:
        if el is None:
            continue
        try:
            rep = getattr(el, "repr", None)
        except Exception:
            rep = None
        if not isinstance(rep, str):
            rep = str(el)
        if rep.startswith("[Reply"):
            continue
        out.append(rep)
    return "".join(out).strip()


def media_kind(message) -> str:
    """返回消息里的媒体类型，没有媒体返回空串。"""
    try:
        chain = getattr(message, "chain", None) or []
    except Exception:
        return ""
    for element in chain:
        if isinstance(element, Image):
            return "image"
        if isinstance(element, Record):
            return "record"
        if isinstance(element, Sticker):
            return "sticker"
        if isinstance(element, Video):
            return "video"
        if isinstance(element, File):
            return "file"
        if isinstance(element, Emoji):
            return "emoji"
    return ""


def _file_attr(el, names):
    """File 元素的字段名几版框架不太一样，按候选名挨个试。"""
    for nm in names:
        try:
            v = el.get(nm) if isinstance(el, dict) else getattr(el, nm, None)
        except Exception:
            v = None
        if v not in (None, ""):
            return v
    return None


def file_refs_of(message) -> list:
    """把消息里的文件元素摘成 [{name, size, kind}]，只给页面画文件卡用。

    文字上下文里仍然是 [File attached]（v4.22，用户 m21783 选定的口径：
    只美化显示，不动 raw——缓存前缀一个字节都不变）。
    """
    try:
        chain = getattr(message, "chain", None)
        if chain is None and isinstance(message, dict):
            chain = message.get("chain")
    except Exception:
        return []
    out = []
    try:
        for el in chain or []:
            if not isinstance(el, File):
                continue
            name = str(_file_attr(el, ("name", "file_name", "filename", "title", "summary")) or "").strip()
            try:
                size = int(_file_attr(el, ("size", "file_size", "length", "bytes")) or 0)
            except Exception:
                size = 0
            kind = str(_file_attr(el, ("file_type", "kind", "ext", "suffix")) or "").strip().lower()
            if not name and not size:
                continue
            out.append({"name": name, "size": size, "kind": kind})
    except Exception:
        return out
    return out


_REPLY_ID_RE = re.compile(r"-?\d{3,}")
_REPLY_STAMP_RE = re.compile(r"^\[([^\]]+)\]")
_REPLY_NICK_RE = re.compile(r"\[user_nickname:\s*([^,\]]+)")
# bot 自己发的那条在窗口里是 XML 一行：<msg message_id="X">[昵称] <text>正文</text></msg>。
# 它既没有「[message_id: X]」也没有「 | 」，引用回查和正文提取都得单独认它（用户 m11986）。
_MSG_XML_RE = re.compile(
    r'<msg\s+message_id="(-?\d+)"[^>]*>\s*\[([^\]]*)\]\s*<text>([\s\S]*?)</text>',
    re.I,
)
_REPLY_SEEN = []
_REPLY_MISS = []


def _reply_raw_of(el) -> str:
    for getter in ("repr", "text", "content"):
        try:
            v = getattr(el, getter, None)
        except Exception:
            v = None
        if isinstance(v, str) and v.strip():
            return v.strip()
    try:
        return str(el).strip()
    except Exception:
        return ""


def _reply_element(message):
    """挑出 chain 里的引用元素。

    框架各版本给的形状完全不同（用户 m21126）：2026-10-20 那次 repr 是
    「[Reply ID: 1703024648 content: …]」，2026-10-27 只剩「[Reply 1050111951]」
    ——只有 id、没有原话，class 名也不保证。所以按 class 名 + repr 两头认，
    取不到的字段再按属性名一个个试。
    """
    try:
        seq = list(getattr(message, "chain", None) or [])
    except Exception:
        return None
    for el in seq:
        raw = _reply_raw_of(el)
        cls = type(el).__name__
        if "reply" in cls.lower() or raw.lower().startswith("[reply"):
            return el, raw
    # 兜底：有的版本把引用挂在 message.reply 上，不在 chain 里
    try:
        rp = getattr(message, "reply", None)
    except Exception:
        rp = None
    if rp is not None and not isinstance(rp, (str, int, bytes)):
        return rp, _reply_raw_of(rp)
    return None


def _reply_parts(el, raw: str):
    rid = ""
    for attr in ("id", "target_id", "reply_id", "message_id", "msg_id", "seq"):
        try:
            v = getattr(el, attr, None)
        except Exception:
            v = None
        if v is None:
            continue
        s = str(v).strip()
        if s and re.fullmatch(r"-?\d+", s):
            rid = s
            break
    if not rid:
        m = _REPLY_ID_RE.search(raw)
        if m:
            rid = m.group(0)
    txt = ""
    m = re.search(r"content:\s*([\s\S]*)$", raw)
    if m:
        txt = m.group(1).strip()
        # repr 自带外层方括号时（「[Reply ID: x content: …]」）尾巴会多一个 ]，
        # 但原话本身以 ] 结尾的（被引用的是 [表情]）不能删——按中括号是否配平判断
        if txt.endswith("]") and txt.count("[") < txt.count("]"):
            txt = txt[:-1].rstrip()
    if not txt:
        for attr in ("content", "text", "message", "origin", "source"):
            try:
                v = getattr(el, attr, None)
            except Exception:
                v = None
            if isinstance(v, str) and v.strip():
                txt = v.strip()
                break
    if not txt:
        # 框架的 Reply 元素把被引用那条的原文放在 chain 里（QQ 通道 message_content
        # 恒为 None，实测 chain = [Text("[2026-10-29 17:37:43] 樱井桃华: 那我大概已经超越完了")]）。
        # 以前只看属性，引用 bot 自己的消息就只剩一个 id，前端只能画「引用了更早的一条消息」。
        try:
            sub = list(getattr(el, "chain", None) or [])
        except Exception:
            sub = []
        bits = []
        for it in sub:
            v = ""
            for attr in ("text", "content", "repr"):
                try:
                    cand = getattr(it, attr, None)
                except Exception:
                    cand = None
                if isinstance(cand, str) and cand.strip():
                    v = cand.strip()
                    break
            if not v:
                try:
                    v = str(it).strip()
                except Exception:
                    v = ""
            if v:
                bits.append(v)
        txt = re.sub(r"\s+", " ", " ".join(bits)).strip()
    return rid, txt


def _quote_body_of(raw: str) -> str:
    """把一条 raw 行压成引用里那行「2026-10-27 19:10:00 昵称: 正文」。"""
    s = str(raw or "").strip()
    stamp, who, body = "", "", ""
    xm = _MSG_XML_RE.search(s)
    if xm:
        # bot 自己发的那条是 <msg message_id="X">[昵称] <text>正文</text></msg>，
        # 没有「 | 」可切；不单独认它就取不到正文（用户 m11986 图2：引用的正是 bot 上一条）。
        who = str(xm.group(2) or "").strip()
        body = str(xm.group(3) or "").strip()
        tm = _REPLY_STAMP_RE.search(s)
        if tm:
            stamp = tm.group(1).strip()
    else:
        m = _REPLY_STAMP_RE.search(s)
        if m:
            stamp = m.group(1).strip()
        m = _REPLY_NICK_RE.search(s)
        if m:
            who = m.group(1).strip()
        if " | " in s:
            body = s.split(" | ", 1)[1]
    # v4.31：只剥正文开头的完整引用壳；时间戳/卡片等内部也有 []，
    # 不能删到第一个 ] 就停，否则旧原话和残余 ] 会串进下一条引用。
    body = body.strip()
    while re.match(r"^\[Reply(?:\s|:)", body):
        depth, end = 0, 0
        for i, ch in enumerate(body):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if not end:
            # 残缺引用没有可靠结束位置，整段保留，不局部删字或猜正文边界。
            break
        body = body[end:].lstrip()
    iso = ""
    mm = re.match(r"^(\d{1,2}):(\d{2})$", stamp)
    if mm:
        iso = time.strftime("%Y-%m-%d ", time.localtime()) + "%02d:%02d:00" % (int(mm.group(1)), int(mm.group(2)))
    else:
        mm = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})", stamp)
        if mm:
            iso = "%s-%02d-%02d %02d:%02d:00" % (mm.group(1), int(mm.group(2)), int(mm.group(3)), int(mm.group(4)), int(mm.group(5)))
        else:
            mm = re.match(r"^([A-Z][a-z]{2}) (\d{1,2}) (\d{4}) (\d{1,2}):(\d{2})", stamp)
            if mm:
                try:
                    dt = time.strptime("%s %s %s %s:%s" % (mm.group(1), mm.group(2), mm.group(3), mm.group(4), mm.group(5)), "%b %d %Y %H:%M")
                    iso = time.strftime("%Y-%m-%d %H:%M:00", dt)
                except Exception:
                    iso = ""
    head = ""
    if iso:
        head = "[" + iso + "] "
    if who:
        head += who + ":"
    return (head + " " + body).strip()


def _quoted_text(window, sid: str, mid: str) -> str:
    """被引用那条消息的原话——本地本来就有（用户 m21126）。"""
    mid = str(mid or "").strip()
    if window is None or not mid:
        return ""
    key = "[message_id: %s]" % mid
    # bot 自己发的消息在窗口里是 <msg message_id="X">，不带方括号那层（用户 m11986）
    key_xml = 'message_id="%s"' % mid
    try:
        st = window.state(sid)
    except Exception:
        st = None
    if st is None:
        return ""
    for src in (getattr(st, "pending", None), getattr(st, "messages", None)):
        try:
            items = list(src or [])
        except Exception:
            continue
        for item in items:
            raw = item.get("raw", "") if isinstance(item, dict) else getattr(item, "raw", "")
            raw = str(raw or "")
            if key in raw or key_xml in raw:
                got = _quote_body_of(raw)
                if got:
                    return got
    return ""


def reply_line_of(message, lookup=None) -> str:
    """把这条消息的引用取出来，拼成前端认的那一行（v4.02 起，v4.19 重写）。

    前端 splitMeta 认 [Reply ID: … content: …]；但框架给的引用元素只有被引用
    消息的 id（实测「[Reply 1050111951]」），原话得自己补——本地窗口里就有，
    这就是「待发和历史数据本来一致」的那份数据（用户 m21126）。
    """
    pick = _reply_element(message)
    if not pick:
        # 形状再变（class 名和 repr 都不带 reply）就只能靠日志现场看，前 5 次记原文
        if len(_REPLY_MISS) < 5:
            try:
                whole = str(message)
            except Exception:
                whole = ""
            if "[Reply" in whole:
                _REPLY_MISS.append(whole[:200])
                logger.info("[multi-decision] 引用元素没认出来 message=%r", whole[:200])
        return ""
    el, raw = pick
    rid, txt = _reply_parts(el, raw)
    if not txt and lookup is not None and rid:
        try:
            txt = lookup(rid) or ""
        except Exception:
            txt = ""
    if not rid and not txt:
        return ""
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(_REPLY_SEEN) < 30:
        _REPLY_SEEN.append((type(el).__name__, raw[:120], rid, bool(txt)))
        logger.info("[multi-decision] 引用元素 cls=%s raw=%r id=%r 有原话=%s",
                    type(el).__name__, raw[:120], rid, bool(txt))
    if rid and txt:
        return "[Reply ID: %s content: %s]" % (rid, txt)
    if rid:
        return "[Reply ID: %s]" % rid
    return "[Reply %s]" % txt

def raw_line_of(message, text: str, sender: str, name: str, ts: float = 0.0, lookup=None) -> str:
    """拼一条「和框架记忆里长得一样」的消息行。

    优先用框架给的 message_str；它为空时（实测 webchat 通道就是这样）按框架
    的格式自己拼一份，带 message_id 和昵称，下一轮真进记忆时才能对得上、
    不会在窗口里出现两条一样的消息。
    """
    raw = str(getattr(message, "message_str", "") or "").strip()
    if raw:
        raw = _fill_forward_shell(message, raw)
        # v4.11（用户 m17596 第 4 条）：框架给的 message_str **不含引用那一段**
        # （实测 QQ 通道原文只到 "… | @冇似 派头林立是这样的"，[Reply ID …] 整节
        # 缺失），v4.02 拼的引用只挂在下面「自拼」分支里，所以真机上引用块从来没
        # 画出来过。这里把 chain 里的引用补到第一个 " | " 之后，格式与前端
        # splitMeta 期望的一致：head | [Reply ID: x content: …] 正文。
        reply = reply_line_of(message, lookup)
        if reply and "[Reply" not in raw:
            head, sep, tail = raw.partition(" | ")
            return head + sep + reply + " " + tail if sep else raw + " | " + reply
        return raw
    mid = ""
    for attr in ("message_id", "msg_id", "id"):
        try:
            mid = str(getattr(message, attr, "") or "").strip()
        except Exception:
            mid = ""
        if mid:
            break
    try:
        stamp = time.strftime("[%b %d %Y %H:%M %a]", time.localtime(float(ts or time.time())))
    except Exception:
        stamp = ""
    parts = [stamp]
    if mid:
        parts.append("[message_id: %s]" % mid)
    who = str(name or sender or "")
    if who:
        parts.append("[user_nickname: %s, user_id: %s]" % (who, sender or ""))
    body = str(text or "").strip()
    head = " ".join(p for p in parts if p)
    if not body:
        # v3.98（用户 m10169 图3/图4）：纯图片/表情消息 extract_text 摘不出正文
        # （它只认 Text/At/Emoji/转发/卡片），以前这里返回空串，整条待发就在下一次
        # collect 时被 _pending_messages 丢掉——决策读不到那两张图，判不回 fold 也拿不回来。
        # 有媒体就补一个占位，头部行照留：with_owned_images 靠这行里的
        # [message_id: …] 才认得这张图属于谁（split.py:229），图才会挂上去。
        kind = media_kind(message)
        body = {"image": "[图片]", "sticker": "[表情]", "video": "[视频]",
                "record": "[语音]", "file": "[文件]"}.get(kind, "")
        if not body:
            return ""
    # v4.02（用户 m11629 图4/图5）：引用要带回 raw 里，待发卡才画得出「回复了谁」。
    # 引用里的图片仍旧只有 [图片] 占位——那是被引消息的图，不在本条的 media_refs 里。
    reply = reply_line_of(message, lookup)
    if reply:
        return head + " | " + reply + " " + body
    return head + " | " + body


def sender_name_of(message) -> str:
    """尽量取一个人类可读的发送者名字，取不到就退回 id。"""
    try:
        sender = getattr(message, "sender", None)
        if sender is None:
            return ""
        for attr in ("card", "nickname", "name", "display_name"):
            value = getattr(sender, attr, "")
            if value:
                return str(value)
        return str(getattr(sender, "user_id", "") or "")
    except Exception:
        return ""


def sender_id_of(message) -> str:
    try:
        sender = getattr(message, "sender", None)
        if sender is None:
            return ""
        return str(getattr(sender, "user_id", "") or "")
    except Exception:
        return ""


class _InputFlow:
    """Runtime-only admission boundary; never put deferred input in window.pending."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.ready = []
        self.active = []
        self.deferred = deque()
        self.phase = "idle"
        self.epoch = 0
        self.event_id = ""
        self.runner = None
        self.sent_ids = set()
        self.admitting = None  # accepted event suspended in media admission


class DecisionEngine:
    def __init__(self, plugin):
        self.plugin = plugin
        self.ctx = plugin.ctx
        self._states: dict[str, SessionState] = {}
        # 归一 sid -> 框架原始 sid。v2.42 把会话 id 的机器人名前缀去掉了，
        # 但框架自己的会话记忆（buffer、记忆库）还挂在原始 id 上，
        # 回传给框架的接口必须用原始那个，否则框架找不到这个会话。
        self._raw_sids: dict[str, str] = {}
        self._input_ready: set[str] = set()
        self._input_flows: dict[str, _InputFlow] = {}
        self._input_tasks: set = set()
        self._tasks: set = set()
        # 投递后的超时兜底任务（sid -> asyncio.Task），关闭时要一起取消
        self._watchdogs: dict[str, object] = {}
        self._retries: dict[str, object] = {}
        # 每个会话最近一次的原始消息事件（重试时要用它重投同一个 batch）
        self._last_events: dict[str, object] = {}
        self._active_rounds: dict[str, str] = {}
        # v3.51（用户 m07385 第 7 条）：这一轮「触发那一刻」的墙钟（sid -> ts）。
        # 轨迹 begin 时取走写进记录，算「触发 → 请求结束」的整段用时。
        self._round_trigger_at: dict[str, float] = {}
        # 用户 m05453：这一轮「抖动真的等了几秒」与「决策花了多久」，记录页要分三段写。
        self._round_jitter: dict[str, float] = {}
        self._round_decision: dict[str, float] = {}
        self._round_errors: dict[tuple[str, str], str] = {}
        self._settled_rounds: dict[tuple[str, str], float] = {}
        self._retry_events: dict[str, list] = {}
        self._round_roots: dict[tuple[str, str], str] = {}
        self._retry_roots: dict[str, str] = {}
        self._cleared_retry_rounds: set[tuple[str, str]] = set()
        # 已经写过 round_end 的轮（(sid, event_id) -> 写入时刻）。失败轮可能先被
        # 异常钩子/看门狗写一条，隔一会儿又来个 final，同一个 event_id 不能记两遍，
        # 否则统计里的 rounds 会虚高（用户 2026-10-24 第 2 条）。
        self._round_end_logged: dict[tuple[str, str], float] = {}
        # 最近一次被看门狗强制释放的那一轮（全局只留最近一条，健康条要看它）：
        # {sid, root_event_id, ts, attempts, retry_event_id, recovered_by, recovered_ts}
        # 只存「身份」，重试次数与成功与否读的时候从 retry_history 现取，避免两处记账。
        self._watchdog_last: dict = {}
        self.retry_history = None
        self._closed = False
        self.log = None            # 由 main.py 注入 DecisionLog
        self.window = None         # 由 main.py 注入 WindowManager
        self.request_log = None    # 由 main.py 注入 RequestLog
        self.stats = None          # 由 main.py 注入 AdaptiveStats
        self.band_stats = None     # 由 main.py 注入 BandStats（按时段算平均消息/触发/回复）
        self.band_tune = None      # 由 main.py 注入 BandTuner（动态调整：按段微调概率）
        # v3.79（用户 m02095）：动态调整的节奏器（state/band_rhythm.py）。按「这次该
        # 隔几条」排队取代每条独立掷骰——兜底那套会把触发率顶高，调参器追不准。
        self.band_rhythm = None
        # 动态调整这一轮落在哪一段（槽位起止），结算时按它调那一段的概率
        self._adaptive_pending: dict[str, tuple] = {}
        # v3.84（用户 m04084 第 3 条）：判定时调参的内存节流（每条消息都试一遍太费，
        # 这里 60 秒一次；真正的间隔由 BandTuner 的 cooldown 管）
        self._tune_try_at: dict[str, float] = {}
        self.store = None          # 由 main.py 注入 StateStore
        self.overrides = None      # 由 main.py 注入 Overrides（每群独立配置）
        # 运行时健康计数（v2.82）：页面顶部那条状态灯靠它回答「插件还活着吗、
        # 消息收到了吗、为什么一条都没回」。全部只在内存里，重启归零。
        self.health = {
            "started": time.time(),
            "msgs": 0,            # 收到的消息条数（含被静音/太短丢掉的）
            "muted": 0,           # 因为会话被关掉直接丢弃的
            "triggers": 0,        # 触发了几轮
            "delivered": 0,       # 真投进 agent 的轮数
            "finals": 0,          # 收到结算的轮数
            "watchdog": 0,        # 看门狗强制释放的次数
            "last_msg_ts": 0.0,
            "last_msg_sid": "",
            "muted_by_sid": {},
            # v3.75（用户本轮第 3 条）：重试也救不回来的失败。页面顶部那条横幅
            # 读它——出错不能只在一个会话卡的小红字里，翻到别的页面就看不到了。
            "last_fail": {},
            # v3.91（用户 m05749）：进阶决策最近一次判了什么、跳过了几轮
            "last_decision": {},
            "decision_skip": 0,
        }

    def _c(self, key: str, sid: str = ""):
        """取某项配置在当前会话下的有效值。

        每群独立配置全走这里：先查 overrides 再看全局，engine 里任何
        「跟某个群有关」的判定都不该直接读 plugin.xxx。
        """
        try:
            return self.plugin.eff(key, sid)
        except Exception:
            return getattr(self.plugin, key, None)

    # ------------------------------------------------------------------ 持久化

    # 计数最多跨这么久还有效。隔夜的 9 条不该让今天第一条就触发。
    PERSIST_TTL = 6 * 3600

    def load_states(self) -> int:
        """从磁盘恢复会话状态，返回恢复了几个会话。

        热重载会把内存清空，不恢复的话「攒到 9 条」会归零，
        用户看到的就是「计数坏了」。
        """
        if self.store is None:
            return 0
        raw = self.store.load()
        now = time.time()
        restored = 0
        for sid, data in (raw or {}).items():
            try:
                st = SessionState.from_dict(sid, data)
            except Exception:
                continue
            # 太久以前的计数作废，避免昨天的 9 条今天开局就触发
            last = max(
                float(st.last_msg_ts or 0),
                float(st.last_trigger_ts or 0),
                float(st.last_reply_ts or 0),
            )
            if last and now - last > self.PERSIST_TTL:
                st.counter = 0
            # 会话 id 统一去掉适配器名（v2.42）；改名前后留下的多份合并成一份
            st.sid = normalize_sid(sid)
            old = self._states.get(st.sid)
            if old is not None:
                self._states[st.sid] = self._merge_states(old, st)
            else:
                self._states[st.sid] = st
                restored += 1
        if restored:
            logger.info(f"[multi-decision] 已恢复 {restored} 个会话的计数与统计")
        return restored

    @staticmethod
    def _merge_states(old: SessionState, new: SessionState) -> SessionState:
        """同一会话被拆成多份（旧 sid 带机器人名）时合并成一份。

        累计字段相加，瞬时字段以较新的那份为准。改名前后两段数据本来就属于
        同一个会话，分开算会让「总共触发多少轮」少一截。
        """
        if float(new.last_msg_ts or 0) > float(old.last_msg_ts or 0):
            old, new = new, old
        for key in ("total_messages", "total_triggers", "total_replies"):
            setattr(old, key, int(getattr(old, key, 0) or 0) + int(getattr(new, key, 0) or 0))
        for key in ("last_trigger_ts", "last_reply_ts"):
            if float(getattr(new, key, 0) or 0) > float(getattr(old, key, 0) or 0):
                setattr(old, key, getattr(new, key))
        return old

    def _persist_data(self) -> dict:
        out = {}
        for sid, st in self._states.items():
            try:
                out[sid] = st.to_dict()
            except Exception:
                continue
        return out

    def _persist_soon(self) -> None:
        if self.store is None:
            return
        try:
            self.store.save_throttled(self._persist_data())
        except Exception:
            logger.exception("[multi-decision] 会话状态落盘失败（已忽略）")

    # ------------------------------------------------------------------ 状态

    def state(self, sid: str) -> SessionState:
        sid = normalize_sid(sid)      # 会话 id 统一去掉适配器名（v2.42）
        st = self._states.get(sid)
        if st is None:
            st = SessionState(sid=sid)
            self._states[sid] = st
        return st

    def snapshot(self, sid: str) -> dict:
        st = self._states.get(sid)
        return st.snapshot() if st else {"sid": sid}

    # ------------------------------------------------------------------ 入口

    async def handle_llm_request(self, event, request, tag_set=None) -> None:
        """ON_LLM_REQUEST 入口：采集历史 → 换成窗口快照 → 暂存请求记录。

        这里只读不裁。裁切必须等真空闲，见 handle_final。
        """
        try:
            await self._handle_llm_request(event, request)
        except Exception:
            logger.exception(
                "[multi-decision] 窗口钩子异常（已吞掉，本轮退回框架原生记忆）"
            )

    async def _handle_llm_request(self, event, request) -> None:
        plugin = self.plugin
        if not plugin.enabled or request is None:
            return

        sid = ""
        try:
            sid = getattr(event, "sid", "") or event.session.sid
        except Exception:
            sid = getattr(event, "sid", "") or ""
        sid = normalize_sid(sid)
        if not sid:
            return

        eid = str(getattr(event, "event_id", "") or "")
        if self._closed or (sid, eid) in self._settled_rounds:
            return
        if self._active_rounds.get(sid) != eid:
            self._bind_retry_round(sid, eid, event)
            st = self.state(sid)
            st.last_error = ""
            st.last_error_at = 0.0
        self._active_rounds[sid] = eid
        flow = self._input_flow(sid)
        flow.event_id = eid
        if flow.phase == "idle":
            flow.epoch += 1
            flow.phase = "active"
        self._capture_retry_events(sid, event)
        now = time.time()
        for key, settled_at in list(self._settled_rounds.items()):
            if now - settled_at > 900:
                self._settled_rounds.pop(key, None)
                self._round_errors.pop(key, None)
                self._cleared_retry_rounds.discard(key)
        # round_end 去重表同样带 TTL：失败轮的 event_id 过了 15 分钟不可能再来 final
        for key, logged_at in list(self._round_end_logged.items()):
            if now - logged_at > 900:
                self._round_end_logged.pop(key, None)

        epoch = flow.epoch
        media_by_id = await self._incoming_media(event, request, sid)
        if (self._closed or flow.epoch != epoch or flow.phase != "active"
                or self._active_rounds.get(sid) != eid or (sid, eid) in self._settled_rounds):
            return
        framework = list(getattr(request, "messages", None) or [])
        if self._c("window_enabled", sid) and self.window is not None:
            is_group = sid.startswith("gm:")
            added = self.window.collect(
                sid,
                framework,
                skip_history=is_group,
                incoming_ids=self._incoming_ids(request),
                raw_sid=self._raw_sids.get(sid) or sid,
            )
            # v4.05（用户 m12604）：这里只是兜底——正常路径上判定一做完就把自己
            # 那条消息 append 进窗口了（_decision_gate 的判要回／判不回两处），
            # 这一处只补「上轮异常没落下来」的漏网判定。commit_decision 只追加还
            # 没 committed 的，所以正常情况下它什么都不做。
            try:
                self.window.commit_decision(sid)
            except Exception:
                logger.exception(f"[multi-decision] {sid} 决策消息补落失败（已吞掉）")
            # 裁切挪到「本轮请求组装前」（用户 m03343 第 3 条）：这一刻窗口里已经有
            # 本轮输入、上一轮 bot 回复补回的、以及工具往返，全都算名额；裁完紧接着
            # 取快照，所以请求里带的就是保留条数（keep=30 就是 30 条），不会再出现
            # 「刚裁到 36 条、这轮请求也带 36 条」。round_end 那一处仍保留，兜
            # 「之后没人再发消息，所以一直没裁」的场景。
            try:
                self.window.maybe_trim(sid, trigger="before_request", event_id=eid)
            except Exception:
                logger.exception(f"[multi-decision] {sid} 请求前裁切异常（已吞掉）")
            snap = self.window.snapshot(sid)
            if snap:
                request.messages = snap
                logger.debug(
                    f"[multi-decision] {sid} 窗口快照 {len(snap)} 条（本轮新增 {added}）"
                )
            elif is_group:
                # 群聊冷启动：一条历史都不带，只发本轮消息（用户 m24996：
                # 「群聊我说重启后不需要历史消息就是不需要」）
                request.messages = []
                logger.debug(f"[multi-decision] {sid} 群聊冷启动，本轮不带历史")
            else:
                # 私聊窗口还没接到消息时，仍按窗口的格式整理框架历史，
                # 否则这轮发长格式时间戳、下轮接管后发短格式，前缀整段作废（v2.32）
                request.messages = self.window.normalize(framework, sid)

        # 原生图片引用必须指向真实存在的文件（用户 m05205）：框架的
        # resolve_media_references 遇到被清理掉的图会抛 ValueError，而这一抛
        # 在框架里没有日志也没有兜底，整轮请求就这么挂着，只有看门狗 180 秒后
        # 才发现。这里有副本就把文件还原回 session_media，找不回来就换成占位。
        try:
            _kept, _dropped = sanitize_native_refs(request.messages)
            if _dropped:
                logger.warning(
                    f"[multi-decision] {sid} 有 {_dropped} 张图片的原始文件已被框架清理且没有副本，"
                    f"已改成 [图片] 占位（保留 {_kept} 张）"
                )
        except Exception:
            logger.exception(f"[multi-decision] {sid} 原生图片引用检查异常（已吞掉）")

        self._repair_render_failures(request)

        self._hoist_identity(request, sid)

        self._strip_group_tags(request)

        # v4.06（用户 m12879）：本轮真正的 message_id 得在这里算——下面的
        # _strip_seen_incoming 会把已经并进窗口的那几条从 user_prompt 里剔掉，
        # 晚点再取就是空的，快照也就切不出本轮那一段了。
        round_ids = self._incoming_ids(request)

        self._drop_incoming(request)

        self._split_incoming(request)

        self._strip_seen_incoming(request)

        self._mark_mentions(request, sid)

        # 本轮消息发出去之后立刻记进窗口（v2.53，原来只有群聊冷启动才记）。
        # 不记的话它们要等下一轮 collect 从框架记忆里捞回来，这中间的间隙里
        # 既不在历史也不在待发，页面上就是「两轮之间凭空少了几条」（m26251）。
        # commit 在 snapshot 之后，本轮请求不会因此多出内容。
        if self.window is not None and self._c("window_enabled", sid):
            self.window.commit_round(sid, getattr(request, "user_prompt", None), media_by_id=media_by_id)

        self._drop_noisy_prompts(request)

        self._relocate_dynamic_tail(request)

        # v4.33：装配后再落快照，页面与真实请求不再看到两份 system。
        reply_prompt = getattr(plugin, "reply_prompt", None)
        layouts = getattr(plugin, "prompt_layouts", None)
        if layouts is not None and reply_prompt is not None:
            # 用户 m23323 图2：人格正文必须先 await 取到，同步装配那一步才拿得到——
            # 以前没预热就直接同步取，框架方法是协程，取不到就整段丢掉，请求里没人设。
            try:
                await layouts.prefetch_personas(sid)
            except Exception:
                logger.exception("[multi-decision] 预取人格失败（本轮按没有这段继续）")
            reply_prompt.apply(request, sid)

        if self.request_log is not None:
            st = self._states.get(sid)
            self.request_log.stage(
                sid,
                request,
                trigger_reason=(st.last_trigger_reason if st else ""),
                dynamic_position=getattr(plugin, "dynamic_prompt_position", "latest_user"),
                memory_position=getattr(plugin, "memory_prompt_position", "system"),
                window_total=(self.window.counted(sid) if self.window is not None else 0),
                # v3.24（用户 m45932）：柱子悬浮的「user 条数」要用窗口里此刻的真实
                # 用户条数（含本轮刚 commit 进去的），不能从 request.messages 数——
                # 那份是框架历史，缺本轮输入，实测柱子 55 / 快照 65 对不上。
                window_user=(self.window.counted_user(sid) if self.window is not None else None),
                trigger_count=(st.last_trigger_count if st else 0),
                count_threshold=int(self._c("trigger_threshold", sid) or 0),
                trigger_prob=(st.last_trigger_prob if st else 0.0),
                event_id=eid,
                incoming_rows=self._round_rows(sid, round_ids),
                # v4.11（用户 m17596 第 6 条）：这一轮是不是决策判要回之后才发的。
                # 记一次就清，重试轮沿用同一份 pending 不受影响。
                decision_passed=bool(getattr(st, "decision_passed", False)) if st else False,
            )
            if st is not None:
                st.decision_passed = False

    def _repair_render_failures(self, request) -> None:
        """渲染失败的 Prompt 降级成原文，别让整段 system 消失。

        assemble_prompt 是 "".join(p.to_string() ...)，任何一个 Prompt 抛异常
        都会让整段 system 拼不出来——框架不会吞这个异常，但会直接丢掉全部
        系统提示。这里提前把会抛的段换成静态文本。
        """
        prompts = getattr(request, "system_prompt", None) or []
        for p in prompts:
            if not getattr(p, "render_template", False):
                continue
            try:
                p.to_string()
                continue
            except Exception:
                pass
            try:
                content = str(getattr(p, "content", "") or "")
                for key, value in _TEMPLATE_FALLBACK.items():
                    content = content.replace("{{ " + key + " }}", value)
                    content = content.replace("{{" + key + "}}", value)
                p.content = content
                p.render_template = False
                logger.warning(
                    f"[multi-decision] {getattr(p, 'name', '?')} 段渲染失败，已降级为静态文本"
                )
            except Exception:
                logger.exception("[multi-decision] 修复提示词渲染失败（已忽略）")


    #: 从 chat_env 提到系统提示词的三行身份信息（同一会话里固定不变）
    _IDENTITY_LABELS = ("当前平台", "当前聊天类型", "你的账号 ID")

    def _hoist_identity(self, request, sid: str = "") -> bool:
        """把平台 / 聊天类型 / 账号 ID / 群昵称 / 外号提到系统提示词。

        chat_env 每轮都跟着动态段重发一遍，但这五个值在同一个群里是固定的。
        system 里原本有一条 accounts 段，内容与 chat_env 重复（Platform: QQ,
        adapter_name: ..., account_id: ...），只占位置不提供新信息。这里用它
        原来的位置装这几行，并把 chat_env 里重复的行删掉（v2.39，用户 m23841）。

        位置保持 accounts 原位（format 之后、tools 之前）：前面的人格与规则
        每轮、每个群都一样，仍能跨会话共用前缀，只有身份这几行按群变化。
        """
        try:
            system = getattr(request, "system_prompt", None)
            if not isinstance(system, list) or not system:
                return False
            chat = None
            for p in system:
                if (getattr(p, "name", "") or "") == "chat_env":
                    chat = p
                    break
            if chat is None:
                return False
            try:
                text = str(chat.to_string() or "")
            except Exception:
                return False

            found = {}
            rest = []
            for line in text.split("\n"):
                hit = ""
                for label in self._IDENTITY_LABELS:
                    if re.match(r"^\s*[-*]?\s*" + re.escape(label) + r"\s*[：:]", line):
                        hit = label
                        break
                if hit:
                    found[hit] = re.split(r"[：:]", line, maxsplit=1)[-1].strip()
                else:
                    rest.append(line)
            if not found:
                return False

            name = ""
            try:
                name = str(self.plugin.bot_name_for(sid) or "").strip()
            except Exception:
                name = ""
            alias = ""
            try:
                raw = self.plugin.eff("nicknames", sid)
                if isinstance(raw, str):
                    alias = raw.strip()
                elif isinstance(raw, (list, tuple)):
                    alias = "、".join(str(x).strip() for x in raw if str(x).strip())
            except Exception:
                alias = ""

            lines = ["## 你的身份（Identity）"]
            for label in self._IDENTITY_LABELS:
                if label in found:
                    lines.append("- " + label + "：" + found[label])
            lines.append("- 你在群里的昵称：" + (name or "（未设置）"))
            lines.append("- 你在群里的外号：" + (alias or "（未设置）"))
            # v3.46（用户 m05828 第 4 条）：管理员是谁也写进身份段——群里的管理
            # 权限靠人认，模型得先知道「谁说了算」。两项都空就不写，省得占位置。
            # v3.47（用户 m06761 第 7 条）：管理员支持多位，设置页按外号那样加胶囊，
            # 整表以 JSON 存进 admin_list。第 8 条：注入不再带
            # 「（QQ 真实名字，不是群昵称）」「（这个人说的话按管理员处理）」这些括号。
            admins = []
            try:
                raw_admins = self.plugin.eff("admin_list", sid)
                if isinstance(raw_admins, str) and raw_admins.strip():
                    raw_admins = json.loads(raw_admins)
                if isinstance(raw_admins, (list, tuple)):
                    for it in raw_admins:
                        if not isinstance(it, dict):
                            continue
                        a_nm = str(it.get("name") or "").strip()
                        a_qq = str(it.get("qq") or "").strip()
                        if a_nm or a_qq:
                            admins.append((a_nm, a_qq))
            except Exception:
                admins = []
            if not admins:
                # 老配置只有单个管理员（v3.46 的两个字段），没有 admin_list 时照旧生效
                admin_name = ""
                admin_id = ""
                try:
                    admin_name = str(self.plugin.eff("admin_name", sid) or "").strip()
                    admin_id = str(self.plugin.eff("admin_id", sid) or "").strip()
                except Exception:
                    admin_name = ""
                    admin_id = ""
                if admin_name or admin_id:
                    admins = [(admin_name, admin_id)]
            if admins:
                lines.append("- 管理员名字：" + "、".join(n or "（未设置）" for n, _q in admins))
                lines.append("- 管理员 QQ 号：" + "、".join(q or "（未设置）" for _n, q in admins))
            block = "\n".join(lines)

            placed = False
            for p in system:
                if (getattr(p, "name", "") or "") != "accounts":
                    continue
                try:
                    p.content = block
                    p.render_template = False
                    placed = True
                except Exception:
                    pass
                break
            if not placed and Prompt is not None:
                idx = len(system)
                for i, p in enumerate(system):
                    if (getattr(p, "name", "") or "") == "tools":
                        idx = i
                        break
                try:
                    system.insert(
                        idx,
                        Prompt(block, name="accounts", source="system", persist=False),
                    )
                except Exception:
                    return False

            try:
                chat.content = "\n".join(rest).strip("\n")
                chat.render_template = False
            except Exception:
                pass
            logger.debug(
                "[multi-decision] 身份段已提到系统提示词：昵称="
                + (name or "-")
                + " 外号="
                + (alias or "-")
            )
            return True
        except Exception:
            logger.exception("[multi-decision] 提取身份段失败（已忽略）")
            return False

    def _strip_group_tags(self, request) -> None:
        """洗本轮消息前缀：去群标识 + 压时间戳，格式必须和历史一模一样。

        框架的 message_str 每条都带 `[group_name: … group_id: … user_nickname: …]`，
        群名是花体乱码、群号对「谁在说话、说了什么」没用，纯占 token。

        时间戳更要命（v2.32，用户 m22800）：历史是我们压过的 `[12:46]`，
        而框架给的本轮消息还是 `[Oct 20 2026 12:36 Tue]`。不压的话同一条消息
        这一轮发长格式、下一轮进历史变短格式，前缀从这条消息的第一个字符就分叉，
        等于每轮都白算一次整段历史。
        历史那部分由窗口快照统一处理（window.manager.strip_observe）。
        """
        try:
            for p in getattr(request, "user_prompt", None) or []:
                if getattr(p, "name", "") != "message":
                    continue
                content = getattr(p, "content", "")
                if isinstance(content, str):
                    new = shrink_timestamp(content)
                    if "group_name:" in new:
                        new = strip_group_tags(new)
                    if new != content:
                        p.content = new
                    continue
                if isinstance(content, (list, tuple)):
                    # 带图消息的 content 是多模态数组，只洗 str 会整条漏掉
                    p.content = shrink_content(clean_content(content))
        except Exception:
            logger.exception("[multi-decision] 清理群标识失败（已忽略）")

    def _incoming_ids(self, request) -> set:
        """本轮 user_prompt 里那批消息的 message_id 集合。

        window.collect() 会把待发并进窗口；但待发里有一部分就是本轮要发出去的
        消息，框架还会再追加一份 user_prompt，模型于是连着看两遍（用户 m25834
        截图里 [40]~[44] 和 [45]~[48] 是同一批消息）。这两处按 id 对齐，
        让 collect() 跳过它们。
        """
        try:
            raw = getattr(request, "user_prompt", None)
            if isinstance(raw, str):
                return ids_of([{"role": "user", "content": raw}])
            out = set()
            for p in raw or []:
                if getattr(p, "name", "") != "message":
                    continue
                content = getattr(p, "content", "")
                text = content if isinstance(content, str) else text_of_content(content)
                out |= ids_of([{"role": "user", "content": text}])
            return out
        except Exception:
            logger.exception("[multi-decision] 提取本轮消息 id 失败（已忽略）")
        return set()

    def _drop_incoming(self, request) -> int:
        """窗口末几条就是本轮 user_prompt 时把它们删掉，本轮交给 user_prompt 发。

        window.collect() 会把本轮触发的那批消息并进窗口（下一轮它们就是历史，
        必须留在窗口状态里）；但框架另外还会把 user_prompt 作为本轮 user 发
        出去，两份都留着模型就收到两遍——用户 m22999 在请求详情里看到
        `[24] user` 和「本轮消息」是完全同一条。

        拆包之后一条 user_prompt 可能对应窗口末尾的好几条（v2.36），所以按
        message_id 从尾部往回数，删够为止。只删发给模型的那份，
        `WindowManager` 里的状态一条不动。
        """
        try:
            incoming = ""
            for p in getattr(request, "user_prompt", None) or []:
                if getattr(p, "name", "") != "message":
                    continue
                content = getattr(p, "content", "")
                incoming = content if isinstance(content, str) else text_of_content(content)
                break
            msgs = getattr(request, "messages", None)
            if not incoming or not isinstance(msgs, list) or not msgs:
                return 0

            prompt_ids = ids_of([{"role": "user", "content": incoming}])
            if prompt_ids:
                # 窗口尾巴上常常先挂着插件自己写的判定注记（assistant，没有
                # message_id）。老写法一遇到它 ids 就是空 → 直接 break → drop=0，
                # 于是本轮那条在 request.messages 和 user_prompt 里各出现一次，
                # 模型收到两遍（用户本轮：回复记录里同一条 @ 也画两遍）。
                # 现在跳过这种注记继续往前认，按索引删，注记本身留着。
                drop_idx = []
                for i in range(len(msgs) - 1, -1, -1):
                    ids = ids_of([msgs[i]])
                    if not ids:
                        _m = msgs[i]
                        _role = str((_m.get("role") if isinstance(_m, dict)
                                     else getattr(_m, "role", "")) or "")
                        if _role == "assistant":
                            continue
                        break
                    if ids <= prompt_ids:
                        drop_idx.append(i)
                        continue
                    break
                if not drop_idx:
                    return 0
                _gone = set(drop_idx)
                request.messages = [m for i, m in enumerate(msgs) if i not in _gone]
                logger.debug(f"[multi-decision] 窗口末 {len(drop_idx)} 条与本轮消息重复，已去重")
                return len(drop_idx)

            last = msgs[-1]
            content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
            if text_of_content(content).strip() != incoming.strip():
                return 0
            request.messages = msgs[:-1]
            logger.debug("[multi-decision] 窗口末条与本轮消息相同，已去重")
            return 1
        except Exception:
            logger.exception("[multi-decision] 去重本轮消息失败（已忽略）")
            return 0

    def _split_incoming(self, request) -> int:
        """把本轮 user_prompt 里打包的多条群消息拆成多个 part（v2.36）。

        窗口里存的是拆好的单条，本轮如果还按整包发出去，模型会同时看到
        「N 条单条」和「1 条整包」，内容全重复；下一轮这整包进框架记忆、
        再拆开，前缀又对不上。两边都用拆开的形态才一致。
        """
        if Prompt is None:
            return 0
        parts = getattr(request, "user_prompt", None)
        if not isinstance(parts, list) or not parts:
            return 0
        out = []
        changed = 0
        for p in parts:
            if (getattr(p, "name", "") or "") != "message":
                out.append(p)
                continue
            pieces = split_user_content(getattr(p, "content", ""))
            if len(pieces) <= 1:
                out.append(p)
                continue
            built = []
            ok = True
            for one in pieces:
                try:
                    built.append(
                        Prompt(
                            one,
                            name="message",
                            source=getattr(p, "source", "user") or "user",
                            persist=bool(getattr(p, "persist", True)),
                        )
                    )
                except Exception:
                    ok = False
                    break
            if not ok:
                out.append(p)
                continue
            out.extend(built)
            changed += len(built)
        if changed:
            request.user_prompt = out
            logger.debug(f"[multi-decision] 本轮消息拆成 {changed} 条")
        return changed

    def _round_rows(self, sid: str, round_ids) -> list:
        """v4.06（用户 m12879）：这一轮真正新来的那几条 + 紧随其后的决策注记。

        判要回那一轮，本轮消息是先并进窗口、再落决策注记的（_decision_gate），
        所以窗口尾部就是 [本轮 user × N, 注记]。快照据此把 ② 与 ③ 切开：
        ② 只剩真历史，③ 就是「本轮 user → 决策理由」，用户要的结构才看得见。

        取不到本轮 id（框架没给 message_id）或尾部对不上时返回空列表，
        调用方照旧用 user_prompt_parts 显示，不会把上轮的注记当成本轮。
        """
        if self.window is None or not round_ids:
            return []
        try:
            msgs = list(self.window.state(sid).messages or [])
        except Exception:
            return []
        if not msgs:
            return []
        notes = []
        i = len(msgs) - 1
        while i >= 0 and is_decision_note(msgs[i]):
            notes.insert(0, msgs[i])
            i -= 1
        users = []
        while i >= 0:
            m = msgs[i]
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
            if (role or "") != "user":
                break
            if not (ids_of([m]) & set(round_ids)):
                break
            users.insert(0, m)
            i -= 1
        if not users:
            return []
        return list(users + notes)


    def _strip_seen_incoming(self, request) -> int:
        """本轮 user_prompt 里窗口已经有的消息，发出去之前剔掉（v2.49）。

        框架在 @ / 回复这类触发里会把「被引用的旧消息」一起塞进 user_prompt，
        而那些旧消息早就在窗口状态里了——用户 m25834 看到的就是同一条
        （-137785003「够扫」）先在历史 [11]、又在末尾 [13] 出现两遍。

        _drop_incoming 只从尾部连着删，且只认窗口末尾那几条，够不着窗口中间
        的老消息；这里按 id 对齐，直接剔掉 prompt 里重复的那一条。
        只在拆包（_split_incoming）之后调用，保证一个 part 就是一条消息；
        至少留一条，免得 prompt 被清空。
        """
        try:
            parts = getattr(request, "user_prompt", None)
            if not isinstance(parts, list) or not parts:
                return 0
            seen = ids_of(getattr(request, "messages", None) or [])
            if not seen:
                return 0
            total = sum(1 for p in parts if (getattr(p, "name", "") or "") == "message")
            if total <= 1:
                return 0
            # v4.06（用户 m12879）：判要回那一轮本轮消息已经先并进窗口了，这里要把
            # prompt 里的重复条目剔干净——留一条就会排到决策注记后面，顺序正好反了。
            # v4.12（用户 m02159 图6）：但一条都不留也不行——模型看不到「现在要回的是
            # 哪条」，10-28 17:04 那一轮就是这样跑偏的（写了一段裸文本，框架解析 0 条，
            # 最终没发出去）。所以重复条目再多，也留最后一条在本轮当锚点。
            keep_one = not bool(getattr(self.plugin, "dynamic_to_tail", True))
            msg_pos = [k for k, p in enumerate(parts)
                       if (getattr(p, "name", "") or "") == "message"]
            dup_pos = []
            for k in msg_pos:
                text = getattr(parts[k], "content", "")
                text = text if isinstance(text, str) else text_of_content(text)
                ids = ids_of([{"role": "user", "content": text}])
                if ids and (ids & seen):
                    dup_pos.append(k)
            room = len(msg_pos) - (1 if keep_one else 0)
            drop = set(dup_pos[:room]) if room > 0 else set()
            anchored = bool(msg_pos) and len(dup_pos) >= len(msg_pos) and not keep_one
            if room > 0 and len(drop) >= len(msg_pos):
                drop.discard(msg_pos[-1])
            out = [p for k, p in enumerate(parts) if k not in drop]
            dropped = len(drop)
            if dropped:
                request.user_prompt = out
                logger.info(
                    "[multi-decision] 本轮消息 %d 条，其中 %d 条历史里已有，已剔掉%s",
                    total,
                    dropped,
                    "（留了最后一条当本轮锚点）" if anchored else "",
                )
            return dropped
        except Exception:
            logger.exception("[multi-decision] 剔除重复本轮消息失败（已忽略）")
            return 0


    def _mark_mentions(self, request, sid: str = "") -> int:
        """消息里喊到机器人时，在 meta 后面插一个 [@你]（用户 m23841）。

        放在 meta（[message_id: …] [user_nickname: …]）后面、正文前面：
        正文一个字不改，模型一眼能看出「这条是在叫我」。

        - 宽松匹配：正文里出现昵称或外号就算。QQ 侧的「@某人」落下来就是
          普通文本，没有结构化标记可依；event.is_mentioned 只能覆盖当轮，
          下一轮它变成历史就没了，那样同一行前后不一致，前缀会从这行断掉。
        - 历史也标：每轮都按同一份昵称重算，同样的输入产出同样的标记，
          所以不影响前缀缓存；只有改昵称/外号会断一次。
        """
        names = []
        try:
            own = str(self.plugin.bot_name_for(sid) or "").strip()
            if own:
                names.append(own)
            for nick in (self._c("nicknames", sid) or []):
                nick = str(nick or "").strip()
                if nick and nick not in names:
                    names.append(nick)
        except Exception:
            logger.exception("[multi-decision] 取唤醒昵称失败（已忽略）")
            return 0
        if not names:
            return 0
        try:
            changed = mark_messages(getattr(request, "messages", None) or [], names)
            for p in getattr(request, "user_prompt", None) or []:
                if (getattr(p, "name", "") or "") != "message":
                    continue
                content = getattr(p, "content", "")
                if isinstance(content, str):
                    new = mark_mention(content, names)
                    if new != content:
                        p.content = new
                        changed += 1
                elif isinstance(content, (list, tuple)):
                    parts = list(content)
                    hit = False
                    for i, item in enumerate(parts):
                        if not isinstance(item, dict) or item.get("type") != "text":
                            continue
                        text = str(item.get("text") or "")
                        new = mark_mention(text, names)
                        if new != text:
                            parts[i] = dict(item, text=new)
                            hit = True
                    if hit:
                        p.content = parts
                        changed += 1
            if changed:
                logger.debug(f"[multi-decision] {changed} 条消息打了唤醒标记")
            return changed
        except Exception:
            logger.exception("[multi-decision] 打唤醒标记失败（已忽略）")
            return 0

    def _drop_noisy_prompts(self, request) -> int:
        """去掉动态段里两段冗余信息（Sessions / Chat Environment）。

        记忆注入（memory）与时间（time）保留——它们是模型真正要看的。
        system_prompt 与 user_prompt 都扫一遍：前者是没挪动时的位置，
        后者是 dynamic_to_tail 挪过去之后的位置。
        """
        dropped = 0
        for attr in ("system_prompt", "user_prompt"):
            prompts = getattr(request, attr, None)
            if not isinstance(prompts, list):
                continue
            kept = []
            for p in prompts:
                if (getattr(p, "name", "") or "") in _NOISY_PROMPT_NAMES:
                    dropped += 1
                    continue
                kept.append(p)
            if len(kept) != len(prompts):
                setattr(request, attr, kept)
        if dropped:
            logger.debug(f"[multi-decision] 动态段已去掉 {dropped} 段冗余信息（sessions/chat_env）")
        return dropped

    def _relocate_dynamic_tail(self, request) -> bool:
        """把动态段从「本轮消息前面」挪到「本轮消息后面」。

        框架把 sessions / chat_env / memory / time 拼成最后一条 user 的开头。
        于是这一轮发出去的内容是 [动态段][正文]，但下一轮它变成历史时框架存的
        只是 [正文]——同一个位置 token 对不上，前缀缓存从这条开始整段失效，
        每轮都要重算「上一轮正文 + 上一轮回复」（群里常有 2000~4000 字符）。

        改成 [正文][动态段] 之后，正文仍在老位置上，上一轮正文能完整命中，
        只白算动态段（~400 字符）与上一轮回复。代价是动态信息排在正文之后，
        模型对时间的注意力会稍弱——由 dynamic_to_tail 开关控制，可关。
        """
        if Prompt is None:
            return False
        try:
            if not bool(getattr(self.plugin, "dynamic_to_tail", True)):
                return False
        except Exception:
            return False

        system_prompt = getattr(request, "system_prompt", None)
        user_prompt = getattr(request, "user_prompt", None)
        if not isinstance(system_prompt, list) or not isinstance(user_prompt, list):
            return False

        names = set()
        try:
            if getattr(self.plugin, "dynamic_prompt_position", "latest_user") == "latest_user":
                names.update(_DYNAMIC_TAIL_NAMES)
            if getattr(self.plugin, "memory_prompt_position", "system") == "latest_user":
                names.add("memory")
        except Exception:
            return False
        if not names:
            return False

        moved, keep = [], []
        for p in system_prompt:
            if (getattr(p, "name", "") or "") in names:
                moved.append(p)
            else:
                keep.append(p)
        if not moved:
            return False

        for p in moved:
            try:
                p.persist = False
            except Exception:
                pass

        try:
            head = Prompt(
                "<system_reminder>",
                name="dynamic_context_start",
                source="system",
                persist=False,
            )
            tail = Prompt(
                "</system_reminder>",
                name="dynamic_context_end",
                source="system",
                persist=False,
            )
        except Exception:
            logger.exception("[multi-decision] 造动态段包裹失败（本轮保持框架默认）")
            return False

        # v2.70：那句「role=assistant 都是你自己」删掉了（用户 m30252 明确不要）。
        # assistant 消息现在带 [HH:MM] 时间戳、且不再贴 [名字]，与 user 的格式区分得开，
        # 不需要再多发一段说明去占 token。
        request.system_prompt = keep
        request.user_prompt = list(user_prompt) + [head] + moved + [tail]
        logger.debug(
            f"[multi-decision] 动态段已挪到本轮消息之后：{', '.join(getattr(p, 'name', '?') for p in moved)}"
        )
        return True

    async def reset_session_input(self, sid: str) -> None:
        """Discard buffered input at an explicit session enable/disable boundary."""
        sid = normalize_sid(sid)
        self._input_ready.discard(sid)
        self._cancel_retry(sid)
        self._retry_status(sid, self._retry_roots.pop(sid, ""), "cancelled")
        self._retry_events.pop(sid, None)
        st = self._states.get(sid)
        if st is not None:
            st.retry_at = 0.0
            st.retry_count = 0
        flow = self._input_flow(sid)
        async with flow.lock:
            flow.epoch += 1
            flow.ready.clear()
            flow.active.clear()
            flow.deferred.clear()
            flow.phase = "idle"
            flow.event_id = ""
            flow.sent_ids.clear()
            if st is not None:
                st.busy = False
                st.busy_since = 0.0
                st.queued = False
            await self._discard_buffered_input(sid, set())

    async def _discard_buffered_input(self, sid: str, keep_ids: set) -> None:
        getter = getattr(self.ctx, "get_buffer", None)
        if not callable(getter):
            return
        buffer = getter(self._raw_sids.get(sid) or sid)
        def stale(event):
            message = getattr(event, "message", None)
            mid = str(getattr(message, "message_id", "") or "")
            return not mid or mid not in keep_ids
        async with buffer.lock:
            drain = getattr(buffer, "flush", None)
            if callable(drain):
                drain(filter_fn=stale)
            else:
                # Compatibility with hosts exposing only pop/add.
                events = buffer.pop(buffer.get_length())
                for event in events:
                    if not stale(event):
                        buffer.add(event)

    async def _prepare_session_input(self, sid: str) -> None:
        if sid in self._input_ready:
            return
        keep = set()
        if self.window is not None:
            for item in self.window.pending_of(sid):
                keep |= ids_of([{"role": "user", "content": item.get("raw", "")}])
        await self._discard_buffered_input(sid, keep)
        self._input_ready.add(sid)

    async def _incoming_media(self, event, request, sid: str) -> dict:
        """Cache direct attachments without traversing replies or forwards."""
        incoming = self._incoming_ids(request)
        media = {}
        if self.window is not None:
            for item in self.window.pending_of(sid):
                for mid in ids_of([{"role": "user", "content": item.get("raw", "")}]) & incoming:
                    media[mid] = list(item.get("media_refs") or [])
        for message in getattr(event, "messages", None) or []:
            mid = str(getattr(message, "message_id", "") or "")
            if mid and mid in incoming:
                refs = await self._cache_media(message, sid)
                if not refs:
                    # 兜底：元素上没带出来也不要紧，消息库里存着归档路径
                    try:
                        refs = _media_from_db(mid)
                    except Exception:
                        refs = []
                if refs:
                    media[mid] = refs
        request._mdp_incoming_media = media
        request._mdp_media_raw_sid = self._raw_sids.get(sid) or sid
        return media

    async def handle_message(self, event) -> None:
        """ON_IM_MESSAGE 入口。任何异常都不能漏进流水线。"""
        try:
            await self._handle_message(event)
        except Exception:
            logger.exception("[multi-decision] 决策钩子异常（已吞掉，不影响消息流转）")

    def _input_flow(self, sid: str) -> _InputFlow:
        return self._input_flows.setdefault(sid, _InputFlow())

    async def _handle_message(self, event) -> None:
        # asyncio.Lock is FIFO: register before the first media/cache await, not
        # when downloads complete. Never hold this lock across decide/publication.
        sid = normalize_sid(getattr(getattr(event, "session", None), "sid", ""))
        if not sid or self._closed:
            return
        task = asyncio.current_task()
        self._input_tasks.add(task)
        try:
            async with self._input_flow(sid).lock:
                if not self._closed:
                    await self._receive_message(event)
        finally:
            self._input_tasks.discard(task)

    def _qq_bot(self):
        """拿 QQ 适配器底下的 NapCat 客户端。

        ctx 上这个管理器叫 adapter_mgr（不是 adapter_manager）；get_adapters() 的
        key 是显示名（"樱井桃华"），按 "qq" 取不到，只能按类名认。
        """
        mgr = getattr(self.ctx, "adapter_mgr", None)
        if mgr is None:
            return None
        try:
            ads = mgr.get_adapters()
        except Exception:
            return None
        items = list(ads.values()) if isinstance(ads, dict) else list(ads or [])
        for one in items:
            if type(one).__name__ != "QQAdapter":
                continue
            bot = getattr(one, "bot", None)
            if bot is not None and hasattr(bot, "send_action"):
                return bot
        return None

    async def _hydrate_forwards(self, message) -> int:
        """把空壳转发补成真内容（框架 im.py 把 message_id 当成 res_id 传给 get_forward_msg，
        取不到就 Forward(chains=[])）。必须在 _admit_message 取正文之前做。"""
        try:
            chain = list(getattr(message, "chain", None) or [])
        except Exception:
            return 0
        need = []
        for el in chain:
            try:
                if _looks_forward(el) and not _fw_items(el):
                    need.append(el)
            except Exception:
                continue
        if not need:
            return 0
        mid = ""
        for attr in ("message_id", "msg_id", "id"):
            try:
                mid = str(getattr(message, attr, "") or "").strip()
            except Exception:
                mid = ""
            if mid:
                break
        if not mid:
            return 0
        bot = self._qq_bot()
        if bot is None:
            logger.info("[multi-decision] 空转发补取跳过：没找到 QQ 适配器（mid=%s）" % mid)
            return 0
        try:
            items = await fetch_forward_items(bot, mid)
        except Exception as exc:
            logger.info("[multi-decision] 空转发补取异常 mid=%s：%s: %s" % (mid, type(exc).__name__, exc))
            return 0
        if not items:
            logger.info("[multi-decision] 空转发补取无内容 mid=%s" % mid)
            return 0
        for el in need:
            try:
                setattr(el, "chains", list(items))
            except Exception:
                continue
        try:
            remember_by_mid(mid, [i["message_str"] for i in items])
        except Exception:
            pass
        logger.info("[multi-decision] 空转发补取成功 mid=%s，%d 条" % (mid, len(items)))
        return len(items)

    async def _receive_message(self, event) -> None:
        plugin = self.plugin
        if not plugin.enabled or not plugin.decision_enabled:
            return

        # 单会话开关：关掉就完全不碰这个群/私聊——不计数、不进窗口、不触发。
        # 注意 default-chat 被接管后是停用的，所以这里关掉等于「这个会话没人回」，
        # 这是用户要的语义（页面上有说明）。
        try:
            _sid0 = normalize_sid(event.session.sid)
        except Exception:
            _sid0 = ""
        if _sid0:
            try:
                self._raw_sids[_sid0] = str(event.session.sid or "") or _sid0
            except Exception:
                self._raw_sids[_sid0] = _sid0
            # 窗口也记一份：图片要按 message_id 反查，哈希用的是框架原始 sid（v2.85）
            try:
                if self.window is not None:
                    self.window._raw_sids[_sid0] = self._raw_sids[_sid0]
            except Exception:
                pass
            # _last_events 只在真正 admit 时更新；隔离输入不能变成旧轮的重投模板。
            # v3.23（用户 m45332 第6条）：第一次出现的会话立刻关掉插件。
            # 放在静音判定之前——新群进来的第一条消息就该被拦住，别先攒一轮再关。
            try:
                plugin.note_new_session(_sid0)
            except Exception:
                logger.exception("[multi-decision] 新会话默认关闭失败（已忽略）")
        # 只有「明确被设成 False」才拦；取不到值（None）按开处理，
        # 否则一个读配置失败就会把所有会话静音。
        if _sid0 and self._c("session_enabled", _sid0) is False:
            # 被关掉的会话最容易看成「插件卡死」：消息照进日志，页面一动不动。
            # 这里记账，页面才能明说「已静音 · 忽略 N 条」（v2.82）
            self.health["muted"] += 1
            # msgs 的语义是「收到的消息条数（含被静音/太短丢掉的）」，
            # 这里漏了会让健康条在 uptime>10 分钟时误报「一条消息都没收到」
            self.health["msgs"] += 1
            self.health["last_msg_ts"] = time.time()
            self.health["last_msg_sid"] = _sid0
            try:
                mb = self.health["muted_by_sid"]
                mb[_sid0] = int(mb.get(_sid0, 0)) + 1
                n = int(mb[_sid0])
                # 静音是完全静默的：日志里一条痕迹都没有，看日志只会以为插件死了。
                # 每会话只留第 1 条和第 50/100/150… 条，既能查又不会刷屏。
                if n == 1 or n % 50 == 0:
                    logger.info(
                        f"[multi-decision] {_sid0} 这个会话被单独静音了，已跳过 {n} 条消息"
                        f"（左栏会话开关或设置页可以恢复）"
                    )
            except Exception:
                pass
            return

        # default-chat 被重载会把 _terminating 复位，这里顺手自愈
        plugin.ensure_takeover()

        message = getattr(event, "message", None)
        if message is None:
            return

        # 空壳合并转发：框架只留了 {"type":"forward"}，这里自己问 NapCat 要回内容
        # （用户 m12494）。失败就保持原样，前端照旧画「内容未获取」，不编内容。
        try:
            await self._hydrate_forwards(message)
        except Exception:
            logger.exception("[multi-decision] 转发补取失败（已忽略）")

        # 机器人自己的名字：优先用适配器里的真实昵称（QQ 里别人看到的名字），
        # 而不是人格文件名。@ 消息里是 `[At 1429260473(nickname: 桃华)]`，
        # 历史里 assistant 前缀是【名字】——两边必须一致，模型才知道被 @ 的是自己。
        # 名字按会话记：群聊里是群名片，私聊里是 QQ 昵称，同一个 bot 两处可以不同。
        if getattr(plugin, "bot_name_auto", False):
            try:
                bot = getattr(message, "bot", None)
                nick = str(getattr(bot, "nickname", "") or "").strip()
                if nick:
                    try:
                        _sid = normalize_sid(event.session.sid)
                    except Exception:
                        _sid = ""
                    names = getattr(plugin, "bot_names", None)
                    changed = isinstance(names, dict) and _sid and names.get(_sid) != nick
                    remember = getattr(plugin, "remember_bot_name", None)
                    if callable(remember):
                        # 落盘 + 按会话记（热重载后不会丢，也不会拿别的会话的名字顶）
                        remember(_sid, nick)
                    elif isinstance(names, dict) and changed:
                        names[_sid] = nick
                    if changed:
                        logger.info(f"[multi-decision] bot 名字取适配器昵称：{_sid} → {nick}")
                    if nick != getattr(plugin, "bot_name", ""):
                        plugin.bot_name = nick
            except Exception:
                pass

        sid = normalize_sid(event.session.sid)
        st = self.state(sid)
        now = time.time()
        self.health["msgs"] += 1
        self.health["last_msg_ts"] = now
        self.health["last_msg_sid"] = sid

        # The timer also covers decide; retain an arrival-driven escape hatch.
        if st.busy and st.busy_since and (now - st.busy_since) > BUSY_TIMEOUT:
            st.last_error = "上一轮超过忙碌时限未结算"
            st.last_error_at = now
            self._watchdog_record_failure(sid, st)
            self._retry_after_fail(sid, st)
            await self._settle_input_locked(sid, self._input_flow(sid), trigger="watchdog")

        # 自己发的消息不回灌
        sender = sender_id_of(message)
        try:
            self_id = str(getattr(message, "self_id", "") or "")
        except Exception:
            self_id = ""
        if sender and self_id and sender == self_id:
            return

        # 通知类（入群、撤回等）直接丢弃，不计数也不进上下文
        if getattr(event, "is_notice", False):
            event.discard(force=True)
            return

        await self._prepare_session_input(sid)

        # 先收进 buffer 并锁死策略，避免 default-chat 抢先 discard/flush
        if not event.buffer(force=True):
            return

        flow = self._input_flow(sid)
        # 用户 m05124 第 5 条：已经触发的那段也算「运行期间」——正在抖动等，或者已经派活。
        # 以前只拦 busy / phase，抖动的几秒里进来的消息会被正常接纳进这一轮，用户就是
        # 「@ 之后进来的消息没被按运行期间拦截」。
        claimed = st.round_claimed > 0 and (now - st.round_claimed) < 600
        if st.busy or flow.phase != "idle" or flow.deferred or claimed:
            # The host buffers the original event after this hook returns. Replay
            # only admission below: never buffer/add/copy/publish this event again.
            flow.deferred.append((event, now))
            if not st.busy and flow.phase == "idle":
                await self._drain_input_locked(sid, flow)
            return
        await self._admit_message(event, sid, st, now, flow)

    async def _admit_message(self, event, sid: str, st, now: float, flow) -> None:
        """Internal replay entry: already buffered, ordered, no receive accounting."""
        message = event.message
        epoch = flow.epoch
        sender = sender_id_of(message)
        self._last_events[sid] = event
        flow.ready.append(event)
        flow.admitting = (event, now)
        text = extract_text(message)

        # 用户 m11091 第 1 条：来了新消息就把上一轮的「静默 / 休兜已冒泡」勾撤掉，
        # 倒计时从头算。否则勾会一直挂着，看不出这一轮到底触发过没有。
        if st.silent_fired_ts or st.rest_fired_ts:
            st.silent_fired_ts = 0.0
            st.rest_fired_ts = 0.0

        # 上下文页要「一直能看到下一轮会送给 agent 什么」，这里挂进窗口的待发列表
        pend = None
        media_refs = []
        if self.window is not None:
            try:
                media_refs = await self._cache_media(message, sid)
                if not media_refs:
                    # v4.38（用户 m20312）：框架把图归档进 session_media/archive/ 之后，
                    # 消息链上拿不到可直接用的路径，按 message_id 回消息库捞一次。
                    for _attr in ("message_id", "msg_id", "id"):
                        try:
                            _mid = str(getattr(message, _attr, "") or "").strip()
                        except Exception:
                            _mid = ""
                        if _mid:
                            break
                    if _mid:
                        media_refs = _media_from_db(_mid) or []
            except Exception:
                logger.debug("[multi-decision] 缓存待发图片失败（已忽略）", exc_info=True)
        if self._closed or flow.epoch != epoch:
            return
        file_refs = []
        if self.window is not None:
            try:
                file_refs = file_refs_of(message)
            except Exception:
                logger.debug("[multi-decision] 取待发文件信息失败（已忽略）", exc_info=True)
        if self.window is not None:
            try:
                pend = self.window.push_pending(
                    sid, sender, sender_name_of(message), text, media_kind(message), now,
                    # 框架格式化好的原文：下一轮采集直接拿它当窗口消息，
                    # 格式与框架记忆一致，去重和缓存前缀都对得上
                    raw_line_of(message, text, sender, sender_name_of(message), now,
                                 lookup=lambda mid: _quoted_text(self.window, sid, mid)),
                    media_refs=media_refs,
                    files=file_refs,
                )
            except Exception:
                logger.exception("[multi-decision] 记窗口待发消息失败（已忽略）")
        flow.admitting = None

        # 用户 m13250 第 4 条：私聊不再有独立策略，走下面同一条触发链（计数 / 概率 / 直达 /
        # 休息时段 / 短语过滤 / 连发合并 / 退避 / 配额都一样）。新开的私聊默认「计数模式 + 阈值 1」
        # （见 main.py note_new_session），一条消息就触发，效果和原来的必回一样。

        # 自适应统计：所有消息都算「收到」，含短消息与连发
        if self.stats is not None:
            try:
                self.stats.record_message(sid, now)
            except Exception:
                logger.exception("[multi-decision] 记录自适应统计失败")

        # 用户 m00347：「必回」正式开始用——叫到就直接回，跳过模型判断
        # （_must_reason 给的 reason 是 mention / nickname，_decision_gate 的白名单里没有，
        # 所以这一路永远不问模型）。「触发」那一路的 reason 记成 wake，进了白名单，
        # 开了进阶模式就先让模型判一次要不要开口。
        # 用户 m00363：休息时段里还要不要理，按「休息时段·触发 / 休息时段·必回」两个
        # 开关分路裁剪；两个都默认开着，跟改之前一样（休息时被叫到照样回）。
        in_rest = self._in_rest_window(now, sid)
        must = self._must_reason(event, text, sid)
        direct = self._direct_reason(event, text, sid)
        if in_rest:
            if not self._c("rest_wake_always", sid):
                must = ""
            if not self._c("rest_wake_trigger", sid):
                direct = ""

        if must:
            st.last_sender_id = sender
            st.last_msg_ts = now
            # 必回也要过配额（用户 m00363：每日配额 必回也算配额里）；静音在更前面
            # 就拦掉了，所以静音照样生效，不用在这再判一次。
            if self._quota_blocked(st, now):
                self._mark(sid, pend, "quota")
                self._write_log(sid, "quota_skip")
                return
            self._mark(sid, pend, "direct")
            self._write_log(sid, "direct")
            self._trigger(sid, must, st)
            return

        if direct:
            st.last_sender_id = sender
            st.last_msg_ts = now
            self._mark(sid, pend, "direct")
            self._write_log(sid, "direct")
            self._trigger(sid, "wake", st)
            return

        # 休息时段：这段时间只按「休息回复概率」掷骰——不看曲线、不走动态调整、
        # 也不吃伪随机。v3.72（用户 m24260 第 2 条）：普通模式用 rest_*（设置页放在
        # 普通模式那一列），曲线模式有自己独立的 band_rest_*（画在曲线上、默认
        # 23:00–09:00）。_in_rest_window / _rest_probability 按当前模式自动挑一套。
        if in_rest:
            probability = self._rest_probability(sid)
            if random.uniform(0, 100) >= probability:
                self._mark(sid, pend, "rest")
                self._write_log(sid, "rest_skip")
                return
            # 掷中了就按这个概率直接回一条，不进计数（休息时段的条数不攒）
            if self._quota_blocked(st, now):
                self._mark(sid, pend, "quota")
                self._write_log(sid, "quota_skip")
                return
            self._mark(sid, pend, "counted")
            self._trigger(sid, "rest", st, prob=float(probability))
            return

        # v3.16（用户 m40583）：概率模式要「每条消息独立掷骰」。短消息过滤和
        # 连发合并都是计数模式攒条数的配套规则，套在概率模式上会让实际触发率
        # 明显低于设定值（「哈哈」直接被过滤掉、同一个人连发只算一条，都不掷骰）。
        prob_mode = (self._c("trigger_mode", sid) == "probability")

        # v3.33（用户 m51068 第 11 条）：字数阈值下线，改成「整条就是清单里的短语」
        # 才不计入触发。带媒体的消息照常计数。
        media = media_kind(message)
        if not prob_mode and not media and self._phrase_hit(text, sid):
            self._mark(sid, pend, "short")
            self._write_log(sid, "phrase_skip")
            return

        # 连发合并：同一个人短时间内连续发，只算 1 条。
        # v3.44（用户本轮第 2 条）：加开关，默认关——关着时每条都照常计数，
        # 开了才按 burst_merge_seconds 把同一人的连发合并成 1 条。
        if (
            not prob_mode
            and self._c("burst_merge_enabled", sid)
            and sender
            and sender == st.last_sender_id
            and (now - st.last_msg_ts) <= self._c("burst_merge_seconds", sid)
        ):
            st.last_msg_ts = now
            self._mark(sid, pend, "burst")
            self._write_log(sid, "burst_merged")
            return
        st.last_sender_id = sender
        st.last_msg_ts = now

        # 计数
        st.roll_hour(now)
        st.counter += 1
        st.total_messages += 1
        st.hour_messages += 1

        # 退避
        # v3.52（用户 m08226 第 6 条）：退避只有开了进阶模式才生效
        if (
            self._c("advanced_enabled", sid)
            and self._c("backoff_enabled", sid)
            and st.backoff_until > now
        ):
            if st.counter < self._c("backoff_bypass_count", sid):
                self._mark(sid, pend, "backoff")
                self._write_log(sid, "backoff_skip")
                return
            logger.info(
                f"[multi-decision] {sid} 积压 {st.counter} 条，达到绕过阈值，忽略退避"
            )

        # 判定：口径（计数/概率/曲线/自适应）跟概率一起带回来，别在触发时另行编一个
        # 「count」（用户 m15045：概率模式的触发在请求页全被写成「攒够条数」）。
        hit, reason, prob = self._judge(st)
        if not hit:
            self._mark(sid, pend, "counted")
            self._write_log(sid, "counting")
            return

        # 配额：本小时说得够多了就先忍住。计数不清零，配额一恢复立刻触发。
        if self._quota_blocked(st, now):
            self._mark(sid, pend, "quota")
            self._write_log(sid, "quota_skip")
            return

        self._mark(sid, pend, "counted")
        self._write_log(sid, "counting")
        self._trigger(sid, reason, st, prob=prob)

    async def _cache_media(self, message, sid: str) -> list:
        """把这条消息里的图片/表情存一份到本地，页面才能显示出来（用户 m28965）。

        框架本来就会把图片落到 data/session_media 下，但那要等 agent 组装请求时
        才发生；没触发 agent 的那批群消息，页面上只能看到「(空)」。
        这里收到消息就存，返回 [{path, mime}]，路径相对 data 目录。
        """
        try:
            chain = getattr(message, "chain", None) or []
        except Exception:
            return []
        targets = []
        for el in chain:
            if isinstance(el, (Image, Sticker)):
                targets.append(el)
                continue
            # v4.20（用户 m21435）：合并转发里的图也要一起存。框架把子消息挂在
            # Forward.chains 的 MessageChain 里，只取顶层等于把整段转发的图丢掉，
            # 页面上那条「合并转发 · N 条」的弹层就只能显示「[图片]」占位。
            if _looks_forward(el):
                for item in _fw_items(el):
                    if _fw_is_chain(item):
                        subs = getattr(item, "message_list", None) or []
                    elif isinstance(item, dict):
                        subs = item.get("chain") or item.get("message_chain") or []
                    else:
                        subs = [item]
                    for one in subs:
                        if isinstance(one, (Image, Sticker)):
                            targets.append(one)
        if not targets:
            return []
        try:
            from core.utils.media_refs import store_session_media
        except Exception:
            return []
        mid = ""
        for attr in ("message_id", "msg_id", "id"):
            try:
                mid = str(getattr(message, attr, "") or "").strip()
            except Exception:
                mid = ""
            if mid:
                break
        if not mid:
            mid = "%.6f" % time.time()
        out = []
        for el in targets:
            try:
                ref = await store_session_media(el, self._raw_sids.get(sid) or sid, mid)
            except Exception:
                logger.debug("[multi-decision] 图片落盘失败（已忽略）", exc_info=True)
                ref = None
            if not (isinstance(ref, dict) and ref.get("path")):
                # v4.38（用户 m20312）：框架可能在入库那一刻就把图归档了
                # （session_media/archive/<哈希>），这时元素自带的 file 就是
                # data 下的相对路径，直接拿来用，别再让它去重新下载一次。
                for _attr in ("file", "path", "url"):
                    try:
                        _own = str(getattr(el, _attr, "") or "")
                    except Exception:
                        _own = ""
                    if _own.startswith("session_media/"):
                        ref = {"path": _own, "mime_type": str(getattr(el, "mime", "") or "")}
                        break
            if isinstance(ref, dict) and ref.get("path"):
                # v4.38（用户 m20312）：框架改按内容哈希归档，ref 里没有 mime_type，
                # 这里补一次（归档文件名没后缀，只能读文件头认）。
                _mime = str(ref.get("mime_type") or ref.get("mime") or "").strip()
                if not _mime.startswith("image/"):
                    _mime = _looks_image(str(ref.get("path") or ""))
                out.append(dict(ref, mime=_mime))
        return out

    def _mark(self, sid: str, item, mark: str) -> None:
        """给待发列表里这一条打标（计入 / 连发 / 太短…），页面逐条显示用。"""
        # 同时记到会话状态上：左栏要显示「攒数中 8/10」「太短跳过」这类
        # 实时原因，否则用户只看到「没反应」，分不清是攒数还是卡死（v2.82）
        try:
            _st = self._states.get(sid)
            if _st is not None:
                _st.last_mark = str(mark or "")
                _st.last_mark_ts = time.time()
        except Exception:
            pass
        if self.window is None or not isinstance(item, dict):
            return
        try:
            self.window.mark_pending(sid, item, mark)
        except Exception:
            pass

    def note_sent(self, event, action, result) -> None:
        """bot 的回复先挂进待发列表（v2.26）。

        框架要等下一轮 collect 才把 assistant 消息并进历史，在那之前实时窗里
        看不到「我刚说了什么」。这里提前挂一条，下一轮采集时随待发一起清空。
        """
        if self.window is None or self._closed:
            return
        try:
            if not bool(getattr(result, "ok", False)):
                return
        except Exception:
            return
        try:
            sid = getattr(event, "sid", "") or event.session.sid
        except Exception:
            sid = ""
        sid = normalize_sid(sid)
        if not sid:
            return
        eid = str(getattr(event, "event_id", "") or "")
        if ((sid, eid) in self._settled_rounds or
                (sid in self._active_rounds and self._active_rounds[sid] != eid)):
            return
        flow = self._input_flows.get(sid)
        mid = getattr(result, "message_id", None)
        stable_id = str(mid) if mid is not None else ""
        if stable_id and flow is not None and stable_id in flow.sent_ids:
            return
        try:
            text = chain_text(action)
        except Exception:
            text = ""
        text = (text or "").strip()
        if not text:
            return
        name = ""
        try:
            name = str(self.plugin.bot_name_for(sid) or "")
        except Exception:
            name = str(getattr(self.plugin, "bot_name", "") or "")
        try:
            item = self.window.push_pending(sid, "", name, text, "", time.time())
            if isinstance(item, dict) and stable_id:
                item["message_id"] = stable_id
            self.window.mark_pending(sid, item, "bot")
            if stable_id and flow is not None:
                flow.sent_ids.add(stable_id)
        except Exception:
            logger.exception("[multi-decision] 记 bot 待发消息失败（已忽略）")

    def _quota_blocked(self, st: SessionState, now: float) -> bool:
        """配额门：每小时 / 每天两个开关各自封顶（v3.52 用户 m08226 第 5 条）。

        旧版还有 count / ratio 两套玩法，用户说「只允许每小时回复次数和每天回复
        次数这 2 个玩法」，ratio 那套已经删掉（后端不再读 quota_mode /
        quota_ratio_percent）。私聊不走这里：私聊是一条一条对话，被配额卡住就成了
        「问了不回」（用户 m13250 第 4 条起私聊也走全局触发链，但配额仍然豁免）。
        """
        if str(st.sid).startswith("dm:"):
            return False
        hour_on = bool(self._c("quota_hour_enabled", st.sid))
        day_on = bool(self._c("quota_day_enabled", st.sid))
        if not (hour_on or day_on):
            return False
        st.roll_hour(now)
        st.roll_day(now)
        if hour_on and st.hour_replies >= max(1, self._c("quota_max_per_hour", st.sid)):
            return True
        if day_on and st.day_replies >= max(1, self._c("quota_max_per_day", st.sid)):
            return True
        return False

    def forget(self, sid: str) -> bool:
        """丢掉某个会话的内存状态（删会话记录时用）。"""
        sid = normalize_sid(str(sid or "").strip())
        st = self._states.pop(sid, None)
        self._active_rounds.pop(sid, None)
        self._cleared_retry_rounds.difference_update({key for key in self._cleared_retry_rounds if key[0] == sid})
        self._retry_events.pop(sid, None)
        self._retry_status(sid, self._retry_roots.pop(sid, ""), "cancelled")
        for key in list(self._round_roots):
            if key[0] == sid:
                self._round_roots.pop(key, None)
        self._cancel_retry(sid)
        for key in list(self._round_errors):
            if key[0] == sid:
                self._round_errors.pop(key, None)
        for key in list(self._settled_rounds):
            if key[0] == sid:
                self._settled_rounds.pop(key, None)
        for key in list(self._round_end_logged):
            if key[0] == sid:
                self._round_end_logged.pop(key, None)
        if st is None:
            return False
        task = getattr(st, "debounce_task", None)
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:
            pass
        self._persist_soon()
        return True

    def persist_table(self) -> dict:
        """内存里的完整会话状态表。

        删会话时 StateStore.drop 要用它：不能让 drop 拿磁盘旧表改完写回去，
        那会把别的会话还没落盘的计数一起盖掉。
        """
        return self._persist_data()

    async def handle_exception(self, event, exc) -> None:
        """ON_EXCEPTION 入口：记下错误并按次数安排自动重试（v2.71）。

        框架在 provider 全挂、工具抛异常时会发这个事件，但异常本身被事件
        总线静默吞掉 —— 插件不接这个钩子，就只会看到 busy 永远不释放。
        """
        try:
            sid = ""
            try:
                sid = getattr(event, "sid", "") or event.session.sid
            except Exception:
                sid = getattr(event, "sid", "") or ""
            sid = normalize_sid(sid)
            if not sid:
                return
            st = self._states.get(sid)
            if st is None:
                return
            eid = str(getattr(event, "event_id", "") or "")
            key = (sid, eid)
            if (sid in self._active_rounds and self._active_rounds[sid] != eid) or key in self._settled_rounds:
                return
            name = str(getattr(exc, "name", "") or "")
            msg = str(getattr(exc, "message", "") or "")
            stage = str(getattr(exc, "stage", "") or "")
            text = (name + (": " + msg if msg else "")) if name else (msg or "请求失败")
            text = text[:120]
            if key in self._round_errors:
                return  # The exception and final hooks can report the same round repeatedly.
            self._round_errors[key] = text
            self._record_retry_error(sid, eid)
            now = time.time()
            st.last_error = text
            st.last_error_at = now
            # 出错说明这轮结算不了了，先把忙标记放掉，否则永远卡在「运行中」
            st.busy = False
            st.busy_since = 0.0
            # 异常路径不会有 final，不在这里落盘的话这一轮在「最近请求」里查无此行，
            # 看起来就像「那次错误没被算进去」（用户 2026-10-24 图7）
            self._flush_error_round(sid, eid, text)
            # 异常这一轮不会有 final，round_end 得在这里补：不然「最近 24 小时」
            # 只剩成功轮的结算，成功率永远是 100%（用户 2026-10-24 第 2 条）。
            self._log_failed_round(sid, eid, st, text)
            self._retry_after_fail(sid, st)
            self._settled_rounds[key] = time.time()
            await self._settle_input(sid)
        except Exception:
            logger.exception("[multi-decision] 异常钩子处理失败（已吞掉）")

    def _flush_error_round(self, sid: str, eid: str, reason: str) -> None:
        """出错这一轮立刻落一条失败记录（保留 failed/outcome 标记）。

        请求一旦抛异常，框架不会再给这一轮 final，stage() 写下的快照就永远停在内存里；
        而「最近请求」读的是磁盘，于是整轮消失。这里只补「落盘」这一件事，
        内容仍是出错瞬间的真实请求快照，之后一个字节都不改。
        """
        if self.request_log is None or not eid:
            return
        checker = getattr(self.request_log, "has_pending", None)
        try:
            if callable(checker) and not checker(sid, eid):
                return  # 已经结算过（或压根没 stage），不重复写
        except Exception:
            pass
        try:
            self.request_log.flush(
                sid,
                replied=False,
                event_id=eid,
                extra={
                    "failed": True,
                    "outcome": "failed",
                    "error": str(reason or "请求失败")[:200],
                    "error_flush": 1,
                },
            )
        except Exception:
            logger.exception("[multi-decision] 出错轮落盘失败（已吞掉）")

    def _round_failed(self, st, final_result) -> bool:
        """这轮算不算失败：异常钩子报过错，或者结算出来的就是错误文案。"""
        err = str(getattr(st, "last_error", "") or "").strip()
        if err:
            # 上一轮残留的错误不算这轮的：错误发生在本轮开始之前就忽略
            err_at = float(getattr(st, "last_error_at", 0.0) or 0.0)
            since = float(getattr(st, "busy_since", 0.0) or 0.0)
            if since <= 0 or err_at >= since:
                return True
        for step in getattr(final_result, "step_results", None) or []:
            raw = str(getattr(step, "raw_output", "") or "")
            if raw.lstrip().startswith("[ProviderError]"):
                return True
        return False

    def _note_fail(self, sid: str, st) -> None:
        """v3.75（用户本轮第 3 条）：把「这次真失败了」记到全局，给页面横幅用。

        用户原话：「红色报错时，没有重试成功的，也需要像之前看门狗那样做个横幅」。
        错误信息原来只挂在单个会话卡的小红字上，翻到别的页签就看不见了；这里只记
        最后一次（谁最后失败的），横幅上点「我已知晓」由页面自己记，跟看门狗一样。
        """
        text = str(getattr(st, "last_error", "") or "请求失败")
        self.health["last_fail"] = {
            "ts": time.time(),
            "sid": sid,
            "text": text[:160],
            "retries": int(getattr(st, "retry_count", 0) or 0),
        }

    def _retry_after_fail(self, sid: str, st) -> None:
        """失败轮结算后按剩余次数排重试；已经排好的不重复排。"""
        if (sid, self._active_rounds.get(sid, "")) in self._cleared_retry_rounds:
            return  # Explicitly cleared pending replay must not return through a late final.
        if st.retry_at > 0:
            return  # 异常钩子已经排好了，别再排一个
        limit = int(self._c("retry_max", sid) or 0)
        delay = float(self._c("retry_delay", sid) or 0.0)
        root = self._retry_roots.get(sid, "")
        if limit <= 0 or delay <= 0:
            st.retry_at = 0.0
            self._retry_status(sid, root, "not_retried" if st.retry_count == 0 else "failed")
            self._note_fail(sid, st)
            return
        if st.retry_count >= limit:
            st.retry_at = 0.0
            self._retry_status(sid, root, "failed")
            self._note_fail(sid, st)
            logger.warning(
                f"[multi-decision] {sid} 请求失败（已重试 {st.retry_count} 次，不再重试）"
            )
            return
        st.retry_count += 1
        st.retry_at = time.time() + delay
        self._retry_status(sid, root, "scheduled")
        logger.warning(
            f"[multi-decision] {sid} 请求失败，{delay:.0f} 秒后重试（{st.retry_count}/{limit}）"
        )
        self._schedule_retry(sid, st)

    def clear_record_retries(self, sid: str) -> None:
        """Cancel old pending replay without discarding normal input or interrupting publication."""
        sid = normalize_sid(sid)
        st = self._states.get(sid)
        active = st is not None and st.busy
        if active:
            # The scheduler task can already be awaiting publication. Do not cancel it.
            task = self._retries.pop(sid, None)
            if task is not None and not task.done():
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            eid = self._active_rounds.get(sid, "")
            if eid:
                # Subsequent failure belongs to the surviving round, not erased history.
                self._round_roots[(sid, eid)] = eid
        else:
            self._cancel_retry(sid)
            eid = self._active_rounds.get(sid, "")
            if eid and (sid in self._retry_roots or (st is not None and st.retry_at > 0)):
                # An exception hook may have scheduled retry before its late final arrives.
                self._cleared_retry_rounds.add((sid, eid))
        self._retry_roots.pop(sid, None)
        self._retry_events.pop(sid, None)
        if st is not None:
            st.retry_at = 0.0
            st.retry_count = 0
            # v3.75（用户本轮第 3 条）：停止（清掉重试）之后，会话卡上的「上次出错」
            # 不该还挂着——用户原话「停止后 群组状态就应该恢复正常」。错误的证据仍在
            # 请求记录里（那一行是红色「运行出错」），这里只清实时状态。
            st.last_error = ""
            st.last_error_at = 0.0

    def _cancel_retry(self, sid: str) -> None:
        task = self._retries.pop(sid, None)
        try:
            if task is not None and not task.done():
                task.cancel()
        except Exception:
            pass

    def _schedule_retry(self, sid: str, st) -> None:
        """排一次自动重试；同一个会话只留最新那一个。"""
        old = self._retries.pop(sid, None)
        if old is not None and not old.done():
            old.cancel()
        try:
            # get_event_loop() 在 3.12+ 已弃用：没有运行中的循环时会直接抛错，
            # 而这里被 except 静默吞掉就等于「重试没排上」。用 get_running_loop()
            # 并留下一条 warning，排障时看得到。
            loop = asyncio.get_running_loop()
        except Exception:
            logger.warning(f"[multi-decision] {sid} 没有可用的运行循环，自动重试未排上")
            return
        deadline = st.retry_at
        root = self._retry_roots.get(sid, "")

        async def _run():
            try:
                await asyncio.sleep(max(0.0, deadline - time.time()))
                if self._closed or st.retry_at != deadline or self._retry_roots.get(sid, "") != root:
                    return
                st.retry_at = 0.0
                logger.warning(f"[multi-decision] {sid} 开始第 {st.retry_count} 次自动重试")
                await self._execute(sid, retry=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[multi-decision] 自动重试失败 sid={sid}")

        self._retries[sid] = loop.create_task(_run())

    async def handle_final(self, event, final_result) -> None:
        """ON_FINAL_RESULT 入口：唯一的结算点。"""
        try:
            await self._handle_final(event, final_result)
        except Exception:
            logger.exception("[multi-decision] 结算钩子异常（已吞掉）")

    async def _handle_final(self, event, final_result) -> None:
        plugin = self.plugin
        if self._closed:
            return

        sid = ""
        try:
            sid = getattr(event, "sid", "") or event.session.sid
        except Exception:
            sid = getattr(event, "sid", "") or ""
        sid = normalize_sid(sid)
        if not sid:
            return

        # 这里用 state() 而不是 get()：可能本轮是 default-chat 起的，本插件只见过
        # ON_LLM_REQUEST 没见过 ON_IM_MESSAGE，状态还没建。没状态就不裁窗口了，
        # 那是 bug。
        st = self.state(sid)
        eid = str(getattr(event, "event_id", "") or "")
        key = (sid, eid)
        tracked = sid in self._active_rounds
        if key in self._settled_rounds:
            return
        error = self._round_errors.get(key, "")
        if tracked:
            failed = bool(error) or self._round_failed(None, final_result)
        else:
            failed = self._round_failed(st, final_result)
            error = st.last_error if failed else ""
        replied = False
        bubble_count = 0
        for step in getattr(final_result, "step_results", None) or []:
            for result in getattr(step, "message_results", None) or []:
                if getattr(result, "ok", False) and not getattr(result, "is_notice", False):
                    replied = True
                    bubble_count += 1
        if failed and not error:
            error = next((str(getattr(step, "raw_output", "") or "")[:500]
                          for step in getattr(final_result, "step_results", None) or []
                          if str(getattr(step, "raw_output", "") or "").lstrip().startswith("[ProviderError]")), "请求失败")
        outcome = "failed" if failed else "replied" if replied else "no_reply"
        if self.request_log is not None:
            try:
                self.request_log.flush(sid, replied=replied, reply_bubbles=bubble_count,
                                       event_id=eid, extra={"failed": failed, "outcome": outcome, "error": error})
            except Exception:
                logger.exception("[multi-decision] 落盘请求记录失败")
        if tracked:
            self._settled_rounds[key] = time.time()
            if self._active_rounds[sid] != eid:
                logger.warning(f"[multi-decision] {sid} 忽略过期回合结算 event_id={eid}")
                return
        self.health["finals"] += 1
        # 这轮到底成功没有。provider 全挂时框架不抛异常，而是把
        # [ProviderError] 当成一条正常回复结算 —— 所以「走到 final 了」
        # 绝不等于「成功了」，无脑清零会把排好的重试直接取消掉（v2.72 修）。
        if failed:
            st.last_error = error or "请求失败"
            self._record_retry_error(sid, eid)
            self._retry_after_fail(sid, st)
            # 重试轮又失败了也要记下来，不然健康条永远停在「第一次释放在排队」
            self._note_watchdog_retry_round(sid, eid)
        else:
            if self.retry_history is not None:
                self.retry_history.finish(sid, eid, failed=False)
            self._retry_roots.pop(sid, None)
            st.last_error = ""
            st.retry_at = 0.0
            st.retry_count = 0
            self._cancel_retry(sid)
            # 重试成功：记下「回复落在这一轮」，原错误轮那边由 main.py 只读拼出来（图5）
            self._note_watchdog_retry_round(sid, eid)
            # 看门狗放掉的那轮没重试成功、由后面某轮接上的，也算补回来了
            self._note_watchdog_recovery(sid, eid, replied)

        now = time.time()
        st.busy = False
        st.busy_since = 0.0
        self._round_trigger_at.pop(sid, None)
        self._round_jitter.pop(sid, None)
        self._round_decision.pop(sid, None)
        # 这里不能再清 counter：触发的那一刻已经清过一遍了，此刻 counter 里装的是
        # 「本轮请求期间新到、这轮没覆盖到」的消息。清掉等于让它们永远不计入下一次
        # 触发阈值——群越活跃、回复越慢，实际触发比配置慢一档（审查 bug）

        if replied:
            st.roll_hour(now)
            st.hour_replies += 1
            # v3.52（用户 m08226 第 5 条）：按天配额也在这里记一笔
            st.roll_day(now)
            st.day_replies += 1
            st.total_replies += 1
            st.last_reply_ts = now
            st.reject_streak = 0
            st.backoff_until = 0.0
        elif not failed:
            # 失败轮不是「模型决定不回」，不该累加未回复退避
            st.reject_streak += 1
            start_rejects = self._c("backoff_start_rejects", sid)
            # v3.52（用户 m08226 第 6 条）：没开进阶模式就不武装退避
            if (
                self._c("advanced_enabled", sid)
                and self._c("backoff_enabled", sid)
                and st.reject_streak >= start_rejects
            ):
                extra = st.reject_streak - start_rejects
                delay = self._c("backoff_base_seconds", sid) * (2 ** max(0, extra))
                delay = min(delay, self._c("backoff_max_seconds", sid))
                st.backoff_until = now + delay
                logger.info(
                    f"[multi-decision] {sid} 连续 {st.reject_streak} 次未回复，退避 {delay}s"
                )

        # 同一个 event_id 只写一条 round_end：失败轮可能已经被异常钩子/看门狗记过了
        # （那两条路先到，final 后到），再写一条会让统计把一轮算成两轮。
        is_retry = self._is_retry_round(sid, eid)
        if not (eid and key in self._round_end_logged):
            if eid:
                self._round_end_logged[key] = time.time()
            self._write_log(
                sid, "round_end",
                extra={"replied": replied, "failed": failed,
                       "event_id": eid, "reason": st.last_trigger_reason,
                       "is_retry": is_retry},
            )

        if self.stats is not None and not failed:
            try:
                self.stats.record_round(sid, replied, now)
            except Exception:
                logger.exception("[multi-decision] 记录自适应统计失败")

        # v3.71（用户 m23596 第 5/8 条）：动态调整——这一轮落在哪一段，就按那一段的
        # 平均回复数据往目标挪一格概率（样本不足 / 冷却中 / 没设目标都由 BandTuner 挡掉）。
        if st.last_trigger_reason == "adaptive" and self.band_tune is not None and not failed:
            try:
                self.band_tune.observe(
                    sid,
                    self._band_list(sid),
                    self._adaptive_pending.get(sid),
                    replied,
                    **self._tune_kwargs(sid),
                )
            except Exception:
                logger.exception("[multi-decision] 动态调整概率失败（已忽略）")

        # Output enters the window first; only after trimming may deferred input
        # be counted/admitted one-by-one. queued is not an admission shortcut.
        await self._settle_input(sid)

    # ------------------------------------------------------------------ 判定

    def _own_names(self, sid: str = "") -> list:
        """机器人在这个会话里的名字：设置页填的「机器人昵称」+ 适配器探测到的群名片。"""
        out = []
        try:
            out.append(str(self.plugin.bot_name_for(sid) or "").strip())
        except Exception:
            pass
        try:
            out.append(str((getattr(self.plugin, "bot_names", {}) or {}).get(sid) or "").strip())
        except Exception:
            pass
        return [name for name in out if name]

    def _must_reason(self, event, text: str, sid: str = "") -> str:
        """L0 必回：叫到就回，跳过模型判断。

        用户 m00347：「必回是直接调用 回复」。三个开关各管一路（艾特 / 外号 / 昵称）。
        返回的 reason 跟直达那一路一样是 mention / nickname——_decision_gate 的白名单
        里没有这两个值，所以这一路永远不问模型、直接进回复。

        静音在更前面就拦掉了，配额在主流程里照算（用户 m00363：必回也算配额里）。
        """
        try:
            mentioned = bool(event.is_mentioned)
        except Exception:
            mentioned = False
        if mentioned and self._c("mention_always_reply", sid):
            return "mention"
        if self._c("name_wake_always_reply", sid):
            for own in self._own_names(sid):
                if own in text:
                    return "nickname"
        if self._c("nickname_wake_always_reply", sid):
            for nick in (self._c("nicknames", sid) or []):
                if nick and str(nick) in text:
                    return "nickname"
        return ""

    def _direct_reason(self, event, text: str, sid: str = "") -> str:
        """L1 直达：@ / 唤醒词 / 昵称。"""
        try:
            mentioned = bool(event.is_mentioned)
        except Exception:
            mentioned = False
        # v3.30（用户 m47796 第 4 条）：@ 拆成「艾特触发 / 艾特必回」两个开关。
        # 用户 m00347：「触发」这一路只算一次直达触发（主流程里 reason 记成 wake）。
        if mentioned and self._c("mention_trigger", sid):
            return "mention"

        # v3.11：「唤醒短语」已从设置页下线，不再参与判定（配置值仍保留在文件里，
        # 想恢复随时加回来）。
        # v3.11：机器人自己的名字（设置页「机器人昵称」+ 适配器探测到的群名片）
        # 算不算「直接叫到」由「昵称触发唤醒」控制；外号由「外号触发唤醒」控制，
        # 两个开关各管一边。
        if self._c("name_wake", sid):
            for own in self._own_names(sid):
                if own in text:
                    return "nickname"
        if self._c("nickname_wake", sid):
            for nick in (self._c("nicknames", sid) or []):
                if nick and str(nick) in text:
                    return "nickname"
        return ""

    def _should_trigger(self, st: SessionState) -> bool:
        """这条消息该不该触发（只问结果，不管口径）。"""
        return self._judge(st)[0]

    def _judge(self, st: SessionState) -> tuple:
        """判一次触发，并把「按什么口径、掷到多大点概率」一起带出来。

        返回 (是否触发, 口径, 概率)：口径取 count / probability / curve /
        adaptive，概率只在概率与曲线两种口径下有意义（其余是 0.0）。

        用户 m15045：请求页整列写着「攒够条数 · 10/3」，可那几条其实是概率模式
        掷出来的——触发调用点把 reason 硬编码成 "count"，页面就跟着撒谎。判定
        与记账从此走同一处，别再各算各的。
        """
        sid = st.sid
        mode = self._c("trigger_mode", sid)
        # v3.70（用户 m21533 第 3 条）：动态调整也不对私聊开放，私聊退回概率判定。
        if mode == "adaptive" and _is_dm_sid(sid):
            mode = "probability"

        # v3.71（用户 m23596 第 5/8 条）：动态调整 = 曲线 + 自动调每段概率。
        # 按「这一段现在的概率」每条独立掷骰；这个概率由 BandTuner 按每段的平均
        # 回复数据慢慢逼近目标（state/band_tune.py），不再用全局阈值。
        if mode == "adaptive" and self._bands_on(sid):
            info = self._band_info(sid)
            if info is None:
                return False, "adaptive", 0.0
            # v3.84（用户 m04084 第 3 条）：先给调参一次机会，再取「这段现在该用多少概率」，
            # 这样刚调出来的值这一条就生效，不用等下一轮。
            self._tune_tick(sid, info)
            probability = self._band_prob_now(sid)
            # 记下这一轮落在哪一段，结算时按它调这一段
            self._adaptive_pending[sid] = (info[0], info[1])
            if probability <= 0:
                return False, "adaptive", 0.0
            if probability >= 100:
                return True, "adaptive", 100.0
            # v3.79（用户 m02095）：不再每条独立掷骰，改由节奏器排队
            # （state/band_rhythm.py）。平均间隔仍是 100/p 条，所以 BandTuner
            # 那本账（M*p 次触发）不变；差别在于不再出现「连着 30 条不来」和
            # 「刚回完又回」，也不会像兜底那样把触发数顶到设定值之上。
            # v3.88（用户 m05012）：排队方式从「抽一个数死等」换成「每条一个递增的
            # 命中概率」，最长只等 1.5 倍（p=10% 时 15 条）。
            # v3.81（用户 m02645 图2）：节奏器升格成全模式共用的「节奏模式」，
            # 动态调整是强制开，所以这里 force=True，不看 prob_pseudo。
            rolled = self._rhythm_roll(sid, info[0], info[1], probability, force=True)
            if rolled is not None:
                return (
                    bool(rolled.get("hit")),
                    "adaptive",
                    self._rolled_prob(rolled, probability),
                )
            return random.uniform(0, 100) < probability, "adaptive", float(probability)

        # v3.33（用户 m51068 第 8/9 条）：曲线开着就由曲线接管——按当前时段的
        # 概率每条独立掷骰，普通模式的阈值/概率与自适应都不再参与。
        # v3.71（用户 m23596 第 8 条）：伪随机兜底只归概率模式，曲线不再吃它
        # （「通用设置里的概率兜底太粗糙」，用户要它只管概率模式）。
        if self._bands_on(sid):
            probability = self._band_prob_now(sid)
            if probability <= 0:
                return False, "curve", float(probability)
            if probability >= 100:
                return True, "curve", float(probability)
            # v3.81（用户 m02645 图2）：节奏模式接管全部分支——曲线模式不再「抬概率」，
            # 而是按这一段自己的节奏排队（每段一本账）；关掉节奏模式就是每条独立掷骰。
            hit = self._band_info(sid)
            seg = (hit[0], hit[1]) if hit else (0, 48)
            rolled = self._rhythm_roll(sid, seg[0], seg[1], probability)
            if rolled is not None:
                return bool(rolled.get("hit")), "curve", self._rolled_prob(rolled, probability)
            return random.uniform(0, 100) < probability, "curve", float(probability)
        # v3.71（用户 m23596 第 5 条）：adaptive 分支已上移到曲线之前、按段概率掷骰。
        # 走到这里的 adaptive 只剩「曲线没开」——没有曲线就没有每段目标，按概率模式兜底。
        if mode == "adaptive":
            mode = "probability"
        if mode == "probability":
            probability = max(0, min(100, self._c("trigger_probability", sid)))
            # v3.81（用户 m02645 图2）：v3.30 那套「连着没中就抬概率」的兜底换成节奏器
            # （_rhythm_roll）——抬多少都是拍脑袋，实测触发率比设定高四成；节奏器按
            # 100/p 的正常间隔排队，长期平均刚好对上设定值。
            rolled = self._rhythm_roll(sid, 0, 48, probability)
            if rolled is not None:
                return bool(rolled.get("hit")), "probability", self._rolled_prob(rolled, probability)
            return random.uniform(0, 100) < probability, "probability", float(probability)
        return st.counter >= max(1, self._c("trigger_threshold", sid)), "count", 0.0

    # ------------------------------------------------------- 时段曲线 / 短语（v3.33）

    def _bands_on(self, sid: str = "") -> bool:
        """时段曲线是否接管判定。开着但曲线为空时视为没开（否则一天都没概率）。"""
        # v3.70（用户 m21533 第 3 条）：时段曲线不对私聊开放。
        if _is_dm_sid(sid):
            return False
        if not self._c("bands_enabled", sid):
            return False
        return bool(self._band_list(sid))

    def _band_list(self, sid: str = "", now: Optional[float] = None) -> list:
        try:
            raw = self._c("time_bands", sid)
        except Exception:
            return []
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw.strip() else []
            except Exception:
                return []
        else:
            data = raw
        # v3.81（用户 m02645 图3）：目标值可以按星期分（trw / tpw = 周一..周日），
        # 取哪一天取决于「现在」——判定与调参都得用同一个星期，所以把 now 也算进来。
        weekday = time.localtime(time.time() if now is None else now).tm_wday
        out = []
        for it in data or []:
            try:
                start = int(it.get("s"))
                end = int(it.get("e"))
                value = int(round(float(it.get("v"))))
            except (TypeError, ValueError, AttributeError):
                continue
            if end <= start:
                continue
            # v3.71（用户 m23596 第 5/8 条）：每段除了概率还带两个目标——
            # tr = 目标回复次数/天、tp = 目标回复概率（每条消息被回的概率 %）。
            # 动态调整按这两个目标自动改这一段的概率（见 state/band_tune.py）。
            out.append(
                (
                    max(0, min(48, start)),
                    max(0, min(48, end)),
                    max(0, min(100, value)),
                    _band_day_target(it.get("trw"), weekday, 100000)
                    or _band_target(it.get("tr"), 100000),
                    _band_day_target(it.get("tpw"), weekday, 100.0)
                    or _band_target(it.get("tp"), 100.0),
                )
            )
        out.sort()
        return out

    def _band_at(self, sid: str = "", now: Optional[float] = None):
        """当前时刻落在哪一段，返回那一段的触发概率（%）；没落进任何段返回 None。"""
        bands = self._band_list(sid, now)
        if not bands:
            return None
        local = time.localtime(time.time() if now is None else now)
        slot = local.tm_hour * 2 + (1 if local.tm_min >= 30 else 0)
        for start, end, value, _tr, _tp in bands:
            if start <= slot < end:
                return value
        return None

    def _band_info(self, sid: str = "", now: Optional[float] = None):
        """当前时刻落在哪一段，返回 (槽位起, 槽位止, 配置概率, 目标回复次数/天, 目标回复概率%)。

        v3.71（用户 m23596 第 5/8 条）：动态调整要知道「这一段的配置概率与目标」，
        只回一个概率不够用了，另开一个函数，_band_at 的对外口径保持不变。
        """
        bands = self._band_list(sid, now)
        if not bands:
            return None
        local = time.localtime(time.time() if now is None else now)
        slot = local.tm_hour * 2 + (1 if local.tm_min >= 30 else 0)
        for it in bands:
            if it[0] <= slot < it[1]:
                return it
        return None

    def _tune_kwargs(self, sid: str = "") -> dict:
        """调参这一套参数（结算时与判定时共用，别写两份）。

        v3.72（用户 m24260 第 9 条）：回看改成「周」，最少 4 周、默认 8 周；第 6 条：
        不再有「最少观察天数」，有数据就调。
        v3.84（用户 m04084 第 3 条）：调参间隔改成可配（adaptive_tune_minutes）。
        """
        try:
            gap = max(1, int(self._c("adaptive_tune_minutes", sid) or 5))
        except (TypeError, ValueError):
            gap = 5
        return {
            "step": self._c("adaptive_step", sid),
            "weekday_bucket": bool(self._c("adaptive_weekday_bucket", sid)),
            "weekday_window_days": max(4, int(self._c("adaptive_weekday_window_weeks", sid) or 8)) * 7,
            "week_days": max(7, int(self._c("stats_window_days", sid) or 7)),
            "cooldown": float(gap) * 60.0,
        }

    def _tune_weekday(self, sid: str = ""):
        """开了「使用星期数据」就返回今天周几（周一=0），否则 None。

        v3.84（用户 m04084 第 1 条）：调参结果按星期分开存，读的时候要按同一个桶找。
        """
        try:
            if not bool(self._c("adaptive_weekday_bucket", sid)):
                return None
            return int(time.localtime().tm_wday)
        except Exception:
            return None

    def _tune_tick(self, sid: str, info) -> None:
        """v3.84（用户 m04084 第 3 条）：判定的时候就给调参一次机会。

        以前只在「这一轮触发了而且结算了」才调：概率低的时候可能几十分钟才排上一次，
        段概率就迟迟上不来——越不触发越不调，越不调越不触发。现在每条消息判定时也试
        一次，间隔由 BandTuner 的 cooldown（调参间隔）挡；这里再加一层 60 秒的内存节流，
        免得没设目标的段每次都白扫一遍日志。
        """
        if self.band_tune is None or info is None:
            return
        try:
            now = time.time()
            if now - float(self._tune_try_at.get(sid) or 0.0) < 60.0:
                return
            self._tune_try_at[sid] = now
            self.band_tune.observe(
                sid, self._band_list(sid), (info[0], info[1]), False,
                **self._tune_kwargs(sid),
            )
        except Exception:
            logger.exception("[multi-decision] 判定时调参失败（已忽略）")

    def _band_prob_now(self, sid: str = "", now: Optional[float] = None) -> float:
        """当前时刻落在曲线哪一段，返回那一段的触发概率（%）。

        动态调整模式下返回 BandTuner 调过的那份概率（没调过就用配置值）。
        """
        hit = self._band_info(sid, now)
        if hit is None:
            return 0.0
        if self._c("trigger_mode", sid) == "adaptive" and self.band_tune is not None:
            try:
                tuned = self.band_tune.prob(sid, hit[0], hit[1], v=hit[2],
                                           weekday=self._tune_weekday(sid))
                if tuned is not None:
                    return float(tuned)
            except Exception:
                logger.exception("[multi-decision] 读动态调整后的概率失败")
        return float(hit[2])

    def _phrase_hit(self, text: str, sid: str = "") -> bool:
        """整条消息就是清单里的某个短语 → 不算一次触发。

        清单 = 全局池 + 本群独立池（用户 m02693 第 4 条）：单群覆盖不再
        整份替换全局，只在全局后面追加。合并逻辑在 plugin._phrase_effective_items
        里，这里只管比对。
        """
        if not self._c("phrase_filter_enabled", sid):
            return False
        items = None
        try:
            pick = getattr(self.plugin, "_phrase_effective_items", None)
            if callable(pick):
                items = list(pick(sid) or [])
        except Exception:
            items = None
        if items is None:
            # 老路径：plugin 不支持合并时（比如单测里的桩），照旧读单群有效值
            try:
                raw = self._c("phrase_filter_list", sid)
                data = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except Exception:
                return False
            items = list(data or [])
        norm = _norm_phrase(text)
        if not norm:
            return False
        for it in items:
            phrase = _norm_phrase(str(it))
            if phrase and norm == phrase:
                return True
        return False

    def _rhythm_sigma(self, sid: str) -> float:
        """节奏松紧：0 = 间隔恒定，65 = 正常，120 = 很随意（state/band_rhythm.py 的 sigma）。"""
        raw = self._c("adaptive_rhythm_sigma", sid)
        # 注意别用 `or 65`：0 是合法值（间隔恒定），falsy 兜底会让它永远吃不到。
        try:
            return 0.65 if raw in (None, "") else float(raw) / 100.0
        except (TypeError, ValueError):
            return 0.65

    def _rhythm_on(self, sid: str) -> bool:
        """节奏模式开没开（prob_pseudo）。动态调整不看它，那是强制开。"""
        return bool(self._c("prob_pseudo", sid))

    def _rhythm_roll(self, sid: str, start: int, end: int, probability: float, force: bool = False):
        """节奏模式：按节奏器掷一次（state/band_rhythm.py）。

        v3.81（用户 m02645 图2）：换掉了 v3.30 的「伪随机兜底」——那套是连续没中就
        把概率往上抬，抬多少全是拍脑袋，实测触发率比设定高四成，动态调整那边追目标
        也追不准。所以概率模式 / 曲线模式 / 动态调整三种模式共用同一套（动态调整
        force=True）。

        v3.88（用户 m05012）：节奏器不再是「抽一个数、前几条概率全 0 地死等」，而是
        每条给一个递增的命中概率，最长只等 1.5 倍（p=10% 时 15 条）。长期平均仍是
        100/p 条，调参器那本账照旧。

        返回 None = 这次不用节奏器（关着，或节奏器不可用），调用方退回每条独立掷骰。
        """
        if not force and not self._rhythm_on(sid):
            return None
        if self.band_rhythm is None:
            return None
        try:
            return self.band_rhythm.roll(sid, start, end, probability, sigma=self._rhythm_sigma(sid))
        except Exception:
            logger.exception("[multi-decision] 节奏器判定失败，退回独立掷骰")
            return None

    @staticmethod
    def _rolled_prob(rolled, probability: float) -> float:
        """节奏器给的机会值（只作显示 / 记日志用）。

        v3.88：chance 可以是 0——松紧 0 时前 N-1 条就是不可能会中。原来写成
        float(rolled.get("chance") or probability)，0 被当成没给，页面于是显示
        设定的 10%，看着像「这一条有 10% 机会」其实根本没有。
        """
        try:
            chance = rolled.get("chance")
        except AttributeError:
            return float(probability)
        if chance is None:
            return float(probability)
        try:
            return float(chance)
        except (TypeError, ValueError):
            return float(probability)

    def _rest_ranges(self, sid: str = "") -> list:
        """曲线模式下被标成休息的段：[(起点槽位, 终点槽位), ...]，可以有好几段。

        v3.73（用户 m24640 第 5 条）：休息时段回到第一版的做法——标记直接写在
        time_bands 的段对象上（rest: true），在曲线上点那一段右上角的月亮开关。
        所以曲线模式不再读 band_rest_start / band_rest_end 那两个时间点。
        """
        try:
            raw = self._c("time_bands", sid)
        except Exception:
            return []
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw.strip() else []
            except Exception:
                return []
        else:
            data = raw
        out = []
        for it in data or []:
            try:
                if not it.get("rest"):
                    continue
                start = int(it.get("s"))
                end = int(it.get("e"))
            except (TypeError, ValueError, AttributeError):
                continue
            start = max(0, min(48, start))
            end = max(0, min(48, end))
            if end <= start:
                continue
            out.append((start, end))
        out.sort()
        return out

    def _rest_cfg(self, sid: str = "") -> tuple:
        """当前模式该用哪一套休息时段：(开关, 开始, 结束, 概率)。

        v3.72（用户 m24260 第 2 条）：普通模式用 rest_*，曲线模式（含动态调整）另算。
        v3.73（用户 m24640 第 5 条）：曲线模式改成看段上的 rest 标记；这里给出的
        开始 / 结束只是第一段的显示值，判定请走 _in_rest_window（支持多段）。
        """
        if self._bands_on(sid):
            ranges = self._rest_ranges(sid)
            prob = self._c("band_rest_probability", sid)
            if not ranges:
                return (False, "", "", prob)
            return (True, _slot_hhmm(ranges[0][0]), _slot_hhmm(ranges[0][1]), prob)
        return (bool(self._c("rest_mode_enabled", sid)),
                self._c("rest_start", sid),
                self._c("rest_end", sid),
                self._c("rest_probability", sid))

    def _rest_probability(self, sid: str = "") -> float:
        """休息时段用的回复概率（当前模式那一套）。"""
        try:
            return max(0, min(100, float(self._rest_cfg(sid)[3])))
        except (TypeError, ValueError):
            return 0.0

    def _in_window(self, start_s, end_s, now: float) -> bool:
        """now 是否落在 start→end 的连续窗口里（可跨零点，两端相同视为没有）。"""
        try:
            start_h, start_m = [int(x) for x in str(start_s).split(":")]
            end_h, end_m = [int(x) for x in str(end_s).split(":")]
        except (TypeError, ValueError):
            return False
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            return False
        local = time.localtime(now)
        current = local.tm_hour * 60 + local.tm_min
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _in_rest_window(self, now: float, sid: str = "") -> bool:
        """现在算不算休息时段（按当前模式挑普通模式 / 曲线模式那一套配置）。

        v3.73（用户 m24640 第 5 条）：曲线模式的休息时段是段标记，可能有好几段，
        按半小时格逐段比对；普通模式仍是一个连续窗口（可跨零点）。
        """
        if self._bands_on(sid):
            ranges = self._rest_ranges(sid)
            if not ranges:
                return False
            local = time.localtime(now)
            slot = local.tm_hour * 2 + (1 if local.tm_min >= 30 else 0)
            for start, end in ranges:
                if start <= slot < end:
                    return True
            return False
        on, start_s, end_s, _prob = self._rest_cfg(sid)
        if not on:
            return False
        return self._in_window(start_s, end_s, now)

    # ------------------------------------------------------------------ 静默

    def silent_eta(self, sid: str) -> dict:
        """静默 / 休兜 下一次会自动冒泡的绝对时刻（给左栏标签倒计时用）。

        返回 {kind: "silent"|"rest", at: epoch}；没有压着没消化的消息、会话正忙、
        或者对应兜底没开时返回空 dict（前端就不显示倒计时）。判定口径与
        _check_silent 保持一致，否则标签上的秒数会跟实际触发对不上。
        """
        st = self._states.get(sid)
        if st is None:
            return {}
        now = time.time()
        if not st.is_idle() or st.counter <= 0:
            return {}
        in_rest = self._in_rest_window(now, sid)
        if in_rest:
            if not self._c("rest_silent_enabled", sid):
                st.silent_armed_ts = 0.0
                return {}
            kind = "rest"
            need = max(1, int(self._c("rest_silent_minutes", sid) or 3)) * 60
        else:
            if not self._c("silent_trigger_enabled", sid):
                st.silent_armed_ts = 0.0
                return {}
            kind = "silent"
            # v4.28：静默分钟数也按群覆盖——以前这里读全局，群里单独调的
            # 分钟数只在设置页好看，实际还按全局走，倒计时和触发对不上。
            need = max(1, int(self._c("silent_trigger_minutes", sid) or 3)) * 60
        # v4.28（用户 m22969）：开关打开的那一刻重新起算。以前只按「上次消息 /
        # 上次触发」算，群已经安静很久的会话一开开关，倒计时立刻是 0 秒并马上
        # 冒泡，看着像没在倒数。打开开关时把这一刻钉进基准，就能看到完整倒数。
        if not st.silent_armed_ts:
            st.silent_armed_ts = now
        last = max(st.last_msg_ts, st.last_trigger_ts, st.last_silent_ts,
                   st.silent_armed_ts)
        return {"kind": kind, "at": float(last + need)}

    def start_silent_loop(self) -> None:
        """静默触发：长时间没人说话、buffer 里又攒着消息时，主动冒一次泡。"""
        plugin = self.plugin
        if not plugin.enabled or not plugin.decision_enabled:
            return
        if not (plugin.silent_trigger_enabled or plugin.rest_silent_enabled):
            return
        task = asyncio.create_task(self._silent_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info(
            f"[multi-decision] 静默兜底已开启：群安静 "
            f"{plugin.silent_trigger_minutes} 分钟后，把还压着没消化的消息补处理一次"
            f"（休息时段{'另有 ' + str(plugin.rest_silent_minutes) + ' 分钟兜底' if plugin.rest_silent_enabled else '不兜底'}）"
        )

    async def _silent_loop(self) -> None:
        interval = 30.0
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                try:
                    self._check_silent()
                except Exception:
                    logger.exception("[multi-decision] 静默检查异常")
        except asyncio.CancelledError:
            raise

    def _check_silent(self) -> None:
        plugin = self.plugin
        if not (plugin.silent_trigger_enabled or plugin.rest_silent_enabled):
            return
        now = time.time()
        for sid, st in list(self._states.items()):
            if not st.is_idle():
                continue
            # v3.31（用户 m48323 第 7 条）：静默兜底只补「还压着没消化的消息」，
            # counter 归零后不再重复冒泡——一次安静只冒一次，不是持续效果。
            if st.counter <= 0:
                continue
            # v3.33（用户 m51068 第 10 条）：休息时段单独一套兜底（默认不开）。
            # v3.39（用户本轮）：去掉「曲线没开」这个前提——曲线接管的是回复概率，
            # 不接管兜底窗口。曲线开着又落在设定时段里，仍按休息兜底补一次；
            # 其余时段照旧走普通静默兜底，没开曲线时行为完全不变。
            # v3.72（用户 m24260 第 2 条）：开关与时间点由 _in_rest_window 按模式挑
            in_rest = self._in_rest_window(now, sid)
            if in_rest:
                if not self._c("rest_silent_enabled", sid):
                    st.silent_armed_ts = 0.0
                    continue
                need = max(1, int(self._c("rest_silent_minutes", sid) or 3)) * 60
            else:
                if not self._c("silent_trigger_enabled", sid):
                    st.silent_armed_ts = 0.0
                    continue
                need = max(1, int(self._c("silent_trigger_minutes", sid) or 3)) * 60
            # v4.28（用户 m22969）：与 silent_eta 同一口径——开关刚打开就从这一刻
            # 重新数，不让「群已经安静很久」直接跳成 0 秒。
            if not st.silent_armed_ts:
                st.silent_armed_ts = now
                continue
            last = max(st.last_msg_ts, st.last_trigger_ts, st.last_silent_ts,
                       st.silent_armed_ts)
            if now - last < need:
                continue
            st.last_silent_ts = now
            # 用户 m11091 第 1 条：打勾。两套兜底分开记，前端按 kind 亮对应的勾。
            if in_rest:
                st.rest_fired_ts = now
            else:
                st.silent_fired_ts = now
            logger.info(
                f"[multi-decision] {sid} {'休息' if in_rest else '静默'} "
                f"{int((now - last) / 60)} 分钟，主动冒泡"
            )
            self._trigger(sid, "silent", st)

    # ------------------------------------------------------------------ 裁切缓冲

    def start_buffer_loop(self) -> None:
        """裁切缓冲巡检（v3.52 用户 m08226 第 7 条）。

        概率设得很低的群可能几十条消息都不触发请求，只按触发阈值走窗口会一直
        变长：又占 token 又没必要。这里 30 秒看一次，user 条数攒到「阈值 + 缓冲」
        （默认 60+10=70 条）就交给 window.maybe_trim 强制裁掉，不看最小裁切条数。
        用户 m11091 第 4/6 条：原来按含 bot 的总条数比 keep+buffer，四十来条就裁。
        """
        plugin = self.plugin
        if not plugin.enabled or not plugin.decision_enabled:
            return
        if plugin.window is None:
            return
        task = asyncio.create_task(self._buffer_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _buffer_loop(self) -> None:
        try:
            ticks = 0
            while not self._closed:
                await asyncio.sleep(30.0)
                try:
                    self._check_buffer_trim()
                except Exception:
                    logger.exception("[multi-decision] 缓冲裁切巡检异常")
                # 用户 m14457 第 3 条：图片缓存以前只在「写一张新图」那一刻顺手裁
                # （window/manager.py 里只有 _store_cache / _cache_media_file 两个调用点），
                # 所以停机、没新消息的时候一张都不会清，天数线也就永远等不到执行——
                # 表现就是「停止工作期间图片清理没触发」。这里每 10 分钟自己巡检一次，
                # 规则跟写图时完全同一份（_prune_media_cache，按群组各自算）。
                ticks += 1
                if ticks % 20 == 0:
                    try:
                        await asyncio.to_thread(self._prune_media_cache_now)
                    except Exception:
                        logger.exception("[multi-decision] 图片缓存巡检异常")
        except asyncio.CancelledError:
            raise

    def _prune_media_cache_now(self) -> None:
        """按规则裁一次图片缓存（跟写图时同一个函数，不额外定标准）。"""
        try:
            from ..window import manager as _wm
            root = _wm._media_cache_root()
            if root:
                _wm._prune_media_cache(root)
        except Exception:
            logger.debug("[multi-decision] 图片缓存巡检失败", exc_info=True)

    def _check_buffer_trim(self) -> None:
        plugin = self.plugin
        if plugin.window is None:
            return
        for sid, st in list(self._states.items()):
            # 忙的时候别动窗口：这一轮请求可能正拿着它
            if not st.is_idle():
                continue
            try:
                plugin.window.maybe_trim(sid, trigger="buffer")
            except Exception:
                logger.exception(f"[multi-decision] {sid} 缓冲裁切失败（已忽略）")

    # ------------------------------------------------------------------ 触发链

    def _trigger(self, sid: str, reason: str, st: SessionState, prob: float = 0.0) -> None:
        plugin = self.plugin
        now = time.time()
        self.health["triggers"] += 1
        triggered_at = st.counter
        st.last_trigger_count = triggered_at
        # 用户 m15045：概率/曲线模式要知道这轮是掷到多大点概率才中的；
        # 计数与自适应没有「概率」这回事，一律记 0。
        st.last_trigger_prob = float(prob or 0.0)
        st.counter = 0
        st.total_triggers += 1
        st.last_trigger_ts = now
        st.trigger_wall = now
        st.last_trigger_reason = reason
        # 用户 m05124 第 5 条：触发这一刻本轮就算「已经开跑」，_settle_input 收尾才放开。
        # 以前只认 busy / phase，抖动的这几秒里进来的消息会被正常接纳进「马上要发出去的那一轮」；
        # 存时间戳而不是布尔，是怕某条路径没走到收尾把会话永久卡死（超 600 秒自然失效）。
        st.round_claimed = now

        # v3.44（用户本轮第 1 条）：抖动改成「开关 + 固定秒数」。
        # 开关默认关着——关着就立刻动，不再等；开了就固定等 jitter_seconds 秒
        # （以前是 random.uniform(0, N) 的随机等待，用户明确要固定秒数）。
        # 用户 m05124 第 5 条：必回（@ / 叫名字）这一路默认不等抖动——都被点名了还干等几秒，
        # 用户看到的就是「@ 完一秒多才动」。抖动只服务计数 / 概率那一路「攒一会儿再决定」。
        # 用户 m13212 后：这条改成设置项「必回不等防抖」，默认开；关掉就一起等。
        must_go = str(reason or "") in ("mention", "nickname")
        _skip_raw = self._c("jitter_skip_must_go", sid)
        skip_must = True if _skip_raw is None else bool(_skip_raw)
        jitter_on = bool(self._c("jitter_enabled", sid)) and not (must_go and skip_must)
        jitter = max(0, self._c("jitter_seconds", sid)) if jitter_on else 0
        # 用户 m05427 / m05583：抖动是「固定 S 秒；这 S 秒里又攒够一次触发，就再叠 S 秒」（7 → 14 → 21 …）。
        # 以前每来一次触发都把 deadline 推回「现在 + S」，看起来永远只等 S 秒，用户要的是叠加。
        # 用户 m05691 第 3 条：叠几跳改成配置项「防抖上限轮次」，默认 3 次（7+7+7）——到顶就不再往后拖。
        try:
            rounds = int(self._c("jitter_max_rounds", sid))
        except Exception:
            rounds = 3
        rounds = max(1, min(20, rounds))
        if not jitter_on:
            st.debounce_first = now
            st.debounce_jitter = 0.0
            st.debounce_spent = 0.0
            st.debounce_deadline = now
        else:
            if st.debounce_first <= 0:
                st.debounce_first = now
                st.debounce_jitter = 0.0
            st.debounce_jitter = min(st.debounce_jitter + jitter, jitter * rounds)
            st.debounce_deadline = st.debounce_first + st.debounce_jitter
        jitter_text = f"{int(jitter)}s" if jitter_on else ("必回不等" if must_go else "关")

        task = st.debounce_task
        if task is None or task.done():
            task = asyncio.create_task(self._debounce_loop(sid))
            st.debounce_task = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            logger.info(
                f"[multi-decision] {sid} 触发 reason={reason} "
                f"抖动 {jitter_text} 计数清零"
            )
        else:
            logger.debug(f"[multi-decision] {sid} 抖动计时器重置 reason={reason}")

        # 触发本身也要留痕：靠这条看出「这轮是被什么点起来的」（统计只读 reason）
        self._write_log(
            sid, "trigger",
            extra={"reason": reason, "message_logged": True},
        )

    async def _debounce_loop(self, sid: str) -> None:
        """等安静 S 秒；期间再有触发会推迟 deadline，最终只跑一轮。

        注意 ``_execute`` 内部是 await，这期间可能又有消息触发、把 deadline
        重新设成未来时刻。所以执行完必须回头再看一眼 deadline —— 只看一次就
        return 会留下一个没人消费的 deadline，而 is_idle() 判的是
        ``debounce_deadline <= 0``，从此恒为 False，窗口再也不裁、静默也不触发。
        """
        try:
            while not self._closed:
                st = self._states.get(sid)
                if st is None:
                    return
                delay = st.debounce_deadline - time.time()
                if delay > 0:
                    await asyncio.sleep(min(delay, 1.0))
                    continue
                st.debounce_deadline = 0.0
                # 用户 m05453：抖动到这一刻就结束了，把「真的等了几秒」量下来。
                # 后面排队与准备的时间不算抖动（那是决策 / 回复那两段的事）。
                if st.debounce_first > 0:
                    st.debounce_spent = max(0.0, time.time() - st.debounce_first)
                await self._execute(sid)
                if self._closed:
                    return
                st = self._states.get(sid)
                if st is None:
                    return
                if st.debounce_deadline <= 0:
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[multi-decision] 抖动任务异常 sid={sid}")

    def round_trigger_at(self, sid: str) -> float:
        """取走这一轮的触发墙钟（没有就返回 0）。

        v3.51（用户 m07385 第 7 条）：一次请求只消费一次 —— 同一轮里若因为工具调用
        又起了第二次 LLM 请求，第二次拿不到触发时刻，就退回按它自己的开始时间算，
        免得把前面那次请求的时间重复算进「触发 → 结束」。
        """
        try:
            return float(self._round_trigger_at.pop(normalize_sid(sid), 0.0) or 0.0)
        except Exception:
            return 0.0

    def round_jitter_at(self, sid: str) -> float:
        """取走这一轮抖动实际等了几秒（用户 m05453：记录页「防抖」那一格只放这个）。"""
        try:
            return float(self._round_jitter.pop(normalize_sid(sid), 0.0) or 0.0)
        except Exception:
            return 0.0

    def round_decision_at(self, sid: str) -> float:
        """取走这一轮决策花了多久（判要回才有，回复记录里单独写一段）。"""
        try:
            return float(self._round_decision.pop(normalize_sid(sid), 0.0) or 0.0)
        except Exception:
            return 0.0

    def _retry_status(self, sid: str, root: str, status: str) -> None:
        if self.retry_history is not None and root:
            self.retry_history.status(sid, root, status)

    def watchdog_retry_status(self) -> Optional[dict]:
        """最近一次看门狗强制释放那一轮，后来到底补回来没有（健康条用）。

        attempts：这一轮**真正投出去过**的重试次数（每投递一次 +1，不是「最多重试几次」）；
        succeeded：最后一次重试是否结算成功；True 也可能是「看门狗放掉之后由后面某一轮
        接续答上的」，那时 recovered_by 写明是哪一轮 event_id；
        None：还没有结论（重试排着队 / 正跑着，或链被新消息撤掉且还没人接上）。
        """
        wd = self._watchdog_last or {}
        if not wd:
            return None
        sid = str(wd.get("sid") or "")
        root = str(wd.get("root_event_id") or "")
        attempts = max(0, int(wd.get("attempts") or 0))
        status = "unknown"
        succeeded = None
        if self.retry_history is not None and root:
            try:
                summary = self.retry_history.summary(sid, root)
            except Exception:
                summary = None
            if summary:
                attempts = max(0, int(summary.get("attempts") or 0))
                status = str(summary.get("status") or "unknown")
                succeeded = summary.get("succeeded")
        recovered_by = str(wd.get("recovered_by") or "")
        if succeeded is None and recovered_by:
            succeeded = True
        outcome = "success" if succeeded is True else "failed" if succeeded is False else "pending"
        ts = float(wd.get("ts") or 0.0)
        return {
            "sid": sid,
            "root_event_id": root,
            "ts": ts,
            "ago": int(time.time() - ts) if ts > 0 else -1,
            "attempts": attempts,
            "status": status,
            "succeeded": succeeded,
            "outcome": outcome,
            "retry_event_id": str(wd.get("retry_event_id") or ""),
            "recovered_by": recovered_by,
            "recovered_ts": float(wd.get("recovered_ts") or 0.0),
        }

    def _note_watchdog_retry_round(self, sid: str, eid: str) -> None:
        """记一下这一轮是不是「看门狗放掉那一轮」的重试轮（健康条要指着它看结果）。

        请求记录一律不改：重试轮自己落自己那份记录，次数与成败由 retry_history 持久化，
        展示时在 main.py 里只读拼接出 retry_outcome（用户 2026-10-24 图4/图5）。
        """
        wd = self._watchdog_last or {}
        if not wd or wd.get("sid") != sid:
            return
        root = self._round_roots.get((sid, eid), eid)
        if root and root == str(wd.get("root_event_id") or "") and eid != root:
            wd["retry_event_id"] = eid

    def _note_watchdog_recovery(self, sid: str, eid: str, replied: bool) -> None:
        """看门狗那一轮没靠重试补回来时，后面某轮把话接上了也算它成功。

        线上 16:21 那轮被放掉，重试排上以后又被新消息撤掉（链状态 cancelled），
        16:26 新的一轮正常回了。健康条要是只认 retry_history，就一直显示未知。
        """
        if not replied:
            return
        wd = self._watchdog_last or {}
        if not wd or not wd.get("root_event_id") or wd.get("recovered_by"):
            return
        if wd.get("sid") != sid or str(wd.get("root_event_id")) == eid:
            return
        if self.retry_history is not None:
            try:
                summary = self.retry_history.summary(sid, str(wd.get("root_event_id")))
            except Exception:
                summary = None
            if summary and summary.get("succeeded") is True:
                return  # 重试轮自己补回来了，不用记接续
        ts = float(wd.get("ts") or 0.0)
        if ts > 0 and (time.time() - ts) > WATCHDOG_RECOVER_SECONDS:
            return  # 隔太久的回复跟那一轮无关
        wd["recovered_by"] = eid
        wd["recovered_ts"] = time.time()

    def _record_retry_error(self, sid: str, eid: str) -> None:
        if (sid, eid) in self._cleared_retry_rounds:
            return
        root = self._round_roots.get((sid, eid), eid)
        if root and eid:
            self._retry_roots[sid] = root
            if self.retry_history is not None:
                self.retry_history.error(sid, root, eid)

    def _bind_retry_round(self, sid: str, eid: str, event) -> None:
        # The host passes replay.message objects into batch.messages verbatim.
        # Event attributes are NOT propagated by publish_buffered_messages.
        messages = list(getattr(event, "messages", None) or [])
        if not messages and getattr(event, "message", None) is not None:
            messages = [event.message]
        links = []
        for msg in messages:
            extra = getattr(msg, "extra", None)
            links.append(extra.get(RETRY_META) if isinstance(extra, dict) else None)
        link = links[0] if links else None
        linked = False
        if (isinstance(link, dict) and all(x == link for x in links)
                and link.get("sid") == sid and self.retry_history is not None
                and self._retry_roots.get(sid) == link.get("root_event_id")):
            linked = self.retry_history.bind(sid, link.get("root_event_id"),
                                               link.get("attempt_token"), eid)
        if linked:
            root = link["root_event_id"]
        else:
            # A fresh request is never retrospectively claimed by a previous error.
            old_root = self._retry_roots.pop(sid, "")
            self._retry_status(sid, old_root, "cancelled")
            st = self.state(sid)
            st.retry_at = 0.0
            st.retry_count = 0
            self._cancel_retry(sid)
            root = eid
        if eid:
            self._round_roots[(sid, eid)] = root

    @staticmethod
    def _tag_retry_events(sid: str, root: str, token: str, events: list) -> list:
        replays = []
        for event in events:
            replay = copy.copy(event)
            replay.message = copy.copy(event.message)
            replay.message.extra = dict(getattr(event.message, "extra", None) or {})
            replay.message.extra[RETRY_META] = {"sid": sid, "root_event_id": root,
                                                 "attempt_token": token}
            replays.append(replay)
        return replays

    def _capture_retry_events(self, sid: str, event) -> None:
        """Freeze the actual failed batch, not the latest arriving chat message."""
        messages = list(getattr(event, "messages", None) or [])
        if not messages:
            self._retry_events[sid] = [event] if getattr(event, "message", None) is not None else []
            return
        template = self._last_events.get(sid) or event
        events = []
        for message in messages:
            replay = copy.copy(template)
            replay.message = message
            replay.session = event.session
            for attr in ("adapter", "supported_elements"):
                if hasattr(event, attr):
                    setattr(replay, attr, getattr(event, attr))
            events.append(replay)
        self._retry_events[sid] = events

    async def _publish_retry(self, raw_sid: str, events: list) -> bool:
        """Use the public buffer/filtered-flush contract without stealing new work."""
        flush = self.ctx.flush_session_messages
        # Older hosts exposed extra_event; inspect before calling, never replay
        # a call after TypeError since publication may already have side effects.
        if len(events) == 1 and "extra_event" in inspect.signature(flush).parameters:
            return await flush(raw_sid, extra_event=events[0])
        buffer = self.ctx.get_buffer(raw_sid)
        replays = [copy.copy(event) for event in events]
        identities = {id(event) for event in replays}
        selected = lambda event: id(event) in identities
        async with buffer.lock:
            for event in replays:
                buffer.add(event)
        try:
            return await flush(raw_sid, filter_fn=selected)
        finally:
            # On failed/empty publication remove only our injected events.
            # Real hosts drain these on success; concurrent new messages stay.
            async with buffer.lock:
                drain = getattr(buffer, "flush", None)
                if callable(drain):
                    drain(filter_fn=selected)

    @staticmethod
    def _input_selector(events):
        originals = tuple(events)
        mids = set()
        for ev in events:
            mid = getattr(getattr(ev, "message", None), "message_id", None)
            if mid is not None and str(mid):
                mids.add(str(mid))
        def selected(ev):
            mid = getattr(getattr(ev, "message", None), "message_id", None)
            return any(ev is original for original in originals) or (mid is not None and str(mid) in mids)
        return selected

    @staticmethod
    def _buffer_events(buffer):
        """Read published host buffers without pop/add or assumptions about test .items."""
        for owner in (buffer, getattr(buffer, "buf", None)):
            if owner is None:
                continue
            for name in ("items", "buffer"):
                events = getattr(owner, name, None)
                if isinstance(events, (list, tuple, deque)):
                    return list(events)
        return None

    async def _wait_input_buffered(self, sid, flow, epoch) -> bool:
        # buffer(force=True) chooses policy; the host may add AFTER this hook
        # returns and may yield before doing so. Do not copy/re-buffer the event.
        await asyncio.sleep(0)
        getter = getattr(self.ctx, "get_buffer", None)
        if not callable(getter) or not flow.active:
            return True
        buffer = getter(self._raw_sids.get(sid) or sid)
        while not self._closed and flow.epoch == epoch and flow.phase == "active":
            async with buffer.lock:
                current = self._buffer_events(buffer)
            if current is None or all(any(self._input_selector([ev])(item)
                                         for item in current) for ev in flow.active):
                return True
            await asyncio.sleep(0.01)  # watchdog bounds a host that never commits
        return False

    async def _discard_active_input(self, sid: str, events) -> None:
        if not events:
            return  # An empty frozen batch must NEVER mean "flush everything".
        getter = getattr(self.ctx, "get_buffer", None)
        if not callable(getter):
            return
        buffer = getter(self._raw_sids.get(sid) or sid)
        selected = self._input_selector(events)
        async with buffer.lock:
            drain = getattr(buffer, "flush", None)
            if callable(drain):
                drain(filter_fn=selected)
            else:
                # Legacy pop/add API; original objects only, never republish.
                events = buffer.pop(buffer.get_length())
                for event in events:
                    if not selected(event):
                        buffer.add(event)

    async def _drain_input_locked(self, sid: str, flow) -> None:
        st = self._states.get(sid)
        while (st is not None and flow.deferred and not self._closed
               and flow.phase == "idle" and not st.busy):
            if st.debounce_deadline > 0:
                break
            event, arrived = flow.deferred.popleft()
            before = st.total_triggers
            await self._admit_message(event, sid, st, arrived, flow)
            if st.total_triggers != before:
                break  # The triggering item belongs to the next frozen batch.

    async def _settle_input_locked(self, sid: str, flow, *, trigger="idle_check",
                                   trim=True, drain=True, discard=True) -> None:
        flow.phase = "settling"
        st = self._states.get(sid)
        eid = flow.event_id or self._active_rounds.get(sid, "")
        if eid:
            self._settled_rounds[(sid, eid)] = time.time()
        # Keep the boundary closed until all output is durable in the window and
        # trimming has finished. Deferred input is absent from both operations.
        if self.window is not None:
            try:
                self.window.fold_pending(sid)
            except Exception:
                logger.exception("[multi-decision] 收束输出入窗失败（继续释放隔离）")
            if trim:
                try:
                    self.window.maybe_trim(sid, trigger=trigger)
                except Exception:
                    logger.exception("[multi-decision] 收束裁切失败（继续释放隔离）")
        if discard:
            try:
                await self._discard_active_input(sid, flow.active)
            except Exception:
                logger.exception("[multi-decision] 收束清理已接纳buffer失败（继续释放隔离）")
        flow.active = []
        flow.event_id = ""
        flow.runner = None
        flow.sent_ids.clear()
        flow.phase = "idle"
        if st is not None:
            st.busy = False
            st.busy_since = 0.0
            st.queued = False
            st.round_claimed = 0.0
        watchdog = self._watchdogs.pop(sid, None)
        if watchdog is not None and watchdog is not asyncio.current_task() and not watchdog.done():
            watchdog.cancel()
        if (drain and not self._closed and self.plugin.enabled and self.plugin.decision_enabled
                and self._c("session_enabled", sid) is not False):
            await self._drain_input_locked(sid, flow)

    async def _settle_input(self, sid: str, *, epoch=None, **kwargs) -> None:
        flow = self._input_flow(sid)
        async with flow.lock:
            if epoch is not None and (flow.epoch != epoch or flow.phase != "active"):
                return
            await self._settle_input_locked(sid, flow, **kwargs)

    async def _execute(self, sid: str, retry: bool = False) -> None:
        st = self._states.get(sid)
        if st is None or self._closed:
            return
        if retry and (not self.plugin.enabled or not self.plugin.decision_enabled
                      or self._c("session_enabled", sid) is False):
            self._cancel_retry(sid)
            self._retry_status(sid, self._retry_roots.pop(sid, ""), "cancelled")
            self._retry_events.pop(sid, None)
            st.retry_at = 0.0
            st.retry_count = 0
            return
        flow = self._input_flow(sid)
        async with flow.lock:
            if self._closed:
                return
            if st.busy or flow.phase != "idle":
                # New arrivals are not counted yet, so they cannot queue a round.
                st.queued = bool(flow.ready)
                return
            if retry and flow.ready:
                # New input is already admitted/counted for its own next round.
                # An old retry must not consume its pending or original buffer.
                scheduled = self._retries.pop(sid, None)
                if scheduled is not None and scheduled is not asyncio.current_task():
                    scheduled.cancel()
                st.retry_at = 0.0
                self._retry_status(sid, self._retry_roots.pop(sid, ""), "cancelled")
                self._retry_events.pop(sid, None)
                return
            flow.epoch += 1
            epoch = flow.epoch
            flow.phase = "active"
            flow.event_id = ""
            flow.sent_ids.clear()
            flow.runner = asyncio.current_task()
            flow.active, flow.ready = flow.ready, []
            # Compatibility with external/default-chat entry points which have
            # no receive registration; only use an enumerable snapshot, never a
            # selector that accepts an unknown/empty batch.
            if not flow.active and not retry:
                getter = getattr(self.ctx, "get_buffer", None)
                if callable(getter):
                    buffer = getter(self._raw_sids.get(sid) or sid)
                    excluded = self._input_selector([ev for ev, _ in flow.deferred])
                    async with buffer.lock:
                        flow.active = [ev for ev in (self._buffer_events(buffer) or [])
                                       if not excluded(ev)]
            st.busy = True
            st.busy_since = time.time()
            # 用户 m05453：这一轮的起点按「第一次触发」算（含抖动 + 决策 + 回复整段）；
            # 抖动与决策另外量好存着，记录页把三段分开写，谁也别混进谁格里。
            _first = float(st.debounce_first or st.trigger_wall or 0.0)
            self._round_trigger_at[sid] = 0.0 if retry else _first
            self._round_jitter[sid] = 0.0 if retry else float(st.debounce_spent or 0.0)
            self._round_decision[sid] = 0.0
            st.trigger_wall = 0.0
            st.debounce_first = 0.0
            st.debounce_jitter = 0.0
            st.debounce_spent = 0.0
            self._active_rounds[sid] = ""  # reject late finals until request binding
            # Cover decide and publication too, not just the already-published agent.
            self._arm_watchdog(sid, st)
        raw_sid = self._raw_sids.get(sid) or sid
        root = self._retry_roots.get(sid, "")
        try:
            if not retry and not await self._wait_input_buffered(sid, flow, epoch):
                return
            if not retry and self._c("advanced_enabled", sid):
                try:
                    if await self._decision_gate(sid, st):
                        await self._settle_input(sid, epoch=epoch, trim=False)
                        return
                except Exception:
                    logger.exception("[multi-decision] 决策闸门异常，按没开进阶模式继续")
            if self._closed or flow.epoch != epoch or flow.phase != "active":
                return
            if retry:
                events = self._retry_events.get(sid) or [self._last_events.get(sid)]
                events = [ev for ev in events if ev is not None]
                if not events:
                    st.retry_at = 0.0
                    self._retry_status(sid, root, "cancelled")
                    await self._settle_input(sid, epoch=epoch)
                    return
                token = uuid.uuid4().hex
                if root and self.retry_history is not None:
                    if not self.retry_history.start(sid, root, token):
                        await self._settle_input(sid, epoch=epoch)
                        return
                    events = self._tag_retry_events(sid, root, token, events)
                ok = await self._publish_retry(raw_sid, events)
            else:
                if not await self._wait_input_buffered(sid, flow, epoch):
                    return
                ok = await self.ctx.flush_session_messages(
                    raw_sid, filter_fn=self._input_selector(flow.active))
                # Preserve explicit no-new-input timer rounds, but never replay a
                # normal/deferred batch after a failed/empty publication.
                reason = str(getattr(st, "last_trigger_reason", "") or "")
                last = self._last_events.get(sid)
                if (not ok and not flow.active and not flow.deferred and last is not None
                        and reason in ("silent", "buffer", "curve", "adaptive", "probability", "count")):
                    ok = await self._publish_retry(raw_sid, [last])
            if self._closed or flow.epoch != epoch or flow.phase != "active":
                return  # A synchronous host may have delivered final while awaiting flush.
            if not ok:
                if retry:
                    self._retry_status(sid, self._retry_roots.get(sid, ""), "cancelled")
                await self._settle_input(sid, epoch=epoch)
                return
            if not retry:
                st.retry_at = 0.0
                st.retry_count = 0
            self.health["delivered"] += 1
            logger.info(f"[multi-decision] {sid} 已投递，等待结算")
        except asyncio.CancelledError:
            if st.debounce_task is asyncio.current_task():
                st.debounce_task = None
                st.debounce_deadline = 0.0
            await self._settle_input(sid, epoch=epoch, drain=not self._closed)
            raise
        except Exception as exc:
            logger.exception(f"[multi-decision] 执行失败 sid={sid}")
            if flow.epoch == epoch and flow.phase == "active":
                st.last_error = f"{type(exc).__name__}: {exc}"[:500]
                st.last_error_at = time.time()
                if retry and (not root or self._retry_roots.get(sid) == root):
                    self._retry_after_fail(sid, st)
                await self._settle_input(sid, epoch=epoch)

    async def _decision_gate(self, sid: str, st) -> bool:
        """进阶模式的第一段：让模型读一遍实时窗口，判「这一轮要不要开口」。

        只接管模式判定（计数 / 概率 / 曲线 / 动态调整）与两套兜底（静默兜底、
        休息时段兜底）通过之后的那一步——兜底本身也是「这轮要不要开口」的判断，
        开了决策就一并交给它（v4.09，用户 m17145 第 2 条）；
        @、叫到名字（reason = mention / nickname，也就是「必回」那一路）这些已经
        明摆着要回的，不重复花钱；「触发」（wake）与「休息时段掷中」（rest）这两路
        会进来判；重试轮也不重判（上一轮已经判过了）。

        返回 True = 这一轮就此结束（不 flush、不跑 agent、不挂看门狗）。
        """
        reason = str(getattr(st, "last_trigger_reason", "") or "")
        # v4.09（用户 m17145 第 2 条）：静默兜底与休息时段兜底共用 reason="silent"
        # （_check_silent 里是同一个 _trigger），加进白名单两套就都会先过决策。
        # 判不回时 counter 早在 _trigger 里清零，不会再连着冒泡。
        # 用户 m00347：「触发」那一路算一次普通触发，也该先交给模型判（reason="wake"）；
        # 「必回」那一路的 reason 是 mention / nickname，故意不在这里——必回就是必回。
        # 用户 m24162：休息时段掷中的那一路（reason="rest"）以前是直接开口，
        # 到了夜里照样会「偷偷说话」。休息时段本来就按「不该说话」算，掷中了
        # 也只算拿到一次开口的机会，还得让模型判一遍才真回。
        if reason not in ("count", "probability", "curve", "adaptive", "silent", "wake", "rest"):
            return False
        phase = getattr(self.plugin, "decision_phase", None)
        if phase is None:
            return False
        try:
            opt = self.plugin.decision_options()
        except Exception:
            logger.exception("[multi-decision] 取决策配置失败，按没装进阶模式继续")
            return False
        # v3.95（用户 m09171）：决策接管的是「这一轮要不要开口」，读的必须是这一轮
        # 真正的那批消息——包括还挂在待发里、框架记忆里还没有的那些。只读窗口本体
        # 时窗口一空就判出「没人说话」。
        try:
            msgs = self.window.decision_snapshot(sid)
        except Exception:
            logger.exception("[multi-decision] 决策取窗口失败（按空记录判）")
            msgs = []
        # v3.97（用户 m09615 图9）：快照要分两段存——② 用窗口本体（历史），③ 只留
        # 这一轮新来的那批待发。以前 ③ 直接拿整段 transcript（历史＋待发），页面上
        # 就是「本轮消息把历史又叠了一遍」。
        try:
            hist = self.window.snapshot(sid)
        except Exception:
            logger.exception("[multi-decision] 决策取窗口历史失败（② 会空着）")
            hist = []
        try:
            incoming = self.window.decision_incoming(sid)
        except Exception:
            logger.exception("[multi-decision] 决策取待发失败（③ 会空着）")
            incoming = []
        flow = self._input_flows.get(sid)
        epoch = flow.epoch if flow is not None and flow.phase == "active" else None
        started = time.time()
        # v4.39（用户 m20816）：决策也要能看图。v4.40（用户 m21305）：不再自己
        # 定张数——跟回复那条同一个口径，窗口里留几张原图（history_images，
        # 默认 128）就带几张；开关关掉才退回纯文字。
        dec_imgs = []
        try:
            if self._c("decision_see_images", sid) is not False:
                dec_imgs = self.window.decision_images(sid)
        except Exception:
            logger.exception("[multi-decision] 决策取图失败（这一轮按纯文字判）")
            dec_imgs = []
        got = await phase.decide(sid, msgs, opt, images=dec_imgs)
        if self._closed or (epoch is not None and
                (flow.epoch != epoch or flow.phase != "active")):
            return True  # A timed-out/cancelled decision may not mutate a later window.
        # 用户 m05124 第 8 条：决策轮也要记防抖——触发那一刻（_trigger 记在 st.trigger_wall，
        # 由 _execute 转存到 _round_trigger_at）到真正开始判，中间干等了多久。
        # 用户 m05453：防抖 / 决策 / 回复是三段。这一格只写「抖动真的等了几秒」，
        # 不再用 started - 触发时刻——那里面还含着决策前的准备，看着像防抖变长了。
        # 用户 m05691 第 4 条 / 用户 m13212 再确认：链路是「防抖 → 决策 → 回复」，
        # 进阶模式下抖动的这几秒属于**决策之前**的等待，只记在决策那一行。
        # 决策一旦要回就马上进回复流程，回复那一段不再叠加防抖，所以这里取走即清空。
        _jit = float(self._round_jitter.pop(sid, 0.0) or 0.0)
        rec = {
            "ts": started,
            "sid": sid,
            "debounce": round(_jit, 2),
            "reply": bool(got.get("reply")),
            "reason": str(got.get("reason") or ""),
            "say": str(got.get("say") or ""),
            "raw": str(got.get("raw") or ""),
            "model": str(got.get("model") or ""),
            "via": str(got.get("via") or ""),
            "attempts": int(got.get("attempts") or 0),
            "elapsed": float(got.get("elapsed") or 0.0),
            "tools_used": list(got.get("tools_used") or []),
            "system_parts": list(got.get("system_parts") or []),
            "transcript": str(got.get("transcript") or ""),
            # 用户 m06046 图3：真正发出去的那条 user 消息（记录 + 末尾动态段），
            # 快照照它写「本轮输入 / 动态段」，开了系统动态段才看得到注入。
            "user_prompt": str(got.get("user_prompt") or ""),
            "dynamic_text": str(got.get("dynamic_text") or ""),
            "dynamic_parts": list(got.get("dynamic_parts") or []),
            # v4.39（用户 m20816）：这一轮决策到底带了几张图、是不是退回纯文字了。
            "images": list(got.get("images") or []),
            "image_count": int(got.get("image_count") or 0),
            "images_fallback": bool(got.get("images_fallback")),
            "input_tokens": int(got.get("input_tokens") or 0),
            "output_tokens": int(got.get("output_tokens") or 0),
            "cached_tokens": int(got.get("cached_tokens") or 0),
            "failed": bool(got.get("failed")),
            "error": str(got.get("error") or ""),
            "messages": list(hist or []),
            "incoming": list(incoming or []),
            "trigger_reason": reason,
            # v3.98（用户 m10169 图6）：触发时掷到多大点概率（_trigger 记在
            # st.last_trigger_prob 上，计数模式恒 0）。
            "trigger_probability": float(getattr(st, "last_trigger_prob", 0.0) or 0.0),
        }
        # v4.12（用户 m18246/m18275）：决策快照的 ②③ 跟回复快照走同一套投影。
        # 以前 display_messages 直接存原始窗口消息（纯文本、没有 media），转发消息
        # 里的图片在决策记录里永远不显示；回复记录用的是 view_of 投影，所以能显示。
        # 两边都从 view_of 出、条数一一对应，页面才有同样的图片。
        try:
            _raw_sid = self.window.raw_sid(sid)
        except Exception:
            _raw_sid = ""
        try:
            _who = self.window._bot_name(sid)
        except Exception:
            _who = ""
        try:
            from ..window.manager import view_of as _view_of
            rec["display_messages"] = _view_of(hist or [], bot_name=_who, raw_sid=_raw_sid)
            rec["incoming_display_messages"] = _view_of(
                incoming or [], bot_name=_who, raw_sid=_raw_sid
            )
        except Exception:
            logger.exception("[multi-decision] 决策快照投影失败（退回原始消息）")
        # 两份账：一份落成快照（和请求记录同一套模板），一份进窗口当聊天记录。
        if self.request_log is not None:
            try:
                self.request_log.write_decision_snapshot(sid, rec)
            except Exception:
                logger.exception("[multi-decision] 写决策快照失败（已忽略）")
        try:
            self.window.push_decision(sid, rec)
        except Exception:
            logger.exception("[multi-decision] 决策记录进窗口失败（已忽略）")
        self.health["last_decision"] = {
            "ts": float(rec["ts"]),
            "sid": sid,
            "reply": bool(rec["reply"]),
            "reason": str(rec["reason"])[:160],
            "say": str(rec["say"])[:160],
            "failed": bool(rec["failed"]),
            "error": str(rec["error"])[:160],
            "model": str(rec["model"]),
            "elapsed": float(rec["elapsed"]),
        }
        if rec["reply"]:
            # 用户 m05453：判要回时把决策耗时留给回复轮——回复记录那行要写
            # 「防抖 7s + 决策 5s + 12s」，不然总时长对不上。
            self._round_decision[sid] = round(float(rec["elapsed"] or 0.0), 2)
            # v4.06（用户 m12879）：判要回先把这一轮的待发并进窗口，再落这条判定——
            # 它得排在本轮消息之后（本轮 user → 决策理由 → agent 回复），不能卡在
            # 本轮消息前面被当成历史。待发并完就清空，下面 collect 不会再并一遍。
            try:
                self.window.fold_pending(sid)
            except Exception:
                logger.exception("[multi-decision] 判要回并入待发失败（已忽略）")
            # v4.05（用户 m12604）：判要回，这条判定就是 agent 自己的一句话——
            # 当场 append 到窗口末尾（本轮消息之后、下面这轮回复之前），之后没
            # 人再动它：不合并、不搬位置、裁切时也不特殊照顾。
            try:
                self.window.commit_decision(sid)
            except Exception:
                logger.exception("[multi-decision] 判定要回的消息落地失败（已忽略）")
            self._write_log(sid, "decision_reply")
            # v4.11（用户 m17596 第 6 条）：这一轮是决策判出来的，触发列要写「决策触发」。
            st.decision_passed = True
            # v4.11（用户 m17596 第 1 条）：判要回、真去跑 agent 之前按阈值（60 条 user）
            # 先裁一次——决策读的是完整窗口，但发出去的请求只需要保留条数。裁完单独落
            # 一份裁切快照，趋势图上那根柱子就是橙色的。
            try:
                self.window.maybe_trim(sid, trigger="decision")
            except Exception:
                logger.exception("[multi-decision] 决策后裁切异常（已吞掉）")
            logger.info(
                "[multi-decision] %s 进阶决策判定要回：%s（%.1fs）",
                sid, rec["reason"][:60], rec["elapsed"],
            )
            return False
        # 不回：这一轮到此为止。
        self.health["decision_skip"] = int(self.health.get("decision_skip") or 0) + 1
        self._write_log(sid, "decision_skip")
        if epoch is None:  # direct gate compatibility; managed rounds settle centrally
            st.busy = False
            st.busy_since = 0.0
            st.queued = False
        # v3.96（用户 m09331「判不回也要成为快照的」）：judge 完不回，这一轮就到此
        # 为止，待发就地并进窗口再清空——否则没有真实请求，collect 永远不跑，待发
        # 会一路堆到下一次真回，页面上窗口一直空着。
        try:
            self.window.fold_pending(sid)
        except Exception:
            logger.exception("[multi-decision] 判不回并入待发失败（已忽略）")
        # v4.05（用户 m12604）：判不回也要留一条，位置在本轮消息之后——待发刚
        # 并进窗口，这一条紧跟其后，就是 agent 自己说的一句话；下一轮读到的
        # 就是「上一轮我判过不回，理由是…」。判要回那条已在上面落过，这里再调
        # 一次是空转（commit_decision 只追加未 committed 的）。
        try:
            self.window.commit_decision(sid)
        except Exception:
            logger.exception("[multi-decision] 判不回注记落地失败（已忽略）")
        # v4.12（用户 m18224 第 1 条）：判不回也要在「判定的那一刻」按阈值裁切。
        # 用户原话：「决策判定不回也要触发裁切（判定的那一刻整个上下文 user 满设置
        # 数量就裁）」。以前这里走 idle_check，要攒到「阈值 + 缓冲」（60+10=70）才动，
        # 判不回的轮次常常等不到那一刻；trigger="decision" 与判要回那条同口径（60）。
        try:
            self.window.maybe_trim(sid, trigger="decision")
        except Exception:
            logger.exception("[multi-decision] 判不回后裁切异常（已吞掉）")
        if rec["failed"] and rec["error"]:
            st.last_error = str(rec["error"])[:500]
            st.last_error_at = time.time()
        logger.info(
            "[multi-decision] %s 进阶决策判定不回：%s（%.1fs）",
            sid, rec["reason"][:60], rec["elapsed"],
        )
        return True

    def _watchdog_seconds(self, sid: str = "") -> float:
        """看门狗到底等多少秒（设置页的「看门狗超时」，用户 2026-10-24 第 4 条）。

        BUSY_WATCHDOG 只是老默认值；用户把 watchdog_seconds 调小，这里立刻跟着走。
        兜底 max(1.0, ...)：0/负数/脏值不能变成「投递完立刻强释放」，那会把
        正常轮全掐死。
        """
        try:
            raw = self._c("watchdog_seconds", sid)
        except Exception:
            raw = None
        try:
            value = float(raw if raw is not None else BUSY_WATCHDOG)
        except (TypeError, ValueError):
            value = BUSY_WATCHDOG
        return max(1.0, value)

    def _arm_watchdog(self, sid: str, st) -> None:
        """投递成功后挂一个超时兜底。

        框架一旦在 assemble/execute 阶段抛异常，事件总线会把它静默吞掉
        （连日志都不打），插件永远等不到 _handle_final，st.busy 就永久为
        True —— 表现为「重启就好、第一轮正常、第二轮起永远不回」。
        v2.46 修了根因（窗口里混进 dict），这里再兜一层：到点还没结算就
        强制解锁并裁一次窗口，免得坏轮把窗口越撑越大。
        等多久由配置 watchdog_seconds 决定（BUSY_WATCHDOG 只是默认值）。
        """
        started = st.busy_since
        wd = self._watchdog_seconds(sid)
        task = None

        async def _wait():
            try:
                await asyncio.sleep(wd)
                if not st.busy or st.busy_since != started:
                    return  # 已经结算，或者又开了新的一轮
                logger.warning(
                    f"[multi-decision] {sid} 投递后 {int(wd)}s 没有任何结算，"
                    f"强制释放忙碌标记（框架可能在中途抛了异常并被事件总线吞掉）"
                )
                self.health["watchdog"] += 1
                # 投递后等不到结算，和「请求失败」是同一件事：这一轮的东西没出来。
                # 以前只放掉忙碌标记，于是这个群就这么静着，非得等下一条消息才恢复；
                # 现在按失败走同一条重试路径，retry_max 次内自己补回来（用户 m51xxx 图2）。
                st.last_error = "投递后 %d 秒没有任何结算" % int(wd)
                st.last_error_at = time.time()
                # 只放掉忙碌标记的话，走势图上根本没有「触发看门狗的那一轮」：
                # 用户只看到健康条说释放过 1 次，对不上是哪根柱子（用户 m01927）。
                self._watchdog_record_failure(sid, st)
                self._retry_after_fail(sid, st)
                flow = self._input_flow(sid)
                runner = flow.runner
                if runner is not None and not runner.done() and runner is not asyncio.current_task():
                    if st.debounce_task is runner:
                        st.debounce_task = None
                        st.debounce_deadline = 0.0
                    runner.cancel()
                await self._settle_input(sid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[multi-decision] 看门狗异常 sid={sid}")

        try:
            task = asyncio.get_running_loop().create_task(_wait())
            self._watchdogs[sid] = task
        except Exception:
            logger.exception("[multi-decision] 看门狗挂载失败（已吞掉）")

    def _watchdog_record_failure(self, sid: str, st) -> None:
        """把被看门狗强释放的这一轮按「失败」记下来（用户 m01927）。

        _handle_final 的失败路径会 flush 一条 failed 记录并登记成重试原轮；
        看门狗抢在结算之前放行，那条路径根本走不到，于是这一轮在走势图上
        凭空消失。这里补同样的两步：登记出错原轮（后面重试成功也绑回这根
        柱子，柱子保持红色并写明重试次数）+ 落一条 failed 记录（柱子存在）。
        """
        try:
            eid = str(self._active_rounds.get(sid, "") or "")
            if not eid:
                return
            # 这一轮不会再有 final 了，round_end 只能在这里补，否则统计里它凭空消失
            self._log_failed_round(sid, eid, st, st.last_error or "请求失败")
            self._record_retry_error(sid, eid)
            # 健康条要知道「最近一次被强释放的是哪一轮、后来重试了几次、成了没有」
            # （用户 2026-10-24 图3）。这里只记身份，次数与结果读的时候现取。
            self._watchdog_last = {
                "sid": sid,
                "root_event_id": eid,
                "ts": time.time(),
                "attempts": int(getattr(st, "retry_count", 0) or 0),
                "retry_event_id": "",
                "recovered_by": "",
                "recovered_ts": 0.0,
            }
            if self.request_log is None:
                return
            self.request_log.flush(
                sid, replied=False, event_id=eid,
                extra={"failed": True, "outcome": "failed",
                       "error": st.last_error or "请求失败", "watchdog_released": 1})
        except Exception:
            logger.exception("[multi-decision] 看门狗落盘失败记录异常（已吞掉）")

    # ------------------------------------------------------------------ 日志

    def _is_retry_round(self, sid: str, eid: str) -> bool:
        """这一轮是不是「重试产生的」（round_end 里的 is_retry）。

        _bind_retry_round 会把每轮挂到它的根 event_id 上：原始轮 root == 自己，
        重试轮 root == 原始轮的 event_id。所以看根就知道，不用猜 st.retry_count
        （原始失败轮在写 round_end 之前就已经把 retry_count +1 了，那个数不能当信号）。
        缺键时退回 retry_history（重试链路自己持久化的那份），再没有就说不是重试 ——
        宁可少报重试，也不能把普通轮标成重试。
        """
        if not sid or not eid:
            return False
        try:
            root = str(self._round_roots.get((sid, eid), "") or "")
            if root:
                return root != str(eid)
            if self.retry_history is not None:
                summary = self.retry_history.summary(sid, eid)
                if summary:
                    root = str(summary.get("root_event_id") or "")
                    if root:
                        return root != str(eid)
        except Exception:
            logger.exception("[multi-decision] 判定重试轮失败（当非重试处理）")
        return False

    def _log_failed_round(self, sid: str, eid: str, st, error: str = "") -> None:
        """给「这一轮最终失败」补一条 round_end（用户 2026-10-24 第 2 条）。

        以前 round_end 只在 _handle_final 里写，而看门狗兜底、异常钩子这两条路
        根本等不到 final —— 于是「最近 24 小时」里只剩成功轮的结算（实测 173 条
        round_end 全是 replied），卡片上的成功率永远是 100%。失败轮一律写
        failed=True / replied=False / outcome=failed，并标上 is_retry。
        同一个 event_id 只写一次（final 后面又来了也不重复记，免得 rounds 虚高）。
        """
        if not sid or not eid or st is None:
            return
        key = (sid, eid)
        if key in self._round_end_logged:
            return
        self._round_end_logged[key] = time.time()
        try:
            self._write_log(
                sid, "round_end",
                extra={
                    "replied": False,
                    "failed": True,
                    "event_id": eid,
                    "reason": getattr(st, "last_trigger_reason", ""),
                    "is_retry": self._is_retry_round(sid, eid),
                },
            )
        except Exception:
            logger.exception("[multi-decision] 补写失败轮 round_end 失败（已吞掉）")

    def _write_log(self, sid, result, extra: Optional[dict] = None) -> None:
        """消息日志：只记统计要用的字段，正文不落盘（用户 m19238）。

        一行 = 一次事件：收下一条消息（counting / 各种 skip）、一次触发
        （trigger）、一轮结算（round_end）。落盘的键只有 ts / sid / result
        和 extra 里那几个（reason / event_id / replied / failed / is_retry /
        message_logged）——state/decision_log.py 的统计、时段曲线与日志页
        只用这些。正文、发送者、媒体类型、计数、错误文本 v3.65 起不再写
        （日志页本来也不展示，删了省一半体积）。
        """
        # 每次落日志都是状态刚刚变化过的时刻，顺手落一次盘（内部有节流）
        self._persist_soon()
        if self.log is None:
            return
        record = {"ts": time.time(), "sid": sid, "result": result}
        if extra:
            record.update(extra)
        self.log.write(record)

    # ------------------------------------------------------------------ 生命周期

    def _preserve_shutdown_input(self, sid, flow) -> None:
        """Persist accepted but unadmitted input; never count, trigger or publish."""
        if self.window is None:
            return
        waiting = ([flow.admitting] if flow.admitting is not None else []) + list(flow.deferred)
        try:
            self.window.fold_pending(sid)
        except Exception:
            logger.exception("[multi-decision] 关闭时保存现有待发失败")
        for event, arrived in waiting:
            message = getattr(event, "message", None)
            if message is None:
                continue
            try:
                sender = sender_id_of(message)
                name = sender_name_of(message)
                text = extract_text(message)
                self.window.push_pending(sid, sender, name, text, media_kind(message), arrived,
                    raw_line_of(message, text, sender, name, arrived),
                    files=file_refs_of(message))
                self.window.fold_pending(sid)
            except Exception:
                logger.exception("[multi-decision] 关闭时保存隔离输入失败")
        flow.admitting = None

    async def shutdown(self) -> None:
        self._closed = True
        for flow in self._input_flows.values():
            flow.epoch += 1  # invalidate suspended media/request/decision work first
        current = asyncio.current_task()
        running = set(self._tasks) | set(self._watchdogs.values()) | set(self._retries.values()) | set(self._input_tasks)
        running |= {flow.runner for flow in self._input_flows.values() if flow.runner is not None}
        running = {task for task in running if task is not None and task is not current and not task.done()}
        for task in running:
            task.cancel()
        if running:
            # A host publication may suppress cancellation; never hang unload on it.
            done, pending = await asyncio.wait(running, timeout=1.0)
            for task in done:
                if not task.cancelled():
                    task.exception()
            for task in pending:
                task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        for sid, flow in list(self._input_flows.items()):
            if flow.phase != "idle":
                # Teardown is exclusive (_closed + invalidated epochs). Do not wait
                # on a media/host lock held by a cancellation-resistant coroutine.
                await self._settle_input_locked(sid, flow, drain=False, discard=False)
            self._preserve_shutdown_input(sid, flow)
            flow.epoch += 1
            flow.ready.clear()
            flow.active.clear()
            flow.deferred.clear()
        self._input_flows.clear()
        self._input_tasks.clear()
        for sid, root in self._retry_roots.items():
            self._retry_status(sid, root, "cancelled")
        if self.store is not None:
            try:
                self.store.flush(self._persist_data())
            except Exception:
                logger.exception("[multi-decision] 关闭前保存会话状态失败")
        for st in self._states.values():
            task = st.debounce_task
            if task is not None and not task.done():
                task.cancel()
        for task in list(self._watchdogs.values()):
            try:
                if task is not None and not task.done():
                    task.cancel()
            except Exception:
                pass
        for task in list(self._retries.values()):
            try:
                if task is not None and not task.done():
                    task.cancel()
            except Exception:
                pass
        self._retries.clear()
        self._watchdogs.clear()
        self._last_events.clear()
        self._active_rounds.clear()
        self._round_jitter.clear()
        self._round_decision.clear()
        self._round_errors.clear()
        self._settled_rounds.clear()
        self._retry_events.clear()
        self._retry_roots.clear()
        self._cleared_retry_rounds.clear()
        self._round_roots.clear()
        self._round_end_logged.clear()
        for task in list(self._tasks):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._states.clear()
