"""进阶决策（两段式）：先用实时窗口问一遍模型「要不要回」，要回再走完整 agent。

用户 m05749 / m05834 / m05856 / m05902。

原来的流程是一段式：判定（计数/概率/曲线/动态调整）一过，就直接
flush_session_messages 让框架带着完整系统提示词跑 agent。进阶模式在两段之间
插一层：把实时窗口（还没有系统提示词、没有工具、没有记忆）摊成一段聊天记录，
交给一个便宜/快的模型判断「此刻该不该开口」；只有它说回，才继续走原来的完整流程。

几件事按用户的要求定死：

* 决策用的 system 由五段拼成，顺序 = 人格 → 聊天规则 → 决策提示词 → 工具说明 →
  身份环境。人格只取「纯人设正文」，不读人格里的群聊规则/私聊规则
  （用户 m05902：那两块由「聊天规则提示词」这一栏自己写）。
* 工具是双通道：勾选的工具既写进提示词（告诉模型能干什么），也作为原生 tool_set
  一起发；模型真发起 tool_calls 就执行并把结果回填，最多 tool_rounds 轮。
* 模型链：主模型 → 失败重试 retry 次（每次间隔 retry_delay 秒）→ 连续
  fail_switch 次整体失败后切 fallback 模型，并在 fallback_cooldown 秒内
  直接用 fallback；冷却结束后先试主模型。
* 回不回都要在窗口里留一条决策记录（用户 m05834）：那是 assistant 角色的一条
  固定消息，不是动态段，所以下一轮决策还能看到它。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .sids import bot_name

# v4.65（用户 m36234）：这条链路以前用模块自己的 logger，而插件的文件日志只挂在
# get_logger("multi_decision") 上——「决策带图失败」这类告警压根没写进 log.log，
# 事后想查「带图那次到底为什么没成」无从查起。跟 engine / request_log 用同一个。
try:  # 离线测试里 core 不一定在
    from core.logging_manager import get_logger as _plugin_logger  # type: ignore
except Exception:  # pragma: no cover - 只在离线测试里
    _plugin_logger = None  # type: ignore
logger = _plugin_logger("multi_decision", "orange") if _plugin_logger else logging.getLogger(__name__)

try:  # 离线测试里 core 不一定在
    from core.provider import LLMRequest  # type: ignore
except Exception:  # pragma: no cover - 只在真机上有
    LLMRequest = None  # type: ignore

try:  # 离线测试里 core 不一定在
    from core.agent.tool import ToolResult  # type: ignore
except Exception:  # pragma: no cover - 老框架/测试桩没有工具结果类型
    ToolResult = None  # type: ignore


def _comparison_snapshot(messages, dynamic: str = "") -> dict:
    """只旁路观察基础聊天；记录失败不得影响模型发送。"""
    try:
        from .prefix_projection import build_fingerprints
        return build_fingerprints(messages, dynamic)
    except Exception:
        logger.debug("[multi-decision] 聊天比较签名不可用，退回记录估算", exc_info=True)
        return {}


# 2026-11-08（J）：最上面单独一块「角色设定」——先说清判断的人不是 bot 本身，它只负责判断。
# 正文里要指代 bot 的地方写{{bot}}，装配时换成这个会话里它自己的昵称（没设就退全局名）。
DECISION_ROLE = """### 角色设定
我不是{{bot}}，我只负责判断这条群消息此刻要不要开口。要不要说话、说什么，都是{{bot}}的事。
"""

# 决策提示词默认稿。用户 m05902 要「可以预填充一些简单通用的」，所以这里写的是一份
# 能直接用的判断规则，不是空壳；设置页首次打开时把这段当默认值显示。
# 用户 m01209（图2/图3）要求：决策词写成通用、偏「节奏控制」的版本——群里本来就是
# 话题选择性聊天，别人在说话不等于我不能接，判的是「这一句接进去会不会打断」。
# 同时把「怎么答」抽成独立的一块（DEFAULT_DECISION_FORMAT），提示词里不再出现 say：
# 决策那一步只决定回不回，不引导角色说什么。
# 2026-11-08（J）：判断规则只管回不回，凡带说话方向的措辞一律不要——原来的「接一句能
# 顺着往下走」改成「插一句不会把话打断」，原来在「该接」里的「先弄清是什么意思」挪进
# 「别接」，写成「拿不准在说什么就别硬接」。
# 结构固定五段：该接 / 别接 / 是不是在跟我说 / 分寸 / 底线。
DEFAULT_DECISION_PROMPT = """你在判断：这条群消息，此刻要不要开口。
只判断回不回。说什么、怎么说、用什么语气，都不是这一步的事，留给下一步。
看的是节奏：这一句接进去会不会打断，现在该不该有{{bot}}的声音。

### 该接
- 有人 @{{bot}}、叫{{bot}}的名字或昵称、直接回复{{bot}}的消息——一条就够；
- 有人自我宣告（「睡了」「晚安」「早」「来了」）——一条就够；
- 有人情绪吐槽（「好累」「不想上班」）——同一个人说到第二条再接；
- 两三个人在实质聊天，{{bot}}插一句不会把话打断。别人在互相说话，不等于不该插话；
- 有人分享自己的经历；
- 有人问了个完整的问题，还没人接；
- 明显在玩梗、接梗、双关、谐音梗；
- 三个人以上围着同一个话题聊得很热——偶尔插一句，不逐轮跟。

### 别接
- 两个人或几个人正在点对点说一件具体的事（约时间、吵架、交接事情、私人约定）；
- 最近一条已经过去 5 分钟以上、中间没有新消息接着——不起话头；
- 5 分钟内有人（包括{{bot}}自己）说过差不多的话——不重复；
- 只是表情、「哈哈」「草」「6」这类附和，没有可接的内容；
- 出现不认识的词、黑话、简称，拿不准在说什么——别硬接；
- 消息里带链接：纯链接、链接加一句话、视频没有文字评论；
- 小程序、卡片分享；
- 被反复 @、反复点名，可翻来覆去只有「在吗」「人呢」「理我」这类，没有实质内容——可以拒回，不必每次都应付，等他说出真正想说的再接；
- 广告、刷屏、无意义重复、完全看不懂在聊什么。

### 这一句是不是在跟{{bot}}说
- @{{bot}}、叫{{bot}}的名字或昵称、直接回复{{bot}}的消息 → 是；
- 没有指向的「你」→ 不是；只有当「你」明显在接{{bot}}上一句时才算；
- 「前辈」「大佬」这类泛称 → 不是；
- 「bot」「机器人」→ 不是；
- @的是别人 → 不是；
- 几点钟不影响判断，凌晨深夜一样按上面走。

### 分寸
- 同一个人连着发的几条算一件事，别当成好几轮；
- 同一个话题连着回不超过 3 轮；话题换了重新算；
- 被追问时回一两轮就主动收；
- 一模一样的话由不同人说出来，只当一次触发；
- 上一轮答得不准，可以补一句，不算进连续轮数；
- 不主动分析 5 分钟前的消息；旧消息只用来理解背景，除非被当前消息直接引用或追问。

### 底线
宁可少回，不要硬回；但话题合适时也不用等着被点名。"""

# 「输出规则」独立一块（用户图3）。决策那一步只回一个判断，不替角色写台词。
# 2026-11-08（J）：reason 里不再补建议——决策只给结论，怎么回是下一步的事；
# 早前那条「理由里再补一条对回复的建议」按此撤掉。
DEFAULT_DECISION_FORMAT = """### 怎么回答
只输出一个 JSON 对象，不要写别的，不要代码块，不要解释：
{"reply": true, "reason": "一句话理由，20 字以内，给用户看"}

reason 只说为什么回 / 为什么不回，半句话就够。
不写打算怎么回，不写往哪个方向接，不给建议——那是真正开口那一步的事。
不回就把 reply 写成 false，理由照样写清楚。

### 一次只回一个人
群里可能同时几个人在说话。回的时候挑最该回的那一个人，只对着他说；
不要一条里同时应付好几个人，也不要挨个都答一遍。"""

# 用户 m36396：决策这一侧也补一段「强化格式遵守」，位置跟回复页那一块一模一样——贴在本轮消息最后。
# 回复那块讲的是「用 <msg> 包气泡、空壳代表不发」；决策只回一个判断，这块讲的是「只准吐一个 JSON」。
# 真踩过：模型不吐 JSON、只回一句聊天话（"[16:55] 判定不回（……）"），那一轮按失败算。
DECISION_FORMAT_HARD = """【格式强化】
只输出一个 JSON 对象，前后不写任何字、不加代码块，字段名照抄：
{"reply": true, "reason": "一句话理由，20 字以内"}
{"reply": false, "reason": "一句话理由，20 字以内"}
reply 只能是 true 或 false；想说的都写进 reason；拿不准写 false。"""

# 用户 m36396：没按 JSON 回本身就是一次失败。重试时把这句话贴在最后，
# 别让模型拿着原样的 prompt 原地重复犯错（以前重试是完全相同的消息，它照样再犯一遍）。
DECISION_RETRY_NOTE = """（提醒）上一条没有按要求的格式回。这一次只输出一个 JSON 对象：
{"reply": true, "reason": "一句话理由，20 字以内"}
前面不要写解释，后面不要写别的，也不要加代码块。"""

# 老版本的默认决策提示词（带 say 字段、偏保守那版）。用户在设置页存过一次，这段文字
# 就落进了配置里；光改上面的常量不会生效，所以读配置时按「逐字相同」认出来换成新稿。
# 用户 m01209 图3：决策词不要引导角色说什么，say 这段说明必须消失。
LEGACY_DECISION_PROMPT = """你是群里的「要不要开口」判断器。读完人设、聊天规则和下面的聊天记录，
只回答一件事：此刻这一句，我该不该接。

该接：
- 有人 @ 我、叫我的名字、或者直接回复我的消息；
- 话题我确实有话说，接一句能顺着聊下去；
- 有人在提问，而没有人接。

不该接：
- 只是表情、图片、「哈哈」「草」这类附和，没有可接的内容；
- 广告、刷屏、无意义重复、纯链接；
- 刚回过不久，再回就是刷屏；
- 看不懂在说什么。

宁可少回，不要硬回。只输出一个 JSON 对象，不要写别的：
{"reply": true, "reason": "一句话理由，20 字以内，给用户看", "say": "如果回，打算往哪个方向说，一句话，可留空"}
不要回就把 reply 写成 false，reason 照样写清楚。"""


def migrate_prompt(text):
    """老默认稿 -> 新稿。用户自己改过的（差一个字都算改过）原样返回，绝不覆盖。"""
    got = str(text or "")
    if not got.strip():
        return DEFAULT_DECISION_PROMPT
    if got.strip() == LEGACY_DECISION_PROMPT.strip():
        return DEFAULT_DECISION_PROMPT
    return got


# 用户 m01209 图2：决策规则可以拆成几个小模块写。这几条是「＋ 新增模块」里能直接挑的
# 骨架，挑出来就是一块能改的正文，不用把整段判断规则全塞在一块里。
# 2026-11-08（J）：骨架对齐默认稿的五个小标题，标题写法也跟默认稿一致（### ）。
DECISION_SKELETONS = (
    ("when-yes", "什么时候接", """### 该接
- 有人 @{{bot}}、叫{{bot}}的名字或昵称、直接回复{{bot}}的消息——一条就够；
- 有人自我宣告（「睡了」「晚安」「早」「来了」）——一条就够；
- 有人情绪吐槽（「好累」「不想上班」）——同一个人说到第二条再接；
- 两三个人在实质聊天，{{bot}}插一句不会把话打断。别人在互相说话，不等于不该插话；
- 有人分享自己的经历；
- 有人问了个完整的问题，还没人接；
- 明显在玩梗、接梗、双关、谐音梗；
- 三个人以上围着同一个话题聊得很热——偶尔插一句，不逐轮跟。"""),
    ("when-no", "什么时候别接", """### 别接
- 两个人或几个人正在点对点说一件具体的事（约时间、吵架、交接事情、私人约定）；
- 最近一条已经过去 5 分钟以上、中间没有新消息接着——不起话头；
- 5 分钟内有人（包括{{bot}}自己）说过差不多的话——不重复；
- 只是表情、「哈哈」「草」「6」这类附和，没有可接的内容；
- 出现不认识的词、黑话、简称，拿不准在说什么——别硬接；
- 消息里带链接：纯链接、链接加一句话、视频没有文字评论；
- 小程序、卡片分享；
- 被反复 @、反复点名，可翻来覆去只有「在吗」「人呢」「理我」这类，没有实质内容——可以拒回，不必每次都应付，等他说出真正想说的再接；
- 广告、刷屏、无意义重复、完全看不懂在聊什么。"""),
    ("who", "是不是在跟我说", """### 这一句是不是在跟{{bot}}说
- @{{bot}}、叫{{bot}}的名字或昵称、直接回复{{bot}}的消息 → 是；
- 没有指向的「你」→ 不是；只有当「你」明显在接{{bot}}上一句时才算；
- 「前辈」「大佬」这类泛称 → 不是；
- 「bot」「机器人」→ 不是；
- @的是别人 → 不是；
- 几点钟不影响判断，凌晨深夜一样按上面走。"""),
    ("pace", "分寸", """### 分寸
- 同一个人连着发的几条算一件事，别当成好几轮；
- 同一个话题连着回不超过 3 轮；话题换了重新算；
- 被追问时回一两轮就主动收；
- 一模一样的话由不同人说出来，只当一次触发；
- 上一轮答得不准，可以补一句，不算进连续轮数；
- 不主动分析 5 分钟前的消息；旧消息只用来理解背景，除非被当前消息直接引用或追问。"""),
    ("bottom", "底线", """### 底线
宁可少回，不要硬回；但话题合适时也不用等着被点名。"""),
)

# 「聊天规则提示词」默认稿。用户 m05902 要求注释里写清楚写什么（语气、喜欢聊什么），
# 并预填一份简单的示例，所以这段既是说明也是模板。
DEFAULT_CHAT_RULES = """【说话语气】口语、短句，像群里一个正常人随手打字；不用书面语，不写「首先/其次/总之」。
【喜欢聊什么】二次元、游戏、动画画风、日常吐槽；感兴趣的话题就接一句，不感兴趣就略过。
【不聊什么】政治、别人的隐私、看不懂的梗不硬接。
【分寸】别人在说正事、吵架、发广告时不插嘴；被点到名才正经回答。
【长度】一般一两句，最多三行。"""


TOOL_HEAD = """需要外部信息（查资料、算数、看时间之类）时可以调工具，最多 %d 轮。
工具结果会贴回给你，看到结果后再给最终判断。如果调用轮数到上限仍没有合适的结果，就直接回复告知。"""


# 回复链的工具说明（用户 m13442 第 3 条）：回复这一轮最后一定要开口，所以轮数要留一轮
# 给「看完工具结果再写回复」——%d 传的是「回复侧最大循环轮数 − 1」，不是决策那边的数。
REPLY_TOOL_HEAD = """下面是你可以使用的工具，最多调用 %d 轮。
工具结果会贴回给你，看到结果后再给最终判断，然后进行回复。如果调用轮数到上限仍没有合适的结果，就直接回复告知。"""


# 模型回「回」的各种写法。判断句只认这些，其余一律当「不回」——宁可漏，不要硬回。
_YES = {"true", "yes", "y", "1", "reply", "回", "回复", "要回", "该回", "是"}
_NO = {"false", "no", "n", "0", "skip", "不回", "不用回", "略过", "否"}


def _field(obj, key, default=None):
    """框架消息是 OpenAIMessage 对象、测试里是 dict，两种都要能取。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


_RE_BOT_MSG = re.compile(r"<msg\b[^>]*>([\s\S]*?)</msg>", re.S)
_RE_BOT_TEXT = re.compile(r"<text>([\s\S]*?)</text>", re.S)
_RE_USER_LINE = re.compile(
    r"^\s*\[(?P<time>[^\]]{1,24})\]\s*"
    r"(?:\[message_id:[^\]]*\]\s*)?"
    r"\[user_nickname:\s*(?P<nick>[^,\]\n]+)(?:,[^\]]*)?\]\s*\|?\s*"
    r"(?P<body>[\s\S]*)$"
)


def _clean_bot(text) -> list:
    """我发出去的气泡在窗口里是一串 <msg ...><text>…</text></msg>，决策时只留说的话。

    一次回了好几条就会有好几行；每条前面带上时间，跟群友那几行一个样子。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    blocks = _RE_BOT_MSG.findall(raw)
    if not blocks:
        return [raw]
    rows = []
    for block in blocks:
        m = _RE_BOT_TEXT.search(block)
        body = (m.group(1) if m else block).strip()
        if not body:
            continue
        t = re.search(r"<time>([^<]*)</time>", block)
        rows.append(("[%s] " % t.group(1).strip() if t else "") + body)
    return rows or [raw]


def _clean_user(text) -> str:
    """群友那行原本是 [时间] [message_id: …] [user_nickname: 谁, user_id: …] | 内容。

    决策模型只需要「时间 · 谁 · 说了什么」（用户 m05902），id 那堆只会白吃 token。
    """
    raw = str(text or "").strip()
    m = _RE_USER_LINE.match(raw)
    if not m:
        return raw
    nick = m.group("nick").strip()
    body = m.group("body").strip()
    # 插件会在 meta 和「|」之间插一个唤醒标记 [@你]。这里不把它当正文，改成
    # 「（叫你）」挂在名字后面——决策模型要一眼看出这条是在叫它（用户 m12833）。
    you = False
    if body.startswith("[@你]"):
        you = True
        body = body[len("[@你]"):].lstrip()
    if body.startswith("|"):
        body = body[1:].lstrip()
    if not body:
        return raw if not you else "[%s] %s（叫你）" % (m.group("time").strip(), nick)
    return "[%s] %s%s：%s" % (
        m.group("time").strip(), nick, "（叫你）" if you else "", body)


# 2026-11-08（J）：决策注记不是它自己说的话。摊聊天记录时以前一律挂它的名字，
# 看起来跟它真说过的一句一模一样；注记只留「[HH:MM] [对当前聊天消息的决策建议] …」这个长相。
_NOTE_NAMES = ("对当前聊天消息的决策建议", "对以上聊天记录的决策建议")
_NOTE_DECL = "[对当前聊天消息的决策建议] "
_NOTE_TIME_RE = re.compile(r"^\s*\[\s*(\d{1,2}:\d{2})\s*\]\s*")
_NOTE_DECL_RE = re.compile(r"^\s*\[(?:对当前聊天消息的决策建议|对以上聊天记录的决策建议)\]\s*")


def _is_note_msg(msg) -> bool:
    """这条窗口消息是不是我们自己塞进上下文的决策注记。"""
    try:
        if str(_field(msg, "role") or "").strip().lower() != "assistant":
            return False
        if bool(_field(msg, "dec")):
            return True
        if str(_field(msg, "name") or "").strip() in _NOTE_NAMES:
            return True
        text = _text_of(_field(msg, "content")).lstrip()
    except Exception:
        return False
    if text.startswith("[决策记录]"):
        return True
    rest = _NOTE_TIME_RE.sub("", text)
    if _NOTE_DECL_RE.match(rest):
        return True
    return rest.startswith(("建议回复", "建议不回", "判定要回", "判定不回"))


def _note_line(line: str) -> str:
    """注记行去掉名字，补上「[对当前聊天消息的决策建议]」这个声明。"""
    s = str(line or "").strip()
    if s.startswith("[决策记录]"):
        # 老记录自带这个抬头，换成新的声明，不再叠一层。
        s = s[len("[决策记录]"):].strip()
    m = _NOTE_TIME_RE.match(s)
    if m:
        rest = s[m.end():]
        if _NOTE_DECL_RE.match(rest):
            return s
        return "[%s] %s%s" % (m.group(1), _NOTE_DECL, rest)
    if _NOTE_DECL_RE.match(s):
        return s
    return _NOTE_DECL + s


_RE_MID = re.compile(r"\[message_id:\s*(-?\d+)\]|<msg\b[^>]*\bmessage_id=\"?(-?\d+)\"?", re.I)


def _mid_of_text(text) -> str:
    """这条消息在原文里带的 message_id（图靠它对号入座）。"""
    m = _RE_MID.search(str(text or ""))
    if not m:
        return ""
    return str(m.group(1) or m.group(2) or "").strip()


def _text_of(value) -> str:
    """把消息 content 摊成纯文本（多模态只取 text 段）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        out = []
        for part in value:
            if isinstance(part, dict):
                if str(part.get("type") or "") == "text":
                    out.append(str(part.get("text") or ""))
                elif "text" in part and not part.get("type"):
                    out.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(x for x in out if x)
    return str(value)


def _tool_calls_of(msg) -> list:
    """把一条消息上的 tool_calls 规整成能直接发给上游的纯 dict 列表（v4.92，用户 m13535）。

    框架记忆里是对象、测试里是 dict，字段还可能只给了一半；上游只认
    {"id", "type": "function", "function": {"name", "arguments"}}，这里补齐，
    取不到 id 或函数名的调用直接跳过（发出去只会让整轮 422）。
    """
    out = []
    for c in (_field(msg, "tool_calls") or []):
        cid = str(_field(c, "id") or "").strip()
        fn = _field(c, "function") or {}
        name = str(_field(fn, "name") or "").strip()
        args = _field(fn, "arguments")
        if not cid or not name:
            continue
        if not isinstance(args, str):
            try:
                args = json.dumps(args if args is not None else {}, ensure_ascii=False)
            except Exception:
                args = "{}"
        out.append({"id": cid, "type": "function",
                    "function": {"name": name, "arguments": args or "{}"}})
    return out


def _strip_fence(text: str) -> str:
    """去掉 markdown 代码围栏与前后废话，只留下 JSON 那一坨。"""
    s = str(text or "").strip()
    if not s:
        return ""
    fence = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", s)
    if fence:
        s = fence.group(1).strip()
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    return s.strip()


def parse_decision(text: str):
    """把模型输出解析成 {"reply": bool, "reason": str, "say": str}。

    解析不出来返回 None——调用方据此当成「失败」，交给重试 / fallback 决定，
    而不是瞎猜一个结论。
    """
    body = _strip_fence(text)
    if not body:
        return None
    data = None
    try:
        data = json.loads(body)
    except Exception:
        # 模型可能写成松格式，退一步只抓 reply 字段。
        m = re.search(r'"?reply"?\s*[:=]\s*"?([a-zA-Z\u4e00-\u9fff]+)"?', body)
        if m:
            data = {"reply": m.group(1)}
        else:
            return None
    if not isinstance(data, dict):
        return None
    raw = data.get("reply", data.get("answer", data.get("结果")))
    if isinstance(raw, bool):
        reply = raw
    else:
        token = str(raw if raw is not None else "").strip().lower()
        if token in _YES:
            reply = True
        elif token in _NO:
            reply = False
        else:
            return None
    reason = str(data.get("reason") or data.get("理由") or "").strip()
    say = str(data.get("say") or data.get("说") or "").strip()
    return {"reply": bool(reply), "reason": reason[:120], "say": say[:200]}


#: 单张图上限 4MB（v4.39，用户 m20816）：再大 base64 之后会把请求撑爆，干脆不带。
_IMG_MAX_BYTES = 4 * 1024 * 1024

#: 上游临时性故障（v4.67，用户 m02347）：5xx / 429 / 超时 / 连接断开这一类，跟「这一轮带了图」
#: 没有关系——同一个请求换成纯文字也一样会挂。这类失败只按重试设置再试，不再丢图重判。
_TRANSIENT_RE = re.compile(
    r"(?:\b(?:429|500|502|503|504)\b)|too busy|overloaded|service_unavailable|"
    r"service unavailable|timed? ?out|timeout|connection (?:reset|aborted|error)|"
    r"reset by peer|temporarily|temporarily unavailable|bad gateway|gateway time-?out",
    re.I)


def _is_transient_error(text) -> bool:
    """上游临时性故障（5xx / 429 / 超时 / 断连）——重试能救，跟图没关系。"""
    s = str(text or "")
    if not s:
        return False
    return _TRANSIENT_RE.search(s) is not None


def _sniff_image_mime(raw: bytes, rel: str = "") -> str:
    """按文件头认图片类型，认不出再退回后缀（v4.39，用户 m20816）。"""
    head = raw[:16]
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    low = str(rel or "").lower()
    for ext, mime in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"),
                      (".gif", "image/gif"), (".webp", "image/webp"), (".bmp", "image/bmp")):
        if low.endswith(ext):
            return mime
    return ""


def _image_data_url(path: str) -> str:
    """把一张图编成 data URL，直接塞进决策的 user 消息（v4.39，用户 m20816）。

    没走框架的 kira_image_ref：决策这条链是插件自己拼 LLMRequest，框架内部怎么解析
    那个引用这边看不到也验不了；data URL 是最通用的形态，上游不认的话 decide() 会
    退回纯文字再判一次。
    """
    rel = str(path or "").strip().replace("\\", "/")
    if not rel:
        return ""
    if rel.startswith("data:"):
        return rel
    if rel.startswith(("http://", "https://")):
        return rel
    try:
        from core.utils.path_utils import get_data_path  # type: ignore

        target = Path(get_data_path()) / rel
        if not target.is_file():
            return ""
        raw = target.read_bytes()
    except Exception:
        return ""
    if not raw or len(raw) > _IMG_MAX_BYTES:
        return ""
    mime = _sniff_image_mime(raw, rel)
    if not mime:
        return ""
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


@dataclass
class DecisionOptions:
    """一次决策要用的全部配置（从插件配置里读出来再传进来，便于单测）。"""

    chat_rules: str = DEFAULT_CHAT_RULES
    prompt: str = DEFAULT_DECISION_PROMPT
    tools: list = field(default_factory=list)
    model: str = ""
    fallback: str = ""
    retry: int = 2
    retry_delay: float = 3.0
    fail_switch: int = 2
    cooldown: float = 300.0
    timeout: float = 60.0
    fail_action: str = "allow"
    tool_rounds: int = 2
    # 用户 m13442 第 4 条：框架「代理设置」那四项改由插件接管，决策链与回复链各一套。
    # 决策链这三个只作用于插件自己的 _tool_loop（框架那套 agent 不跑决策）。
    tool_calls_per_turn: int = 5
    tool_timeout: float = 180.0
    tool_output_tokens: int = 10000


def options_from_config(get) -> DecisionOptions:
    """从 get(key, default) 读配置。get 由调用方给（能带 sid 覆盖）。"""
    def _int(key, default, lo=0, hi=None):
        try:
            raw = get(key, default)
            # 数字零是有效值；False 仍沿用原来的默认回退。
            v = int(float(default if raw is None or raw is False or raw == "" else raw))
        except Exception:
            v = default
        v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    # v4.90（发版前审计）：schema 里 retry_delay 是 float、设置页也是
    # step='any'，但一直走 _int 被截成整数（填 3.5 存进去变 3）。小数项单独走这个。
    def _num(key, default, lo=0, hi=None):
        try:
            raw = get(key, default)
            v = float(default if raw is None or raw is False or raw == "" else raw)
        except Exception:
            v = float(default)
        # NaN / Inf 跟 _int 一样回退默认值，不能靠 max/min 夹成 0 或上限。
        if not math.isfinite(v):
            v = float(default)
        v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    tools = get("decision_tools", [])
    if isinstance(tools, str):
        try:
            parsed = json.loads(tools) if tools.strip() else []
        except Exception:
            parsed = [x.strip() for x in tools.replace("，", ",").split(",") if x.strip()]
        tools = parsed if isinstance(parsed, list) else []
    if not isinstance(tools, (list, tuple)):
        tools = []

    return DecisionOptions(
        chat_rules=str(get("decision_chat_rules", DEFAULT_CHAT_RULES) or ""),
        prompt=migrate_prompt(get("decision_prompt", DEFAULT_DECISION_PROMPT) or ""),
        tools=[str(x).strip() for x in tools if str(x).strip()],
        model=str(get("decision_model", "") or "").strip(),
        fallback=str(get("decision_fallback_model", "") or "").strip(),
        # 用户 m07859：重试 / 重试间隔 / 单次超时不再各留一份，统一读基础设置页那三项。
        retry=_int("retry_max", 2, 0, 10),
        retry_delay=_num("retry_delay", 5, 0, 600),
        fail_switch=_int("decision_fail_switch", 2, 1, 10),
        cooldown=float(_int("decision_fallback_cooldown", 300, 30, 3600)),
        timeout=float(_int("watchdog_seconds", 180, 30, 600)),
        fail_action=str(get("decision_fail_action", "allow") or "allow").strip().lower(),
        # 用户 m08678：决策页保留自己的一套次数 / 时间设置，默认值与回复一致。
        tool_rounds=_int("decision_tool_loop", 5, 1, 10),
        tool_calls_per_turn=_int("decision_tool_calls_per_turn", 5, 1, 20),
        # 0 = 不限制，跟框架「工具调用超时时间」同一口径。
        tool_timeout=_num("decision_tool_timeout", 180, 0, 600),
        # 0 = 不限制，跟框架「工具输出上限」同一口径（估算 token，4 字节 ≈ 1 token）。
        tool_output_tokens=_int("decision_tool_output_tokens", 10000, 0, 200000),
    )


def _clip_tool_text(text, max_tokens: int) -> str:
    """按「估算 token」截工具输出，口径与框架 core.agent.tool.truncate_tool_output 一致。

    用户 m13442 第 4 条：框架「工具输出上限」改由插件接管，决策链的工具结果也照这个
    口径截——0 表示不限制；超限保留首尾各一半，中间插一行说明（跟框架同一句英文，
    模型两边看到的是同一种标记）。
    """
    body = str(text or "")
    try:
        budget = int(max_tokens)
    except (TypeError, ValueError):
        budget = 10000
    if budget <= 0:
        return body
    max_bytes = budget * 4
    if len(body) <= budget:
        return body
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return body
    marker = ("\n[Tool output truncated; original size: %d UTF-8 bytes. "
              "Narrow the query or request a smaller range.]\n" % len(encoded))
    if len(marker.encode("utf-8")) > max_bytes:
        marker = "[truncated]" if max_bytes >= 11 else "[..]"
    remaining = max_bytes - len(marker.encode("utf-8"))
    head_bytes = (remaining + 1) // 2
    tail_bytes = remaining // 2
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + marker + tail


class DecisionPhase:
    """装配提示词、调模型、跑工具、解析结论。不碰窗口，也不碰框架的 agent。"""

    def __init__(self, plugin=None):
        self.plugin = plugin
        # 工具目录：从最近一次 llm_request 的 tool_set 里抄下来的 name / description。
        # 框架没有「列出全部工具」的公开方法（03-agent-guide.md 只写了 register/unregister），
        # 所以走这条旁路；真机跑过一轮之后设置页就有清单可勾。
        self._catalog: dict = {}
        # 身份环境（accounts / sessions / chat_env / time 四段原文）最近一次的样子。
        self._identity = ""
        self._identity_at = 0.0
        # 失败计数与 fallback 冷却
        self._fail_streak = 0
        self._fallback_until = 0.0

    # ---------- 工具目录 ----------

    def note_tool_set(self, tool_set) -> None:
        """记下这一轮框架实际带着的工具（name + description）。"""
        try:
            tools = list(getattr(tool_set, "tools", []) or [])
        except Exception:
            return
        for item in tools:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                continue
            desc = str(getattr(item, "description", "") or "").strip()
            self._catalog[name] = {"name": name, "description": desc[:200], "tool": item}

    def _probe_ctx_tools(self) -> int:
        """还没跑过任何一轮请求时，直接问框架的工具管理器要清单。

        用户 m05856 要的是「工具表可以选择暴露什么 tool 给决策流程」——热重载完
        还没收到消息的话，note_tool_set 里一份都没有，页面就是张空表。
        真机探到的 FuncToolManager 只有这几个口子：tool_set / build_tool_set /
        register_tool / unregister_tool / execute_tool。
        """
        ctx = getattr(self.plugin, "ctx", None)
        mgr = getattr(ctx, "tool_mgr", None)
        boxes = []
        if mgr is not None:
            ts = getattr(mgr, "tool_set", None)
            if ts is not None:
                boxes.append(list(getattr(ts, "tools", []) or []))
            build = getattr(mgr, "build_tool_set", None)
            if callable(build):
                try:
                    made = build()
                    boxes.append(list(getattr(made, "tools", []) or []))
                except Exception:
                    logger.debug("[multi-decision] build_tool_set() 调用失败", exc_info=True)
        got = 0
        for items in boxes:
            for item in items:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    desc = str(item.get("description") or "").strip()
                else:
                    name = str(getattr(item, "name", "") or "").strip()
                    desc = str(getattr(item, "description", "") or "").strip()
                if not name or name in self._catalog:
                    continue
                self._catalog[name] = {
                    "name": name,
                    "description": desc[:200],
                    "tool": item if hasattr(item, "execute") else None,
                }
                got += 1
        self._probe_at = time.time()
        return got

    def tool_catalog(self) -> list:
        # 目录空 = 还没见过真实请求，试着从框架那边捞一次（60 秒最多捞一次）。
        if not self._catalog or (time.time() - float(getattr(self, "_probe_at", 0.0) or 0.0)) > 60.0:
            try:
                self._probe_ctx_tools()
            except Exception:
                logger.debug("[multi-decision] 内省框架工具表失败", exc_info=True)
        return [
            {"name": v["name"], "description": v["description"]}
            for v in sorted(self._catalog.values(), key=lambda x: x["name"])
        ]

    def pick_tools(self, names) -> list:
        """按名字挑出 BaseTool 实例，目录里没有的跳过。"""
        want = [str(x).strip() for x in (names or []) if str(x).strip()]
        if not want:
            return []
        if not self._catalog:
            try:
                self._probe_ctx_tools()
            except Exception:
                logger.debug("[multi-decision] 内省框架工具表失败", exc_info=True)
        out = []
        for name in want:
            item = self._catalog.get(name)
            if item and item.get("tool") is not None:
                out.append(item["tool"])
        return out

    # ---------- 身份环境 ----------

    def note_system_prompt(self, prompts) -> None:
        """从框架这一轮的 system_prompt 里抄 accounts / sessions / chat_env / time。

        用户 m05856：「最下面还需要注入身份环境 和系统提示词那边一样的」。直接复用
        框架自己拼好的原文，比我们自己另写一份更准（称呼、群名、时间格式都对得上）。
        """
        try:
            items = list(prompts or [])
        except Exception:
            return
        keep = []
        for p in items:
            name = str(getattr(p, "name", "") or "").strip().lower()
            if name in ("accounts", "sessions", "chat_env", "time"):
                content = str(getattr(p, "content", "") or "").strip()
                if content:
                    keep.append(content)
        if keep:
            self._identity = "\n".join(keep)
            self._identity_at = time.time()

    def identity_text(self) -> str:
        return self._identity

    # ---------- 人格 ----------

    async def _persona_items(self) -> list:
        """把框架里的人设文件全列一遍（框架三套列法都可能存在，谁有内容用谁）。"""
        mgr = getattr(getattr(self.plugin, "ctx", None), "persona_mgr", None)
        if mgr is None:
            return []
        for fn in ("list_persona", "list_personas", "get_all_persona"):
            f = getattr(mgr, fn, None)
            if not callable(f):
                continue
            try:
                got = f()
                if asyncio.iscoroutine(got):
                    got = await got
                items = list(got or [])
            except Exception:
                logger.debug("[multi-decision] 列人格失败", exc_info=True)
                items = []
            if items:
                return items
        return []

    @staticmethod
    def _persona_pick(obj, keys):
        """dict / 对象两种形态都能取字段。"""
        for key in keys:
            if obj is None:
                return None
            if isinstance(obj, dict):
                val = obj.get(key)
            else:
                val = getattr(obj, key, None)
            if val:
                return val
        return None

    def _persona_body(self, obj) -> str:
        """从一条人设记录里取正文（纯人设，不含群聊 / 私聊规则）。"""
        val = self._persona_pick(obj, ("persona", "content", "prompt", "text", "description"))
        return val.strip() if isinstance(val, str) else ""

    def _remember_persona(self, name: str, body: str) -> None:
        if not isinstance(getattr(self, "_persona_cache", None), dict):
            self._persona_cache = {}
        if name and body:
            self._persona_cache[str(name)] = body

    async def persona_options(self) -> list:
        """人格列表（给设置页下拉用）。只取名字与正文开头，不碰群聊/私聊规则。"""
        out = []
        for it in await self._persona_items():
            name = str(self._persona_pick(it, ("name", "persona_name", "title", "id")) or "").strip()
            if not name:
                continue
            pid = str(self._persona_pick(it, ("id",)) or "").strip()
            body = self._persona_body(it)
            out.append({"name": name, "id": pid, "preview": body[:60]})
        return out

    def persona_text_now(self, name: str) -> str:
        """同步取人格正文。真实回复在事件循环里，不能 await，只能走这条。

        优先用异步路径缓存下来的正文；没有再直接问框架（框架这几个取人格的方法
        本身是同步的，只有外面包了一层协程时才拿不到）。
        """
        want = str(name or "").strip()
        if not want:
            return ""
        hit = (getattr(self, "_persona_cache", None) or {}).get(want)
        if hit:
            return hit
        mgr = getattr(getattr(self.plugin, "ctx", None), "persona_mgr", None)
        if mgr is None:
            return ""
        for fn in ("get_persona", "get_persona_by_name", "find_persona"):
            f = getattr(mgr, fn, None)
            if not callable(f):
                continue
            try:
                got = f(want)
            except Exception:
                continue
            if asyncio.iscoroutine(got):
                got.close()
                continue
            body = self._persona_body(got)
            if body:
                self._remember_persona(want, body)
                return body
        # 用户 m04617 图6：框架按 id 查人设，下拉里给的是名字，名字查不到就整段丢了。
        # 这里退回「自己列一遍人设文件，名字 / id 对上就取正文」。
        for fn in ("list_persona", "list_personas", "get_all_persona"):
            f = getattr(mgr, fn, None)
            if not callable(f):
                continue
            try:
                got = f()
            except Exception:
                continue
            if asyncio.iscoroutine(got):
                got.close()
                continue
            for it in list(got or []):
                keys = [str(self._persona_pick(it, ("name", "persona_name", "title")) or "").strip(),
                        str(self._persona_pick(it, ("id",)) or "").strip()]
                if want in [k for k in keys if k]:
                    body = self._persona_body(it)
                    if body:
                        self._remember_persona(want, body)
                        return body
        return ""

    async def persona_text(self, name: str) -> str:
        """取某个人格的纯人设正文。名字空 = 不用人格。"""
        want = str(name or "").strip()
        if not want:
            return ""
        mgr = getattr(getattr(self.plugin, "ctx", None), "persona_mgr", None)
        if mgr is None:
            return ""
        info = None
        for fn in ("get_persona", "get_persona_by_name", "find_persona"):
            f = getattr(mgr, fn, None)
            if not callable(f):
                continue
            try:
                got = f(want)
                if asyncio.iscoroutine(got):
                    got = await got
            except Exception:
                continue
            if got is not None:
                info = got
                break
        body = self._persona_body(info)
        if body:
            self._remember_persona(want, body)
            return body
        # 用户 m04617 图6：同上——按 id 查不到名字时，自己列一遍人设文件再对名字。
        for it in await self._persona_items():
            keys = [str(self._persona_pick(it, ("name", "persona_name", "title")) or "").strip(),
                    str(self._persona_pick(it, ("id",)) or "").strip()]
            if want in [k for k in keys if k]:
                body = self._persona_body(it)
                if body:
                    self._remember_persona(want, body)
                    return body
        return ""

    # ---------- 装配 ----------

    def _bot_name(self, sid: str = "") -> str:
        """2026-11-08（J）：提示词里的{{bot}}换成这个会话里它自己的昵称，没设就退全局名。"""
        return bot_name(self.plugin, sid)

    def build_system(self, opt: DecisionOptions, persona: str, tools_help: str = "",
                     identity: str = "", sid: str = "") -> list:
        """按固定顺序拼 system 段，返回非空段列表（顺序即发送顺序）。"""
        parts = []
        p = (persona or "").strip()
        if p:
            # v4.84（用户 m09179 第 4 条）：不再加「【人设】」抬头，人设文件自带标题。
            parts.append(p)
        # v3.92（用户 m06951 第 6 条）：「聊天规则」不再进决策这一份——它只在真正开口那一轮
        # 起作用，改由 state/reply_prompt.py 插到回复系统提示词的人格段后面。
        # 决策这一份只剩「人设 → 判断规则 → 工具 → 身份环境」。
        prompt = ((opt.prompt or "").strip() or DEFAULT_DECISION_PROMPT)
        prompt = prompt.replace("{{bot}}", self._bot_name(sid))
        parts.append("【判断规则】\n" + prompt)
        if tools_help:
            parts.append("【可用工具】\n" + tools_help)
        env = (identity or "").strip()
        if env:
            parts.append("【身份与环境】\n" + env)
        return parts

    def tools_help(self, tools) -> str:
        rows = []
        for t in tools:
            name = str(getattr(t, "name", "") or "").strip()
            if not name:
                continue
            desc = str(getattr(t, "description", "") or "").strip().splitlines()
            rows.append("- " + name + ("：" + desc[0].strip() if desc else ""))
        return "\n".join(rows)

    def transcript(self, messages, sid: str = "") -> str:
        """把窗口消息摊成聊天记录（内容原样，只去掉空行）。

        2026-11-08（J）：bot 自己那几行以前统一写【我】，看不出是谁说的；改成它在这个会话里
        的名字，跟群友那几行一样能认出说话的人。决策注记不是它说的话，不挂名字（同日）。
        """
        tag = "【%s】" % self._bot_name(sid)
        rows = []
        for m in messages or []:
            role = str(_field(m, "role") or "").strip().lower()
            body = _text_of(_field(m, "content")).strip()
            if not body:
                continue
            if role == "system":
                continue
            if role == "assistant":
                note = _is_note_msg(m)
                for line in _clean_bot(body):
                    rows.append(_note_line(line) if note else (tag + line))
            else:
                rows.append(_clean_user(body))
        return "\n".join(rows)

    # ---------- 模型链 ----------

    def _client_for(self, model: str):
        """取客户端。空 = 框架默认快速模型（用户 m05856：默认选快速模型）。"""
        ctx = getattr(self.plugin, "ctx", None)
        if ctx is None:
            return None
        key = str(model or "").strip()
        try:
            if key:
                return ctx.get_llm_client(key)
            getter = getattr(ctx, "get_default_fast_llm_client", None)
            if callable(getter):
                return getter()
            return ctx.get_default_llm_client()
        except Exception as exc:
            logger.warning("[multi-decision] 取决策模型失败（%s）：%s", key or "fast", exc)
            return None

    async def _one_call(self, client, messages, tools, timeout: float) -> dict:
        """发一次请求，保留这个真实响应的 token、模型身份与请求开始时刻。"""
        if LLMRequest is None:
            return {"ok": False, "error": "框架没有 core.provider.LLMRequest"}
        req = LLMRequest(messages=messages)
        if tools:
            try:
                from core.utils.tool_utils import ToolSet  # type: ignore
                ts = ToolSet()
                for t in tools:
                    ts.add(t)
                req.tool_set = ts
                req.tool_choice = "auto"
            except Exception:
                logger.debug("[multi-decision] 决策工具挂载失败", exc_info=True)
        identity = {}
        try:
            actual_model = getattr(client, "model", None)
            for key in ("provider_id", "model_id", "model_name", "provider_name"):
                value = str(_field(actual_model, key, "") or "").strip()
                if value:
                    identity[key] = value
        except Exception:
            logger.debug("[multi-decision] 读取决策模型身份失败（保留请求引用）", exc_info=True)
        model_ref = ("%s:%s" % (identity["provider_id"], identity["model_id"])
                     if identity.get("provider_id") and identity.get("model_id") else "")
        try:
            request_started = time.time()
            resp = client.chat(req)
            if asyncio.iscoroutine(resp):
                resp = await asyncio.wait_for(resp, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "决策模型超时（%d 秒）" % int(timeout)}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        text = str(getattr(resp, "text_response", "") or "")
        calls = list(getattr(resp, "tool_calls", []) or [])
        token_in = max(0, int(getattr(resp, "input_tokens", 0) or 0))
        token_out = max(0, int(getattr(resp, "output_tokens", 0) or 0))
        token_cached = min(token_in, max(0, int(getattr(resp, "cached_tokens", 0) or 0)))
        # 仅有响应中的 token 字段才是用量证据；超时/网络异常不能虚构一次用量。
        evidenced = any(getattr(resp, key, None) is not None
                        for key in ("input_tokens", "output_tokens", "cached_tokens"))
        usage = [{"ts": request_started, "timestamp_source": "request_start",
                  "model": model_ref, **identity,
                  "input": token_in, "output": token_out, "cached": token_cached,
                  # v5.x（用户 m12891）：上游这一响应有没有真的回报缓存命中。
                  # 没回报时 cached 只能是 0，不能当成「实测 0%」拿去跟预估比。
                  "cached_reported": getattr(resp, "cached_tokens", None) is not None,
                  }] if evidenced else []
        return {
            "ok": True,
            "text": text,
            "tool_calls": calls,
            "model": model_ref,
            "in": token_in,
            "out": token_out,
            "cached": token_cached,
            "cache_reported": getattr(resp, "cached_tokens", None) is not None,
            "usage": usage,
        }

    async def _run_tool(self, tool, args: dict, timeout: float = 0):
        """跑一个工具，返回 (正文, 附件 refs)。

        v4.69（用户 m00969）：工具可以返回 ToolResult。以前一律 str(got)，带图的
        工具结果会变成一段 repr 文字——模型既看不到图，正文也读不懂。现在正文从
        assemble_result() 取，media_refs 交给 _tool_loop 拼成一条 user 消息。

        用户 m13442 第 4 条：timeout > 0 时按「工具调用超时时间」掐断，超时算一次
        失败（跟框架 execute_tool 一样回一条 error 正文，不抛出去）。
        """
        try:
            got = tool.execute(**args)
            if asyncio.iscoroutine(got):
                got = await (asyncio.wait_for(got, timeout) if timeout else got)
        except asyncio.TimeoutError:
            return "工具执行超时（%d 秒）：%s" % (int(timeout), getattr(tool, "name", "")), []
        except Exception as exc:
            return "工具执行失败：%s: %s" % (type(exc).__name__, exc), []
        if ToolResult is not None and isinstance(got, ToolResult):
            media = [dict(r) for r in (getattr(got, "media_refs", None) or [])
                     if isinstance(r, dict) and r.get("path")]
            try:
                text = await got.assemble_result()
            except Exception:
                text = str(getattr(got, "text", "") or "")
            return str(text or ""), media
        if isinstance(got, str):
            return got, []
        try:
            return json.dumps(got, ensure_ascii=False), []
        except Exception:
            return str(got), []

    async def _tool_loop(self, client, messages, tools, opt: DecisionOptions, budget: dict,
                         tool_cache: dict = None, reuse: bool = False) -> dict:
        """多轮工具循环。返回最后一次模型结果（带上累计 token）。

        H07：reuse=True 表示这是同一次决策的重试/降级尝试，同一件工具的同一个参数
        直接吃上一次的成功结果，不再执行——副作用工具不该因为重试被跑第二遍。
        """
        names = {str(getattr(t, "name", "") or ""): t for t in tools}
        # 用户 m13442 第 4 条：单轮次数上限 / 超时 / 输出上限都从配置现读，改完立即生效。
        cap = max(1, int(getattr(opt, "tool_calls_per_turn", 5) or 5))
        timeout = float(getattr(opt, "tool_timeout", 60) or 0)
        out_tokens = int(getattr(opt, "tool_output_tokens", 10000) or 0)
        last = await self._one_call(client, messages, tools, opt.timeout)
        budget.setdefault("usage", []).extend(last.get("usage") or [])
        rounds = 0
        while last.get("ok") and last.get("tool_calls") and rounds < max(0, opt.tool_rounds):
            rounds += 1
            calls = []
            media_all = []
            media_names = []
            for idx, raw in enumerate(last["tool_calls"]):
                if isinstance(raw, dict):
                    fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
                    name = str(raw.get("name") or fn.get("name") or "").strip()
                    args = raw.get("arguments", fn.get("arguments", {}))
                    cid = str(raw.get("id") or raw.get("tool_call_id") or ("call_%d" % rounds))
                else:
                    name, args, cid = "", {}, ""
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                calls.append({"id": cid, "type": "function",
                              "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}})
                if idx >= cap:
                    # 超出的照框架的做法回一条说明：assistant 的 tool_calls 与 tool 回执
                    # 必须一一对上，直接丢掉会让下一轮请求被 provider 判成残缺。
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "content": _clip_tool_text(
                                         "本轮工具调用次数已达上限（%d 个），这个工具没有执行。" % cap,
                                         out_tokens)})
                    continue
                tool = names.get(name)
                key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
                result = None
                media = []
                if reuse and tool_cache is not None and tool is not None and key in tool_cache:
                    cached = tool_cache[key]
                    if isinstance(cached, tuple) and len(cached) == 2:
                        result, media = cached[0], list(cached[1] or [])
                    else:
                        result = cached
                if result is None:
                    if tool:
                        result, media = await self._run_tool(tool, args, timeout)
                    else:
                        result, media = "没有这个工具：" + name, []
                    # 只复用「真跑成功了」的；工具报错允许重试时再试一次。
                    if (tool is not None and tool_cache is not None
                            and not result.startswith("工具执行失败：")):
                        tool_cache[key] = (result, list(media))
                if media:
                    media_all.extend(media)
                    if name not in media_names:
                        media_names.append(name)
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": _clip_tool_text(result, out_tokens)})
            messages.insert(len(messages) - len(calls),
                            {"role": "assistant", "content": last.get("text") or "",
                             "tool_calls": calls})
            # 带图的工具结果按框架 agent_executor 的形状补一条 user 消息：
            # 部分 provider 拒绝 tool 角色带图，图片一律走 user（用户 m00969）。
            if media_all:
                parts = [{"type": "kira_image_ref", "path": str(m.get("path") or ""),
                          "mime_type": str(m.get("mime_type") or m.get("mime")
                                          or "image/jpeg")}
                         for m in media_all if isinstance(m, dict) and m.get("path")]
                if parts:
                    messages.append({"role": "user", "content": [
                        {"type": "text",
                         "text": "Media returned by tool call(s): "
                                 + ", ".join(media_names)},
                        *parts]})
            budget["in"] += last.get("in", 0)
            budget["out"] += last.get("out", 0)
            budget["cached"] += last.get("cached", 0)
            last = await self._one_call(client, messages, tools, opt.timeout)
            budget["usage"].extend(last.get("usage") or [])
        return last

    async def _chain(self, messages, tools, opt: DecisionOptions, tool_cache: dict = None) -> dict:
        """主模型 → 重试 → fallback。返回最后一次尝试的结果并附上用量。

        tool_cache 由 decide() 持有，跨「带图→纯文字降级」也共用，这样工具不会被
        第二个链重新执行一遍。
        """
        if tool_cache is None:
            tool_cache = {}
        budget = {"in": 0, "out": 0, "cached": 0, "usage": []}
        now = time.time()
        tripped = self._fail_streak >= max(1, opt.fail_switch)
        cooling = self._fallback_until > 0 and now < self._fallback_until
        # M17：冷却到期后放一次主模型探测。探测期间先把冷却位占住，失败不会变成探测风暴，
        # 成功则清掉冷却；只有备用一直失败、fail_streak 没被清零时才需要这条出口。
        probe = bool(opt.fallback) and tripped and not cooling and self._fallback_until > 0
        if probe:
            self._fallback_until = now + opt.cooldown
        use_fallback = bool(opt.fallback) and (cooling or (tripped and not probe))
        order = []
        if use_fallback:
            order.append(("fallback", opt.fallback))
        else:
            order.append(("main", opt.model))
        if opt.fallback and not use_fallback:
            order.append(("fallback", opt.fallback))

        attempts = 0
        last = {"ok": False, "error": "没有可用的决策模型"}
        for idx, (kind, model) in enumerate(order):
            client = self._client_for(model)
            if client is None:
                last = {"ok": False, "error": "模型不可用：" + (model or "快速模型")}
                continue
            tries = (max(0, opt.retry) + 1) if kind == "main" else 1
            note = ""
            for t in range(tries):
                attempts += 1
                msg_copy = [dict(x) for x in messages]
                # 用户 m36396：上一次是「没按 JSON 回」才失败的，重试时补一句提醒；网络/上游报错则原样重试。
                if note:
                    msg_copy.append({"role": "user", "content": note})
                usage_start = len(budget["usage"])
                last = await self._tool_loop(client, msg_copy, tools, opt, budget,
                                             tool_cache, reuse=attempts > 1)
                for item in budget["usage"][usage_start:]:
                    if not item.get("model"):
                        item["model"] = model or "fast"
                last["attempts"] = attempts
                last["model"] = last.get("model") or model or "fast"
                last["via"] = kind
                last["tool_messages"] = msg_copy
                budget["in"] += last.get("in", 0)
                budget["out"] += last.get("out", 0)
                budget["cached"] += last.get("cached", 0)
                if last.get("ok"):
                    parsed = parse_decision(last.get("text") or "")
                    if parsed is not None:
                        self._fail_streak = 0
                        if probe and kind == "main":
                            self._fallback_until = 0.0
                        last["parsed"] = parsed
                        last.update(budget)
                        return last
                    last["error"] = "模型没按 JSON 回（%s）" % (str(last.get("text") or "")[:80])
                    note = DECISION_RETRY_NOTE
                    logger.warning("[multi-decision] 决策第 %d 次没按 JSON 回（%s），重试：%s",
                                   t + 1, model or "快速模型", str(last.get("text") or "")[:160])
                else:
                    note = ""
                    # v4.67（用户 m02347）：上游 5xx / 429 / 超时跟这一轮带没带图无关，照样
                    # 按同一套重试参数（retry_max / retry_delay）再试，只是日志
                    # 里单独标一句，事后能跟「没按 JSON 回」区分开。
                    if t < tries - 1 and _is_transient_error(last.get("error")):
                        logger.warning("[multi-decision] 决策请求上游临时故障（%s），第 %d/%d 次失败，"
                                       "%s 秒后再试",
                                       str(last.get("error") or "")[:140], t + 1, tries,
                                       int(opt.retry_delay))
                if t < tries - 1 and opt.retry_delay > 0:
                    await asyncio.sleep(opt.retry_delay)
            # 主模型整轮（含重试）都没成，才记账；fallback 失败不再加计
            if kind == "main":
                self._fail_streak += 1
                if self._fail_streak >= max(1, opt.fail_switch):
                    self._fallback_until = time.time() + opt.cooldown
        last.update(budget)
        return last

    # ---------- 对外入口 ----------

    async def decide(self, sid: str, messages, opt: DecisionOptions,
                     identity: str = "", images=None) -> dict:
        """跑一次决策。返回结构给 engine / 快照 / 页面共用。"""
        started = time.time()
        # v4.89（用户 m14715 第 1 条）：决策页不再有「设置里那份人格」——人设只能由
        # 提示词布局里的人格模块显式选文件，没选就是空的。
        persona = ""
        tools = self.pick_tools(opt.tools)
        help_text = self.tools_help(tools)
        if tools:
            help_text = (TOOL_HEAD % max(1, opt.tool_rounds)) + "\n" + help_text
        layouts = getattr(self.plugin, "prompt_layouts", None)
        dyn = ""
        dyn_parts = []
        if layouts is not None:
            compiled = await layouts.assemble_decision(sid, opt)
            # 用户 m05691：系统动态段勾上「启用」以后，这些内容要拼在本轮消息的最下面一条
            # （跟回复页的动态段同一个位置）——决策看到的是同一份现场信息，不再只看 system。
            dyn_parts = [{"name": str(p.get("name") or ""), "label": str(p.get("label") or ""),
                          "text": str(p.get("text") or "")}
                         for p in compiled["parts"]
                         if p.get("inject") and str(p.get("text") or "").strip()]
            dyn = "\n\n".join(p["text"] for p in dyn_parts)
            # 用户 m03879：记录侧只算真正进了 system 的那几段。实时窗口 / 动态段是拼在聊天记录
            # 后面（user 末尾）的，以前也被算进 system_parts，记录页就显示成「系统提示词 1020 字」。
            system_parts = [p["text"] for p in compiled["parts"] if not p.get("dynamic")]
            system_text = compiled["system"]
        else:
            system_parts = self.build_system(opt, persona, help_text,
                                             identity or self.identity_text(), sid)
            system_text = "\n\n".join(system_parts)
        body = self.transcript(messages, sid)
        user_text = body or "（这段时间没人说话）"
        if dyn:
            user_text = user_text + "\n\n" + dyn
        # v4.64（用户 m35742）：决策请求改成跟回复请求一个样子——[system] + 一条聊天一条消息，
        # 图挂在自己那条消息里，动态段拼在最后一条 user 后面。
        # 以前的写法是「整段摊成一条 user、图夹在正文前后」：正文每轮都要长几条，一长就把
        # 后面那串图整串顶成未命中（真实记录长期只有 13%）；把图提到最前面也救不了——
        # 窗口一裁，最旧那张图没了，整串前缀立刻全废（实测 16:30/16:34 两轮都只剩 system
        # 的 384 命中）。拆成一条条消息以后，老消息和它自己的图都停在原位，新消息只接在
        # 后面，前缀就是追加式的——跟回复侧同一个道理。
        payload_msgs, img_rows = self.chat_payload(messages, images or [], dyn)
        payload = [{"role": "system", "content": system_text}] + payload_msgs
        if len(payload) < 2:
            payload.append({"role": "user", "content": user_text})
        # 只存逐条摘要，不保存 data URL；动态段与工具续轮不属于基础聊天前缀。
        chat_comparison = _comparison_snapshot(payload[1:], dyn)
        tool_cache: dict = {}
        got = await self._chain(payload, tools, opt, tool_cache)
        usage = list(got.get("usage") or [])
        parsed = got.get("parsed")
        # 带图那次没成（上游不认 image_url / 图太大 / 模型没有视觉），退回纯文字再判
        # 一次：宁可这一次看不见图，也不能因为有图把整轮判断判死。
        img_fallback = False
        img_busy = False
        img_err = ""
        img_tries = 0
        if img_rows and not (bool(got.get("ok")) and parsed):
            # v4.65（用户 m36234）：带图那次的真实报错要留下来。以前只留「最后一次
            # 尝试」的 error（纯文字那次），带图为什么没成、试了几次，事后全查不到。
            img_err = str(got.get("error") or "")[:300]
            img_tries = int(got.get("attempts") or 0)
            # v4.67（用户 m02347）：上游 5xx / 429 / 超时是临时过载，跟这一轮带没带图无关——
            # 同一个请求换成纯文字一样会挂，白花一次调用还把「带图失败」记到这一轮头上。
            # 这类失败只按重试设置再试（参数跟「决策失败重试」共用），试完仍失败就按决策
            # 失败处理（fail_action 决定放行还是拦下），图不丢、原图留在记录里。
            img_busy = _is_transient_error(img_err)
            if img_busy:
                logger.warning("[multi-decision] 决策带图请求上游临时故障（%d 张图，试了 %d 次）：%s，"
                               "本轮按决策失败处理，不丢图重判",
                               len(img_rows), img_tries, img_err[:200])
            else:
                logger.warning("[multi-decision] 决策带图失败（%d 张图，试了 %d 次）：%s，退回纯文字重判一次",
                               len(img_rows), img_tries, img_err[:200])
                img_fallback = True
                no_img, _unused = self.chat_payload(messages, None, dyn)
                payload = [{"role": "system", "content": system_text}] + no_img
                if len(payload) < 2:
                    payload.append({"role": "user", "content": user_text})
                chat_comparison = _comparison_snapshot(payload[1:], dyn)
                got = await self._chain(payload, tools, opt, tool_cache)
                usage.extend(got.get("usage") or [])
                parsed = got.get("parsed")
                img_rows = []
        out = {
            "ok": bool(parsed) and bool(got.get("ok")),
            "reply": bool(parsed.get("reply")) if parsed else False,
            "reason": (parsed or {}).get("reason", ""),
            "say": (parsed or {}).get("say", ""),
            "raw": str(got.get("text") or "")[:4000],
            "error": str(got.get("error") or ""),
            "model": str(got.get("model") or ""),
            "via": str(got.get("via") or ""),
            "attempts": int(got.get("attempts") or 0),
            "elapsed": round(time.time() - started, 3),
            "input_tokens": sum(int(item.get("input") or 0) for item in usage),
            "output_tokens": sum(int(item.get("output") or 0) for item in usage),
            "cached_tokens": sum(int(item.get("cached") or 0) for item in usage),
            "usage": usage,
            "tools_used": [str(getattr(t, "name", "")) for t in tools],
            "system_parts": system_parts,
            "transcript": body,
            "chat_comparison": chat_comparison,
            # 用户 m06046 图3：记录页以前只拿 transcript 拼快照，动态段注没注入看不出来。
            # 这里把「真正发给模型的那条 user 消息」和它末尾追加的动态段一并交出去。
            "user_prompt": user_text,
            "dynamic_text": dyn,
            "dynamic_parts": dyn_parts,
            # v4.39（用户 m20816）：这一轮真带了几张图、都是谁在什么时候发的。
            "images": img_rows,
            "image_count": len(img_rows),
            "images_fallback": img_fallback,
            "images_fallback_error": img_err,
            "images_fallback_attempts": img_tries,
            # v4.67（用户 m02347）：这一轮是「上游临时故障」而不是「图有问题」时标出来，
            # 记录页 / 快照里能一眼分清。
            "upstream_busy": img_busy,
            "sid": sid,
        }
        if not out["ok"]:
            # 失败时按配置决定放行还是拦下。放行 = 当成没装进阶模式，走原来的完整流程。
            out["reply"] = (opt.fail_action != "block")
            out["failed"] = True
        return out

    def _image_rows(self, images) -> list:
        """把要发的图编成 data URL，顺手记下体积（v4.39，用户 m20816）。"""
        rows = []
        for it in list(images or []):
            if not isinstance(it, dict):
                continue
            url = _image_data_url(str(it.get("path") or ""))
            if not url:
                continue
            # v4.41（用户 m21541）：体积估算要算上这次真发出去的图。base64 的长度就是
            # wire 上多出来的体积，源图大小按同一套公式反推（跟 payload_metrics 一致）。
            _enc = url.split(",", 1)[1] if (url.startswith("data:") and "," in url) else ""
            _b64 = len(_enc)
            _src = (_b64 // 4 * 3 - _b64 + len(_enc.rstrip("="))) if _b64 else 0
            rows.append({"path": str(it.get("path") or ""), "mime": str(it.get("mime") or ""),
                         "ts": str(it.get("ts") or "").strip(), "who": str(it.get("who") or "").strip(),
                         "mid": str(it.get("mid") or "").strip(),
                         "b64": _b64, "bytes": _src, "url": url})
        return rows

    def image_parts(self, images):
        """把所有图编成一串多模态 parts（老写法，留作兜底）。"""
        rows = self._image_rows(images or [])
        if not rows:
            return [], []
        parts = []
        for i, r in enumerate(rows):
            label = "图 %d" % (i + 1)
            if r["ts"]:
                label += " · [%s]" % r["ts"]
            if r["who"]:
                label += " %s" % r["who"]
            parts.append({"type": "text", "text": label})
            parts.append({"type": "image_url", "image_url": {"url": r["url"]}})
        head = {"type": "text", "text": ("【本轮图片】下面这些图是这段聊天里刚发过来的，"
                                         "看清楚再决定要不要接话：")}
        return [head] + parts, [dict((k, v) for k, v in r.items() if k != "url") for r in rows]

    def chat_payload(self, messages, images=None, dyn: str = "") -> tuple:
        """把窗口消息编成一条条聊天消息（v4.64，用户 m35742）。

        跟回复请求同一个形状：一条聊天一条消息，user / assistant 分开，
        图挂在自己那条消息里；动态段拼在最后一条 user 的正文后面。
        返回 (payload_msgs, image_rows)，image_rows 是不带 base64 的那份，给记录页用。
        """
        rows = self._image_rows(images or [])
        by_mid = {}
        loose = []
        for r in rows:
            if r.get("mid"):
                by_mid.setdefault(str(r["mid"]), []).append(r)
            else:
                loose.append(r)
        msgs = []
        used = []
        image_types = ("kira_image_ref", "image_url", "image", "input_image")
        # v4.92（用户 m13535）：窗口里留着工具往返时，调用和结果必须成对发出去，
        # 少一边上游就 422 整轮作废（2026-11-12 早上 22 轮）。先把两边都齐的调用
        # 挑出来，孤立的调用、孤立的结果都不发。
        calls_all = {}
        for _m in messages or []:
            for _c in _tool_calls_of(_m):
                calls_all[_c["id"]] = _c
        has_result = set()
        for _m in messages or []:
            if str(_field(_m, "role") or "").strip().lower() != "tool":
                continue
            _cid = str(_field(_m, "tool_call_id") or "").strip()
            if _cid:
                has_result.add(_cid)
        paired = set(calls_all) & has_result

        def _push(_role, _content, _calls=None):
            row = {"role": _role or "user", "content": _content}
            if _calls:
                row["tool_calls"] = _calls
            msgs.append(row)

        for m in messages or []:
            role = str(_field(m, "role") or "").strip().lower()
            if role == "system":
                continue
            if role == "tool":
                # v4.92（用户 m13535）：工具结果不再一律丢——窗口里有成对的调用时
                # 原样带上，决策也看得到 bot 上一轮查到了什么；孤立的结果（对应的
                # 调用已被裁掉）上游判非法（422 missing field tool_call_id），不发。
                cid = str(_field(m, "tool_call_id") or "").strip()
                if not cid or cid not in paired:
                    continue
                msgs.append({"role": "tool", "tool_call_id": cid,
                             "content": _text_of(_field(m, "content"))})
                continue
            content = _field(m, "content")
            raw = _text_of(content).strip()
            body = "\n".join(_clean_bot(raw)) if role == "assistant" else _clean_user(raw)
            # 只发配对齐全的调用：有调用没结果的，上游同样会拒（tool_calls 后面
            # 必须跟 tool 消息）。
            mine_calls = ([c for c in _tool_calls_of(m) if c["id"] in paired]
                          if role == "assistant" else [])
            got = by_mid.pop(_mid_of_text(raw), []) if rows else []
            inline = isinstance(content, (list, tuple)) and any(
                isinstance(p, dict) and p.get("type") in image_types for p in content)
            if inline and got:
                # 窗口已准备好 [正文, 图, 图后说明]，严格按原 part 逐项发。
                # images 只是本轮允许带图的白名单，不能用其中的旧原图覆盖派生图。
                parts = []
                selected = []
                head_cleaned = False
                for p in content:
                    if not isinstance(p, dict):
                        if isinstance(p, str) and p:
                            parts.append({"type": "text", "text": p})
                        continue
                    typ = p.get("type")
                    if typ == "text" or (not typ and "text" in p):
                        text = str(p.get("text") or "")
                        if not head_cleaned and text.strip():
                            text = "\n".join(_clean_bot(text)) if role == "assistant" else _clean_user(text)
                            head_cleaned = True
                        if text:
                            parts.append({"type": "text", "text": text})
                    elif typ in image_types:
                        value = p.get("image_url")
                        path = str(p.get("path") or p.get("url") or
                                   (value.get("url") if isinstance(value, dict) else value) or "")
                        row = next((r for r in got if r["path"] == path and id(r) not in selected), None)
                        if row is not None:
                            image = {"url": row["url"]}
                            detail = p.get("detail") or (value.get("detail") if isinstance(value, dict) else None)
                            if detail:
                                image["detail"] = detail
                            parts.append({"type": "image_url", "image_url": image})
                            selected.append(id(row))
                            used.append(row)
                if selected:
                    _push(role, parts, mine_calls)
                elif body:
                    _push(role, body, mine_calls)
            elif got and not inline:
                # 旧记录只有正文时仍按编号兜底；有 prepared refs 的消息不得补回旧图。
                parts = [{"type": "text", "text": body or "[图片]"}]
                for r in got:
                    parts.append({"type": "image_url", "image_url": {"url": r["url"]}})
                    used.append(r)
                _push(role, parts, mine_calls)
            elif body:
                # images=None/[]（包括视觉失败后的纯文字降级）不发送 content 内图片。
                _push(role, body, mine_calls)
            elif mine_calls:
                # 正文空、纯调工具的 assistant 行：配对齐全时也要发，否则下一轮
                # 的 tool 结果没有归属（上游只认紧跟着的那条 assistant）。
                _push(role, "", mine_calls)
        # 对不上号的图（老记录里没带 message_id）不丢：单独一条 user 摆在最后。
        if by_mid:
            _seen = set()
            for r in rows:
                k = str(r.get("mid") or "")
                if k and k in by_mid and k not in _seen:
                    _seen.add(k)
                    loose.extend(by_mid[k])
        if loose:
            parts = [{"type": "text", "text": ("【本轮图片】这几张图是这段聊天里刚发过来的，"
                                               "看清楚再决定要不要接话：")}]
            for r in loose:
                parts.append({"type": "image_url", "image_url": {"url": r["url"]}})
                used.append(r)
            msgs.append({"role": "user", "content": parts})
        if dyn:
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") != "user":
                    continue
                c = msgs[i].get("content")
                if isinstance(c, str):
                    msgs[i]["content"] = (c + "\n\n" + dyn) if c else dyn
                else:
                    # 动态内容在整条消息最末尾，不插到图和固定说明之前。
                    c.append({"type": "text", "text": dyn})
                break
            else:
                msgs.append({"role": "user", "content": dyn})
        return msgs, [dict((k, v) for k, v in r.items() if k != "url") for r in used]

    def status(self) -> dict:
        return {
            "fail_streak": self._fail_streak,
            "fallback_until": self._fallback_until,
            "fallback_left": max(0, int(self._fallback_until - time.time())),
            "tools": len(self._catalog),
            "identity_at": self._identity_at,
        }

    def reset_fail(self) -> None:
        self._fail_streak = 0
        self._fallback_until = 0.0
