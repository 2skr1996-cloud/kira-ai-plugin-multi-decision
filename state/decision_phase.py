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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

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


# 决策提示词默认稿。用户 m05902 要「可以预填充一些简单通用的」，所以这里写的是一份
# 能直接用的判断规则，不是空壳；设置页首次打开时把这段当默认值显示。
# 用户 m01209（图2/图3）要求：决策词写成通用、偏「节奏控制」的版本——群里本来就是
# 话题选择性聊天，别人在说话不等于我不能接，判的是「这一句接进去会不会打断」。
# 同时把「怎么答」抽成独立的一块（DEFAULT_DECISION_FORMAT），提示词里不再出现 say：
# 决策那一步只决定回不回，不引导角色说什么。
DEFAULT_DECISION_PROMPT = """你在判断：这条群消息，此刻我要不要开口。
只管节奏——什么时候该接一句，什么时候该安静。跟具体聊什么、跟我是谁无关。

### 该接
- 有人 @ 我、叫我的名字，或者直接回复我的消息；
- 正在聊的话题我有话可说，接一句能顺着往下走。群里本来就是几个人你一句我一句，
  **别人在互相说话，不等于我不该插话**——看的是这句接进去会不会打断，不是有没有人在聊；
- 有人提问还没人接，我正好能答；
- 冷场有一阵了，我抛一句能让气氛重新活起来的话。

### 别接
- 两个人正在点对点说一件具体的事（约时间、吵架、交接事情），我插进去只会打断；
- 只是表情、「哈哈」「草」这类附和，没有可接的内容；
- 广告、刷屏、无意义重复、纯链接；
- 我上一句刚发出去没多久，再说就是刷屏；
- 被人反复 @、反复点名，可翻来覆去只有「在吗」「理我」「叫你半天了」这种，没有实质的
  问题或需求——可以拒回，不必每次都应付；等他把真正想说的说出来再接；
- 完全看不懂在聊什么。

宁可少回，不要硬回；但话题合适时也不用等着被点名。"""

# 「输出规则」独立一块（用户图3）。决策那一步只回一个判断，不替角色写台词。
DEFAULT_DECISION_FORMAT = """### 怎么回答
只输出一个 JSON 对象，不要写别的，不要代码块，不要解释：
{"reply": true, "reason": "一句话理由，20 字以内，给用户看"}

reason 是给用户看的一句话，两件事都要照顾到：
1. 先说清为什么回 / 为什么不回（半句话就够）；
2. 回的时候顺手补一条对这次回复有用的信息——对方真正想要的是什么、这一句该往哪个方向接
   （例如「他在问配置，直接给一套能跑的」）。这句是给下一段写回复时的提示，不是要发出去的内容。

不回就把 reply 写成 false，只有理由，不用补建议。
不要在这里写整段台词「打算说什么」「要怎么回」——那是真正开口那一步的事。

### 一次只回一个人
群里可能同时几个人在说话。回的时候挑最该回的那一个人，只对着他说；
不要一条里同时应付好几个人，也不要挨个都答一遍。"""

# 用户 m36396：决策这一侧也补一段「强化格式遵守」，位置跟回复页那一块一模一样——贴在本轮消息最后。
# 回复那块讲的是「用 <msg> 包气泡、空壳代表不发」；决策只回一个判断，这块讲的是「只准吐一个 JSON」。
# 真踩过：模型不吐 JSON、只回一句聊天话（"[16:55] 判定不回（……）"），那一轮按失败算。
DECISION_FORMAT_HARD = """【格式强化 · 每一条都要照做】
- 这一次你的全部输出只有一个 JSON 对象：开头是 {，结尾是 }。
- 前后不要写任何字：不解释、不寒暄、不复述这段规则、不加代码块。
- 只有这两种形状，字段名逐字照抄：
  {"reply": true, "reason": "一句话理由，20 字以内"}
  {"reply": false, "reason": "一句话理由，20 字以内"}
- reply 只能是 true 或 false，不带引号，不要写成「要回」「不回」。
- 想说的别的话也只写进 reason；reason 一句话就够。
- 拿不准就写 false。"""

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


# 用户 m01209 图2：决策规则可以拆成几个小模块写。这三条是「＋ 新增模块」里能直接挑的
# 骨架，挑出来就是一块能改的正文，不用把整段判断规则全塞在一块里。
DECISION_SKELETONS = (
    ("when-yes", "什么时候接", """该接：
- 有人 @ 我、叫我的名字，或者直接回复我的消息；
- 正在聊的话题我有话可说，接一句能顺着往下走。别人在互相说话，不等于我不该插话——看的是这句接进去会不会打断；
- 有人提问还没人接，我正好能答；
- 冷场有一阵了，我抛一句能让气氛重新活起来的话。"""),
    ("when-no", "什么时候别接", """别接：
- 两个人正在点对点说一件具体的事（约时间、吵架、交接事情），我插进去只会打断；
- 只是表情、「哈哈」「草」这类附和，没有可接的内容；
- 广告、刷屏、无意义重复、纯链接；
- 我上一句刚发出去没多久，再说就是刷屏；
- 被人反复 @、反复点名，可翻来覆去只有「在吗」「理我」「叫你半天了」这种，没有实质的问题或需求——可以拒回，不必每次都应付；
- 完全看不懂在聊什么。"""),
    ("rhythm", "节奏与分寸", """节奏：
- 宁可少回，不要硬回；但话题合适时也不用等着被点名。
- 别人在互相说话不是我该闭嘴的理由，只看这一句接进去会不会打断。
- 一次只回一个人：几个人同时在说，就挑最该回的那个，只对着他说，别一条里挨个答一遍。
- 连着两轮没人接，就先安静一阵，不要自己接自己。"""),
)


# 「聊天规则提示词」默认稿。用户 m05902 要求注释里写清楚写什么（语气、喜欢聊什么），
# 并预填一份简单的示例，所以这段既是说明也是模板。
DEFAULT_CHAT_RULES = """【说话语气】口语、短句，像群里一个正常人随手打字；不用书面语，不写「首先/其次/总之」。
【喜欢聊什么】二次元、游戏、动画画风、日常吐槽；感兴趣的话题就接一句，不感兴趣就略过。
【不聊什么】政治、别人的隐私、看不懂的梗不硬接。
【分寸】别人在说正事、吵架、发广告时不插嘴；被点到名才正经回答。
【长度】一般一两句，最多三行。"""


TOOL_HEAD = """需要外部信息（查资料、算数、看时间之类）时可以调工具，最多 %d 轮。
工具结果我会贴回给你，看到结果后再给最终判断。"""


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

    persona: str = ""
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


def options_from_config(get) -> DecisionOptions:
    """从 get(key, default) 读配置。get 由调用方给（能带 sid 覆盖）。"""
    def _int(key, default, lo=0, hi=None):
        try:
            v = int(float(get(key, default) or default))
        except Exception:
            v = default
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
        persona=str(get("decision_persona", "") or "").strip(),
        chat_rules=str(get("decision_chat_rules", DEFAULT_CHAT_RULES) or ""),
        prompt=migrate_prompt(get("decision_prompt", DEFAULT_DECISION_PROMPT) or ""),
        tools=[str(x).strip() for x in tools if str(x).strip()],
        model=str(get("decision_model", "") or "").strip(),
        fallback=str(get("decision_fallback_model", "") or "").strip(),
        retry=_int("decision_retry", 2, 0, 5),
        retry_delay=float(_int("decision_retry_delay", 3, 0, 60)),
        fail_switch=_int("decision_fail_switch", 2, 1, 10),
        cooldown=float(_int("decision_fallback_cooldown", 300, 30, 3600)),
        timeout=float(_int("decision_timeout", 60, 5, 300)),
        fail_action=str(get("decision_fail_action", "allow") or "allow").strip().lower(),
        tool_rounds=_int("decision_tool_rounds", 2, 0, 5),
    )


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

    def build_system(self, opt: DecisionOptions, persona: str, tools_help: str = "",
                     identity: str = "") -> list:
        """按固定顺序拼 system 段，返回非空段列表（顺序即发送顺序）。"""
        parts = []
        p = (persona or "").strip()
        if p:
            parts.append("【人设】\n" + p)
        # v3.92（用户 m06951 第 6 条）：「聊天规则」不再进决策这一份——它只在真正开口那一轮
        # 起作用，改由 state/reply_prompt.py 插到回复系统提示词的人格段后面。
        # 决策这一份只剩「人设 → 判断规则 → 工具 → 身份环境」。
        prompt = (opt.prompt or "").strip() or DEFAULT_DECISION_PROMPT
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

    def transcript(self, messages) -> str:
        """把窗口消息摊成聊天记录（内容原样，只去掉空行）。"""
        rows = []
        for m in messages or []:
            role = str(_field(m, "role") or "").strip().lower()
            body = _text_of(_field(m, "content")).strip()
            if not body:
                continue
            if role == "system":
                continue
            if role == "assistant":
                for line in _clean_bot(body):
                    rows.append("【我】" + line)
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
        """发一次请求。返回 {ok, text, tool_calls, in, out, cached, error}。"""
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
        try:
            resp = client.chat(req)
            if asyncio.iscoroutine(resp):
                resp = await asyncio.wait_for(resp, timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "决策模型超时（%d 秒）" % int(timeout)}
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        text = str(getattr(resp, "text_response", "") or "")
        calls = list(getattr(resp, "tool_calls", []) or [])
        return {
            "ok": True,
            "text": text,
            "tool_calls": calls,
            "in": int(getattr(resp, "input_tokens", 0) or 0),
            "out": int(getattr(resp, "output_tokens", 0) or 0),
            "cached": int(getattr(resp, "cached_tokens", 0) or 0),
        }

    async def _run_tool(self, tool, args: dict) -> str:
        try:
            got = tool.execute(**args)
            if asyncio.iscoroutine(got):
                got = await got
        except Exception as exc:
            return "工具执行失败：%s: %s" % (type(exc).__name__, exc)
        if isinstance(got, str):
            return got
        try:
            return json.dumps(got, ensure_ascii=False)
        except Exception:
            return str(got)

    async def _tool_loop(self, client, messages, tools, opt: DecisionOptions, budget: dict) -> dict:
        """多轮工具循环。返回最后一次模型结果（带上累计 token）。"""
        names = {str(getattr(t, "name", "") or ""): t for t in tools}
        last = await self._one_call(client, messages, tools, opt.timeout)
        rounds = 0
        while last.get("ok") and last.get("tool_calls") and rounds < max(0, opt.tool_rounds):
            rounds += 1
            calls = []
            for raw in last["tool_calls"]:
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
                tool = names.get(name)
                result = await self._run_tool(tool, args) if tool else "没有这个工具：" + name
                messages.append({"role": "tool", "tool_call_id": cid, "content": result[:4000]})
            messages.insert(len(messages) - len(calls),
                            {"role": "assistant", "content": last.get("text") or "", "tool_calls": calls})
            budget["in"] += last.get("in", 0)
            budget["out"] += last.get("out", 0)
            budget["cached"] += last.get("cached", 0)
            last = await self._one_call(client, messages, tools, opt.timeout)
        return last

    async def _chain(self, messages, tools, opt: DecisionOptions) -> dict:
        """主模型 → 重试 → fallback。返回最后一次尝试的结果并附上用量。"""
        budget = {"in": 0, "out": 0, "cached": 0}
        now = time.time()
        use_fallback = bool(opt.fallback) and (now < self._fallback_until or
                                               self._fail_streak >= max(1, opt.fail_switch))
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
                last = await self._tool_loop(client, msg_copy, tools, opt, budget)
                last["attempts"] = attempts
                last["model"] = model or "fast"
                last["via"] = kind
                last["tool_messages"] = msg_copy
                budget["in"] += last.get("in", 0)
                budget["out"] += last.get("out", 0)
                budget["cached"] += last.get("cached", 0)
                if last.get("ok"):
                    parsed = parse_decision(last.get("text") or "")
                    if parsed is not None:
                        self._fail_streak = 0
                        last["parsed"] = parsed
                        last.update(budget)
                        return last
                    last["error"] = "模型没按 JSON 回（%s）" % (str(last.get("text") or "")[:80])
                    note = DECISION_RETRY_NOTE
                    logger.warning("[multi-decision] 决策第 %d 次没按 JSON 回（%s），重试：%s",
                                   t + 1, model or "快速模型", str(last.get("text") or "")[:160])
                else:
                    note = ""
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
        persona = ""
        if opt.persona:
            try:
                persona = await self.persona_text(opt.persona)
            except Exception:
                logger.exception("[multi-decision] 取人格失败，按无人格处理")
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
            system_parts = self.build_system(opt, persona, help_text, identity or self.identity_text())
            system_text = "\n\n".join(system_parts)
        body = self.transcript(messages)
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
        got = await self._chain(payload, tools, opt)
        parsed = got.get("parsed")
        # 带图那次没成（上游不认 image_url / 图太大 / 模型没有视觉），退回纯文字再判
        # 一次：宁可这一次看不见图，也不能因为有图把整轮判断判死。
        img_fallback = False
        img_err = ""
        img_tries = 0
        if img_rows and not (bool(got.get("ok")) and parsed):
            # v4.65（用户 m36234）：带图那次的真实报错要留下来。以前只留「最后一次
            # 尝试」的 error（纯文字那次），带图为什么没成、试了几次，事后全查不到。
            img_err = str(got.get("error") or "")[:300]
            img_tries = int(got.get("attempts") or 0)
            logger.warning("[multi-decision] 决策带图失败（%d 张图，试了 %d 次）：%s，退回纯文字重判一次",
                           len(img_rows), img_tries, img_err[:200])
            img_fallback = True
            no_img, _unused = self.chat_payload(messages, None, dyn)
            payload = [{"role": "system", "content": system_text}] + no_img
            if len(payload) < 2:
                payload.append({"role": "user", "content": user_text})
            got = await self._chain(payload, tools, opt)
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
            "input_tokens": int(got.get("in") or 0),
            "output_tokens": int(got.get("out") or 0),
            "cached_tokens": int(got.get("cached") or 0),
            "tools_used": [str(getattr(t, "name", "")) for t in tools],
            "system_parts": system_parts,
            "transcript": body,
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
        for m in messages or []:
            role = str(_field(m, "role") or "").strip().lower()
            if role == "system":
                continue
            raw = _text_of(_field(m, "content")).strip()
            body = "\n".join(_clean_bot(raw)) if role == "assistant" else _clean_user(raw)
            got = by_mid.pop(_mid_of_text(raw), []) if rows else []
            if not body and not got:
                continue
            if got:
                parts = [{"type": "text", "text": body or "[图片]"}]
                for r in got:
                    parts.append({"type": "image_url", "image_url": {"url": r["url"]}})
                msgs.append({"role": role or "user", "content": parts})
            else:
                msgs.append({"role": role or "user", "content": body})
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
            msgs.append({"role": "user", "content": parts})
        if dyn:
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") != "user":
                    continue
                c = msgs[i].get("content")
                if isinstance(c, str):
                    msgs[i]["content"] = (c + "\n\n" + dyn) if c else dyn
                else:
                    for part in c:
                        if isinstance(part, dict) and part.get("type") == "text":
                            part["text"] = (str(part.get("text") or "") + "\n\n" + dyn).strip()
                            break
                    else:
                        c.append({"type": "text", "text": dyn})
                break
            else:
                msgs.append({"role": "user", "content": dyn})
        return msgs, [dict((k, v) for k, v in r.items() if k != "url") for r in rows]

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
