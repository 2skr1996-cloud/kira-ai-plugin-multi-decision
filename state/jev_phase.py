"""JEV 决策：用专用 decision 类模型（TypeSafe 的 choice()）判「回不回」。

与 state/decision_phase.py 的普通决策并列，但走的是框架另一条接口：

* 普通决策 = LLM 客户端，发一条完整 chat 请求，让模型吐 JSON（reply/reason/say）。
* JEV 决策 = DecisionModelClient，发一次 choice()，只拿「回复 / 不回复」两档的
  概率分布，不生成任何正文。

判定合同（交接文档 §4，全部按它实现）：

* p = probabilities["reply"]，下限 L / 上限 U 都归一到 0~1。
* p < L → 不回复；L <= p <= U → 不确定（含两端点）；p > U → 回复。
* 判定一律用模型返回的原始概率，四舍五入只用于展示（round(p*100) 写进记录）。
* confidence 不是回复概率，不参与判定。

失败（缺字段 / 非数值 / NaN / inf / 越界 / 选项名不匹配 / 分布非法 / 超时 /
网络与鉴权错误）一律算「失败」，不进不确定分支：按决策失败动作放行或拦下，
绝不把网络失败伪装成概率 0 / 50% / 不确定。

不确定区间的第三个动作「交给决策模型」会串行转交一次普通决策：用同一批冻结
输入（不再取窗口、不再防抖），只转交一次，不递归回 JEV。

用户 m08123：宿主目前只有 TypeSafe 一家 decision 接口，所以再加一条「自定义第三方
接口」后端——直接按 SystemOne 协议（POST {api_base}/v1/systemone，Bearer 密钥）
发一道 noul 题，拿回的 noul 值就是回复概率。两条后端共用同一套区间判定 / 记录 /
转交逻辑，只换取概率的那一层；密钥单独落盘，不进插件配置、不回给页面。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .sids import bot_name

try:  # 离线测试里 core 不一定在
    from core.logging_manager import get_logger as _plugin_logger  # type: ignore
except Exception:  # pragma: no cover - 只在离线测试里
    _plugin_logger = None
logger = _plugin_logger("multi_decision", "orange") if _plugin_logger else logging.getLogger(__name__)

# 宿主缺 DECISION 时不能把插件加载搞崩：这一段允许整体缺失，运行入口再判 available()。
try:  # pragma: no cover - 真机才有
    from core.provider import ModelType, ProviderAPIError  # type: ignore
except Exception:  # pragma: no cover - 离线测试 / 旧宿主
    ModelType = None  # type: ignore
    ProviderAPIError = None  # type: ignore

#: choice() 的两档选项名。改这里等于改判定合同，记录里的概率键会跟着变。
JEV_CHOICES = ("reply", "no_reply")

#: 不确定区间的出厂默认（0~1）。判断规则里写的是「明显该接 0.75 以上、明显别接 0.25 以下」，
#: 页面、配置回退、接口默认值都从这里取，别再各写一份。
DEFAULT_LOWER = 0.25
DEFAULT_UPPER = 0.75

CRITERIA = {
    "reply": "由你开口，往群里说一句符合当前话题的话。",
    "no_reply": "这一轮什么都不说，让群里继续。",
}

DEFAULT_JEV_PROMPT = """判断此刻该不该有{{bot}}的声音，只给一个概率。

- 有人在问{{bot}}、叫{{bot}}的名字、接着{{bot}}上一句说：给高概率。
- 群里两人在对聊、聊的事与{{bot}}无关、刷屏 / 吵架 / 只发表情：给低概率。
- 话题冷场、有人抛出问题没人接、气氛正需要有人接一句：可以给高一点。
- 拿不准就给中间概率，交给「不确定区间」去决定，别硬凑一个 0 或 1。

只给概率，不写回复内容。"""


# 2026-11-08（J）：JEV 页也分段——最上面先说清判断的人不是 bot 本身，再五段通用规则。
# 正文里指代 bot 的地方写{{bot}}，装配时换成这个会话里它自己的昵称（没设就退全局名）。
JEV_ROLE = """### 角色设定
我不是{{bot}}，我只负责判断这条群消息此刻该不该有{{bot}}的声音，只给一个概率，不写回复内容。
"""

#: 2026-11-08（J）：JEV 的判断规则也拆成默认模板（跟决策页同一个长相、同一种骨架），
#: 内容按「一道概率题、只吐一个回复概率」的合同写，参考判断规则·模板2 的四维口径，
#: 但不再逐个维度打分，只把「什么局面给高、什么局面给低、拿不准给中间」说清。
JEV_SKELETONS = (
    ("when-yes", "什么时候给高概率", """### 该给高概率
- 有人 @{{bot}}、叫{{bot}}的名字或昵称、直接回复{{bot}}的消息——一条就够；
- 有人自我宣告（「睡了」「晚安」「早」「来了」）——一条就够；
- 有人情绪吐槽（「好累」「不想上班」）——同一个人说到第二条再接；
- 两三个人在实质聊天，{{bot}}插一句不会把话打断。别人在互相说话，不等于不该插话；
- 有人分享自己的经历；
- 有人问了个完整的问题，还没人接；
- 明显在玩梗、接梗、双关、谐音梗；
- 三个人以上围着同一个话题聊得很热——偶尔插一句，不逐轮跟。"""),
    ("when-no", "什么时候给低概率", """### 别给高概率
- 两个人或几个人正在点对点说一件具体的事（约时间、吵架、交接事情、私人约定）；
- 最近一条已经过去 5 分钟以上、中间没有新消息接着——不起话头；
- 5 分钟内有人（包括{{bot}}自己）说过差不多的话——不重复；
- 只是表情、「哈哈」「草」「6」这类附和，没有可接的内容；
- 出现不认识的词、黑话、简称，拿不准在说什么——别硬给；
- 消息里带链接：纯链接、链接加一句话、视频没有文字评论；
- 小程序、卡片分享；
- 被反复 @、反复点名，可翻来覆去只有「在吗」「人呢」「理我」这类，没有实质内容——可以给低，不必每次都应付；
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
- 同一个话题连着接不超过 3 轮；话题换了重新算；
- 被追问时接一两轮就该收；
- 一模一样的话由不同人说出来，只当一次触发；
- 不主动分析 5 分钟前的消息；旧消息只用来理解背景，除非被当前消息直接引用或追问。"""),
    ("bottom", "底线", """### 底线
宁可少给，不要硬给；但话题合适时也不用等着被点名。
明显该接给 0.75 以上；明显别接给 0.25 以下；拿不准给中间概率，交给不确定区间去定，别硬凑 0 或 1。
不写回复内容，也不替{{bot}}决定说什么。"""),
)


#: 两条后端：framework = 宿主的 decision 类模型（get_model_client + choice()）；
#: custom = 用户自己填的第三方 SystemOne 接口。
JEV_BACKENDS = ("framework", "custom")
DEFAULT_API_BASE = "https://api.typesafe.ai"
DEFAULT_API_MODEL = "jev-latest"
#: 官方协议自己限的响应体上限（astrbot-plugin-jev 同样按 64KB 拦）。
MAX_RESPONSE_BYTES = 65536
#: 自定义接口只有一道 noul 题：true = 该回复，概率就是回复概率。
CUSTOM_QUESTION_NAME = "reply"
CUSTOM_QUESTION_LABEL = "回复"
CUSTOM_CRITERIA = {
    "true": CRITERIA["reply"],
    "false": CRITERIA["no_reply"],
}

_zones = ("reply", "no_reply", "uncertain")
_actions = ("reply", "no_reply", "decision")


def normalize_action(raw, default: str = "no_reply") -> str:
    """把不确定时的动作归一成 reply / no_reply / decision。"""
    val = str(raw or "").strip().lower()
    if val in ("", "auto"):
        return default
    if val in ("off", "no", "none", "silent", "skip", "block", "不回复"):
        return "no_reply"
    if val in ("on", "yes", "reply", "re", "回复"):
        return "reply"
    if val in ("decision", "decide", "llm", "交给决策模型", "交给普通决策"):
        return "decision"
    return default


def normalize_backend(raw, default: str = "framework") -> str:
    """接口来源：framework = 宿主的 decision 类模型；custom = 自己的第三方接口。"""
    val = str(raw or "").strip().lower()
    if val in ("", "auto"):
        return default
    if val in ("custom", "api", "third", "thirdparty", "third_party", "http", "systemone",
               "自定义", "第三方", "自定义接口", "第三方接口"):
        return "custom"
    if val in ("framework", "host", "kira", "model", "builtin", "host_model", "宿主", "框架"):
        return "framework"
    return default


def _is_private_host(host: str) -> bool:
    """判 host 是不是本机 / 内网 / 保留地址（官方协议不允许打到这些地方）。"""
    name = str(host or "").strip().lower().strip("[]")
    if not name:
        return True
    if name in ("localhost", "localhost.localdomain"):
        return True
    for suffix in (".local", ".internal", ".home.arpa", ".arpa", ".localhost"):
        if name.endswith(suffix):
            return True
    try:
        addr = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def check_api_base(raw, default: str = "") -> tuple:
    """体检第三方地址，返回 (归一地址, 不合法原因)。

    规则与官方插件一致：必须 https；不许带账号密码、query、fragment、".."；
    不许指向本机 / 内网 / 保留地址。合法时原因为空串；不合法时地址为空串、
    原因说清是哪一条不满足（配置体检、列模型、真正发请求共用这一份口径）。
    空地址不算「不合法」：回 default（空串表示不回落），原因为空。
    """
    text = str(raw or "").strip()
    if not text:
        return default, ""
    try:
        parts = urllib.parse.urlsplit(text)
    except Exception:
        return "", "地址解析不了"
    scheme = parts.scheme.lower()
    if scheme != "https":
        if scheme == "http":
            return "", "只收 https，不能是 http"
        if not scheme:
            return "", "地址要以 https:// 开头"
        return "", "只收 https（当前是 %s）" % scheme
    if parts.username or parts.password:
        return "", "不能带账号密码"
    if parts.query or parts.fragment:
        return "", "不能带查询参数或 # 片段"
    path = parts.path or ""
    if ".." in urllib.parse.unquote(path):
        return "", "路径里不能有 .."
    if not (parts.hostname or ""):
        return "", "地址里没有主机名"
    if _is_private_host(parts.hostname or ""):
        return "", "不能指向本机或内网地址"
    netloc = parts.netloc.lower()
    base = "https://" + netloc + path.rstrip("/")
    if not base:
        return "", "地址解析不了"
    return base, ""


def normalize_api_base(raw, default: str = DEFAULT_API_BASE) -> str:
    """归一第三方地址：只留 https + host + 路径前缀，去掉尾斜杠。

    非法就回落到 default（空串表示不回落）；具体是哪条不满足由
    check_api_base 给出，这里只取归一后的地址。
    """
    base, _why = check_api_base(raw, default)
    return base or default


def api_endpoint(base: str) -> str:
    """把用户填的地址拼成真正要打的那条 URL（插件自己补 /v1/systemone）。"""
    text = str(base or "").strip().rstrip("/")
    low = text.lower()
    if low.endswith("/v1/systemone"):
        return text
    if low.endswith("/v1"):
        return text + "/systemone"
    return text + "/v1/systemone"


PROTOCOLS = ("systemone", "openai")
DEFAULT_PROTOCOL = "systemone"


def normalize_protocol(raw, default: str = DEFAULT_PROTOCOL) -> str:
    """自定义接口说哪种协议：SystemOne（Jev 那套）还是 OpenAI 兼容聊天。"""
    text = str(raw or "").strip().lower()
    if text in ("openai", "chat", "openai-compatible", "compatible", "兼容", "openai兼容", "聊天"):
        return "openai"
    if text in ("systemone", "system-one", "system_one", "jev", "typesafe"):
        return "systemone"
    return default


def api_chat_url(base: str) -> str:
    """OpenAI 兼容协议要打的地址（阿里百炼 / OpenRouter 这类网关带 /v1）。"""
    text = str(base or "").strip().rstrip("/")
    low = text.lower()
    if low.endswith("/chat/completions"):
        return text
    if low.endswith("/v1/systemone"):
        return text[: -len("/systemone")] + "/chat/completions"
    if low.endswith("/v1"):
        return text + "/chat/completions"
    return text + "/v1/chat/completions"


def api_model_urls(base: str) -> list:
    """列模型的候选地址（用户 m08755 要「自动获取」）。

    阿里百炼、OpenRouter 这些 OpenAI 兼容网关都提供 GET /v1/models；
    官方 SystemOne 没有列模型的接口，列不出来就由页面提示手填。
    """
    text = normalize_api_base(base, "")
    if not text:
        return []
    low = text.lower()
    if low.endswith("/v1/systemone"):
        text = text[: -len("/v1/systemone")].rstrip("/")
        low = text.lower()
    if low.endswith("/v1"):
        return [text + "/models"]
    return [text + "/v1/models", text + "/models"]


#: 用户 m10825：SystemOne 是独立协议，模型名在文档里就是固定的，网关的
#: /models 只列 OpenAI 兼容聊天模型，永远找不到 decision-model-preview。
#: 「获取」在 SystemOne 页不再去问 /models，直接给这份内置候选，仍可手填。
SYSTEMONE_KNOWN_MODELS = ("decision-model-preview", "jev-latest")


def systemone_models(base: str) -> list:
    """按网关给出 SystemOne 的已知模型名。

    阿里百炼的决策模型固定叫 decision-model-preview；TypeSafe 官方叫 jev-latest。
    认不出网关时两个都给，页面仍允许手改，避免把用户锁死在一个猜测上。
    """
    low = str(base or "").strip().lower()
    if "typesafe.ai" in low or "systemonemodels.ai" in low:
        return ["jev-latest"]
    if "aliyuncs.com" in low or "aliyun.com" in low:
        return ["decision-model-preview"]
    return list(SYSTEMONE_KNOWN_MODELS)


#: 用户 m09981：自定义接口不再只存一份——设置页用 1/2/3/4 小页签放多份，
#: 统一成 [{id, protocol, base, model}]，密钥按 id 分开落盘（见 load_secrets）。
MAX_APIS = 12


def normalize_api_id(raw, default: str = "1") -> str:
    """端点 id：就是页面上的小页签编号，短、稳定、能当字典键。"""
    text = str(raw or "").strip()
    return text[:8] if text else default


def make_api(eid="1", protocol=DEFAULT_PROTOCOL, base="", model="") -> dict:
    """一条自定义接口的归一形状。

    地址只去空白、不在这里洗成空串：用户把 http:// 或内网地址存进来时，
    配置体检要能按原样指出来，页面也不能看着自己填的地址凭空消失。
    真正发请求前由 _custom_call 用 check_api_base 再校验一遍（不合法就当场
    失败、不发请求，重试也不会变）。
    """
    return {
        "id": normalize_api_id(eid),
        "protocol": normalize_protocol(protocol),
        "base": str(base or "").strip()[:400],
        "model": str(model or "").strip()[:120],
    }


def parse_apis(raw) -> list:
    """jev_apis 配置（JSON 字符串或列表）→ 去重后的端点列表；坏数据当空。"""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except Exception:
            return []
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for item in data[:MAX_APIS * 2]:
        if not isinstance(item, dict):
            continue
        eid = normalize_api_id(item.get("id") or item.get("name"))
        if eid in seen:
            continue
        seen.add(eid)
        out.append(make_api(eid, item.get("protocol"), item.get("base"), item.get("model")))
        if len(out) >= MAX_APIS:
            break
    return out


def dump_apis(items) -> str:
    """存回配置用：始终写归一后的 JSON，页面读到的和运行时用的同一份。"""
    return json.dumps(parse_apis(items), ensure_ascii=False)


def endpoint_url(protocol: str, base: str) -> str:
    """按协议给真正要打的地址（页面「实际请求地址」与运行时共用这一条规则）。"""
    return api_chat_url(base) if normalize_protocol(protocol) == "openai" else api_endpoint(base)


def clamp_unit(raw, default: float) -> float:
    """0~1 的小数；非法值回默认，不做静默调换。"""
    try:
        val = float(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(val):
        return float(default)
    if 1.0 < val <= 100.0:  # 允许页面里按百分比存（20 而不是 0.2）
        val = val / 100.0
    return min(1.0, max(0.0, val))


def sort_bounds(lower, upper) -> tuple:
    """上下限：各自归一；顺序反了就交换（页面能填反，运行时不崩）。"""
    lo = clamp_unit(lower, DEFAULT_LOWER)
    hi = clamp_unit(upper, DEFAULT_UPPER)
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def zone_of(prob: float, lower: float, upper: float) -> str:
    """p < L 不回；L <= p <= U 不确定（含端点）；p > U 回。"""
    if prob < lower:
        return "no_reply"
    if prob > upper:
        return "reply"
    return "uncertain"


def read_reply_probability(probabilities) -> tuple:
    """从 ChoiceResponse.probabilities 里取回复概率。

    返回 (prob, clean, error)。error 非空 = 这次判定不算数（按失败处理）。
    clean 是逐项归一后的 {选项: 0~1}，给记录页用。
    """
    if not isinstance(probabilities, dict) or not probabilities:
        return None, {}, "模型没有返回概率分布"
    keys = set(probabilities.keys())
    if keys != set(JEV_CHOICES):
        return None, {}, "模型返回的选项名对不上（%s）" % ", ".join(sorted(map(str, keys)))
    clean = {}
    for key in JEV_CHOICES:
        raw = probabilities.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, {}, "概率不是数值（%s）" % key
        val = float(raw)
        if not math.isfinite(val):
            return None, {}, "概率不是有限值（%s）" % key
        if val < 0.0 or val > 1.0:
            return None, {}, "概率越界（%s=%.4f）" % (key, val)
        clean[key] = val
    total = clean["reply"] + clean["no_reply"]
    if not math.isclose(total, 1.0, abs_tol=0.01):
        return None, clean, "概率分布非法（合计 %.4f）" % total
    return clean["reply"], clean, ""


def format_prob(prob: float) -> str:
    """展示用：四舍五入到整数百分比，但只在展示层用。"""
    try:
        return "%d%%" % int(round(float(prob) * 100))
    except Exception:
        return "0%"


@dataclass
class JevOptions:
    """一次 JEV 判定要用的配置（从插件配置里读出来，便于单测）。"""

    enabled: bool = False
    model: str = ""
    fallback: str = ""
    lower: float = DEFAULT_LOWER
    upper: float = DEFAULT_UPPER
    action: str = "no_reply"
    prompt: str = DEFAULT_JEV_PROMPT
    persona: str = ""
    retry: int = 2
    retry_delay: float = 3.0
    fail_switch: int = 2
    cooldown: float = 300.0
    timeout: float = 60.0
    fail_action: str = "allow"
    # 用户 m08123：除宿主 decision 模型外，还能走自定义第三方接口
    backend: str = "framework"
    api_base: str = DEFAULT_API_BASE
    api_model: str = DEFAULT_API_MODEL
    api_key: str = ""
    # 用户 m08755：自定义接口的协议。阿里这类网关只认 OpenAI 兼容，没有 SystemOne。
    protocol: str = DEFAULT_PROTOCOL
    # 用户 m09981：多份自定义接口（设置页 1/2/3/4 小页签），运行前由插件补上各自密钥。
    apis: list = field(default_factory=list)

    def endpoint(self, eid):
        """按页签编号取一条自定义接口；没有就 None。"""
        want = normalize_api_id(eid)
        for ep in self.apis or []:
            if str(ep.get("id") or "") == want:
                return ep
        return None

    def is_custom(self) -> bool:
        return normalize_backend(self.backend) == "custom"

    def is_openai(self) -> bool:
        return normalize_protocol(self.protocol) == "openai"

    def bounds(self) -> tuple:
        return sort_bounds(self.lower, self.upper)


def options_from_config(get) -> JevOptions:
    """从 get(key, default) 读配置；重试 / 超时 / 失败动作跟普通决策共用那套键。"""

    def _int(key, default, lo=0, hi=None):
        try:
            raw = get(key, default)
            val = int(float(default if raw is None or raw is False or raw == "" else raw))
        except Exception:
            val = default
        val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    # v4.90（发版前审计）：小数项（重试间隔）不能走 _int，否则 3.5 会被截成 3。
    def _num(key, default, lo=0, hi=None):
        try:
            raw = get(key, default)
            val = float(default if raw is None or raw is False or raw == "" else raw)
        except Exception:
            val = float(default)
        # NaN / Inf 跟 _int 一样回退默认值，不能靠 max/min 夹成 0 或上限。
        if not math.isfinite(val):
            val = float(default)
        val = max(lo, val)
        if hi is not None:
            val = min(hi, val)
        return val

    try:
        enabled = bool(get("jev_enabled", False))
    except Exception:
        enabled = False
    lower, upper = sort_bounds(get("jev_lower", DEFAULT_LOWER), get("jev_upper", DEFAULT_UPPER))
    # 用户 m09981：自定义接口可以有多份。老配置没有 jev_apis，就用原来那三个键
    # 拼出第 1 页——升级后用户看到的东西不变，也不用重填。
    protocol_legacy = normalize_protocol(get("jev_api_protocol", DEFAULT_PROTOCOL))
    base_legacy = normalize_api_base(get("jev_api_base", DEFAULT_API_BASE))
    model_legacy = str(get("jev_api_model", DEFAULT_API_MODEL) or DEFAULT_API_MODEL).strip()
    apis = parse_apis(get("jev_apis", ""))
    if not apis:
        apis = [make_api("1", protocol_legacy, base_legacy or DEFAULT_API_BASE,
                         model_legacy or DEFAULT_API_MODEL)]
    model = str(get("jev_model", "") or "").strip()
    chosen = apis[0]
    if model.startswith("custom:"):
        want = normalize_api_id(model.split(":", 1)[1])
        chosen = next((ep for ep in apis if str(ep.get("id") or "") == want), apis[0])
    return JevOptions(
        enabled=enabled,
        model=model,
        fallback=str(get("jev_fallback_model", "") or "").strip(),
        lower=lower,
        upper=upper,
        action=normalize_action(get("jev_action", "no_reply")),
        prompt=str(get("jev_prompt", DEFAULT_JEV_PROMPT) or DEFAULT_JEV_PROMPT),
        # 用户 m06706：判断「要不要开口」也得知道自己在装谁，所以 JEV 自己有一份 jev_persona。
        # v4.89（用户 m14715 第 1 条）：旧 decision_persona 已删除，不再回退到它。
        persona=str(get("jev_persona", "") or "").strip(),
        retry=_int("decision_retry", 2, 0, 5),
        retry_delay=_num("decision_retry_delay", 3, 0, 60),
        fail_switch=_int("decision_fail_switch", 2, 1, 10),
        cooldown=float(_int("decision_fallback_cooldown", 300, 30, 3600)),
        timeout=float(_int("decision_timeout", 60, 5, 300)),
        fail_action=str(get("decision_fail_action", "allow") or "allow").strip().lower(),
        # 用户 m08123：接口来源与自定义接口的三个参数；密钥不从这里读，
        # 由插件在读取时补进来（它单独落盘，不进配置、不回页面）。
        backend=normalize_backend(get("jev_backend", "framework")),
        # 聚合字段跟随真正选中的那一页（选宿主模型时就是第 1 页），
        # 记录页 / 老测试读的就是这几个字段。
        api_base=str(chosen.get("base") or DEFAULT_API_BASE),
        api_model=str(chosen.get("model") or DEFAULT_API_MODEL),
        api_key="",
        protocol=str(chosen.get("protocol") or DEFAULT_PROTOCOL),
        apis=apis,
    )


def split_model_key(key: str) -> tuple:
    """provider:model_id → (provider, model)。model_id 里允许再有冒号。"""
    text = str(key or "").strip()
    if ":" not in text:
        return "", ""
    pid, mid = text.split(":", 1)
    return pid.strip(), mid.strip()


# ---------- 自定义第三方接口：密钥落盘 + SystemOne 协议 ----------

SECRET_FILENAME = "jev_secret.json"


def _secret_path(data_dir) -> Path:
    return Path(str(data_dir)) / SECRET_FILENAME


def _read_secret_file(data_dir) -> dict:
    """读整个密钥文件（坏文件 / 没有都当空）。"""
    if not data_dir:
        return {}
    try:
        path = _secret_path(data_dir)
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _clean_item(raw) -> dict:
    if not isinstance(raw, dict):
        return {"api_key": "", "api_base": "", "updated": ""}
    return {
        "api_key": str(raw.get("api_key") or "").strip(),
        "api_base": str(raw.get("api_base") or "").strip(),
        "updated": str(raw.get("updated") or ""),
    }


def load_secrets(data_dir) -> dict:
    """{页签编号: {api_key, api_base, updated}}；老的单份结构当第 1 页。"""
    raw = _read_secret_file(data_dir)
    items = raw.get("items")
    if not isinstance(items, dict):
        one = _clean_item(raw)
        return {"1": one} if (one["api_key"] or one["api_base"]) else {}
    out = {}
    for eid, item in items.items():
        one = _clean_item(item)
        if one["api_key"] or one["api_base"]:
            out[normalize_api_id(eid)] = one
    return out


def load_secret(data_dir, eid: str = "1") -> dict:
    """取某一页的密钥（默认第 1 页，兼容老调用点）；没有就返回空 dict。"""
    return load_secrets(data_dir).get(normalize_api_id(eid)) or {}


def save_secret(data_dir, api_key: str, api_base: str = "", eid: str = "1") -> bool:
    """写某一页的密钥（权限尽量收紧到 0600）；写失败返回 False。"""
    if not data_dir:
        return False
    try:
        path = _secret_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        items = load_secrets(data_dir)
        items[normalize_api_id(eid)] = {
            "api_key": str(api_key or "").strip(),
            "api_base": str(api_base or "").strip(),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        payload = {"items": items}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def clear_secret(data_dir, eid: str = "1") -> bool:
    """删掉某一页的密钥；最后一页删完就把文件删掉。"""
    if not data_dir:
        return False
    try:
        path = _secret_path(data_dir)
        items = load_secrets(data_dir)
        items.pop(normalize_api_id(eid), None)
        if not items:
            if path.is_file():
                path.unlink()
            return True
        payload = {"items": items}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def mask_key(api_key: str) -> str:
    """页面只看得到「存没存」和尾巴四位，永远不回明文。"""
    text = str(api_key or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * min(12, len(text) - 4) + text[-4:]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """官方协议不允许跟随重定向（避免密钥被带到别处）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _http_post_json(url: str, payload: dict, api_key: str, timeout: float) -> tuple:
    """POST 一个 JSON，返回 (status, body_bytes)；网络错误原样抛出。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + str(api_key or ""),
            "User-Agent": "kira-ai-plugin-multi-decision",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=float(timeout)) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    return status, raw


def _http_get_json(url: str, api_key: str, timeout: float) -> tuple:
    """GET 一个 JSON，返回 (status, body_bytes)；HTTP 错误原样抛出。"""
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + str(api_key or ""),
            "User-Agent": "kira-ai-plugin-multi-decision",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(req, timeout=float(timeout)) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    return status, raw


def parse_model_list(body) -> list:
    """从各种网关的模型清单里取出模型名（{data:[{id}]} / {models:[...]} / {models:{...}} / 列表）。"""
    items = []
    if isinstance(body, dict):
        for key in ("data", "models"):
            seq = body.get(key)
            if isinstance(seq, list):
                items = seq
                break
            if isinstance(seq, dict):
                items = list(seq.keys())
                break
    elif isinstance(body, list):
        items = body
    out = []
    for item in items or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or item.get("model") or ""
        else:
            name = ""
        name = str(name or "").strip()
        if name and name not in out:
            out.append(name)
    return out[:200]


def list_api_models(base: str, api_key: str = "", timeout: float = 15.0,
                     protocol: str = DEFAULT_PROTOCOL) -> tuple:
    """问第三方接口要模型清单，返回 (models, url, error)。

    只读、不带业务状态；拿不到就返回空清单 + 原因，页面据此提示手填。
    SystemOne 没有清单接口，直接回内置候选（url 为空表示不是从网关拉的）。
    """
    base_ok, base_why = check_api_base(base)
    if not base_ok:
        return [], "", "地址不合法（%s）" % (base_why or "还没填地址")
    if normalize_protocol(protocol, "") == "systemone":
        return systemone_models(base), "", ""
    urls = api_model_urls(base)
    if not urls:
        return [], "", "地址不合法（%s）" % (base_why or "拼不出列模型的地址")
    err = ""
    for url in urls:
        try:
            status, raw = _http_get_json(url, api_key, timeout)
        except urllib.error.HTTPError as exc:
            err = "HTTP %s" % int(getattr(exc, "code", 0) or 0)
            continue
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
            continue
        if status != 200:
            err = "HTTP %d" % status
            continue
        if len(raw) > MAX_RESPONSE_BYTES:
            err = "响应超过 %d 字节" % MAX_RESPONSE_BYTES
            continue
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            err = "返回的不是 JSON"
            continue
        models = parse_model_list(body)
        if models:
            return models, url, ""
        err = "接口通了，但里面没有模型清单"
    return [], urls[-1], err or "拿不到模型清单"


def read_custom_answer(body, question: str = CUSTOM_QUESTION_NAME) -> tuple:
    """按 SystemOne 协议校验响应，返回 (prob, model, usage, error)。

    只认 answers[question] 里的 noul：必须是 0~1 的有限数；type 必须是 noul。
    任何结构不对都算失败（不能当成 0% 或 50%）。
    """
    if not isinstance(body, dict):
        return None, "", {}, "接口返回的不是 JSON 对象"
    answers = body.get("answers")
    if not isinstance(answers, dict):
        return None, "", {}, "接口返回里没有 answers"
    answer = answers.get(question)
    if not isinstance(answer, dict):
        return None, "", {}, "接口没给出「%s」这道题的答案" % question
    kind = str(answer.get("type") or "").strip().lower()
    if kind != "noul":
        return None, "", {}, "「%s」的答案类型不是 noul（%s）" % (question, kind or "空")
    raw = answer.get("noul")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "", {}, "「%s」的 noul 不是数值" % question
    val = float(raw)
    if not math.isfinite(val):
        return None, "", {}, "「%s」的 noul 不是有限值" % question
    if val < 0.0 or val > 1.0:
        return None, "", {}, "「%s」的 noul 越界（%.4f）" % (question, val)
    model = body.get("model")
    model = model[:100] if isinstance(model, str) else ""
    usage = {}
    raw_usage = body.get("usage")
    if isinstance(raw_usage, dict):
        for key in ("input_tokens", "output_tokens"):
            val_u = raw_usage.get(key)
            if isinstance(val_u, int) and not isinstance(val_u, bool) and val_u >= 0:
                usage[key] = val_u
    return val, model, usage, ""


OPENAI_OUTPUT_RULE = (
    '只输出一个 JSON 对象，形如 {"reply": 0.73}。'
    "reply 是你判断「此刻该有人接话」的概率，取 0 到 1 之间的小数。"
    "不要输出解释、不要 Markdown、不要别的字段。"
)


def openai_messages(instructions: str, state_obj: dict) -> list:
    """把判断规则与现场拼成 OpenAI 兼容的 messages（人设 / 聊天 / 最新一条）。"""
    system = str(instructions or "").strip()
    system = (system + "\n\n" + OPENAI_OUTPUT_RULE).strip() if system else OPENAI_OUTPUT_RULE
    st = state_obj if isinstance(state_obj, dict) else {}
    lines = []
    persona = str(st.get("bot_persona") or "").strip()
    if persona:
        # v4.84（用户 m09179 第 4 条）：不再加「【人设】」抬头。
        lines.append(persona[:4000])
    recent = str(st.get("recent_conversation") or "").strip()
    if recent:
        lines.append("【最近的聊天】\n" + recent[:6000])
    target = str(st.get("target_message") or "").strip()
    if target:
        lines.append("【最新这一条】\n" + target[:2000])
    if not lines:
        lines.append("【最近的聊天】\n（这段时间没人说话）")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(lines)}]


def _extract_json_object(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw[:4].lower() == "json":
            raw = raw[4:]
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        got = json.loads(raw[start:end + 1])
    except Exception:
        return {}
    return got if isinstance(got, dict) else {}


def parse_openai_answer(body) -> tuple:
    """按 OpenAI 兼容响应取概率：choices[0].message.content 里的 JSON。"""
    if not isinstance(body, dict):
        return None, "", {}, "接口返回的不是 JSON 对象"
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None, "", {}, "接口返回里没有 choices"
    first = choices[0] if isinstance(choices[0], dict) else {}
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    got = _extract_json_object(content)
    if not got:
        return None, "", {}, "模型没有回 JSON（内容是空或不是对象）"
    raw = got.get("reply", got.get("probabilities", got.get("probability")))
    if isinstance(raw, dict):
        raw = raw.get("reply")
    if raw is None:
        for key in ("p", "score", "prob"):
            if key in got:
                raw = got.get(key)
                break
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "", {}, "JSON 里没有 0~1 的 reply 数值"
    val = float(raw)
    if not math.isfinite(val):
        return None, "", {}, "reply 不是有限值"
    if val < 0.0 or val > 1.0:
        return None, "", {}, "reply 越界（%.4f）" % val
    model = body.get("model")
    model = model[:100] if isinstance(model, str) else ""
    usage = {}
    raw_usage = body.get("usage")
    if isinstance(raw_usage, dict):
        for out_key, src in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
            val_u = raw_usage.get(src)
            if isinstance(val_u, int) and not isinstance(val_u, bool) and val_u >= 0:
                usage[out_key] = val_u
    return val, model, usage, ""


class JevPhase:
    """装配 JEV 请求、调 choice()、按区间给结论；转交普通决策也在这里做。"""

    def __init__(self, plugin=None):
        self.plugin = plugin
        self._fail_streak = 0
        self._fallback_until = 0.0
        self._last_error = ""

    # ---------- 名字占位符 ----------

    def _bot_name(self, sid: str = "") -> str:
        """2026-11-08（J）：这个会话里 bot 自己的昵称，没设就退全局名，再没有就写「机器人」。"""
        return bot_name(self.plugin, sid)

    def _fill_bot(self, text, sid: str = "") -> str:
        """把默认判断规则里的{{bot}}换成真名字（跟决策稿、提示词模块同一个口径）。"""
        return str(text or "").replace("{{bot}}", self._bot_name(sid))

    # ---------- 宿主能力 ----------

    @staticmethod
    def available() -> bool:
        """宿主是否支持 decision 类模型（旧宿主缺 DECISION 只挡住新模式）。"""
        return ModelType is not None and hasattr(ModelType, "DECISION")

    def status(self) -> dict:
        return {
            "available": self.available(),
            "models": len(self.models()),
            "error": self._last_error,
            "fail_streak": self._fail_streak,
        }

    def models(self) -> list:
        """只列启用提供商里的 decision 类模型（普通决策仍是 llm/vlm）。"""
        return models_of_type("decision")

    # ---------- 客户端 ----------

    def _client_for(self, key: str):
        """按 provider:model_id 取 decision 客户端。失败返回 (None, 原因)。"""
        if not self.available():
            return None, "当前宿主不支持 decision 类模型（需要 KiraAI 3.1.1 及以上）"
        pid, mid = split_model_key(key)
        if not pid or not mid:
            return None, "没有选择 JEV 决策模型"
        ctx = getattr(self.plugin, "ctx", None)
        mgr = getattr(ctx, "provider_mgr", None)
        if mgr is None:
            return None, "宿主没有 provider_mgr，取不到 decision 模型"
        try:
            client = mgr.get_model_client(pid, mid, ModelType.DECISION)
        except Exception as exc:
            return None, "%s: %s" % (type(exc).__name__, exc)
        if client is None:
            return None, "模型 %s 不存在，或不是 decision 类型" % key
        if not callable(getattr(client, "choice", None)):
            return None, "模型 %s 不支持 choice()" % key
        return client, ""

    async def _one_call(self, key: str, state_text: str, instructions: str,
                        timeout: float) -> dict:
        """发一次 choice()。返回 {ok, error, probabilities, choice, confidence, usage, model...}。"""
        started = time.time()

        def _fail(message: str, model: str = "") -> dict:
            """失败也要给全形状：decide() / 记录 / 页面都按同一组键取值，缺键会炸。"""
            return {"ok": False, "error": message, "key": key, "model": model,
                    "usage": [], "attempts": 1, "probabilities": {}, "prob_reply": None,
                    "choice": "", "confidence": None,
                    "elapsed": round(time.time() - started, 3)}

        client, why = self._client_for(key)
        if client is None:
            return _fail(why)
        identity = {}
        try:
            actual = getattr(client, "model", None)
            for name in ("provider_id", "model_id", "model_name", "provider_name"):
                value = getattr(actual, name, "") if actual is not None else ""
                value = str(value or "").strip()
                if value:
                    identity[name] = value
        except Exception:
            logger.debug("[multi-decision] 读取 JEV 模型身份失败", exc_info=True)
        model_ref = ("%s:%s" % (identity["provider_id"], identity["model_id"])
                     if identity.get("provider_id") and identity.get("model_id") else key)
        try:
            pending = client.choice(state_text, instructions or None, criteria=dict(CRITERIA))
            if asyncio.iscoroutine(pending):
                resp = await asyncio.wait_for(pending, timeout=timeout)
            else:  # 测试里的假客户端可能直接给结果
                resp = pending
        except asyncio.TimeoutError:
            return _fail("JEV 决策模型超时（%d 秒）" % int(timeout), model_ref)
        except Exception as exc:
            return _fail("%s: %s" % (type(exc).__name__, exc), model_ref)
        usage = []
        try:
            u = getattr(resp, "usage", None)
            tin = max(0, int(getattr(u, "input_tokens", 0) or 0))
            tout = max(0, int(getattr(u, "output_tokens", 0) or 0))
            usage = [{"ts": started, "timestamp_source": "request_start",
                      "model": model_ref, **identity, "input": tin, "output": tout,
                      "cached": 0}]
        except Exception:
            logger.debug("[multi-decision] 读 JEV 用量失败（不虚构一次用量）", exc_info=True)
        probs = getattr(resp, "probabilities", None)
        prob, clean, err = read_reply_probability(probs)
        choice = str(getattr(resp, "choice", "") or "").strip()
        confidence = getattr(resp, "confidence", None)
        out = {
            "ok": not err,
            "error": err,
            "key": key,
            "model": str(getattr(resp, "model", "") or model_ref),
            "model_ref": model_ref,
            "choice": choice,
            "confidence": (float(confidence) if isinstance(confidence, (int, float))
                           and not isinstance(confidence, bool) else None),
            "probabilities": dict(clean or {}),
            "prob_reply": prob,
            "usage": usage,
            "attempts": 1,
            "elapsed": round(time.time() - started, 3),
        }
        # 选项名对不上 / 分布非法时，框架那边已经拦过一次；这里再确认一次，
        # 免得别的 Provider 实现放宽了校验。choice 也不该跑出两档之外。
        if out["ok"] and choice and choice not in JEV_CHOICES:
            out["ok"] = False
            out["error"] = "模型返回的结论不在选项里（%s）" % choice
        return out

    async def _custom_call(self, opt: JevOptions, state_obj: dict,
                           target: dict = None) -> dict:
        """打一次自定义接口。target 给了就用它（多页签），否则用 opt 的老字段。"""
        started = time.time()
        target = target if isinstance(target, dict) else {
            "kind": "custom", "id": "1", "protocol": opt.protocol,
            "base": opt.api_base, "model": opt.api_model, "api_key": opt.api_key,
        }
        protocol = normalize_protocol(target.get("protocol") or opt.protocol)
        base = str(target.get("base") or "")
        api_key = str(target.get("api_key") or "")
        is_openai = protocol == "openai"
        url = api_chat_url(base) if is_openai else api_endpoint(base)
        model_name = str(target.get("model") or "").strip() or DEFAULT_API_MODEL
        ref = "custom:%s" % model_name

        def _fail(message: str, model: str = "", fatal: bool = False) -> dict:
            return {
                "ok": False, "error": str(message or "自定义接口失败"), "key": url,
                "model": model or ref, "model_ref": ref, "choice": "", "confidence": None,
                "probabilities": {}, "prob_reply": None, "usage": [], "attempts": 1,
                "elapsed": round(time.time() - started, 3), "backend": "custom",
                "api_base": url, "api_model": model_name, "fatal": bool(fatal),
            }

        # 地址体检：以前这里只判 https 前缀，账号密码、查询参数、本机 / 内网
        # 地址都能混过去，跟设置页写的规则对不上。现在跟配置体检、列模型共用
        # check_api_base，不合法就当场失败、不发请求（重试也没用，见 fatal）。
        base_ok, base_why = check_api_base(base)
        if not base_ok:
            return _fail("自定义接口地址不合法：%s" % (base_why or "还没填地址"), fatal=True)
        if not api_key.strip():
            return _fail("自定义接口还没填密钥")
        rules = str(opt.prompt or DEFAULT_JEV_PROMPT)[:4000]
        if is_openai:
            # 用户 m08755：阿里这类网关只有 OpenAI 兼容，没有 SystemOne——
            # 就让普通模型按 JSON 回一个概率，判定口径不变。
            payload = {
                "model": model_name,
                "messages": openai_messages(rules, state_obj),
                "temperature": 0,
                "max_tokens": 200,
            }
        else:
            # 用户 m09981：真正的 SystemOne 要求 questions 是「题名 → 题目」的字典，
            # 每道题只含 {type, instructions, criteria, label}（见 astrbot-plugin-jev
            # 的 jev/policy.py:78-115）。以前发的是列表，真接口一律回 400。
            payload = {
                "model": model_name,
                "state": dict(state_obj or {}),
                "questions": {
                    CUSTOM_QUESTION_NAME: {
                        "type": "noul",
                        "instructions": rules,
                        "criteria": dict(CUSTOM_CRITERIA),
                        "label": CUSTOM_QUESTION_LABEL,
                    },
                },
            }
        try:
            status, raw = await asyncio.wait_for(
                asyncio.to_thread(_http_post_json, url, payload, api_key, opt.timeout),
                timeout=float(opt.timeout) + 5.0,
            )
        except asyncio.TimeoutError:
            return _fail("自定义接口超时（%d 秒）" % int(opt.timeout))
        except urllib.error.HTTPError as exc:  # 4xx/5xx：URL 也有，只是状态码不对
            detail = ""
            try:
                detail = exc.read(400).decode("utf-8", "replace").strip()
            except Exception:
                detail = ""
            return _fail("自定义接口返回 HTTP %s%s" % (
                getattr(exc, "code", "?"), ("：" + detail[:160]) if detail else ""))
        except urllib.error.URLError as exc:
            return _fail("自定义接口连不上（%s）" % (getattr(exc, "reason", exc),))
        except Exception as exc:
            return _fail("%s: %s" % (type(exc).__name__, exc))
        if int(status) != 200:
            return _fail("自定义接口返回 HTTP %s" % status)
        if len(raw) > MAX_RESPONSE_BYTES:
            return _fail("自定义接口响应过大（超过 64KB）")
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return _fail("自定义接口返回的不是 JSON")
        if is_openai:
            prob, model, usage_raw, err = parse_openai_answer(body)
        else:
            prob, model, usage_raw, err = read_custom_answer(body, CUSTOM_QUESTION_NAME)
        if err:
            return _fail(err, model)
        usage = []
        if usage_raw:
            usage = [{"ts": started, "timestamp_source": "request_start",
                      "model": ref, "input": int(usage_raw.get("input_tokens", 0) or 0),
                      "output": int(usage_raw.get("output_tokens", 0) or 0), "cached": 0}]
        return {
            "ok": True,
            "error": "",
            "key": url,
            "model": model or ref,
            "model_ref": ref,
            "choice": "reply" if prob > 0.5 else "no_reply",
            "confidence": None,
            "probabilities": {"reply": prob, "no_reply": 1.0 - prob},
            "prob_reply": prob,
            "usage": usage,
            "attempts": 1,
            "elapsed": round(time.time() - started, 3),
            "backend": "custom",
            "api_base": url,
            "api_model": model_name,
        }

    @staticmethod
    def _resolve_target(opt: JevOptions, key: str) -> tuple:
        """模型键 → 调用目标。custom:<页签> 走第三方，其余 provider:model 走宿主。

        用户 m09981：两条路现在共用同一份模型下拉，键必须能自我说明，
        否则「选的是哪个」只能靠另一个开关猜。
        """
        text = str(key or "").strip()
        if text.startswith("custom:"):
            eid = normalize_api_id(text.split(":", 1)[1])
            ep = opt.endpoint(eid)
            if not ep and opt.is_custom() and not opt.apis:
                # 老配置只有一份接口（没有页签列表）：custom:1 就当那份。
                return {"kind": "custom", "id": "1", "protocol": opt.protocol,
                        "base": opt.api_base, "model": opt.api_model,
                        "api_key": opt.api_key}, ""
            if not ep:
                return None, "自定义接口 %s 不存在（可能已经被删掉）" % eid
            if not str(ep.get("base") or "").strip():
                return None, "自定义接口 %s 还没填地址" % eid
            if not str(ep.get("model") or "").strip():
                return None, "自定义接口 %s 还没填模型名" % eid
            return {"kind": "custom", "id": eid,
                    "protocol": ep.get("protocol"), "base": ep.get("base"),
                    "model": ep.get("model"),
                    "api_key": str(ep.get("key") or ep.get("api_key") or "")}, ""
        if not text:
            if opt.is_custom():
                # 老配置（backend=custom + 三个 api_* 键）没有 custom:id，照旧走第 1 页。
                return {"kind": "custom", "id": "1", "protocol": opt.protocol,
                        "base": opt.api_base, "model": opt.api_model,
                        "api_key": opt.api_key}, ""
            return None, "没有选择 JEV 决策模型"
        pid, mid = split_model_key(text)
        if not pid or not mid:
            return None, "模型 %s 不完整（要 provider:model）" % text
        return {"kind": "host", "key": text, "provider": pid, "model": mid}, ""

    async def _chain_custom(self, opt: JevOptions, state_obj: dict,
                            target: dict = None) -> dict:
        """自定义接口这条路：重试（没有「备用模型」概念，备用是另一个键的事）。"""
        attempts = 0
        tgt = target if isinstance(target, dict) else {
            "kind": "custom", "id": "1", "protocol": opt.protocol,
            "base": opt.api_base, "model": opt.api_model, "api_key": opt.api_key}
        _first_url = endpoint_url(tgt.get("protocol") or opt.protocol, tgt.get("base") or "")
        last = {"ok": False, "error": "自定义接口还没配置好", "key": _first_url,
                "model": "", "model_ref": "", "usage": [], "attempts": 0, "elapsed": 0.0,
                "probabilities": {}, "prob_reply": None, "choice": "", "confidence": None,
                "via": "custom", "backend": "custom",
                "api_base": _first_url, "api_model": str(tgt.get("model") or "")}
        tries = max(1, int(opt.retry) + 1)
        for t in range(tries):
            attempts += 1
            got = await self._custom_call(opt, state_obj, tgt)
            got["attempts"] = attempts
            got["via"] = "custom"
            if got.get("ok"):
                self._fail_streak = 0
                self._fallback_until = 0.0
                self._last_error = ""
                return got
            last = got
            self._last_error = str(got.get("error") or "")[:300]
            logger.warning("[multi-decision] JEV 自定义接口失败（第 %d 次）：%s",
                           attempts, self._last_error[:160])
            if got.get("fatal"):
                # 地址本身不合法：重试多少次都是同一个结果，别白等 retry_delay。
                break
            if t < tries - 1 and opt.retry_delay > 0:
                await asyncio.sleep(opt.retry_delay)
        self._fail_streak += 1
        last["attempts"] = attempts
        return last

    async def _chain(self, state_text: str, instructions: str, opt: JevOptions,
                     state_obj: dict = None) -> dict:
        """主模型 → 重试 → 备用模型；跟普通决策同一套重试 / 冷却参数。

        用户 m09981：主模型与备用各自可以是宿主模型或某一页自定义接口，
        所以这里按目标类型分别调用，不再在最外层分叉。
        """
        now = time.time()
        # 老配置（backend=custom，没有页签列表）只有那一份接口，模型键不参与选路。
        legacy = opt.is_custom() and not opt.apis
        main_key = "" if legacy else opt.model
        fb_key = "" if legacy else opt.fallback
        order = [("main", main_key)]
        if fb_key:
            order.append(("fallback", fb_key))
        if fb_key and self._fallback_until > now:
            order = [("fallback", fb_key), ("main", main_key)]
        last = {"ok": False, "error": "没有可用的 JEV 决策模型", "key": main_key,
                "usage": [], "attempts": 0, "elapsed": 0.0, "probabilities": {},
                "prob_reply": None, "choice": "", "confidence": None, "via": ""}
        attempts = 0
        for kind, key in order:
            if not str(key or "").strip() and not (kind == "main" and opt.is_custom()):
                continue
            target, why = self._resolve_target(opt, key)
            if target is None:
                last = dict(last, error=why, key=str(key or ""))
                self._last_error = str(why)[:300]
                continue
            tries = (max(0, int(opt.retry)) + 1) if kind == "main" else 1
            for t in range(tries):
                attempts += 1
                if target.get("kind") == "custom":
                    got = await self._custom_call(opt, state_obj or {}, target)
                else:
                    got = await self._one_call(target["key"], state_text, instructions,
                                               opt.timeout)
                got["attempts"] = attempts
                got["via"] = kind
                if got.get("ok"):
                    self._fail_streak = 0
                    self._fallback_until = 0.0
                    self._last_error = ""
                    return got
                last = got
                self._last_error = str(got.get("error") or "")[:300]
                logger.warning("[multi-decision] JEV 判定失败（%s，第 %d 次）：%s",
                               key or "未选模型", attempts, self._last_error[:160])
                if got.get("fatal"):
                    # 地址本身不合法：重试多少次都是同一个结果，别白等 retry_delay。
                    break
                if t < tries - 1 and opt.retry_delay > 0:
                    await asyncio.sleep(opt.retry_delay)
            if kind == "main":
                self._fail_streak += 1
                if self._fail_streak >= max(1, int(opt.fail_switch)):
                    self._fallback_until = time.time() + float(opt.cooldown)
        last["attempts"] = attempts
        return last

    # ---------- 对外入口 ----------

    def _reason_text(self, prob, clean, zone, lower, upper, action, delegated=None) -> str:
        head = "回复 %s · 不回复 %s" % (format_prob(prob), format_prob(1.0 - prob))
        if zone == "reply":
            return head + "｜回复概率高于上限 %s，判要回" % format_prob(upper)
        if zone == "no_reply":
            return head + "｜回复概率低于下限 %s，判不回" % format_prob(lower)
        label = {"reply": "回复", "no_reply": "不回复", "decision": "交给决策模型"}.get(action, action)
        text = head + "｜命中不确定区间 %s～%s，按设置%s" % (
            format_prob(lower), format_prob(upper), label)
        if action == "decision" and isinstance(delegated, dict):
            text += " → " + ("回复" if delegated.get("reply") else "不回复")
            extra = str(delegated.get("reason") or "").strip()
            if extra and delegated.get("ok"):
                text += "（%s）" % extra[:80]
        return text

    async def decide(self, sid: str, messages, opt: JevOptions, *, phase=None,
                     normal_opt=None, images=None, allow_transfer: bool = True) -> dict:
        """跑一次 JEV 判定。返回结构与 DecisionPhase.decide() 同形，另加 jev_* 键。"""
        started = time.time()
        dp = phase if phase is not None else getattr(self.plugin, "decision_phase", None)
        layouts = getattr(self.plugin, "prompt_layouts", None)
        persona = ""
        if opt.persona and dp is not None:
            try:
                persona = await dp.persona_text(opt.persona)
            except Exception:
                logger.exception("[multi-decision] 取 JEV 人格失败，按无人格处理")
        dyn = ""
        dyn_parts = []
        system_parts = []
        # 用户 m09981：主模型与备用各自可能是自定义接口的某一页，所以这里按
        # 「这一轮会不会用到第三方」把题面与 state 都备好；真正走哪条由 _chain 定。
        want_custom = (opt.is_custom()
                       or str(opt.model).strip().startswith("custom:")
                       or str(opt.fallback).strip().startswith("custom:"))
        # 2026-11-08（J）：默认判断规则里指代 bot 的地方写成{{bot}}，这里一次性换成这个会话
        # 的昵称；写回 opt 之后，下面骨架、自定义接口两条路拿到的都是填好的那份。
        opt.prompt = self._fill_bot(str(opt.prompt or DEFAULT_JEV_PROMPT), sid)
        rules_text = ""
        persona_text = ""
        context_parts = []
        if layouts is not None:
            compiled = await layouts.assemble_jev(sid, opt, persona=persona)
            parts = list(compiled.get("parts") or [])
            dyn_parts = [{"name": str(p.get("name") or ""), "label": str(p.get("label") or ""),
                          "text": str(p.get("text") or "")}
                         for p in parts
                         if p.get("inject") and str(p.get("text") or "").strip()]
            dyn = "\n\n".join(p["text"] for p in dyn_parts)
            system_parts = [p["text"] for p in parts if not p.get("dynamic")]
            instructions = compiled["system"]
            # 自定义接口要把「题面」和「上下文」分开送：题面只放判断规则，
            # 人设 / 身份 / 实时信息改走 state 字段（见 _custom_state）。
            fixed = [p for p in parts if not p.get("dynamic")]
            kind_of = lambda p: str(p.get("type") or p.get("name") or "")  # noqa: E731
            rules_text = "\n\n".join(str(p.get("text") or "") for p in fixed
                                     if kind_of(p) in ("text", "custom"))
            persona_text = "\n\n".join(str(p.get("text") or "") for p in fixed
                                       if kind_of(p) == "persona")
            context_parts = [str(p.get("text") or "") for p in fixed
                             if kind_of(p) not in ("text", "custom", "persona")]
        else:  # 没有提示词层时退回骨架，保证还能判
            system_parts = ["【JEV 判断规则】\n" + (opt.prompt or DEFAULT_JEV_PROMPT)]
            if persona:
                system_parts.insert(0, persona)
            instructions = "\n\n".join(system_parts)
            rules_text = opt.prompt or DEFAULT_JEV_PROMPT
            persona_text = str(persona or "")
        if want_custom:
            instructions = rules_text.strip() or (opt.prompt or DEFAULT_JEV_PROMPT)
        body = dp.transcript(messages, sid) if dp is not None else ""
        state_text = body or "（这段时间没人说话）"
        if dyn:
            state_text = state_text + "\n\n" + dyn
        state_obj = None
        if want_custom:
            state_obj = self._custom_state(persona_text or persona, body, dyn, context_parts)
        got = await self._chain(state_text, instructions, opt, state_obj)
        backend = "custom" if str(got.get("backend") or "") == "custom" else "framework"
        lower, upper = opt.bounds()
        usage = list(got.get("usage") or [])
        prob = got.get("prob_reply")
        clean = dict(got.get("probabilities") or {})
        zone = ""
        action = "no_reply"
        delegated = None
        reply = False
        reason = ""
        transfer_skipped = False
        error = str(got.get("error") or "")
        ok = bool(got.get("ok"))
        model_text = str(got.get("model") or got.get("key") or "")
        via = str(got.get("via") or "")
        attempts = int(got.get("attempts") or 0)
        if ok and prob is not None:
            zone = zone_of(float(prob), lower, upper)
            if zone == "reply":
                reply = True
            elif zone == "no_reply":
                reply = False
            else:
                action = opt.action if opt.action in _actions else "no_reply"
                if action == "reply":
                    reply = True
                elif action == "no_reply":
                    reply = False
                elif not allow_transfer:
                    # /jev-test 这类试跑：只把结论回给页面，不额外调一次普通决策。
                    # 仍然算「按设置交给决策模型」，只是这一轮没有真的转交。
                    transfer_skipped = True
                    reply = False
                else:
                    # 串行转交一次普通决策：同一批冻结输入，不重新取窗口、不再防抖。
                    delegated = await self._transfer(sid, messages, normal_opt or opt,
                                                     images, allow_transfer)
                    reply = bool(delegated.get("reply"))
                    if delegated.get("usage"):
                        usage.extend(delegated.get("usage") or [])
                    if delegated.get("model"):
                        model_text = ("%s → %s" % (model_text, delegated["model"])
                                      if model_text else str(delegated["model"]))
                    if delegated.get("via"):
                        via = ("%s → %s" % (via, delegated["via"]) if via else str(delegated["via"]))
                    attempts += int(delegated.get("attempts") or 0)
                    if not delegated.get("ok"):
                        ok = False
                        error = str(delegated.get("error") or "转交普通决策失败")
            reason = self._reason_text(prob, clean, zone, lower, upper, action, delegated)
            if transfer_skipped:
                reason += "（试跑，不转交）"
        elif ok:
            ok = False
            error = error or "模型没有给出可用的回复概率"
        if not ok:
            # 转交失败按**普通决策**自己的失败动作处理（用户 m06706：不能静默变成
            # 「按不确定设置」）；JEV 自己判失败才用 JEV 的失败动作。
            fail_src = (normal_opt if delegated is not None and not delegated.get("ok")
                        else opt)
            reply = (getattr(fail_src, "fail_action", "allow") != "block")
            if not reason:
                reason = "JEV 判定失败：%s" % (error or "未知原因")
        out = {
            "ok": ok,
            "reply": bool(reply),
            "reason": reason,
            "say": "",
            "raw": str(got.get("raw") or "")[:4000],
            "error": error,
            "model": model_text,
            "via": via,
            "attempts": attempts,
            "elapsed": round(time.time() - started, 3),
            "input_tokens": sum(int(item.get("input") or 0) for item in usage),
            "output_tokens": sum(int(item.get("output") or 0) for item in usage),
            "cached_tokens": sum(int(item.get("cached") or 0) for item in usage),
            "usage": usage,
            "tools_used": [],
            "system_parts": system_parts,
            "transcript": body,
            "user_prompt": state_text,
            "dynamic_text": dyn,
            "dynamic_parts": dyn_parts,
            "images": [] if delegated is None else list(delegated.get("images") or []),
            "image_count": 0 if delegated is None else int(delegated.get("image_count") or 0),
            "images_fallback": bool(delegated.get("images_fallback")) if delegated else False,
            "images_fallback_error": (str(delegated.get("images_fallback_error") or "")[:300]
                                      if delegated else ""),
            "images_fallback_attempts": (int(delegated.get("images_fallback_attempts") or 0)
                                         if delegated else 0),
            "upstream_busy": bool(delegated.get("upstream_busy")) if delegated else False,
            "sid": sid,
            # ---- JEV 自己的结论（结构化存，改区间不回写旧记录）
            "decision_mode": "jev",
            "jev": {
                "probabilities": dict(clean),
                "prob_reply": (float(prob) if prob is not None else None),
                "prob_reply_display": (format_prob(prob) if prob is not None else ""),
                "lower": lower,
                "upper": upper,
                "zone": zone,
                "action": action,
                "choice": str(got.get("choice") or ""),
                "confidence": got.get("confidence"),
                "model": str(got.get("model") or ""),
                "model_key": str(got.get("key") or ""),
                "backend": backend,
                "custom_state": (dict(state_obj or {}) if backend == "custom" else {}),
                # 用户 m08755 衍生：记录里那条「接口」要写真正的目标路径，
                # OpenAI 兼容协议打的是 /chat/completions，不是 /v1/systemone。
                "api_base": str(got.get("api_base") or "") if backend == "custom" else "",
                "api_model": str(got.get("api_model") or "") if backend == "custom" else "",
                "criteria": dict(CUSTOM_CRITERIA if backend == "custom" else CRITERIA),
                "instructions": instructions[:20000],
                "state": state_text[:40000],
                "usage": [dict(item) for item in (got.get("usage") or [])],
                "error": error if not ok else "",
                "attempts": int(got.get("attempts") or 0),
                "elapsed": float(got.get("elapsed") or 0.0),
                "transferred": bool(delegated),
                "transfer_skipped": transfer_skipped,
                "transfer_ok": (bool(delegated.get("ok")) if delegated else None),
                "transfer_error": (str(delegated.get("error") or "")[:300] if delegated else ""),
            },
        }
        if delegated is not None:
            out["jev_transfer"] = {
                "reply": bool(delegated.get("reply")),
                "ok": bool(delegated.get("ok")),
                "failed": bool(delegated.get("failed")),
                "reason": str(delegated.get("reason") or "")[:200],
                "model": str(delegated.get("model") or ""),
                "error": str(delegated.get("error") or "")[:300],
            }
        if not out["ok"]:
            out["failed"] = True
        try:
            from .decision_phase import _comparison_snapshot  # 同一套前缀签名
            out["chat_comparison"] = _comparison_snapshot(
                [{"role": "user", "content": state_text}], "")
        except Exception:
            out["chat_comparison"] = {}
        return out

    @staticmethod
    def _custom_state(persona_text: str, body: str, dyn: str, context_parts) -> dict:
        """把这一轮拆成 SystemOne 的四个 state 字段。

        target_message 取窗口里最后一条非空行（这一轮真正要判的那句），
        recent_conversation 放整段窗口；人设单独一段，别混进聊天记录。
        """
        text = str(body or "").strip()
        target = ""
        for line in reversed([x.strip() for x in text.splitlines()]):
            if line:
                target = line[:2000]
                break
        recent = []
        for item in context_parts or []:
            chunk = str(item or "").strip()
            if chunk:
                recent.append(chunk[:2000])
        if str(dyn or "").strip():
            recent.append(str(dyn).strip()[:2000])
        if text:
            recent.append(text[:6000])
        return {
            "bot_persona": str(persona_text or "").strip()[:4000],
            "target_message": target,
            "recent_conversation": recent,
            "candidate_reply": "",
        }

    async def _transfer(self, sid: str, messages, normal_opt, images, allow: bool) -> dict:
        """把这一轮交给普通决策再判一次。失败按普通决策自己的失败动作处理。"""
        if not allow:
            return {"ok": False, "reply": True, "error": "本轮不允许转交普通决策",
                    "usage": [], "model": "", "attempts": 0}
        dp = getattr(self.plugin, "decision_phase", None)
        if dp is None or normal_opt is None:
            return {"ok": False, "reply": True, "error": "普通决策不可用，无法转交",
                    "usage": [], "model": "", "attempts": 0}
        try:
            return await dp.decide(sid, messages, normal_opt, images=images or [])
        except Exception as exc:
            logger.exception("[multi-decision] 转交普通决策失败")
            return {"ok": False, "reply": True, "failed": True,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "usage": [], "model": "", "attempts": 0}


def models_of_type(kind: str) -> list:
    """从 system_config.json 里列出某一类模型（decision 只列启用提供商的）。"""
    import os

    out = []
    try:
        from core.utils.path_utils import get_data_path  # type: ignore

        path = os.path.join(str(get_data_path()), "config", "system_config.json")
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        logger.debug("[multi-decision] 读框架模型清单失败（%s）", kind, exc_info=True)
        return out
    if not isinstance(data, dict):
        return out
    provs = data.get("providers")
    if not isinstance(provs, dict):
        return out
    for pid, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        if str(prov.get("status") or "active").strip().lower() not in ("", "active", "enabled", "on"):
            continue
        pname = str(prov.get("name") or pid)
        mcfg = prov.get("model_config")
        mcfg = mcfg if isinstance(mcfg, dict) else {}
        group = mcfg.get(kind)
        if not isinstance(group, dict):
            continue
        for mid, item in group.items():
            if not isinstance(item, dict):
                continue
            out.append({"value": "%s:%s" % (pid, mid),
                        "label": "%s / %s" % (pname, str(item.get("model_name") or mid)),
                        "kind": kind})
    return out
