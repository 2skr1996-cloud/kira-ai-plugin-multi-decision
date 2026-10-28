"""请求上下文记录：每轮把真正发给模型的上下文落一份。

观察窗只有消息文本，这个有 agent 真正看到的一切：
system 段有哪些、历史多长、尾部动态块写了什么、本批怎么拼的。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.logging_manager import get_logger

try:  # 同包内的计数口径，测试桩里没有就退回「数组长度」
    from ..window.trim import count_messages as _count_messages
except Exception:  # pragma: no cover
    def _count_messages(ms):
        return len(ms or [])

logger = get_logger("multi_decision", "orange")

#: v3.67（用户 m20117）：超上限不立刻删——刚写下的东西先留 5 分钟缓冲，
#: 下一次写入触发的清理再把超出的部分带走（免得一轮还在跑、文件就被删）。
PRUNE_GRACE_SECONDS = 300

#: v3.47（用户 2026-10-24 第 2 条）：请求账本只保留这么多天。
#: requests/<sid>/ 每会话只留 keep 份（默认 100），忙的群一天就超了；
#: 账本跟它并列放在 stats/requests.jsonl，只按时间滚动清理。
LEDGER_KEEP_DAYS = 30


def _outcome_of(data: dict) -> str:
    """一份快照属于哪一类：replied / no_reply / failed / decision / trim。"""
    try:
        oc = str((data or {}).get('outcome') or '')
    except Exception:
        return 'replied'
    if oc:
        return oc
    d = data or {}
    if d.get('failed'):
        return 'failed'
    return 'replied' if d.get('replied') else 'no_reply'


def _safe_sid(sid: str) -> str:
    """sid 里带冒号（adapter:gm:1001），Windows 文件名不允许，换成下划线。

    注意别放行 "."：一旦 sid 是 ".."，这里会原样拼出上级目录，
    而 purge() 拿到路径后是直接 rmtree 的（审查报告：可删上级目录）。
    """
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(sid))
    return safe or "unknown"


#: 命中预估用的粗略 tokenizer 口径（v3.19）：CJK 一字算一个 token，其余每 4 字符算一个。
#: 只用于给「前缀能复用多少」估个百分比，不参与任何计费或上限判断。
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


# 框架 assemble_prompt 会把这几段挪到最后一条 user 消息（见 core/provider/llm_model.py）
DYNAMIC_PROMPT_NAMES = ("sessions", "chat_env", "time")
MEMORY_PROMPT_NAME = "memory"
#: 插件把动态段挪到本轮消息之后时，用它包住那几段（dynamic_to_tail）
TAIL_MARK_NAMES = ("dynamic_context_start", "dynamic_context_end")
#: 唤醒标记（window/split.py 的 MARK=" [@你]"）：只打在 request.messages 与
#: user_prompt 的 message 段上，窗口本体那条没有。比对前必须归一，否则每条
#: 都「不相等」，本轮消息永远切不出历史（用户 m07060 图3：回复记录里同一条
#: @ 画了两遍，38/38 份 replied 快照都没有 incoming_messages）。
_MENTION_MARK = re.compile(r"\s*\[@你\]\s*")
#: user_prompt 里的 @昵称 形态：[At 1429260473(nickname: 樱井桃华)]
_AT_MARK = re.compile(r"\[At\s+-?\d+\(nickname:\s*([^)]*)\)\]")


def _join_reloc_blocks(blocks) -> str:
    """把几段动态文字拼成页面里那一块「系统动态段」。

    用户 m07111 图2：以前是空串相接，「…群里不会出现任何东西。## 核心记忆（Core
    Memory）」糊在同一行，前端认不出那是标题，看着像记忆被粘在规则句尾；只有标题
    没有正文的那一段（那次会话确实没记忆）也跟着挤成一团。现在段与段之间空一行，
    纯空段不占位。
    """
    parts = [str(b or "").strip("\n").rstrip() for b in (blocks or [])]
    return "\n\n".join(p for p in parts if p.strip())


def _row_key(value: Any) -> str:
    """比对两条消息是不是同一条：去掉唤醒标记、压掉空白、取前 400 字。"""
    text = str(value or "")
    if not text:
        return ""
    if "[@你]" in text:
        text = _MENTION_MARK.sub(" ", text)
    if "[At " in text:
        # 框架会把 user_prompt 里的 @昵称 改写成 [At 1429260473(nickname: 樱井桃华)]，
        # 窗口与 messages 里仍是 @樱井桃华；不还原就只差这一处。
        text = _AT_MARK.sub(lambda m: "@" + (m.group(1) or "").strip(), text)
    return " ".join(text.split())[:400]
#: 插件自己拼在本轮消息最下面的那一段（格式强化 / 系统动态段）。用户 m06046 图4：
#: 以前它没进这个名单，被算成「本轮消息」，动态段那一栏就只剩框架给的几十字。
DYNAMIC_TAIL_NAME = "dynamic_tail"


def _relocated(name: str, dynamic_position: str, memory_position: str) -> bool:
    """这一段组装后还留在 system 里吗（False = 会被挪到本轮用户消息末尾）。"""
    if name in DYNAMIC_PROMPT_NAMES and dynamic_position == "latest_user":
        return True
    if name == MEMORY_PROMPT_NAME and memory_position == "latest_user":
        return True
    return False


def _to_dict(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg
    to_dict = getattr(msg, "to_dict", None)
    if callable(to_dict):
        try:
            return to_dict()
        except Exception:
            return {"role": getattr(msg, "role", ""), "content": _plain_content(getattr(msg, "content", ""))}
    return {"role": getattr(msg, "role", ""), "content": _plain_content(getattr(msg, "content", ""))}


_DEC_NOTE_HEAD = ("判定要回", "判定不回")


def _decorate_rows(rows: Any, media_by_id: Any) -> None:
    """给快照里的消息行补上「判定注记」标记与图片引用（用户 m08158）。

    快照存的是框架 Message，只有 role/content：窗口里那些 media 与 decnote
    全丢了。于是回复记录 ③ 里既看不到这次决策的注记（前端认注记靠 decnote /
    name / 「[决策记录]」前缀，注记正文却是「判定要回（…）」），也看不到刚发的图
    （media 为空 → 一张白卡）。这里按正文把注记认回来（跟 window/manager.py
    同一套规则），图片按 message_id 从 request._mdp_incoming_media 取。
    """
    if not rows:
        return
    try:
        from ..window.buffer import ids_of
    except Exception:  # pragma: no cover
        ids_of = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = row.get("content")
        text = body if isinstance(body, str) else ""
        if str(row.get("role") or "") == "assistant" and not row.get("decnote"):
            # 老注记带「[决策记录] 12:00」前缀（v4.05 之后才去掉），两种都认。
            head = re.sub(r"^\[决策记录\]\s*(?:\d{1,2}:\d{2})?\s*", "", text.lstrip())
            if head.startswith(_DEC_NOTE_HEAD):
                row["decnote"] = True
                row.setdefault("name", "对当前聊天消息的决策建议")
                row.setdefault("who", "对当前聊天消息的决策建议")
        if row.get("media") or not media_by_id or ids_of is None:
            continue
        try:
            ids = ids_of([{"role": "user", "content": body}])
        except Exception:
            ids = []
        got: list[dict] = []
        for mid in ids:
            for ref in (media_by_id.get(mid) or []):
                if not isinstance(ref, dict) or not ref.get("path"):
                    continue
                item = {"path": ref.get("path"), "mime": ref.get("mime") or ""}
                if ref.get("name"):
                    item["name"] = ref.get("name")
                if item not in got:
                    got.append(item)
        if got:
            row["media"] = got


def _chars_of(msgs: list) -> int:
    """粗算一份消息列表的字符数（字符串算长度，多模态只数文字部分）。

    v3.89（用户 m05321）：裁切快照现在含待发，字符数要按整份快照算，
    不能再拿裁后窗口的 after_chars 充数。
    """
    total = 0
    for m in msgs or []:
        c = (m or {}).get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(c, str):
            total += len(c)
            continue
        if isinstance(c, (list, tuple)):
            for part in c:
                if isinstance(part, str):
                    total += len(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(part.get("text") or "")
    return total


def _plain_content(content: Any) -> Any:
    """保留数组结构，别把多模态 content 拍成 str（会变成 Python 字典表示）。"""
    if content is None:
        return ""
    if isinstance(content, (str, dict)):
        return content
    if isinstance(content, (list, tuple)):
        out = []
        for part in content:
            if isinstance(part, (str, int, float, dict)) or part is None:
                out.append(part)
            else:
                out.append(str(part))
        return out
    return str(content)


def _content_chars(content: Any) -> int:
    """content 可能是 str / 多模态数组 / dict，逐层累加字符数。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, (list, tuple)):
        return sum(_content_chars(x) for x in content)
    if isinstance(content, dict):
        return sum(_content_chars(k) + _content_chars(v) for k, v in content.items())
    return len(str(content))


def _msg_chars(msg: Any) -> int:
    d = _to_dict(msg)
    n = _content_chars(d.get("content"))
    tc = d.get("tool_calls")
    if tc:
        try:
            n += len(json.dumps(tc, ensure_ascii=False))
        except Exception:
            n += _content_chars(tc)
    return n


_IMAGE_TYPES = {"kira_image_ref", "image_url", "image", "input_image"}


def _count_image_parts(value: Any) -> int:
    """数一段投影里有多少个图片 part（v3.52，用户 m07864 第 4 条）。

    图片只活在 native 投影里，``assembled`` 的 content 是纯字符串，所以字符前缀
    比例看不见它们；命中预估要按 token 折算时得先知道图有几张。
    """
    if isinstance(value, dict):
        n = 1 if value.get("type") in _IMAGE_TYPES else 0
        for v in value.values():
            n += _count_image_parts(v)
        return n
    if isinstance(value, (list, tuple)):
        return sum(_count_image_parts(v) for v in value)
    return 0


_PAYLOAD_FIELDS = (
    "text_payload_bytes", "image_count", "image_bytes",
    "image_base64_estimated_bytes", "total_estimated_bytes",
    "image_unknown_count", "payload_estimate_complete", "payload_measurement",
    "payload_estimate_source",
)


def payload_metrics(record: dict, data_root=None) -> dict:
    """Estimate JSON/base64 size from explicit request parts, never display lookups.

    File sizes are source sizes, not compressed images or measured network traffic.
    Missing/remote references remain unknown; partial totals are explicitly marked.
    """
    if all(key in record for key in _PAYLOAD_FIELDS):
        return {key: record[key] for key in _PAYLOAD_FIELDS}
    if data_root is None:
        try:
            from core.utils.path_utils import get_data_path
            data_root = get_data_path()
        except Exception:
            data_root = None
    image_count = image_bytes = encoded_bytes = unknown = 0

    def image_part(part):
        nonlocal image_count, image_bytes, encoded_bytes, unknown
        image_count += 1
        url = part.get("image_url") or part.get("url") or ""
        if isinstance(url, dict):
            url = url.get("url") or ""
        path = part.get("path") or ""
        mime = str(part.get("mime_type") or "")
        size = None
        encoded_size = None
        if isinstance(url, str) and url.startswith("data:") and ";base64," in url:
            prefix, encoded = url.split(",", 1)
            encoded = re.sub(r"\s", "", encoded)
            if (encoded and len(encoded) % 4 == 0
                    and re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded)):
                encoded_size = len(encoded)
                size = encoded_size // 4 * 3 - len(encoded) + len(encoded.rstrip("="))
                mime = prefix[5:].split(";", 1)[0]
        elif path and data_root is not None:
            try:
                root = Path(data_root).resolve()
                media_root = (root / "session_media").resolve()
                target = (root / str(path)).resolve()
                target.relative_to(media_root)
                if target.is_file():
                    size = target.stat().st_size
                    mime = mime or mimetypes.guess_type(target.name)[0] or "image/jpeg"
            except (OSError, ValueError, TypeError):
                pass
        if size is None:
            unknown += 1
            # Keep unknown references/URLs in the JSON baseline, without fetching them.
            return dict(part)
        image_bytes += size
        encoded_bytes += encoded_size if encoded_size is not None else 4 * ((size + 2) // 3)
        image_url = {"url": "data:%s;base64," % (mime or "image/jpeg")}
        detail = part.get("detail")
        if isinstance(part.get("image_url"), dict):
            detail = part["image_url"].get("detail", detail)
        if detail:
            image_url["detail"] = detail
        return {"type": "image_url", "image_url": image_url}

    def content(value):
        if isinstance(value, dict):
            if value.get("type") in _IMAGE_TYPES:
                return image_part(value)
            return {key: content(v) for key, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [content(v) for v in value]
        return value

    projected = isinstance(record.get("assembled_with_native"), list)
    assembled = record.get("assembled_with_native") if projected else (record.get("assembled") or record.get("messages") or [])
    baseline = [
        {"role": m.get("role"), "content": content(m.get("content"))}
        for m in assembled if isinstance(m, dict)
    ]
    text_bytes = len(json.dumps(baseline, ensure_ascii=False).encode("utf-8"))
    return {
        "text_payload_bytes": text_bytes,
        "image_count": image_count,
        "image_bytes": image_bytes,
        "image_base64_estimated_bytes": encoded_bytes,
        "total_estimated_bytes": text_bytes + encoded_bytes,
        "image_unknown_count": unknown,
        "payload_estimate_complete": unknown == 0,
        "payload_measurement": ("estimated_native_projection_not_wire_bytes" if projected
                                else "estimated_json_and_explicit_images_not_wire_bytes"),
        "payload_estimate_source": ("read_time_native_projection_reference_files" if projected
                                    else "read_time_reference_files"),
    }


def _unwrapped_reply(trace: Any) -> bool:
    """模型写了文字，却没有包在 <msg> 里——这一轮会算成「未回复」。

    用户 m02159 图6：记录里明明有模型输出，群里却没有这条消息。原因就是框架
    只解析 <msg> 里的东西，裸文本解析出 0 条消息。这里给前端一个标记，让那一行
    说清是哪种没回，而不是笼统的「未回复」。<msg/> 是模型主动不说话，不算。
    """
    if not isinstance(trace, dict):
        return False
    text = ""
    for step in trace.get("steps") or []:
        if isinstance(step, dict) and step.get("text"):
            text += str(step["text"])
    text = text.strip()
    return bool(text) and "<msg" not in text.lower()


class RequestLog:
    """data/requests/<sid>/<毫秒时间戳>.json，每会话保留最近 N 轮。"""

    def __init__(self, data_dir, keep_rounds: int = 100, enabled: bool = True,
                 keep_days: int = 0, keep_of=None, days_of=None,
                 dec_keep: int = 0, dec_keep_of=None):
        self.enabled = bool(enabled)
        self.dir = Path(data_dir) / "requests"
        try:
            self.keep = max(1, int(keep_rounds or 100))
        except (TypeError, ValueError):
            self.keep = 100
        # 用户 m13708 第 3 条：份数之外再加一条天数线（设置里的 requests_keep_days）。
        # 0 = 不限，只按每个会话的份数滚动；>0 时早于这么多天的快照也一起删。
        try:
            self.keep_days = max(0, int(keep_days or 0))
        except (TypeError, ValueError):
            self.keep_days = 0
        # 用户 m18059 第 4 条：缓存保留线按会话独立。keep_of(sid)/days_of(sid) 是
        # main.py 传进来的回调（读「这个群」的有效设置），没传回调就退回实例值。
        self._keep_of = keep_of
        self._days_of = days_of
        # v4.00（用户 m10782）：决策快照单独一类缓存。它和回复快照同目录、同命名，
        # 只能靠记录里的 decision_only 区分，所以份数上限也各算各的；没给就用回复那份。
        try:
            self.dec_keep = max(0, int(dec_keep or 0))
        except (TypeError, ValueError):
            self.dec_keep = 0
        self._dec_keep_of = dec_keep_of
        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                logger.exception("[multi-decision] 无法创建请求记录目录")
        self._pending: dict[tuple[str, str], dict] = {}
        # (sid, event_id) -> 落盘文件名。重试成功要把结果补写回「原错误那一轮」的
        # 记录，得先能按事件号找回那份文件；重启后索引是空的，_find_name 兜底扫盘。
        self._names: dict[tuple[str, str], str] = {}
        # v3.47（用户 2026-10-24 第 2 条）：24 小时统计的账本。
        # requests/<sid>/ 每会话只留 keep 份，一天超过 keep 轮的会话
        # rounds / replied / failed / silent 就全被截在那个上限上
        # （线上 gm:1145141974：一天 179 轮结算、目录只剩 100 份、回复只数到 90，
        # 于是卡片上「群聊 186 次 回复 90 次」对不上）。
        # 账本只存计数需要的几个字段，不受 keep 影响，跟 traces 的 tokens.jsonl 同一套做法。
        self.ledger = Path(data_dir) / "stats" / "requests.jsonl"
        self._ledger_cache: Optional[tuple] = None
        # 账本是「读一整行 → 追加一行」的写法，多个会话（或线程池里的统计任务）
        # 同时结算会互相插进半行（审查报告）。写入串行化，读侧本来就容忍坏行。
        self._ledger_lock = threading.Lock()
        self._ledger_trim_at = 0.0
        # v3.66（用户 m19536 第 6 条）：同一份目录会被页面每 5 秒轮询一次。
        # 线上 gm:1145141974 100 份 / 14 MB，每次重算要 2.5 秒，直接把事件循环
        # 堵住——用户点「设置」「首页」时任何请求都排在后面，就是「卡很久」。
        # 这里按目录指纹（文件数 + 最新修改时间）缓存摘要行，新落一轮或裁掉旧文件
        # 才会重建；重建本身由调用方丢进线程，不占事件循环。
        self._rounds_cache: dict[str, tuple] = {}
        # v4.10（用户 m17328）：缓存命中只看「跟上一份同类请求的公共前缀」。
        # 决策走 fast 模型、回复走主模型，两边的缓存不共享，所以必须按 kind 分开记：
        # (sid, "decision") 只跟上一次决策比，(sid, "reply") 只跟上一次真回复比。
        self._prev_cache: dict[tuple[str, str], tuple] = {}

    # ------------------------------------------------------------------ 采集

    DEC_NOTE_NAME = "对当前聊天消息的决策建议"

    @staticmethod
    def _role_of(msg) -> str:
        try:
            if isinstance(msg, dict):
                return str(msg.get("role") or "")
            return str(getattr(msg, "role", "") or "")
        except Exception:
            return ""

    @classmethod
    def _is_note(cls, msg) -> bool:
        """这条 assistant 消息是不是决策注记（v4.14）。

        注记是「上一轮判定」的产物，属于本轮输入；bot 真正的回复才是冷条。
        v4.05 起正文不再带 [决策记录] 前缀，只剩 name，所以两个都认。
        """
        try:
            if isinstance(msg, dict):
                name = str(msg.get("name") or msg.get("who") or "")
                content = msg.get("content")
            else:
                name = str(getattr(msg, "name", "") or getattr(msg, "who", "") or "")
                content = getattr(msg, "content", "")
        except Exception:
            return False
        if name == cls.DEC_NOTE_NAME:
            return True
        if isinstance(content, str):
            return content.strip().startswith("[决策记录")
        return False

    def _tail_split(self, prev_series: list, messages: list, prefix_len: int) -> tuple:
        """前缀之后那一段再分「冷」和「本轮输入」（v4.14 用户 m19095）。

        用户口径：回复只有连着的时候才有历史可复用。上一份回复请求之后新进来的
        群消息与决策注记，是这一轮要回复的输入，不算冷；真正冷的是
        ① 上一份请求里就有、被裁切搬走位置的老消息，
        ② bot 自己上一次的回复（上一份请求的输出，这轮才第一次进前缀）。
        返回 (冷条数, 本轮输入条数, 逐条标记串)；标记串与 messages 一一对应，
        h=命中 / c=冷 / f=本轮输入，system 这类不参与统计的记 h。
        """
        prev_set = set(prev_series or [])
        msgs = list(messages or [])
        n = max(0, min(int(prefix_len or 0), len(msgs)))
        # v4.26（用户 m22251）：本轮输入的起点改成「上一次回复的位置」，不再是
        # 「基准花名册里没有的」。裁切之后窗口里保留下来的老消息对不上前缀，
        # 但它们仍然是历史（上一轮已经请求过、只是被裁掉了），不能算这一轮新增；
        # bot 上一次的回复连同它之后的一切，才是这一轮要送进去的输入。
        last_reply = -1
        for i, m in enumerate(msgs):
            if self._role_of(m) != "assistant":
                continue
            if self._is_note(m):
                continue
            last_reply = i
        start = last_reply
        if start > 0:
            # 同一次回复常常是几条连着的 assistant（多气泡），往前一并算本轮。
            j = start - 1
            while j >= 0 and self._role_of(msgs[j]) == "assistant":
                start = j
                j -= 1
        # v4.29（用户 m23262）：没有真回复可参照时，边界取公共前缀的末端。
        # 上一段全部命中（n>0）说明那些消息上一轮已经发过，紧跟其后的这条
        # （典型是上一次决策刚落的注记）就是这一轮第一次发出去的东西，该算本轮输入；
        # 裁切后整段不命中（n==0）时保持老口径，全部算冷。
        if start < 0 and n > 0:
            start = n
        cold = 0
        fresh = 0
        flags = []
        for i, m in enumerate(msgs):
            role = self._role_of(m)
            if role not in ("user", "assistant"):
                flags.append("h")
                continue
            if i < n:
                # 公共前缀之内：上游缓存真的能复用，命中优先。
                flags.append("h")
                continue
            # v4.28（用户 m22895）：注记不再强行算本轮输入。v4.27 那版把它标成 f，
            # 前端就把注记整段搬到 ③ 开头，看着像决策结论被挪进了本轮消息里，
            # 上下文顺序被破坏。注记按它在 messages 里的真实位置判：落在上一次
            # 真回复之后才是本轮输入，落在前面就跟别的历史一样算冷。
            if start >= 0 and i >= start:
                # 上一次回复与它之后的群消息、决策注记：这一轮的输入。
                fresh += 1
                flags.append("f")
                continue
            # 上一次回复之前、又落不进公共前缀的：被裁切搬过位置的历史，算冷。
            # 上一份请求里就没有的（裁切后重建、从没进过 prompt）同样不命中，也记冷。
            cold += 1
            flags.append("c" if self._msg_sig(m) in prev_set else "c")
        return cold, fresh, "".join(flags)

    @staticmethod
    def _msg_sig(msg) -> str:
        """一条消息在缓存里的身份：角色 + 正文前 400 字（v4.10）。

        跟 stage 里切尾部用的比对口径一致，两边不会打架。
        """
        try:
            if isinstance(msg, dict):
                role = str(msg.get("role") or "")
                content = msg.get("content")
            else:
                role = str(getattr(msg, "role", "") or "")
                content = getattr(msg, "content", "")
        except Exception:
            return ""
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except Exception:
                content = str(content)
        return role + "|" + content.strip()[:400]

    @staticmethod
    def _tail_equal(arr: list, inc: list) -> bool:
        """messages 的尾部是不是就是这批待发（v4.13）。

        决策快照有两种形态：待发已经并进窗口（尾部就是它们），或者待发还在
        pending 里（messages 里根本没有）。判断口径跟 dec_split 一致。
        """
        if not inc or not isinstance(arr, list) or len(arr) < len(inc):
            return False
        k = len(inc)
        for i in range(k):
            a = arr[len(arr) - k + i] or {}
            b = inc[i] or {}
            try:
                ca = str(a.get("content") or "").strip()[:400]
                cb = str(b.get("content") or "").strip()[:400]
            except Exception:
                return False
            if ca != cb:
                return False
        return True

    def _series_of(self, data: dict, kind: str = "") -> list:
        """一份快照真正发出去的整条序列（v4.13 / v4.15）。

        用户 m18728 第 2 条：决策请求实际发的是「窗口 + 待发」，但以前只用
        messages 做前缀比较——待发没并进窗口时那几条在下一轮就变成「冷」，
        明明上一轮刚请求过。决策系列把待发补到尾部再比。

        v4.15（用户 m19439）：基准换成「最近一次任意真实请求」之后，这里要按
        **这份快照自己的 outcome** 判断补不补待发，不能再听调用方的 kind——
        拿一份决策快照当回复轮的基准时，它同样带着待发。
        """
        arr = list(data.get("messages") or [])
        oc = str(data.get("outcome") or "") or str(kind or "")
        if oc == "decision":
            inc = data.get("incoming_messages") or []
            if isinstance(inc, list) and inc and not self._tail_equal(arr, inc):
                arr = arr + list(inc)
        return arr

    def _prev_series(self, sid: str, kind: str) -> tuple:
        """上一份请求真正发出去的 messages 指纹（v4.10 / v4.15）。

        返回 (文件名, [指纹...])；没有快照就返回 ("", [])。
        kind = "any"（v4.15 起 _prefix_of 用的就是它）认最近一次任意真实请求，
        决策与回复都算——上游 KV 缓存只认最后一次 prompt，上一轮是决策请求时
        它同样把整段缓存住了，只按「上一份同类」比会把刚请求过的消息重新算成
        冷（用户 m19439）。
        kind = "decision" 只认 outcome == "decision"；"reply" 跳过 decision
        与 trim —— 裁切快照没有模型调用，拿它当前缀会把整段都算成命中。
        """
        key = (sid, str(kind or "reply"))
        if key in self._prev_cache:
            return self._prev_cache[key]
        out: tuple = ("", [])
        folder = self.dir / _safe_sid(sid)
        try:
            files = sorted(folder.glob("*.json"), reverse=True)
        except Exception:
            files = []
        for f in files[:60]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            oc = str(data.get("outcome") or "")
            if key[1] == "decision":
                if oc != "decision":
                    continue
            elif key[1] == "any":
                # 只排除裁切快照：它没有模型调用。空 outcome 的老快照是正常请求，
                # 不能当坏文件跳过，否则基准会退到更早的一份（v4.15）。
                if oc == "trim":
                    continue
            elif oc in ("decision", "trim"):
                continue
            arr = self._series_of(data)
            if not isinstance(arr, list) or not arr:
                continue
            out = (f.name, [self._msg_sig(m) for m in arr])
            break
        self._prev_cache[key] = out
        return out

    def _trim_between(self, sid: str, prev_name: str) -> bool:
        """基准快照之后、这一份之前有没有发生过裁切（v4.26 用户 m22281）。

        裁切会把窗口里的消息重排/搬位置，公共前缀必然从某一处断掉。页面上那条
        黄色说明要能说清「这是裁切造成的」，说不清就只写「不命中缓存」。
        """
        if not prev_name:
            return False
        folder = self.dir / _safe_sid(sid)
        try:
            files = sorted(folder.glob("*.json"), reverse=True)
        except Exception:
            return False
        for f in files:
            if f.name == prev_name:
                return False
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("outcome") or "") == "trim":
                return True
        return False

    def recalc_cache(self, sid: str, name: str, data: dict) -> dict:
        """按当前口径重算一份快照的「命中 / 不命中 / 本轮输入」边界（v4.26 用户 m22251）。

        老快照里存的 cache_cold_len / cache_tail_flags 是旧规则算出来的，页面直接读
        就一直显示旧边界。详情返回前用与落盘同一套基准重算，历史行也跟着对。
        """
        oc = _outcome_of(data)
        if oc == "trim":
            return {}
        folder = self.dir / _safe_sid(sid)
        try:
            files = sorted(folder.glob("*.json"), reverse=True)
        except Exception:
            return {}
        prev_pair = None
        seen_self = False
        for f in files:
            if f.name == name:
                seen_self = True
                continue
            if not seen_self:
                continue  # 只看比这一份更早的
            try:
                pd = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            o = _outcome_of(pd)
            if oc == "decision":
                if o == "trim":
                    continue
            elif o in ("decision", "trim"):
                continue
            prev_pair = (f.name, pd)
            break
        if prev_pair is None:
            return {}
        # v4.28（用户 m22895）：当前这份只看它自己的 messages——_series_of 对决策
        # 快照会把 incoming_messages 补到尾部（那是当时还没并进窗口的待发），
        # 页面 ② 画的却是 messages：补上去 flags 比消息多几条，前端整串丢弃、
        # 退回老二分口径，② 末尾就冒出两条本不该有的「不命中」。
        cur_msgs = list(data.get("messages") or [])
        prev_msgs = self._series_of(prev_pair[1])
        if not cur_msgs or not prev_msgs:
            return {}
        # 前缀比的是指纹（跟落盘时一致），_tail_split 要原始消息自己取指纹。
        prev_ser = [self._msg_sig(m) for m in prev_msgs]
        cur_sigs = [self._msg_sig(m) for m in cur_msgs]
        pre = self._common_prefix(prev_ser, cur_sigs)
        cold, fresh, flags = self._tail_split(prev_ser, cur_msgs, pre)
        return {
            "cache_prefix_len": pre,
            "cache_cold_len": cold,
            "cache_input_len": fresh,
            "cache_tail_flags": flags,
            "cache_prefix_of": prev_pair[0],
            "cache_trim_between": self._trim_between(sid, prev_pair[0]),
        }

    @staticmethod
    def _common_prefix(prev: list, cur: list) -> int:
        """两条消息序列从头开始逐条相同到第几条（v4.10）。"""
        n = 0
        for a, b in zip(prev or [], cur or []):
            if a != b:
                break
            n += 1
        return n

    def _prefix_of(self, sid: str, kind: str, messages: list, tail: list = None) -> dict:
        """这一份快照跟上一份同类快照的公共前缀，落盘用（v4.10）。

        v4.13（用户 m18728 第 2 条）：tail 是本轮待发。跟 _series_of 同一口径，
        待发没并进 messages 时补到尾部再比，否则刚请求过的那几条会被算成冷。
        """
        # v4.15（用户 m19439）：基准换成「最近一次任意真实请求」。上一轮是决策
        # 请求时，那些消息同样已经被上游缓存，只跟「上一份同类」比会把它们全
        # 算成没请求过。kind 只留给 _series_of 兜底，不再决定基准是谁。
        # v4.23（用户 m21976）：回复轮的基准退回「上一份回复请求」。
        # 上一轮若是决策请求，那批消息只发给了决策模型，主模型的缓存里没有；
        # 拿决策快照当基准会把它们算成冷（用户看到「上一份同类请求里没有这些」）。
        # 决策轮仍用 any，保持 v4.15 的修复（用户 m19439）。
        use_kind = "reply" if str(kind or "") == "reply" else "any"
        prev_name, prev_series = self._prev_series(sid, use_kind)
        if not prev_series and use_kind != "any":
            prev_name, prev_series = self._prev_series(sid, "any")
        if not prev_series:
            return {}
        cur_msgs = list(messages or [])
        if tail and not self._tail_equal(cur_msgs, tail):
            cur_msgs = cur_msgs + list(tail)
        cur = [self._msg_sig(m) for m in cur_msgs]
        prefix_len = self._common_prefix(prev_series, cur)
        info = {
            "cache_prefix_len": prefix_len,
            "cache_prefix_of": prev_name,
            "cache_prefix_base": len(prev_series),
        }
        # v4.26（用户 m22281）：不命中是不是裁切造成的，写进快照供页面说明。
        try:
            info["cache_trim_between"] = self._trim_between(sid, prev_name)
        except Exception:
            info["cache_trim_between"] = False
        # v4.14（用户 m19095）：前缀之后那段还要再分一次——上一次请求之后新进来
        # 的群消息与决策注记，是这一轮要送进去的输入，不能算冷。
        # v4.15（用户 m19439）：决策轮也走这一套。上一轮请求过、这轮却对不上前缀
        # 的才是冷；从没请求过的算本轮输入。两边口径统一，页面才不会出现
        # 「决策把上次的输入当冷」。
        try:
            cold, fresh, flags = self._tail_split(prev_series, list(messages or []), prefix_len)
            info["cache_cold_len"] = cold
            info["cache_input_len"] = fresh
            info["cache_tail_flags"] = flags
        except Exception:
            # 记账出错不能拖累真实请求：这三个键不写，前端自动退回 v4.10 的二分。
            pass
        return info

    @staticmethod
    def _assemble(
        static_text: str,
        messages: list,
        relocated_text: str,
        up_parts: list,
        tail: bool = False,
        up_seq: Optional[list] = None,
    ) -> list:
        """按真实发送顺序拼出「模型最终看到的那一条请求」。

        core/provider/llm_model.py 的 assemble_prompt 是这么组的：
          ① system = 静态段拼接（放在第 0 条）
          ② 历史 messages 原样接在后面
          ③ 最后一条 user = <system_reminder> 包住的动态段 + 本批消息（拼成同一条）
        这里如实还原，排错时看到的就是真正发出去的东西。

        tail=True 是插件自己挪过之后的形态：动态段被放到本批消息后面，
        这样上一轮正文在历史里的位置不变，前缀缓存能完整命中。
        """
        out: list[dict] = []
        if static_text:
            out.append({"role": "system", "content": static_text, "tag": "system"})
        for m in messages:
            if isinstance(m, dict):
                d = dict(m)
            else:
                d = {
                    "role": getattr(m, "role", ""),
                    "content": _plain_content(getattr(m, "content", "")),
                }
            d["tag"] = "history"
            out.append(d)
        incoming = "".join(text for _, text in up_parts)
        # up_seq 是 request.user_prompt 的真实顺序：正文、框架挪过来的动态段、
        # 插件拼在最下面的动态段都在里面。有它就照它拼——真实请求里「格式强化」
        # 本来就在 <system_reminder> 之后，不要再按「先正文再动态段」自己重排
        # （用户 m06046 图4：记录页把顺序搞反，看着像没注入到最下面）。
        seq_chunks = []
        for _nm, _t in (up_seq or []):
            if _nm == "dynamic_context_start":
                # 标记段的正文有时是空串（标记只挂在 name 上），这里补回标签，
                # 保证记录页看到的就是真正发出去的那段文本。
                seq_chunks.append(_t if str(_t).strip() else "<system_reminder>\n")
            elif _nm == "dynamic_context_end":
                seq_chunks.append(_t if str(_t).strip() else "</system_reminder>\n")
            else:
                seq_chunks.append(_t)
        seq_text = "".join(seq_chunks)
        body = ""
        if seq_text.strip():
            body = seq_text
        elif tail:
            # 动态段挪到了本轮消息之后
            body = incoming
            if relocated_text:
                body += "<system_reminder>\n" + relocated_text + "\n</system_reminder>\n"
        else:
            if relocated_text:
                body += "<system_reminder>\n" + relocated_text + "\n</system_reminder>\n"
            body += incoming
        if body.strip():
            out.append({"role": "user", "content": body, "tag": "incoming"})
        return out

    def stage(
        self,
        sid: str,
        request,
        trigger_reason: str = "",
        dynamic_position: str = "latest_user",
        memory_position: str = "system",
        window_total: int = 0,
        trigger_count: int = 0,
        count_threshold: int = 0,
        trigger_prob: float = 0.0,
        event_id: str = "",
        window_user: int | None = None,
        incoming_rows: Optional[list] = None,
        decision_passed: bool = False,
    ) -> None:
        """ON_LLM_REQUEST 时暂存，等结算时补上 replied 再落盘。

        window_user 传 None 时才从 request.messages 里数（v3.24，用户 m45932）：
        那一份是框架历史，**不含本轮新进来的 user 消息**，柱子就会比快照少一截
        （实测柱子 55、快照 65）。调用方把窗口里当前用户条数传进来就对得上了。


        window_total 是「这一轮时窗口里到底有多少条」，跟实际发出去的
        messages 条数不是一回事：群聊冷启动那轮 messages 是空的，窗口却
        已经记下了本轮消息，只记 len(messages) 页面上就永远显示 0 条
        （用户 m25454 的截图）。

        `dynamic_position` / `memory_position` 取自框架 bot 配置：`latest_user` 时
        sessions / chat_env / time（以及 memory）会在组装阶段被挪到最后一条 user
        消息，**不进 system**。这里据此把「真正留在 system 的部分」与「会被挪走
        的部分」分开记，免得看记录的人误以为时间之类的动态段占着缓存前缀。
        """
        if not self.enabled or request is None:
            return
        try:
            messages = [_to_dict(m) for m in (getattr(request, "messages", None) or [])]
        except Exception:
            messages = []

        # v4.06（用户 m12879）：本轮消息在这一轮也进了窗口（判要回时先并进、再落
        # 决策注记），快照 ② 历史里就会把它们一起画出来，跟 ③ 重复、也看不出
        # 「本轮 user → 决策理由」这个结构。窗口尾部正好是本轮的 [user × N, 注记]，
        # 比对得上就从历史里切出去，单独记成 incoming_messages。
        round_rows = []
        for _r in (incoming_rows or []):
            try:
                _d = _to_dict(_r)
            except Exception:
                continue
            if isinstance(_d, dict) and str(_d.get("content") or "").strip():
                round_rows.append(_d)
        hist_messages = messages
        if round_rows and len(messages) >= len(round_rows):
            k = len(round_rows)
            keys = [_row_key(b.get("content")) for b in round_rows]
            # 逐条对齐尾部是不可靠的：窗口里顺序是「先并本轮 → 再落判定注记 →
            # 最后才是 bot 回复」，快照里却常常只有 [本轮, 注记]，两边差一条，
            # zip 一配就全错，切分永远失败（用户本轮：回复记录 ③ 里同一条 @
            # 出现两次）。改成从尾部往回找「连续 k 条与本轮完全相同」的那一段。
            cut = None
            for start in range(len(messages) - k, -1, -1):
                ok = True
                for i in range(k):
                    a = messages[start + i]
                    ca = a.get("content") if isinstance(a, dict) else getattr(a, "content", "")
                    if _row_key(ca) != keys[i]:
                        ok = False
                        break
                if ok:
                    cut = start
                    break
            if cut is not None:
                hist_messages = messages[:cut]
        tail_split = hist_messages is not messages
        names = []
        static_names: list[str] = []
        relocated_names: list[str] = []
        static_parts: list[str] = []
        relocated_parts: list[str] = []
        system_text = ""
        system_errors = []
        try:
            raw_list = list(getattr(request, "system_prompt", None) or [])
        except Exception:
            raw_list = []
        parts = []
        sys_parts: list[dict] = []
        for idx, p in enumerate(raw_list):
            try:
                nm = getattr(p, "name", "") or ""
            except Exception as exc:
                nm = "?"
                system_errors.append(f"{idx}:name {type(exc).__name__}: {exc}")
            names.append(nm)
            try:
                to_string = getattr(p, "to_string", None)
                text = to_string() if callable(to_string) else str(p)
                parts.append(text)
                moved = _relocated(nm, dynamic_position, memory_position)
                sys_parts.append({"name": nm, "chars": len(text), "relocated": moved})
                if moved:
                    relocated_names.append(nm)
                    relocated_parts.append(text)
                else:
                    static_names.append(nm)
                    static_parts.append(text)
            except Exception as exc:
                tpl = ""
                try:
                    tpl = str(getattr(p, "content", "") or "")[:3000]
                except Exception:
                    tpl = ""
                system_errors.append(
                    f"{idx}:{nm} {type(exc).__name__}: {exc} | tpl={tpl!r}"
                )
                parts.append("")
                moved = _relocated(nm, dynamic_position, memory_position)
                sys_parts.append({"name": nm, "chars": 0, "relocated": moved})
                if moved:
                    relocated_names.append(nm)
                else:
                    static_names.append(nm)
        system_text = "".join(parts)
        user_prompt = ""
        up_parts: list[tuple[str, str]] = []
        up_dynamic: list[tuple[str, str]] = []
        # 用户 m06046 图4：request.user_prompt 里各段的顺序就是框架拼最后一条 user 的真实
        # 顺序（正文 → <system_reminder> 包住的动态段 → 插件拼在最下面的动态段）。
        # 整条序列记下来，展示时按它拼，不再自己猜谁该在前。
        up_seq: list[tuple[str, str]] = []
        try:
            for p in getattr(request, "user_prompt", None) or []:
                to_string = getattr(p, "to_string", None)
                text = to_string() if callable(to_string) else str(p)
                nm = getattr(p, "name", "") or ""
                up_seq.append((nm, text))
                if (nm in TAIL_MARK_NAMES or nm in DYNAMIC_PROMPT_NAMES
                        or nm == MEMORY_PROMPT_NAME or nm == DYNAMIC_TAIL_NAME):
                    # 插件把动态段挪到了本轮消息之后（dynamic_to_tail），
                    # 这几段得算「动态段」，不能混进「本轮消息」里。
                    up_dynamic.append((nm, text))
                else:
                    up_parts.append((nm, text))
                    # user_prompt 只记「本轮消息」本身；动态段单独记在 system_relocated，
                    # 否则前端两块会把同一段文字画两遍。
                    user_prompt += text
        except Exception:
            user_prompt = ""
        static_text = "".join(static_parts)
        # 用户 m06428 图5：回复轮这边 _round_rows 有时取不到（窗口尾部那条已被打散 / 合并），
        # incoming_rows 就成了空，记录里没有 incoming_messages；前端只能先用 user_prompt_parts
        # 画一遍本轮，又按 cache_tail_flags 从历史尾部搬一遍 → 同一条 @ 出现两次。
        # 这里用 request.user_prompt 里「本轮消息」那几段兜底重建，并在尾部比对成功时
        # 把本轮从历史里切出去（跟上面 incoming_rows 那条路一个结果）。
        if not tail_split:
            cand = [{"role": "user", "content": t} for nm, t in up_parts
                    if str(t or "").strip() and nm not in TAIL_MARK_NAMES]
            if cand:
                round_rows = cand
                hist_messages = messages
                # 文本会被框架改写过（@昵称 → [At 123(nickname: …)]、图片 → [图片]），
                # 逐字比对认不出同一条，所以先认 message_id：本轮那几条的 id 在历史尾部
                # 都找得到，就从最靠前的那个位置切下去（连后面插件写的判定注记一起切）。
                want: list[str] = []
                for _c in cand:
                    for _wid in re.findall(r"message_id:\s*(-?\d+)", str(_c.get("content") or "")):
                        if _wid not in want:
                            want.append(_wid)
                cut = None
                if want:
                    found: dict[str, int] = {}
                    _idx = len(messages) - 1
                    while _idx >= 0 and len(found) < len(want):
                        _cur = messages[_idx]
                        _body = str((_cur.get("content") if isinstance(_cur, dict)
                                     else getattr(_cur, "content", "")) or "")
                        for _wid in want:
                            if _wid not in found and ("message_id: %s" % _wid) in _body:
                                found[_wid] = _idx
                        _idx -= 1
                    if len(found) == len(want):
                        cut = min(found.values())
                if cut is None:
                    k = len(cand)
                    if len(messages) >= k:
                        same = True
                        for i in range(k):
                            got = messages[len(messages) - k + i]
                            body = got.get("content") if isinstance(got, dict) else getattr(got, "content", "")
                            if _row_key(body) != _row_key(cand[i]["content"]):
                                same = False
                                break
                        if same:
                            cut = len(messages) - k
                if cut is not None:
                    hist_messages = messages[:cut]
                tail_split = hist_messages is not messages
        tail_dynamic = bool(up_dynamic)
        # 挪过来的动态段：标签（<system_reminder>）不算内容，正文补进动态段统计
        tail_body = [(nm, t) for nm, t in up_dynamic if nm not in TAIL_MARK_NAMES]
        for nm, text in tail_body:
            if nm not in relocated_names:
                relocated_names.append(nm)
            sys_parts.append({"name": nm, "chars": len(text), "relocated": True})
        # 用户 m06428 图1：顺序照真实请求来——插件拼的动态段（强化格式）在前，
        # 框架搬到末尾的记忆 / 时间在后，最后一条才是系统动态段。
        # 用户 m07111 图2：各段以前是空串相接，「…群里不会出现任何东西。## 核心记忆（Core Memory）」
        # 糊在同一行，前端认不出那是标题，看着像记忆被粘在规则句尾；只有标题没有正文的段
        # （那次会话确实没记忆）也挤成一团。改成段与段之间空一行，空段直接不占位。
        relocated_text = _join_reloc_blocks(
            [t for _, t in tail_body] + list(relocated_parts))
        incoming_chars = (sum(_msg_chars(r) for r in round_rows) if tail_split
                          else sum(len(t) for _, t in up_parts))
        history_chars = 0
        for m in hist_messages:
            try:
                history_chars += _msg_chars(m)
            except Exception:
                pass
        # v4.10（用户 m17328 第 2 条）：历史消息不是全都命中。跟上一份真回复比，
        # 从头能对上多少条才是命中的；裁切把消息搬走了，那之后的一条都对不上。
        prefix_info = self._prefix_of(sid, "reply", messages)
        sections = [
            {"key": "system", "label": "系统提示词", "chars": len(static_text)},
            {"key": "history", "label": "历史消息", "chars": history_chars},
            {"key": "incoming", "label": "本轮消息", "chars": incoming_chars},
            {"key": "relocated", "label": "动态段（时间/会话/记忆）", "chars": len(relocated_text)},
        ]
        assembled = self._assemble(
            static_text, messages, relocated_text, up_parts, tail_dynamic, up_seq
        )
        # 这一轮真正发出去的东西有多大（文本 JSON 的 UTF-8 字节数）。
        # 页面原来只有 token 和字符数，用户想直接看到体积（用户 m32956 图7）。
        # 注意：图片在发送前才被框架换成 base64，这里算的是文本部分。
        try:
            payload_bytes = len(
                json.dumps(
                    [{"role": a.get("role"), "content": a.get("content")} for a in assembled],
                    ensure_ascii=False,
                ).encode("utf-8")
            )
        except Exception:
            payload_bytes = 0
        # This is a forecast of the host's later native injection, not a wire capture.
        from ..window.split import with_owned_images
        from ..window.buffer import ids_of
        media_by_id = getattr(request, "_mdp_incoming_media", None) or {}
        media_sid = str(getattr(request, "_mdp_media_raw_sid", "") or "")
        # 用户 m08158：快照里的行只有 role/content，窗口里的 media / decnote 全丢，
        # 页面就看不出这次决策注记、也画不出刚发的图。落盘前补一次。
        try:
            _decorate_rows(messages, media_by_id)
            _decorate_rows(round_rows, media_by_id)
        except Exception:
            logger.debug("[multi-decision] 快照行补标记失败", exc_info=True)
        native_projection = []
        incoming_with_native = []
        refs = []
        for nm, text in up_parts:
            if nm != "message":
                continue
            owned = [r for mid in ids_of([{"role": "user", "content": text}])
                     for r in media_by_id.get(mid, [])]
            content = with_owned_images(text, owned, media_sid)
            incoming_with_native.append({"role": "user", "content": content, "name": nm})
            if isinstance(content, list):
                typed = [p for p in content if isinstance(p, dict) and p.get("type") == "kira_image_ref"]
                if typed:
                    native_projection.append({"text": text, "refs": typed})
                    refs.extend(typed)
        assembled_with_native = [dict(item) for item in assembled]
        if refs:
            refs = list({r["path"]: r for r in refs}.values())
            for item in assembled_with_native:
                if item.get("tag") == "incoming":
                    item["content"] = [{"type": "text", "text": item["content"]}] + refs
        has_native_context = hasattr(request, "_mdp_incoming_media")
        metrics = payload_metrics({"assembled_with_native": assembled_with_native} if has_native_context
                                  else {"assembled": assembled})
        metrics["payload_estimate_source"] = ("request_time_native_projection_reference_files"
                                              if has_native_context else "request_time_reference_files")
        # Identity is captured at request time, never borrowed from a later final.
        now = time.time()
        for key, pending in list(self._pending.items()):
            if now - pending.get("ts", now) > 900:
                self._pending.pop(key, None)
        self._pending[(sid, str(event_id or ""))] = {
            "ts": now,
            "sid": sid,
            "event_id": str(event_id or ""),
            "trigger_reason": trigger_reason,
            # v4.11（用户 m17596 第 6 条）：这一轮是先过进阶决策、判定要回才发的。
            # 触发列就写「决策触发」，原来的触发原因降成副标。
            "decision_passed": bool(decision_passed),
            "trigger_count": int(trigger_count or 0),
            "count_threshold": int(count_threshold or 0),
            # 用户 m15045：概率/曲线模式触发时，把当时掷到的那一档概率一起存，
            # 列表里就不用拿「条数/阈值」硬套概率模式了。
            "trigger_prob": float(trigger_prob or 0.0),
            "messages": messages,
            "system_prompt_names": names,
            "system_prompt_count": len(names),
            "system_prompt_errors": system_errors,
            "system_prompt": system_text,
            "system_static": "".join(static_parts),
            "system_static_names": static_names,
            # 动态段可能仍在 system 里（head），也可能已被挪到本轮消息之后（tail），两者都要算
            "system_relocated": relocated_text,
            "system_relocated_names": relocated_names,
            "prompt_positions": {
                "dynamic": dynamic_position,
                "memory": memory_position,
                # head = 框架默认（动态段在本轮消息前面）；tail = 插件挪到了后面
                "actual": "tail" if tail_dynamic else "head",
            },
            "user_prompt": user_prompt,
            # 拆好的本轮消息（一条一个 Prompt）。user_prompt 是为了兼容老页面
            # 拼起来的整串，其实每条早就分开发给模型了；页面按这个逐条画，
            # 才不会把三十条群消息显示成一大坨（用户 m24807）。
            "user_prompt_parts": [{"name": nm, "text": text} for nm, text in up_parts],
            "window_size": len(messages),
            # 窗口真实条数（含本轮刚记进去的那条），页面上「窗口」列用这个
            "window_total": int(window_total or 0),
            # 真实消息条数：一条 user 里可能装着十几条群消息（框架把一批拼成一条）
            "window_counted": _count_messages(messages),
            # 只数用户侧（role=user）：走势图柱子悬浮报的是「群里说了多少条」，
            # bot 自己的回复不占窗口位数（用户 m42220）。快照标题仍用含 bot 的 counted。
            # v3.24：标一下这个数是「窗口里当场数的」，老记录没有这个键，
            # 读的时候要补上本轮输入（见 list_rounds 里的兜底）。
            "window_user_src": "window" if window_user is not None else "history",
            "window_user": (
                int(window_user)
                if window_user is not None
                else _count_messages(
                    [
                        m
                        for m in messages
                        if str(
                            (m.get("role") if isinstance(m, dict) else getattr(m, "role", "")) or ""
                        )
                        == "user"
                    ]
                )
            ),
            "assembled": assembled,
            **({"assembled_with_native": assembled_with_native,
                "native_projection": native_projection,
                "incoming_with_native": incoming_with_native,
                "metric_stage": "native_projection"} if has_native_context else {}),
            **({"incoming_messages": round_rows,
                "history_messages": hist_messages} if tail_split else {}),
            **prefix_info,
            "sections": sections,
            "total_chars": sum(s["chars"] for s in sections),
            "payload_bytes": payload_bytes,  # Legacy JSON/reference byte count.
            **metrics,
            "system_parts": sys_parts,
        }

    # ------------------------------------------------------------------ 落盘

    def flush(
        self,
        sid: str,
        replied: bool = False,
        reply_bubbles: int = 0,
        event_id: str = "",
        extra: Optional[dict] = None,
    ) -> Optional[str]:
        key = (sid, str(event_id or ""))
        # Compatibility for callers that staged without an event id. Never
        # consume another explicitly identified request when a final is late.
        if key not in self._pending and (sid, "") in self._pending:
            key = (sid, "")
        if not self.enabled:
            self._pending.pop(key, None)
            return None
        record = self._pending.pop(key, None)
        if record is None:
            return None
        record["replied"] = bool(replied)
        record["reply_bubbles"] = int(reply_bubbles)
        record["event_id"] = event_id
        record["summary_present"] = False
        record["failed"] = False
        record["outcome"] = "replied" if replied else "no_reply"
        record["error"] = ""
        if extra:
            record.update(extra)
        return self._save_round(sid, record, ledger=True)

    def _save_round(self, sid: str, record: dict, ledger: bool = True) -> Optional[str]:
        """把一份快照记录落盘。flush 与「裁切快照」（write_trim_snapshot）共用。

        v3.75（用户本轮第 5 条）：抽出来是为了让裁切快照走同一套命名、防撞名与
        数量裁剪；ledger=False 时不进账本——账本是给「请求次数/成功率」统计用的，
        一次纯裁切不能算成一轮请求。
        """
        sid = str(sid or "")
        try:
            folder = self.dir / _safe_sid(sid)
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{int(record['ts'] * 1000)}.json"
            path = folder / name
            if path.exists():
                suffix = hashlib.sha256(str(record.get("event_id") or "").encode()).hexdigest()[:12]
                path = folder / f"{int(record['ts'] * 1000)}_{suffix}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            self._names[(sid, str(record.get("event_id") or ""))] = path.name
            # v4.10（用户 m17328）：这份落盘了，它就是下一轮的「上一次」。
            # 不在这里刷新的话，下一轮算前缀时读到的还是上上次那份。
            try:
                oc = str(record.get("outcome") or "")
                if oc != "trim":
                    kind = "decision" if oc == "decision" else "reply"
                    arr = self._series_of(record)
                    if isinstance(arr, list) and arr:
                        sigs = [self._msg_sig(m) for m in arr]
                        self._prev_cache[(sid, kind)] = (path.name, sigs)
                        # v4.15：前缀基准改成「最近一次任意真实请求」，这里要一起刷新，
                        # 否则本轮 stage 时缓存下来的空值会让下一轮永远拿不到基准。
                        self._prev_cache[(sid, "any")] = (path.name, sigs)
            except Exception:
                pass
            self._prune(folder, sid)
            # 账本先于返回写：文件会被 _prune 裁掉，账本不会。
            if ledger:
                self._append_ledger(sid, record)
            return str(path)
        except Exception:
            logger.exception("[multi-decision] 写请求上下文记录失败")
            return None

    def write_trim_snapshot(self, sid: str, messages: list, cut: dict,
                            view: list = None, pending_n: int = 0) -> str:
        """v3.75（用户本轮第 5 条）：非请求触发的裁切单独落一份快照。

        以前这种裁切只写 window 日志，趋势图上没有它：窗口在「实时快照」那根
        柱子里突然变短，看不出是哪一次裁的。这里补一份最小快照，前端就多一根
        独立柱子代表这次裁切。

        v3.88（用户 m05012 第 2 条）：messages 收**裁切之后**的窗口，窗口条数 / 用户
        条数 / 字符数都按这份快照自己记。v3.87 曾改成按裁切前记（想让柱子跟裁前一样
        高），但用户看图说得很清楚：这份快照本来就只有 26 条，没必要硬拔高。裁切前后
        的对比放在 trim_before / trim_after / trim_user_before 里给悬停用。

        v3.89（用户 m05321 第 1 条）：messages 里**带上待发**——「触发时候，当前这些
        消息应该变成快照，待发也要算进快照里，你可以在快照里标注这些是待发」。待发
        那几项由调用方在 view 里标 pending，页面据此加「待发」标记与虚线框；条数 /
        字符数也按整份快照（窗口 + 待发）算，柱子高度就是它真实的长度。
        """
        if not self.enabled:
            return ""
        now = float(cut.get("ts") or time.time())
        msgs = [_to_dict(m) for m in (messages or [])]
        pend_n = max(0, int(pending_n or 0))
        disp = list(view or [])
        record = {
            "ts": now,
            "sid": sid,
            "event_id": "trim-%d" % int(now * 1000),
            "trigger_reason": "trim",
            "trigger_count": int(cut.get("user_total_before") or 0),
            "count_threshold": 0,
            "trigger_prob": 0.0,
            "messages": msgs,
            # v3.89（用户 m05321）：前端渲染用这份（带 pending 标记）；老快照没有这个
            # 键，前端会退回按 messages 逐个拼
            "display_messages": disp,
            "system_prompt_names": [],
            "system_prompt_count": 0,
            "system_prompt_errors": [],
            "system_prompt": "",
            "system_static": "",
            "system_static_names": [],
            "system_relocated": "",
            "system_relocated_names": [],
            "user_prompt": "",
            "user_prompt_parts": [],
            # v3.88：这几项都按这份快照自己记（= 裁切之后），柱子高度就按 window_user 画
            "window_size": len(msgs),
            "window_total": _count_messages(msgs),
            "window_counted": _count_messages(msgs),
            "window_user_src": "window",
            "window_user": _count_messages(
                [m for m in msgs if str((m or {}).get("role") or "") == "user"]
            ),
            "assembled": [],
            "sections": [],
            "total_chars": _chars_of(msgs) or int(cut.get("after_chars") or 0),
            "payload_bytes": 0,
            "replied": False,
            "reply_bubbles": 0,
            "failed": False,
            "outcome": "trim",
            "error": "",
            "summary_present": False,
            # 裁切前后的条数，页面悬停能直接读到
            "trim_before": int(cut.get("before") or 0),
            "trim_after": int(cut.get("after") or 0),
            "trim_user_before": int(cut.get("user_before") or 0),
            "trim_pending_before": int(cut.get("pending_before") or 0),
            # 裁完剩多少（上面那些数已经是裁后的；这里留着给悬停写「裁前 70 → 26」）
            "trim_after_user": int(cut.get("user_after") or 0),
            # v3.89（用户 m05321 第 1 条）：快照里有多少条是待发、从第几条开始
            "trim_pending_count": pend_n,
            "trim_pending_from": max(0, len(msgs) - pend_n) if pend_n else 0,
            # 这份快照自己的用户条数（含待发）。list_rounds 优先用它——老快照没这个键，
            # 才退回 trim_after_user（那是裁后窗口、不含待发）
            "trim_snapshot_user": _count_messages(
                [m for m in msgs if str((m or {}).get("role") or "") == "user"]
            ),
            "trim_only": True,
        }
        return self._save_round(sid, record, ledger=False) or ""

    def write_decision_snapshot(self, sid: str, rec: dict) -> str:
        """v3.91（用户 m05749）：进阶决策这一层单独落一份快照。

        两段式里第一段（决策模型读实时窗口判要不要回）在框架的 llm_request 之前
        就结束了，正常快照根本没有它。用户要「在当前窗口和最近请求总结新增一个
        决策记录，和请求快照用一样的模板，只是系统提示词换成决策用的提示词」，
        所以这里照请求快照的键位写一份，只把 ① 换成决策提示词、③ 换成决策的
        聊天记录，另加 decision_* 字段给页面画结论。

        ledger=False：决策不是一次请求，不进「请求次数 / 成功率」的账本。
        """
        if not self.enabled:
            return ""
        data = dict(rec or {})
        now = float(data.get("ts") or time.time())
        msgs = [_to_dict(m) for m in (data.get("messages") or [])]
        # v3.97（用户 m09615 图9）：③ 只放这一轮新来的那批（决策时还挂在待发里的），
        # 以前塞的是整段 transcript，等于把 ② 又铺了一遍，转发消息还会被拆成好几条。
        inc = [_to_dict(m) for m in (data.get("incoming") or [])]
        # v4.12（用户 m18246/m18275 第 2 条）：②③ 的展示投影。engine 那边用
        # view_of 算好了（跟回复快照同一套，带 media），这里只负责接住并按同样的
        # 切法同步切一份——否则 dec_split 之后 ② 会拿全量投影去对切过的历史，
        # 图片和文字一起错位。条数对不上就整份退回原始消息，宁可没图也不错位。
        disp = [_to_dict(m) for m in (data.get("display_messages") or [])]
        if len(disp) != len(msgs):
            disp = list(msgs)
        inc_disp = [_to_dict(m) for m in (data.get("incoming_display_messages") or [])]
        if len(inc_disp) != len(inc):
            inc_disp = list(inc)
        # v4.10（用户 m17328 第 1 条）：决策这一份的 ② 只放真历史，本轮那几条
        # 只出现在 ③，不然同一批消息画两遍。切法与回复快照一致：从尾部逐条比
        # content 前 400 字，全等才切。
        hist_msgs = msgs
        hist_disp = disp
        dec_split = False
        if inc and len(msgs) >= len(inc):
            k = len(inc)
            same = True
            for i in range(k):
                a = msgs[len(msgs) - k + i]
                b = inc[i]
                if (str(a.get("content") or "").strip()[:400]
                        != str(b.get("content") or "").strip()[:400]):
                    same = False
                    break
            if same:
                hist_msgs = msgs[:len(msgs) - k]
                hist_disp = disp[:len(disp) - k]
                dec_split = True
        # 决策是 fast 模型、回复是主模型，缓存不共享，所以这里只跟上一次决策比。
        dec_prefix = self._prefix_of(sid, "decision", msgs, inc)
        parts = [str(x) for x in (data.get("system_parts") or []) if str(x).strip()]
        names = []
        for part in parts:
            head = part.strip().splitlines()[0] if part.strip() else ""
            head = head.strip("【】 ").strip()
            names.append(head or "提示词")
        system_text = "\n\n".join(parts)
        transcript = str(data.get("transcript") or "")
        # 用户 m06046 图3：决策请求真正的 user 内容是「聊天记录 + 末尾动态段」。
        # 以前这里只写 transcript，页面上永远看不到动态段，看着像「开了也没注入」。
        dyn_text = str(data.get("dynamic_text") or "")
        dyn_parts = [p for p in (data.get("dynamic_parts") or [])
                     if isinstance(p, dict) and str(p.get("text") or "").strip()]
        dyn_names = [str(p.get("name") or "") for p in dyn_parts]
        user_text = str(data.get("user_prompt") or "")
        if not user_text:
            user_text = transcript + (("\n\n" + dyn_text) if dyn_text.strip() else "")
        body = [m for m in msgs if str((m or {}).get("role") or "") in ("user", "assistant")]
        # v3.93（用户 m07570 图7）：决策记录换成回复记录同款模板——① 决策提示词
        # ② 聊天历史 ③ 本轮输入 ④ agent 轨迹。以前 sections 给空数组，页面只能拿
        # 兜底字数，③④ 也被 isDecSnap 整段跳过，看起来就只有两块。
        in_tok = int(data.get("input_tokens") or 0)
        out_tok = int(data.get("output_tokens") or 0)
        cached = int(data.get("cached_tokens") or 0)
        elapsed = float(data.get("elapsed") or 0.0)
        cache_hit = round(cached * 100.0 / in_tok, 1) if (in_tok and cached) else None
        dec_trace = {
            "steps": [{
                "index": 1,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cached_tokens": (cached or None),
                "cache_hit": cache_hit,
                "time_consumed": (round(elapsed, 3) if elapsed else None),
                "reasoning": "",
                "tool_calls": [],
                "tool_results": [],
                # 决策模型那一步的原始输出（JSON 里的 reply/reason/say 就是它的三个键）
                "text": str(data.get("raw") or ""),
            }],
            "totals": {"input_tokens": in_tok, "output_tokens": out_tok,
                       "cache_hit": cache_hit},
            "tools": list(data.get("tools_used") or []),
            "elapsed": (round(elapsed, 3) if elapsed else 0),
        }
        record = {
            "ts": now,
            "sid": sid,
            "event_id": "dec-%d" % int(now * 1000),
            "trigger_reason": "decision",
            "trigger_count": _count_messages(body),
            "count_threshold": 0,
            # v3.98（用户 m10169 图6）：决策表的「触发」列要写当时的触发概率，
            # 以前这里写死 0，页面只能写「决策模式」。
            "trigger_prob": float(data.get("trigger_probability") or 0.0),
            "messages": msgs,
            "display_messages": disp,
            "incoming_messages": inc,
            "incoming_display_messages": inc_disp,
            **({"history_messages": hist_msgs} if dec_split else {}),
            **({"history_display_messages": hist_disp} if dec_split else {}),
            **dec_prefix,
            "system_prompt": system_text,
            "system_static": system_text,
            "system_prompt_names": names,
            "system_static_names": names,
            "system_prompt_count": len(parts),
            "system_prompt_errors": [],
            "system_relocated": dyn_text,
            "system_relocated_names": dyn_names,
            # 用户 m06428 图4：决策的动态段（记忆 / 时间）是插件自己拼在本轮消息**后面**的，
            # 可记录里没有 prompt_positions，前端就当成「开头」，卡片画到了第一条上面。
            "prompt_positions": {"dynamic": "latest_user", "memory": "latest_user", "actual": "tail"},
            "user_prompt": user_text,
            # 决策没有框架意义上的「本轮新消息」，它读的是整段聊天记录（transcript），
            # 动态段（记忆/时间/会话）由插件拼在记录末尾（用户 m06046）。
            # v3.93（用户 m07570 图7）：这份就是 ③「本轮输入」，给成一条 part，
            # 页面才画得出卡片（v3.91 修过一次：塞字符串进来，接口按 dict 取 name 会炸）。
            "user_prompt_parts": (([{"name": "message", "text": transcript}]
                                   if transcript.strip() else [])
                                  + ([{"name": "dynamic_tail", "text": dyn_text}]
                                     if dyn_text.strip() else [])),
            "window_size": len(msgs),
            "window_total": _count_messages(msgs),
            "window_counted": _count_messages(msgs),
            "window_user": _count_messages(
                [m for m in msgs if str((m or {}).get("role") or "") == "user"]
            ),
            "window_user_src": "window",
            "assembled": [
                {"role": "system", "content": system_text, "tag": "system"},
                {"role": "user", "content": user_text, "tag": "incoming"},
            ],
            "sections": [
                {"key": "system", "label": "系统提示词", "chars": len(system_text)},
                {"key": "history", "label": "历史消息", "chars": _chars_of(hist_msgs)},
                {"key": "incoming", "label": "本轮消息", "chars": _chars_of(inc)},
                {"key": "relocated", "label": "动态段（时间/会话/记忆）", "chars": len(dyn_text)},
            ],
            # v3.94（用户 m08398 图6）：概览条按 total_chars 算各段占比，
            # 这里必须等于 sections 四段之和（以前只给了聊天历史的字符数，条会溢出）。
            "total_chars": len(system_text) + _chars_of(hist_msgs) + _chars_of(inc) + len(dyn_text),
            "payload_bytes": len((system_text + "\n\n" + user_text).encode("utf-8")),
            "replied": False,
            "reply_bubbles": 0,
            "failed": bool(data.get("failed")),
            "outcome": "decision",
            "error": str(data.get("error") or ""),
            "summary_present": False,
            # ---- 决策自己的结论与用量
            "decision_only": True,
            "decision_reply": bool(data.get("reply")),
            "decision_reason": str(data.get("reason") or "")[:200],
            "decision_say": str(data.get("say") or "")[:200],
            # v4.01（用户 m11166 图3）：决策表末列从「触发概率」改叫「触发类型」。
            # 决策这一轮是被谁叫起来的（count / probability / curve / mention / nickname…），
            # 概率类再配 trigger_prob 一起显示，@、叫昵称这种没有概率就写来源。
            "decision_source": str(data.get("trigger_reason") or ""),
            "decision_raw": str(data.get("raw") or "")[:4000],
            "decision_model": str(data.get("model") or ""),
            "decision_via": str(data.get("via") or ""),
            "decision_attempts": int(data.get("attempts") or 0),
            "decision_elapsed": float(data.get("elapsed") or 0.0),
            # 用户 m05124 第 8 条：决策轮也把「触发 → 真正开始判」的防抖等待存下来，
            # 记录页的「用时」列才能写成「防抖 7s + 2s」。
            "decision_debounce": float(data.get("debounce") or 0.0),
            "decision_tools": list(data.get("tools_used") or []),
            "decision_system_names": names,
            "decision_transcript": transcript,
            "input_tokens": int(data.get("input_tokens") or 0),
            "output_tokens": int(data.get("output_tokens") or 0),
            "cached_tokens": cached,
            # ④ agent 轨迹（决策模型那一次调用）。模板跟回复记录一样，
            # 页面 traceChips / traceHtml / traceRawText 都认这套键。
            "trace": dec_trace,
        }
        return self._save_round(sid, record, ledger=False) or ""

    # ---------------------------------------------- 调用轨迹合并（v3.64，用户 m18870）
    def attach_trace(self, sid: str, event_id: str, trace: dict) -> str:
        """把一轮的调用轨迹并进它的请求快照（用户 m18870：两份合成一份）。

        正常路径：这一轮还没 flush，pending 里那份记录直接带上 "trace"，落盘
        一次写完。异常路径（先 flush 后 finish）：读回文件补一个 "trace" 再原子
        替换。返回 "pending" / "merged" / ""（这一轮没有请求快照，轨迹只能丢）。
        """
        sid = str(sid or "")
        eid = str(event_id or "")
        record = self._pending.get((sid, eid))
        if record is not None:
            record["trace"] = trace
            return "pending"
        if not self.enabled or not eid:
            return ""
        name = self.name_of(sid, eid)
        if not name:
            return ""
        path = self.dir / _safe_sid(sid) / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        data["trace"] = trace
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return "merged"
        except Exception:
            logger.exception("[multi-decision] 合并调用轨迹失败")
            return ""

    def keep_for(self, sid: str = "") -> int:
        """这个会话该留几份旧请求（用户 m18059 第 4 条：缓存设置按会话独立）。

        优先问 main.py 传进来的回调（读这个群的有效设置 = 单群覆盖 > 全局），
        回调没给或抛异常就退回实例值（全局设置）。
        """
        if self._keep_of is not None:
            try:
                value = int(self._keep_of(sid) or 0)
                if value > 0:
                    return value
            except Exception:
                logger.debug("[multi-decision] 取会话请求保留份数失败", exc_info=True)
        return self.keep

    def dec_keep_for(self, sid: str = "") -> int:
        """这个会话该留几份决策快照（用户 m10782：决策快照是单独一类缓存）。

        口径同 keep_for：先问 main.py 传进来的回调（单群覆盖 > 全局），没给回调
        或取不到就退回实例值；实例值也没给（0）就跟回复快照用同一个数。
        """
        if self._dec_keep_of is not None:
            try:
                value = int(self._dec_keep_of(sid) or 0)
                if value > 0:
                    return value
            except Exception:
                logger.debug("[multi-decision] 取会话决策快照保留份数失败", exc_info=True)
        return self.dec_keep or self.keep

    @staticmethod
    def _is_decision_file(path) -> bool:
        """这份快照是不是决策快照（决策那一轮单独落的那份）。

        两者同目录、同命名（<毫秒时间戳>.json），唯一的分辨办法就是记录里的
        decision_only 标记。文件也就几 KB，直接找文本比 json.loads 快一个数量级。
        """
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return '"decision_only": true' in text

    def days_for(self, sid: str = "") -> int:
        """这个会话该留多少天的旧请求；0 = 不限。口径同 keep_for。"""
        if self._days_of is not None:
            try:
                return max(0, int(self._days_of(sid) or 0))
            except Exception:
                logger.debug("[multi-decision] 取会话请求保留天数失败", exc_info=True)
        return self.keep_days

    def _prune(self, folder: Path, sid: str = "") -> None:
        """裁掉这个会话的旧快照（用户 m13708 第 3 条：份数 + 天数两条线）。

        ① 天数线：keep_days > 0 时，早于 keep_days 天的文件删掉；0 = 不限。
        ② 份数线：**回复快照与决策快照各算各的上限**（v4.00，用户 m10782：
           决策快照是一类独立的缓存，不能被回复快照的份数挤掉，反之亦然）。
           两者同目录，靠 _is_decision_file 分堆；文件总数没超过「较小那个上限」
           时两堆必然都没超，直接返回、连文件都不用读——稳态下每条消息只多一次
           stat。（v4.00 修：一开始拿 keep + keep_dec 当闸门，等于把回复快照的份数
           线也放宽了一倍，留 3 份的会话会一直堆到 6 份。）
        文件名就是毫秒时间戳，按名字排序即按时间排序；刚写下 5 分钟内的先留着
        （那一轮可能还在跑 / 刚回完，删了页面点进去是一片空）。
        """
        keep = self.keep_for(sid)
        keep_dec = self.dec_keep_for(sid)
        keep_days = self.days_for(sid)
        grace_cut = time.time() - PRUNE_GRACE_SECONDS
        try:
            files = sorted(folder.glob("*.json"))
            if keep_days > 0:
                cutoff = time.time() - keep_days * 86400
                for path in files:
                    try:
                        if path.stat().st_mtime >= cutoff:
                            continue
                        # 刚写的也别当天数线带走（同一份 5 分钟宽限）
                        if path.stat().st_mtime >= grace_cut:
                            continue
                        path.unlink()
                    except OSError:
                        continue
                files = sorted(folder.glob("*.json"))
            if len(files) > min(keep, keep_dec):
                groups = {"reply": [], "decision": []}
                for path in files:
                    groups["decision" if self._is_decision_file(path) else "reply"].append(path)
                for name, cap in (("reply", keep), ("decision", keep_dec)):
                    overflow = max(0, len(groups[name]) - cap)
                    if overflow <= 0:
                        continue
                    for path in groups[name]:
                        if overflow <= 0:
                            break
                        try:
                            if path.stat().st_mtime >= grace_cut:
                                continue
                        except OSError:
                            continue
                        try:
                            path.unlink()
                            overflow -= 1
                        except OSError:
                            continue
        except Exception:
            logger.exception("[multi-decision] 清理请求记录失败")


    # ---------------------------------------------------- 事件号 -> 记录文件名

    def has_pending(self, sid: str, event_id: str) -> bool:
        """这一轮还停在内存里、没落过盘吗（异常/看门狗路径据此判断要不要补一条记录）。"""
        return (str(sid or ""), str(event_id or "")) in self._pending

    def name_of(self, sid: str, event_id: str) -> str:
        """按事件号反查这一轮的记录文件名（找不到返回空串）。"""
        event_id = str(event_id or "")
        if not self.enabled or not event_id:
            return ""
        sid = str(sid or "")
        cached = self._names.get((sid, event_id))
        if cached and (self.dir / _safe_sid(sid) / cached).is_file():
            return cached
        found = self._find_name(sid, event_id)
        if found:
            self._names[(sid, event_id)] = found
        return found

    def _find_name(self, sid: str, event_id: str, scan: int = 300) -> str:
        """索引是内存里的，重启后为空，从最近的文件往回扫一遍补上。"""
        folder = self.dir / _safe_sid(sid)
        try:
            files = sorted(folder.glob("*.json"), reverse=True)[: max(1, int(scan))]
        except Exception:
            return ""
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("event_id") or "") == event_id:
                return path.name
        return ""

    # 注意：请求记录一律只读。原错误轮那份文件停在 failed 那一刻是**证据**，
    # 不能被后来的重试改写（v3.04 契约 test_original_message_and_request_files_remain_unchanged）。
    # 「这轮后来重试了几次、成没成、回复落在哪一轮」由 retry_history.json 持久化，
    # 展示时用 name_of() 把事件号映射回文件名再拼出来（见 main.py 的 retry_outcome）。

    # ------------------------------------------------------------------ 读取

    def purge(self, sid: str) -> int:
        """删掉某个会话的请求记录目录，返回删掉的文件数。"""
        sid = str(sid or "").strip()
        if not sid:
            return 0
        # 账本先清：目录不存在时下面提前 return，账本行会留下来接着被统计。
        self._drop_ledger_sid(sid)
        folder = self.dir / _safe_sid(sid)
        if not folder.is_dir():
            return 0
        for key in [k for k in self._names if k[0] == sid]:
            self._names.pop(key, None)
        try:
            n = len(list(folder.iterdir()))
            shutil.rmtree(folder)
            return n
        except Exception:
            logger.exception(f"[multi-decision] 删除请求记录失败：{folder}")
            return 0

    def clear_all(self, only_reply: bool = False) -> int:
        """清空所有会话的请求快照，返回删掉的文件数（用户 m13708 第 4 条）。

        v4.00（用户 m10782）：决策快照从「请求快照」里分出去了，缓存页的「回复
        快照」只该清回复那一半——only_reply=True 时留着决策快照，只删其余的。
        

        和 purge() 的区别：**不碰** stats/requests.jsonl 这本用量账。账本是
        「最近 24 小时请求数 / 回复数」的来源，缓存设置页清缓存不该让概览
        数字凭空归零；只有删会话（purge）才连账本一起清。
        """
        removed = 0
        # 早期版本把快照直接写在 requests/ 根下，这里一并算（决策快照只出现在会话目录里）
        try:
            loose = list(self.dir.glob("*.json"))
        except Exception:
            loose = []
        for path in loose:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        try:
            folders = [d for d in self.dir.iterdir() if d.is_dir()]
        except Exception:
            return removed
        for folder in folders:
            if only_reply:
                try:
                    files = list(folder.glob("*.json"))
                except Exception:
                    files = []
                for path in files:
                    if self._is_decision_file(path):
                        continue
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
                continue
            try:
                n = len(list(folder.glob("*.json")))
            except Exception:
                n = 0
            try:
                shutil.rmtree(folder)
                removed += n
            except Exception:
                logger.exception(f"[multi-decision] 清理请求记录目录失败：{folder}")
                continue
            for key in [k for k in self._names if _safe_sid(k[0]) == folder.name]:
                self._names.pop(key, None)
        return removed

    def clear_decisions(self) -> int:
        """只清决策快照，回复快照留着（用户 m10782：决策快照单独一类缓存）。"""
        removed = 0
        roots = []
        try:
            roots = list(self.dir.glob("*.json"))
        except Exception:
            roots = []
        try:
            folders = [d for d in self.dir.iterdir() if d.is_dir()]
        except Exception:
            folders = []
        roots.extend([f for fd in folders for f in fd.glob("*.json")])
        for path in roots:
            if not self._is_decision_file(path):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def usage(self) -> dict:
        """按类别数磁盘占用：回复快照 / 决策快照各多少份、几个字节。

        用户 m10782：缓存账本里这两类是两行，同目录、靠 decision_only 区分。
        最坏情况（每会话几百份）也就读几 MB，调用方 /cache-stats 自己有 10 秒缓存。
        """
        out = {"reply": [0, 0], "decision": [0, 0]}
        # 早期版本把快照直接写在 requests/ 根下（没有会话子目录），那些一律算回复快照
        try:
            loose = list(self.dir.glob("*.json"))
        except Exception:
            loose = []
        for path in loose:
            try:
                size = int(path.stat().st_size or 0)
            except OSError:
                continue
            slot = out["decision" if self._is_decision_file(path) else "reply"]
            slot[0] += 1
            slot[1] += size
        try:
            folders = [d for d in self.dir.iterdir() if d.is_dir()]
        except Exception:
            folders = []
        for folder in folders:
            try:
                files = list(folder.glob("*.json"))
            except Exception:
                continue
            for path in files:
                try:
                    size = int(path.stat().st_size or 0)
                except OSError:
                    continue
                slot = out["decision" if self._is_decision_file(path) else "reply"]
                slot[0] += 1
                slot[1] += size
        return {k: {"count": v[0], "bytes": v[1]} for k, v in out.items()}

    def list_sessions(self) -> list:
        """列出有记录的会话及各自轮数。"""
        out = []
        try:
            folders = sorted(self.dir.iterdir())
        except Exception:
            return out
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                files = sorted(folder.glob("*.json"))
            except Exception:
                continue
            if not files:
                continue
            latest = files[-1]
            try:
                ts = latest.stat().st_mtime
            except OSError:
                ts = 0
            # 目录名是转义过的（冒号换成下划线），真实 sid 要从记录里读回来，
            # 否则前端按 ":" 判断群聊/私聊会全部认错，也会和窗口状态里的 sid 对不上
            real_sid = ""
            try:
                real_sid = str(json.loads(latest.read_text(encoding="utf-8")).get("sid") or "")
            except Exception:
                real_sid = ""
            out.append({
                "sid": real_sid or folder.name,
                "dir": folder.name,
                "rounds": len(files),
                "last_ts": ts,
            })
        out.sort(key=lambda x: x["last_ts"], reverse=True)
        return out

    @staticmethod
    def _user_count_with_incoming(data: dict) -> int:
        """v3.24（用户 m45932）：老记录的 window_user 只数了框架历史，漏掉本轮输入。

        那些记录里没有 window_user_src，补上 user_prompt 里本轮 user 消息的条数
        （一条 user 里可能装着好几条群消息，按 [message_id: 数才是真实条数）。
        """
        try:
            total = int(data.get("window_user") or 0)
        except (TypeError, ValueError):
            total = 0
        if data.get("window_user_src") == "window":
            return total
        try:
            prompt = str(data.get("user_prompt") or "")
            if not prompt:
                parts = data.get("user_prompt_parts") or []
                prompt = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
            return total + max(0, prompt.count("[message_id:"))
        except Exception:
            return total

    def list_rounds(self, sid: str, limit: int = 20) -> list:
        """列出某会话最近几轮的摘要（不含完整消息，避免一次传太大）。"""
        try:
            limit = max(1, min(int(limit or 20), 200))
        except (TypeError, ValueError):
            limit = 20
        folder = self.dir / _safe_sid(sid)
        try:
            # 多读一轮，为本页最老记录也提供同会话上一轮的字符前缀比较。
            files = sorted(folder.glob("*.json"), reverse=True)[: limit + 1]
        except Exception:
            return []
        records = []
        for path in files:
            try:
                records.append((path, json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue

        def _flat(rec: dict) -> str:
            """序列化记录中的消息投影；不是 provider wire，也不是 token 序列。"""
            try:
                return json.dumps(
                    rec.get("assembled") or rec.get("messages") or [],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except Exception:
                return ""

        def _prefix(a: str, b: str) -> int:
            """公共前缀长度。二分 + 切片比较，比逐字符循环快一个量级。"""
            lo, hi = 0, min(len(a), len(b))
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if a[:mid] == b[:mid]:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        out = []
        for idx, (path, data) in enumerate(records):
            if len(out) >= limit:
                break
            # 老记录里没存 payload_bytes（v2.91 之前），用当时的 assembled 现算一份，
            # 不然整张表的老行体积列全是「—」
            pbytes = data.get("payload_bytes") or 0
            if not pbytes:
                try:
                    pbytes = len(
                        json.dumps(
                            data.get("assembled") or data.get("messages") or [],
                            ensure_ascii=False,
                        ).encode("utf-8")
                    )
                except Exception:
                    pbytes = 0
            # JSON 字符前缀复用率仅诊断消息投影的稳定性，不预测缓存 token。
            # 字符密度、provider 包装/工具、图片编码及缓存块粒度均不在该分母里。
            est = None
            prefix_chars = total_chars = 0
            prefix_tokens = total_tokens = 0
            previous_name = ""
            gap_seconds = None
            if idx + 1 < len(records):
                cur_flat = _flat(data)
                # v4.25（用户 m22166）：命中预估只跟**同类**的上一份比。
                # 回复轮与决策轮的 assembled 结构完全不同（一个是 messages、
                # 一个是 transcript），拿上一份文件不分类地比，前缀会整段断掉，
                # 页面上的命中预估就常年是 0 或个位数，跟实测差几十个点。
                cur_oc = _outcome_of(data)
                prev_path = prev_data = None
                for _k in range(idx + 1, len(records)):
                    if _outcome_of(records[_k][1]) == cur_oc:
                        prev_path, prev_data = records[_k]
                        break
                if prev_path is None:
                    prev_path, prev_data = records[idx + 1]
                if cur_flat:
                    total_chars = len(cur_flat)
                    prefix_chars = _prefix(cur_flat, _flat(prev_data))
                    previous_name = prev_path.name
                    # 距上一轮的间隔：provider 的 KV 缓存会过期，隔太久前缀再稳也命中不了
                    try:
                        gap_seconds = int((data.get("ts") or 0) - (prev_data.get("ts") or 0))
                    except (TypeError, ValueError):
                        gap_seconds = None
                    est = round(prefix_chars * 100.0 / total_chars, 1)
                    # v3.19：字符比例会被「中文/英文/JSON 结构」的比例带偏，
                    # 换算成近似 token 再当分子，跟真实 input_tokens 才有可比性。
                    total_tokens = _approx_tokens(cur_flat)
                    prefix_tokens = _approx_tokens(cur_flat[:prefix_chars])
            out.append({
                "name": path.name,
                "ts": data.get("ts") or 0,
                "replied": bool(data.get("replied")),
                "failed": bool(data.get("failed")),
                "outcome": data.get("outcome") or ("failed" if data.get("failed") else "replied" if data.get("replied") else "no_reply"),
                "error": data.get("error") or "",
                "reply_bubbles": data.get("reply_bubbles") or 0,
                "window_size": data.get("window_size") or 0,
                "window_counted": data.get("window_counted") or 0,
                "window_total": data.get("window_total") or 0,
                # v3.22（用户 m42531）：窗口条数也分出「只数用户消息」的口径，
                # 跟走势柱一致；含 bot 的总数仍留在 window_total 里给 title 用。
                # v3.88（用户 m05012 第 2 条）：裁切快照按它自己的条数画（= 裁后）；
                # v3.87 那两批快照存的是裁前
                # 的 56 条，用 trim_after_user 兜回真实条数，柱高才一致。
                # v3.89（用户 m05321 第 1 条）：快照现在含待发，条数按它自己算
                # （trim_snapshot_user）；v3.89 以前的快照没这个键，退到
                # trim_after_user（裁后窗口、不含待发）。
                "window_user": (int(data.get("trim_snapshot_user") or 0)
                                if (data.get("trim_only") and data.get("trim_snapshot_user"))
                                else (int(data.get("trim_after_user") or 0)
                                      if (data.get("trim_only") and data.get("trim_after_user"))
                                      else self._user_count_with_incoming(data))),
                # v3.87（用户 m04715 第 1 条）：裁切快照的前后条数要带给前端，
                # 走势柱的悬停说明要用；trim_only 用来认「这不是一轮请求」。
                "trim_only": bool(data.get("trim_only")),
                "trim_before": int(data.get("trim_before") or 0),
                "trim_after": int(data.get("trim_after") or 0),
                # v3.89（用户 m05321 第 1 条）：快照里有多少条是待发（悬停要写出来）
                "trim_pending_count": int(data.get("trim_pending_count") or 0),
                "trigger_reason": data.get("trigger_reason") or "",
                "trigger_count": data.get("trigger_count") or 0,
                "count_threshold": data.get("count_threshold") or 0,
                "trigger_prob": float(data.get("trigger_prob") or 0.0),
                "message_count": len(data.get("messages") or []),
                "event_id": data.get("event_id") or "",
                # v3.x（用户 2026-10-24 图4）：出错那一轮后来重试的结果补写在它自己
                # 那份记录里，列表也带出来，前端不用点开就知道这轮最后补回来没有。
                "watchdog_released": int(data.get("watchdog_released") or 0),
                "total_chars": data.get("total_chars") or 0,
                "payload_bytes": pbytes,
                **payload_metrics(data),
                # est_hit 为旧客户端保留；数值含义仍是 JSON 字符比例。
                "est_hit": est,
                "prefix_reuse_percent": est,
                "prefix_reuse_chars": prefix_chars,
                "prefix_reuse_total_chars": total_chars,
                "prefix_reuse_tokens": prefix_tokens,
                "prefix_reuse_total_tokens": total_tokens,
                # v3.52（用户 m07864 第 4 条）：本轮新消息里带了几张图。
                # 命中预估要按「这张图是不是这轮刚加的」折算成不参与缓存的 token。
                "incoming_image_count": _count_image_parts(data.get("incoming_with_native") or []),
                "prefix_reuse_previous": previous_name,
                "gap_from_previous": gap_seconds,
                "prefix_reuse_basis": "request_record_json_characters",
                "prefix_reuse_is_token_prediction": False,
                # v3.66（用户 m19536 第 6 条）：这一轮的轨迹就写在同一份文件里，
                # 顺手带出来给 /requests 用——原来每行还要按 event_id 把整目录
                # 重扫一遍（read_by_event 最坏 100 份），等于读两遍 14 MB。
                # 读接口返回前 pop 掉，不进响应体。
                # v3.91（用户 m05749）：进阶决策的快照。它不是一轮请求，页面上
                # 要单独开一个「决策记录」页签，所以把结论与用量一并带出去。
                "decision_only": bool(data.get("decision_only")),
                "decision_reply": bool(data.get("decision_reply")),
                "decision_reason": data.get("decision_reason") or "",
                # v4.11（用户 m17596 第 6 条）：这一轮是不是决策判出来的。前端
                # 触发列靠它把标签换成「决策触发」，原来只写进快照、没带到
                # /requests 的行里，页面上一直没生效。
                "decision_passed": bool(data.get("decision_passed")),
                "decision_say": data.get("decision_say") or "",
                "decision_source": data.get("decision_source") or "",
                "decision_model": data.get("decision_model") or "",
                "decision_via": data.get("decision_via") or "",
                "decision_attempts": int(data.get("decision_attempts") or 0),
                "decision_elapsed": float(data.get("decision_elapsed") or 0.0),
                # 用户 m05124 第 8 条：决策轮的防抖等待（触发 → 真正开始判）。
                "decision_debounce": float(data.get("decision_debounce") or 0.0),
                "decision_tools": list(data.get("decision_tools") or []),
                # v3.94（用户 m08398 图7/图8）：决策行也要跟回复记录一样给出
                # token（入/出）与缓存命中，决策表才能照抄回复表的列。
                "input_tokens": int(data.get("input_tokens") or 0),
                "output_tokens": int(data.get("output_tokens") or 0),
                "cached_tokens": int(data.get("cached_tokens") or 0),
                "decision_system_names": list(data.get("decision_system_names") or []),
                "_trace": data.get("trace") if isinstance(data.get("trace"), dict) else None,
                # 用户 m02159 图6：模型写了话但没按 <msg> 包起来 → 这一轮算「未回复」。
                "reply_unwrapped": _unwrapped_reply(data.get("trace")),
            })
        return out

    def _rounds_sig(self, sid: str) -> tuple:
        """目录指纹：文件数 + 最新修改时间（纳秒）。用来判断摘要缓存要不要重算。"""
        folder = self.dir / _safe_sid(sid)
        try:
            entries = list(os.scandir(folder))
        except Exception:
            return (0, 0)
        newest = 0
        for ent in entries:
            try:
                mtime = ent.stat().st_mtime_ns
            except Exception:
                continue
            if mtime > newest:
                newest = mtime
        return (len(entries), newest)

    def list_rounds_cached(self, sid: str, limit: int = 20) -> list:
        """list_rounds 的缓存版：目录指纹没变就直接复用上次算好的摘要行。

        只给读接口用。写盘（flush / purge / clear_all）会改目录指纹（文件数或
        最新修改时间变），下一次读自然重建，不需要手动失效。
        返回浅拷贝：调用方会 pop 掉 _trace、往里补 token 字段，不能弄脏缓存。
        """
        try:
            limit = max(1, min(int(limit or 20), 200))
        except (TypeError, ValueError):
            limit = 20
        sig = self._rounds_sig(sid)
        hit = self._rounds_cache.get(sid)
        if hit is not None and hit[0] == sig:
            rows = hit[1]
        else:
            # 一次算满 200（list_rounds 的内部上限），后面的读都是切片，
            # 换个 limit 也不必重算。
            rows = self.list_rounds(sid, 200)
            self._rounds_cache[sid] = (sig, rows)
        return [dict(x) for x in rows[:limit]]

    def read_round(self, sid: str, name: str) -> Optional[dict]:
        """读取一轮的完整记录。name 必须是纯文件名，防止路径穿越。"""
        safe_name = Path(str(name)).name
        if not safe_name.endswith(".json"):
            return None
        path = self.dir / _safe_sid(sid) / safe_name
        try:
            if not path.is_file():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("[multi-decision] 读取请求记录失败")
            return None

    def last_snapshot(self, sid: str) -> Optional[dict]:
        """最近一轮请求时发给模型的历史快照。

        热重载后窗口状态是空的（只在内存里），但请求记录还在盘上，
        用它兜底才能让上下文页一打开就有东西看。
        """
        folder = self.dir / _safe_sid(sid)
        try:
            files = sorted(folder.glob("*.json"), reverse=True)
        except Exception:
            return None
        if not files:
            return None
        try:
            data = json.loads(files[0].read_text(encoding="utf-8"))
        except Exception:
            return None
        return {
            "name": files[0].name,
            "ts": data.get("ts") or 0,
            "messages": data.get("messages") or [],
            "window_size": data.get("window_size") or 0,
            "window_total": data.get("window_total") or 0,
        }

    def discard(self, sid: str) -> None:
        for key in list(self._pending):
            if key[0] == sid:
                self._pending.pop(key, None)
        for key in [k for k in self._names if k[0] == sid]:
            self._names.pop(key, None)

    def shutdown(self) -> None:
        self._pending.clear()

    # --------------------------------------------------------- 24 小时计数账本

    @staticmethod
    def _round_key(sid: str, event_id: str, ts) -> tuple:
        """账本行与请求文件对同一轮的共同身份：有事件号用事件号，否则用时刻。"""
        eid = str(event_id or "")
        if eid:
            return (str(sid or ""), eid)
        try:
            return (str(sid or ""), "ts:%.3f" % float(ts or 0.0))
        except (TypeError, ValueError):
            return (str(sid or ""), "ts:0.000")

    def _append_ledger(self, sid: str, record: dict) -> None:
        """把这一轮的计数追加进账本。写不进去只记日志：卡片不能因为账本坏掉就报错。"""
        if not self.enabled:
            return
        try:
            raw_ts = record.get("ts")
            ts = round(float(raw_ts) if isinstance(raw_ts, (int, float)) else 0.0, 3)
            outcome = str(record.get("outcome") or "")
            row = {
                "ts": ts,
                # 归一后的会话 id（带冒号，跟消息日志口径一致），不是目录名
                "sid": str(record.get("sid") or sid or ""),
                "event_id": str(record.get("event_id") or ""),
                "root": str(record.get("root_event_id") or record.get("retry_root") or ""),
                "replied": bool(record.get("replied") or outcome == "replied"),
                "failed": bool(record.get("failed") or outcome in ("failed", "error")
                               or record.get("error")),
            }
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with self._ledger_lock:
                with self.ledger.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._ledger_cache = None
            self._trim_ledger()
        except Exception:
            logger.debug("[multi-decision] 写请求账本失败", exc_info=True)

    def _rewrite_ledger(self, lines: list) -> None:
        """整份重写账本（临时文件 + 原子替换），顺手让读缓存失效。"""
        self._ledger_cache = None
        if not lines:
            try:
                self.ledger.unlink()
            except OSError:
                pass
            return
        tmp = self.ledger.with_suffix(self.ledger.suffix + ".tmp")
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            tmp.replace(self.ledger)
        except Exception:
            logger.debug("[multi-decision] 重写请求账本失败", exc_info=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _trim_ledger(self) -> None:
        """按天滚动清理（每小时最多跑一次）：账本不该无限长。"""
        now = time.time()
        if now - self._ledger_trim_at < 3600:
            return
        self._ledger_trim_at = now
        cutoff = now - LEDGER_KEEP_DAYS * 86400
        try:
            lines = self.ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        keep, dropped = [], 0
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                ts = float(json.loads(text).get("ts") or 0.0)
            except Exception:
                dropped += 1  # 坏行一并丢掉
                continue
            if ts < cutoff:
                dropped += 1
                continue
            keep.append(text)
        if dropped:
            self._rewrite_ledger(keep)

    def _drop_ledger_sid(self, sid: str) -> None:
        """删会话时把它的账本行一起清掉，否则统计会把删掉的会话接着算进去。"""
        sid = str(sid or "")
        if not sid:
            return
        try:
            lines = self.ledger.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        keep, dropped = [], 0
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                row_sid = str(json.loads(text).get("sid") or "")
            except Exception:
                dropped += 1
                continue
            if row_sid == sid:
                dropped += 1
                continue
            keep.append(text)
        if dropped:
            self._rewrite_ledger(keep)

    def _ledger_rows(self) -> list:
        """账本行（按文件 mtime+size 缓存：5 秒一次轮询不必反复解析）。"""
        try:
            st = self.ledger.stat()
        except OSError:
            return []
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self._ledger_cache
        if cached and cached[0] == stamp:
            return cached[1]
        rows = []
        try:
            text = self.ledger.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue  # 坏行跳过就好
            if not isinstance(row, dict):
                continue
            try:
                ts = float(row.get("ts") or 0.0)
            except (TypeError, ValueError):
                continue
            sid = str(row.get("sid") or "")
            event_id = str(row.get("event_id") or "")
            rows.append({
                "ts": ts,
                "sid": sid,
                "event_id": event_id,
                "root": str(row.get("root") or ""),
                "replied": bool(row.get("replied")),
                "failed": bool(row.get("failed")),
                "key": self._round_key(sid, event_id, ts),
            })
        self._ledger_cache = (stamp, rows)
        return rows

    # ------------------------------------------------------- 概览卡统计（v3.46）

    def _window_rows(self, hours: int = 24, retry_events=None) -> list:
        """最近 hours 小时里每一轮一行（只读）：账本优先、请求文件补历史，同一轮只算一次。

        v3.48（用户 2026-10-24 第 1 条）：stats_recent 和 24 小时合并口径
        （state/round_stats.py）要的是同一份「窗口内逐轮」清单，抽出来共用，
        别让两边各扫一遍盘、各写一套去重。

        返回 [{sid, ts, event_id, root, replied, failed, retried}]。
        容错是硬要求：坏 json 跳过、目录不存在返回空、绝不抛——这是给 5 秒轮询的
        /overview 用的，一条坏记录不该让整张卡片打红。
        """
        rows = []
        try:
            hours = max(1, int(hours or 24))
        except (TypeError, ValueError):
            hours = 24
        retry_set = set()
        try:
            retry_set = {str(e) for e in (retry_events or ()) if e}
        except TypeError:
            retry_set = set()
        start = time.time() - hours * 3600

        def _row(sid, ts, replied, failed, event_id, root) -> dict:
            eid = str(event_id or "")
            root_id = str(root or "")
            return {
                "sid": str(sid or ""),
                "ts": ts,
                "event_id": eid,
                "root": root_id,
                "replied": bool(replied),
                "failed": bool(failed),
                # 重试轮两条路认：retry_history 链路里的事件号，或记录自带 root_event_id
                "retried": bool((eid and eid in retry_set)
                                or (root_id and eid and root_id != eid)),
            }

        seen = set()
        # ① 先读账本：它不受 keep 截断，一天跑多少轮就记多少轮。
        for row in self._ledger_rows():
            if row["ts"] < start:
                continue
            seen.add(row["key"])
            rows.append(_row(row["sid"], row["ts"], row["replied"], row["failed"],
                             row["event_id"], row["root"]))
        # ② 再用请求文件补账本上线前的历史；同一轮（同一个 key）只算一次。
        try:
            folders = list(self.dir.glob("*"))
        except Exception:
            return rows
        for folder in folders:
            try:
                if not folder.is_dir():
                    continue
            except OSError:
                continue
            try:
                files = list(folder.glob("*.json"))
            except Exception:
                continue
            for path in files:
                # 记录里的 ts 是 stage（真发请求）时刻，flush 落盘只会更晚，
                # 所以文件 mtime 一定不早于 ts：mtime 已早于窗口起点的文件直接跳过，
                # 不必把几十上百 KB 的 assembled 消息读进内存（24 小时能上千份）。
                try:
                    if path.stat().st_mtime < start:
                        continue
                except OSError:
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue  # 坏 json、正被写的文件：跳过这一条就够
                if not isinstance(data, dict):
                    continue
                ts = data.get("ts")
                if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                    try:
                        ts = path.stat().st_mtime  # 老记录没有 ts，就按落盘时间算
                    except OSError:
                        ts = 0
                if ts < start:
                    continue
                outcome = str(data.get("outcome") or "")
                # 失败三选一：显式 failed 标记 / outcome 落在失败态 / 带 error 文本
                failed = bool(data.get("failed") or outcome in ("failed", "error")
                              or data.get("error"))
                event_id = str(data.get("event_id") or "")
                root = str(data.get("root_event_id") or data.get("retry_root") or "")
                key = self._round_key(str(data.get("sid") or folder.name), event_id,
                                      data.get("ts"))
                if key in seen:
                    continue  # 账本里已经有这一轮
                seen.add(key)
                rows.append(_row(str(data.get("sid") or folder.name), ts,
                                 bool(data.get("replied") or outcome == "replied"),
                                 failed, event_id, root))
        return rows

    def recent_round_rows(self, hours: int = 24, retry_events=None) -> list:
        """最近 hours 小时每一轮的极简形状（只读），给 state/round_stats.merge_rounds 用。

        v3.48（用户 2026-10-24 第 1 条）：概览卡顶上一排数字要一份 24 小时全量口径，
        消息日志的 round_end 与这里的逐轮记录按 event_id 合并。字段只留计数需要的
        六个，别把整份 assembled 消息带出去。
        """
        return [
            {
                "event_id": row["event_id"],
                "sid": row["sid"],
                "ts": row["ts"],
                "replied": row["replied"],
                "failed": row["failed"],
                "retried": row["retried"],
            }
            for row in self._window_rows(hours, retry_events)
        ]

    def stats_recent(self, hours: int = 24, retry_events=None) -> dict:
        """最近 hours 小时的请求记录里：总轮数 / 失败条数 / 有回复条数 / 重试轮条数 / 真不回复条数。

        为什么另起一个口径：概览卡「最近24小时请求」原先吃的是
        decision_log.stats_recent(24)，那边只数 result=="round_end"（结算）事件，
        而看门狗强制释放、请求异常终止的那些轮根本没机会写 round_end，
        failed / retried 就永远 0，卡片于是假成「成功率 100%」（用户 2026-10-24 图6）。
        真实失败只留在请求记录里——engine 落盘时写的是
        failed=True / outcome="failed" / error="投递后 180 秒没有任何结算"。
        重试轮要对着 retry_history.json 的链路认（events[1:] 才是重试出来的轮），
        调用方把 RetryHistory.retry_events() 传进来即可。

        容错是硬要求：坏 json 跳过、目录不存在返回全 0、绝不抛——
        这是给 5 秒轮询的 /overview 用的，一条坏记录不该让整张卡片打红。

        v3.48：逐轮清单抽到 _window_rows（24 小时合并口径共用），这里只负责计数，
        返回字段一个没动。
        """
        out = {"rounds": 0, "failed": 0, "replied": 0, "retried": 0, "silent": 0}
        for row in self._window_rows(hours, retry_events):
            out["rounds"] += 1
            if row["failed"]:
                out["failed"] += 1
            if row["replied"]:
                out["replied"] += 1
            elif not row["failed"]:
                # v3.47（用户 2026-10-24 第 2 条）：既没回复也没失败的轮才是
                # 「真不回复」；失败轮不是不回复，失败但有回复的轮只算 replied。
                out["silent"] += 1
            if row["retried"]:
                out["retried"] += 1
        return out

    # ------------------------------------------------- 决策次数（概览卡）

    def decision_stats(self, hours: int = 24) -> dict:
        """最近 hours 小时里决策快照跑了几次、其中判「要回」几次（只读）。

        概览条要在「平均回复间隔」右边摆一格「最近24小时决策」，这个数别处拿不到：
        决策轮不是一次请求，write_decision_snapshot 走的是 ledger=False，
        压根不进 requests 账本，只能自己数盘上的决策快照。

        口径：
        - 决策快照与回复快照同目录（requests/<sid>/<毫秒时间戳>.json），靠记录里的
          decision_only 认，回复快照一条都不数；
        - 时间用记录里的 ts（决策那一刻的秒级 float），老记录没有 ts 就按文件名
          （毫秒）换算，再不行用落盘 mtime；
        - 文件 mtime 一定不早于 ts（判定完才落盘），mtime 已早于窗口起点的直接
          stat 掉、不读盘——几百份快照里大半能这样省掉。
        返回 {"rounds", "reply", "silent", "failed", "sids"}：rounds=决策次数、
        reply=其中判要回、silent=判定不回、failed=判定调用本身失败、
        sids=涉及几个会话。

        容错是硬要求：坏 json、目录不存在、单个文件读失败都只跳过、绝不抛——
        这是给 5 秒轮询的 /overview 用的，一条坏记录不该让整张卡片打红。
        """
        out = {"rounds": 0, "reply": 0, "silent": 0, "failed": 0, "sids": 0}
        try:
            hours = max(1, int(hours or 24))
        except (TypeError, ValueError):
            hours = 24
        start = time.time() - hours * 3600
        ts_re = re.compile(r'"ts"\s*:\s*([0-9]+(?:\.[0-9]+)?)')

        def _flag(text: str, key: str) -> bool:
            # json.dumps(indent=2) 的写法是 '"key": true'；紧凑写法也认一份，
            # 免得哪天 dump 参数一换这里就全数成 0。
            return ('"%s": true' % key) in text or ('"%s":true' % key) in text

        try:
            folders = list(self.dir.glob("*"))
        except Exception:
            return out
        sids = set()
        for folder in folders:
            try:
                if not folder.is_dir():
                    continue
            except OSError:
                continue
            try:
                files = list(folder.glob("*.json"))
            except Exception:
                continue
            for path in files:
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < start:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if not _flag(text, "decision_only"):
                    continue
                ts = 0.0
                m = ts_re.search(text[:2000])
                if m:
                    try:
                        ts = float(m.group(1))
                    except (TypeError, ValueError):
                        ts = 0.0
                if not ts:
                    try:
                        ts = int(path.stem) / 1000.0
                    except (TypeError, ValueError):
                        ts = st.st_mtime
                if ts < start:
                    continue
                out["rounds"] += 1
                if _flag(text, "failed"):
                    out["failed"] += 1
                elif _flag(text, "decision_reply"):
                    out["reply"] += 1
                else:
                    # 既没判要回、也没失败 = 判定不回
                    out["silent"] += 1
                sids.add(str(folder.name or ""))
        out["sids"] = len(sids)
        return out
