"""中期摘要：把被裁掉的那批消息压成一段文本，挂在窗口顶部。

只做机械压缩（每条压一行），不给模型额外调用。位置固定在窗口最前面，
role 用 user —— 框架的 assemble_prompt 里有
`if self.messages[0].role == "system": self.messages.pop(0)`，
system 放不住，只有 user 能活下来。

为什么不额外花缓存：裁切那一刻前缀本来就变了、缓存全 miss，
摘要跟着变没有额外代价，它占的正好是刚被裁掉的缺口。
"""

from __future__ import annotations

import re
from typing import Any, List

HEADER = "【中期摘要】以下是更早的对话，已从上下文中裁掉，仅供了解背景："
MAX_LINE_CHARS = 40
SKIP_ROLES = ("tool", "system")


def _get(msg: Any, key: str, default=None):
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _content_text(content: Any) -> str:
    """把 content 拉平成纯文本（多模态数组只取 text 部分）。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return " ".join(parts)
    return str(content)


def speaker_of(msg: Any) -> str:
    role = str(_get(msg, "role") or "")
    if role == "assistant":
        return "我"
    name = _get(msg, "name")
    if name:
        return str(name)
    return {"user": "对方", "tool": "工具"}.get(role, role or "?")


_GROUP_RE = re.compile(r"group_name:\s*.*?\s*group_id:\s*-?\d+\s*")
_PREFIX_RE = re.compile(r"^\s*(?:\[[^\]]{0,120}\]\s*)+")
_TEXT_TAG_RE = re.compile(r"<text>(.*?)</text>", re.S | re.I)
_ANY_TAG_RE = re.compile(r"</?[A-Za-z_][^>]{0,300}>")
_LEAD_RE = re.compile(r"^[\s|:：·,，]+")
_MEDIA_TAGS = (
    ("<image", "[图片]"), ("<img", "[图片]"), ("<sticker", "[表情]"),
    ("<record", "[语音]"), ("<video", "[视频]"), ("<file", "[文件]"),
)


def clean_text(raw: Any) -> str:
    """把一条消息的原始内容洗成「人话」。

    历史里存的 assistant 消息是 `<msg ...><text>正文</text></msg>` 这种 XML，
    user 消息则带 `[Oct 19 2026 01:19 Mon] [message_id: ...] [group_name: ...]`
    一长串前缀。直接截前 40 字会得到一堆标签和 id，摘要等于没写，所以先剥干净：
    群标识 → 取 <text> 正文（没有就剥所有标签）→ 剥中括号前缀 → 压空白。
    """
    s = _content_text(raw)
    if not s:
        return ""
    low = s.lower()
    s = _GROUP_RE.sub("", s)
    parts = [p.strip() for p in _TEXT_TAG_RE.findall(s)]
    if parts:
        s = " / ".join(p for p in parts if p)
    else:
        s = _ANY_TAG_RE.sub(" ", s)
    s = _PREFIX_RE.sub("", s)
    s = _LEAD_RE.sub("", s)
    s = " ".join(s.split())
    if s:
        return s
    # 纯媒体消息：正文被剥光了，至少留个标记，别让它从摘要里凭空消失
    for tag, label in _MEDIA_TAGS:
        if tag in low:
            return label
    return ""


def line_of(msg: Any, max_chars: int = MAX_LINE_CHARS) -> str:
    """一条消息压成一行：`说话人: 前 N 字…`。空内容返回空串。"""
    text = clean_text(_get(msg, "content"))
    if not text:
        return ""
    limit = max(8, int(max_chars or MAX_LINE_CHARS))
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return f"{speaker_of(msg)}: {text}"


def build_lines(messages, max_chars: int = MAX_LINE_CHARS) -> List[str]:
    """把一批消息压成摘要行，跳过工具结果与 system。"""
    lines: List[str] = []
    for msg in messages or []:
        if str(_get(msg, "role") or "") in SKIP_ROLES:
            continue
        line = line_of(msg, max_chars)
        if line:
            lines.append(line)
    return lines


def body_of(text: str) -> List[str]:
    """取出已有摘要里的正文行（去掉头部说明与空行）。"""
    if not text:
        return []
    out = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("【中期摘要】"):
            continue
        out.append(line)
    return out


def _size(lines: List[str]) -> int:
    return len(HEADER) + 1 + sum(len(x) + 1 for x in lines)


def drop_overlap(old_lines: List[str], new_lines: List[str]) -> List[str]:
    """去掉新旧之间的重复段。

    第二次裁切时，被裁的那批通常包含「上次保留、这次才裁」的消息，
    开头几条其实已经进过摘要了。找 old 的尾 == new 的头，跳过重叠。
    """
    limit = min(len(old_lines), len(new_lines))
    for k in range(limit, 0, -1):
        if old_lines[-k:] == new_lines[:k]:
            return new_lines[k:]
    return list(new_lines)


def merge_summary(old_text: str, new_lines, max_chars: int = 300) -> str:
    """把新裁掉的消息并进已有摘要，超长从**最旧**的行开始丢。

    越久远的信息越先被牺牲，最近被裁掉的那批一定留得住。
    """
    limit = max(60, int(max_chars or 300))
    old_lines = body_of(old_text)
    fresh = [str(x) for x in (new_lines or []) if str(x).strip()]
    fresh = drop_overlap(old_lines, fresh)
    lines = old_lines + fresh
    while lines and _size(lines) > limit:
        lines.pop(0)
    if not lines:
        return ""
    return HEADER + "\n" + "\n".join(lines)
