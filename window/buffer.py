"""窗口缓冲：插件自己维护的上下文历史。

框架记忆每轮从头部裁，下标一直在漂，而且默认只有 10 轮，
撑不起「保留 30 条、攒到 60 条才裁」这种需求。所以这里自己存一份。

框架每次给的 `request.messages` 只当「新消息的来源」：先拆包（一条打包的
user 拆成一条一条群消息，见 split.py），再用指纹和 message_id 在尾部做
增量合并。合不上时不再重锚（那样窗口每轮首条都变、缓存全断），改成保守
补最后一条。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Set

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

#: 窗口里一条 user 可能装着好几条群消息，每条都带 [message_id: ...]
_ID_RE = re.compile(r"\[message_id:\s*([^\]]{1,200})\]")

#: 窗口里一条 assistant 也可能装着好几段 <msg message_id="...">……</msg>
_MSG_ID_RE = re.compile(r"<msg\b[^>]*\bmessage_id\s*=\s*[\"']([^\"']{1,200})[\"']", re.I)


def _get(msg: Any, key: str, default=None):
    """同时兼容 OpenAIMessage 对象和 dict。"""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def _content_key(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(content)


def _flat_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def ids_of(messages) -> Set[str]:
    """消息里出现过的所有 message_id，用来判断「这条是不是已经有了」。

    两种都要收：user 打包消息的 [message_id: X]，assistant 多段的
    <msg message_id="X">。后者漏了的话，拆开后的每条 assistant 都没有 id，
    每轮都会被当成新消息再追加一遍。
    """
    out: Set[str] = set()
    for msg in messages or []:
        text = _flat_text(_get(msg, "content"))
        for one in _ID_RE.findall(text):
            out.add(one.strip())
        for one in _MSG_ID_RE.findall(text):
            out.add(one.strip())
    return out


#: 唤醒标记（window/split.py 的 MARK）：只影响渲染，不参与指纹比对
_MENTION_RE = re.compile(r"\s*\[@你\]")


def fingerprint(msg: Any) -> str:
    """OpenAIMessage 没有稳定 id，只能靠 (role, content) 认人。

    带工具调用的消息还要算上 tool_calls / tool_call_id：agent 一轮里可能
    有好几条 content 为空的 assistant（各自带不同的调用），只比 content
    会被当成同一条去重掉，工具往返就进不了窗口（用户 m32663）。
    """
    role = _get(msg, "role") or ""
    raw = _content_key(_get(msg, "content"))
    # 唤醒标记不参与认人：窗口里带、框架历史里不带，带不带都得算同一条，
    # 否则每轮 collect 都把同一条当成新消息再并一遍（用户 m12833）。
    if "[@你]" in raw:
        raw = _MENTION_RE.sub("", raw)
    tc = _get(msg, "tool_calls")
    if tc:
        raw += "|tc:" + _content_key(tc)
    tcid = _get(msg, "tool_call_id")
    if tcid:
        raw += "|id:" + str(tcid)
    digest = hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()
    return f"{role}|{digest}"


#: 时间前缀：「[23:43] 」
_TIME_PREFIX_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}\]\s*")
#: <msg> 上的 message_id 属性
_MSG_ID_ATTR_RE = re.compile(r"\s*\bmessage_id\s*=\s*[\"'][^\"']*[\"']", re.I)
#: <msg> 段里的正文；占位与原件只在段尾空白上不一样
_TEXT_SEG_RE = re.compile(r"<text>([\s\S]*?)</text>", re.I)
#: 表情的标签写法：框架把 assistant 原件写进记忆时，表情是 <text> 的兄弟元素
_EMOJI_TAG_RE = re.compile(r"<emoji>[^<>]*</emoji>\s*", re.I)
#: 文字写法里的表情： [Emoji 128] / [Emoji 汪汪 (ID: 277)] / [Emoji]
_LITERAL_EMOJI_ITEM_RE = re.compile(r"[ \t]*\[Emoji\b([^\]]*)\]", re.I)
_LITERAL_EMOJI_ID_RE = re.compile(r"\(ID:\s*(\d+)\)", re.I)


def split_emoji_marks(text: str):
    """把正文里的「文字写法的表情标记」拆成 (干净正文, <emoji>N</emoji> 串)。

    v3.55（用户 m14082 根因）：我们替 bot 补回显占位时，正文来自 chain_text →
    emoji_repr，表情是文字写法 [Emoji 128]；框架把同一条原件写进记忆时，表情是
    <text> 之外的 <emoji>128</emoji> 标签。占位按正文认领（echo_key）永远对不上，
    于是一个带表情的回复会在窗口里永久多一条——历史里摆着「同一条两遍、其中一遍
    带字面标记」的先例，模型下一轮照着学，就直接把同一条发两遍了。
    这里按框架原件的结构重排：正文进 <text>，表情变成同级 <emoji> 标签。
    """
    src = str(text or "")
    found = []

    def _take(match):
        tail = match.group(1) or ""
        hit = _LITERAL_EMOJI_ID_RE.search(tail) or re.search(r"\d+", tail)
        if hit:
            found.append(hit.group(1) if hit.lastindex else hit.group(0))
        return ""

    body = _LITERAL_EMOJI_ITEM_RE.sub(_take, src)
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    tags = "".join("<emoji>%s</emoji>" % one for one in found)
    return body, tags


def echo_key(msg: Any) -> str:
    """「同一句话」的宽松指纹：只比正文，不管包装。

    只用来认领我们替 bot 补进窗口的回显占位（window/manager.py 的
    _pending_messages 里 mark=="bot" 那条）：发送时还没有 message_id，
    框架后来把带 id 的原件写进记忆，两条必须合成一条（用户 m11091 第 5 条）。

    v3.37（用户 m12023「现在会回复2次重复的」）：原来比的是整段 content，
    只剥时间前缀和 message_id。可占位是我们自己拼的「[02:09] <msg>[名]
    <text>正文</text></msg>」，原件是框架写的「[02:11] <msg message_id="…">
    [名] <text>正文</text> + 换行 + </msg>」——原件 </text> 后多一个换行，
    空白归一后一个成了「</text> </msg>」、一个还是「</text></msg>」，指纹
    对不上，同一条回复就在窗口里留了两份（窗口页同一条出现两遍）。现在
    assistant 直接取 <text> 段里的正文来比，包装怎么变都对得上；user 打包
    消息的格式是稳定的，仍走原来的剥前缀逻辑。
    """
    role = _get(msg, "role") or ""
    text = _flat_text(_get(msg, "content"))
    if not text:
        return ""
    body = ""
    if role == "assistant":
        segs = _TEXT_SEG_RE.findall(text)
        if segs:
            body = " ".join(segs)
    if not body:
        body = _MSG_ID_ATTR_RE.sub("", text)
        body = _ID_RE.sub("", body)
        body = _TIME_PREFIX_RE.sub("", body)
    # v3.55（用户 m14082 根因）：表情两种写法不参与指纹。原件是
    # "<text>正文</text><emoji>128</emoji>"、我们补的占位是
    # "<text>正文[Emoji 128]</text>"——正文差着一个标记，指纹就对不上，
    # 占位永远认领不回来（每个带表情的回复在窗口里多一条）。
    body = _EMOJI_TAG_RE.sub("", body)
    body = _HIST_LITERAL_RE.sub("", body)
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""
    digest = hashlib.md5(body.encode("utf-8", "replace")).hexdigest()
    return f"{role}|{digest}"


#: 用户 m13676：模型这一轮选择「不回复」时，框架照样会写一条 assistant 记忆。
#: 这种条目只有时间、名字和一串空标签（渲染出来就是「[19:45] [樱井桃华]」），
#: 没有正文也没有图——留在窗口里白占 token，还会被当成「bot 刚说过话」拼进下一轮。
_BOT_PIC_HINT_RE = re.compile(
    r"\[(?:图片|Image attached|表情|Json card)\]|<image\b|<img\b", re.I)


def empty_bot_reply(msg: Any) -> bool:
    """这条助手消息是不是「什么都没说」（用户 m13676）。"""
    if (_get(msg, "role") or "") != "assistant":
        return False
    # 决策注记是插件自己写进去的结论，不是 bot 说的话，永远保留
    if _get(msg, "decnote"):
        return False
    for key in ("media", "media_refs", "media_path", "images"):
        if _get(msg, key):
            return False
    text = _flat_text(_get(msg, "content"))
    if not text:
        return True
    # 带图 / 带卡片的不算空
    if _BOT_PIC_HINT_RE.search(text):
        return False
    # 只发表情也是回复，别当空消息删掉
    if re.search(r"<emoji>|\[Emoji\b", text, re.I):
        return False
    segs = _TEXT_SEG_RE.findall(text)
    body = " ".join(segs) if segs else text
    body = _EMOJI_TAG_RE.sub("", body)
    body = _HIST_LITERAL_RE.sub("", body)
    body = re.sub(r"<[^<>\n]{0,200}>", "", body)      # <msg …> / </msg> / <text> / <time>
    body = re.sub(r"\[[^\[\]\n]{0,40}\]", "", body)  # 时间前缀、[名字]、[图片] 之类
    body = _ID_RE.sub("", body)
    return not re.search(r"[\w\u4e00-\u9fff]", body)


def drop_empty_bot_replies(messages):
    """丢掉「什么都没说」的助手消息，返回 (留下的列表, 丢掉的条数)。"""
    out = []
    gone = 0
    for m in messages or []:
        if empty_bot_reply(m):
            gone += 1
            continue
        out.append(m)
    return out, gone


def clone_message(msg: Any) -> Any:
    """浅层复制一条消息对象，避免和框架的记忆对象共用同一份。"""
    if isinstance(msg, dict):
        return dict(msg)
    copier = getattr(msg, "model_copy", None)
    if callable(copier):
        try:
            return copier(deep=True)
        except Exception:
            try:
                return copier()
            except Exception:
                return msg
    return msg


@dataclass
class WindowState:
    """一个会话的窗口状态。纯内存，插件卸载即丢。"""

    sid: str
    messages: List[Any] = field(default_factory=list)
    trimmed: int = 0
    # 已经进来、但还没被任何一轮请求带走的群消息。给「一直能看到的观察窗」用：
    # 用户随时能看见「下一轮 agent 会看到什么」，不用等 agent 跑起来。
    pending: List[dict] = field(default_factory=list)
    # 进阶决策（v3.91，用户 m05834）：回与不回都记一条，下一轮判断与下一轮回复
    # 都要能看到「上一轮是怎么想的」。它不是窗口消息，不进条数统计、也不参与裁切。
    decisions: List[dict] = field(default_factory=list)
    # 已见过的框架消息身份独立于可裁切正文，热重载后也不能把旧回复当新消息。
    framework_seen_ids: List[str] = field(default_factory=list)

    def size(self) -> int:
        return len(self.messages)

    def snapshot(self) -> dict:
        return {
            "sid": self.sid,
            "size": self.size(),
            "trimmed": self.trimmed,
            "pending": len(self.pending),
            "decisions": len(self.decisions),
        }

    def to_dict(self) -> dict:
        """落盘用：消息统一转成 {role, content, ...} 的纯 JSON 结构。"""
        return {
            "sid": self.sid,
            "messages": [_msg_to_json(m) for m in self.messages],
            "trimmed": self.trimmed,
            # 待发消息也跟着窗口走，否则热重载后「下一轮会看到什么」会空掉
            "pending": [p for p in self.pending if isinstance(p, dict)],
            # 决策记录同理：热重载后接着用，不然模型会「忘了上一轮怎么判的」
            "decisions": [d for d in self.decisions if isinstance(d, dict)],
            "framework_seen_ids": list(self.framework_seen_ids[-4096:]),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WindowState":
        msgs = data.get("messages") or []
        return cls(
            sid=str(data.get("sid") or ""),
            messages=[m for m in msgs if isinstance(m, dict)],
            trimmed=int(data.get("trimmed") or 0),
            pending=[p for p in (data.get("pending") or []) if isinstance(p, dict)],
            decisions=[d for d in (data.get("decisions") or []) if isinstance(d, dict)],
            framework_seen_ids=[str(mid) for mid in (data.get("framework_seen_ids") or [])][-4096:],
        )


def _msg_to_json(msg: Any) -> dict:
    """把一条消息压成能 json.dump 的 dict（多模态 content 原样保留）。"""
    if isinstance(msg, dict):
        out = {}
        for k in ("role", "content", "tool_calls", "tool_call_id", "name"):
            if k in msg:
                out[k] = msg[k]
        return out
    out = {"role": _get(msg, "role") or "user", "content": _get(msg, "content")}
    tc = _get(msg, "tool_calls")
    if tc:
        out["tool_calls"] = tc
    tcid = _get(msg, "tool_call_id")
    if tcid:
        out["tool_call_id"] = tcid
    nm = _get(msg, "name")
    if nm:
        out["name"] = nm
    return out


def _locate(messages: List[Any], msg: Any) -> int:
    """在 messages 里找和 msg 对应的位置：先比指纹，再比 message_id。"""
    fp = fingerprint(msg)
    for i in range(len(messages) - 1, -1, -1):
        if fingerprint(messages[i]) == fp:
            return i
    ids = ids_of([msg])
    if ids:
        for i in range(len(messages) - 1, -1, -1):
            if ids & ids_of([messages[i]]):
                return i
    return -1


def dedupe_echo_placeholders(messages) -> List[Any]:
    """删掉窗口里「与带 message_id 原件同正文」的 bot 回显占位（v3.37）。

    占位是 _pending_messages 替 bot 补的（mark=="bot"），本身没有 message_id；
    框架后来把原件写进记忆时本该原位替换，可旧指纹把原件末尾的换行算成了
    不同（见 echo_key），结果两条都留在窗口里——窗口页同一条回复出现两遍
    （用户 m12023 的图）。这里按正文收口：同一正文既有原件又有占位时只留
    原件。两条都带 id 的不动，那可能是 bot 真的连发了两句一样的话。
    """
    msgs = list(messages or [])
    if not msgs:
        return msgs
    origin: Set[str] = set()
    for m in msgs:
        if (_get(m, "role") or "") != "assistant" or not ids_of([m]):
            continue
        k = echo_key(m)
        if k:
            origin.add(k)
    if not origin:
        return msgs
    out: List[Any] = []
    dropped = 0
    for m in msgs:
        if (_get(m, "role") or "") == "assistant" and not ids_of([m]):
            k = echo_key(m)
            if k and k in origin:
                dropped += 1
                continue
        out.append(m)
    return out if dropped else msgs


#: 表情在历史里出现的样子：<emoji>21</emoji> 或 [Emoji 21] / [Emoji 汪汪 (ID: 277)]
_HIST_EMOJI_RE = re.compile(r"<emoji>|\[Emoji\b", re.I)
#: 正文里的字面表情标记（摘掉之后再比正文，两种写法才认得出是同一条）
_HIST_LITERAL_RE = re.compile(r"[ \t]*\[Emoji\b[^\]]*\]", re.I)


def _has_emoji(msg: Any) -> bool:
    """这条消息里有没有表情（标签或文字写法都算）。"""
    return bool(_HIST_EMOJI_RE.search(_flat_text(_get(msg, "content"))))


def _reply_key(msg: Any) -> str:
    """相邻重复判定用的正文指纹（v3.54）。

    原先比 echo_key 多剥一层字面表情标记，v3.55 起表情两种写法（<emoji>N</emoji>
    与 [Emoji N]）在 echo_key 里就已经不参与指纹了，所以这里直接复用。
    """
    return echo_key(msg)


def dedupe_same_reply(messages) -> List[Any]:
    """删掉窗口里「相邻两条 bot 回复正文一模一样」的后一条（v3.54）。

    用户 m14082「早上的几条消息 bot 会重复发送」：模型把同一条内容写成两版
    （一版 `<emoji>128</emoji>`、一版正文里内联 `[Emoji 128]`）时框架两条都
    发、都写进记忆，窗口里也就成了成对的两条。发出去之前已经拦了
    （decision/output_guard.py），这里再把更早留下的成对重复清掉——不然历史里
    摆着成对的先例，模型下一轮还照着学。

    只认相邻、只认 assistant、只在有表情参与时合并（正文按 echo_key 比：
    时间前缀、message_id、表情写法都不参与），免得把 bot 真连说两句一样的
    话也一并吃掉。
    """
    msgs = list(messages or [])
    if len(msgs) < 2:
        return msgs
    out: List[Any] = []
    dropped = 0
    for m in msgs:
        if out and (_get(m, "role") or "") == "assistant" \
                and (_get(out[-1], "role") or "") == "assistant":
            k = _reply_key(m)
            if k and k == _reply_key(out[-1]) and (_has_emoji(m) or _has_emoji(out[-1])):
                dropped += 1
                continue
        out.append(m)
    return out if dropped else msgs


def patch_forward_shells(messages) -> List[Any]:
    """把正文里的空壳转发换成真内容。

    框架只给了 {"type":"forward"} 时，历史里就是这个样子：
      [18:40] [message_id: -326662202] [user_nickname: 千夜丶修, …] | [Forward]
    插件收到消息时会自己问 NapCat 把内容取回来（decision/forward_fetch.py），
    这里按 message_id 把那份内容换进去；没补到过的那几条原样留着，不编内容。
    """
    try:
        from ..decision.forward_fetch import patch_text
    except Exception:
        return list(messages or [])
    out = []
    for msg in messages or []:
        content = _get(msg, "content")
        if not isinstance(content, str) or "[Forward" not in content:
            out.append(msg)
            continue
        new = patch_text(content)
        if new == content:
            out.append(msg)
            continue
        copy = clone_message(msg)
        try:
            setattr(copy, "content", new)
        except Exception:
            if isinstance(copy, dict):
                copy["content"] = new
            else:
                out.append(msg)
                continue
        out.append(copy)
    return out


def merge_framework_messages(state: WindowState, framework_messages, seed_limit: int = 0,
                             anchor_msg=None, require_anchor: bool = False,
                             trusted_messages=None, mention_names=None) -> int:
    """把框架这次给的历史增量并进窗口缓冲，返回新增条数。

    - 先拆包：框架常把一批群消息拼成一条 user，窗口里一条一条存
    - 尾部已覆盖 → 不动
    - 找到窗口末条在框架历史里的位置 → 它之后的全是新增
    - 一条都对不上 → 群聊不补未知历史；兼容调用保守补一条，绝不重锚
    - trusted_messages 来自本地已接收的待发，独立按序合并，不依赖框架锚点

    anchor_msg：拿哪一条当锚点。默认是窗口最后一条；但决策注记（v4.03）是插件
    自己写进窗口的，框架历史里没有它，拿它当锚点必然对不上，于是每轮都退化成
    「只补最后一条」，中间新增的消息全丢。调用方要把最后一条非注记消息传进来。

    mention_names：唤醒标记用的名字（主名 + 外号）。给了就把窗口里所有用户消息
    重扫一遍：标记落在窗口本体上，页面、决策记录、回复请求三处看到的是同一份，
    不会出现「刚收到那条有、被框架历史覆盖之后就没了」（用户 m12833）。
    """
    from .split import expand_packed_messages, mark_copies, mark_messages

    # 老窗口里还留着「一条 assistant 塞好几段 <msg>」的形态（v2.57 之前只拆 user）。
    # 每次 collect 顺手迁移一次，条数和页面当场就对了，不用等窗口重来。
    if state.messages:
        flat = expand_packed_messages(state.messages)
        if len(flat) != len(state.messages):
            state.messages = flat
        # v3.37（用户 m12023）：旧指纹把原件末尾的换行算成不同，占位和原件
        # 都留在了窗口里，同一条回复显示两遍。每次 collect 顺手收口一次，
        # 存量重复当场清掉，不用等插件重启。
        deduped = dedupe_echo_placeholders(state.messages)
        if len(deduped) != len(state.messages):
            state.messages = deduped
        # v3.54（用户 m14082）：模型把同一条回复写成两版时，框架两条都发、都进
        # 记忆，窗口里就成了成对重复。每轮 collect 顺手清一次，别让成对的先例
        # 一直教模型继续成对地写。
        deduped = dedupe_same_reply(state.messages)
        if len(deduped) != len(state.messages):
            state.messages = deduped
        # 用户 m13676（图1「[19:45] [樱井桃华]」）：模型选择不回复的那一轮，框架
        # 也会写一条空 assistant。存量顺手清一次，别让它一直躺在窗口里被当成
        # 「bot 刚说过话」拼进下一轮。
        kept, empty_gone = drop_empty_bot_replies(state.messages)
        if empty_gone:
            state.messages = kept
            logger.debug("[multi-decision] 窗口清掉空回复 %d 条", empty_gone)
        # 存量补标记：老窗口里那些行本来没标，也不会自己长出来，每轮顺手扫一次
        # （幂等，已经带标记的行原样跳过）。用户 m12833：@你 只在刚收到那一阵显示，
        # 被框架历史覆盖之后就没了。
        if mention_names:
            marked = mark_messages(state.messages, mention_names)
            if marked:
                logger.debug("[multi-decision] 窗口补唤醒标记 %d 条", marked)

    incoming = expand_packed_messages([
        m for m in (framework_messages or [])
        if (_get(m, "role") or "") != "system"
    ])
    incoming = patch_forward_shells(incoming)
    trusted = expand_packed_messages(list(trusted_messages or []))
    # 用户 m13676：框架回传的历史里也可能夹着「什么都没说」的助手条目，
    # 进来之前先丢掉，别让它在窗口里长住。
    incoming, _gone_in = drop_empty_bot_replies(incoming)
    trusted, _gone_tr = drop_empty_bot_replies(trusted)
    # 打标走副本：框架给的消息对象是它自己的记忆条目，就地改会把 [@你] 写回框架那边
    if mention_names:
        incoming = mark_copies(incoming, mention_names)
        trusted = mark_copies(trusted, mention_names)
    seen_before = set(state.framework_seen_ids)
    # 记录框架确实回传过的身份，而不是仅记当前保留的窗口。去重表有界、无正文。
    seen_order = dict.fromkeys(state.framework_seen_ids)
    for msg in incoming:
        for mid in sorted(ids_of([msg])):
            seen_order.pop(mid, None)
            seen_order[mid] = None
    state.framework_seen_ids = list(seen_order)[-4096:]
    if not incoming and not trusted:
        return 0

    if not state.messages:
        incoming = incoming + trusted
        # 冷启动：拿框架当前这段历史当起点（v2.32 回退 v2.31）。
        #
        # v2.31 曾让窗口留空、每轮用框架历史原样发出去，结果是每轮都发
        # 「框架记忆的最后 N 条」——框架记忆自己是滑动的，最老的一条每轮被挤出去，
        # 于是首条一变、整段前缀全断，缓存命中率怎么都上不去（用户 m22800）。
        # 窗口必须真正接管并累积，裁切交给 trim（超阈值才裁）。
        # seed_limit > 0 只取最后 N 条；= 0（默认）全取。
        if seed_limit:
            if len(incoming) > seed_limit:
                incoming = incoming[-seed_limit:]
        # v3.55：冷启动这批也来自框架记忆 + 待发占位，记忆里可能已经留着成对的
        # 重复（一条带 <emoji>、一条正文内联 [Emoji 128]），占位也可能是框架已经把
        # 原件写进记忆之后剩下的。这里先收口再灌，免得重启后的第一轮就把成对先例
        # 发给模型。
        seeded = dedupe_same_reply(
            dedupe_echo_placeholders([clone_message(m) for m in incoming]))
        state.messages = seeded
        if not seeded:            # 理论上收不空；真空了就别去取 [-1]
            return 0
        return len(seeded)

    # v4.03（用户 m11947）：锚点由调用方指定（跳过决策注记），默认仍是窗口末条。
    probe = anchor_msg if anchor_msg is not None else state.messages[-1]
    anchor = _locate(incoming, probe)
    # v4.06（用户 m12879）：判要回那一轮的本轮消息是先并进窗口、后落决策注记的，
    # 它们框架历史里还没有，拿窗口末条当锚点必然对不上，于是退化成「只补一条」。
    # 这里从窗口末尾往回找第一条能在框架历史里定位的消息当锚点——不重锚，也不丢。
    if anchor < 0:
        for _cand in reversed(state.messages):
            _a = _locate(incoming, _cand)
            if _a >= 0:
                anchor = _a
                break
    if anchor >= 0:
        tail = incoming[anchor + 1:]
    else:
        # 完全对不上：v2.35 起不再重锚。
        #
        # 重锚 = 把窗口整个换成框架记忆的最后 N 条。框架记忆自己是滑动的，
        # 换进去之后窗口首条每轮都不一样，整段前缀作废、缓存命中率掉回去；
        # 页面上还会看到窗口条数一会儿一百多一会儿几十条（用户 m23347）。
        # 现在只保守补一条：框架最新那条，而且它里面的 message_id 一个都不能
        # 已经在窗口里出现过——有重叠就当「已经有了」，宁可少一条也不重复。
        # 群聊由本地实时窗接管：失去衔接证据时，框架最后一条也可能是几小时前的回复。
        # 私聊/兼容调用仍保留原来的保守补一条策略；本地待发另走可信来源。
        if require_anchor:
            placeholders = {echo_key(m) for m in state.messages
                            if (_get(m, "role") or "") == "assistant" and not ids_of([m])}
            # 失锚时仅允许升级仍在本窗的已发送占位，不能把陌生旧输出追加回来。
            tail = [m for m in incoming if ids_of([m]) and echo_key(m) in placeholders]
        else:
            tail = incoming[-1:]

    tail = [(msg, False) for msg in tail] + [(msg, True) for msg in trusted]
    return _merge_tail(state, tail, seen_before)

def _merge_tail(state: WindowState, tail, seen_before=None) -> int:
    """把一批待并消息逐条去重后并入窗口，返回新增条数。

    tail 每项是 (msg, is_trusted)。trusted 是本地已接收的待发，它们本来就没被
    框架回传过，所以不做「框架已给过」拦截，否则整批会被误杀。

    框架回传（merge_framework_messages）与判不回就地并待发（merge_trusted_messages）
    共用这一份：按编号认、按内容指纹认、占位与原件互斥、占位升级原件、并完再
    收口成对重复。两边以前各写一套，标准不一样——v3.37（同一条回复显示两遍）
    与 v3.54（模型学着成对地写）都只修在主路上，小路上原样复现。
    """
    have_ids = ids_of(state.messages)
    have_fp = {fingerprint(m) for m in state.messages}
    # 用户 m11091 第 5 条：窗口里可能已经有「替 bot 补的占位」（发送时还没有
    # message_id）。框架把同一条原件写进记忆后，按正文认领、原位替换，不新增
    # 也不留两条——占位与原件只差时间前缀和 message_id。
    echo_at: dict = {}
    for i, m in enumerate(state.messages):
        k = echo_key(m)
        if k:
            echo_at.setdefault(k, i)
    fresh: List[Any] = []
    fresh_echo = set()
    for msg, is_trusted in tail:
        ids = ids_of([msg])
        fp = fingerprint(msg)
        if ids:
            if ids <= have_ids:
                continue
        elif fp in have_fp:
            continue
        k = echo_key(msg)
        if not ids and k and (k in fresh_echo or k in echo_at):
            # 同一批 incoming 里既有原件又有占位（框架历史在前、待发在后），
            # 或者窗口里已经有一模一样的正文：只留原件，别再补一条占位
            continue
        if not is_trusted and seen_before and ids and ids <= seen_before:
            continue
        if ids and k and k in echo_at:
            idx = echo_at.pop(k)
            # 只有无身份的回显占位可升级；同文但不同 ID 的两次真实输出不能互相覆盖。
            if (0 <= idx < len(state.messages) and not ids_of([state.messages[idx]])
                    and (_get(msg, "role") or "") == "assistant"):
                upgraded = clone_message(msg)
                old_stamp = _TIME_PREFIX_RE.match(_flat_text(_get(state.messages[idx], "content")))
                content = _get(upgraded, "content")
                if old_stamp and isinstance(content, str) and not _TIME_PREFIX_RE.match(content):
                    content = old_stamp.group(0) + content
                    if isinstance(upgraded, dict):
                        upgraded["content"] = content
                    else:
                        upgraded = upgraded.model_copy(update={"content": content})
                state.messages[idx] = upgraded
                have_ids |= ids
                have_fp.add(fp)
                continue
        fresh.append(clone_message(msg))
        have_ids |= ids
        have_fp.add(fp)
        if k:
            fresh_echo.add(k)

    if fresh:
        state.messages.extend(fresh)
    # v3.55（用户 m14082 根因）：框架记忆里已经成对写进去的重复（一条
    # "<text>正文</text><emoji>128</emoji>"、一条正文里内联 "[Emoji 128]"）每轮
    # 都会顺着增量再并进来一次。并完立刻再收口一次，别让这一轮的请求里就带着
    # 成对的先例去教模型继续成对地写。
    deduped = dedupe_same_reply(state.messages)
    if len(deduped) != len(state.messages):
        gone = len(state.messages) - len(deduped)
        state.messages = deduped
        return max(0, len(fresh) - gone)
    return len(fresh)


def merge_trusted_messages(state: WindowState, messages) -> int:
    """把一批本地待发并进窗口，返回新增条数（v3.96 判不回就地定格用）。

    以前这里另写了一套精简去重，只比 message_id 与正文全等，也不做前后收口：
    占位认不出原件、成对重复不清，主路修过的两个重复 bug 就地复现。现在整段
    交给 _merge_tail，与框架回传那条路同一套标准；区别只是这批不做
    「框架已给过」拦截——它们正是框架还没回传的本地待发。
    """
    from .split import expand_packed_messages

    incoming = expand_packed_messages([
        m for m in (messages or [])
        if (_get(m, "role") or "") != "system"
    ])
    # 窗口里的旧形态先收口：打包成一条的、占位与原件并存的、成对重复的，
    # 不收口就认不准「这个我到底有没有」。待发为空也跑一次——判不回的那一轮
    # 顺手把存量重复清掉，跟 collect 那条路的行为一致。
    if state.messages:
        flat = expand_packed_messages(state.messages)
        if len(flat) != len(state.messages):
            state.messages = flat
        deduped = dedupe_echo_placeholders(state.messages)
        if len(deduped) != len(state.messages):
            state.messages = deduped
        deduped = dedupe_same_reply(state.messages)
        if len(deduped) != len(state.messages):
            state.messages = deduped
    if not incoming:
        return 0
    return _merge_tail(state, [(m, True) for m in incoming], None)

