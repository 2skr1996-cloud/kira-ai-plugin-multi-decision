# -*- coding: utf-8 -*-
"""回复落笔前的收口：模型把同一条内容写两遍时只留一条（v3.54）。

用户 m14082「早上的几条消息 bot 会重复发送」：2026-10-25 早间 gm:1145141974
连着三轮（06:02 / 08:22 / 08:42），模型都在最后一个气泡上把同一句话写了
两遍——

    <msg><text>…先把自己那杯底的水喝完</text><emoji>128</emoji></msg>
    <msg><text>…先把自己那杯底的水喝完[Emoji 128]</text></msg>

一遍用标签，一遍把表情内联成文字。框架一个 <msg> 发一条，于是两条都进了群；
adapter 的 emoji.json 只收 99 个 id、128 不在里面，标签那版的表情被它自己
丢掉只剩正文，字面那版就把 `[Emoji 128]` 当正文发了出去——用户看到的就是
「一条没表情、一条结尾挂着 [Emoji 128]」。

钩子在框架 parse_xml 之前（ON_LLM_RESPONSE 全部跑完才读 text_response），
所以能在这里收口：

1) 字面表情标记搬回标签：`[Emoji 128]`、`[Emoji 汪汪 (ID: 277)]`、裸
   `[Emoji]` 从 <text> 里摘出来，变成 <msg> 的直接子元素
   `<emoji>128</emoji>`（框架只认这种位置）。摘不出 id 的裸标记直接丢掉，
   别当正文发出去。
2) 相邻两个 <msg> 正文归一后一样、且这对里带表情（标签或字面都算），只留
   带标签的那条。纯文字重复不动：bot 真连说两句一样的话不算这个 bug。

只认「相邻」：这是模型一次输出里自己重复自己的形态；隔开的两条同文更像是
它故意分两句说，不该合并。
"""

from __future__ import annotations

import re
from typing import List, Tuple

__all__ = ["dedupe_reply", "strip_text_emoji"]


#: 一个气泡：开标签带属性（模型常自己编 message_id），内容非贪婪
_MSG_RE = re.compile(r"(<msg\b[^>]*>)([\s\S]*?)(</msg>)", re.I)
#: 正文段
_TEXT_RE = re.compile(r"<text>([\s\S]*?)</text>", re.I)
#: 正当写法：<emoji>21</emoji>
_EMOJI_TAG_RE = re.compile(r"<emoji>\s*([^<]*?)\s*</emoji>", re.I)
#: 历史里的文字写法：[Emoji 21] / [Emoji 汪汪 (ID: 277)] / [Emoji]
_EMOJI_TEXT_RE = re.compile(r"[ \t]*\[Emoji\b([^\]]*)\]", re.I)
#: 括号尾巴里的 id：(ID: 277)
_TEXT_ID_RE = re.compile(r"\(\s*ID\s*:\s*(\d+)\s*\)", re.I)
#: 纯数字尾巴：128
_TEXT_DIGITS_RE = re.compile(r"^\s*(\d+)\s*$")
#: 气泡前面模型照抄的 "[08:42] " 前缀（删气泡时一起删）
_TRAIL_TIME_RE = re.compile(r"\[\d{1,2}:\d{2}\]\s*$")


def _text_emoji_id(tail: str) -> str:
    """从 `[Emoji` 后面的那段文字里取表情 id，取不到就给空串。"""
    m = _TEXT_ID_RE.search(tail or "")
    if m:
        return m.group(1)
    m = _TEXT_DIGITS_RE.match(tail or "")
    if m:
        return m.group(1)
    return ""


def strip_text_emoji(seg: str) -> Tuple[str, List[str], int]:
    """把一段正文里的字面表情标记摘出来。

    返回 (清洗后的正文, 摘到的 id 列表, 摘掉的标记处数)。没 id 的裸
    `[Emoji]` 只算摘掉、不进 id 列表——它的 id 已经无从得知了。
    """
    ids: List[str] = []
    count = 0

    def repl(m):
        nonlocal count
        count += 1
        eid = _text_emoji_id(m.group(1))
        if eid:
            ids.append(eid)
        return ""

    clean = _EMOJI_TEXT_RE.sub(repl, seg)
    if count:
        # 标记被摘走后常留下行尾空格或整行空白，收一下，别让气泡末尾挂空格
        clean = re.sub(r"[ \t]+$", "", clean)
        if not clean.strip():
            clean = ""
    return clean, ids, count


def _fix_block(inner: str) -> Tuple[str, int]:
    """修一个 <msg> 的内容：字面表情搬成标签，返回 (新内容, 搬了几处)。"""
    found: List[str] = []
    moved = 0

    def repl(m):
        nonlocal moved
        clean, ids, count = strip_text_emoji(m.group(1))
        found.extend(ids)
        if count:
            moved += count
            return "<text>" + clean + "</text>"
        return m.group(0)

    out = _TEXT_RE.sub(repl, inner)
    if moved:
        have = set(x.strip() for x in _EMOJI_TAG_RE.findall(out))
        add = [x for x in found if x and x not in have]
        if add:
            out = out.rstrip() + "".join("<emoji>%s</emoji>" % x for x in add)
    return out, moved


def _key(inner: str) -> str:
    """一个气泡的「正文指纹」：标签统一、空白全忽略、表情只留 id。"""
    s = _EMOJI_TAG_RE.sub(lambda m: "\x01" + m.group(1).strip() + "\x01", inner)
    s = re.sub(r"<[^>]+>", "<>", s)
    s = re.sub(r"\s+", "", s)
    return s


def dedupe_reply(text: str) -> Tuple[str, int, int]:
    """收口一次回复的文本。

    返回 (新文本, 删掉的重复气泡数, 搬回标签的字面表情处数)。没有 <msg>、
    或什么都不用改时原样返回（连同原对象，调用方可以只比较返回值）。
    """
    if not text or "<msg" not in text.lower():
        return text, 0, 0
    blocks = []
    moved_total = 0
    for m in _MSG_RE.finditer(text):
        fixed, moved = _fix_block(m.group(2))
        moved_total += moved
        blocks.append(
            {
                "start": m.start(),
                "end": m.end(),
                "open": m.group(1),
                "inner": fixed,
                "key": _key(fixed),
                "emoji": bool(_EMOJI_TAG_RE.search(fixed)),
            }
        )
    if not blocks:
        return text, 0, 0

    drops = set()
    kept = -1
    for i, b in enumerate(blocks):
        if kept >= 0:
            prev = blocks[kept]
            same = b["key"] and b["key"] == prev["key"]
            if same and (b["emoji"] or prev["emoji"]):
                if b["emoji"] and not prev["emoji"]:
                    # 留带真标签的那条：表情信息更全（真发出去的表情就它是）
                    drops.add(kept)
                    kept = i
                else:
                    drops.add(i)
                continue
        kept = i
    if not drops and not moved_total:
        return text, 0, 0

    out: List[str] = []
    pos = 0
    for i, b in enumerate(blocks):
        out.append(text[pos:b["start"]])
        if i in drops:
            # 连同气泡前面的空白一起吃掉，别在两条之间留个空行；模型常给每个
            # 气泡前面写个 "[08:42] " 的照抄前缀，气泡删了那截也该删。
            while out and not out[-1].strip():
                out.pop()
            if out:
                out[-1] = _TRAIL_TIME_RE.sub("", out[-1])
        else:
            out.append(b["open"] + b["inner"] + "</msg>")
        pos = b["end"]
    out.append(text[pos:])
    return "".join(out).strip(), len(drops), moved_total

def wrap_plain_reply(text: str) -> Tuple[str, bool]:
    """把「没按 <msg> 格式写」的回复包成一条消息，别让模型这段话凭空消失。

    框架只认 <msg> 里的东西，标签外的纯文本解析出来是 0 条消息，于是这一轮
    记成「未回复」——用户 m02159 图6 看到的就是这个：记录里明明有模型输出，
    群里却没有这条消息。这里补一层兜底：整段裸文本包成 <msg><text>…</text></msg>。
    已经写了 <msg 的原样放过；写了 <text> 但漏了外层 <msg> 的只补外壳。
    正文里的半角 & < > 换成全角再包，免得破坏解析（视觉上几乎看不出差别）。
    """
    if not isinstance(text, str) or not text.strip():
        return text, False
    low = text.lower()
    if "<msg" in low:
        return text, False
    body = text.strip()
    if "<text" in low and "</text>" in low:
        return "<msg>" + body + "</msg>", True
    for a, b in (("&", "＆"), ("<", "＜"), (">", "＞")):
        body = body.replace(a, b)
    return "<msg><text>" + body + "</text></msg>", True
