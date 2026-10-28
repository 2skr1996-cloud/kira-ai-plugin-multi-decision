"""窗口裁切：选切点、保护工具组、清孤儿。

切点必须落在「一条消息能被当成对话起点」的位置：
- user 消息
- 不带 tool_calls 的 assistant 消息

否则会把 assistant(tool_calls) 和它的 tool 结果拆开，模型直接 400。
"""

from __future__ import annotations

import re
from typing import Any, List

# 框架把「同一批进来的多条群消息」拼成一条 role=user 再写进记忆，
# 所以窗口里一条 user 消息内部可能装着 20 条不同的群消息（每条都带
# [message_id: ...]）。只数数组元素个数会让 192 条真实消息显示成 20 条，
# 阈值 60 永远达不到、窗口永不裁切——v2.23 起按内部真实条数计。
_INNER_RE = re.compile(r"\[message_id:\s*[^\]]{1,200}\]")


def _get(msg: Any, key: str, default=None):
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def is_cut_boundary(msg: Any) -> bool:
    role = _get(msg, "role") or ""
    if role == "user":
        return True
    if role == "assistant" and not _get(msg, "tool_calls"):
        return True
    return False


def is_counted(msg: Any) -> bool:
    """这条消息算不算「一条积累」。

    用户的说法是「30/60 指的是积累，不算工具调用那些」：一次工具往返
    （assistant 带 tool_calls + 若干 tool 结果）是机器和人之间的中间步骤，
    不该占窗口条数，否则工具一多窗口就飞快涨满。
    """
    role = _get(msg, "role") or ""
    if role in ("tool", "system"):
        return False
    if role == "assistant" and _get(msg, "tool_calls"):
        return False
    return True


def _text_of(content) -> str:
    """content → 可读文本（只处理窗口里真实出现的两种形态）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if str(item.get("type") or "") == "text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return "" if content is None else str(content)


def inner_count(msg: Any) -> int:
    """这条消息里装了几条真实消息。

    框架把一批群消息拼成一条 user，所以一条 user 可能装着十几条。
    assistant / 其它 role 都是 1。
    """
    content = _get(msg, "content")
    text = _text_of(content)
    if not text:
        return 1
    n = len(_INNER_RE.findall(text))
    return n if n > 1 else 1


def weight_of(msg: Any) -> int:
    """这条消息占几个「计入条数」。不计入的返回 0。"""
    if not is_counted(msg):
        return 0
    return inner_count(msg)


def count_messages(messages: List[Any]) -> int:
    """数一遍「计入条数」——按内部真实消息条数加权。"""
    return sum(weight_of(msg) for msg in messages)


def user_weight_of(msg: Any) -> int:
    """只数用户消息的权重（v3.23）。

    用户 m45332：「裁切是看 user 消息数触发的，不是总消息数触发的，看总
    消息数就包含 bot 了，就会太快触发裁切」。bot 的回复算 1 条会把 60 条
    的阈值提前撞满，所以阈值/保留都只数用户侧，bot 的消息跟着一起留着。
    """
    if str(_get(msg, "role") or "") != "user":
        return 0
    return inner_count(msg)


def count_user_messages(messages: List[Any]) -> int:
    """数一遍「用户消息条数」——bot 自己的回复不计入。"""
    return sum(user_weight_of(msg) for msg in messages)


def trim_message_to(msg: Any, keep_n: int) -> Any:
    """把一条装着多条群消息的 user 裁成「只留最后 keep_n 条」。

    用户 m20752：「裁切为什么都裁切到 3 条了，不应该是裁切到保留 30 条么」——
    因为一条 user 里装着十几条群消息，整条留下就会多出好几条；整条丢掉又
    会少好几条。所以边界那条按 message_id 切开，只留最新的 keep_n 条。
    改不了（多模态 content、找不到切点）就返回 None，调用方退回「整条保留」。
    """
    content = _get(msg, "content")
    if not isinstance(content, str):
        return None
    text = content
    if not text:
        return None
    positions = [m.start() for m in _INNER_RE.finditer(text)]
    if len(positions) <= keep_n:
        return None
    cut = positions[len(positions) - keep_n]
    nl = text.rfind("\n", 0, cut)
    if nl >= 0:
        cut = nl + 1
    new_text = text[cut:].lstrip()
    if not new_text:
        return None
    if isinstance(msg, dict):
        out = dict(msg)
        out["content"] = new_text
        return out
    try:
        out = msg.model_copy(deep=True)
    except Exception:
        try:
            import copy as _copy
            out = _copy.copy(msg)
        except Exception:
            return None
    try:
        out.content = new_text
    except Exception:
        return None
    return out


def choose_cut_index(messages: List[Any], target: int, tolerance: float = 0.2) -> int:
    """在 target 附近找一个安全的切点，找不到就退回 target。"""
    n = len(messages)
    if n <= 1:
        return n
    target = max(1, min(n - 1, target))
    low = max(1, int(target * (1 - tolerance)))
    high = min(n - 1, int(target * (1 + tolerance)))
    for i in range(target, high + 1):
        if is_cut_boundary(messages[i]):
            return i
    for i in range(target - 1, low - 1, -1):
        if is_cut_boundary(messages[i]):
            return i
    return target


def clean_orphans(messages: List[Any]) -> int:
    """从头部删掉悬空的 tool 消息和没人应答的 tool_calls，返回删掉的条数。"""
    removed = 0
    while messages:
        head = messages[0]
        role = _get(head, "role") or ""
        if role == "tool":
            messages.pop(0)
            removed += 1
            continue
        if role == "assistant":
            calls = _get(head, "tool_calls") or []
            if calls:
                ids = set()
                for call in calls:
                    cid = _get(call, "id")
                    if cid:
                        ids.add(cid)
                answered = False
                for msg in messages[1:]:
                    if (_get(msg, "role") or "") == "tool" and _get(msg, "tool_call_id") in ids:
                        answered = True
                        break
                if not answered:
                    messages.pop(0)
                    removed += 1
                    continue
        break
    return removed


def trim_by_count(
    messages: List[Any],
    keep: int,
    tolerance: float = 0.2,
    protect: bool = True,
    user_only: bool = False,
) -> int:
    """按「计入条数」裁切：留下最新 keep 条计入的消息，工具往返跟着对话一起留。

    user_only=True 时只数用户消息（v3.23）：bot 的回复权重为 0，会跟着
    它前后最近的用户消息一起留下，不会占 keep 的名额。

    返回切点（原始下标，0 表示没动）。kept 里若还有悬空的 tool 结果，
    `clean_orphans` 会再从头摘掉几条，它们不进返回的下标、也不进摘要。
    """
    n = len(messages)
    # 只有一条消息也可能要裁：那一条 user 里可能装着十几条群消息（v2.23）
    if keep < 1 or n < 1:
        return 0
    weigh = user_weight_of if user_only else weight_of
    counted = 0
    cut = n
    part = None
    for i in range(n - 1, -1, -1):
        w = weigh(messages[i])
        if w and counted + w > keep:
            # 这一条 user 里装着好几条真实消息：装得下就劈开，只留最新 room 条
            room = keep - counted
            if w > 1 and room >= 1:
                part = trim_message_to(messages[i], room)
                if part is not None:
                    cut = i
            break
        counted += w
        cut = i
    # 只数用户消息时，切点容易落在一条孤零零的 assistant 上——它回复的那条
    # 用户消息已经裁掉了，留下半截对话没意义。往前挪到最近的一条用户消息。
    if user_only and part is None and 0 < cut < n:
        while cut < n and (_get(messages[cut], "role") or "") != "user":
            cut += 1
        if cut >= n:
            return 0
    # cut == 0 但劈开了边界那条，内容确实变了，不能当「没动」
    if cut <= 0 and part is None:
        return 0
    if protect and part is None:
        limit = min(n - 1, cut + max(1, int(keep * tolerance)))
        for i in range(cut, limit + 1):
            if is_cut_boundary(messages[i]):
                cut = i
                break
    kept = messages[cut:]
    if part is not None:
        kept[0] = part
    if protect:
        clean_orphans(kept)
    if not kept or (len(kept) >= n and part is None):
        return 0
    messages[:] = kept
    return cut

