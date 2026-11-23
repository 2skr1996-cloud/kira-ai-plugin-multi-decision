"""多样决策 —— KiraAI 群聊触发判定与上下文窗口。

决策模块接管群聊的「要不要回 / 什么时候回」；
窗口模块接管上下文历史（自己存、增量合并、真空闲才裁）。
WebUI 页面挂在左侧边栏「多样决策」：群聊 / 私聊 / 上下文（观察窗）。
设置单独一页（/settings），从日志页右上角的齿轮进去。

装饰器必须写在本文件里：框架靠模块所在目录的 manifest.json 反查 plugin_id，
子模块里定义的钩子会找不到插件身份。
"""

from __future__ import annotations

import asyncio
import inspect
import contextvars
import copy
import ctypes
import os
import json
import mimetypes
import re
import time
from pathlib import Path

from core.plugin import BasePlugin, logger, on, Priority, register
from core.agent.tool import ToolResult

from .decision.engine import DecisionEngine, _norm_phrase, _quote_body_of
from .decision.output_guard import dedupe_reply, wrap_plain_reply
from .state.adaptive import AdaptiveStats
from .state.aliases import Aliases
from .state.user_blacklist import UserBlacklist
from .state.band_stats import BandStats
from .state.band_rhythm import BandRhythm
from .state.band_tune import BandTuner
from .state import compat
from .state.decision_phase import (
    DEFAULT_CHAT_RULES,
    DEFAULT_DECISION_PROMPT,
    TOOL_HEAD,
    DecisionPhase,
    options_from_config,
)
from .state.decision_log import DecisionLog
# 用户 m06706：JEV 决策（专用 decision 类模型走 choice()），跟普通决策并列的一条链。
from .state.jev_phase import DEFAULT_API_BASE as DEFAULT_JEV_API_BASE
from .state.jev_phase import DEFAULT_API_MODEL as DEFAULT_JEV_API_MODEL
from .state.jev_phase import DEFAULT_LOWER as DEFAULT_JEV_LOWER
from .state.jev_phase import DEFAULT_UPPER as DEFAULT_JEV_UPPER
from .state.jev_phase import DEFAULT_JEV_PROMPT as DEFAULT_JEV_PROMPT
from .state.jev_phase import JevPhase
from .state.jev_phase import api_chat_url as jev_api_chat_url
from .state.jev_phase import api_endpoint as jev_api_endpoint
from .state.jev_phase import check_api_base as jev_check_api_base
from .state.jev_phase import clear_secret as jev_clear_secret
from .state.jev_phase import endpoint_url as jev_endpoint_url
from .state.jev_phase import list_api_models as jev_list_api_models
from .state.jev_phase import load_secret as jev_load_secret
from .state.jev_phase import load_secrets as jev_load_secrets
from .state.link_title import LinkTitles
from .state.github_repo import GitHubRepos
from .state.jev_phase import make_api as jev_make_api
from .state.jev_phase import normalize_api_id as normalize_jev_api_id
from .state.jev_phase import parse_apis as jev_parse_apis
from .state.jev_phase import mask_key as jev_mask_key
from .state.jev_phase import normalize_action as normalize_jev_action
from .state.jev_phase import normalize_backend as normalize_jev_backend
from .state.jev_phase import normalize_protocol as normalize_jev_protocol
from .state.jev_phase import options_from_config as jev_options_from_config
from .state.jev_phase import save_secret as jev_save_secret
from .decision.forward_fetch import (
    display_proof_for_mid as forward_display_proof_for_mid,
    fetch_forward_items,
    media_for_mid as forward_media_for_mid,
    media_db_clear as forward_media_clear,
    media_db_gc as forward_media_gc,
    media_db_stats as forward_media_stats,
    set_media_db_keep_days_fn as set_forward_media_keep_days_fn,
    set_media_db_limit_fn as set_forward_media_limit_fn,
)
from .state.display_projection import (
    content_text,
    message_id as display_message_id,
    project_items,
    raw_forward_proof_items,
    read_native_rows,
)
from .state.hidden import Hidden
from .state.overrides import Overrides
from .state.pricing import PRICE_UNIT, Pricing, _CURRENCY, _UNKNOWN_NAMES
from .state.reply_prompt import PERSONA_NAME, ReplyPrompt, apply_reply_tools
from .state.prompt_layout import PromptLayouts
from .state.provider_bind import ProviderBindings
from .state.request_log import RequestLog, _safe_sid, payload_metrics
from .state.retry_history import RetryHistory
from .state.round_stats import merge_rounds
from .state.session_registry import SessionRegistry
from .state.sids import normalize_sid
from .state.state_store import StateStore
from .state.summaries import Summaries
from .state.tracer import Tracer, infer_cached_steps, infer_gate
from .window.manager import (
    WindowManager,
    view_of,
    media_list,
    media_render_begin,
    media_render_resume,
    media_render_state,
    public_media,
    _media_by_ids,
    original_media_for_mid,
    media_cache_by_digest,
    _assistant_prefix,
    _sniff_image_mime,
    _media_cache_root,
    set_forward_media_fn,
    forward_model_refs,
    set_forward_policy_fn,
    round_kind,
    trim_owner_of,
    prune_other_media,
    clear_other_media,
    _owners_remember,
)
from .window.split import regroup_images, expand_packed_messages
from .window.trim import count_messages, count_user_messages

PLUGIN_ID = "kira-ai-plugin-multi-decision"

#: 会话 id 的形状：框架里是 "gm:1145141974" / "dm:123" 这类，只允许字母数字与 :_- .
#: 删会话（purge_session）会拿它拼目录名再 rmtree，所以入口必须挡掉 ".." 与路径分隔符，
#: 否则一个构造过的 sid 就能删到插件数据目录外面去（审查报告的高危项）。
_SID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,80}$")


def _quote_media_by_mid(mid: str, raw_sid: str) -> list:
    """按 message_id 反查「那条消息的图」，给引用块用（用户 m12256）。

    请求快照里带着 media 时用不上这里；被引用那条早就滑出窗口、当轮快照也没
    留下图时，只能顺着 message_id 去 session_media / 插件索引 / 归档库里认回来，
    再过一遍 public_media 把副本落进插件缓存。
    """
    out: list = []
    try:
        refs = _media_by_ids("[message_id: %s]" % str(mid or ""), raw_sid)
    except Exception:
        refs = []
    for ref in (refs or [])[:6]:
        if not isinstance(ref, dict):
            continue
        try:
            got = public_media(str(ref.get("path") or ""), str(ref.get("mime") or ""), raw_sid)
        except Exception:
            got = {}
        if got and (got.get("path") or got.get("url")):
            out.append(got)
    return out


def _forward_media_attach(messages, raw_sid: str = "") -> list:
    """老消息里的转发图（用户 m25038 第 4 项）：窗口里存的是当时的文本快照，
    media 早就定死了，这里按正文里的 [message_id: X] 现查一次插件缓存再挂上。
    """
    try:
        from .decision.forward_fetch import media_for_text as _fwd_media_of
    except Exception:
        return messages
    out = []
    for m in messages or []:
        try:
            if isinstance(m, dict) and not (m.get("media") or []) and "[Forward" in str(m.get("text") or ""):
                med = []
                for ref in (_fwd_media_of(str(m.get("text") or "")) or [])[:4096]:
                    try:
                        got = public_media(str(ref.get("path") or ""), str(ref.get("mime") or ""), raw_sid)
                    except Exception:
                        got = {}
                    if got and (got.get("path") or got.get("url")):
                        med.append(got)
                if med:
                    m = dict(m)
                    m["media"] = med
        except Exception:
            pass
        out.append(m)
    return out


def _sid_ok(sid: str) -> bool:
    """sid 能不能安全地拿去拼路径。空串、含路径分隔符、含 ".."、超长一律否。"""
    s = str(sid or "").strip()
    if not s or not _SID_RE.match(s):
        return False
    if "/" in s or "\\" in s or ".." in s:
        return False
    return True


#: 测试窗只喂最近这么多条（用户 m01977）。
TEST_WINDOW_LIMIT = 10


def _test_window_messages(messages, limit: int = TEST_WINDOW_LIMIT) -> list:
    """测试窗只留「用户发的纯文字」，其余全过滤掉（用户 m01977）。

    测试按钮只是看看模型怎么判，喂整份窗口（bot 自己的话、工具往返、图片、
    表情、转发卡片）反而看不清是哪句话影响了判断。所以这里只认 role=user
    且正文是纯文字的消息，再取最近 limit 条。

    注意：真实决策链不走这里（decision/engine.py 自己取窗口），只影响
    「实时窗」显示与三个测试按钮。
    """
    out = []
    for m in messages or []:
        try:
            if isinstance(m, dict):
                role = str(m.get("role") or "").strip().lower()
                content = m.get("content")
                calls = m.get("tool_calls")
                media = m.get("media") or m.get("images")
            else:
                role = str(getattr(m, "role", "") or "").strip().lower()
                content = getattr(m, "content", None)
                calls = getattr(m, "tool_calls", None)
                media = getattr(m, "media", None) or getattr(m, "images", None)
        except Exception:
            continue
        if role != "user" or calls or media:
            continue
        if isinstance(content, str):
            text = content
        elif isinstance(content, (list, tuple)):
            # 多段内容里只要有一段不是文字（图 / 附件），整条跳过。
            parts = []
            pure = True
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and str(part.get("type") or "text") == "text":
                    parts.append(str(part.get("text") or ""))
                else:
                    pure = False
                    break
            if not pure:
                continue
            text = "\n".join(x for x in parts if x)
        else:
            continue
        text = text.strip()
        if not text:
            continue
        # 重新拼成纯文字消息：窗口里那份可能还挂着图 / 附件，不能原样带下去。
        out.append({"role": "user", "content": text})
    if limit and limit > 0 and len(out) > limit:
        out = out[-limit:]
    return out


#: 左栏竖杠判「卡住」的秒数上限。看门狗（schema 的 watchdog_seconds，默认 180 秒）要等满
#: 才强制释放，但用户 m04917 第 2 条要求「出错时立刻变色」——一轮投递后几十秒没动静就已经不正常，
#: 所以指示灯先一步转红（只影响展示，不影响看门狗释放时机与重试逻辑）。
#: 实际用 self._stall_seconds(sid)：看门狗还是 180 秒时就是这里的 60 秒；用户把看门狗
#: 调小（30-600）时按 watchdog_seconds/3 跟着缩，保证红灯不会比看门狗还晚亮。
STALL_SECONDS = 60

#: /media 单张图片的读取上限。以前直接 read_bytes() 把整个文件读进内存、且没有上限，
#: 一张超大图就能把内存与事件循环一起拖住（审查报告）。超过这个大小回 413，
#: 页面会按「图片加载失败」画占位，不影响其它记录。
MAX_MEDIA_BYTES = 32 * 1024 * 1024

class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """Windows 的 PROCESS_MEMORY_COUNTERS，只用到其中三个数。"""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _framework_bot_identity() -> dict:
    """从框架的适配器配置里读 bot 自己的账号信息（QQ 号 + 适配器显示名）。

    v3.11：设置页不再要求手填 QQ 号——框架 data/config/system_config.json 的
    adapters.*.config.bot_pid 就是 bot 的 QQ 号，直接读过来用。读不到就返回空，
    前端退回首字圆头像，不影响任何判定逻辑。
    """
    try:
        from core.utils.path_utils import get_data_path
        path = os.path.join(str(get_data_path()), "config", "system_config.json")
    except Exception:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            conf = json.load(f)
    except Exception:
        return {}
    adapters = (conf or {}).get("adapters") or {}
    if not isinstance(adapters, dict):
        return {}
    for _aid, ad in adapters.items():
        if not isinstance(ad, dict) or ad.get("enabled") is False:
            continue
        pid = str((ad.get("config") or {}).get("bot_pid") or "").strip()
        if not pid:
            continue
        return {
            "qq": pid,
            "name": str(ad.get("name") or "").strip(),
            "platform": str(ad.get("platform") or "").strip(),
        }
    return {}


_DISK_CACHE: dict = {"t": 0.0, "v": {}}


def _disk_usage(ttl: float = 60.0) -> dict:
    """插件自己写出来的日志/缓存占了多少磁盘（用户 m30009 图5：要目录占用，不是进程内存）。

    用户第 4 条：概览卡已经改用缓存表（_mem_usage）的数字，这里只当兜底——它数的是
    目录实际占用，会把转发图 fw_ / 推文图 tw_ / 未归属的图全算进 media_cache，和
    缓存记录页的口径对不上，所以只在 _mem_usage 出错时才用。

    v4.88：结果缓存 60 秒。这个函数要把整个插件数据目录走一遍（线上 2572 个文件 /
    1.2 GB），而 /overview 被前端 5 秒轮询一次——以前每 5 秒就同步走一遍盘，
    实测 200ms 上下，全卡在事件循环里，页面和收发消息都跟着顿一下。
    占用不会秒级变化，60 秒足够；返回的是副本，调用方随便改。
    """
    now = time.time()
    cached = _DISK_CACHE.get("v") or {}
    if cached and (now - float(_DISK_CACHE.get("t") or 0.0)) < ttl:
        return {"total_mb": cached.get("total_mb", 0.0),
                "parts": [dict(p) for p in (cached.get("parts") or [])]}
    try:
        from core.utils.path_utils import get_data_path
        root = os.path.join(str(get_data_path()), "plugin_data", PLUGIN_ID)
    except Exception:
        return {}
    if not os.path.isdir(root):
        return {"total_mb": 0.0, "parts": []}
    parts = []
    total = 0
    try:
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            size = 0
            # v4.85（用户 m10425 第 5 条）：顺便数一份文件个数——概览卡要照缓存记录那样列「数量」，
            # 反正这里本来就要把每个目录走一遍，多记一个计数不额外读盘。
            nfiles = 0
            if os.path.isdir(p):
                # v4.88：用 os.scandir 递归，entry.stat() 是枚举目录时顺带拿到的，
                # 比 os.walk + os.path.getsize 快两个数量级——Windows 上按路径 stat
                # 每次约 2ms，2572 个文件就是 5 秒，而这里只要几十毫秒。
                stack = [p]
                while stack:
                    cur = stack.pop()
                    try:
                        with os.scandir(cur) as it:
                            for ent in it:
                                try:
                                    if ent.is_dir(follow_symlinks=False):
                                        stack.append(ent.path)
                                        continue
                                    size += ent.stat(follow_symlinks=False).st_size
                                    nfiles += 1
                                except OSError:
                                    pass
                    except OSError:
                        pass
            else:
                try:
                    size = os.stat(p).st_size
                    nfiles = 1
                except OSError:
                    size = 0
            total += size
            # v3.72（用户 m24260 第 11 条）：也带上字节数——几十 KB 的目录（窗口快照）
            # 四舍五入到一位小数会变成 0.0 MB，前端要靠 bytes 显示成 KB。
            parts.append({"name": name, "mb": round(size / 1048576.0, 1), "bytes": size, "files": nfiles})
    except Exception:
        return {}
    parts.sort(key=lambda x: -x.get("bytes", 0))
    result = {"total_mb": round(total / 1048576.0, 1), "parts": parts[:8]}
    _DISK_CACHE["t"] = now
    _DISK_CACHE["v"] = result
    return {"total_mb": result["total_mb"], "parts": [dict(p) for p in result["parts"]]}


#: 缓存表（_cache_kinds）的行 → 概览卡片 parts 的名字。
#: 前端 web/index.html 的 MEM_GROUPS 按这几个名字分组（图片缓存 / 快照 / 窗口快照 /
#: 消息日志 / 会话统计 / 摘要），名字不能改；没列在这里的行（未归属图片、其他图片
#: 缓存、转发图片、转发原图记录、自动重试记录、私聊上下文存档）前端会并进「其他」。
_MEM_PART_NAMES = {
    "media": "media_cache",
    "requests": "requests",
    # 2026-11-18（用户 m13966）：消息日志和会话统计合成一行（logs），
    # 用量账本单独一行（ledger）。
    "logs": "logs",
    "ledger": "ledger",
    "summaries": "summaries",
}


def _mem_parts_from_kinds(kinds) -> list:
    """把缓存表的行翻成概览卡片的 parts（名字按 _MEM_PART_NAMES 映射）。

    用户第 4 条：概览卡和缓存记录页必须说同一件事，所以这里一个字节都不自己算，
    直接照抄缓存表（_cache_kinds）的 count / bytes——两边永远同一口径。
    """
    parts = []
    for row in kinds or []:
        key = str(row.get("key") or "")
        if not key:
            continue
        size = int(row.get("bytes") or 0)
        parts.append({
            "name": _MEM_PART_NAMES.get(key, key),
            "mb": round(size / 1048576.0, 1),
            "bytes": size,
            "files": int(row.get("count") or 0),
        })
    return parts


def _process_memory() -> dict:
    """当前进程的内存占用（MB）。取不到就返回空字典，页面显示「—」。

    不引 psutil：KiraAI 的运行环境里没装，ctypes 调 kernel32 就够了。
    """
    if not hasattr(ctypes, "windll"):  # 非 Windows
        return {}
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
        kernel32 = ctypes.windll.kernel32
        fn = kernel32.K32GetProcessMemoryInfo
        fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), ctypes.c_uint32]
        fn.restype = ctypes.c_int
        if not fn(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {}
        mb = 1024.0 * 1024.0
        return {
            "rss_mb": round(counters.WorkingSetSize / mb, 1),
            "peak_mb": round(counters.PeakWorkingSetSize / mb, 1),
            "private_mb": round(counters.PagefileUsage / mb, 1),
        }
    except Exception:
        return {}

# 页面 API 在 3.0 之后才有；老版本没有 pages 模块时退化成 no-op，不影响插件加载。
try:
    from core.plugin.pages import PageMenu, PluginPage

    _HAS_PAGES = True
except Exception:  # pragma: no cover - 仅老版本会走到
    PageMenu = None
    PluginPage = None
    _HAS_PAGES = False


def _noop_deco(*args, **kwargs):
    def wrap(fn):
        return fn

    return wrap


_page = register.page if (_HAS_PAGES and hasattr(register, "page")) else _noop_deco
_api = register.api if hasattr(register, "api") else _noop_deco

_MENU = (
    PageMenu(
        label={"zh": "多样决策", "en": "Multi Decision"},
        icon="DataAnalysis",
        order=55,
    )
    if _HAS_PAGES
    else None
)


# 允许「每个群单独设置」的键：只存与全局不同的项，其余跟随全局。
# 总开关、接管、日志保留、自适应统计窗口这类全局项刻意不在里面。
PER_SESSION_KEYS = (
    "session_enabled",
    "trigger_mode",
    "trigger_threshold",
    "trigger_probability",
    "prob_pseudo",
    "adaptive_step",
    # v3.84（用户 m04084 第 3 条）：调参间隔也按群调——大群消息多、调得勤点没问题，
    # 安静群调太快只会被噪声带偏。
    "adaptive_tune_minutes",
    # v3.79（用户 m02095）：节奏离散度也能按群调
    "adaptive_rhythm_sigma",
    # v3.91（用户 m05749 / m05856）：进阶决策的两段式——先让模型读实时窗口判回不回，
    # 判回才走完整那一轮。这些都按群走：哪个群该不该回、回得多不多都不一样。
    "decision_chat_rules",
    "decision_prompt",
    "decision_tools",
    "decision_model",
    "decision_fallback_model",
    # 用户 m01994：思考等级（reasoning_effort）也按群走，留空 = 跟随模型自己的设置。
    "decision_reasoning_effort",
    # 用户 m7217：主模型和备用模型各有一套思考等级，不共用。
    "decision_fallback_reasoning_effort",
    # 用户 m08678：决策页保留自己的一套次数 / 时间设置，默认值与回复一致，可各自调整。
    "decision_fail_switch",
    "decision_fallback_cooldown",
    "decision_tool_loop",
    "decision_tool_calls_per_turn",
    "decision_tool_timeout",
    "decision_tool_output_tokens",
    "decision_fail_action",
    # 回复链的次数 / 时间设置。
    "reply_tool_rounds",
    "reply_tool_calls_per_turn",
    "reply_tool_timeout",
    "reply_tool_output_tokens",
    # 用户 m06995 第 2 项：回复那条链也有一套模型与看图开关，跟决策卡一一对应
    # （少「决策失败时」——回复没有「跳过决策」这回事）。
    "reply_model",
    "reply_fallback_model",
    "reply_reasoning_effort",
    "reply_fallback_reasoning_effort",
    "reply_fail_switch",
    "reply_fallback_cooldown",
    "reply_see_images",
    # 用户 m07859：重试 / 重试间隔 / 单次超时只留基础设置那一份，决策链和回复链都读它；
    # 放进白名单后也按群走。
    "retry_max",
    "retry_delay",
    "watchdog_seconds",
    "watchdog_total_seconds",
    # 用户 m05550：重试的两个取舍开关（默认都关），跟重试参数一样按群走。
    "retry_take_pending",
    "timeout_no_retry",
    # 用户 m06706：JEV 决策跟普通决策一样按群一套——哪个群用专用模型、区间多宽都可以不同。
    "jev_enabled",
    "jev_model",
    "jev_fallback_model",
    "jev_fail_switch",
    "jev_fallback_cooldown",
    "jev_lower",
    "jev_upper",
    "jev_action",
    "jev_persona",
    # 用户 m08123：接口来源也能按群分（一个群走宿主模型、另一个群走第三方接口）。
    # 密钥不在这里——它单独落盘，永不进配置、不回页面。
    "jev_backend",
    "jev_api_base",
    "jev_api_model",
    # 用户 m08755：协议也按群分（同一个地址不同网关，有的只说 OpenAI 兼容）。
    "jev_api_protocol",
    # 用户 m09981：自定义接口可以是好几页，每页各存协议 / 地址 / 模型名（JSON 字符串）。
    "jev_apis",
    "time_bands",
    "wake_words",
    "nicknames",
    "nickname_wake",
    "name_wake",
    "bot_name",
    # v3.46（用户 m05828 第 4 条）：管理员名字 / QQ 号也跟着群走（不同群管理员不同）。
    "admin_name",
    "admin_id",
    # v3.47（用户 m06761 第 7 条）：管理员可多位，整表存在这个键里（JSON 字符串）
    "admin_list",
    "mention_always_reply",
    "mention_trigger",
    # 用户 m00347：外号 / 昵称的「必回」也正式开始用，跟艾特必回一样能单群覆盖
    "nickname_wake_always_reply",
    "name_wake_always_reply",
    # 用户 m00363：休息时段里被叫到还算不算，两路各一个开关
    "rest_wake_trigger",
    "rest_wake_always",
    # v3.44（用户本轮第 1/2 条）：抖动与连发合并都加了开关，开关本身也能单群覆盖
    "jitter_enabled",
    "jitter_seconds",
    # 用户 m05691 第 3 条：抖动最多叠几跳
    "jitter_max_rounds",
    # 用户 m13212 后：@ / 叫名字（必回）要不要跳过抖动，也做成开关
    "jitter_skip_must_go",
    "burst_merge_enabled",
    "burst_merge_seconds",
    "short_msg_chars",
    "phrase_filter_enabled",
    "phrase_filter_list",
    # 用户 m08153：用户黑名单总开关也按会话走——单群 / 私聊可以自己关掉，
    # 全局名单一个字不动，只影响这个会话剔不剔。
    "user_blacklist_enabled",
    "silent_trigger_enabled",
    "silent_trigger_minutes",
    "bands_enabled",
    "rest_silent_enabled",
    "rest_silent_minutes",
    # 用户 m02652：按消息类型分开过滤，每个类型也能单群覆盖。
    "silent_skip_video_link",
    "silent_skip_short",
    "silent_skip_short_len",
    "silent_skip_single_image",
    "silent_skip_single_sticker",
    "silent_skip_empty_forward",
    # 用户 m04896 第 1 条：每个过滤项各自带「管前几条」。
    "silent_skip_video_link_count",
    "silent_skip_short_count",
    "silent_skip_single_image_count",
    "silent_skip_single_sticker_count",
    "silent_skip_empty_forward_count",
    "silent_skip_file_video",
    "silent_skip_file_video_count",
    # 用户 m04896 第 5 条：丢弃文件视频。
    "file_skip_context",
    "rest_mode_enabled",
    "rest_start",
    "rest_end",
    "rest_probability",
    # v3.73（用户 m24640 第 5 条）：曲线模式的休息时段改成段上的 rest 标记，
    # 三个时间点键撤掉，只剩一个概率能单会话覆盖。
    "band_rest_probability",
    # v3.52（用户 m08226 第 5 条）：配额只剩「每小时 / 每天」两个开关，各自封顶
    "quota_hour_enabled",
    "quota_max_per_hour",
    "quota_day_enabled",
    "quota_max_per_day",
    # v3.52（用户 m08226 第 6 条）：退避只有开了进阶模式才生效，开关本身也能单群覆盖
    "advanced_enabled",
    "backoff_enabled",
    "backoff_start_rejects",
    "backoff_base_seconds",
    "backoff_max_seconds",
    "backoff_bypass_count",
    "retry_max",
    "retry_delay",
    "watchdog_seconds",
    "retry_take_pending",
    "timeout_no_retry",
    "window_enabled",
    "window_keep",
    "history_images",
    # v4.45（用户 m22611）：历史里的动图怎么发（九帧 / 原图 / 只读首帧），三个都关＝跳过。
    "history_gif_grid",
    "history_gif_original",
    "history_gif_first",
    # 用户 m10267：动图派生的参数也按会话独立——单帧边长、JPEG 质量。
    # 用户 m30656：拼图抽几帧（9 / 16 / 25）同样按会话独立。
    # 用户 m30982：首帧边长撤掉，只读首帧那一档也按单帧边长缩。
    "gif_tile",
    "gif_quality",
    "gif_frames",
    "window_trim_threshold",
    "protect_tool_groups",
    # v4.92（用户 m13535）：工具往返默认不进实时窗，窗口那边照这个开关整段摘。
    "window_drop_tools",
    "summary_enabled",
    "summary_max_chars",
    "summary_line_chars",
    # v3.60（用户 m18059 第 4 条）：缓存保留线也按会话独立——大群刷得快、小群
    # 一周没人说话，用一套全局上限总有一头不对。这里只放「每会话」的口径，
    # 全局值仍是默认值；清理时按各自有效的这份裁。
    "trace_keep",              # 每会话请求快照/轨迹份数
    "requests_keep_days",      # 请求快照保留天数（0 = 不限）
    # 2026-11-18（用户 m13966）：decisions_keep_days 从这份清单里撤掉——
    # 消息日志和会话统计合成一行，保留天数只按全局算。
    "windows_keep_days",       # 窗口快照保留天数
    "windows_keep",            # 窗口快照份数（0 = 不限）
    "window_show",             # 窗口快照在页面上显示多少条（v4.90，用户 m01875 第 1 条）
    "media_cache_limit",       # 图片缓存张数（每个群组各自的上限，非全目录总上限）
    "media_other_limit",       # 其他图片（推文卡片图）张数（每个群组各自的上限）
    "media_cache_keep_days",   # 图片缓存保留天数
    "media_other_keep_days",   # 其他图片（推文卡片图）保留天数
    # v3.66（用户 m19536 第 4 条）：上限拆成「群聊默认 / 私聊默认」两套——私聊的人少、
    # 刷得慢，用群聊那一套等于白占盘。这四个是私聊默认值，群聊仍用上面那些原键。
    "media_cache_limit_dm",    # 图片缓存张数（私聊默认）
    "media_other_limit_dm",    # 其他图片（推文卡片图）张数（私聊默认）
    "trace_keep_dm",           # 每会话请求快照份数（私聊默认）
    # v4.00（用户 m10782）：决策快照单独一类缓存，份数线自己一套
    "decision_keep",           # 每会话决策快照份数
    "decision_keep_dm",        # 每会话决策快照份数（私聊默认）
    "windows_keep_dm",         # 窗口快照份数（私聊默认）
    "window_show_dm",          # 窗口快照显示条数（私聊默认）
    "summaries_keep_dm",       # 中期摘要上限（私聊默认）
    # v4.54（用户 m18227）：转发缓存 / 重试记录也按会话设上限。
    # 私聊窗口存档不设个数上限，也不放进单会话覆盖。
    # 用户 m10466：转发图片和原图记录合成一份，上限 / 天数只留一套。
    "forward_media_keep",      # 每个会话留多少条转发缓存
    "retry_keep",              # 每个会话留多少条重试链
    "forward_media_keep_days", # 转发缓存保留天数
    "retry_keep_days",         # 重试链保留天数
    # 用户 m19210：这两类也按老规矩分「群聊默认 / 私聊默认」两套值。
    "forward_media_keep_dm",   # 转发缓存（私聊默认）
    "retry_keep_dm",           # 重试链（私聊默认）
)

# 整数项的取值范围，覆盖值也要守规矩（前端提交什么都有可能）。
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BOUNDS = {
    "trigger_threshold": (1, None),
    "trigger_probability": (0, 100),
    "adaptive_step": (1, None),
    "adaptive_tune_minutes": (1, 120),
    "adaptive_rhythm_sigma": (0, 120),
    # 用户 m08678：决策页保留自己的一套次数 / 时间设置，范围跟回复那边一致。
    "decision_tool_loop": (1, 10),
    "decision_tool_calls_per_turn": (1, 20),
    "decision_tool_timeout": (0, 600),
    "decision_tool_output_tokens": (0, 200000),
    # 用户 m08153：三个「连续失败切换」从次数改成开关，阈值固定 2，不再有范围。
    # 用户 m10202：冷却改成按分钟填（界面 30 分钟 = 存盘 1800 秒），范围 1 分钟 ~ 24 小时。
    "decision_fallback_cooldown": (60, 86400),
    # 回复链的次数 / 时间设置。
    "reply_tool_rounds": (1, 10),
    "reply_tool_calls_per_turn": (1, 20),
    "reply_tool_timeout": (0, 600),
    "reply_tool_output_tokens": (0, 200000),
    # 用户 m06995 第 2 项：回复模型的切换阈值与冷却，范围跟决策那边一致。
    # 用户 m08153：reply_fail_switch 改成开关，不再走 _BOUNDS。
    "reply_fallback_cooldown": (60, 86400),
    # 用户 m06706：JEV 的不确定区间按「百分比」存（40 / 60），运行时再归一到 0~1。
    "jev_lower": (0, 100),
    "jev_upper": (0, 100),
    # 用户 m08153：jev_fail_switch 改成开关，不再走 _BOUNDS。
    "jev_fallback_cooldown": (60, 86400),
    "jitter_seconds": (0, None),
    "jitter_max_rounds": (1, 20),
    "burst_merge_seconds": (0, None),
    "short_msg_chars": (0, None),
    "silent_trigger_minutes": (1, 1440),
    "rest_silent_minutes": (1, 1440),
    # 用户 m02652：短句阈值与「决策后过滤条数」，引擎里也夹了同样的范围。
    "silent_skip_short_len": (1, 50),
    "silent_skip_video_link_count": (1, 50),
    "silent_skip_short_count": (1, 50),
    "silent_skip_single_image_count": (1, 50),
    "silent_skip_single_sticker_count": (1, 50),
    "silent_skip_empty_forward_count": (1, 50),
    "silent_skip_file_video_count": (1, 50),
    "rest_probability": (0, 100),
    "band_rest_probability": (0, 100),
    # v3.72（用户 m24260 第 9 条）：回看改成「周」，最少 4 周
    # A3（用户 m26791）：上限从 52 周收到 12 周——分桶统计最多只扫 120 天（约 17 周），
    # 再往上填也不会多看到一天数据，干脆锁在 12 周，界面上的数和实际用到的数据对得上。
    "adaptive_weekday_window_weeks": (4, 12),
    "quota_max_per_hour": (1, None),
    "quota_max_per_day": (1, None),
    "backoff_start_rejects": (1, None),
    "backoff_base_seconds": (1, None),
    "backoff_max_seconds": (1, None),
    "backoff_bypass_count": (1, None),
    "retry_max": (0, 10),
    "retry_delay": (0, 600),
    "watchdog_seconds": (30, 600),
    # 用户 m01587：整轮回复的总时长上限，60-3600 秒（默认 600）。
    "watchdog_total_seconds": (60, 3600),
    "window_keep": (1, None),
    "history_images": (0, 500),
    # 用户 m10267：动图参数的范围跟 ui_schema 的 min/max 对齐。
    "gif_tile": (128, 512),
    "gif_quality": (1, 100),
    # 用户 m30656：拼图抽几帧，界面上只有 9 / 16 / 25 三档，范围放宽到 4–36 兜底。
    # 用户 m13967：多一个 0 = 自动，所以下限放到 0。
    "gif_frames": (0, 36),
    "window_trim_threshold": (1, None),
    # v3.52（用户 m08226 第 7 条）：裁切缓冲，0 = 关掉强制裁切
    "window_buffer": (0, None),
    "media_cache_limit": (1, 100000),
    # v3.66（用户 m19536 第 4 条）：私聊那一套默认值，范围跟群聊一致；0 = 不限的项保留 0
    "media_cache_limit_dm": (0, 100000),
    "trace_keep_dm": (1, 1000),
    "decision_keep_dm": (1, 1000),
    "windows_keep_dm": (0, 1000),
    "window_show_dm": (1, 1000),
    "summaries_keep_dm": (0, 1000),
    # 用户 m11091 第 11 条：图片缓存的两条保留线，0 = 不限
    "media_cache_keep_days": (0, None),
    "media_other_keep_days": (0, 3650),
    # 用户 m18059 第 3/4 条：缓存保留线可以单群覆盖，覆盖值一样要守住范围
    "trace_keep": (1, 1000),
    "decision_keep": (1, 1000),
    "windows_keep": (0, 1000),
    "window_show": (1, 1000),
    # 用户 m13708 第 3/4 条：每类缓存各自的保留天数 / 上限，这类全局键同样要守住范围。
    # 天数类一律 0-3650（0 = 不限）；消息日志/窗口快照是按天文件，至少要留 1 天。
    "requests_keep_days": (0, 3650),
    # v3.66（用户 m19536 第 3 条）：消息日志与会话统计是自适应阈值的原料，最少留 56 天，
    # 只能往高调——调低了自适应就成天在「样本不够」里打转。
    "decisions_keep_days": (56, 3650),
    "windows_keep_days": (1, 3650),
    "summaries_keep": (0, 1000),
    "summaries_keep_days": (0, 3650),
    # v4.54（用户 m18227）：这两类缓存按会话设上限，0 = 不限。
    # 私聊窗口存档不设上限：用户 m18819 要求有多少私聊就留多少。
    "forward_media_keep": (0, 10000),
    "forward_media_keep_dm": (0, 10000),
    "media_other_limit": (0, 10000),
    "media_other_limit_dm": (0, 10000),
    "retry_keep": (0, 5000),
    "retry_keep_dm": (0, 5000),
    # 用户 m19940：两类新缓存也加保留天数，0 = 不限。
    "forward_media_keep_days": (0, 3650),
    "retry_keep_days": (0, 3650),
    "stats_keep_days": (56, 3650),
    # v4.90（发版前审计）：设置页写「最少 7 天」，但后端一直没夹这个键，
    # 填 1 也能存进去，跟界面对不上。补上范围。
    "stats_window_days": (7, 365),
    "summary_max_chars": (60, 4000),
    "summary_line_chars": (8, 200),
}

_ENUMS = {
    "trigger_mode": ("count", "probability", "adaptive"),
    # 用户 m08755：自定义接口的协议，只收这两个值。
    "jev_api_protocol": ("systemone", "openai"),
    # 用户 m01994：思考等级只收这几档，留空 = 跟随模型自己的设置。
    "decision_reasoning_effort": ("", "off", "low", "medium", "high", "xhigh", "max"),
    "decision_fallback_reasoning_effort": ("", "off", "low", "medium", "high", "xhigh", "max"),
    "reply_reasoning_effort": ("", "off", "low", "medium", "high", "xhigh", "max"),
    "reply_fallback_reasoning_effort": ("", "off", "low", "medium", "high", "xhigh", "max"),
}


# Q02（用户 m04478 定「以设置页显示为准」）：下面两个默认值必须跟 ui_schema.json
# 的 default 一致，否则界面显示一个值、实际生效另一个值。两处要一起改，
# tests/test_q02_q03_contract.py 会逐项对照，改漏一边就红。
_UI_DEFAULT_TIME_BANDS = [
    {"s": 0, "e": 24, "v": 20},
    {"s": 24, "e": 46, "v": 8},
    {"s": 46, "e": 48, "v": 20},
]
_UI_DEFAULT_PHRASE_FILTER = ["哈哈", "草"]


def _merge_sid_key(sid: str) -> str:
    """把 <bot名>:gm:<群号> 归一成 gm:<群号>，给左栏合并新旧 sid 用。

    sid 由框架生成，前缀是机器人自己在该会话里的昵称；机器人改名后
    同一个群会同时留下旧 sid 和新 sid 两套窗口/请求记录。
    """
    parts = str(sid or "").split(":")
    if len(parts) >= 3 and parts[-2] in ("gm", "dm"):
        return parts[-2] + ":" + parts[-1]
    return str(sid or "")


# 时段曲线（v3.28，用户 m47321）：24 小时按 30 分钟切 48 格，
# 每段一个触发概率百分比。前端拖两根竖线划出一段、在段顶填数字。
_BAND_SLOTS = 48


def _norm_week_list(raw, high: float):
    """把段上的星期目标（trw / tpw = 周一..周日各一个）规整成 7 元列表。

    用户本轮第 1 条：设置页能填周一到周日 7 个不同的目标，可一保存就没了——
    前端发得没错，是 _normalize_bands 只认 s/e/v/rest/tr/tp，把这两项整份丢了。
    这里补上解析：永远回 7 位，0 / 缺项 / 坏值 = 那天不单独设（判定时回落到
    tr / tp，见 decision/engine.py 的 _band_day_target）；全是 0 就当没填，回 None。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    out = []
    for i in range(7):
        try:
            val = float(raw[i]) if i < len(raw) else 0.0
        except (TypeError, ValueError, IndexError):
            val = 0.0
        if val <= 0:
            out.append(0)
        elif high > 1000:
            out.append(int(round(min(float(high), val))))
        else:
            out.append(round(min(float(high), val), 1))
    if not any(v > 0 for v in out):
        return None
    return out


def _normalize_bands(raw) -> str:
    """把时段曲线规整成 JSON 文本，存进配置就这一份口径。

    段用 {"s": 起点槽位, "e": 终点槽位（不含）, "v": 百分比}，槽位 0..48。
    这里做四件事：按起点排序、夹紧到 0..48 与 0..100、前一段吃掉后一段的
    重叠部分、相邻同值的段合并。解析失败一律返回空串，调用方按「没设置」处理，
    这样界面填了脏数据不会把插件搞崩。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except Exception:
            return ""
    else:
        data = raw
    if not isinstance(data, list):
        return ""
    bands = []
    for it in data:
        if not isinstance(it, dict):
            continue
        try:
            start = int(it.get("s"))
            end = int(it.get("e"))
            value = int(round(float(it.get("v"))))
        except (TypeError, ValueError):
            continue
        start = max(0, min(_BAND_SLOTS - 1, start))
        end = max(start + 1, min(_BAND_SLOTS, end))
        seg = {"s": start, "e": end, "v": max(0, min(100, value))}
        # v3.73（用户 m24640 第 5 条）：休息时段回到第一版——段上带 rest 标记，
        # 曲线上点某段右上角的月亮就是「这一段是休息时段」。可以有好几段。
        if it.get("rest"):
            seg["rest"] = True
        # v3.71（用户 m23596 第 8 条）：动态调整的目标挂在段上——
        # tr = 目标回复次数/天、tp = 目标回复概率（每条消息 %）。0 / 缺省 = 不设目标。
        try:
            tr = int(round(float(it.get("tr") or 0)))
        except (TypeError, ValueError):
            tr = 0
        try:
            tp = round(float(it.get("tp") or 0), 1)
        except (TypeError, ValueError):
            tp = 0.0
        if tr > 0:
            seg["tr"] = max(1, min(100000, tr))
        if tp > 0:
            seg["tp"] = max(0.1, min(100.0, tp))
        # 用户本轮第 1 条：星期目标（周一..周日各一个）原来在规整这一层被整份丢掉——
        # 设置页填了 7 个不同的目标，一保存就只剩统一值，重新打开全空。
        # 这里跟 tr / tp 一样原样接手，解析成 7 元列表再存回去。
        trw = _norm_week_list(it.get("trw"), 100000)
        if trw:
            seg["trw"] = trw
        tpw = _norm_week_list(it.get("tpw"), 100.0)
        if tpw:
            seg["tpw"] = tpw
        bands.append(seg)
    if not bands:
        return ""
    bands.sort(key=lambda b: (b["s"], b["e"]))
    out = []
    cursor = 0
    for b in bands:
        if b["e"] <= cursor:
            continue
        start = max(b["s"], cursor)
        if b["e"] <= start:
            continue
        # v3.71：目标不同的相邻段不能合并，否则「每段一个目标」就丢了
        # v3.73：休息标记不同的相邻段也不能合并（用户 m24640 第 5 条）
        if (out and out[-1]["v"] == b["v"] and out[-1]["e"] == start
                and out[-1].get("tr") == b.get("tr") and out[-1].get("tp") == b.get("tp")
                # 用户本轮第 1 条：星期目标不同的相邻段同样不能合并，
                # 否则刚填进去的 7 天数据会被「同概率就并段」吞掉。
                and out[-1].get("trw") == b.get("trw") and out[-1].get("tpw") == b.get("tpw")
                and bool(out[-1].get("rest")) == bool(b.get("rest"))):
            out[-1]["e"] = b["e"]
        else:
            merged = {"s": start, "e": b["e"], "v": b["v"]}
            # v3.71：目标要跟着段一起搬，否则整份规整完 tr / tp 就没了
            if b.get("tr"):
                merged["tr"] = b["tr"]
            if b.get("tp"):
                merged["tp"] = b["tp"]
            if b.get("rest"):
                merged["rest"] = True
            # 用户本轮第 1 条：星期目标也要跟着段一起搬，否则并段之后 7 天数据就没了
            if b.get("trw"):
                merged["trw"] = b["trw"]
            if b.get("tpw"):
                merged["tpw"] = b["tpw"]
            out.append(merged)
        cursor = b["e"]
    return json.dumps(out, ensure_ascii=False)


def _normalize_phrases(raw) -> str:
    """把「不触发短语」规整成 JSON 数组文本。

    接受 JSON 数组文本、真数组、或一行一条的纯文本（前端文本域直接提交）。
    空串一律返回 "[]"，这样「一条都没填」和「关掉开关」是两种状态，不会混。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return "[]"
        try:
            data = json.loads(text)
        except Exception:
            data = text.splitlines()
    else:
        data = raw
    if not isinstance(data, list):
        return "[]"
    out = []
    for it in data:
        word = str(it).strip()
        if word and word not in out:
            out.append(word)
    return json.dumps(out, ensure_ascii=False)


def _coerce(key: str, value, default):
    """把覆盖值转成与全局属性同一个类型，并守住取值范围。"""
    # 时段曲线存的是 JSON 文本，单群覆盖也要走同一套规整，否则脏数据会原样存进去
    if key == "time_bands":
        return _normalize_bands(value)
    # 「不触发短语」存的是 JSON 数组文本，单群覆盖同样要规整
    if key == "phrase_filter_list":
        return _normalize_phrases(value)
    if isinstance(default, bool):
        return _as_bool(value, default)
    if isinstance(default, int):
        num = _as_int(value, default)
        low, high = _BOUNDS.get(key, (None, None))
        if low is not None and num < low:
            num = low
        if high is not None and num > high:
            num = high
        return num
    if isinstance(default, list):
        return _as_list(value)
    if isinstance(default, str):
        text = str(value)
        allowed = _ENUMS.get(key)
        if allowed and text not in allowed:
            return default
        return text
    return value


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _snapshot_id(ts) -> str:
    """一份快照的稳定编号（v4.71，用户 m01516 第 3 条）。

    用户要「给每一个窗口快照都生成一个有规律的 ID」，以后按时间段找聊天记录时
    能直接说「W20261106-090000 到 W20261106-120000 之间那几份」。编号只由快照
    自己的时间戳算出来：不落盘、不新建文件、不重写老记录，所以历史快照也立刻
    有号，重新载入不会变，按字符串排序就是时间顺序。
    """
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return ""
    lt = time.localtime(t)
    ms = int(round((t - int(t)) * 1000.0)) % 1000
    return "W%s-%s-%03d" % (time.strftime("%Y%m%d", lt), time.strftime("%H%M%S", lt), ms)


def _record_has_mid(data, mid: str) -> bool:
    """这份记录里有没有这条消息（用户 m12814：点引用块跳过去要按 id 回查）。

    只看 messages / incoming_messages：system_prompt 里也常把同一段聊天记录整段塞进去，
    拿整份文本搜会被它骗到，跳过去却找不到那条。
    """
    needle = "[message_id: %s]" % mid
    for key in ("messages", "incoming_messages"):
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            # 用户 m18902：content 有时是分片列表（[{"type": "text", "text": …}]），
            # 以前只认字符串，这类消息永远查不到，点引用块跳不过去。
            txt = content_text(item.get("content"))
            if not txt:
                txt = str(item.get("text") or "")
            if needle in str(txt):
                return True
    return False

def _stamp_snap_ids(rows) -> None:
    """给一批快照记录补上 snap_id（原地写；老记录同样算得出来）。"""
    for it in rows or []:
        if isinstance(it, dict):
            it["snap_id"] = _snapshot_id(it.get("ts"))

def _bounded_int(key: str, value, default: int) -> int:
    """读全局整数配置：先 _as_int 转型，再按 _BOUNDS 夹范围（用户 m13708 第 4 条）。

    单群覆盖走 _coerce，本来就会夹；全局项没有覆盖层，所以读配置时也得守一遍，
    免得手改配置文件塞进 999999 天这种值。
    """
    num = _as_int(value, default)
    low, high = _BOUNDS.get(key, (None, None))
    if low is not None and num < low:
        num = low
    if high is not None and num > high:
        num = high
    return num


def _path_usage(folder, suffix: str = "", recursive: bool = False) -> tuple:
    """数一个目录下（可选后缀的）文件个数与总字节，返回 (count, bytes)。

    recursive=True 时连子目录一起数——请求快照和调用轨迹是「每会话一个子目录」。
    """
    count = 0
    size = 0
    try:
        root = Path(folder)
        if suffix:
            items = list(root.rglob("*" + suffix) if recursive else root.glob("*" + suffix))
        elif recursive:
            items = list(root.rglob("*"))
        else:
            items = list(root.iterdir())
    except Exception:
        return (0, 0)
    for path in items:
        try:
            if not path.is_file():
                continue
            size += int(path.stat().st_size or 0)
            count += 1
        except OSError:
            continue
    return (count, size)


def _jsonl_usage(folder, suffix: str = ".jsonl", sid: str = "", subdirs: bool = False) -> tuple:
    """数一个目录下 *.jsonl 的「记录条数」（非空行）与字节，返回 (count, bytes)。

    用户 m13708 第 5 条：消息日志/窗口快照是一天一个文件、多会话混写，数文件
    个数会变成「有几天记录」——用户看到的就是「一个群那么多快照只有 5 份」。
    所以缓存页这类一律按非空行数算条数。

    sid 非空时只算这个会话的记录（用户 m22090：单群设置里要看这个群自己的量）：
    逐行读 sid 字段再累加，字节也只算这些行，不再把整份混写文件都记到它头上。

    subdirs=True 时连子目录一起数（用户 m09847 第 2 条：用量账本往月的行在
    stats/archive/ 里，只 glob 顶层会把 2.5 MB 归档整个漏掉）。
    """
    count = 0
    size = 0
    want = normalize_sid(str(sid or "").strip()) if sid else ""
    try:
        items = list(Path(folder).glob(("**/*" if subdirs else "*") + suffix))
    except Exception:
        return (0, 0)
    for path in items:
        try:
            if not want:
                size += int(path.stat().st_size or 0)
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.strip():
                            count += 1
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if normalize_sid(str(rec.get("sid") or "")) != want:
                        continue
                    count += 1
                    size += len(line.encode("utf-8"))
        except OSError:
            continue
    return (count, size)


def _as_price(value) -> float:
    """把价目里的单价洗成数字：None / 空串 / 非法 → 0.0。

    0 元是有效价格（用户 2026-10-24 第 5 条：免费的模型不该被当成「没定价」），
    所以这里只有「读不出来」才归 0，不能顺手把 0 也当成空。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num or num == float("inf") or num == float("-inf") or num < 0:
        return 0.0
    return num


# v3.13（用户 m39983）：这几个键是锁定项，恒为 True。
# - window_enabled：接管上下文窗口是本插件的核心功能，关掉它等于插件不存在；
# - dynamic_to_tail：动态段挪到本轮消息之后是缓存命中的关键优化（实测 37.5% → 98%），
#   关掉会让每轮前缀从中间断开。
# - protect_tool_groups：裁切时拆开工具调用和结果，模型会看到半截配对（用户 m25921）。
# 前端已把这几项从表单撤下 / 标为「已锁定」，这里再兜一层，防止旧配置文件、
# 单群 overrides 或直接调接口把它关掉。
_LOCKED_ON = frozenset({"window_enabled", "dynamic_to_tail", "protect_tool_groups"})


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    return bool(value)


def _as_list(value) -> list:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        # 兼容手改配置文件或老前端：逗号 / 分号 / 换行都能当分隔符。
        text = value.replace("，", ",").replace("；", ";").replace("\n", ";")
        return [p.strip() for p in text.replace(",", ";").split(";") if p.strip()]
    return []


# 用户 m13442 第 4 条：框架「代理设置」里的四项（最大代理循环次数 / 单轮工具调用次数上限 /
# 工具调用超时时间 / 工具输出上限）改由插件接管。框架在三个地方现读这四项：
#   execute.py:31（ExecuteAgentStage 开头）、func_tool_manager.py:65/71/80（工具循环里）、
#   prompt_manager.py:84-93（BuildRequest 阶段烤进 Prompt kwargs）。
# 前两处都在 on.llm_request 之后、同一个任务里，所以只要在 on.llm_request 里把值塞进
# contextvar、收尾时清掉，框架读到的就是插件的值；第三处（kwargs）由 prompt_layout 的
# _patch_agent_limits 单独改。
_REPLY_LIMITS = contextvars.ContextVar("kira_md_reply_limits", default=None)
_AGENT_LIMIT_KEYS = {
    "bot_config.agent.max_tool_loop": "rounds",
    "bot_config.agent.max_tool_calls_per_turn": "calls_per_turn",
    "bot_config.agent.tool_call_timeout": "timeout",
    "bot_config.agent.max_tool_output_tokens": "output_tokens",
}


def _install_agent_limit_patch() -> bool:
    """给框架的 KiraConfig.get_config 打一次类方法补丁（幂等）。

    只在 contextvar 有值（也就是这一轮是插件的回复请求）时才改返回值，其余请求原样透传。
    """
    if not compat.has_config_patch_target():
        # 框架换了配置类或改了 get_config 的签名：补丁打上去也不会被调用。
        # 与其静默失效，不如留一条 WARNING，让人知道这四项又回到框架值了。
        logger.warning(
            "[multi-decision] 框架配置补丁不可用，回复轮数/超时设置将沿用框架值"
        )
        return False
    try:
        from core.config.config_loader import KiraConfig
    except Exception:
        logger.exception("[multi-decision] 找不到框架配置类，代理设置接管失效")
        return False
    if getattr(KiraConfig, "_kira_md_limits_patched", False):
        return True
    original = KiraConfig.get_config

    def get_config(self, key, default=None, splitter="."):
        value = original(self, key, default, splitter)
        limits = _REPLY_LIMITS.get()
        if limits:
            field = _AGENT_LIMIT_KEYS.get(key)
            if field is not None and field in limits:
                return limits[field]
        return value

    KiraConfig.get_config = get_config
    KiraConfig._kira_md_limits_patched = True
    logger.info(
        "[multi-decision] 已接管框架代理设置四项（回复侧）："
        + "、".join(_AGENT_LIMIT_KEYS)
    )
    return True


def _remember_provider(store, keys, label: str, provider_id: str = "") -> None:
    """把认出来的渠道名记到 model_providers.json（用户 m22269 第 3 条；失败不影响展示）。"""
    if store is None or not label:
        return
    try:
        store.remember(keys, label, provider_id=provider_id)
    except Exception:
        logger.exception("[multi-decision] 记录渠道绑定失败（忽略）")


def _bound_provider(store, keys, provider_id: str = "") -> str:
    """现读配置查不到渠道时的兜底：先按 provider_id，再按模型名/模型键。"""
    if store is None:
        return ""
    try:
        return store.lookup(keys, provider_id=provider_id)
    except Exception:
        return ""


def _skip_ts_of(item) -> float:
    """取一条消息 / 一条虚线记录的时间戳，取不到给 0。"""
    try:
        if isinstance(item, dict):
            return float(item.get("ts") or 0.0)
        return float(getattr(item, "ts", 0.0) or 0.0)
    except Exception:
        return 0.0


def _skip_at_by_ts(rts: float, stamps) -> int:
    """虚线卡按时间找插入位置（用户 m13127）：第一个比它晚的下标，都比它早就放末尾。

    返回 -1 表示这条没有可用时间，调用方退回锚点。
    """
    if rts <= 0:
        return -1
    seen = False
    for i, st in enumerate(stamps):
        if st > 0:
            seen = True
            if st > rts:
                return i
    return len(stamps) if seen else -1


#: 窗口里的历史消息只有正文开头那个 [23:25]，没有 ts（框架原文的短格式）。
_SKIP_CLOCK_RE = re.compile(r"^\[(\d{1,2}):(\d{2})\]")


def _skip_clock_of(item) -> int:
    """消息自己的钟点（当天第几分钟），认不出给 -1。

    用户 m16145：窗口里的历史消息没有 ts，只有正文开头的 [23:25]，虚线卡按 ts
    找位置时这些消息全被当成「没有时间」，于是整组挂到窗口最底部。
    """
    if not isinstance(item, dict):
        return -1
    cands = [item.get("raw"), item.get("text")]
    try:
        cands.append(content_text(item.get("content")))
    except Exception:
        pass
    for s in cands:
        if not isinstance(s, str):
            continue
        m = _SKIP_CLOCK_RE.match(s.lstrip())
        if not m:
            continue
        h, mi = int(m.group(1)), int(m.group(2))
        if h <= 23 and mi <= 59:
            return h * 60 + mi
    return -1


def _skip_stamp_of(item, ref_ts: float = 0.0) -> float:
    """一条消息的时刻：有 ts 用 ts，没有就按 ref_ts 那天把 [HH:MM] 补成完整时间。

    跨零点按 ±12 小时折：ref 是 00:05、消息写 [23:50] 就是前一天。
    """
    ts = _skip_ts_of(item)
    if ts > 0:
        return ts
    cm = _skip_clock_of(item)
    if cm < 0 or ref_ts <= 0:
        return 0.0
    lt = time.localtime(ref_ts)
    d = cm - (lt.tm_hour * 60 + lt.tm_min)
    if d > 720:
        d -= 1440
    elif d < -720:
        d += 1440
    return ref_ts + d * 60.0


class MultiDecisionPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        # 进阶决策（v3.91，用户 m05749）：初始化时的配置快照，给 _decision_get 兜底用
        # （运行中的 ctx.config 拿不到时退回这里）。
        self._cfg_snapshot = dict(cfg) if isinstance(cfg, dict) else {}
        self._takeover_checked_at = 0.0
        self._takeover_owned = False
        # M06：接管的是「哪一个」实例。只凭布尔的话，宿主换过实例之后
        # 停用插件会把不是我们按住的新实例放开（可能是用户自己停用的）。
        self._takeover_instance = None
        # 私有标志 _terminating 消失时的兜底：用官方 set_plugin_enabled 停掉
        # default-chat。它会把状态写进宿主持久配置，所以要记下是我们关的。
        self._takeover_toggled = False
        self._takeover_task = None
        self.enabled = _as_bool(cfg.get("enabled"), True)
        self.decision_enabled = _as_bool(cfg.get("decision_enabled"), True)
        # 单会话总开关的全局默认值。这个键不进 schema（它天生是「每群各一份」），
        # 只作为 eff() 的兜底默认，实际值来自 overrides。
        self.session_enabled = True
        # v3.13（用户 m39983）：核心功能，恒开。配置里的旧值不再读取，见 _LOCKED_ON。
        self.window_enabled = True
        # v3.09（用户 m39057）：这个开关并进 decision_enabled 了。单独关掉它只会让
        # 内置 default-chat 和本插件同时回复（双回复），没有正当用途，所以直接跟随
        # decision_enabled；配置里残留的旧值不再读取。
        self.takeover_default_chat = self.decision_enabled

        self.trigger_mode = str(cfg.get("trigger_mode") or "count")
        if self.trigger_mode not in ("count", "probability", "adaptive"):
            logger.warning(
                f"[multi-decision] 无法识别的 trigger_mode: {self.trigger_mode!r}，回退 count"
            )
            self.trigger_mode = "count"
        # 默认 5（用户 m03014 第 6 条：计数模式默认别攒到 10 条才回一次）；
        # ui_schema.json 里 trigger_threshold 的 default 也必须是 5，两处一起改。
        # v3.70（用户 m21533 第 2 条）：私聊只有「触发概率默认 100%」这一个特例，
        # 触发方式仍跟全局走（用户 m13250 第 4 条明确取消了「私聊恒必回」）。
        self.trigger_threshold = max(1, _as_int(cfg.get("trigger_threshold"), 5))
        # 默认 20（用户 m03616 第 5 条）；ui_schema.json 里 trigger_probability 的
        # default 也必须是 20，两处一起改。
        self.trigger_probability = _as_int(cfg.get("trigger_probability"), 20)
        # v3.68（用户 m20632 第 2 条）：私聊默认必回——概率 100%。
        # 私聊没有独立的触发策略（整组已删），所以走「私聊默认」这一套：eff() 对 dm 的
        # trigger_probability 回退到这里。没进 ui_schema.json，页面不出现（私聊不用调）；
        # 想改可以手写 config 里的 trigger_probability_dm。
        self.trigger_probability_dm = max(0, min(100, _as_int(cfg.get("trigger_probability_dm"), 100)))
        # v3.81（用户 m02645 图2）：prob_pseudo 从「伪随机兜底」改名成「节奏模式」——
        # 开 = 按 state/band_rhythm.py 的间隔排队回（概率、曲线、动态调整共用一套），
        # 关 = 每条独立掷骰。动态调整是强制开，不看这个开关。
        self.prob_pseudo = _as_bool(cfg.get("prob_pseudo"), False)
        # v3.81（用户 m02645 图2）：prob_pseudo_step / prob_pseudo_boost 是 v3.30~v3.80
        # 那套「抬概率」兜底的参数，算法换掉后就没有消费者了——这里连同按群覆盖白名单
        # 与取值校验一起删掉，老 config.json 里残留的键只是被忽略（插件不校验未知键）。
        # 用户 m12007 第 4 条：私聊策略（always / count / probability）整组删除——
        # 群聊那套触发条件对私聊一样生效，私聊现在恒为必回，不再有独立的 mode / 阈值 / 概率。
        # 老配置里残留的 private_* 键不再读，也不再出现在设置页和 schema 里。
        # v3.71（用户 m23596 第 5 条）：全局「自适应目标回复率」整项撤掉——目标改成
        # 每个时段各自设（存在 time_bands 每段的 tr / tp 里），动态调整按段算。
        self.adaptive_step = max(1, _as_int(cfg.get("adaptive_step"), 2))
        # v3.84（用户 m04084 第 3 条）：调参间隔（分钟）。原来是写死的 10 分钟、而且只在
        # 「触发并结算」时才调。现在判定时也调，间隔由这一项管，默认 5 分钟。
        self.adaptive_tune_minutes = max(1, min(120, _as_int(cfg.get("adaptive_tune_minutes"), 10)))
        # v3.79（用户 m02095）：节奏离散度（百分数）。0 = 每次间隔都一样，
        # 65 = 正常，120 = 很随意。只改节奏、不改平均次数，所以调参目标不受影响。
        # v3.81（用户 m02645 图2）：从「动态调整专属」升格为节奏模式全模式共用的一档，
        # 设置页挪到「节奏模式」组下面。
        self.adaptive_rhythm_sigma = max(0, min(120, _as_int(cfg.get("adaptive_rhythm_sigma"), 65)))
        self.stats_window_days = max(1, _as_int(cfg.get("stats_window_days"), 7))
        # v3.72（用户 m24260 第 6 条）：撤掉「最少观察天数」——开动态调整就是为了用
        # 数据，有数据就调。第 9 条：分桶回看改成「周」，最少 4 周、默认 8 周。
        self.adaptive_weekday_bucket = _as_bool(cfg.get("adaptive_weekday_bucket"), False)
        self.adaptive_weekday_window_weeks = max(
            4, min(12, _as_int(cfg.get("adaptive_weekday_window_weeks"), 8))
        )
        # v3.28（用户 m47321）：时段曲线。24 小时切 48 格，每段一个触发概率。
        # 空串 = 没设置（界面按「未覆盖的时间用触发概率」显示）。目前只存不用，
        # 等动态模式方案定下来再接进判定。
        # v3.33（用户 m51068 第 8/9 条）：曲线本身变成开关——开着才参与判定，
        # 并且接管普通模式的阈值/概率与休息时段（曲线已经把一天切成不同概率）。
        # v3.39（用户本轮）：只接管回复概率，休息时段的兜底窗口照用。
        self.bands_enabled = _as_bool(cfg.get("bands_enabled"), False)
        # 缺键 = 从没存过：用设置页默认那三段曲线；显式清空（""）仍按「没设置」处理。
        raw_bands = cfg.get("time_bands")
        self.time_bands = _normalize_bands(
            _UI_DEFAULT_TIME_BANDS if raw_bands is None else raw_bands
        )

        self.wake_words = _as_list(cfg.get("wake_words"))
        self.nicknames = _as_list(cfg.get("nicknames"))
        # v3.10：「外号触发唤醒」开关。默认开，老配置里没有这个键也照样按开处理，
        # 否则升级后群里喊外号突然不理人了。engine._direct_reason 用它决定
        # nicknames 要不要算「直接叫到」；机器人自己的名字不受它影响。
        self.nickname_wake = _as_bool(cfg.get("nickname_wake"), True)
        # v3.11：「昵称触发唤醒」开关，管机器人自己的名字（bot_name / 群名片）。
        # 默认开，理由同上：老配置升级后喊名字不能突然不理。
        self.name_wake = _as_bool(cfg.get("name_wake"), True)
        # v3.30（用户 m47796 第 4 条）：@ 拆成两个开关。
        # 「艾特触发」= 被 @ 直接算一次触发（就是老 mention_always_reply 的行为），
        #   老配置里没有 mention_trigger 时拿旧值兜底，升级后行为不变。
        # 「艾特必回」= 跳过模型判断直接回（见下面 m00347 那条说明）。
        self.mention_trigger = _as_bool(
            cfg.get("mention_trigger"), _as_bool(cfg.get("mention_always_reply"), True)
        )
        self.mention_always_reply = _as_bool(cfg.get("mention_always_reply"), False)
        # 用户 m00347：「必回」不再是占位——三个开关各管一路（艾特 / 外号 / 昵称），
        # 命中就跳过模型判断直接回（见 decision/engine.py 的 _must_reason）。
        self.nickname_wake_always_reply = _as_bool(cfg.get("nickname_wake_always_reply"), False)
        self.name_wake_always_reply = _as_bool(cfg.get("name_wake_always_reply"), False)
        # 用户 m00363：休息时段里被叫到还要不要理，也分触发 / 必回两路各一个开关
        # （基础设置右下角，跟唤醒那三行排在一起）。
        # 用户 m01432：休息时段默认关闭——睡觉时被叫到默认不理，想理就把触发拨过去。
        # 必回默认关（用户 m09403）。
        self.rest_wake_trigger = _as_bool(cfg.get("rest_wake_trigger"), False)
        self.rest_wake_always = _as_bool(cfg.get("rest_wake_always"), False)

        # v3.44（用户本轮第 1 条）：抖动 = 开关 + 固定秒数。开关默认关（关着就是
        # 触发即动），秒数默认 7，开了之后固定等 7 秒，不再是 0~N 的随机等待。
        # ui_schema.json 里 jitter_enabled / jitter_seconds 的默认值要和这里一致。
        self.jitter_enabled = _as_bool(cfg.get("jitter_enabled"), False)
        self.jitter_seconds = max(0, _as_int(cfg.get("jitter_seconds"), 7))
        # 用户 m05691 第 3 条：抖动最多叠几跳（默认 3 次 = 7+7+7），到顶就不再往后拖。
        self.jitter_max_rounds = max(1, _as_int(cfg.get("jitter_max_rounds"), 3))
        # 用户 m13212 后：必回（@ / 叫名字）默认跳过抖动——都被点名了还干等几秒，
        # 用户看到的就是「@ 完一秒多才动」。想让它也老实等完，把这个开关关掉。
        self.jitter_skip_must_go = _as_bool(cfg.get("jitter_skip_must_go"), True)
        # v4.39（用户 m20816）：决策也要能看图——以前决策的 user 消息是纯文字，图片
        # 只剩「[图片]」四个字，模型看不到。开关默认开。v4.40（用户 m21305）：张数
        # 不再单独设，跟回复同一个口径吃 history_images（默认 128）。
        self.decision_see_images = _as_bool(cfg.get("decision_see_images"), True)
        # 用户 m06995 第 2 项：回复那条链也有一套模型与看图开关（跟决策卡一一对应，
        # 少「决策失败时」）。模型留空 = 跟随框架默认；看图默认开。
        self.reply_model = str(cfg.get("reply_model") or "")
        self.reply_fallback_model = str(cfg.get("reply_fallback_model") or "")
        # 用户 m01994：思考等级留空 = 跟随模型自己的设置（不传 reasoning_effort）。
        self.reply_reasoning_effort = str(cfg.get("reply_reasoning_effort") or "").strip().lower()
        self.reply_fallback_reasoning_effort = str(cfg.get("reply_fallback_reasoning_effort") or "").strip().lower()
        self.decision_reasoning_effort = str(cfg.get("decision_reasoning_effort") or "").strip().lower()
        self.decision_fallback_reasoning_effort = str(cfg.get("decision_fallback_reasoning_effort") or "").strip().lower()
        # 用户 m08153：连续失败切换改成开关（阈值固定 2），默认开。
        self.reply_fail_switch = _as_bool(cfg.get("reply_fail_switch"), True)
        # 用户 m10202：冷却默认 30 分钟（存盘按秒，1800）。
        self.reply_fallback_cooldown = max(60, _as_int(cfg.get("reply_fallback_cooldown"), 1800))
        self.reply_see_images = _as_bool(cfg.get("reply_see_images"), True)
        # 用户 m09851：JEV 那个单独的「回退带图」开关撤了——「不确定 → 交给决策模型」
        # 那一次转交要不要带图，统一看 decision_see_images（设置页「窗口设置 · 图片」）。
        # v3.44（用户本轮第 2 条）：连发合并同样加开关，默认关——关着时同一人连发
        # 每条都照常计数；开了才按 burst_merge_seconds 合并。
        self.burst_merge_enabled = _as_bool(cfg.get("burst_merge_enabled"), False)
        self.burst_merge_seconds = max(0, _as_int(cfg.get("burst_merge_seconds"), 15))
        self.short_msg_chars = max(0, _as_int(cfg.get("short_msg_chars"), 5))

        # v3.33（用户 m51068 第 11 条）：字数阈值下线，换成「整条就是清单里的短语」
        # 才不计入触发——「哈哈」「草」照样能挡，但「好」「嗯」这种真在接话的
        # 短句不再被一起吞掉。
        self.phrase_filter_enabled = _as_bool(cfg.get("phrase_filter_enabled"), True)
        # 同 Q02：缺键用设置页默认黑名单，显式清空仍是 "[]"。
        raw_phrases = cfg.get("phrase_filter_list")
        self.phrase_filter_list = _normalize_phrases(
            _UI_DEFAULT_PHRASE_FILTER if raw_phrases is None else raw_phrases
        )

        # v3.33（第 6 条）：静默兜底等待时长重新开放成设置项，默认 3 分钟。
        self.silent_trigger_enabled = _as_bool(cfg.get("silent_trigger_enabled"), True)
        self.silent_trigger_minutes = max(1, _as_int(cfg.get("silent_trigger_minutes"), 3))
        # v3.33（第 10 条）：休息时段单独一套静默兜底，默认不开。
        self.rest_silent_enabled = _as_bool(cfg.get("rest_silent_enabled"), False)
        self.rest_silent_minutes = max(1, _as_int(cfg.get("rest_silent_minutes"), 3))
        # 用户 m02652：按消息类型分开过滤，每个类型一个开关，默认值按用户点名的来。
        self.silent_skip_video_link = _as_bool(cfg.get("silent_skip_video_link"), True)
        self.silent_skip_short = _as_bool(cfg.get("silent_skip_short"), True)
        # 用户 m04896 第 3 条：短句阈值默认 5 个字。
        self.silent_skip_short_len = max(1, _as_int(cfg.get("silent_skip_short_len"), 5))
        self.silent_skip_single_image = _as_bool(cfg.get("silent_skip_single_image"), False)
        self.silent_skip_single_sticker = _as_bool(cfg.get("silent_skip_single_sticker"), True)
        self.silent_skip_empty_forward = _as_bool(cfg.get("silent_skip_empty_forward"), True)
        # 用户 m04896 第 1 条：每个过滤项各自带「管前几条」。用户 m10685 把默认值调成
        # 链接 / 文件视频 9 条，转发 / 表情包 / 图片 / 短句 3 条（原来一律 1 条）。
        self.silent_skip_video_link_count = max(
            1, _as_int(cfg.get("silent_skip_video_link_count"), 9))
        self.silent_skip_short_count = max(
            1, _as_int(cfg.get("silent_skip_short_count"), 3))
        self.silent_skip_single_image_count = max(
            1, _as_int(cfg.get("silent_skip_single_image_count"), 3))
        self.silent_skip_single_sticker_count = max(
            1, _as_int(cfg.get("silent_skip_single_sticker_count"), 3))
        self.silent_skip_empty_forward_count = max(
            1, _as_int(cfg.get("silent_skip_empty_forward_count"), 3))
        # 用户 m04896 第 4 条：文件 / 视频消息也过滤，默认开。
        self.silent_skip_file_video = _as_bool(cfg.get("silent_skip_file_video"), True)
        self.silent_skip_file_video_count = max(
            1, _as_int(cfg.get("silent_skip_file_video_count"), 9))
        # 用户 m22683：正文里 @ 了别人（不是机器人）也算没什么可接，默认开、管前 3 条。
        self.silent_skip_at_other = _as_bool(cfg.get("silent_skip_at_other"), True)
        self.silent_skip_at_other_count = max(
            1, _as_int(cfg.get("silent_skip_at_other_count"), 3))
        # 用户 m04896 第 5 条：丢弃文件视频，默认开。
        self.file_skip_context = _as_bool(cfg.get("file_skip_context"), True)

        # v3.52（用户 m08226 第 5 条）：配额玩法收敛成「每小时 / 每天」两个开关，
        # 两个都能单独封顶（互斥是设置页的事，后端两个都认）。旧的 quota_enabled /
        # quota_mode / quota_ratio_percent 不再读，老配置里残留的值直接忽略。
        self.quota_hour_enabled = _as_bool(cfg.get("quota_hour_enabled"), False)
        self.quota_max_per_hour = max(1, _as_int(cfg.get("quota_max_per_hour"), 10))
        self.quota_day_enabled = _as_bool(cfg.get("quota_day_enabled"), False)
        self.quota_max_per_day = max(1, _as_int(cfg.get("quota_max_per_day"), 100))
        # v3.52（用户 m08226 第 6 条）：进阶模式先只当「退避的前置开关」，
        # 模型 + 决策词那部分还在做（ui_schema.json 里的 default 也是 false）。
        self.advanced_enabled = _as_bool(cfg.get("advanced_enabled"), False)
        # 用户 m20823 第 2 条：私聊单独一套「决策模式」默认值。私聊不走决策——它
        # 就在跟一个人说话，判「要不要开口」纯属多花一次调用；这里默认 false，
        # 想让某个私聊单独开，在设置页选中那个会话把开关拨开即可（写的是覆盖）。
        self.advanced_enabled_dm = _as_bool(cfg.get("advanced_enabled_dm"), False)

        # 用户 m06706：JEV 决策（接入的专用 decision 类模型，如 Jev）自己一整套配置。
        # 跟 advanced_enabled 互斥，但两套配置各存各的——切换开关不清空对方的值，
        # 用户想来回试随时能切回去。区间按百分比整数存（20 / 70），运行时再归一到 0~1。
        self.jev_enabled = _as_bool(cfg.get("jev_enabled"), False)
        self.jev_model = str(cfg.get("jev_model") or "").strip()
        self.jev_fallback_model = str(cfg.get("jev_fallback_model") or "").strip()
        self.jev_lower = _bounded_int("jev_lower", cfg.get("jev_lower"), int(round(DEFAULT_JEV_LOWER * 100)))
        self.jev_upper = _bounded_int("jev_upper", cfg.get("jev_upper"), int(round(DEFAULT_JEV_UPPER * 100)))
        self.jev_action = normalize_jev_action(cfg.get("jev_action"))
        self.jev_prompt = str(cfg.get("jev_prompt") or "")
        self.jev_persona = str(cfg.get("jev_persona") or "").strip()
        # 用户 m08123：接口来源（framework 宿主 decision 模型 / custom 自定义第三方接口）。
        # 地址与模型名按群可覆盖，密钥只全局一份。
        self.jev_backend = normalize_jev_backend(cfg.get("jev_backend"))
        # 原始值原样留着（不合法的地址、空模型名都别在这里悄悄洗掉）：
        # 运行时会回落到官方地址 / jev-latest，但自检要能看见用户到底写了什么。
        self.jev_api_base = str(cfg.get("jev_api_base") or "").strip()
        self.jev_api_model = str(cfg.get("jev_api_model") or "").strip()
        # 用户 m08755：地址后面接的是哪套协议（SystemOne / OpenAI 兼容）。
        self.jev_api_protocol = str(cfg.get("jev_api_protocol") or "").strip().lower()
        # 用户 m09981：自定义接口的页签列表（JSON 字符串，逐页协议 / 地址 / 模型名）。
        self.jev_apis = str(cfg.get("jev_apis") or "").strip()

        # Q02：缺键跟设置页一致（默认开）；显式存 False 仍然关。
        self.rest_mode_enabled = _as_bool(cfg.get("rest_mode_enabled"), True)
        self.rest_start = str(cfg.get("rest_start") or "23:00")
        self.rest_end = str(cfg.get("rest_end") or "08:00")
        # 默认 2（用户 m03616 第 4 条）；ui_schema.json 里 rest_probability 的
        # default 也必须是 2，两处一起改。
        self.rest_probability = _as_int(cfg.get("rest_probability"), 2)
        # v3.72（用户 m24260 第 2 条）：曲线模式自己那套休息时段，默认 23:00-09:00。
        # 画在曲线上，跟普通模式那两个时间点互不影响。
        # v3.73（用户 m24640 第 5 条）：曲线模式的休息时段改成段上的 rest 标记，
        # band_rest_enabled / _start / _end 三个键撤掉，只留一个概率。
        self.band_rest_probability = _as_int(cfg.get("band_rest_probability"), 2)

        # Q02：缺键跟设置页一致（默认关）；显式存 True 才开。
        self.backoff_enabled = _as_bool(cfg.get("backoff_enabled"), False)
        self.backoff_start_rejects = max(1, _as_int(cfg.get("backoff_start_rejects"), 2))
        self.backoff_base_seconds = max(1, _as_int(cfg.get("backoff_base_seconds"), 15))
        self.backoff_max_seconds = max(1, _as_int(cfg.get("backoff_max_seconds"), 300))
        self.backoff_bypass_count = max(1, _as_int(cfg.get("backoff_bypass_count"), 6))

        # 请求出错后的自动重试（用户 m30428 要求次数与间隔可配置）
        self.retry_max = max(0, _as_int(cfg.get("retry_max"), 2))
        self.retry_delay = max(0, _as_int(cfg.get("retry_delay"), 5))
        # 看门狗超时（用户 2026-10-24 第 4 条：放进「重试」里由用户自己设）。
        # 30-600 秒，ui_schema.json 里的 default 必须与这里的兜底值一致（都是 180），
        # 否则前端显示的默认值和实际生效的值会对不上。
        self.watchdog_seconds = max(30, min(600, _as_int(cfg.get("watchdog_seconds"), 180)))
        # 用户 m01587：整轮时长上限（默认 600 秒）。看门狗改成按「无进展」计时后，
        # 单步卡住由 watchdog_seconds 兜，整轮跑太久由这个总上限兜；60-3600 秒。
        self.watchdog_total_seconds = max(60, min(3600, _as_int(cfg.get("watchdog_total_seconds"), 600)))
        # 用户 m05550：重试的两个取舍开关，默认都关（＝保持原来的行为）。
        #  retry_take_pending：重试时把攒下的待发一起带进上下文（不积压，但可能跑题）。
        #  timeout_no_retry：看门狗超时后直接放弃、不再重试（3 分钟后再回也失真）。
        self.retry_take_pending = _as_bool(cfg.get("retry_take_pending"), False)
        self.timeout_no_retry = _as_bool(cfg.get("timeout_no_retry"), False)
        # 用户 m07157：用户黑名单总开关，默认开。关掉后名单还在，但不再剔除消息，
        # 窗口页也不显示「拉黑」按钮（前端读 /user-blacklist 返回的 enabled）。
        self.user_blacklist_enabled = _as_bool(cfg.get("user_blacklist_enabled"), True)

        self.window_keep = max(1, _as_int(cfg.get("window_keep"), 30))
        # v3.52（用户 m08226 第 7 条）：一直没触发请求时，窗口攒到「保留 + 缓冲」
        # 就强制裁一次；0 = 关掉这个强制裁切，只按阈值走。
        self.window_buffer = max(0, _as_int(cfg.get("window_buffer"), 10))
        # 图片缓存上限（用户 m10749 第 5 条）：插件替群友图片留的副本，**每个群组各自**
        # 留几张（用户本轮：「全局设置的是每个群组的上限 不是总的上限，总的上限=全局设置*
        # 群组数量」）。所以这个值不是整个缓存目录的天花板，实际总配额 = 本值 × 群组数，
        # 由 window/manager.py 的 _prune_media_cache 按群组桶分别裁。
        # 用户 m06046 图2：600 张（那时是全目录共享一份）两天就攒满，每次收新图都会顺手
        # 删掉一张旧的，记录里刚出现过的图跟着没。默认放到 1500，配合 2 小时的保护期。
        self.media_cache_limit = max(1, _as_int(cfg.get("media_cache_limit"), 1500))
        # 用户 m24364：其他图片（推文卡片图 tw_）也按每个群 / 私聊各自的上限留，
        # 默认 40 张；0 = 不限。tw_ 的归属写在 media_owners.json 里。
        self.media_other_limit = max(0, _as_int(cfg.get("media_other_limit"), 40))
        # 用户 m11091 第 11 条：图片缓存只留最近 N 天 / 最多 N MB，0 = 不限
        self.media_cache_keep_days = max(0, _as_int(cfg.get("media_cache_keep_days"), 30))
        # 用户 m24990：其他图片也补保留天数，默认 30 天；0 = 不限。
        self.media_other_keep_days = max(0, _as_int(cfg.get("media_other_keep_days"), 30))
        # v3.66（用户 m19536 第 5 条）：「缓存容量上限」这个总上限撤掉了——群聊默认 +
        # 私聊默认两套加起来就是总上限。配置里残留的旧值不再读取，属性留 0 给老代码兜底。
        self.media_cache_max_mb = 0
        # 只保留最近几张原图，更早的换成 [图片]。框架发图前会把超过 1MB 的图压缩，
        # 所以正常群聊几乎不会摸到上限；设 128 张和 MaiBot 的 max_image_num 一致，
        # 好处是历史内容几乎不变，前缀缓存不会因为「图被换掉」而断。
        self.history_images = max(0, _as_int(cfg.get("history_images"), 128))
        # v4.45（用户 m22611）：历史里的动图怎么发给模型，三档互斥——
        # 九帧拼图（默认）/ 原图发送 / 只读首帧；三个都不开＝跳过，图不发。
        # 判定顺序在 window/manager.py 的 _gif_mode 里（grid > original > first > skip）。
        self.history_gif_grid = _as_bool(cfg.get("history_gif_grid"), True)
        self.history_gif_original = _as_bool(cfg.get("history_gif_original"), False)
        self.history_gif_first = _as_bool(cfg.get("history_gif_first"), False)
        # 用户 2026-11-06：转发消息怎么进请求——off 不展开 / text 只给文字 /
        # on_demand 默认只挂前几张，模型需要时用 view_forward 看完整内容。
        self.forward_mode = str(cfg.get("forward_mode") or "on_demand").strip().lower()
        if self.forward_mode not in ("off", "text", "on_demand"):
            logger.warning(
                f"[multi-decision] 无法识别的 forward_mode: {self.forward_mode!r}，回退 on_demand")
            self.forward_mode = "on_demand"
        # 风险开关：打开后转发里的图全部发给模型，请求体积可能很大。
        self.forward_images_all = _as_bool(cfg.get("forward_images_all"), False)
        # v4.70（用户 m01237）：转发里的动图八成是表情包，默认整档不发——
        # 只丢动图，静态图照发，正文留 [图片] 占位；实时窗照旧显示。
        self.forward_drop_gif = _as_bool(cfg.get("forward_drop_gif"), True)
        self.window_trim_threshold = max(1, _as_int(cfg.get("window_trim_threshold"), 60))
        # v3.85（用户 m04418 第 2 条）：撤掉「重启后回填条数」这条设置——群聊窗口
        # 热重载由寄存处接回、私聊另有 window_dm.json 存档，都不需要从框架记忆里回填。
        # 冷启动一律不灌历史，见 window/manager.py 的 collect()。
        # 用户 m25921：保护工具锁死开启（见 _LOCKED_ON），配置里的旧值不再读取。
        self.protect_tool_groups = True
        # v4.92（用户 m13535、m13870）：工具结果默认照旧进实时窗——现在还不能一刀切，
        # 有些工具结果对后续判断有用。开关打开才整段摘掉，请求那边补全配对。
        self.window_drop_tools = _as_bool(cfg.get("window_drop_tools"), False)
        self.trim_strategy = str(cfg.get("trim_strategy") or "mechanical")
        self.summary_enabled = _as_bool(cfg.get("summary_enabled"), False)
        self.summary_max_chars = max(60, _as_int(cfg.get("summary_max_chars"), 300))
        self.summary_line_chars = max(8, _as_int(cfg.get("summary_line_chars"), 40))

        # 用户 m13250 第 5 条：设置里撤掉了「记录请求上下文」开关——
        # 请求快照本来就要一直记（跟请求记录一一对应，条数由 trace_keep 管）。
        # 用户 m13708 第 3/4 条：消息日志 / 窗口快照的保留天数又拉回设置页了，
        # 而且分开设（decisions_keep_days / windows_keep_days，见下面的新读取）。
        self.request_log_enabled = True

        # 动态段（时间/会话/记忆）默认挪到本轮消息之后。框架原本把这几段插在
        # 最后一条 user 消息前面，会让「上一轮正文」的位置每轮变化，前缀缓存
        # 从那里开始整段失效。挪到后面之后，上一轮的正文能完整命中。
        # v3.13（用户 m39983）：已锁定，恒开（见 _LOCKED_ON），配置里的旧值不再读取。
        self.dynamic_to_tail = True

        # 历史里机器人自己的消息前面加这个名字（形如「【桃华】」）。模型自己
        # 生成的 assistant 消息只有 <msg> 壳，和用户的「[时间] [昵称] | 正文」
        # 长得完全不一样，长对话里容易把自己说过的话当成别人说的。留空不加。
        self.bot_name = str(cfg.get("bot_name") or "").strip()
        # v3.46（用户 m05828 第 4 条）：管理员名字 / QQ 号。只用于身份段注入，
        # 不参与任何触发判定——谁说了算这件事交给模型自己看上下文。
        self.admin_name = str(cfg.get("admin_name") or "").strip()
        self.admin_id = str(cfg.get("admin_id") or "").strip()
        # v3.47（用户 m06761 第 7 条）：管理员可多位——设置页把整表存成 JSON 文本，
        # engine 用 eff("admin_list", sid) 读（单群可覆盖）。这里不接全局值的话，
        # 设置页填的全局管理员永远读不到，白填。
        self.admin_list = str(cfg.get("admin_list") or "").strip()
        self.bot_qq = str(cfg.get("bot_qq") or "").strip()
        # v3.11：QQ 号不用手填了。框架适配器配置里就有（bot_pid），留空时自动读；
        # 老配置里手填过的值优先，行为不变。
        self._bot_identity = _framework_bot_identity()
        if not self.bot_qq:
            self.bot_qq = str(self._bot_identity.get("qq") or "").strip()
        # 没手填就等于「自动」：每来一条消息用适配器里的真实昵称覆盖一次
        # （QQ 里别人看到的名字），人格名只当第一顺位兜底。
        self.bot_name_auto = not bool(self.bot_name)
        # 按会话记的真实昵称：群里是群名片、私聊是 QQ 昵称，全局单值会互相覆盖
        self.bot_names: dict = {}

        # 观察窗：v2.3 起停用。触发时本轮消息里本来就带着这批消息，历史里
        # 也有一份，再单独贴一遍纯属重复（实测一个群块 2778 字里一多半是它）。
        # 配置项保留只为兼容旧配置文件。
        self.observe_enabled = _as_bool(cfg.get("observe_enabled"), False)
        self.observe_messages = max(1, _as_int(cfg.get("observe_messages"), 30))
        self.observe_max_chars = max(200, _as_int(cfg.get("observe_max_chars"), 1500))

        self.engine = DecisionEngine(self)
        self.window = None
        self.request_log = None
        self.retry_history = None
        self.aliases = None
        self.hidden = None
        self.store = None
        self.overrides = None
        self.summaries = None
        # 会话登记簿：老会话照旧，之后第一次出现的会话默认关掉（v3.23）
        self.session_registry = None
        self._recent24_cache = None
        # 新会话默认关闭的保险丝：内部开关，不进 schema。测试要跑真实判定路径
        # 时把它关掉，线上保持默认开启。
        self.new_session_default_off = _as_bool(cfg.get("new_session_default_off"), True)
        self.tracer = None
        # API 价目：用户手填单价，概览卡按模型算钱（用户 m02080 第 4 条）
        self.pricing = None
        # 轨迹记录：把每一轮的完整请求、每步思考、工具往返、token 与缓存命中落盘。
        # 默认开——它就是给排障看的，不参与任何判定。
        # 用户 m11091 第 10 条：设置页里把「记录 agent 调用轨迹」这个开关拿掉了，
        # 后端恒为开——关掉它，窗口页的思考/工具调用块和请求页明细就没数据了。
        self.trace_enabled = True
        # 用户 m12007 第 2 条：保留口径只留一个数——请求页表格、走势图、明细（思考 / 工具调用）
        # 都按「每会话保留请求数」走，request_log 也吃这个数（下面几行）。原来两个键默认
        # 60 / 100 各说各话，页面上写着「最近 100 次请求」而明细只留 60 轮；旧键
        # request_log_rounds 已从 schema 与设置页删除，配置里残留的旧值不再读取。
        self.trace_keep = max(1, _as_int(cfg.get("trace_keep"), 100))
        self.request_log_rounds = self.trace_keep
        # v3.66（用户 m19536 第 4 条）：上限拆成「群聊默认 / 私聊默认」。
        # v3.67（用户 m20117）：私聊默认改成写死的 200 张 / 50 份——不再按「群聊的两成」
        # 现算，用户在自己页面上填过这三个值（600/200、100/50、100/20）并要求「以后这就是默认值」。
        self.media_cache_limit_dm = _bounded_int(
            "media_cache_limit_dm", cfg.get("media_cache_limit_dm"), 200
        )
        # 用户 m24364：其他图片的私聊默认上限，同样是 40 张。
        self.media_other_limit_dm = _bounded_int(
            "media_other_limit_dm", cfg.get("media_other_limit_dm"), 40
        )
        # v4.90（用户 m01875 第 1 条）：私聊的显示条数跟群聊一样默认 100。
        self.trace_keep_dm = _bounded_int(
            "trace_keep_dm", cfg.get("trace_keep_dm"), 100
        )
        # 用户 m13708 第 3/4 条：每一类缓存的保留天数都做成设置项，语义统一
        # 「0 = 不限」（消息日志/窗口快照按天文件，至少要留 1 天）。默认值和以前
        # 的实际行为对齐，升级后不会突然删数据。
        # v3.66（用户 m19536 第 1 条）：保留天数默认统一 56 天（0 仍可手动填 = 不限）
        # 2026-11-12（用户 m09879）：窗口快照的保留天数默认改成 30 天。
        self.requests_keep_days = _bounded_int(
            "requests_keep_days", cfg.get("requests_keep_days"), 30
        )
        # v3.64（用户 m18870）：轨迹并进请求快照，保留天数只剩 requests_keep_days 一条线
        # v3.66（用户 m19536 第 1/3 条）：所有保留天数默认统一 56 天；消息日志是自适应的
        # 原料，_BOUNDS 里把它锁在 56 以上，只能往高调。
        self.decisions_keep_days = _bounded_int(
            "decisions_keep_days", cfg.get("decisions_keep_days"), 56
        )
        self.windows_keep_days = _bounded_int(
            "windows_keep_days", cfg.get("windows_keep_days"), 56
        )
        # 用户 m18059 第 3 条：窗口快照原本只有天数线，加一条份数线。0 = 不限。
        # v3.67（用户 m20117）：份数线默认群聊 100 / 私聊 20（用户自己填的那组值）。
        # v4.00（用户 m10782）：窗口快照现在既有回复轮、也有决策轮（决策接管这一轮
        # 也会记一条窗口），份数线从 100 / 20 提到 300 / 60——不然一天就被滚掉。
        # v4.90（用户 m01133 第 2 条）：回复 / 裁切 / 决策三种快照合成一类，份数线只剩
        # 这一条（以前三堆各按 100 / 200 / 300 裁，加起来跟走势图对不上）。
        # v4.90（用户 m01875 第 1 条）：盘上留多少份跟页面上显示多少条分开——上限默认
        # 300，显示默认 200（见下面的 window_show），多出来的那 100 份只当备份。
        self.windows_keep = _bounded_int(
            "windows_keep", cfg.get("windows_keep"), 300
        )
        self.windows_keep_dm = _bounded_int(
            "windows_keep_dm", cfg.get("windows_keep_dm"), 60
        )
        # v4.90（用户 m01875 第 1 条）：走势图和快照列表最多画多少根。0 = 不限。
        self.window_show = _bounded_int(
            "window_show", cfg.get("window_show"), 200
        )
        self.window_show_dm = _bounded_int(
            "window_show_dm", cfg.get("window_show_dm"), 200
        )
        # v4.00（用户 m10782）：决策快照单独一类缓存，份数线自己一套。
        # v4.90（用户 m01875 第 1 条）：它现在只是「决策记录页列多少条」，默认 100 / 100。
        self.decision_keep = _bounded_int(
            "decision_keep", cfg.get("decision_keep"), 100
        )
        self.decision_keep_dm = _bounded_int(
            "decision_keep_dm", cfg.get("decision_keep_dm"), 100
        )
        # 中期摘要（用户第 5 条：早期记录可以先清掉）：最多留多少个会话的摘要，
        # 以及最多留多少天，0 = 不限。
        # v4.84（用户 m09179 第 9 条）：这三项挪到「缓存记录」页，默认值改成
        # 群聊 150 / 私聊 50 / 保留 30 天（原来是 0 / 0 / 56，等于不限）。
        self.summaries_keep = _bounded_int(
            "summaries_keep", cfg.get("summaries_keep"), 150
        )
        self.summaries_keep_days = _bounded_int(
            "summaries_keep_days", cfg.get("summaries_keep_days"), 30
        )
        self.summaries_keep_dm = _bounded_int(
            "summaries_keep_dm", cfg.get("summaries_keep_dm"), 50
        )
        # v4.54（用户 m18227）：这几类缓存也进「缓存记录」，上限按每个群 / 私聊各自算，
        # 默认值取最近实测用量的约 4 倍；0 = 不限。
        # 用户 m10466：转发图片和原图记录合成一份，上限 / 天数只留一套。
        self.forward_media_keep = _bounded_int(
            "forward_media_keep", cfg.get("forward_media_keep"), 120
        )
        self.retry_keep = _bounded_int(
            "retry_keep", cfg.get("retry_keep"), 120
        )
        # 用户 m19210：私聊默认按群聊的四分之一给，和群聊默认一起组成两套值；
        # 私聊刷得慢、转发也少，这个量够用，又不会让每个私聊都占一大份。
        self.forward_media_keep_dm = _bounded_int(
            "forward_media_keep_dm", cfg.get("forward_media_keep_dm"), 30
        )
        self.retry_keep_dm = _bounded_int(
            "retry_keep_dm", cfg.get("retry_keep_dm"), 30
        )
        # 用户 m19940：两类新缓存也加 30 天保留线；0 = 不限。
        self.forward_media_keep_days = _bounded_int(
            "forward_media_keep_days", cfg.get("forward_media_keep_days"), 30
        )
        self.retry_keep_days = _bounded_int(
            "retry_keep_days", cfg.get("retry_keep_days"), 30
        )
        # 会话统计（自适应阈值）文件保留天数。v3.66（用户 m19536 第 1/3 条）：
        # 默认 56 天、下限锁 56，和消息日志一样只能往高调。
        # 2026-11-18（用户 m13966）：消息日志和会话统计合成一行，保留天数只剩
        # decisions_keep_days 一个键；老配置里的 stats_keep_days 当别名读进来，
        # 取两者较大值——别把用户已经调高的天数降回去。
        self.stats_keep_days = _bounded_int(
            "stats_keep_days", cfg.get("stats_keep_days"), 56
        )
        if self.stats_keep_days > self.decisions_keep_days:
            self.decisions_keep_days = self.stats_keep_days
        self.stats_keep_days = self.decisions_keep_days
        self._data_dir = "data"
        self._log = None
        self._window_log = None
        self.stats = None

    async def initialize(self):
        try:
            data_dir = self.ctx.get_plugin_data_dir()
        except Exception:
            # 取不到会话数据目录时退回框架的数据根（跟 _disk_usage 同一口径），
            # 不能落在相对 "data"——那会把运行数据写进插件自己的目录里。
            try:
                from core.utils.path_utils import get_data_path

                data_dir = os.path.join(str(get_data_path()), "plugin_data", PLUGIN_ID)
            except Exception:
                # 最后一道兜底也用绝对路径：相对 "data" 会落在进程工作目录里，
                # 换机器/换启动方式就写到别处去了（审查报告）。
                import tempfile

                data_dir = os.path.join(tempfile.gettempdir(), PLUGIN_ID, "data")
        self._data_dir = data_dir
        # bot 自己的名字：设置里没填就自动用当前人格名兜底，真正跑起来以后
        # 会被适配器的真实昵称覆盖（见 engine._handle_message）。用来在历史里给
        # assistant 消息加「【名字】」前缀，免得模型把自己说过的话当成别人说的
        if not self.bot_name:
            try:
                persona = await self.ctx.persona_mgr.get_active_persona()
                name = str(getattr(persona, "name", "") or "").strip()
                if name:
                    self.bot_name = name
                    logger.info(f"[multi-decision] bot 名字自动取人格名：{name}")
                else:
                    logger.warning(f"[multi-decision] 人格里没有名字（{persona!r}），assistant 不加前缀")
            except Exception as exc:
                logger.warning(f"[multi-decision] 取人格名失败：{exc}")
        self.aliases = Aliases(data_dir)
        self.hidden = Hidden(data_dir)
        # 机器人各会话的昵称（群里是群名片、私聊是 QQ 昵称）落盘，热重载后不丢
        self.bot_names_store = Aliases(data_dir, filename="bot_names.json")
        saved_names = self.bot_names_store.all()
        if saved_names:
            self.bot_names = dict(saved_names)
            logger.info(f"[multi-decision] 已恢复 {len(saved_names)} 个会话的机器人昵称")
        self.overrides = Overrides(data_dir)
        # 用户 m06193：用户黑名单（按 QQ 号拦，命中的新消息不进请求上下文）。
        # 存插件自己的数据目录，不写框架配置——设置页保存会覆盖框架内存里没有的键。
        self.user_blacklist = UserBlacklist(data_dir)
        # 用户 m07157：用户黑名单总开关（self.user_blacklist_enabled，在 __init__ 里读配置）。
        # 用户 m13708 第 4/5 条：摘要也按「会话数上限 + 保留天数」剪
        self.summaries = Summaries(
            data_dir,
            keep=self.summaries_keep,
            keep_days=self.summaries_keep_days,
            keep_dm=self.summaries_keep_dm,
        )
        self.store = StateStore(data_dir)
        # 用户 m13708 第 3 条：消息日志与窗口快照各自独立的保留天数。
        # 用户 m18059 第 3/4 条：清理时按「这个会话」的有效设置裁（单群覆盖 > 全局），
        # 所以把 eff 包成回调传进去；拿不到会话时回退实例值。
        self._log = DecisionLog(
            data_dir,
            self.decisions_keep_days,
            days_of=lambda sid: self.eff("decisions_keep_days", sid),
        )
        self._window_log = DecisionLog(
            data_dir,
            self.windows_keep_days,
            subdir="windows",
            keep_of=lambda sid: self.eff("windows_keep", sid),
            days_of=lambda sid: self.eff("windows_keep_days", sid),
        )
        # v3.71（用户 m23596 第 5/8 条）：动态调整要按时段算平均消息 / 触发 / 回复，
        # 数据源就是上面这份消息日志；调过的概率单独落 band_tune.json，不写回配置。
        self.band_stats = BandStats(self._log)
        self.band_tune = BandTuner(
            os.path.join(str(data_dir), "band_tune.json"), self.band_stats
        )
        # v3.79（用户 m02095）：节奏器。动态调整不再每条独立掷骰，按「这次该隔几条」
        # 排队（state/band_rhythm.py），进度单独落 band_rhythm.json。
        self.band_rhythm = BandRhythm(
            os.path.join(str(data_dir), "band_rhythm.json")
        )
        # 窗口恢复：群聊走寄存处（_keep.states，热重载接回、真重启丢），私聊另有
        # window_dm.json 存档（用户 m06348 第 6 条）。冷启动不从框架记忆回填旧历史。
        # 用户 m18819：私聊窗口存档不设个数上限，有多少私聊就留多少。
        self.window = WindowManager(
            self,
            window_log=self._window_log,
            summaries=self.summaries,
        )
        self.request_log = RequestLog(
            data_dir,
            self.request_log_rounds,
            self.request_log_enabled,
            keep_days=self.requests_keep_days,
            keep_of=lambda sid: self.eff("trace_keep", sid),
            days_of=lambda sid: self.eff("requests_keep_days", sid),
            # v4.00（用户 m10782）：决策快照与回复快照同目录，份数线分开算
            dec_keep=self.decision_keep,
            dec_keep_of=lambda sid: self.eff("decision_keep", sid),
            # v4.78（用户 2026-11-07 第 3 条）：裁切快照不动决策/回复，属窗口快照那一类，
            # 份数线跟窗口日志共用 windows_keep，不再跟回复快照抢名额。
            trim_keep_of=lambda sid: self.eff("windows_keep", sid),
        )
        # v4.87（用户 m11237 第 4 条）：裸链接卡片的真实标题，抓一次落盘缓存，
        # 之后都走缓存；只认 B 站域名，抓不到就保持卡片上的通用文案。
        self.link_titles = LinkTitles(data_dir)
        # 用户 m18492：github.com 的裸链接画成仓库卡，描述 / star / 主语言由后端补；
        # 只认 github.com 与 gist.github.com，抓不到就只留 owner/repo 的骨架。
        self.github_repos = GitHubRepos(data_dir)
        self.retry_history = RetryHistory(data_dir)
        # v4.54（用户 m18227）：按会话缓冲的缓存把「有效上限」注入各自的裁剪点。
        # 单群覆盖 > 全局默认；0 = 不限。
        # 用户 m10466：转发图片和原图记录合成一份，只注入一套。
        try:
            set_forward_media_limit_fn(
                lambda sid: self.eff("forward_media_keep", sid))
            set_forward_media_keep_days_fn(
                lambda sid: self.eff("forward_media_keep_days", sid))
            self.retry_history.set_limit_fn(
                lambda sid: self.eff("retry_keep", sid))
            self.retry_history.set_keep_days_fn(
                lambda sid: self.eff("retry_keep_days", sid))
        except Exception:
            logger.exception("[multi-decision] 注入缓存上限失败（已忽略）")
        # 用户 m14353：启动时回收一次没人引用的转发图。裁索引只 pop 条目、不删文件，
        # 以前每裁一次就留一批孤儿，实测堆了 1,077 张 / 720 MB。
        try:
            forward_media_gc()
        except Exception:
            logger.exception("[multi-decision] 回收孤儿转发图失败（已忽略）")
        # 用户 2026-11-06：转发图默认只挂前几张，模型需要时用 view_forward 看完整内容。
        try:
            set_forward_media_fn(forward_media_for_mid)
            set_forward_policy_fn(lambda: (
                str(getattr(self, "forward_mode", "on_demand") or "on_demand"),
                bool(getattr(self, "forward_images_all", False)),
                bool(getattr(self, "forward_drop_gif", True)),
            ))
        except Exception:
            logger.exception("[multi-decision] 注入转发图策略失败（已忽略）")
        self.engine.retry_history = self.retry_history
        self.tracer = Tracer(
            data_dir,
            enabled=self.trace_enabled,
            keep=self.trace_keep,
            keep_of=lambda sid: self.eff("trace_keep", sid),
        )
        # v3.64（用户 m18870）：轨迹并进请求快照。tracer 落盘时要靠 RequestLog 找到
        # 这一轮的文件（同一个 event_id），所以初始化完就把读写端注入进去。
        self.tracer.store = self.request_log
        self.pricing = Pricing(data_dir)
        # 用户 m22269 第 3 条：渠道名第一次认出来就记下来，供应商那边删了模型也不丢。
        self.provider_bind = ProviderBindings(data_dir)
        self.stats = AdaptiveStats(
            data_dir,
            self.stats_window_days,
            0,
            weekday_bucket=self.adaptive_weekday_bucket,
            weekday_window_days=self.adaptive_weekday_window_weeks * 7,
            keep_days=self.stats_keep_days,
        )
        # 用户 m13708 第 4 条：开机按 stats_keep_days 剪一次旧会话统计。放在初始化里，
        # 不挂在读接口上——用户打开概览页不该变成偷偷删文件。
        try:
            dropped = self.stats.prune_files(force=True)
            if dropped:
                logger.info(f"[multi-decision] 按保留天数清理了 {dropped} 个会话统计文件")
        except Exception as exc:
            logger.warning(f"[multi-decision] 清理会话统计文件失败：{exc}")
        # v3.23（用户 m45332 第6条）：会话登记簿。开机时把盘上现存的会话全登记成
        # 老会话（保持用户原来的开关），之后第一次出现的 sid 才默认关掉——避免
        # 被陌生人拉进新群、或者陌生人私聊时直接开始烧 token。
        self.session_registry = SessionRegistry(data_dir)
        try:
            seed = set()
            for src in (
                (self.overrides.all() if self.overrides else {}),
                (self.aliases.all() if self.aliases else {}),
                (self.bot_names or {}),
            ):
                seed.update(
                    normalize_sid(str(sid)) for sid in (src or {}) if sid
                )
            for getter in (
                (self.request_log.list_sessions() if self.request_log else []),
                (self.window.list_states() if self.window else []),
            ):
                seed.update(
                    normalize_sid(str(x.get("sid") or ""))
                    for x in (getter or [])
                    if x.get("sid")
                )
            added = self.session_registry.bootstrap(seed)
            logger.info(
                f"[multi-decision] 会话登记簿：老会话 {len(self.session_registry.known)} 个"
                f"（本次登记 {added} 个；之后的新会话默认关闭）"
            )
        except Exception:
            logger.exception("[multi-decision] 登记已有会话失败（已忽略）")
        self.engine.log = self._log
        self.engine.window = self.window
        self.engine.request_log = self.request_log
        self.engine.stats = self.stats
        self.engine.band_stats = self.band_stats
        self.engine.band_tune = self.band_tune
        self.engine.band_rhythm = self.band_rhythm
        # 进阶决策（v3.91，用户 m05749）：两段式——先让模型看实时窗口决定回不回，
        # 判「回」才走完整的带系统提示词那一轮。
        self.decision_phase = DecisionPhase(self)
        # 用户 m06706：JEV 那条链（decision 类模型走 choice()）跟普通决策并列，互相独立。
        self.jev_phase = JevPhase(self)
        # v3.92（用户 m06951 第 6 条）：聊天规则不再塞进决策提示词，改成插进回复那一轮
        # 的系统提示词（人格段后面），这一份对象顺便缓存最近一次的原文给设置页预览。
        self.reply_prompt = ReplyPrompt(self)
        self.prompt_layouts = PromptLayouts(self, data_dir)
        self.engine.store = self.store
        self.engine.overrides = self.overrides
        # 热重载会清空内存状态，这里把上次的计数接上
        try:
            self.engine.load_states()
        except Exception:
            logger.exception("[multi-decision] 恢复会话状态失败（已忽略）")
        # 用户 m00954：触发器模板寄存在 core 包上（进程级，重载不丢），重载后先接回内存，
        # 免得重载后的第一次静默轮因为内存里没有「上一条」而白等一条新消息。
        try:
            self.engine.rehydrate_triggers()
        except Exception:
            logger.exception("[multi-decision] 接回触发器模板失败（已忽略）")

        self._takeover_builtin_chat()
        try:
            self.engine.start_silent_loop()
        except Exception:
            logger.exception("[multi-decision] 静默触发启动失败（已忽略）")
        # v3.52（用户 m08226 第 7 条）：裁切缓冲巡检（一直不触发也要裁窗口）
        try:
            self.engine.start_buffer_loop()
        except Exception:
            logger.exception("[multi-decision] 缓冲裁切巡检启动失败（已忽略）")

        logger.info(
            f"[multi-decision] initialized (enabled={self.enabled}, "
            f"decision={self.decision_enabled}, window={self.window_enabled}, "
            f"mode={self.trigger_mode}, N={self.trigger_threshold}, "
            f"jitter={('%ss' % self.jitter_seconds) if self.jitter_enabled else 'off'}, "
            f"burst={('%ss' % self.burst_merge_seconds) if self.burst_merge_enabled else 'off'}, "
            f"keep={self.window_keep}, trim_at={self.window_trim_threshold}, "
            f"protect_tools={self.protect_tool_groups}, request_log={self.request_log_enabled}, "
            f"quota={'hour:%s' % self.quota_max_per_hour if self.quota_hour_enabled else ''}"
            f"{'+' if self.quota_hour_enabled and self.quota_day_enabled else ''}"
            f"{'day:%s' % self.quota_max_per_day if self.quota_day_enabled else ''}"
            f"{'' if (self.quota_hour_enabled or self.quota_day_enabled) else 'off'}, "
            f"weekday_bucket={self.adaptive_weekday_bucket})"
        )
        self._log_hook_selftest()

    def _log_hook_selftest(self):
        """启动自检：确认框架真的收到了本插件的钩子。

        框架在 import 时按「模块所在目录的 manifest.json」反查 plugin_id。
        反查失败的话钩子会挂到一个空 id 下，插件看着加载成功却从不触发——
        这种故障在日志里只表现为「Registered 1 hooks」，很难看出来。
        """
        try:
            from core.plugin import registry as _registry
        except Exception:
            return
        try:
            comps = getattr(_registry, "_plugin_components", {}) or {}
            found = None
            for pid, comp in comps.items():
                hooks = list(getattr(comp, "hooks", []) or [])
                if any(
                    "MultiDecisionPlugin" in getattr(h.handler, "__qualname__", "")
                    for h in hooks
                ):
                    found = (pid, hooks)
                    break
            if found is None:
                logger.warning("[multi-decision] 自检：框架里没找到本插件的钩子")
                return
            pid, hooks = found
            names = sorted(
                str(getattr(h.event_type, "value", h.event_type)) for h in hooks
            )
            logger.info(
                f"[multi-decision] 自检：plugin_id={pid}，已注册 {len(hooks)} 个钩子 {names}"
            )
            if len(hooks) < 3:
                logger.warning(
                    "[multi-decision] 自检：钩子数少于预期的 3 个，部分功能不会生效"
                )
        except Exception:
            logger.exception("[multi-decision] 钩子自检失败（已忽略）")

    async def terminate(self):
        try:
            if self.store is not None and self.engine is not None:
                self.store.flush(self.engine._persist_data())
        except Exception:
            logger.exception("[multi-decision] 关闭前保存会话状态失败")
        try:
            await self.engine.shutdown()
        except Exception:
            logger.exception("[multi-decision] 关闭决策引擎失败")
        # M05：四个组件逐个收尾。以前共用一个 try，前一个抛错后面全跳过——
        # 窗口没存、统计没落、节奏没刷，日志里却只有一句「关闭窗口模块失败」。
        for name, step in (
            ("窗口模块", lambda: self.window.shutdown() if self.window is not None else None),
            ("请求记录", lambda: self.request_log.shutdown() if self.request_log is not None else None),
            ("自适应统计", lambda: self.stats.shutdown() if self.stats is not None else None),
            ("节奏队列", lambda: self.band_rhythm.flush() if self.band_rhythm is not None else None),
        ):
            try:
                step()
            except Exception:
                logger.exception(f"[multi-decision] 关闭{name}失败（继续收尾其它组件）")
        self._release_builtin_chat()
        logger.info("[multi-decision] terminated")

    def _release_builtin_chat(self):
        """插件被停用/卸载时把 default-chat 放开，避免群里彻底没人回。

        只恢复「我们置的位」：如果加载时 default-chat 本来就是 _terminating=True
        （它自己被用户停用），那不是我们干的，动了等于擅自把它激活。
        """
        if self._takeover_toggled:
            self._release_toggle()
            return
        if not self._takeover_owned:
            return
        owned = self._takeover_instance
        self._takeover_instance = None
        self._takeover_owned = False
        try:
            instance = self.ctx.get_plugin_inst("default-chat")
        except Exception:
            instance = None
        if instance is None:
            return
        if owned is None or instance is not owned:
            logger.info(
                "[multi-decision] default-chat 已不是我们接管的那个实例，不放开新实例"
            )
            return
        try:
            instance._terminating = False
            logger.info("[multi-decision] 已放开 default-chat（_terminating=False）")
        except Exception:
            logger.exception("[multi-decision] 放开 default-chat 失败")

    def _takeover_builtin_chat(self):
        """让内置 default-chat 停手，避免它自己的 debounce 提前 flush。

        default-chat 的所有策略调用都不带 force，本插件的 buffer(force=True)
        能覆盖它的决定；但它被 @ 时会自己起 1.5~2 秒 debounce 并主动 flush，
        会打断本插件的抖动节奏，所以直接让它短路。
        """
        if not self.enabled or not self.decision_enabled:
            logger.info("[multi-decision] 决策未启用，不接管 default-chat")
            return
        if not self.takeover_default_chat:
            logger.info("[multi-decision] 未开启接管，default-chat 仍在工作（可能双触发）")
            return
        try:
            instance = self.ctx.get_plugin_inst("default-chat")
        except Exception:
            instance = None
        if instance is None:
            logger.info("[multi-decision] 未找到 default-chat 实例（可能已停用）")
            return
        if not hasattr(instance, "_terminating"):
            # 3.2.1 起框架可能不再用这个私有标志。私有接管失效时记 ERROR，
            # 再退到官方插件开关兜底——那条路会持久化，只在彻底没辙时才走。
            logger.error(
                "[multi-decision] default-chat 没有 _terminating 标志，私有接管失效，改用官方插件开关兜底"
            )
            self._takeover_via_toggle()
            return
        if getattr(instance, "_terminating", False):
            logger.info("[multi-decision] default-chat 已停用，无需接管")
            return
        try:
            instance._terminating = True
            self._takeover_owned = True
            self._takeover_instance = instance
            self._takeover_checked_at = time.time()
            logger.info("[multi-decision] 已接管 default-chat（置 _terminating=True）")
        except Exception:
            logger.exception("[multi-decision] 接管 default-chat 失败")

    def _takeover_via_toggle(self) -> bool:
        """私有标志消失时的兜底：用官方 set_plugin_enabled 停掉 default-chat。

        它会 _save_plugin_state() 持久化，重启后仍是禁用状态，所以只在
        _terminating 彻底不可用时才走这条路，并且记下是我们关的，
        停用本插件时再打开。
        """
        if self._takeover_toggled:
            return True
        if not compat.has_plugin_toggle():
            logger.error(
                "[multi-decision] 框架也没有 set_plugin_enabled，无法接管 default-chat"
            )
            return False
        mgr = getattr(self.ctx, "plugin_mgr", None)
        setter = getattr(mgr, "set_plugin_enabled", None)
        if not callable(setter):
            logger.error("[multi-decision] ctx.plugin_mgr 不可用，无法接管 default-chat")
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("[multi-decision] 当前没有事件循环，无法停用 default-chat")
            return False
        self._takeover_task = loop.create_task(setter("default-chat", False))
        self._takeover_toggled = True
        logger.info("[multi-decision] 已用官方开关停用 default-chat")
        return True

    def _release_toggle(self) -> None:
        """把 _takeover_via_toggle 关掉的 default-chat 重新打开。"""
        self._takeover_toggled = False
        mgr = getattr(self.ctx, "plugin_mgr", None)
        setter = getattr(mgr, "set_plugin_enabled", None)
        if not callable(setter):
            logger.error("[multi-decision] ctx.plugin_mgr 不可用，无法恢复 default-chat")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("[multi-decision] 当前没有事件循环，无法恢复 default-chat")
            return
        self._takeover_task = loop.create_task(setter("default-chat", True))
        logger.info("[multi-decision] 已用官方开关恢复 default-chat")

    def ensure_takeover(self):
        """自愈：default-chat 被重载时 initialize() 会把 _terminating 复位。

        每 10 秒最多复查一次，发现被复位就重新置位，避免它的 debounce 抢跑。
        """
        if not self.enabled or not self.decision_enabled or not self.takeover_default_chat:
            return
        now = time.time()
        if now - self._takeover_checked_at < 10.0:
            return
        self._takeover_checked_at = now
        try:
            instance = self.ctx.get_plugin_inst("default-chat")
        except Exception:
            return
        if instance is None:
            return
        if not hasattr(instance, "_terminating"):
            if not self._takeover_toggled:
                logger.error(
                    "[multi-decision] default-chat 没有 _terminating 标志，改用官方插件开关兜底"
                )
                self._takeover_via_toggle()
            return
        if getattr(instance, "_terminating", False):
            return
        try:
            instance._terminating = True
            self._takeover_owned = True
            self._takeover_instance = instance
            logger.info("[multi-decision] default-chat 被复位，已重新接管")
        except Exception:
            logger.exception("[multi-decision] 重新接管 default-chat 失败")

    # ------------------------------------------------------------ 钩子

    @on.im_message(priority=Priority.HIGH)
    async def _on_im_message(self, event):
        await self.engine.handle_message(event)

    # 用户 m00536：必须排在别的插件的 MEDIUM 钩子之后。nai 这类工具插件是在
    # llm_request 里把 "<工具>_completion" 挂进 user_prompt 的，同优先级时顺序
    # 取决于插件加载顺序——进程重启后 multi-decision 可能先加载，就看不到
    # completion，Notice 被摘掉而工具回调整条丢失。
    # 框架里 priority 越大越先跑（handlers.py 用 sort(reverse=True)），所以这里
    # 用 LOW：排在所有 MEDIUM 之后；LOW 档其余钩子（nai.ensure_tools、
    # agent.filter_tools、session_tools）只动 tool_set / system_prompt，
    # 不会往 user_prompt 里加东西，所以排在它们前面也不会漏内容。
    @on.llm_request(priority=Priority.LOW)
    async def _on_llm_request(self, event, request, tag_set):
        await self.engine.guard_reply_request(event)
        # 用户 m13442 第 4 条：这一轮回复的代理参数由插件接管——框架在 ExecuteAgentStage
        # 与工具循环里现读这四项，都在本钩子之后、同一个任务里，所以在这里塞进 contextvar。
        try:
            self._apply_agent_limits(self._sid_of(event))
        except Exception:
            logger.exception("[multi-decision] 接管代理设置失败（沿用框架值）")
        # 仅抄原模板，不渲染尚未绑定的 format；素材严格按当前 sid 隔离。
        layouts = getattr(self, "prompt_layouts", None)
        if layouts is not None:
            layouts.capture_native(request, self._sid_of(event))
            # 用户 m00998 第 3 条：框架在 assemble 阶段才填 format 段的标签清单，那时插件已装配完；
            # 这里趁 tag_set 已含内置标签先抄一份，设置页预览就能显示真内容。
            try:
                layouts.remember_tag_set(tag_set)
            except Exception:
                logger.debug("[multi-decision] 记标签清单失败（已吞掉）", exc_info=True)
        await self.engine.handle_llm_request(event, request, tag_set)
        # 放在 engine 之后：这时 messages 已经换成窗口快照、system 已修好、
        # 群标识也剥过了，记下来的才是模型真正收到的样子。
        # v3.91（用户 m05856）：进阶决策的工具表与身份环境都取自真实请求——框架没有
        # 「列出所有工具」的接口，就把每一轮真正发出去的那份抄下来。
        try:
            self.decision_phase.note_tool_set(getattr(request, "tool_set", None))
            self.decision_phase.note_system_prompt(getattr(request, "system_prompt", None))
        except Exception:
            logger.debug("[multi-decision] 记决策素材失败（已吞掉）", exc_info=True)
        # v3.92（用户 m06951 第 6 条）：把聊天规则插在人格段后面——它属于「回复提示词」，
        # 不参与「回不回」的判断，所以从决策那一份里撤掉了。插完顺手抄一份给设置页预览。
        try:
            if getattr(self, "prompt_layouts", None) is None:
                self.reply_prompt.apply(request, self._sid_of(event))
        except Exception:
            logger.debug("[multi-decision] 注入聊天规则失败（已吞掉）", exc_info=True)
        # 用户 m11103：回复链挂哪些工具也由提示词页的勾选决定——以前这里是框架全量，
        # 勾选只改了「工具说明」那段文字，勾掉工具照样能调。
        try:
            self._apply_reply_tools(request, self._sid_of(event))
        except Exception:
            logger.exception("[multi-decision] 回复工具按勾选注入失败（保留框架全量）")
        await self._trace_begin(event, request)

    def _apply_reply_tools(self, request, sid: str = "") -> None:
        """把这一轮回复请求的 tool_set 裁成提示词页勾的那份。

        还没在提示词页勾过（None）就原样不动：保持框架全量，等用户第一次打开那一页
        时把当时的全部工具勾上（默认全选），之后才真正按名单来。
        全不勾＝一个都不给（用户 m11103 定的口径，不回退全量）。
        用户 m11533：框架里新冒出来的工具（新装的插件带的）自动勾上，不用手动点。
        """
        layouts = getattr(self, "prompt_layouts", None)
        if layouts is None:
            return
        # 这一轮框架本来要挂的工具名——新出现的会被 reply_tools_for 自动补进勾选。
        avail = []
        for tool in (getattr(getattr(request, "tool_set", None), "tools", None) or []):
            name = str(getattr(tool, "name", "") or "").strip()
            if name:
                avail.append(name)
        try:
            names = layouts.reply_tools_for(avail)
        except Exception:
            logger.exception("[multi-decision] 回复工具清单自动补勾失败（按已存的来）")
            names = layouts.reply_selected_tools()
        # 裁剪逻辑放在 reply_prompt.apply_reply_tools，离线用例能直接验（用户 m11103）。
        got = apply_reply_tools(request, names)
        if not got:
            return
        kept, dropped = got
        logger.info("[multi-decision] %s 回复工具按勾选注入：挂 %d 个（%s），拿掉 %d 个（%s）",
                    sid or "?", len(kept), ",".join(kept) or "-",
                    len(dropped), ",".join(dropped) or "-")

    @on.llm_response(priority=Priority.SYS_HIGH)
    async def _on_llm_response(self, event, llm_resp):
        await self.engine.guard_reply_response(event, llm_resp)
        self._touch_reply_progress(event)
        # 先记轨迹：轨迹里留模型的原样输出，收口之后也能看出是它自己写了两遍。
        if self.tracer is not None:
            try:
                self.tracer.step(event, llm_resp)
            except Exception:
                logger.exception("[multi-decision] 轨迹记录（响应）异常（已吞掉）")
        # 用户 m10400 图7：这一轮调过工具的话先记下响应对象，结算时把「调用 + 结果」
        # 补进实时窗（框架的记忆有时带工具往返、有时不带）。
        try:
            self.engine.note_tool_round(event, llm_resp)
        except Exception:
            logger.debug("[multi-decision] 记录工具往返异常（已吞掉）", exc_info=True)
        if getattr(llm_resp, "status", None) == "error":
            return
        # v3.54（用户 m14082「早上的几条消息 bot 会重复发送」）：模型偶尔把同一条
        # 回复写成两版发两遍——一版 <emoji>128</emoji>，一版正文里内联
        # [Emoji 128]。框架在这个钩子全跑完之后才读 llm_resp.text_response 去
        # parse_xml 发送，所以只能在这儿收口（见 decision/output_guard.py）。
        try:
            text = getattr(llm_resp, "text_response", "") or ""
            fixed, dropped, moved = dedupe_reply(text)
            if dropped or moved:
                llm_resp.text_response = fixed
                logger.warning(
                    f"[multi-decision] {self._sid_of(event)} 回复收口："
                    f"删掉重复气泡 {dropped} 条，字面表情搬回标签 {moved} 处"
                )
            # 用户 m02159 图6：模型偶尔整段裸文本，没有 <msg> 包裹，框架解析出
            # 0 条消息 → 这一轮算「未回复」，群里什么都看不到。这里补一层兜底。
            # 用户 m04617 图8：动态段里写了「真不该开口就只输出 <msg></msg>」，
            # 这里得认这个约定——空壳不能当一条空消息发到群里，直接把这一轮清空。
            low = str(getattr(llm_resp, "text_response", "") or "").strip().lower()
            if low in ("<msg></msg>", "<msg/>", "<msg />"):
                llm_resp.text_response = ""
                logger.warning(
                    f"[multi-decision] {self._sid_of(event)} 模型自己决定这一轮不开口（空壳），已吞掉不发"
                )
            packed, wrapped = wrap_plain_reply(getattr(llm_resp, "text_response", "") or "")
            if wrapped:
                llm_resp.text_response = packed
                logger.warning(
                    f"[multi-decision] {self._sid_of(event)} 回复没有 <msg> 包裹，"
                    f"已自动包成一条消息发出（{len(packed)} 字符）"
                )
        except Exception:
            logger.exception("[multi-decision] 回复收口异常（已吞掉）")

    @staticmethod
    def _sid_of(event) -> str:
        """日志里认会话用；event 上没有 sid 时退回 session.sid，都没有就空串。"""
        sid = getattr(event, "sid", "") or ""
        if not sid:
            sid = getattr(getattr(event, "session", None), "sid", "") or ""
        return str(sid)

    def _touch_reply_progress(self, event) -> None:
        """用户 m01587：模型返回 / 工具结果都算「有进展」，把看门狗的无进展窗口往后推。"""
        try:
            self.engine._touch_reply_progress(self._sid_of(event),
                                              getattr(event, "_mdp_reply_round", None))
        except Exception:
            logger.debug("[multi-decision] 续期看门狗失败（已吞掉）", exc_info=True)

    @on.tool_result(priority=Priority.LOW)
    async def _on_tool_result(self, event, tool_result):
        await self.engine._check_reply_round(getattr(event, "_mdp_reply_round", None))
        self._touch_reply_progress(event)
        if self.tracer is None:
            return
        try:
            self.tracer.tool(event, tool_result)
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（工具）异常（已吞掉）")

    @on.message_sent(priority=Priority.LOW)
    async def _on_message_sent(self, event, action, result):
        await self.engine._check_reply_round(getattr(event, "_mdp_reply_round", None))
        # 实时窗底部的「我刚说了什么」：bot 的回复要等下一轮采集才进历史，
        # 在那之前先挂进待发列表，带名字和时间戳，跟 user 卡片长得一样（v2.26）
        try:
            self.engine.note_sent(event, action, result)
        except Exception:
            logger.exception("[multi-decision] 记 bot 待发消息异常（已吞掉）")
        if self.tracer is None:
            return
        try:
            self.tracer.sent(event, action, result)
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（发送）异常（已吞掉）")

    @on.exception(priority=Priority.MEDIUM)
    async def _on_exception(self, event, exc):
        """请求出错：记下来并安排自动重试（用户 m30428）。"""
        await self.engine.handle_exception(event, exc)

    @on.final_result(priority=Priority.MEDIUM)
    async def _on_final_result(self, event, final_result):
        await self.engine.guard_reply_final(event, final_result)
        # 用户 m13442 第 4 条：这一轮结束，把接管的代理参数清掉，别漏给同一任务里后面的请求。
        try:
            _REPLY_LIMITS.set(None)
        except Exception:
            pass
        self.engine._reply_delivery.set_check(None)
        await self.engine.handle_final(event, final_result)
        if self.tracer is None:
            return
        try:
            self.tracer.finish(event, final_result)
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（收尾）异常（已吞掉）")

    @register.tool(
        "view_forward",
        "查看一条合并转发的全部图片。消息里出现「[Forward …]」或「[合并转发]」时，"
        "如果只看到前几张图、需要看完整内容就调用它，把这条转发的图都取回来。",
        {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "外层消息的 message_id，就是正文里 [message_id: …] 那个数字",
                },
            },
            "required": ["message_id"],
        },
    )
    async def tool_view_forward(self, event, message_id: str = ""):
        mid = str(message_id or "").strip()
        if not mid:
            return "缺少 message_id"
        try:
            raw_sid = self.window.raw_sid(getattr(event, "sid", "") or "") if self.window is not None else ""
        except Exception:
            raw_sid = ""
        try:
            sid = normalize_sid(getattr(event, "sid", "") or "")
        except Exception:
            sid = str(getattr(event, "sid", "") or "")
        try:
            gif_mode = self.window.gif_mode(sid) if self.window is not None else "grid"
        except Exception:
            gif_mode = "grid"
        try:
            gif_opts = self.window.gif_opts(sid) if self.window is not None else None
        except Exception:
            gif_opts = None
        try:
            refs = list(forward_media_for_mid(mid, raw_sid) or [])
        except Exception:
            refs = []
        if not refs:
            return "这条转发没有可用的图片缓存"
        # v4.69（用户 m00969）：动图拼九宫格、大图压到 1280 再发，别把几十张原图
        # GIF 原样塞进请求体；按当前会话 history_gif_* 那一档走。
        # v4.70（用户 m01237）：开了「转发里的动图一律不发」就整张跳过动图，
        # 只在返回文字里说一声；静态图照旧走九宫格/压小那一套。
        drop_gif = bool(getattr(self, "forward_drop_gif", True))
        got = forward_model_refs(raw_sid, mid, refs, gif_mode, drop_gif, gif_opts)
        live = [r for r in got if r.get("path")]
        notes = []
        for r in got:
            note = str(r.get("note") or "")
            if note and note not in notes:
                notes.append(note)
        if not live:
            if notes:
                return "这条转发的图没有发给模型：" + "".join(notes)
            return "这条转发的图片都取不到"
        text = "这条转发共 %d 张图，已全部附上。%s" % (len(live), "".join(notes))
        return ToolResult(
            text=text,
            media_refs=[{"type": "kira_image_ref", "path": r["path"],
                         "mime_type": r["mime"]} for r in live],
        )

    @register.tool(
        "view_quote",
        "查看一条被引用消息的原文和图片。消息里出现「[Reply ID: …]」、"
        "提示说「引用的那条已经不在窗口里」时，调用它把被引用的那条取回来。",
        {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "被引用消息的 message_id，就是 [Reply ID: …] 后面那个数字",
                },
            },
            "required": ["message_id"],
        },
    )
    async def tool_view_quote(self, event, message_id: str = ""):
        mid = str(message_id or "").strip()
        if not mid:
            return "缺少 message_id"
        try:
            sid = normalize_sid(getattr(event, "sid", "") or "")
        except Exception:
            sid = str(getattr(event, "sid", "") or "")
        try:
            raw_sid = self.window.raw_sid(getattr(event, "sid", "") or "") if self.window is not None else ""
        except Exception:
            raw_sid = ""
        try:
            gif_mode = self.window.gif_mode(sid) if self.window is not None else "grid"
        except Exception:
            gif_mode = "grid"
        try:
            gif_opts = self.window.gif_opts(sid) if self.window is not None else None
        except Exception:
            gif_opts = None
        try:
            found = self.window.quote_of(sid, mid) if self.window is not None else {}
        except Exception:
            found = {}
        found = found if isinstance(found, dict) else {}
        body = str(found.get("text") or "").strip()
        refs = list(found.get("media") or [])
        if not body and not refs:
            return "找不到这条引用（message_id: %s），可能已经被清理了" % mid
        # 窗口里的原文带着 [时间] [message_id: …] [user_nickname: …] | 前缀，
        # 压成引用里那行「时间 昵称: 正文」，免得模型把它当成一条新消息。
        if found.get("source") == "window":
            body = _quote_body_of(body) or body
        elif found.get("source") == "db":
            who = str(found.get("sender") or "").strip()
            ts = float(found.get("ts") or 0.0)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts > 0 else ""
            head = " ".join(x for x in (stamp, (who + ":") if who else "") if x)
            if head:
                body = (head + " " + body).strip()
        if not refs:
            return "这条引用没有图片。原文：%s" % (body or "（空）")
        drop_gif = bool(getattr(self, "forward_drop_gif", True))
        got = forward_model_refs(raw_sid, mid, refs, gif_mode, drop_gif, gif_opts)
        live = [r for r in got if r.get("path")]
        notes = []
        for r in got:
            note = str(r.get("note") or "")
            if note and note not in notes:
                notes.append(note)
        if not live:
            if notes:
                return "这条引用的图没有发给模型：%s。原文：%s" % ("".join(notes), body or "（空）")
            return "这条引用的图片都取不到。原文：%s" % (body or "（空）")
        text = "这条引用共 %d 张图，已全部附上。%s原文：%s" % (
            len(live), "".join(notes), body or "（空）")
        return ToolResult(
            text=text,
            media_refs=[{"type": "kira_image_ref", "path": r["path"],
                         "mime_type": r["mime"]} for r in live],
        )

    def _default_llm_client(self, session=None):
        """框架默认 LLM 客户端；3.2.1 起可以按会话取（会话自己选了模型就用它）。

        取不到返回 None，调用方自己决定怎么兜底。会话选的模型被删了框架会抛错，
        这里一并吞掉，让调用方退回自己的顺序。
        """
        fn = getattr(self.ctx, "get_default_llm_client", None)
        if not callable(fn):
            return None
        try:
            if session is not None and compat.has_session_llm():
                return fn(session=session)
            return fn()
        except Exception:
            logger.debug("[multi-decision] 取默认 LLM 客户端失败（已吞掉）", exc_info=True)
            return None

    def _trace_model_hint(self, event) -> str:
        """这一轮真正拿去发请求的模型名（用户 m02939 第 2 条）。

        框架的 LLMResponse 只有 token、没有模型身份，回复轮的轨迹于是全是空身份，
        价目表把回复用量整片挂在「未记录模型」上。这里在请求阶段就把模型问出来：
        先看 event.model_group 的第一个（agent_executor 就是拿它发请求，失败才往后换），
        再问 provider_mgr 要默认 LLM —— 框架的 BuildRequestStage 也是这么兜底的，
        拿到的是同一个客户端。都取不到返回空串，宁可继续记成「未记录模型」，
        也不拿配置文件里的默认值去猜历史归属。
        """
        try:
            group = list(getattr(event, "model_group", None) or [])
        except Exception:
            group = []
        for one in group:
            name = str(getattr(getattr(one, "model", None), "model_name", "") or "").strip()
            if name:
                return name[:80]
        # 3.2.1 起框架自己会按「会话覆盖 → 全局默认」解析（get_default_llm(session=)），
        # 直接问它最准；会话选的模型被删了它会抛错，那就退回下面那套手工顺序。
        if compat.has_session_llm():
            client = self._default_llm_client(getattr(event, "session", None))
            name = str(getattr(getattr(client, "model", None), "model_name", "") or "").strip()
            if name:
                return name[:80]
        # 3.2.0 起会话可以单独选模型：BuildRequestStage 的顺序是 event.model_group →
        # session.data["llm_model"] → 默认 LLM。这里跟着走一遍，否则会话选了自己的模型时，
        # 回复用量会被记到默认模型名下（价目页归属错）。
        try:
            sid_model = (getattr(getattr(event, "session", None), "data", None) or {}).get("llm_model")
        except Exception:
            sid_model = None
        if sid_model:
            picked = None
            try:
                mgr = getattr(self.ctx, "provider_mgr", None)
                if mgr is not None:
                    picked = mgr.get_llm(sid_model)
            except Exception:
                picked = None
            name = str(getattr(getattr(picked, "model", None), "model_name", "") or "").strip()
            if name:
                return name[:80]
        client = self._default_llm_client()
        if client is None:
            try:
                mgr = getattr(self.ctx, "provider_mgr", None)
                if mgr is not None:
                    client = mgr.get_default_llm()
            except Exception:
                client = None
        try:
            name = str(getattr(getattr(client, "model", None), "model_name", "") or "").strip()
            if name:
                return name[:80]
        except Exception:
            logger.debug("[multi-decision] 取本轮模型名失败（已吞掉）", exc_info=True)
        return ""

    async def _trace_begin(self, event, request):
        if self.tracer is None:
            return
        try:
            # v3.51（用户 m07385 第 7 条）：把引擎记的「触发那一刻」交给轨迹，
            # 页面上的请求用时才是「触发 → 请求结束」（含防抖），而不是只剩请求本身。
            try:
                sid = normalize_sid(getattr(event, "sid", "") or event.session.sid)
            except Exception:
                sid = normalize_sid(getattr(event, "sid", "") or "")
            trig = self.engine.round_trigger_at(sid) if sid else 0.0
            # 用户 m05453：抖动 → 决策 → 回复是三段，防抖这一格只放抖动真的等了几秒，
            # 决策那一段单独交给轨迹，记录页才能写成「防抖 7s + 决策 5s + 12s」。
            jit = self.engine.round_jitter_at(sid) if sid else 0.0
            dec = self.engine.round_decision_at(sid) if sid else 0.0
            # 用户 m21274：账本里记下这一轮走的是普通决策还是 JEV 决策（回复轮留空），
            # 价目页的柱子才能按 决策 / JEV 决策 / 回复 分开画。
            # 用户 m30734：这里以前写成「开了 JEV 就给回复轮标 jev」，结果回复的 token
            # 全被算进 JEV 决策柱、普通决策反而归零。回复轮一律留空，JEV 只由决策轮
            # 自己按 decision_mode 标（见 state/tracer.py 的 decision()）。
            self.tracer.begin(event, request, trigger_at=trig, debounce=jit, decision=dec,
                              model_hint=self._trace_model_hint(event), mode="")
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（请求）异常（已吞掉）")

    # ------------------------------------------------------------ WebUI

    @_page("/decision-log", auth=True, menu=_MENU)
    def page_decision_log(self):
        """侧边栏「多样决策」入口，页面本体是 web/index.html。"""
        return PluginPage.from_folder("./web")

    @_page("/settings", auth=True)
    def page_settings(self):
        """设置页。不单独占侧边栏，从日志页右上角的齿轮进来。"""
        return PluginPage.from_folder("./web/settings")

    def _is_hidden(self, sid: str) -> bool:
        return bool(self.hidden and self.hidden.contains(sid))

    # ------------------------------------------------------ 每群独立配置

    @property
    def _bot_prompt_cfg(self) -> dict:
        """框架 bot 配置里决定「动态段放哪」的两项（reload 后重新读）。"""
        try:
            return (self.ctx.config.get("bot_config", {}) or {}).get("bot", {}) or {}
        except Exception:
            return {}

    @property
    def dynamic_prompt_position(self) -> str:
        """sessions / chat_env / time 这三段的落点：latest_user = 挪到最后一条用户消息。"""
        return str(self._bot_prompt_cfg.get("dynamic_prompt_position", "latest_user") or "latest_user")

    @property
    def memory_prompt_position(self) -> str:
        """核心记忆段的落点。"""
        return str(self._bot_prompt_cfg.get("memory_prompt_position", "system") or "system")

    def remember_bot_name(self, sid: str, nick: str) -> None:
        """记下「这个会话里别人看到的机器人叫什么」。

        同一个 bot 在群里是群名片、在私聊是 QQ 昵称，不能共用一个全局值；
        而且要落盘——热重载会把内存里的表清空，之后就只能拿别的会话的名字顶，
        结果是同一个会话被叠成【樱井桃华】【桃华】。
        """
        nick = str(nick or "").strip()
        if not nick:
            return
        self.bot_name = nick  # 全局兜底，新会话第一眼先用它
        sid = str(sid or "")
        if not sid:
            return
        if (self.bot_names or {}).get(sid) == nick:
            return
        try:
            if self.bot_names is None:
                self.bot_names = {}
            self.bot_names[sid] = nick
        except Exception:
            return
        try:
            if getattr(self, "bot_names_store", None) is not None:
                self.bot_names_store.set(sid, nick)
        except Exception:
            logger.exception("[multi-decision] 保存机器人昵称失败（忽略）")

    def bot_name_for(self, sid: str = "") -> str:
        """这个会话里机器人显示的名字。

        优先级：本会话手填 > 全局手填（设置页「机器人昵称」）> 框架里看到的真实昵称。
        手填永远排第一：用户 2026-10-20 明确要求「让用户自己填名字，不同群可以
        分开取，不取就继承全局」，适配器探测到的群名片只当最后一层兜底
        （用户 m32887「名字是从配置文件获取的，别硬写入」）。
        """
        sid = str(sid or "")
        if sid and self.overrides is not None:
            try:
                own = self.overrides.value(sid, "bot_name", None)
            except Exception:
                own = None
            if isinstance(own, str) and own.strip():
                return own.strip()
        if str(self.bot_name or "").strip():
            return str(self.bot_name).strip()
        try:
            nick = (self.bot_names or {}).get(sid)
        except Exception:
            nick = None
        return str(nick or "").strip()

    # v3.66（用户 m19536 第 4 条）：这四个上限项分「群聊默认 / 私聊默认」两套值。
    # v3.68（用户 m20632 第 2 条）：触发概率也进来——私聊默认 100%（必回）。
    # v3.70（用户 m21533 第 2/3 条）：触发方式也进来——私聊不走自适应与时段曲线，
    # 默认就是概率模式；这两条都不写进 overrides，左栏仍显示「全局配置」。
    _DM_DEFAULT_KEYS = frozenset(
        {
            "media_cache_limit",
            "media_other_limit",
            "trace_keep",
            "decision_keep",
            # v4.90（发版前审计）：窗口快照显示条数也分群聊 / 私聊两套默认值，
            # 不加进来 eff("window_show", 私聊) 永远读不到 window_show_dm。
            "window_show",
            "windows_keep",
            "summaries_keep",
            "forward_media_keep",
            "retry_keep",
            "trigger_probability",
            # 用户 m20823 第 2 条：私聊的「决策模式」不跟全局走，默认关——私聊本来就
            # 该直接回，被决策接管等于每轮多一次调用还常常判成不回。群聊仍看全局。
            "advanced_enabled",
        }
    )

    @staticmethod
    def _is_dm_sid(sid: str) -> bool:
        """这个 sid 是不是私聊。认 gm:1 / 桃华:dm:1 / dm_1 三种写法。"""
        s = str(sid or "")
        if _merge_sid_key(s).startswith("dm:"):
            return True
        return s.split(":")[-1].startswith("dm_")

    def eff(self, key: str, sid: str = ""):
        """取某项配置在当前会话下的有效值：先看覆盖，再回退全局。

        engine 里所有「跟某个群有关」的判定都必须走这里，直接用
        ``self.plugin.trigger_threshold`` 会让单群设置失效。
        """
        # v3.13：锁定项不受任何覆盖影响，单群 overrides 里残留的 false 也无效。
        if key in _LOCKED_ON:
            return True
        default = getattr(self, key, None)
        # v3.66：上限项在私聊里默认走「私聊默认」那一套，群聊仍用原键。
        if key in self._DM_DEFAULT_KEYS and self._is_dm_sid(sid):
            dm_default = getattr(self, key + "_dm", None)
            if dm_default is not None:
                default = dm_default
        if not sid or self.overrides is None:
            return default
        try:
            value = self.overrides.value(sid, key, None)
        except Exception:
            logger.exception(f"[multi-decision] 读取 {sid} 的覆盖配置失败（已忽略）")
            return default
        # 用户 m19210：私聊会话的单会话覆盖存在 *_dm 键上（设置页选中私聊时
        # 画的就是这个键），但清理逻辑问的是原键。原键没有覆盖时再看 *_dm，
        # 免得页面上改了私聊上限、实际裁剪还在用群聊默认值。
        if value is None and key in self._DM_DEFAULT_KEYS and self._is_dm_sid(sid):
            try:
                value = self.overrides.value(sid, key + "_dm", None)
            except Exception:
                value = None
        if value is None:
            return default
        try:
            return _coerce(key, value, default)
        except Exception:
            return default

    def purge_session(self, sid: str) -> dict:
        """删掉某个会话在本插件里产生的全部记录（不动框架数据库）。

        删的是「我们自己写下的东西」：消息日志、窗口日志、请求记录（含轨迹）、
        备注名、隐藏标记、单群配置、中期摘要、持久化计数，以及内存状态。
        框架自己的聊天记录（data.db）不碰。
        """
        sid = str(sid or "").strip()
        if not sid:
            return {}
        # sid 会一路被拿去拼目录名（request_log.purge 里的 rmtree），这里先挡掉
        # 空值、路径分隔符与 ".."，不合法直接当没这个会话（审查报告：避免删到上级目录）。
        if not _sid_ok(sid):
            logger.warning(f"[multi-decision] 拒绝删除记录：会话 id 不合法 {sid!r}")
            return {}
        out = {
            "decisions": 0,
            "windows": 0,
            "requests": 0,
            # 用户 m18870：轨迹并进请求快照，清 requests 就包含轨迹；图片副本另算。
            "media": 0,
            "aliases": 0,
            "hidden": 0,
            "overrides": 0,
            "summary": 0,
            "state": 0,
        }
        errors = [0]

        def _step(key, fn, default=0):
            """一类一类删：某一类失败只记账，不让前面已经删掉的变成「整单失败」。"""
            try:
                val = fn()
            except Exception:
                errors[0] += 1
                logger.exception(f"[multi-decision] 删除会话记录失败（{key}）：{sid}")
                val = default
            out[key] = default if val is None else val
            return out[key]

        def _quiet(key, fn):
            """不进返回值的步骤（清内存、清索引），失败只记数。"""
            try:
                fn()
            except Exception:
                errors[0] += 1
                logger.exception(f"[multi-decision] 删除会话记录失败（{key}）：{sid}")

        _step("decisions", lambda: self._log.purge_session(sid) if self._log is not None else 0)
        _step("windows", lambda: self._window_log.purge_session(sid) if self._window_log is not None else 0)
        _step("requests", lambda: self.request_log.purge(sid) if self.request_log is not None else 0)
        # v3.64：轨迹并进请求快照，删请求记录时已经一起没了，这里只清内存
        _quiet("tracer", lambda: self.tracer.purge(sid) if self.tracer is not None else None)
        _step(
            "media",
            lambda: int((self.window.purge_media_cache(sid) or {}).get("removed") or 0)
            if self.window is not None
            else 0,
        )
        _quiet("retry", lambda: self.retry_history.purge(sid) if self.retry_history is not None else None)
        # Aliases.set 返回的是整份映射（不是计数），直接拿来当 counts 会让
        # api_session_delete 的 sum(counts.values()) 炸成 int + dict。这里显式记 1。
        _step(
            "aliases",
            lambda: (self.aliases.set(sid, ""), 1)[1]
            if (self.aliases is not None and self.aliases.get(sid))
            else 0,
        )
        _step(
            "hidden",
            lambda: (self.hidden.remove(sid), 1)[1]
            if (self.hidden is not None and self.hidden.contains(sid))
            else 0,
        )
        _step(
            "overrides",
            lambda: (self.overrides.clear(sid), 1)[1]
            if (self.overrides is not None and self.overrides.has(sid))
            else 0,
        )
        _step(
            "summary",
            lambda: 1 if (self.summaries is not None and self.summaries.clear(sid)) else 0,
        )
        _step(
            "state",
            lambda: 1
            if (self.store is not None and self.store.drop(sid, self.engine.persist_table()))
            else 0,
        )
        _quiet("window_forget", lambda: self.window.forget(sid) if self.window is not None else None)
        _quiet("band_tune", lambda: self.band_tune.forget(sid) if self.band_tune is not None else None)
        _quiet("band_rhythm", lambda: self.band_rhythm.forget(sid) if self.band_rhythm is not None else None)
        _quiet("engine", lambda: self.engine.forget(sid))
        if errors[0]:
            logger.warning(f"[multi-decision] 删除会话记录 {sid} 时有 {errors[0]} 步失败，其余已删完")
        return out

    def clear_records(self, sid: str) -> dict:
        """清掉这个会话攒下的旧记录，但保留配置。

        和 purge_session 的区别：别名、隐藏标记、单群覆盖、计数都不动，
        只清「看的内容」——请求记录（含调用轨迹）、窗口、裁切日志、消息日志、摘要。
        用户的原话是「有些信息是昨天或者更早之前的，不如清空重新收集」。
        """
        sid = str(sid or "").strip()
        if not sid:
            return {}
        # 同样会拿 sid 拼目录再删，入口校验（见 _sid_ok）。
        if not _sid_ok(sid):
            logger.warning(f"[multi-decision] 拒绝清空记录：会话 id 不合法 {sid!r}")
            return {}
        out = {"requests": 0, "windows": 0, "decisions": 0,
               "summary": 0, "window": 0}
        try:
            # Synchronous boundary: withdraw pending retries before deleting their ledger.
            self.engine.clear_record_retries(sid)
            if self.request_log is not None:
                out["requests"] = self.request_log.purge(sid)
            if self.retry_history is not None:
                self.retry_history.purge(sid)
            if self.tracer is not None:
                self.tracer.purge(sid)
            if self._window_log is not None:
                out["windows"] = self._window_log.purge_session(sid)
            if self._log is not None:
                out["decisions"] = self._log.purge_session(sid)
            if self.summaries is not None and self.summaries.clear(sid):
                out["summary"] = 1
            if self.window is not None and self.window.forget(sid):
                out["window"] = 1
        except Exception:
            logger.exception(f"[multi-decision] 清空会话记录失败：{sid}")
            return {}
        return out

    def overrides_payload(self, sid: str = "") -> dict:
        """给设置页用的：全局值 + 该会话的覆盖值 + 哪些键允许覆盖。"""
        globals_map = {}
        for key in PER_SESSION_KEYS:
            globals_map[key] = getattr(self, key, None)
        data = self.overrides.get(sid) if (sid and self.overrides) else {}
        effective = {}
        for key in PER_SESSION_KEYS:
            effective[key] = self.eff(key, sid)
        return {
            "sid": sid,
            "keys": list(PER_SESSION_KEYS),
            "global": globals_map,
            "override": data,
            "effective": effective,
            # 数字项的上下限，设置页拿去做 min/max；后端 _coerce 会夹，前端得先知道
            "bounds": {k: list(_BOUNDS[k]) for k in PER_SESSION_KEYS if k in _BOUNDS},
            # v3.70（用户 m21533 第 2 条）：私聊的特例默认值（触发概率 100% 等）。
            # 这些键不在 schema 里（也不该变成可覆盖项），设置页在私聊作用范围下
            # 直接把默认值填进对应控件，不再显示「跟随全局」。
            "dm_defaults": {
                k: getattr(self, k + "_dm", None)
                for k in self._DM_DEFAULT_KEYS
                if getattr(self, k + "_dm", None) is not None
            },
        }

    def note_new_session(self, sid: str) -> None:
        """第一次见到的会话默认关掉插件（用户 m45332 第6条）。

        bot 被陌生人拉进新群、或者陌生人私聊时，全局开关是开的，它就会立刻
        开始攒消息、跑 agent、烧 token。这里在会话第一次出现时把
        session_enabled 写 False，人确认要用了再在左栏点开。
        """
        reg = self.session_registry
        if reg is None or not sid or self.overrides is None:
            return
        try:
            first = reg.remember(sid)
        except Exception:
            logger.exception("[multi-decision] 登记新会话失败（已忽略）")
            return
        if not first:
            return
        # v3.70（用户 m21533 第 2 条）：新私聊不再往 overrides 里写触发参数——用户要的
        # 是「还是全局配置」，但「触发概率」这一栏对私聊是特例：默认 100%，不跟随全局。
        # 那个 100 由 _DM_DEFAULT_KEYS 那层默认值给（见 eff），设置页在私聊作用范围下
        # 直接显示 100（overrides_payload 里的 dm_defaults），不用写覆盖值。
        if not self.new_session_default_off:
            return
        try:
            cur = self.overrides.get(sid) or {}
            cur["session_enabled"] = False
            self.overrides.set(sid, cur)
        except Exception:
            logger.exception("[multi-decision] 新会话写入默认关闭失败（已忽略）")
            return
        logger.info(
            f"[multi-decision] 新会话 {sid} 默认关闭（避免被拉群/私聊时白烧 token）"
        )

    def _recent24(self) -> dict:
        """最近 24 小时统计（带 20 秒缓存）。

        会话左栏每 5 秒轮询一次，stats_recent 要把当天和跨零点那份日志整份
        读一遍；没缓存的话光开着页面就一直在读盘。20 秒的滞后对胶囊没影响。
        """
        now = time.time()
        cached = getattr(self, "_recent24_cache", None)
        if cached and now - cached[0] < 20:
            return cached[1]
        data = {}
        try:
            if self._log is not None:
                data = self._log.stats_recent(24)
        except Exception:
            logger.exception("[multi-decision] 统计最近 24 小时失败（已忽略）")
            data = {}
        self._recent24_cache = (now, data)
        return data

    def _eff_summary(self, sid: str) -> dict:
        """给日志页群组头用的单群配置摘要（全局项不重复给）。"""
        band_rest = self._band_rest_view(sid)
        return {
            "decision_enabled": self.decision_enabled,
            "window_enabled": self.eff("window_enabled", sid),
            "window_keep": self.eff("window_keep", sid),
            "window_trim_threshold": self.eff("window_trim_threshold", sid),
            "session_enabled": self.eff("session_enabled", sid),
            "trigger_mode": self.eff("trigger_mode", sid),
            "trigger_threshold": self.eff("trigger_threshold", sid),
            # v3.23（用户 m45332）：概率模式的会话卡胶囊要显示「实际概率/配置概率」
            "trigger_probability": self.eff("trigger_probability", sid),
            # v3.30：概率模式的伪随机兜底，会话卡胶囊上要标「有兜底」
            "prob_pseudo": self.eff("prob_pseudo", sid),
            # v3.51（用户 m07385 第 6 条）：左栏会话卡要显示「防抖 / 兜底 / 静默 /
            # 休息」这些开关的状态，一律取有效值（单群覆盖优先于全局）。
            "jitter_enabled": self.eff("jitter_enabled", sid),
            "jitter_seconds": self.eff("jitter_seconds", sid),
            "jitter_max_rounds": self.eff("jitter_max_rounds", sid),
            "jitter_skip_must_go": self.eff("jitter_skip_must_go", sid),
            "burst_merge_enabled": self.eff("burst_merge_enabled", sid),
            "burst_merge_seconds": self.eff("burst_merge_seconds", sid),
            "silent_trigger_enabled": self.eff("silent_trigger_enabled", sid),
            "silent_trigger_minutes": self.eff("silent_trigger_minutes", sid),
            "rest_mode_enabled": self.eff("rest_mode_enabled", sid),
            "rest_start": self.eff("rest_start", sid),
            "rest_end": self.eff("rest_end", sid),
            # v3.73（用户 m24640 第 5 条）：曲线模式的休息时段由段上的 rest 标记算出来，
            # 左栏胶囊只认这里给的值——band_rest_now 是「此刻算不算休息」。
            "band_rest_enabled": bool(band_rest["on"]),
            "band_rest_start": band_rest["start"],
            "band_rest_end": band_rest["end"],
            "band_rest_now": bool(band_rest["now"]),
            "band_rest_count": int(band_rest["count"]),
            "band_rest_probability": self.eff("band_rest_probability", sid),
            # v3.70（用户 m21533 第 6 条）：左栏「模式」胶囊要知道曲线开没开——
            # 开了写「曲线模式」，右边那个概率就不再显示。
            "bands_enabled": bool(self.eff("bands_enabled", sid)),
            # v3.91（用户 m05749）：会话卡要挂「进阶」胶囊，所以这里得给有效值——
            # 原来这份摘要只列了左栏用得到的老键，进阶开关不在里面，胶囊就一直不出现。
            "advanced_enabled": bool(self.eff("advanced_enabled", sid)),
            "rest_silent_enabled": self.eff("rest_silent_enabled", sid),
            "rest_silent_minutes": self.eff("rest_silent_minutes", sid),
            # 只算「真正改过配置」的项：session_enabled 是那个开关本身，关一下开关
            # 不代表这个会话有独立配置（用户 m33573 图6：一堆静音的会话全被标成独立配置）
            "overridden": len(
                [k for k in (self.overrides.get(sid) or {}) if k != "session_enabled"]
            ) if self.overrides else 0,
        }

    # ------------------------------------------------------ 配置体检

    @staticmethod
    def _rest_hours(start: str, end: str) -> list:
        """把 "HH:MM" 起止压成「哪些整点算休息」，跨午夜自动处理。

        只按整点算：分布图是小时粒度的，分钟级偏移在图上没意义。
        """
        try:
            sh, sm = [int(x) for x in str(start).split(":")]
            eh, em = [int(x) for x in str(end).split(":")]
        except (TypeError, ValueError):
            return []
        start_m = sh * 60 + sm
        end_m = eh * 60 + em
        if start_m == end_m:
            return []
        out = []
        for h in range(24):
            cur = h * 60
            if start_m < end_m:
                hit = start_m <= cur < end_m
            else:
                hit = cur >= start_m or cur < end_m
            if hit:
                out.append(h)
        return out

    def _band_rest_view(self, sid: str = "", now: float = None) -> dict:
        """曲线模式下休息时段的样子：几段、第一段的起止、此刻算不算。

        v3.73（用户 m24640 第 5 条）：休息段是 time_bands 段上的 rest 标记，可能有好几段。
        左栏胶囊只显示第一段的起止（多段时 title 里写「共 N 段」），此刻在不在休息里
        由 now 决定——前端直接用 band_rest_now，不用自己再算窗口。
        """
        if now is None:
            now = time.time()
        ranges = []
        try:
            if self.engine is not None:
                ranges = self.engine._rest_ranges(sid)
        except Exception:
            ranges = []
        if not ranges:
            return {"on": False, "start": "", "end": "", "now": False, "count": 0}
        local = time.localtime(now)
        slot = local.tm_hour * 2 + (1 if local.tm_min >= 30 else 0)
        inside = any(s <= slot < e for s, e in ranges)

        def hhmm(n):
            n = max(0, min(48, int(n)))
            return "%02d:%02d" % (n // 2, (n % 2) * 30)

        return {
            "on": True,
            "start": hhmm(ranges[0][0]),
            "end": hhmm(ranges[0][1]),
            "now": bool(inside),
            "count": len(ranges),
        }

    def _rest_payload(self, sid: str = "") -> dict:
        """当前模式生效的那套休息时段（曲线开着就看段上的 rest 标记，否则 rest_*）。"""
        if bool(self.eff("bands_enabled", sid)):
            view = self._band_rest_view(sid)
            ranges = []
            try:
                if self.engine is not None:
                    ranges = self.engine._rest_ranges(sid)
            except Exception:
                ranges = []
            return {
                "on": view["on"],
                "start": view["start"],
                "end": view["end"],
                "now": view["now"],
                "count": view["count"],
                "ranges": [{"s": s, "e": e} for s, e in ranges],
                "probability": int(self.eff("band_rest_probability", sid) or 0),
                "kind": "band",
            }
        return {
            "on": bool(self.eff("rest_mode_enabled", sid)),
            "start": self.eff("rest_start", sid),
            "end": self.eff("rest_end", sid),
            "probability": int(self.eff("rest_probability", sid) or 0),
            "kind": "normal",
        }

    def rest_overlap(self, sid: str = "", days: int = 7) -> dict:
        """休息时段压到近 N 天的 24 小时分布上，看它盖住了多少消息。

        v3.72（用户 m24260 第 2 条）：普通模式与曲线模式各有一套休息时段，
        这里按当前生效的那一套算（曲线开着就是 band_rest_*）。
        """
        cfg = self._rest_payload(sid)
        start = cfg["start"]
        end = cfg["end"]
        if cfg.get("kind") == "band":
            # v3.73（用户 m24640 第 5 条）：曲线模式的休息时段可能是好几段，
            # 按半小时格并成小时集合，再去看它盖住多少消息。
            hours = []
            for r in (cfg.get("ranges") or []):
                for slot in range(int(r.get("s") or 0), int(r.get("e") or 0)):
                    h = max(0, min(23, slot // 2))
                    if h not in hours:
                        hours.append(h)
            hours.sort()
        else:
            hours = self._rest_hours(start, end) if cfg["on"] else []
        hist = self._log.hour_histogram(days, sid, only_groups=True) if self._log else {"hours": [0] * 24, "slots": [0] * 48, "total": 0, "days": days}
        covered = sum(hist["hours"][h] for h in hours) if hours else 0
        total = hist.get("total", 0)
        share = round(covered * 100.0 / total, 1) if total else 0.0
        if cfg.get("kind") == "band":
            cnt = int(cfg.get("count") or 0)
            label = ("%s–%s" % (start, end)) if cnt <= 1 else ("%s–%s 等 %d 段" % (start, end, cnt))
        else:
            label = "%s–%s" % (start, end)
        return {
            "on": bool(cfg["on"]),
            "kind": cfg["kind"],
            "rest_start": start,
            "rest_end": end,
            "label": label,
            "hours": hours,
            "covered": covered,
            "total": total,
            "share": share,
        }

    def config_check(self, sid: str = "") -> list:
        """检查一组互相牵制的配置，返回 [{level, key, text}]。

        只提示不修改：有些组合用户是故意的（比如就是想让它别裁）。
        level: "error" 基本等于不工作；"warn" 能跑但大概率不是你想要的。
        """
        g = lambda k: self.eff(k, sid)  # noqa: E731
        items = []

        def add(level, key, text):
            items.append({"level": level, "key": key, "text": text})

        keep = g("window_keep")
        trim_at = g("window_trim_threshold")
        # v3.66（用户 m19536 第 7 条）：「最小裁切条数」撤了——默认 60 / 30 时它永远挡不住，
        # 只在阈值与保留条数贴得极近时才生效，等于白给一个会「永远不裁切」的坑。
        if g("window_enabled"):
            if trim_at <= keep:
                add(
                    "error",
                    "window_trim_threshold",
                    f"裁切阈值 {trim_at} 不大于保留条数 {keep}：窗口会一直顶在 {keep} 条附近，"
                    f"实际上裁不动。建议阈值明显大于保留（比如 30 / 60）。",
                )

        if g("decision_enabled"):
            mode = g("trigger_mode")
            if mode == "count":
                n = g("trigger_threshold")
                if n <= 2:
                    add(
                        "warn",
                        "trigger_threshold",
                        f"计数阈值只有 {n} 条，群里随便两句就进一次 agent，容易被当成刷屏。",
                    )
            elif mode == "adaptive":
                # v3.71（用户 m23596 第 5 条）：目标改成写在曲线每段里（tr / tp），
                # 一段都没设目标 = 概率不会自动变，比报「目标太高」更值得提醒。
                try:
                    bands = self.engine._band_list(sid) if self.engine is not None else []
                except Exception:
                    bands = []
                if not any((it[3] or 0) > 0 or (it[4] or 0) > 0 for it in bands):
                    add(
                        "warn",
                        "time_bands",
                        "动态调整开着，但曲线里没有任何一段设了目标回复次数 / 概率，概率不会自己变。",
                    )
            # v3.69（用户 m21003 第 7 条）：休息时段现在画在曲线段上（段带月亮），
            # 所以这里先看有没有休息段，没有再看老的起止，两条都没有才报「等于没有」。
            if g("rest_mode_enabled"):
                # 休息时段压到近 7 天的消息分布上：盖住太多说明「休息」正好压在
                # 群里最热闹的时候，用户八成是把起止或时区搞反了。
                try:
                    ov = self.rest_overlap(sid, days=7)
                except Exception:
                    ov = {}
                if not ov.get("hours"):
                    add(
                        "warn",
                        "rest_start",
                        "没有设置休息时段，如果凌晨有群友发癫，可能会导致凌晨产生频繁调用。",
                    )
                elif ov.get("total", 0) >= 20 and ov.get("share", 0) >= 40:
                    add(
                        "warn",
                        "rest_start",
                        f"休息时段 {ov.get('label') or ''} 盖住了近 7 天 "
                        f"{ov['share']}% 的消息（{ov['covered']}/{ov['total']} 条），"
                        f"这段时间正是群里最热闹的时候，设成休息会错过很多。"
                        f"确认下时段有没有划反。",
                    )
            if g("quota_hour_enabled") and g("quota_max_per_hour") < 1:
                add("error", "quota_max_per_hour", "每小时配额打开但上限小于 1，会一条都不回。")
            if g("quota_day_enabled") and g("quota_max_per_day") < 1:
                add("error", "quota_max_per_day", "每天配额打开但上限小于 1，会一条都不回。")

        # 用户 m06706：JEV 模式开着却不给模型，每一轮都只会在失败分支里打转；
        # 区间写反了更是永远判不出「不确定」。这里只提示，不改值。
        if self.decision_mode(sid) == "jev":
            _pages = jev_parse_apis(g("jev_apis") or "")
            if not _pages:
                # 老配置只有一份接口：当成第 1 页来看，行为跟以前一样。
                _pages = [jev_make_api("1", normalize_jev_protocol(g("jev_api_protocol")),
                                       str(g("jev_api_base") or "").strip(),
                                       str(g("jev_api_model") or "").strip())]
            try:
                _secs = jev_load_secrets(getattr(self, "_data_dir", ""))
            except Exception:
                _secs = {}
            for _pg in _pages:
                _eid = normalize_jev_api_id(_pg.get("id") or "1")
                _raw_base = str(_pg.get("base") or "").strip()
                _base, _base_why = jev_check_api_base(_raw_base)
                _proto = normalize_jev_protocol(_pg.get("protocol"))
                _where = f"第 {_eid} 页"
                if _raw_base and not _base:
                    # 原因由 check_api_base 给，跟真正发请求时的拦截口径一致。
                    add("error", "jev_apis",
                        _where + "的接口地址不合法：" + (_base_why or "地址解析不了") + "。")
                elif _base and _proto == "openai" and "/v1/systemone" in _base.lower():
                    add("error", "jev_apis",
                        _where + "选的是 OpenAI 兼容，地址里却带着 /v1/systemone；"
                        "这条会打到 SystemOne 上去。")
                if not str(_pg.get("model") or "").strip():
                    add("warn", "jev_apis", _where + "没填模型名，失败时会一直打不通。")
                elif (_proto == "openai"
                      and str(_pg.get("model") or "").strip() == DEFAULT_JEV_API_MODEL):
                    add("warn", "jev_apis",
                        _where + f"的模型名还是 {DEFAULT_JEV_API_MODEL}（SystemOne 的默认名），"
                        "OpenAI 兼容网关多数没有这个名字，点「获取」可以从清单里挑。")
                if _raw_base and not str(((_secs.get(_eid) or {}).get("api_key")) or "").strip():
                    add("error", "jev_apis",
                        _where + "还没填密钥，每一轮都会失败（失败时按「决策失败时」处理，"
                        "不会当成 0% 或 50%）。")
            _model = str(g("jev_model") or "").strip()
            _fb = str(g("jev_fallback_model") or "").strip()
            if not _model and not compat.has_default_decision():
                add("error", "jev_model",
                    "JEV 决策开着但没选模型：框架没有默认 decision 模型，必须自己挑一个。")
            for _nm, _val in (("jev_model", _model), ("jev_fallback_model", _fb)):
                if not _val:
                    continue
                if _val.startswith("custom:"):
                    _cid = normalize_jev_api_id(_val.split(":", 1)[1])
                    if not any(normalize_jev_api_id(p.get("id") or "1") == _cid
                               for p in _pages):
                        add("error", _nm,
                            f"选中的自定义接口 {_cid} 已经不存在了，改选别的或重新加一页。")
                    continue
                if not self.jev_phase.available():
                    add("error", "jev_enabled",
                        "当前 KiraAI 版本没有 decision 类模型（需要 3.1.1 及以上），JEV 决策开了也不会工作。")
                    break
                try:
                    _keys = {str(m.get("value") or "") for m in self._jev_models()}
                except Exception:
                    _keys = set()
                if _keys and _val not in _keys:
                    add("warn", _nm,
                        ("主模型" if _nm == "jev_model" else "备用模型")
                        + "不是启用中的 decision 类模型，这一轮会失败"
                        + ("，主模型连续失败时也不会切过去。" if _nm != "jev_model" else "。"))

            low, high = g("jev_lower"), g("jev_upper")
            if not (0 <= low <= high <= 100):
                add("error", "jev_lower",
                    f"不确定区间 {low}%～{high}% 不合法：要 0 ≤ 下限 ≤ 上限 ≤ 100。")
            if g("jev_action") == "decision":
                add("warn", "jev_action",
                    "不确定时选「交给决策模型」：命中区间会在这一轮再跑一次普通决策，"
                    "用的是「决策提示词」那页的模型与提示词。")

        if g("backoff_enabled"):
            base = g("backoff_base_seconds")
            cap = g("backoff_max_seconds")
            if cap < base:
                add(
                    "error",
                    "backoff_max_seconds",
                    f"退避上限 {cap}s 比起步 {base}s 还小，第一次退避就会被压回 {cap}s。",
                )
            if g("backoff_start_rejects") > 5:
                add(
                    "warn",
                    "backoff_start_rejects",
                    f"连续 {g('backoff_start_rejects')} 次不回才开始退避，前期会很吵。",
                )

        # v3.44（用户本轮第 1 条）：抖动默认关着，关着时秒数根本不起作用，
        # 所以只在这个开关真开着的时候才提示「等太久会很迟钝」。
        if g("jitter_enabled") and g("jitter_seconds") > 30:
            add(
                "warn",
                "jitter_seconds",
                f"抖动 {g('jitter_seconds')} 秒，触发后要等这么久才动，体感会很迟钝。",
            )
        return items

    @_api("GET", "/ui-schema")
    async def api_ui_schema(self):
        """设置页用的字段定义（用户 m10096）。

        KiraAI 的「配置插件」抽屉按插件根目录的 schema.json 渲染：只要有键就
        弹一个表单出来。本插件所有配置都由自己的设置页（/settings）管，那个
        抽屉纯属重复入口，还能改出设置页看不见的值。所以根 schema.json 置空
        （抽屉显示「无需配置」），字段定义搬到 ui_schema.json，只给设置页用。
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_schema.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.warning(f"[multi-decision] 读取 ui_schema.json 失败：{exc}")
            return {"ok": False, "schema": {}, "error": str(exc)}
        if not isinstance(data, dict):
            return {"ok": False, "schema": {}, "error": "ui_schema.json 不是对象"}
        return {"ok": True, "schema": data}

    @_api("GET", "/config-check")
    async def api_config_check(self, sid: str = ""):
        """配置体检：返回互相冲突的组合，只提示不改值。"""
        items = self.config_check(sid)
        return {
            "ok": True,
            "sid": sid,
            "items": items,
            "errors": len([x for x in items if x["level"] == "error"]),
            "warnings": len([x for x in items if x["level"] == "warn"]),
        }


    # ---- 进阶决策（v3.91，用户 m05749 / m05834 / m05856 / m05902）----
    # 这一层的定位：普通模式先用便宜的规则（计数/概率/曲线/节奏）决定「要不要开口」，
    # 进阶模式把这一步换成「让模型读一遍实时窗口再判」。判「回」才走完整的带系统提示词
    # 那一轮；判「不回」就到此为止，不烧一轮完整上下文的钱。
    def _decision_get(self, key, default=None, sid=""):
        """读进阶决策的配置项。

        先读运行中的 ctx.config（框架里存完就生效，不用热重载插件），拿不到再退回
        初始化时的 cfg 快照。默认读的是全局值；给了 sid 就再看这个会话有没有单群
        覆盖（设置页「聊天决策」里逐群存的那份），有就以覆盖值为准。

        A1（用户 m26791）：以前这里只读全局，白名单里的那十三个 decision_* 键虽然
        在「按群配置」里存得下，读的时候却永远读不到——页面上填了不生效。
        """
        base = default
        hit = False
        try:
            cfg = getattr(self.ctx, "config", None)
            if isinstance(cfg, dict) and key in cfg:
                base = cfg[key]
                hit = True
        except Exception:
            pass
        if not hit:
            try:
                snap = getattr(self, "_cfg_snapshot", None) or {}
                if key in snap:
                    base = snap[key]
            except Exception:
                pass
        if not sid:
            return base
        ov = getattr(self, "overrides", None)
        if ov is None:
            return base
        try:
            value = ov.value(sid, key, None)
        except Exception:
            logger.exception(f"[multi-decision] 读取 {sid} 的决策覆盖配置失败（已忽略）")
            return base
        if value is None:
            return base
        try:
            return _coerce(key, value, base if base is not None else default)
        except Exception:
            return base

    def decision_options(self, sid: str = ""):
        """把当前配置摊成 DecisionOptions（每轮现读，改完立即生效）。

        用户 m02159 图3：提示词页「工具说明」段里勾的工具才是真正注入、也允许模型调用的
        那份；勾过就以它为准，从没勾过（None）才沿用设置页里那份。

        sid 给了就按这个会话的单群覆盖取值（A1，用户 m26791）。
        """
        try:
            opt = options_from_config(lambda k, d=None: self._decision_get(k, d, sid))
        except Exception:
            logger.exception("[multi-decision] 读决策配置失败，按默认值继续")
            opt = options_from_config(lambda k, d=None: d)
        try:
            picked = self.prompt_layouts.selected_tools()
            if picked is not None:
                opt.tools = picked
        except Exception:
            logger.exception("[multi-decision] 读提示词页工具勾选失败，沿用设置页那份")
        return opt

    def reply_tool_limits(self, sid: str = "") -> dict:
        """回复链的四项代理参数（用户 m13442 第 4 条）。

        以前这四项只能在框架「代理设置」里改，全局一套；现在归插件管，回复提示词
        左栏下面有自己的设置卡，跟决策那张各存各的、互不影响。

        用户 m07137：设置里写几轮就是几轮工具。框架的 max_tool_loop 数的是「总循环
        次数」（含最后写回复那一轮），所以这里 +1 再交给框架。
        """
        def _read(key, default):
            try:
                return _bounded_int(key, self._decision_get(key, default, sid), default)
            except Exception:
                return default
        return {
            "rounds": _read("reply_tool_rounds", 5) + 1,
            "calls_per_turn": _read("reply_tool_calls_per_turn", 5),
            "timeout": _read("reply_tool_timeout", 180),
            "output_tokens": _read("reply_tool_output_tokens", 10000),
        }

    def _apply_agent_limits(self, sid: str = "") -> None:
        """把回复侧的四项代理参数塞进 contextvar，框架读配置时就会拿到插件的值。"""
        if not getattr(self, "_limits_patched", False):
            self._limits_patched = _install_agent_limit_patch()
        _REPLY_LIMITS.set(self.reply_tool_limits(sid))

    def _decision_models(self) -> list:
        """框架里配好的模型清单。

        值 = provider:内部ID（ctx.get_llm_client 认这个格式），名字 = 供应商 / 模型名。
        直接读 system_config.json —— ctx 上没有「列出所有模型」的接口。
        """
        out = []
        try:
            from core.utils.path_utils import get_data_path

            path = os.path.join(str(get_data_path()), "config", "system_config.json")
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            logger.debug("[multi-decision] 读框架模型清单失败（页面只给跟随默认）", exc_info=True)
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
            for kind in ("llm", "vlm"):
                group = mcfg.get(kind)
                if not isinstance(group, dict):
                    continue
                for mid, item in group.items():
                    if not isinstance(item, dict):
                        continue
                    _mname = str(item.get("model_name") or mid)
                    out.append({
                        "value": "%s:%s" % (pid, mid),
                        "label": "%s / %s" % (pname, _mname),
                        # 用户 m15537：记录页把「渠道」拆成独立一列，这里把供应商名和
                        # 模型名分开给，前端不用再从 label 里切。
                        "provider": pname,
                        "model": _mname,
                        "kind": kind,
                    })
        defaults = data.get("models") if isinstance(data.get("models"), dict) else {}
        fast = str(defaults.get("default_fast_llm") or "")
        main = str(defaults.get("default_llm") or "")
        for it in out:
            it["fast"] = bool(fast) and it["value"] == fast
            it["main"] = bool(main) and it["value"] == main
        return out

    def jev_options(self, sid: str = ""):
        """用户 m06706：JEV 那一套配置（每轮现读，改完立即生效）。

        重试 / 间隔 / 连续失败切换 / 切回等待 / 超时 / 失败动作跟普通决策共用
        decision_* 那几项（设置页只有一份，用户不用学两套）；开关、模型、备用、
        区间、动作、人设是 jev_* 独立存的。
        """
        try:
            opt = jev_options_from_config(lambda k, d=None: self._decision_get(k, d, sid))
        except Exception:
            logger.exception("[multi-decision] 读 JEV 配置失败，按默认值继续")
            opt = jev_options_from_config(lambda k, d=None: d)
        # 用户 m08123：自定义接口的密钥单独落盘（data/plugin_data/<插件>/jev_secret.json），
        # 不进配置、不随 GET /config 下发，只在这里补进运行时选项。
        try:
            secrets = jev_load_secrets(getattr(self, "_data_dir", ""))
        except Exception:
            logger.debug("[multi-decision] 读 JEV 密钥失败（当没填）", exc_info=True)
            secrets = {}
        # 用户 m09981：每一页各有各的密钥；老的单份密钥已被 load_secrets 当成第 1 页。
        pages = []
        for item in list(opt.apis or []):
            one = dict(item)
            eid = normalize_jev_api_id(one.get("id") or "1")
            one["id"] = eid
            one["api_key"] = str((secrets.get(eid) or {}).get("api_key") or "")
            pages.append(one)
        opt.apis = pages
        opt.api_key = str((secrets.get("1") or {}).get("api_key") or "")
        return opt

    def _jev_models(self) -> list:
        """只列「启用提供商里的 decision 类模型」。

        用户 m06706：普通决策仍然只列 llm/vlm，JEV 这里必须看 decision 分组——
        它在 system_config.json 的 provider.model_config["decision"] 底下，不是 llm。
        框架没有默认 decision 模型，所以列表空就是真空：页面要给诊断，不能给
        「留空跟随默认」这种不存在的承诺。
        """
        try:
            return self.jev_phase.models()
        except Exception:
            logger.debug("[multi-decision] 列 JEV 模型失败", exc_info=True)
            return []

    def decision_mode(self, sid: str = "") -> str:
        """这一刻该走哪条决策链：off / llm / jev。

        用户 m06706：互斥不能只靠前端。保存 API、单群覆盖、有效配置读取、运行入口
        这四处都要落到同一个判定上，否则「全局开普通决策 + 这个群开 JEV」这种继承
        场景就会两套都跑。手改配置文件写成双开时按 JEV 走，记一条警告，绝不双调。
        """
        try:
            adv = bool(self.eff("advanced_enabled", sid))
        except Exception:
            adv = False
        try:
            jev = bool(self.eff("jev_enabled", sid))
        except Exception:
            jev = False
        if adv and jev:
            logger.warning(
                "[multi-decision] %s 同时开了「决策模式」和「JEV 决策」，按 JEV 走（不会两套都跑）",
                sid or "全局")
            return "jev"
        if jev:
            return "jev"
        if adv:
            return "llm"
        return "off"

    def _decision_messages(self, sid: str) -> list:
        """测试窗要读的那份聊天记录（窗口快照）。取不到就当空。

        用户 m01977：测试窗只留「用户发的纯文字」，bot 自己的话、工具往返、
        图片、表情、转发卡片全过滤掉，再取最近 10 条。测试只是看看模型怎么判，
        喂整份窗口反而看不清是哪句话影响了判断。

        真实决策链不走这里（decision/engine.py 自己取窗口），所以只影响
        「实时窗」显示与三个测试按钮。
        """
        try:
            raw = self.window.snapshot(sid) or []
        except Exception:
            logger.exception("[multi-decision] 取窗口快照失败（决策按空记录处理）")
            return []
        return _test_window_messages(raw)

    def _decision_identity(self) -> str:
        """决策提示词最后那段「身份与环境」。

        优先用框架 system_prompt 里的原文（accounts / sessions / chat_env / time，
        由 _on_llm_request 抄下来的那份）。热重载后还没跑过真实请求时那份是空的，
        这时自己拼一份等价的：我是谁、现在几点、手上都有哪些会话。
        """
        try:
            txt = self.decision_phase.identity_text()
        except Exception:
            txt = ""
        if txt.strip():
            return txt
        rows = []
        try:
            now = time.localtime()
            week = "一二三四五六日"[int(now.tm_wday) % 7]
            rows.append("现在是 %s（星期%s）" % (time.strftime("%Y-%m-%d %H:%M", now), week))
        except Exception:
            pass
        try:
            me = _framework_bot_identity() or {}
            if me.get("name") or me.get("qq"):
                rows.append("我是 %s（QQ %s）" % (me.get("name") or "bot", me.get("qq") or "未知"))
        except Exception:
            pass
        try:
            states = getattr(self.window, "_states", None) or {}
            bits = []
            for sid, st in list(states.items())[:15]:
                n = 0
                try:
                    n = len(getattr(st, "messages", None) or [])
                except Exception:
                    n = 0
                kind = "群" if str(sid).startswith("gm:") else ("私聊" if str(sid).startswith("dm:") else "会话")
                bits.append("%s %s（%d 条）" % (kind, str(sid).split(":", 1)[-1], n))
            if bits:
                rows.append("手上的会话：" + "、".join(bits))
        except Exception:
            pass
        return "\n".join(rows)

    def _decision_pick_sid(self) -> str:
        """v3.91：没指定会话时（全局配置模式下预览/自检没有 sid），
        挑一个窗口里消息最多的会话，省得预览出来一片空白。
        m10297：优先挑抓过框架原文的会话——提示词页的「你的身份 / 工具说明 /
        系统动态段」全靠那份原文，挑到没抓过的会话那些格子永远是 0 字。""" 
        try:
            states = getattr(self.window, "_states", None) or {}
            layouts = getattr(self, "prompt_layouts", None)
            sources = getattr(layouts, "_sources", None) or {}
            picked = [str(k) for k, v in list(sources.items())
                      if isinstance(v, dict) and (v.get("rows") or v.get("native"))]
            best, best_n = "", -1
            for sid, st in list(states.items()):
                try:
                    n = len(getattr(st, "messages", None) or [])
                except Exception:
                    n = 0
                if n > best_n:
                    best, best_n = str(sid), n
            if picked:
                live = [s for s in picked if s in states]
                if live:
                    return max(live, key=lambda k: len(getattr(states.get(k), "messages", None) or []))
                return max(picked, key=lambda k: (sources.get(k) or {}).get("at") or 0)
            return best
        except Exception:
            logger.debug("[multi-decision] 预览挑会话失败", exc_info=True)
            return ""

    async def _decision_preview(self, sid: str) -> dict:
        """装配一次但不发请求——提示词页的「组合预览」与自检都走它。"""
        dp = self.decision_phase
        if not sid:
            sid = self._decision_pick_sid()
        opt = self.decision_options(sid)
        layouts = getattr(self, "prompt_layouts", None)
        if layouts is not None:
            compiled = await layouts.assemble_decision(sid, opt)
            msgs = self._decision_messages(sid)
            tools = dp.pick_tools(opt.tools)
            return dict(compiled, sid=sid, transcript=dp.transcript(msgs, sid),
                        tools=[{"name": str(getattr(t, "name", "")), "description": str(getattr(t, "description", ""))} for t in tools],
                        persona_name="", messages=len(msgs), identity=bool(layouts.identity_text(sid)))
        # v4.90（用户 m14715 第 1 条）：decision_persona 已删除，DecisionOptions 里也没有
        # persona 字段了，这里不再去取人格原文——人格段由提示词布局（prompt_layouts）负责。
        persona = ""
        tools = dp.pick_tools(opt.tools)
        help_text = dp.tools_help(tools)
        if tools:
            help_text = (TOOL_HEAD % {"rounds": max(1, opt.tool_rounds),
                                      "calls": max(1, int(getattr(opt, "tool_calls_per_turn", 5) or 5))}) + "\n" + help_text
        ident = self._decision_identity()
        parts = dp.build_system(opt, persona, help_text, ident, sid)
        msgs = self._decision_messages(sid)
        return {
            "ok": True,
            "sid": sid,
            "parts": parts,
            "system": "\n\n".join(parts),
            "transcript": dp.transcript(msgs, sid),
            "tools": [{"name": str(getattr(t, "name", "")),
                       "description": str(getattr(t, "description", ""))} for t in tools],
            "persona": persona,
            "persona_name": "",
            "messages": len(msgs),
            "identity": bool(ident),
        }

    @_api("GET", "/prompt-layout")
    async def api_prompt_layout(self, kind: str = "decision", sid: str = ""):
        try:
            return await self.prompt_layouts.payload(kind, str(sid or "").strip())
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_LAYOUT", "error": str(exc)}

    @_api("GET", "/prompt-defaults")
    async def api_prompt_defaults(self):
        """用户 m23323：本地默认提示词的现状（存在哪、什么时候抄的、还缺哪几段）。"""
        try:
            return self.prompt_layouts.defaults_info()
        except Exception as exc:
            return {"ok": False, "code": "DEFAULTS_FAILED", "error": str(exc)}

    @_api("POST", "/prompt-defaults")
    async def api_sync_prompt_defaults(self, body: dict):
        """用户 m23323：把框架这一刻的默认提示词重新抄一份到本地（覆盖），装配就只读这份。"""
        try:
            sid = str((body or {}).get("sid") or "")
            if not sid:
                sid = self._decision_pick_sid()
            # 现取一次框架当前模板，再抄进本地副本——不写死出厂文案，跟框架升级同步。
            rows = await self.prompt_layouts.refresh_live(sid)
            info = self.prompt_layouts.capture_defaults(rows, source="框架当前模板 · 手动同步")
            if not info.get("ok"):
                return {"ok": False, "code": "SYNC_FAILED",
                        "error": str(info.get("error") or "同步失败：框架这次没给到可保存的段")}
            out = self.prompt_layouts.defaults_info()
            out.update({"ok": True, "synced": True})
            return out
        except Exception as exc:
            logger.exception("[multi-decision] 同步框架默认提示词失败")
            return {"ok": False, "code": "SYNC_FAILED", "error": str(exc)}

    @_api("POST", "/prompt-preview")
    async def api_prompt_preview(self, body: dict):
        try:
            if "layout" not in body or body["layout"] is None:
                raise ValueError("草稿预览必须包含 layout；空 system 请提交空文档")
            return await self.prompt_layouts.payload(body.get("kind", "decision"), str(body.get("sid") or "").strip(), draft=body["layout"])
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_LAYOUT", "error": str(exc)}

    @_api("POST", "/prompt-layout")
    async def api_save_prompt_layout(self, body: dict):
        try:
            if "layout" not in body:
                raise ValueError("保存必须包含 layout；null 表示恢复继承")
            kind, sid = body.get("kind", "decision"), str(body.get("sid") or "").strip()
            result = self.prompt_layouts.save(kind, sid, body["layout"], body.get("expected_revision"))
            if not result.get("ok"):
                return result
            return await self.prompt_layouts.payload(kind, sid)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_LAYOUT", "error": str(exc)}
        except OSError as exc:
            logger.exception("[multi-decision] 保存模块提示词失败，保留原配置")
            return {"ok": False, "code": "SAVE_FAILED", "error": str(exc)}

    @_api("POST", "/prompt-notice")
    async def api_prompt_notice_dismiss(self, body: dict):
        """2026-11-08（J）：关掉「默认提示词更新了」那条提示。

        只关这一版默认稿：换一版默认稿（指纹变了）还会再提一次。
        """
        try:
            kind, sid = body.get("kind", "decision"), str(body.get("sid") or "").strip()
            result = self.prompt_layouts.dismiss_notice(kind, sid, str(body.get("module") or ""))
            if not result.get("ok"):
                return result
            return await self.prompt_layouts.payload(kind, sid)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_LAYOUT", "error": str(exc)}
        except OSError as exc:
            logger.exception("[multi-decision] 关闭默认稿更新提示失败")
            return {"ok": False, "code": "SAVE_FAILED", "error": str(exc)}

    @_api("POST", "/prompt-tools")
    async def api_prompt_tools(self, body: dict):
        """提示词页「工具说明」段里的勾选（用户 m02159 图3）：勾了才注入、才允许模型调用。

        用户 m11103：kind=decision 写决策链那份，kind=reply 写回复链那份；
        不带 kind 按决策链处理（老页面发的就是这个）。
        """
        kind = str(body.get("kind") or "decision").strip().lower()
        try:
            if kind == "reply":
                names = self.prompt_layouts.set_reply_tools(body.get("names") or [])
            else:
                names = self.prompt_layouts.set_tools(body.get("names") or [])
            return {"ok": True, "kind": kind, "names": names,
                    "layout_revision": self.prompt_layouts.revision()}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_TOOLS", "error": str(exc)}
        except OSError as exc:
            logger.exception("[multi-decision] 保存工具勾选失败")
            return {"ok": False, "code": "SAVE_FAILED", "error": str(exc)}

    @_api("GET", "/prompt-templates")
    async def api_prompt_templates(self):
        """自定义模板库（用户 m01209 图5：模板以卡片形式展示、可增删改）。"""
        return {"ok": True, "templates": self.prompt_layouts.templates(),
                "layout_revision": self.prompt_layouts.revision()}

    @_api("POST", "/prompt-templates")
    async def api_save_prompt_template(self, body: dict):
        try:
            action = str(body.get("action") or "save").strip()
            # save_template 返回的是模板本身、delete_template 返回 True，都不带 ok 字段，
            # 这里必须自己包一层 ok（前端按 ok 判断成败，否则保存成功也报「HTTP 200」）。
            item = {}
            if action == "delete":
                self.prompt_layouts.delete_template(str(body.get("id") or ""))
            else:
                item = self.prompt_layouts.save_template(body.get("name", ""), body.get("content", ""),
                                                         str(body.get("id") or ""))
            return {"ok": True, "templates": self.prompt_layouts.templates(),
                    "item": item if isinstance(item, dict) else {},
                    "layout_revision": self.prompt_layouts.revision()}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_TEMPLATE", "error": str(exc)}
        except OSError as exc:
            logger.exception("[multi-decision] 保存自定义模板失败，保留原文件")
            return {"ok": False, "code": "SAVE_FAILED", "error": str(exc)}

    @_api("GET", "/reply-preview")
    async def api_reply_preview(self, sid: str = ""):
        """设置页「回复提示词」右栏：最近一次真实请求的完整系统提示词。

        我们那一段（聊天规则）用当前配置重写，改完立刻可见；框架那几段要等下一轮
        真实请求才会刷新——那不是插件拼的，拼不出来。
        """
        sid = str(sid or "").strip() or self._decision_pick_sid()
        return self.reply_prompt.preview(sid)

    @_api("GET", "/decision-options")
    async def api_decision_options(self, sid: str = ""):
        """提示词页要的三份清单（人格 / 工具 / 模型）+ 现在生效的配置 + 预填稿。"""
        sid = str(sid or "").strip()
        dp = self.decision_phase
        personas = []
        try:
            personas = await dp.persona_options()
        except Exception:
            logger.exception("[multi-decision] 取人格列表失败（页面按空列表渲染）")
        opt = self.decision_options(sid)
        return {
            "ok": True,
            "sid": sid,
            "personas": personas,
            "tools": dp.tool_catalog(refresh=True),
            "models": self._decision_models(),
            "config": {
                "persona": "",
                "chat_rules": opt.chat_rules,
                "prompt": opt.prompt,
                "tools": list(opt.tools or []),
                "model": opt.model,
                "fallback": opt.fallback,
                "retry": opt.retry,
                "retry_delay": opt.retry_delay,
                "fail_switch": opt.fail_switch,
                "cooldown": opt.cooldown,
                "timeout": opt.timeout,
                "fail_action": opt.fail_action,
                "tool_rounds": opt.tool_rounds,
            },
            "defaults": {"chat_rules": DEFAULT_CHAT_RULES, "prompt": DEFAULT_DECISION_PROMPT},
            "status": dp.status(),
        }

    @_api("GET", "/model-check")
    async def api_model_check(self, sid: str = ""):
        """用户 m01874：设置页红点——哪些模型还没配。

        只报「真的会用到、而且现在没有」的：回复模型（留空时看框架默认模型）、
        决策模型（留空时看框架快速模型）、JEV 模型（框架没有默认 decision 模型）。
        备用模型留空不算——那是可选的，用户明确说过不用亮。
        """
        sid = str(sid or "").strip()
        try:
            models = self._decision_models()
        except Exception:
            logger.debug("[multi-decision] 读框架模型清单失败（红点按没配算）", exc_info=True)
            models = []
        fast_ok = any(bool(m.get("fast")) for m in models)
        main_ok = any(bool(m.get("main")) for m in models)
        try:
            mode = self.decision_mode(sid)
        except Exception:
            mode = "off"

        def _val(key):
            try:
                return str(self._decision_get(key, "", sid) or "").strip()
            except Exception:
                return ""

        no_model = "框架里还没有任何模型，先去框架设置里加一个。"
        missing = []
        if not _val("reply_model") and not main_ok:
            missing.append({
                "key": "reply_model", "page": "prompt", "pane": "reply",
                "label": "回复模型",
                "hint": no_model if not models else "框架没配默认模型，这里也没选，回复发不出去。",
            })
        if mode == "llm" and not _val("decision_model") and not fast_ok:
            missing.append({
                "key": "decision_model", "page": "prompt", "pane": "decision",
                "label": "决策模型",
                "hint": no_model if not models else "框架没配快速模型，这里也没选，决策跑不起来。",
            })
        if mode == "jev":
            jm = _val("jev_model")
            if not jm and not compat.has_default_decision():
                missing.append({
                    "key": "jev_model", "page": "prompt", "pane": "jev",
                    "label": "JEV 模型",
                    "hint": "JEV 决策开着但没选模型，框架没有默认的 decision 模型可以顶。",
                })
            elif jm.startswith("custom:"):
                pid = jm.partition(":")[2]
                page = None
                try:
                    for one in (self.jev_options(sid).apis or []):
                        if str(one.get("id") or "") == pid:
                            page = one
                            break
                except Exception:
                    page = None
                if page is not None and not str(page.get("model") or "").strip():
                    missing.append({
                        "key": "jev_model", "page": "prompt", "pane": "jev",
                        "label": "JEV 模型",
                        "hint": "选的是自定义接口，但那一页没填模型名。",
                    })
        return {"ok": True, "sid": sid, "mode": mode, "missing": missing}

    @_api("GET", "/jev-options")
    async def api_jev_options(self, sid: str = ""):
        """用户 m06706：JEV 页要的模型清单（只有 decision 类）+ 生效配置 + 宿主诊断。

        跟 /decision-options 分开：那边给的是 llm/vlm 的普通决策模型，这里给的是
        decision 类。区间按百分比回给页面（40 / 60），内部还是 0~1。
        """
        sid = str(sid or "").strip()
        opt = self.jev_options(sid)
        try:
            mode = self.decision_mode(sid)
        except Exception:
            mode = "off"
        return {
            "ok": True,
            "sid": sid,
            "models": self._jev_models(),
            # 用户 m09981：自定义接口可以有好几页，页面按这个列表画页签。
            "apis": [
                {
                    "id": str(one.get("id") or "1"),
                    # 用户 m01978 第 3 条：自定义接口页可以自己起名，页面标签与模型下拉都用它。
                    "name": str(one.get("name") or ""),
                    "protocol": normalize_jev_protocol(one.get("protocol")),
                    "base": str(one.get("base") or ""),
                    "model": str(one.get("model") or ""),
                    "endpoint": jev_endpoint_url(one.get("protocol"),
                                                 str(one.get("base") or "")),
                    "api_key_set": bool(one.get("api_key")),
                    "api_key_masked": jev_mask_key(str(one.get("api_key") or "")),
                }
                for one in (opt.apis or [])
            ],
            "enabled": bool(opt.enabled),
            "mode": mode,
            "config": {
                "model": opt.model,
                "fallback": opt.fallback,
                "backend": normalize_jev_backend(opt.backend),
                "api_base": opt.api_base,
                # 用户 m08755 衍生：两套协议目标路径不同，报给页面的也得跟着换。
                "api_endpoint": (jev_api_chat_url(opt.api_base)
                                 if normalize_jev_protocol(opt.protocol) == "openai"
                                 else jev_api_endpoint(opt.api_base)),
                "api_model": opt.api_model,
                "api_protocol": normalize_jev_protocol(opt.protocol),
                "api_key_set": bool(opt.api_key),
                "api_key_masked": jev_mask_key(opt.api_key),
                "lower": opt.lower,
                "upper": opt.upper,
                "lower_percent": int(round(opt.lower * 100)),
                "upper_percent": int(round(opt.upper * 100)),
                "action": opt.action,
                "persona": opt.persona,
                "retry": opt.retry,
                "retry_delay": opt.retry_delay,
                "fail_switch": opt.fail_switch,
                "cooldown": opt.cooldown,
                "timeout": opt.timeout,
                "fail_action": opt.fail_action,
            },
            "defaults": {
                "prompt": DEFAULT_JEV_PROMPT,
                "lower_percent": int(round(DEFAULT_JEV_LOWER * 100)),
                "upper_percent": int(round(DEFAULT_JEV_UPPER * 100)),
                "action": "no_reply",
                "backend": "framework",
                "api_base": DEFAULT_JEV_API_BASE,
                "api_model": DEFAULT_JEV_API_MODEL,
                "api_protocol": "systemone",
                "apis": [],
            },
            "status": self.jev_phase.status(),
        }

    @_api("POST", "/jev-key")
    async def api_jev_key(self, body: dict = None):
        """存 / 清自定义接口的密钥（用户 m08123）。

        密钥永远不回页面：存完只回 api_key_set 与打码后的尾巴四位。
        body 里 api_key 为空字符串 = 清除。
        """
        body = body if isinstance(body, dict) else {}
        raw_key = body.get("api_key")
        _data_dir = getattr(self, "_data_dir", "")
        # 用户 m09981：密钥按页签分开存，body.id 不传就当第 1 页（兼容老页面）。
        eid = normalize_jev_api_id(body.get("id") or body.get("eid") or "1")
        if raw_key is None:
            # 只传了 api_base：不碰密钥，单独改地址（地址本身也是配置项，这里只是兜底）
            return {"ok": True, "id": eid, "api_key_set": bool(
                jev_load_secret(_data_dir, eid).get("api_key")), "skipped": True}
        key_text = str(raw_key).strip()
        try:
            if not key_text:
                ok = jev_clear_secret(_data_dir, eid)
                return {"ok": bool(ok), "id": eid, "api_key_set": False, "masked": "",
                        "error": "" if ok else "删除密钥失败"}
            if not jev_save_secret(_data_dir, key_text,
                                   str(body.get("api_base") or "").strip(), eid):
                return {"ok": False, "error": "保存密钥失败（插件数据目录不可写）",
                        "api_key_set": False}
            return {"ok": True, "id": eid, "api_key_set": True, "masked": jev_mask_key(key_text)}
        except Exception as exc:
            logger.exception("[multi-decision] 保存 JEV 密钥失败")
            return {"ok": False, "id": eid, "error": "%s: %s" % (type(exc).__name__, exc),
                    "api_key_set": False}

    @_api("GET", "/jev-preview")
    async def api_jev_preview(self, sid: str = ""):
        """装配一次但**不发请求**：JEV 页的「组合预览」与自检都走它。

        用户 m06706：预览必须跟真实发送同源，所以这里直接调 assemble_jev，
        不另拼一份提示词——否则「预览看着对、真发出去不对」这种坑又要踩一次。
        """
        sid = str(sid or "").strip()
        if not sid:
            try:
                sid = self._decision_pick_sid()
            except Exception:
                sid = ""
        try:
            opt = self.jev_options(sid)
            layouts = self.prompt_layouts
            compiled = await layouts.assemble_jev(sid, opt)
            msgs = self._decision_messages(sid)
            return dict(compiled, ok=True, sid=sid, messages=len(msgs),
                        transcript=self.decision_phase.transcript(msgs, sid),
                        lower=opt.lower, upper=opt.upper, action=opt.action,
                        model=opt.model, fallback=opt.fallback)
        except Exception as exc:
            logger.exception("[multi-decision] JEV 预览失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "parts": [], "system": ""}

    @_api("POST", "/jev-test")
    async def api_jev_test(self, sid: str = ""):
        """立刻跑一次真 JEV 判定（真调 decision 模型）。

        用户 m06706：跟 /decision-test 一样只把结果回给页面——不写窗口、不发言、
        不进决策记录，转交也关掉（测试按钮不该顺手多调一次普通决策）。
        """
        sid = str(sid or "").strip()
        try:
            opt = self.jev_options(sid)
            got = await self.jev_phase.decide(
                sid, self._decision_messages(sid), opt,
                normal_opt=None, allow_transfer=False)
        except Exception as exc:
            logger.exception("[multi-decision] JEV 测试失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "result": {}}
        return {"ok": True, "result": got}

    @_api("GET", "/jev-api-models")
    async def api_jev_api_models(self, base: str = "", id: str = "", protocol: str = ""):
        """替用户去第三方接口要一次模型清单（用户 m08755 的「自动获取」）。

        用户 m09981：按页签取——传 id 就用那一页的地址与密钥；不传当第 1 页。
        用户 m10825：SystemOne 没有清单接口，按协议直接回内置候选；OpenAI 兼容才问 /models。
        只读：拿不到就回空清单 + 原因，页面据此提示手填，不影响任何已存配置。
        """
        want_id = str(id or "").strip()
        eid = normalize_jev_api_id(want_id or "1")
        raw = str(base or "").strip()
        try:
            pages = jev_parse_apis(self._decision_get("jev_apis", ""))
        except Exception:
            pages = []
        page = next((p for p in pages
                     if normalize_jev_api_id(p.get("id") or "1") == eid), None)
        if not raw:
            if page is not None:
                raw = str(page.get("base") or "").strip()
                if not raw:
                    # 这一页还没填地址，不能拿别的页去顶（用户 m09981 的多页签）。
                    return {"ok": False, "models": [], "url": "",
                            "error": "第 %s 页还没填接口地址" % eid}
            elif want_id or pages:
                return {"ok": False, "models": [], "url": "",
                        "error": "配置里找不到第 %s 页" % eid}
        if not raw:
            # 只有完全没指定页、也没有多页配置时，才回落老的单接口地址。
            raw = str(self._decision_get("jev_api_base", "") or "").strip()
        proto_raw = str(protocol or "").strip()
        if not proto_raw:
            proto_raw = str((page or {}).get("protocol") or "").strip()
        if not proto_raw:
            proto_raw = str(self._decision_get("jev_api_protocol", "") or "").strip()
        proto = normalize_jev_protocol(proto_raw)
        try:
            key = str(jev_load_secret(getattr(self, "_data_dir", ""), eid).get("api_key") or "")
        except Exception:
            key = ""
        try:
            models, url, error = jev_list_api_models(raw, key, protocol=proto)
        except Exception as exc:
            logger.exception("[multi-decision] 获取第三方模型清单失败")
            return {"ok": False, "models": [], "url": "",
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": bool(models), "models": models, "url": url, "error": error,
                "protocol": proto, "builtin": proto == "systemone"}

    @_api("GET", "/window-text")
    async def api_window_text(self, sid: str = ""):
        """摊出某个会话当前的实时窗原文（JEV 页「实时窗」模块用它填正文）。

        用户 m08755：实时窗要读真正的窗口，测试才能用当前最新的聊天内容。
        """
        sid = str(sid or "").strip()
        if not sid:
            try:
                sid = self._decision_pick_sid()
            except Exception:
                sid = ""
        try:
            msgs = self._decision_messages(sid)
        except Exception:
            msgs = []
        try:
            text = self.decision_phase.transcript(msgs, sid) if msgs else ""
        except Exception:
            text = ""
        return {"ok": True, "sid": sid, "messages": len(msgs), "text": text}

    @_api("GET", "/decision-preview")
    async def api_decision_preview(self, sid: str = ""):
        """组合预览：人格 / 聊天规则 / 决策词 / 工具 / 身份环境按顺序拼出来看。"""
        try:
            return await self._decision_preview(str(sid or "").strip())
        except Exception as exc:
            logger.exception("[multi-decision] 决策预览失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    @_api("POST", "/decision-test")
    async def api_decision_test(self, sid: str = ""):
        """立刻跑一次真决策（真调模型）。结果只回给页面，不写窗口、不发言。"""
        sid = str(sid or "").strip()
        # 测试按钮不该把异常甩成 500（页面只会显示 Internal Server Error，看不出是哪一步挂的）。
        try:
            # 用户 m01977：测试窗只喂纯文字，图也一并去掉——窗口里已经没有图了，
            # 再单独挂一份只会跟页面上显示的内容对不上。
            got = await self.decision_phase.decide(
                sid, self._decision_messages(sid), self.decision_options(sid), images=[])
        except Exception as exc:
            logger.exception("[multi-decision] 决策测试失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "result": {}}
        return {"ok": True, "result": got}

    @_api("POST", "/reply-test")
    async def api_reply_test(self, sid: str = ""):
        """立刻跑一次真回复（真调回复模型）。结果只回给页面，不写窗口、不发言。

        用户 m01977：回复页也要一个测试窗，跟决策 / JEV 一样只喂「实时窗」那份
        纯文字记录，顺便把模型的思考过程带回来。
        """
        sid = str(sid or "").strip()
        if not sid:
            try:
                sid = self._decision_pick_sid()
            except Exception:
                sid = ""
        try:
            prev = self.reply_prompt.preview(sid) or {}
        except Exception as exc:
            logger.exception("[multi-decision] 回复测试取提示词失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "result": {}}
        system_text = str(prev.get("system") or "")
        if not system_text.strip():
            return {"ok": False, "error": "回复提示词是空的（先去提示词页把模块填上）", "result": {}}
        msgs = self._decision_messages(sid)
        payload = [{"role": "system", "content": system_text}] + list(msgs)
        clients = []
        try:
            clients = self.engine._reply_model_clients(sid) or []
        except Exception:
            logger.exception("[multi-decision] 回复测试取回复模型失败（改用框架默认模型）")
            clients = []
        if not clients:
            client = self._default_llm_client()
            if client is not None:
                clients = [client]
        if not clients:
            return {"ok": False, "error": "取不到回复模型客户端", "result": {}}
        started = time.time()
        got = {}
        last_err = ""
        for client in clients:
            got = await self.decision_phase.one_shot(payload, client=client)
            if got.get("ok"):
                break
            last_err = str(got.get("error") or "")
        result = {
            "ok": bool(got.get("ok")),
            "error": "" if got.get("ok") else (last_err or "回复模型没有返回"),
            "text": str(got.get("text") or ""),
            "reasoning": str(got.get("reasoning") or ""),
            "model": str(got.get("model") or ""),
            "elapsed": round(time.time() - started, 3),
            "input_tokens": int(got.get("in") or 0),
            "output_tokens": int(got.get("out") or 0),
            "cached_tokens": int(got.get("cached") or 0),
            "messages": len(msgs),
            "system_chars": len(system_text),
            "sid": sid,
        }
        return {"ok": True, "result": result}

    # 注意：/decisions 这个路由老早被「消息日志」占了（下面那个 api_decisions），
    # 同名再挂一个只会被后注册的盖掉——日志里能看到 /decisions 注册了两遍。
    # 决策记录换成 /decision-rounds（v3.91，用户 m05749）。
    @_api("GET", "/decision-rounds")
    async def api_decision_rounds(self, sid: str, limit: int = 20, light: int = 0):
        """决策记录：跟回复记录同一套模板，只是 ① 换成决策用的提示词。"""

        evidence = await self._reply_evidence(sid)
        return await asyncio.to_thread(self._requests_payload, sid, limit, "decision", bool(light),
                                       False, evidence)

    @_api("GET", "/band-stats")
    async def api_band_stats(self, sid: str = "", week: str = ""):
        """时段曲线的「这一段现在怎么样」：平均消息 / 触发 / 回复 + 目标 + 现在用的概率。

        v3.71（用户 m23596 第 6/8 条）：设置页点段上的数字弹面板要用这份数据。
        sid 为空 = 全部会话合起来看；这时 p 就是配置值（调参是按会话存的）。

        week=1 时再多给一份 week 数组：周一..周日各自的同一份统计
        （用户 m03323 —— 开了「使用星期数据」后面板要按天分开看）。
        """
        sid = str(sid or "").strip()
        try:
            bands = self.engine._band_list(sid) if self.engine is not None else []
        except Exception:
            bands = []
        week_days = max(1, int(self.eff("stats_window_days", sid) or 7))
        bucket = bool(self.eff("adaptive_weekday_bucket", sid))
        win_weeks = max(4, int(self.eff("adaptive_weekday_window_weeks", sid) or 8))
        win_days = win_weeks * 7
        data = {}
        try:
            data = self.band_stats.band_stats(
                sid,
                bands,
                weekday_bucket=bucket,
                weekday_window_days=win_days,
                week_days=week_days,
            )
        except Exception:
            logger.exception("[multi-decision] 按时段统计失败（页面按空数据渲染）")
        rows = data.get("bands") if isinstance(data, dict) else None
        rows = rows if isinstance(rows, list) else []
        # v3.73（用户 m24640 第 5 条）：告诉前端这一段是不是休息时段（画月亮用）
        ranges = []
        try:
            if self.engine is not None:
                ranges = self.engine._rest_ranges(sid)
        except Exception:
            ranges = []
        out_bands = []
        for it in bands:
            start, end, value = int(it[0]), int(it[1]), int(it[2])
            want_rounds = it[3] if len(it) > 3 else 0
            want_rate = it[4] if len(it) > 4 else 0
            row = {}
            for r in rows:
                try:
                    if int(r.get("s")) == start and int(r.get("e")) == end:
                        row = r
                        break
                except (TypeError, ValueError):
                    continue
            tuned = None
            manual = False
            if sid and self.band_tune is not None:
                try:
                    # v3.84（用户 m04084 第 1/2 条）：调参结果按星期分桶存，读也要按今天取；
                    # 段概率被手工改过时 BandTuner 会打 manual 标，页面据此显示「手动」。
                    wd = int(time.localtime().tm_wday) if bucket else None
                    tuned = self.band_tune.prob(sid, start, end, v=value, weekday=wd)
                    manual = bool(self.band_tune.is_manual(sid, start, end, v=value, weekday=wd))
                except Exception:
                    tuned = None
                    manual = False
            out_bands.append({
                "s": start,
                "e": end,
                "v": value,
                "p": float(tuned) if tuned is not None else float(value),
                "tuned": tuned is not None,
                "manual": manual,
                "tr": want_rounds or 0,
                "tp": want_rate or 0,
                "rest": bool((start, end) in ranges),
                "m_day": row.get("m_day"),
                "t_day": row.get("t_day"),
                "r_day": row.get("r_day"),
                "reply_rate": row.get("reply_rate"),
                "msg_rate": row.get("msg_rate"),
            })
        out = {
            "ok": True,
            "sid": sid,
            "days": (data or {}).get("days", 0),
            "source": (data or {}).get("source", "week7"),
            "active_days": (data or {}).get("active_days", 0),
            "bands": out_bands,
            "rest": self._rest_payload(sid),
        }
        # 用户 m03323：开了「使用星期数据」时，动态调整面板要按 7 天分开看数据。
        # 只在页面明确要（week=1）时才算——7 个桶各扫一遍，没必要每次刷新都付这个钱。
        if str(week or "").strip() not in ("", "0", "false", "False", "no"):
            try:
                wk = self.band_stats.weekday_band_stats(sid, bands, window_days=win_days)
            except Exception:
                logger.exception("[multi-decision] 按星期分段统计失败（页面按空数据显示）")
                wk = {}
            out["week"] = (wk or {}).get("week") or []
        return out

    @_api("GET", "/hour-histogram")
    async def api_hour_histogram(self, sid: str = "", days: int = 7):
        """近 N 天的 24 小时消息分布，外加「休息时段盖住了多少」。"""
        try:
            days = max(1, min(int(days or 7), 60))
        except (TypeError, ValueError):
            days = 7
        hist = self._log.hour_histogram(days, sid, only_groups=True) if self._log else {"hours": [0] * 24, "slots": [0] * 48, "total": 0, "days": days}
        try:
            rest = self.rest_overlap(sid, days)
        except Exception:
            rest = {}
        return {
            "ok": True,
            "sid": sid,
            "days": hist.get("days", days),
            "hours": hist.get("hours", [0] * 24),
            # v3.28：时段曲线编辑器按 30 分钟一格画底图，这里顺带给出 48 格分布
            "slots": hist.get("slots", [0] * 48),
            # v3.75（用户本轮第 2 条）：曲线页要能叠一条「bot 回复」曲线
            "bot_slots": hist.get("bot_slots", [0] * 48),
            "total": hist.get("total", 0),
            "rest": rest,
        }

    @_api("GET", "/overview")
    async def api_overview(self, light: int = 0):
        """概览：今日统计 + 各会话运行时状态。"""
        sessions = []
        try:
            for sid, st in self.engine._states.items():
                if self._is_hidden(sid):
                    continue
                snap = st.snapshot()
                snap["window_size"] = self.window.size(sid) if self.window else 0
                snap["eff"] = self._eff_summary(sid)
                # v3.52（用户 m08226 第 2 条）：左栏「静默 / 休兜」标签要倒计时，
                # 后端直接给下一次自动冒泡的绝对时刻，前端每秒减就行。
                snap["silent_eta"] = self.engine.silent_eta(sid)
                # 用户 m11091 第 1 条：冒过泡就在胶囊上打勾，来新消息才撤
                snap["silent_fired_ts"] = float(getattr(st, "silent_fired_ts", 0.0) or 0.0)
                snap["rest_fired_ts"] = float(getattr(st, "rest_fired_ts", 0.0) or 0.0)
                # 用户 m02245：待发里只有链接 / 转发 / 图片时兜底不冒泡，左栏标签据此提示
                snap["silent_hold_ts"] = float(getattr(st, "silent_hold_ts", 0.0) or 0.0)
                sessions.append(snap)
        except Exception:
            logger.exception("[multi-decision] 读取会话状态失败")
        # 同上：内存状态里没有、但用户手动设过覆盖或别名的会话也要列出来（v2.32）
        try:
            known = {str(s.get("sid") or "") for s in sessions}
            for src in (
                (self.overrides.all() if self.overrides else {}),
                (self.aliases.all() if self.aliases else {}),
                (self.bot_names or {}),
            ):
                for sid in (src or {}):
                    sid = str(sid or "")
                    if not sid or sid in known or self._is_hidden(sid):
                        continue
                    sessions.append(
                        {
                            "sid": sid,
                            "total_messages": 0,
                            "total_triggers": 0,
                            "total_replies": 0,
                            "source": "override",
                            "eff": self._eff_summary(sid),
                        }
                    )
                    known.add(sid)
        except Exception:
            logger.exception("[multi-decision] 补回覆盖/别名会话失败")
        sessions.sort(key=lambda s: s.get("total_messages", 0), reverse=True)
        # v3.19（用户 m41511 第 5 条）：概览卡片从「今天」改成「最近 24 小时」。
        # 按自然日切的话 0 点一过昨天的量全掉，用户看到的数字会莫名归零。
        # v3.66（用户 m19536 第 6 条）：这里原来直接调 stats_recent(24)，绕过了
        # _recent24() 的 20 秒缓存。设置页冷开一次实测 1.9 秒，同步读盘卡在事件
        # 循环里，同页另外七八个请求一起排队——用户感觉就是「进设置卡很久」。
        # 现在吃缓存；下面会就地改 groups / privates，所以先深拷一层。
        today = dict(self._recent24() or {})
        for _k in ("groups", "privates"):
            if isinstance(today.get(_k), dict):
                today[_k] = dict(today[_k])
        # v3.46（用户 2026-10-24 图6）：请求卡上的「失败 / 重试」得跟请求表里的红行对上。
        # 上面的 today 走的是消息日志的 round_end，只有「结算了」的轮才写，
        # 看门狗强制释放、请求异常终止的那几轮一行都没有 → failed / retried 恒 0，
        # 卡片就假成「成功率 100%」。真实失败只在 requests/<sid>/*.json 每轮记录里
        # （error 写着「投递后 180 秒没有任何结算」那种），重试轮要对 retry_history 认。
        # 只覆盖请求相关的五个数（rounds/failed/replied/retried/silent）；
        # 消息 / 触发 / 拒绝 / by_session / groups.silent 仍是消息日志口径。
        try:
            req = None
            cache = getattr(self, "_req24_cache", None)
            if cache and (time.time() - cache[0]) < 15:
                # /overview 被前端 5 秒轮询一次，24 小时的请求记录可能有上千个大 json，
                # 不能每轮都重读；15 秒的缓存足够让新请求很快出现，又不会拖住接口。
                req = cache[1]
            else:
                # 冷跑一次是实打实的读盘：线上 24 小时窗口里 161 份 json、最大一份 200 KB，
                # 实测约 0.7 秒。直接在事件循环里同步扫会把 bot 收发消息堵住，
                # 所以丢进线程算——stats_recent / retry_events 只读文件、不碰共享状态，
                # 换线程安全。

                def _scan_recent():
                    # v3.48（用户 m06761 第 1 条）：两个来源各缺一块——消息日志只记
                    # 「结算过」的轮（看门狗释放、异常终止一行都没有，failed 恒 0），
                    # 请求记录每会话只留 keep 份（24 小时超过 100 轮前面的被裁掉）。
                    # 按 event_id 合并同一轮取并集，才是真正的 24 小时数字。
                    retry_events = (self.retry_history.retry_events()
                                    if self.retry_history else set())
                    dec_rows = self._log.recent_round_rows(24) if self._log else []
                    req_rows = (self.request_log.recent_round_rows(24, retry_events)
                                if self.request_log else [])
                    return merge_rounds(dec_rows, req_rows)

                req = await asyncio.to_thread(_scan_recent)
                if req is not None:
                    self._req24_cache = (time.time(), req)
            # rounds 为 0 = 窗口内压根没有请求记录（刚装、请求记录开关关着、
            # 或重启后还没发过请求）：这时保留消息日志口径，别把卡片数字抹成 0。
            if req and req.get("rounds"):
                today["rounds"] = int(req["rounds"])
                today["failed"] = int(req.get("failed") or 0)
                today["replied"] = int(req.get("replied") or 0)
                today["retried"] = int(req.get("retried") or 0)
                # v3.47（用户 2026-10-24 第 2 条）：真不回复也按合并口径走，
                # 否则上面三个数换了口径、silent 还留在消息日志口径，卡片对不上。
                today["silent"] = int(req.get("silent") or 0)
                # 成功率跟前端同一口径（web/index.html 用的是 replied / rounds），
                # 顺手给后端一份方便别处直接用；没有轮次给 None，卡片自己显示「—」。
                today["success_rate"] = req.get("success_rate")
                # v3.48（用户 m06761 第 1 条）：分群 / 私聊的轮次计数也换成合并口径，
                # 免得「群聊 177 次」还是消息日志、回复却来自请求记录。消息条数不动，
                # 那一项仍归消息日志（请求记录里没有「收到几条消息」）。
                for _kind in ("groups", "privates"):
                    _src = (req.get("by_kind") or {}).get(_kind) or {}
                    _dst = today.setdefault(_kind, {})
                    for _k in ("rounds", "replied", "failed", "retried", "silent"):
                        if _k in _src:
                            _dst[_k] = int(_src[_k] or 0)
        except Exception:
            logger.debug("[multi-decision] 用请求记录校正概览统计失败", exc_info=True)
        # 用户本轮：概览条在「平均回复间隔」右边加一格「最近24小时决策」。
        # 决策轮不是请求（write_decision_snapshot 走 ledger=False），requests 账本里
        # 没有它，只能数 requests/<sid>/*.json 里的决策快照。一次扫盘实测约 1 秒
        # （线上 270 份快照 / 15 MB），所以跟上面请求扫描一个待遇：丢线程 + 60 秒缓存，
        # 别把 5 秒轮询的 /overview 堵在事件循环里；冷跑失败就不给这个键，
        # 前端卡片写「—」，不假装 0 次。
        try:
            cache = getattr(self, "_dec24_cache", None)
            if cache and (time.time() - cache[0]) < 60:
                today["decisions"] = dict(cache[1] or {})
            elif self.request_log is not None:

                dec = await asyncio.to_thread(self.request_log.decision_stats, 24)
                if isinstance(dec, dict):
                    self._dec24_cache = (time.time(), dec)
                    today["decisions"] = dict(dec)
        except Exception:
            logger.debug("[multi-decision] 统计最近 24 小时决策次数失败", exc_info=True)
        if today.get("by_session"):
            today["by_session"] = {
                sid: agg
                for sid, agg in today["by_session"].items()
                if not self._is_hidden(sid)
            }
        if not sessions:
            # 插件刚重载时内存会话状态是空的，用今日日志聚合兜底，日志页不至于一片空白
            for sid, agg in (today.get("by_session") or {}).items():
                sessions.append(
                    {
                        "sid": sid,
                        "total_messages": agg.get("messages", agg.get("total", 0)),
                        "total_triggers": agg.get("rounds", 0),
                        "total_replies": agg.get("replied", 0),
                        "source": "today_log",
                        "eff": self._eff_summary(sid),
                    }
                )
            sessions.sort(key=lambda s: s.get("total_messages", 0), reverse=True)
        # 最近 24 小时 token：概览卡片要「这段时间吃了多少 token / 命中多少」（用户 m33573 图4）
        # v3.19 起换成滚动窗口，顺带带上每轮平均用时（「请求平均用时」卡片）
        try:
            if self.tracer is not None:
                today["tokens"] = self.tracer.recent_totals(24)
        except Exception:
            logger.debug("[multi-decision] 取今日 token 失败", exc_info=True)
        # 最近 24 小时花费：上面那批 token 乘用户手填的单价（用户 m02080 第 4 条）
        try:
            if self.pricing is not None:
                # 用户 m04493：同一个模型可能落成 pid:mid 与模型名两个 key，先归并再计价
                _tk = today.get("tokens") or {}
                _alias = self._model_alias(_tk, extra=self.pricing.all().keys())
                today["cost"] = self.pricing.summarize(
                    self._merge_model_totals(_tk, _alias),
                    prices=self._canon_prices(self.pricing.all(), _alias))
        except Exception:
            logger.debug("[multi-decision] 计算最近 24 小时花费失败", exc_info=True)
        # light=1：概览卡片只用 tokens 的汇总与 kinds，不用逐模型 usage；
        # 设置页更是只要 sessions / aliases / hidden。把 models 剥掉，
        # 一份 overview 从 ~94KB 降到 ~12KB。
        if light:
            _tk = today.get("tokens")
            if isinstance(_tk, dict):
                _tk.pop("models", None)
        # 用户第 4 条：概览卡和缓存记录页必须说同一件事，所以这里直接用缓存表
        # （_cache_kinds）的数字，不再走 _disk_usage 的目录统计——那个会把转发图
        # fw_ / 推文图 tw_ / 未归属的图全算进 media_cache（线上 1086 MB），而缓存页
        # 的「图片缓存」只算能归属到会话的聊天图（697 MB），两边差 388 MB。
        # 冷跑一次要扫几万个文件，丢线程算，别把 5 秒轮询堵在事件循环里；
        # _cache_kinds_cached 自己有 10 秒缓存，和 /cache-stats 共用同一份结果。
        try:
            mem = await asyncio.to_thread(self._mem_usage)
        except Exception:
            logger.exception("[multi-decision] 统计插件数据占用失败，退回目录统计")
            mem = await asyncio.to_thread(_disk_usage)
        try:
            if self.window is not None:
                states = getattr(self.window, "_states", {}) or {}
                mem["window_sessions"] = len(states)
                mem["window_msgs"] = sum(self.window.counted(sid) for sid in list(states))
                mem["window_pending"] = sum(len(getattr(st, "pending", []) or []) for st in states.values())
        except Exception:
            logger.debug("[multi-decision] 统计窗口内存占用失败", exc_info=True)
        return {
            "plugin": {
                "id": PLUGIN_ID,
                "enabled": self.enabled,
                "decision_enabled": self.decision_enabled,
                "window_enabled": self.window_enabled,
                "trigger_mode": self.trigger_mode,
                "trigger_threshold": self.trigger_threshold,
                "window_keep": self.window_keep,
                "window_trim_threshold": self.window_trim_threshold,
                },
            "today": today,
            "mem": mem,
            "sessions": sessions,
            "aliases": self.aliases.all() if self.aliases else {},
            "hidden": self.hidden.all() if self.hidden else [],
        }

    @staticmethod
    def _norm_day(date: str) -> str:
        """把 ?date= 归一化成 YYYY-MM-DD；非法值当空（= 全部日期）。"""
        text = (date or "").strip()
        if len(text) != 10 or not _DAY_RE.match(text):
            return ""
        return text

    @_api("GET", "/decisions")
    async def api_decisions(self, limit: int = 200, sid: str = "", date: str = ""):
        """消息日志，倒序。date 传 YYYY-MM-DD 只看那一天，留空看全部。"""
        day = self._norm_day(date)
        items = self._log.read_recent(limit, sid=sid, day=day) if self._log else []
        # 隐藏（删除）过的会话不再出现在日志里，刷新页面也不会冒出来
        items = [i for i in items if not self._is_hidden(str(i.get("sid", "")))]
        return {
            "items": items,
            "total": len(items),
            "date": day,
            "days": self._log.days() if self._log else [],
        }

    @_api("GET", "/windows")
    async def api_windows(self, limit: int = 100, sid: str = "", date: str = ""):
        """窗口裁切日志，倒序。

        v3.60（用户 m18059 第 6 条）：走势图要能往前翻更早的快照，前端会传 limit=0
        表示「要全部」——这里退到上限 5000 交给 read_recent（它自己 clamp 1..5000）。
        默认 100 的老行为一个字都不变（别的调用方还在用）。keep 是这个会话
        windows_keep 的有效值（单群覆盖 > 全局，0 = 不限），前端直接拿它拼标题。
        """
        day = self._norm_day(date)
        want = _as_int(limit, 100)
        try:
            keep = int(self.eff("windows_keep", sid) or 0)
        except (TypeError, ValueError):
            keep = int(getattr(self, "windows_keep", 0) or 0)
        if want <= 0:
            want = max(5000, keep)
        items = (
            self._window_log.read_recent(want, sid=sid, day=day) if self._window_log else []
        )
        items = [i for i in items if not self._is_hidden(str(i.get("sid", "")))]
        _stamp_snap_ids(items)
        return {
            "items": items,
            "total": len(items),
            "date": day,
            "days": self._window_log.days() if self._window_log else [],
            "keep": keep,
        }

    async def _refresh_forward_display_proofs(self, raw_sid, native_rows, budget: float = 3.0):
        """Bounded display-only proof refresh; never hydrate model/window messages.

        返回 first` 表示这次只跑完了 budget 秒、还有候选没试（调用方可以丢到后台续跑）。
        v4.76：点柱子的接口只给 0.6 秒，抓不到就先返回，剩下的交给后台任务，
        否则一个取不到转发内容的 mid 会把整页拖到 3 秒以上。
        """
        if not isinstance(raw_sid, str) or not isinstance(native_rows, dict):
            return False
        parts = raw_sid.rsplit(":", 2)
        if len(parts) != 3 or not parts[0] or parts[1] not in ("gm", "dm") or not parts[2]:
            return
        manager = getattr(getattr(self, "ctx", None), "adapter_mgr", None)
        if manager is None:
            return
        try:
            budget = max(0.0, float(budget))
        except (TypeError, ValueError):
            budget = 3.0
        deadline = time.monotonic() + budget
        # None marks in-flight; numbers are short failure deadlines only. Never
        # evict an in-flight operation or retain a permanent negative result.
        now = time.monotonic()
        attempts = getattr(self, "_forward_display_attempts", None)
        if attempts is None:
            attempts = self._forward_display_attempts = {}
        for key, until in list(attempts.items()):
            if until is not None and until <= now:
                attempts.pop(key, None)
        candidates = []
        for mid, row in native_rows.items():
            if (not isinstance(mid, str) or not mid or not isinstance(row, dict)
                    or row.get("session_id") != raw_sid or str(row.get("platform_message_id")) != mid
                    or row.get("chain") != [{"type": "forward"}]):
                continue
            try:
                proof = forward_display_proof_for_mid(raw_sid, mid)
            except Exception:
                proof = None
            key = (raw_sid, mid)
            if proof is not None:
                if attempts.get(key) is not None:
                    attempts.pop(key, None)
                continue
            if key not in attempts:
                candidates.append(mid)
                if len(candidates) == 2:
                    break
        if not candidates:
            return False
        try:
            adapters = manager.get_adapters()
            adapter = adapters.get(parts[0]) if isinstance(adapters, dict) else None
            bot = getattr(adapter, "bot", None) if type(adapter).__name__ == "QQAdapter" else None
            if not callable(getattr(bot, "send_action", None)):
                return
        except Exception:
            return
        pending = False
        for mid in candidates:
            key = (raw_sid, mid)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or key in attempts:
                # 预算用完了、这个还没开始抓：交给后台续跑。
                if key not in attempts:
                    pending = True
                continue
            # Full means conservatively skip, not evict active or cooled keys.
            if len(attempts) >= 128:
                break
            attempts[key] = None
            timed_out = False
            try:
                await asyncio.wait_for(fetch_forward_items(
                    bot, mid, timeout=remaining, raw_sid=raw_sid, refresh_display_proof=True,
                ), timeout=remaining)
            except (asyncio.TimeoutError, TimeoutError):
                # 用户 m10643：预算用完不等于「取不到」。以前这里和真失败一样记 30 秒冷却，
                # 而 /quote-lookup 只给 0.6 秒——一次刷新就把这条消息锁死半分钟，
                # 后台续跑和 /window 都被同一把锁挡住，卡片只能一直挂着「（已发 N）」。
                timed_out = True
            except Exception:
                pass
            finally:
                try:
                    succeeded = forward_display_proof_for_mid(raw_sid, mid) is not None
                except Exception:
                    succeeded = False
                if succeeded:
                    attempts.pop(key, None)
                elif timed_out:
                    # 交给后台续跑，不记冷却。
                    attempts.pop(key, None)
                    pending = True
                else:
                    attempts[key] = time.monotonic() + 30.0
        return pending

    def _spawn_forward_proof_refresh(self, raw_sid, owner_rows) -> None:
        """后台把这次没抓完的转发内容补上，下一次读这条快照自然就有。

        用户 m10643：后台不再受请求预算限制——0.6 秒 / 3 秒都短于一次真实取转发
        （框架自己补取一条 14 条目的转发要 3 秒左右），给足 30 秒才抓得完。
        """
        try:
            task = asyncio.create_task(self._refresh_forward_display_proofs(raw_sid, owner_rows, budget=30.0))
        except RuntimeError:
            return
        tasks = getattr(self, "_proof_tasks", None)
        if tasks is None:
            tasks = self._proof_tasks = set()
        tasks.add(task)
        task.add_done_callback(tasks.discard)



    def _quote_rounds_for(self, sid: str, mids, limit: int = 400, deadline: float = 1.2) -> list:
        """按 message_id 往更早的快照里捞记录（字节预筛，命中才解析）。

        用户 m18902：一轮一个快照，最近 80 轮有时只覆盖几十分钟，引用要的正文早就
        落到更早的记录里了，于是引用块只剩图、正文退化成「内容未获取」。这里和
        /quote-where 一个口径：从新到旧扫，先按原始字节做子串预筛（不解析 JSON），
        命中再解析；解析结果仍由调用方按外层 owner 确认，免得被嵌套引用骗到。
        """
        out: list = []
        mids = [str(m) for m in (mids or []) if str(m)]
        if self.request_log is None or not mids:
            return out
        try:
            folder = Path(self.request_log.dir) / _safe_sid(sid)
            entries = [ent.name for ent in os.scandir(folder) if ent.name.endswith(".json")]
        except Exception:
            return out
        # 文件名就是毫秒时间戳，倒序即从新到旧。
        entries.sort(reverse=True)
        entries = entries[: max(1, int(limit or 400))]
        needles = []
        for mid in mids:
            needles.append(("[message_id: %s]" % mid).encode("utf-8"))
            needles.append(('message_id="%s"' % mid).encode("utf-8"))
            needles.append(("message_id='%s'" % mid).encode("utf-8"))
            # 快照里 message_id 常嵌在 JSON 字符串中，引号被转义成 \"，所以要连转义形态一起筛。
            needles.append(('message_id=\\"%s\\"' % mid).encode("utf-8"))
            needles.append(("message_id=\\'%s\\'" % mid).encode("utf-8"))
        end = time.monotonic() + max(0.1, float(deadline or 1.2))
        for name in entries:
            if time.monotonic() > end:
                break
            try:
                raw = (folder / name).read_bytes()
            except Exception:
                continue
            if not any(needle in raw for needle in needles):
                continue
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
        return out


    @_api("GET", "/quote-lookup")
    async def api_quote_lookup(self, sid: str = "", ids: str = ""):
        """One-level Web quote projection, preferring the target's native own chain."""
        out: dict = {}
        if not sid or not ids:
            return {"ok": True, "items": out}
        want = list(dict.fromkeys(x.strip() for x in str(ids).split(",") if x.strip()))[:12]
        try:
            raw_sid = self.window.raw_sid(sid) if self.window is not None else ""
        except Exception:
            raw_sid = ""
        # Request-local only: one transient failure cannot become ready halfway through
        # this same response. The next GET always retries every media path.
        media_projection = {}

        def quote_public_media(path, mime, owner_sid):
            key = (path, mime, owner_sid)
            if key not in media_projection:
                try:
                    media_projection[key] = public_media(path, mime, owner_sid)
                except Exception:
                    media_projection[key] = {}
            got = media_projection[key]
            return dict(got) if isinstance(got, dict) else {}

        # No negative cache: a not-yet-written native record is retried next GET.
        native_rows = await asyncio.to_thread(read_native_rows, raw_sid, want)
        # v4.88：引用查询一次点击要打 4~6 次，转发证明只给 0.6 秒，剩下的丢后台续跑
        # （和点柱子 /request 一个口径），否则一个取不到转发内容的 mid 就拖 3 秒。
        if await self._refresh_forward_display_proofs(raw_sid, native_rows, budget=0.6):
            self._spawn_forward_proof_refresh(raw_sid, native_rows)
        projected = project_items(
            [{"message_id": mid} for mid in want], raw_sid, native_rows,
            public_media=quote_public_media, text_of_content=content_text,
            # 用户 m00942 图1：引用里的转发卡也要画全所有图，和主卡同一个口径。
            forward_media_for_mid=forward_media_for_mid,
            original_media_for_mid=original_media_for_mid,
            media_by_digest=media_cache_by_digest,
        )
        native_display = {mid: item["display"] for mid, item in zip(want, projected)}

        def native_meta(mid):
            row = native_rows.get(mid) or {}
            try:
                stamp = float(row.get("timestamp") or 0)
                if stamp > 100_000_000_000:
                    stamp /= 1000
                ts = time.strftime("%H:%M", time.localtime(stamp)) if stamp else ""
            except (ValueError, TypeError, OSError, OverflowError):
                ts = ""
            return str(row.get("sender_name") or ""), ts

        # 用户 m18902：原生投影只按 message_id 认回了图（source=media_index）时 own_body 是空串，
        # 以前照样当命中收下，正文就再也不会去快照里补——引用块于是「图都在、内容未获取」。
        # 正文为空的命中降级成待补：下面的快照扫描找到真正文就覆盖它，找不到再退回这份只有图的。
        bodyless = set()
        for mid in want:
            display = native_display[mid]
            if display["own_media_known"] and "own_body" in display:
                who, ts = native_meta(mid)
                out[mid] = {"who": who, "ts": ts, "body": display.get("own_body", ""),
                            "media": display["own_media"], "display": display}
                if not str(display.get("own_body") or "").strip():
                    bodyless.add(mid)
        left = (set(want) - set(out)) | bodyless
        try:
            # v4.88：以前这里用不带缓存的 list_rounds（每次读 81 份快照、约 8MB），
            # 一次点击打 4~6 次引用查询就是几百毫秒。换成带目录指纹缓存的版本。
            _cached = getattr(self.request_log, "list_rounds_cached", None)
            _read = _cached or self.request_log.list_rounds
            rounds = _read(sid, limit=80) if left and self.request_log is not None else []
        except Exception:
            rounds = []
        xml_pat = re.compile(
            r'<msg\s+message_id=["\']([^"\']+)["\'][^>]*>\s*\[([^\]]*)\]\s*<text>([\s\S]*?)</text>',
            re.I,
        )
        def snapshot_row(rec, mid):
            """记录里同一 mid 的 display_messages 那行：请求当时算好的展示投影。"""
            rows = rec.get("display_messages") or []
            if not isinstance(rows, list):
                return None
            for row in rows:
                if isinstance(row, dict) and display_message_id(row, text_of_content=content_text) == mid:
                    return row
            return None

        def owner_body_of(row):
            """展示行里的正文：去掉「[时间] [message_id: …] [user_nickname: …] | 」这层头。"""
            txt = content_text(row.get("text")) or content_text(row.get("raw"))
            if not txt:
                return ""
            split = re.split(r"\]\s*\|\s*", txt, maxsplit=1)
            return (split[1] if len(split) == 2 else txt).strip()

        def row_refs(row):
            """展示行的图集转成页面能直接用的引用（缺图的跳过）。"""
            got = []
            refs = row.get("media") or []
            for ref in refs[:4096] if isinstance(refs, list) else []:
                if not isinstance(ref, dict):
                    continue
                path = str(ref.get("path") or ref.get("url") or "")
                try:
                    ready = quote_public_media(path, str(ref.get("mime") or ""), raw_sid) if path else dict(ref)
                except Exception:
                    ready = {"missing": 1, "path": path, "mime": str(ref.get("mime") or "")}
                if ready and (ready.get("path") or ready.get("url")) and not ready.get("missing"):
                    got.append(ready)
            return got

        def absorb(rec):
            """把一份快照里命中的 mid 补进 out；只认外层 owner，嵌套引用的 id 不算。"""
            if not isinstance(rec, dict):
                return
            for original in rec.get("messages") or []:
                item = original if isinstance(original, dict) else {"content": getattr(original, "content", "")}
                # Only the explicit/outer owner may match; a nested quoted id never does.
                mid = display_message_id(item, text_of_content=content_text)
                if mid not in left:
                    continue
                txt = ""
                for key in ("text", "content", "raw"):
                    txt = content_text(item.get(key))
                    if txt:
                        break
                # 昵称里可能带「]」（用户 m00456 的 [%ĀĀ␇Ö]我在未来，等你）：先按
                # 「, user_id:」切，没有这个字段再退回「] |」；以前 [^,\]]+ 会截成半个名字。
                # 3.3.0 起群消息头多了 group_nickname 字段，展示行只留昵称。
                nm = (re.search(r"\[user_nickname:\s*(.*?),\s*(?:group_nickname:\s*([^,\]]*),\s*)?user_id:", txt)
                      or re.search(r"\[user_nickname:\s*([^\]\r\n]+)\]", txt))
                # 快照里的时间头有三种写法：[08:44]、[Nov 23 2026 08:44 Mon]、[2026-11-23 08:41:05]。
                tm = (re.match(r"\s*\[(\d{1,2}:\d{2})\]", txt)
                      or re.match(r"\s*\[[A-Za-z]{3} \d{1,2} \d{4} (\d{1,2}:\d{2})", txt)
                      or re.match(r"\s*\[(?:\d{4}-\d{2}-\d{2} )?(\d{1,2}:\d{2})(?::\d{2})?\]", txt))
                xm = xml_pat.search(txt)
                if xm and xm.group(1) == mid:
                    who, body = xm.group(2).strip(), xm.group(3).strip()
                else:
                    who = nm.group(1).strip() if nm else ""
                    if nm is not None and nm.re.groups >= 2:
                        gn = (nm.group(2) or "").strip()
                        if gn and gn != who:
                            who = who + " · " + gn
                    split = re.split(r"\]\s*\|\s*", txt, maxsplit=1)
                    body = split[1] if len(split) == 2 else txt
                med = []
                refs = item.get("media") or []
                for ref in refs[:4096] if isinstance(refs, list) else []:
                    if not isinstance(ref, dict):
                        continue
                    path = str(ref.get("path") or ref.get("url") or "")
                    try:
                        got = quote_public_media(path, str(ref.get("mime") or ""), raw_sid) if path else dict(ref)
                    except Exception:
                        got = {"missing": 1, "path": path, "mime": str(ref.get("mime") or "")}
                    if got and (got.get("path") or got.get("url") or got.get("missing")):
                        med.append(got)
                fallback = dict(item, message_id=mid, media=med)
                display = project_items(
                    [fallback], raw_sid, native_rows, public_media=quote_public_media,
                    text_of_content=content_text, forward_display_proof_for_mid=forward_display_proof_for_mid,
                    forward_proof_items=[item],
                    # 用户 m09904：引用里的转发卡也要画全所有图，快照兜底这条路径也得给完整图集，
                    # 否则只有模型侧那几张，卡片上会挂「（已发 N）」。
                    forward_media_for_mid=forward_media_for_mid,
                )[0]["display"]
                # Without native proof these aggregate refs are neutral attachments only.
                ts = tm.group(1) if tm else ""
                if display["own_media_known"]:
                    who, ts = native_meta(mid)
                    body = display.get("own_body", body)
                    med = display["own_media"]
                else:
                    # 用户 m18902：模型侧那行常常没有图（图只记在请求当时的展示投影里），
                    # 或者正文只剩一个 [Forward] 空壳；记录里那行 display_messages 两者都有。
                    if not body.strip() or not med:
                        shown = snapshot_row(rec, mid)
                        if shown is not None:
                            if not body.strip():
                                shown_body = owner_body_of(shown)
                                if shown_body:
                                    body = shown_body
                                    who = str(shown.get("name") or who)
                            if not med:
                                med = row_refs(shown)
                            if med or body.strip():
                                display = {"own_media_known": bool(med), "own_media": med,
                                           "unassigned_media": [], "direct_reply": None,
                                           "source": "snapshot_display", "own_body": body}
                out[mid] = {"who": who, "ts": ts, "body": body.strip(),
                            "media": med, "display": display}
                left.discard(mid)

        for row in rounds or []:
            if not left:
                break
            name = str((row or {}).get("name") or "")
            if not name:
                continue
            try:
                rec = self.request_log.read_round(sid, name) or {}
            except Exception:
                continue
            absorb(rec)
        # 用户 m18902：一轮一个快照，最近 80 轮有时只覆盖几十分钟，引用要的正文早就
        # 落到更早的记录里了。这里按 message_id 往更早的快照再捞一遍。
        if left and self.request_log is not None:
            for rec in self._quote_rounds_for(sid, sorted(left)):
                if not left:
                    break
                absorb(rec)
        for mid in want:
            if mid not in out and mid in native_rows:
                who, ts = native_meta(mid)
                display = native_display[mid]
                row = native_rows[mid]
                if (row.get("session_id") == raw_sid and str(row.get("platform_message_id")) == mid
                        and row.get("chain") == [{"type": "forward"}]):
                    # No original log body exists for this id. Supply only the exact
                    # native owner header/shell; strict projection still validates proof.
                    shell = {"message_id": mid, "text": f"[message_id: {mid}] | [Forward]"}
                    display = project_items(
                        [shell], raw_sid, native_rows, public_media=quote_public_media,
                        text_of_content=content_text, forward_display_proof_for_mid=forward_display_proof_for_mid,
                        forward_media_for_mid=forward_media_for_mid,
                    )[0]["display"]
                out[mid] = {"who": who, "ts": ts, "body": display.get("own_body", ""),
                            "media": display["own_media"] if display["own_media_known"] else [],
                            "display": display}
        # 用户 m21787 图1：引用的是很早的一条消息时，原生消息库和最近 80 轮快照里都
        # 找不到它，引用块就只剩一个「图片」灰块。插件自己的媒体索引按 message_id 记着
        # 当年收消息时留的副本，这里兜底认回来——只给图，正文/昵称仍用引用里那行原文。
        for mid in list(left):
            if mid in out:
                # 正文没补到，但原生已按 message_id 认回了图：保留那份（还带着昵称/时间）。
                continue
            try:
                _raw = original_media_for_mid(mid) or []
            except Exception:
                _raw = []
            _ready = []
            for ref in _raw:
                if not isinstance(ref, dict):
                    continue
                try:
                    got = quote_public_media(str(ref.get("path") or ""), str(ref.get("mime") or ""), raw_sid)
                except Exception:
                    got = {}
                if isinstance(got, dict) and (got.get("path") or got.get("url")) and not got.get("missing"):
                    _ready.append(got)
            if not _ready:
                continue
            out[mid] = {"who": "", "ts": "", "body": "", "media": _ready,
                        "display": {"own_media_known": True, "own_media": _ready,
                                    "unassigned_media": [], "direct_reply": None,
                                    "source": "media_index"}}
        return {"ok": True, "items": out}


    @_api("GET", "/quote-where")
    async def api_quote_where(self, sid: str = "", mid: str = ""):
        """被引用的那条消息出现在哪一轮快照里（用户 m12814：点引用块跳过去）。

        引用块里只有被引用那条的 message_id，它常常不在当前窗口里（窗口只装最近几十条），
        所以要按 id 回查历史快照：记录里每条用户消息都带 `[message_id: <id>]` 标记，
        先按原始字节做子串预筛（不解析 JSON），命中再解析确认，从新到旧取第一份。
        """
        mid = str(mid or "").strip()
        if not sid or not mid:
            return {"ok": False, "detail": "缺少 sid 或 mid"}
        if self.request_log is None:
            return {"ok": True, "found": False, "scanned": 0}
        key = (str(sid), mid)
        cache = getattr(self, "_quote_where_cache", None)
        if cache is None:
            cache = self._quote_where_cache = {}
        try:
            sig = self.request_log._rounds_sig(sid)
        except Exception:
            sig = None
        hit = cache.get(key)
        # 找到过的位置不会变；没找到的要跟着目录指纹失效，新落一份快照可能就有了。
        # 2026-11-18 审查 M09：找到过的那份快照本身可能已经被清掉了（份数上限、手动清理、
        # 目录被轮转），文件不在就当没找到，别把已经不存在的记录位置继续返回给前端。
        if hit is not None and hit.get("found"):
            name = str(hit.get("name") or "")
            folder = Path(self.request_log.dir) / _safe_sid(sid)
            if name and (folder / name).is_file():
                return {k: v for k, v in hit.items() if k != "sig"}
            cache.pop(key, None)
            hit = None
        if hit is not None and hit.get("sig") == sig:
            return {k: v for k, v in hit.items() if k != "sig"}

        def scan():
            folder = Path(self.request_log.dir) / _safe_sid(sid)
            try:
                entries = [ent.name for ent in os.scandir(folder) if ent.name.endswith(".json")]
            except Exception:
                return {"ok": True, "found": False, "scanned": 0}
            # 文件名就是毫秒时间戳，倒序即从新到旧。
            entries.sort(reverse=True)
            entries = entries[:400]
            needles = [
                ("[message_id: %s]" % mid).encode("utf-8"),
                ('message_id="%s"' % mid).encode("utf-8"),
                ("message_id='%s'" % mid).encode("utf-8"),
            ]
            deadline = time.monotonic() + 2.5
            scanned = 0
            for name in entries:
                if time.monotonic() > deadline:
                    break
                scanned += 1
                try:
                    raw = (folder / name).read_bytes()
                except Exception:
                    continue
                if not any(n in raw for n in needles):
                    continue
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                if not _record_has_mid(data, mid):
                    continue
                ts = data.get("ts") or 0
                return {"ok": True, "found": True, "name": name,
                        "snap_id": _snapshot_id(ts), "ts": ts, "scanned": scanned}
            return {"ok": True, "found": False, "scanned": scanned}

        got = await asyncio.to_thread(scan)
        got["sig"] = sig
        cache[key] = got
        if len(cache) > 512:
            cache.clear()
            cache[key] = got
        return {k: v for k, v in got.items() if k != "sig"}


    @_api("GET", "/window")
    async def api_window(self, sid: str = ""):
        """只读窗口事实；可有界补取独立的 Forward 展示凭证，不回填上下文。"""
        if not sid or self.window is None:
            return {"ok": False, "detail": "没有这个会话的窗口"}
        # Cache-filled compatibility attachments must never corroborate themselves.
        # 用户 m06193：转发展示凭证要按窗口原样取，黑名单过滤只作用于请求上下文
        original_messages = raw_forward_proof_items(self.window.snapshot(sid, prepare=False, blacklist=False))
        messages = _forward_media_attach(self.window.messages_of(sid), self.window.raw_sid(sid))
        source = "live"
        last_ts = 0.0
        counted = self.window.counted(sid)
        # v3.16（用户 m40583）：页面上的「当前 N 条」只算用户侧，bot 自己的回复
        # 不该占窗口条数；待发那部分由前端加上 pending_counted。
        user_counted = self.window.counted_user(sid)
        live_counted = counted
        live_size = self.window.size(sid)
        if not messages and self.request_log is not None and self.eff("session_enabled", sid) is not False:
            # 窗口状态只在内存里，热重载/重启后就空了。请求记录还在盘上，
            # 拿最近一轮的快照顶上，页面才不会一打开就说「窗口是空的」。
            last = self.request_log.last_snapshot(sid)
            if last:
                raw = last.get("messages") or []
                original_messages = raw
                messages = view_of(
                    raw,
                    raw_sid=self.window.raw_sid(sid)
                    or (self.engine._raw_sids.get(sid) if self.engine is not None else "")
                    or "",
                )
                counted = count_messages(raw)
                user_counted = count_messages(
                    [
                        m
                        for m in raw
                        if str(
                            (m.get("role") if isinstance(m, dict) else getattr(m, "role", ""))
                            or ""
                        )
                        == "user"
                    ]
                )
                last_ts = last.get("ts") or 0.0
                source = "last_request"
        # A single read-only batch supplies display-only ownership for both lists.
        raw_sid = self.window.raw_sid(sid)
        pending = self.window.pending_of(sid)
        mids = []
        seen_mids = set()
        for item in [*messages, *pending]:
            mid = display_message_id(item, text_of_content=content_text)
            if mid and mid not in seen_mids:
                mids.append(mid)
                seen_mids.add(mid)
            if len(mids) >= 900:
                break
        native_rows = await asyncio.to_thread(read_native_rows, raw_sid, mids)
        if await self._refresh_forward_display_proofs(raw_sid, native_rows):
            self._spawn_forward_proof_refresh(raw_sid, native_rows)
        # 用户 m10400 图4：实时窗口也要能换回聊天里原本那张图（动图/原分辨率），
        # 跟请求快照同一个口径——按 message_id 回插件索引取 orig_media。
        messages = project_items(messages, raw_sid, native_rows, public_media=public_media,
                                 text_of_content=content_text, forward_display_proof_for_mid=forward_display_proof_for_mid,
                                 forward_proof_items=original_messages,
                                 # 用户 m18581：实时窗口也要拿到这条转发的全部图（含贴纸），
                                 # 否则卡片只画框架挂的前 3 张，后面全是 🖼 / 🙂 占位。
                                 forward_media_for_mid=forward_media_for_mid,
                                 original_media_for_mid=original_media_for_mid,
                                 media_by_digest=media_cache_by_digest)
        pending = project_items(pending, raw_sid, native_rows, public_media=public_media,
                                text_of_content=content_text, forward_display_proof_for_mid=forward_display_proof_for_mid,
                                forward_proof_items=pending,
                                forward_media_for_mid=forward_media_for_mid,
                                original_media_for_mid=original_media_for_mid,
                                media_by_digest=media_cache_by_digest)
        # 用户 m17018 图2：跳过记录不随窗口裁切清理，窗口早就翻过去的老消息会一直挂在
        # 页面底部（用户看到「未进入上下文」里躺着两条早就不在窗口里的视频）。
        # 只留还在窗口时间范围内的那些——窗口裁掉之后它们也该跟着消失。
        skipped = self.window.skipped_of(sid)
        skipped = self._window_skips_of(messages, skipped)
        # 用户 m32048 / m13127：虚线卡按它自己的时间插回原位——在 messages + pending 的
        # 时间序列里找第一个比它晚的位置。以前按 after_mid（挡下那一刻窗口的最后一条），
        # 文件到达时对应消息还在待发，锚点就落到更早的历史消息后面（用户 m13127 图1/图3）。
        # 被拉黑的消息本体也在这里标成虚线，跟快照走同一套（用户 m32048 第 4 条）。
        skipped = self._apply_skips(skipped, [*messages, *pending], time.time())
        return {
            "ok": True,
            "sid": sid,
            "size": len(messages),
            "counted": counted,
            "user_counted": user_counted,
            "live_size": live_size,
            "live_counted": live_counted,
            "pending_counted": self.window.pending_counted(sid),
            "keep": self.eff("window_keep", sid),
            "threshold": self.eff("window_trim_threshold", sid),
            "enabled": self.eff("window_enabled", sid),
            "messages": messages,
            "source": source,
            "last_ts": last_ts,
            "pending": pending,
            "summary": self.window.summary_info(sid),
            # 用户 m5411：被「文件/视频不进上下文」挡下来的消息，页面单独一组画虚线框，
            # 只给界面看，不进窗口、不进待发、不参与条数统计。
            "skipped": skipped,
            "bot_qq": self.bot_qq,
            # v3.91（用户 m05749）：当前窗口顶上那条「最近一次判定」直接吃这个，
            # 不用再单独拉一次决策列表（异步拉容易赶不上本次绘制）。
            "last_decision": self._last_decision_of(sid),
            # v3.93（用户 m07570 图3）：决策记录真的进了上下文（assistant 身份），
            # 窗口页末尾也要能看见这一条，不然只有顶上一条横幅，像没生效。
            "decision_note": (
                self.window.decision_note_of(sid) if self.window is not None else {}
            ),
            # 用户 m13254：窗口页要按黑名单折叠，名单跟着窗口一起回来，别等第二个接口。
            "ublk": self._window_ublk(sid),
        }

    def _last_decision_of(self, sid: str) -> dict:
        """最近一次进阶决策的结论：内存里有就用内存的，热重载后用盘上快照顶上。

        用户 m05749 要的是「打开页面就知道上回为什么回 / 为什么不回」，
        热重载把内存清空后那条卡片不该凭空消失。
        """
        try:
            got = (getattr(self.engine, "health", None) or {}).get("last_decision") or {}
            if got and str(got.get("sid") or "") == str(sid or ""):
                return dict(got)
        except Exception:
            logger.debug("[multi-decision] 读内存里的最近决策失败", exc_info=True)
        try:
            rows = self.request_log.list_rounds_cached(sid, 60) if self.request_log else []
        except Exception:
            return {}
        for it in rows:
            if not bool(it.get("decision_only")):
                continue
            return {
                "ts": float(it.get("ts") or 0),
                "sid": sid,
                "reply": bool(it.get("decision_reply")),
                "reason": str(it.get("decision_reason") or ""),
                "say": str(it.get("decision_say") or ""),
                "failed": bool(it.get("failed")),
                "error": str(it.get("error") or ""),
                "model": str(it.get("decision_model") or ""),
                "elapsed": float(it.get("decision_elapsed") or 0),
            }
        return {}

    @_api("POST", "/summary-clear")
    async def api_summary_clear(self, sid: str = ""):
        """清空某个会话的中期摘要（窗口历史不受影响）。"""
        if not sid or self.window is None:
            return {"ok": False, "detail": "没有指定会话"}
        cleared = self.window.clear_summary(sid)
        return {"ok": True, "sid": sid, "cleared": cleared,
                "summary": self.window.summary_info(sid)}

    def _stall_seconds(self, sid: str = "") -> int:
        """左栏竖杠判「卡住」的秒数：跟着看门狗走，但一定比它早。

        STALL_SECONDS（60）是看门狗 180 秒时代的经验值：指示灯要比看门狗先变红，
        但又不能早到「刚投出去就红」。现在看门狗秒数可配（30-600），这里就按
        当前生效值的三分之一缩，上限仍是 STALL_SECONDS：
        看门狗 180 秒 → 60 秒不变；看门狗调到 30 秒 → 10 秒就红，
        不会出现「看门狗都强释放了指示灯还绿着」的情况。
        """
        try:
            wd = float(self.eff("watchdog_seconds", sid) or 0)
        except Exception:
            wd = 0.0
        if wd <= 0:
            return STALL_SECONDS
        return max(1, min(STALL_SECONDS, int(wd / 3)))

    def _health_summary(self) -> dict:
        """运行时健康快照（v2.82）。页面顶部状态灯 + 会话卡片都用它。"""
        eng = self.engine
        h = getattr(eng, "health", None) or {}
        now = time.time()
        started = float(h.get("started") or now)
        last_msg = float(h.get("last_msg_ts") or 0.0)
        busy = []
        try:
            for sid, st in (eng._states or {}).items():
                if getattr(st, "busy", False):
                    since = float(getattr(st, "busy_since", 0.0) or 0.0)
                    stall = self._stall_seconds(sid)
                    busy.append({
                        "sid": sid,
                        "seconds": int(now - since) if since > 0 else 0,
                        "stalled": (now - since) >= stall if since > 0 else False,
                        "stall_seconds": stall,
                    })
        except Exception:
            pass
        muted = []
        try:
            for sid in (self.overrides.all() if self.overrides else {}):
                if self.eff("session_enabled", sid) is False:
                    muted.append(sid)
        except Exception:
            pass
        # 最近一次看门狗强制释放那一轮后来补回来没有（用户 2026-10-24 图3）：
        # 健康条不能只说「释放过 N 次」，还得说刚才那次重试了几次、成没成。
        watchdog_retry = None
        try:
            getter = getattr(eng, "watchdog_retry_status", None)
            if callable(getter):
                watchdog_retry = getter()
        except Exception:
            watchdog_retry = None
        if not isinstance(watchdog_retry, dict):
            watchdog_retry = None
        # v3.51（用户 m07385 第 1 条）：不再算「累计触发」——刷新页面看到的时间与轮数
        # 必须就是「本次运行」的，累计跨重载混在一起反而对不上账，用户明确说不必要。
        return {
            "started": int(started),
            "uptime": int(now - started),
            "msgs": int(h.get("msgs") or 0),
            # 静音跳过前端不再显示（用户 2026-10-24 图3），字段照旧留着给老前端，
            # 计数也没拆：拆了得动 engine.health，不影响就别动（保留兼容）。
            "muted_skipped": int(h.get("muted") or 0),
            "triggers": int(h.get("triggers") or 0),
            "delivered": int(h.get("delivered") or 0),
            "finals": int(h.get("finals") or 0),
            "watchdog": int(h.get("watchdog") or 0),
            "watchdog_retry": watchdog_retry,
            "last_msg_ts": last_msg,
            "last_msg_ago": int(now - last_msg) if last_msg > 0 else -1,
            "last_msg_sid": str(h.get("last_msg_sid") or ""),
            "muted": muted,
            "busy": busy,
            # v3.75（用户本轮第 3 条）：重试也救不回来的最后一次失败，页面顶部出横幅
            "last_fail": h.get("last_fail") or {},
            # v3.91（用户 m05749）：最近一次进阶决策判了什么，首页顶栏读它。
            "last_decision": h.get("last_decision") or {},
            "decision_skip": int(h.get("decision_skip") or 0),
            "enabled": bool(self.enabled),
            "decision": bool(self.decision_enabled),
            "pending_finals": max(0, int(h.get("delivered") or 0) - int(h.get("finals") or 0)),
        }

    @_api("GET", "/health")
    async def api_health(self):
        """健康检查：插件活着吗、最近收到消息没有、为什么一条都没回。"""
        h = self._health_summary()
        h["ok"] = True
        return h

    @_api("GET", "/link-title")
    async def api_link_title(self, url: str = ""):
        """裸链接卡片的真实标题（v4.87，用户 m11237 第 4 条）。

        只认 B 站链接；抓到一次就落盘，之后都走缓存，抓不到返回 ok=False，
        前端保持卡片上的通用文案。
        """
        try:
            return await asyncio.to_thread(self.link_titles.title_of, url)
        except Exception as exc:
            logger.debug("[multi-decision] 取链接标题失败 %s：%s: %s", url, type(exc).__name__, exc)
            return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}

    @_api("GET", "/github")
    async def api_github(self, url: str = ""):
        """GitHub 链接卡片要的仓库信息（用户 m18492）。

        前端先把卡片骨架画出来，渲染完再回来问一次，填描述、star 和主语言；
        取不到就返回 ok=False，卡片只留 owner/repo 和链接。
        """
        try:
            return await asyncio.to_thread(self.github_repos.info_of, url)
        except Exception as exc:
            logger.debug("[multi-decision] 取 GitHub 信息失败 %s：%s: %s", url, type(exc).__name__, exc)
            return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}

    @_api("GET", "/context-sessions")
    async def api_context_sessions(self, light: int = 0):
        """「上下文」页左栏：有窗口或有请求记录的会话。"""
        seen = {}
        try:
            for x in (self.window.list_states() if self.window else []):
                sid = x.get("sid") or ""
                if not sid or self._is_hidden(sid):
                    continue
                seen[sid] = {
                    "sid": sid,
                    # 待发也算进窗口条数：它们下一轮就会被带走，
                    # 页面写着 0 条但其实攒了十条，看着像丢了
                    "window": x.get("counted_total", x.get("size", 0)),
                    "trimmed": x.get("trimmed", 0),
                    "rounds": 0,
                    "last_ts": 0,
                }
        except Exception:
            logger.exception("[multi-decision] 读取窗口列表失败")
        try:
            for x in (self.request_log.list_sessions() if self.request_log else []):
                sid = x.get("sid") or ""
                if not sid or self._is_hidden(sid):
                    continue
                cur = seen.setdefault(
                    sid, {"sid": sid, "window": 0, "trimmed": 0, "rounds": 0, "last_ts": 0}
                )
                cur["rounds"] = x.get("rounds", 0)
                cur["last_ts"] = x.get("last_ts", 0)
        except Exception:
            logger.exception("[multi-decision] 读取请求会话失败")
        # 只看过没触发过的会话（手动关掉决策、或还不攒够条数）在内存窗口和
        # 请求记录里都没有，重启后就从左栏消失，用户没法再把它打开（v2.32）。
        # 单群覆盖与别名是用户手动留下的，按它们把会话补回来。
        try:
            for src in (
                (self.overrides.all() if self.overrides else {}),
                (self.aliases.all() if self.aliases else {}),
                (self.bot_names or {}),
            ):
                for sid in (src or {}):
                    sid = str(sid or "")
                    if not sid or self._is_hidden(sid):
                        continue
                    seen.setdefault(
                        sid,
                        {"sid": sid, "window": 0, "trimmed": 0, "rounds": 0, "last_ts": 0},
                    )
        except Exception:
            logger.exception("[multi-decision] 补回覆盖/别名会话失败")
        # bot 改名（改适配器昵称、换人设）会让 sid 前缀跟着变，新旧两个 sid
        # 各自带着窗口和请求记录，左栏就出现同一个群两行（v2.39，用户 m23841）。
        # 群号 / QQ 号没变，按「类型:号码」归并，只留数据最全的那条。
        merged = {}
        for it in seen.values():
            key = _merge_sid_key(it.get("sid") or "")
            old = merged.get(key)
            if old is None:
                merged[key] = it
                continue
            def _score(x):
                return (
                    x.get("window") or 0,
                    x.get("rounds") or 0,
                    x.get("last_ts") or 0,
                )
            keep, drop = (it, old) if _score(it) > _score(old) else (old, it)
            keep["legacy_sids"] = (
                list(keep.get("legacy_sids") or [])
                + [drop.get("sid")]
                + list(drop.get("legacy_sids") or [])
            )
            merged[key] = keep
        items = list(merged.values())
        # 轻量轮询只按 sid 更新状态点，不消费侧栏排序或最近 24 小时统计。
        act = {}
        by_r24 = {}
        if not light:
            # 最近活跃取消息日志时间与请求时间的较大值，完整列表仍按原口径排序。
            items.sort(key=lambda r: r.get("last_ts") or 0, reverse=True)
            try:
                if self._log is not None:
                    act = self._log.last_seen(7) or {}
            except Exception:
                act = {}
            try:
                by_r24 = (self._recent24() or {}).get("by_session") or {}
            except Exception:
                by_r24 = {}
        for it in items:
            sid = it.get("sid") or ""
            session_enabled = self.eff("session_enabled", sid)
            # 完整列表要回显上轮窗口条数；轻量状态拍仅在请求索引没有时间时回查。
            if (not it.get("window") and self.request_log is not None
                    and session_enabled is not False and (not light or not it.get("last_ts"))):
                try:
                    last = self.request_log.last_snapshot(sid)
                except Exception:
                    last = None
                if last:
                    if not light:
                        it["snapshot_counted"] = count_messages(last.get("messages") or [])
                        it["stale"] = True
                    if not it.get("last_ts"):
                        it["last_ts"] = last.get("ts") or 0
            if light:
                it["eff"] = {"session_enabled": session_enabled}
            else:
                # 完整配置摘要和绝对倒计时供会话卡使用。
                it["eff"] = self._eff_summary(sid)
                try:
                    it["silent_eta"] = self.engine.silent_eta(sid) if self.engine else {}
                except Exception:
                    it["silent_eta"] = {}
            # agent 是否在跑、跑了多久。卡住时插件侧没人看得见，
            # 只能靠重启 —— 用户 m25454 要的状态指示器就是这个。
            st = None
            try:
                st = self.engine._states.get(sid) if self.engine is not None else None
            except Exception:
                st = None
            if st is not None:
                it["busy"] = bool(getattr(st, "busy", False))
                it["queued"] = bool(getattr(st, "queued", False))
                since = float(getattr(st, "busy_since", 0.0) or 0.0)
                it["busy_since"] = since if it["busy"] and since > 0 else 0.0
                it["busy_seconds"] = (
                    max(0, int(time.time() - since)) if it["busy"] and since > 0 else 0
                )
                # 用户 m11091 第 1 条：静默 / 休兜 触发过就打勾（来新消息时由引擎清掉）
                it["silent_fired_ts"] = float(getattr(st, "silent_fired_ts", 0.0) or 0.0)
                it["rest_fired_ts"] = float(getattr(st, "rest_fired_ts", 0.0) or 0.0)
                it["silent_hold_ts"] = float(getattr(st, "silent_hold_ts", 0.0) or 0.0)
                # 投递后 STALL_SECONDS（默认 60 秒，随看门狗调小）没结算就算卡住，标红
                # （看门狗要等更久才动手，别让红灯等那么久）
                it["stall_seconds"] = self._stall_seconds(sid)
                it["stalled"] = it["busy_seconds"] >= it["stall_seconds"]
                it["last_error"] = str(getattr(st, "last_error", "") or "")
                it["retry_count"] = int(getattr(st, "retry_count", 0) or 0)
                _ra = float(getattr(st, "retry_at", 0.0) or 0.0)
                it["retry_in"] = max(0, int(round(_ra - time.time()))) if _ra else 0
            else:
                it["busy"] = False
                it["queued"] = False
                it["busy_seconds"] = 0
                it["busy_since"] = 0.0
                it["stalled"] = False
                it["last_error"] = ""
                it["retry_count"] = 0
                it["retry_in"] = 0
            # 「为什么没回」的实时原因（v2.82）：静音 / 攒数中 / 太短 / 退避 / 配额
            it["muted"] = session_enabled is False
            it["muted_skipped"] = 0
            try:
                it["muted_skipped"] = int(
                    ((getattr(self.engine, "health", None) or {}).get("muted_by_sid") or {}).get(sid, 0)
                )
            except Exception:
                pass
            it["last_mark"] = str(getattr(st, "last_mark", "") or "") if st is not None else ""
            it["last_mark_ago"] = (
                int(time.time() - float(getattr(st, "last_mark_ts", 0.0) or 0.0))
                if st is not None and getattr(st, "last_mark_ts", 0.0) else -1
            )
            it["counter"] = int(getattr(st, "counter", 0) or 0) if st is not None else 0
            it["threshold"] = int(self.eff("trigger_threshold", sid) or 0)
            it["trigger_mode"] = str(self.eff("trigger_mode", sid) or "")
            if not light:
                agg = by_r24.get(sid) or {}
                it["r24_messages"] = int(agg.get("messages") or 0)
                it["r24_replied"] = int(agg.get("replied") or 0)
                it["last_active"] = max(
                    float(it.get("last_ts") or 0.0),
                    float(act.get(_merge_sid_key(sid)) or 0.0),
                )
        if not light:
            items.sort(key=lambda r: r.get("last_active") or 0, reverse=True)
        # light=1：每秒的状态轮询只要运行状态那几个字段，
        # 不要 eff 全量、silent_eta、r24 统计、窗口条数这些。
        # 一份 11KB 的响应能压到 2KB 左右，1 秒一拍省很多。
        if light:
            keep = {
                "sid", "busy", "queued", "busy_since", "busy_seconds",
                "stalled", "last_error", "retry_count", "retry_in",
                "muted", "muted_skipped", "last_mark", "last_mark_ago",
                "counter", "threshold", "trigger_mode",
                # 用户 m17375 第 4 条：新回复要马上出现在记录页。这两个字段一变，
                # 前端那一秒的轮询就能立刻去拉回复记录，不用干等 5 秒那一拍。
                "rounds", "last_ts",
            }
            for it in items:
                for key in list(it.keys()):
                    if key not in keep and key != "eff":
                        it.pop(key, None)
            return {"items": items, "health": self._health_summary()}
        return {
            "items": items,
            "aliases": self.aliases.all() if self.aliases else {},
            "keep": self.window_keep,
            "threshold": self.window_trim_threshold,
            "health": self._health_summary(),
        }

    @_api("GET", "/traces")
    async def api_traces(self, sid: str = "", limit: int = 20):
        """某一轮的调用轨迹列表（按时间倒序）。"""
        if self.tracer is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        if not sid:
            return {
                "ok": False,
                "detail": "没有指定会话",
                "sessions": self.tracer.list_sessions(),
            }
        return {
            "ok": True,
            "sid": sid,
            "items": self.tracer.recent(sid, limit),
            "sessions": self.tracer.list_sessions(),
        }

    @_api("GET", "/trace")
    async def api_trace(self, sid: str = "", name: str = ""):
        """一份完整的调用轨迹：请求 + 每步思考与工具 + token + 发出结果。"""
        if self.tracer is None or not sid or not name:
            return {"ok": False, "detail": "参数不足"}
        rec = self.tracer.read(sid, name)
        if not rec:
            return {"ok": False, "detail": "没有这份记录"}
        return {"ok": True, "record": rec}

    @_api("GET", "/aliases")
    async def api_aliases(self):
        """会话备注名表。"""
        return {"items": self.aliases.all() if self.aliases else {}}

    @_api("POST", "/aliases")
    async def api_alias_set(self, sid: str, alias: str = ""):
        """给会话改个名字；alias 传空等于恢复原名。"""
        if not self.aliases:
            return {"ok": False, "detail": "插件还没初始化完"}
        items = self.aliases.set(sid, alias)
        logger.info(f"[multi-decision] 会话备注 {sid} -> {alias or '(清除)'}")
        return {"ok": True, "items": items}

    @_api("POST", "/session-hide")
    async def api_session_hide(self, sid: str):
        """从页面上移除一个会话（只隐藏，日志不删）。"""
        if not self.hidden:
            return {"ok": False, "detail": "插件还没初始化完"}
        items = self.hidden.add(sid)
        logger.info(f"[multi-decision] 隐藏会话 {sid}")
        return {"ok": True, "hidden": items}

    @_api("POST", "/session-show")
    async def api_session_show(self, sid: str = ""):
        """恢复一个被隐藏的会话；sid 留空表示全部恢复。"""
        if not self.hidden:
            return {"ok": False, "detail": "插件还没初始化完"}
        if not sid:
            items = []
            for one in self.hidden.all():
                items = self.hidden.remove(one)
            logger.info("[multi-decision] 已恢复全部隐藏会话")
            return {"ok": True, "hidden": items}
        items = self.hidden.remove(sid)
        logger.info(f"[multi-decision] 恢复会话 {sid}")
        return {"ok": True, "hidden": items}

    @_api("POST", "/session-delete")
    async def api_session_delete(self, sid: str):
        """把这个会话在我们插件里产生的记录全删掉（不可恢复）。"""
        if not sid:
            return {"ok": False, "detail": "缺少 sid"}
        counts = self.purge_session(sid)
        if not counts:
            return {"ok": False, "detail": "删除失败，详情看日志"}
        total = sum(counts.values())
        logger.warning(f"[multi-decision] 已删除会话记录 {sid}：{counts}")
        return {"ok": True, "sid": sid, "counts": counts, "total": total}

    @_api("POST", "/clear-records")
    async def api_clear_records(self, sid: str):
        """清空这个会话的旧记录（配置与计数保留）。"""
        if not sid:
            return {"ok": False, "detail": "缺少 sid"}
        counts = self.clear_records(sid)
        if not counts:
            return {"ok": False, "detail": "清空失败，详情看日志"}
        logger.warning(f"[multi-decision] 已清空会话记录 {sid}：{counts}")
        return {"ok": True, "sid": sid, "counts": counts, "total": sum(counts.values())}

    def _custom_api_names(self) -> dict:
        """自定义接口页的名字：模型名 / 页号 → 用户起的名字（用户 m01898）。

        JEV 走第三方接口时账本记成 custom:<模型名>，以前渠道一律写死「自定义接口」，
        用户把页名改成「阿里」之后价目表还是老样子。这里按每页的 model 与 id 各记一份；
        没起名的页不进来，仍然显示「自定义接口」。
        """
        try:
            raw = self._decision_get("jev_apis", "", "")
        except Exception:
            raw = ""
        key = str(raw or "")
        cache = getattr(self, "_custom_name_cache", None)
        if isinstance(cache, tuple) and len(cache) == 2 and cache[0] == key:
            return cache[1]
        out = {}
        try:
            items = json.loads(key) if key else []
        except Exception:
            items = []
        if isinstance(items, list):
            for one in items:
                if not isinstance(one, dict):
                    continue
                name = str(one.get("name") or "").strip()
                if not name:
                    continue
                for field in ("model", "id"):
                    val = str(one.get(field) or "").strip()
                    if val:
                        out.setdefault(val, name)
        self._custom_name_cache = (key, out)
        return out

    def _custom_api_label(self, mid: str) -> str:
        """custom:<模型名> 里的模型名 → 页名；没起名就退回「自定义接口」。"""
        mid = str(mid or "").strip()
        if not mid:
            return "自定义接口"
        try:
            return self._custom_api_names().get(mid, "") or "自定义接口"
        except Exception:
            return "自定义接口"

    def _auto_provider(self, name: str, stat: dict) -> str:
        """这一行「框架自己认出来的渠道」（不含价目表里手填的覆盖）。

        用户 m00375 第 6 条：渠道以前只认响应对象上的 provider_name，框架根本不填，
        第三方接口（custom:xxx）也不在框架配置里，整列都是「来源未记录」。这里按
        provider_id / 模型名回框架配置里查一次，查不到才留空。
        """
        stat = stat if isinstance(stat, dict) else {}
        auto = str(stat.get("provider_name") or "").strip()
        if auto:
            return auto
        pid = str(stat.get("provider_id") or "").strip()
        mid = str(stat.get("model_id") or "").strip()
        if not pid and ":" in str(name or ""):
            pid, _, mid = str(name or "").partition(":")
        if pid == "custom":
            return self._custom_api_label(mid)
        if self.tracer is None:
            return ""
        model_name = str(stat.get("model_name") or mid or name)
        keys = (str(name or ""), model_name, mid)
        auto = ""
        used_pid = ""
        if pid:
            try:
                auto = self.tracer.provider_label(pid, model_name)
            except Exception:
                auto = ""
            if auto:
                used_pid = pid
        # 模型 id 本身可能带冒号（框架里 WorkBuddy 那个就叫 cn:deepseek-v4.1-flash），
        # 按冒号拆出来的 pid 在配置里查不到，provider_label 会直接返回空。
        # 这时拿整名再当模型名找一次，别让这一行白着。
        if not auto:
            try:
                auto = self.tracer.provider_label("", str(name or ""))
            except Exception:
                auto = ""
        if auto:
            # 用户 m22269 第 3 条：第一次认出来就记下来（pid 与模型名各记一份），
            # 以后供应商那边把模型删了、配置里再也查不到，也还能按记忆把渠道补回来。
            _remember_provider(getattr(self, "provider_bind", None), keys, auto, used_pid)
            return auto
        return _bound_provider(getattr(self, "provider_bind", None), keys, pid)

    @staticmethod
    def _display_model_name(name: str) -> str:
        """账本里的模型 key → 给人看的名字（用户 m07372）。

        JEV 走第三方接口时账本记成 custom:<模型>（state/jev_phase.py 的 ref），
        框架配置里没有这个 provider，解析不出名字，价目表和饼图图例就带着
        custom: 前缀。前缀只是内部标记，显示时去掉。
        """
        raw = str(name or "").strip()
        if raw.startswith("custom:"):
            return raw.split(":", 1)[1].strip() or raw
        return raw

    def _model_canon(self, name: str) -> str:
        """账本里的模型 key → 模型名（用户 m04493）。取不到就原样返回。"""
        raw = str(name or "").strip()
        if not raw or self.tracer is None:
            return self._display_model_name(raw)
        try:
            return self._display_model_name(self.tracer.model_name_of(raw) or raw)
        except Exception:
            return self._display_model_name(raw)

    def _model_alias(self, totals: dict, extra=()) -> dict:
        """同一个模型、同一个渠道在账本里可能落在两个 key 上（用户 m21592）。

        框架有时回报 provider_id:model_id，有时只回报模型名，账本就落成两个 key
        （例如 d9027599f69c:f5e6… 与 gemini-4-hydrogen-high），价目表只认得其中一个，
        于是同一个模型被拆成「有价」和「未定价」两行。这里按（渠道, 模型名）分组，
        组里有价目条目的 key 当代表，都没有价就取 token 最多的那个。

        用户 m04493：分组用的「模型名」要先把 pid:mid 解析成配置里的 model_name，
        否则 uuid 那行的 model_name 就是它自己、跟模型名分不到一组；价目表里有、
        这段时间没用量的 key（extra）也要参与分组，不然它永远单列一行。
        """
        models = (totals or {}).get("models") if isinstance(totals, dict) else None
        models = models if isinstance(models, dict) else {}
        extra = [str(k) for k in (extra or ()) if str(k or "").strip()]
        if not models and not extra:
            return {}
        try:
            prices = self.pricing.all() if self.pricing is not None else {}
        except Exception:
            prices = {}
        groups = {}
        for name, stat in models.items():
            if not isinstance(stat, dict):
                continue
            label = self._model_canon(str(stat.get("model_name") or "").strip() or str(name))
            prov = str(stat.get("provider_name") or "").strip()
            if not prov:
                prov = self._auto_provider(str(name), stat)
            groups.setdefault((prov, label), []).append(str(name))
        for name in extra:
            if name in models:
                continue
            stat = {"model_name": name}
            prov = self._auto_provider(name, stat)
            groups.setdefault((prov, self._model_canon(name)), []).append(name)
        # 用户 m04883：被用户从价目表删掉的 key（pricing.json 的 hidden）不能当代表——
        # 它会被 gone 过滤掉，整组的花费就跟着丢了（决策柱因此又变成「未定价」）。
        try:
            gone = set(self.pricing.hidden()) if self.pricing is not None else set()
        except Exception:
            gone = set()
        alias = {}
        for (_prov, label), names in groups.items():
            if len(names) == 1:
                alias[names[0]] = names[0]
                continue
            # 代表优先用「key 就是模型名」的那个，其次有价的，最后 token 最多的；
            # 但只在没被删掉的 key 里挑，全被删了才退回全部（整组一起藏）。
            pool = [n for n in names if n not in gone] or names
            if label in pool:
                rep = label
            else:
                priced = [n for n in pool if n in prices]
                pool = priced or pool
                rep = max(pool, key=lambda n: (int((models.get(n) or {}).get("input") or 0)
                                               + int((models.get(n) or {}).get("output") or 0)))
            for n in names:
                alias[n] = rep
        return alias

    def _canon_prices(self, models: dict, alias: dict) -> dict:
        """价目表按 _model_alias 的对照表重排一份（用户 m04493）。

        同一个模型在账本里可能落成 pid:mid 与模型名两个 key，价目表只认得其中一个。
        重排之后 summarize 才能按合并后的 key 查到价，不然有量的那行永远是「未定价」。
        两个 key 都有价时以「key 就是模型名」的那份为准，其余字段只补空。
        """
        out = {}
        for key, val in (models or {}).items():
            if not isinstance(val, dict):
                continue
            k = str(key)
            dst_key = alias.get(k, k)
            old = out.get(dst_key)
            if old is None:
                out[dst_key] = copy.deepcopy(val)
                continue
            for field in ("in", "out", "cache"):
                cur, new = old.get(field), val.get(field)
                if new and (not cur or k == dst_key):
                    old[field] = new
            if not old.get("provider") and val.get("provider"):
                old["provider"] = val["provider"]
            if not old.get("peak") and val.get("peak"):
                old["peak"] = copy.deepcopy(val["peak"])
        return out

    def _merge_model_totals(self, totals: dict, alias: dict) -> dict:
        """按 _model_alias 的对照表把模型桶合成一份（用户 m21592）。"""
        if not isinstance(totals, dict) or not alias:
            return totals
        models = totals.get("models")
        if not isinstance(models, dict) or not models:
            return totals
        if all(alias.get(str(k), str(k)) == str(k) for k in models):
            return totals
        out = {}
        for name, stat in models.items():
            if not isinstance(stat, dict):
                continue
            rep = alias.get(str(name), str(name))
            dst = out.get(rep)
            if dst is None:
                dst = copy.deepcopy(stat)
                dst["model"] = rep
                out[rep] = dst
                continue
            for field in ("input", "output", "cached", "rounds", "decision_rounds",
                          "reply_rounds", "calls"):
                dst[field] = int(dst.get(field) or 0) + int(stat.get(field) or 0)
            usage = dst.get("usage")
            if isinstance(usage, list) and isinstance(stat.get("usage"), list):
                usage.extend(copy.deepcopy(stat["usage"]))
            if not str(dst.get("model_name") or "").strip():
                dst["model_name"] = str(stat.get("model_name") or "")
            if not str(dst.get("provider_name") or "").strip():
                dst["provider_name"] = str(stat.get("provider_name") or "")
            if dst.get("identity_source") != stat.get("identity_source"):
                dst["identity_source"] = "mixed"
        merged = dict(totals)
        merged["models"] = out
        return merged

    @_api("GET", "/pricing")
    async def api_pricing(self, hours: int = 24):
        """API 价目：手填的单价 + 最近一段时间的用量与花费。

        hours 只认 24 / 168 / 720（最近 24 小时 / 7 天 / 30 天，用户 m10749
        第 3 条顶上的切换）。别的一律按 24 小时算，免得前端传错把账本扫一遍。
        """
        # query 传进来的 hours 是字符串（框架按 kwargs 透传），不转 int 会让 168 掉回 24
        try:
            span = int(hours)
        except (TypeError, ValueError):
            span = 24
        if span not in (24, 168, 720):
            span = 24
        models = self.pricing.all() if self.pricing is not None else {}
        seen = {}
        cost = None
        alias = {}
        price_view = None
        try:
            if self.tracer is not None:
                totals = self.tracer.recent_totals(span) or {}
                # 用户 m21592：同一个模型、同一个渠道在账本里可能落在两个 key 上
                # （框架有时只回报模型名，有时回报 provider_id:model_id），表格里就
                # 成了两行——一行有价、一行「未定价」。计价和展示前先归并成一份。
                alias = self._model_alias(totals, extra=models.keys())
                totals = self._merge_model_totals(totals, alias)
                seen = totals.get("models") or {}
                if self.pricing is not None:
                    # 用户 m04493：价目表按归并后的 key 重排，否则有量的那行查不到价
                    price_view = self._canon_prices(models, alias)
                    cost = self.pricing.summarize(totals, prices=price_view)
        except Exception:
            seen = {}
            cost = None
        # 价目页删掉的模型（pricing.json 的 hidden）：哪怕最近 24 小时还在用，
        # 也不许再回到列表里，否则用户删了它又冒出来（用户本轮第 3 条）。
        gone = set()
        if self.pricing is not None:
            try:
                gone = set(self.pricing.hidden())
            except Exception:
                gone = set()
        # 用户 m04493：删除名单也按归并后的 key 算，删了 uuid 那行、模型名那行不能还在。
        # 用户 m04883：只算「自己就是代表」的那些——被并到别的 key 上的 hidden 项
        # （例如删掉的 deepseek-flash 并进有价的 uuid 行）不能把代表一起藏掉。
        gone = {k for k in gone if alias.get(k, k) == k}
        if price_view is None:
            price_view = self._canon_prices(models, alias)
        # 用户 m19842：不做折叠了，只把「所选区间里一次都没调用过」的模型藏起来。
        # 用户 m20332：一次都没产生 token、也没花钱的模型不算「在用」——账本里
        # 可能只留了它的空行（失败轮），图例和表格里不该冒出来。
        cost_of = {}
        for part in ((cost or {}).get("parts") or []):
            cost_of[str(part.get("model") or "")] = float(part.get("cost") or 0.0)
        active = set()
        for _name, _stat in seen.items():
            if not isinstance(_stat, dict):
                continue
            _tok = int(_stat.get("input") or 0) + int(_stat.get("output") or 0)
            if _tok > 0 or cost_of.get(str(_name), 0.0) > 0:
                active.add(str(_name))
        rows = []
        # 两边取并集：价目里有但最近没用到的也要列出来，不然想改价还得重新跑一遍
        for name in sorted(set(price_view) | set(seen)):
            if name in gone:
                continue
            # 归并时被并到别的 key 上的（价目表里还留着它自己），不再单列一行
            if name not in seen and alias.get(name) and alias[name] != name:
                continue
            price = price_view.get(name) or {}
            stat = seen.get(name) or {}
            # 价目表里没有的模型按 0 元算（用户 2026-10-24 第 5 条：「识别到了新的
            # 也默认成0元」）：前端看到 null 会显示成空、看起来像「没定价」，给 0
            # 就直观多了——用户照着 0 改数就行。
            # 用户 m00375 第 6 条：渠道以前只认响应对象上的 provider_name，框架根本
            # 不填，第三方接口（custom:xxx）也不在框架配置里，整列都是「来源未记录」。
            # 这里按 provider_id / 模型名回框架配置里查一次，查不到才留空。
            # 用户 m20332：价目表里手填的渠道优先（框架不回报 provider 的模型只能靠人补）。
            # 自动识别出来的渠道（框架回报 / 配置里查 / custom 前缀），只当占位符用
            _auto = self._auto_provider(name, stat)
            # 手填的优先，没填就用自动识别的
            _prov = str(price.get("provider") or "").strip() or _auto
            rows.append(
                {
                    "model": name,
                    # 用户 m20332：价目表里手填的渠道（空 = 没填，前端拿自动识别的当占位）。
                    "provider": str(price.get("provider") or ""),
                    "in": _as_price(price.get("in")),
                    "out": _as_price(price.get("out")),
                    # 缓存命中的单价单列一项（用户 m03616 第 1 条）：命中部分按它算钱
                    "cache": _as_price(price.get("cache")),
                    "input": int(stat.get("input") or 0),
                    "output": int(stat.get("output") or 0),
                    "cached": int(stat.get("cached") or 0),
                    "rounds": int(stat.get("rounds") or 0),
                    "decision_rounds": int(stat.get("decision_rounds") or 0),
                    "reply_rounds": int(stat.get("reply_rounds") or 0),
                    "calls": int(stat.get("calls") or 0),
                    "model_name": self._display_model_name(
                        str(stat.get("model_name") or ("快速决策模型" if name == "fast" else name))),
                    "provider_id": str(stat.get("provider_id") or ""),
                    "provider_name": _prov,
                    # 自动识别的渠道（不受手填影响），前端拿它当输入框的占位符
                    "provider_auto": _auto,
                    "model_id": str(stat.get("model_id") or ""),
                    "identity_source": str(stat.get("identity_source") or "legacy_unknown"),
                    "active": name in active,
                    # 用户 m23442：没存过高峰设置的模型，周末按高峰算（勾选框默认不勾）。
                    "peak": price.get("peak") or {
                        "enabled": False, "in": None, "out": None, "cache": None,
                        "start": "", "end": "", "weekends": True,
                    },
                }
            )
        zone = time.tzname[1 if time.daylight and time.localtime().tm_isdst > 0 else 0]
        return {"ok": True, "unit": PRICE_UNIT, "hours": span, "rows": rows, "cost": cost,
                "active": sorted(active), "timezone": zone}

    @_api("GET", "/pricing/trend")
    async def api_pricing_trend(self, range_key: str = "", bucket: str = "day", count: int = 0,
                               start: str = "", end: str = ""):
        """价目页下面那两张图：按自然日分桶的 token 与花费。

        用户 m19357：柱 + 折线一张、饼图一张，两张同一行。
        用户 m19975：切换改成 七天 / 本月 / 上月，默认本月；本月只算 1 号到今天，
        未来的日子不返回。range_key 认 7d / week / month / lastmonth（用户 m21274 加了
        按周：从本周一往前 8 周，按真实日历切）；不传就退回旧的
        bucket=day|week|month（老接口和测试还在用）。
        每根柱子按模型拆开（一天可能有多个柱子）；用户 m21274 之后前端改成按
        「决策 / JEV 决策 / 回复」画柱，所以每个点另给一份 kinds。
        """
        key = str(range_key or "").strip().lower()
        if key and key not in ("7d", "month", "lastmonth", "week", "window"):
            key = "month"   # 乱填按本月，别悄悄退回旧的 bucket 模式
        data = {}
        if self.tracer is not None:
            try:
                if key == "window":
                    # 用户 m23442：柱图改成 14 天窗口 + 滚轮按天翻，区间由前端算好传进来。
                    data = self.tracer.window_totals(start, end) or {}
                elif key in ("7d", "month", "lastmonth", "week"):
                    data = self.tracer.range_totals(key) or {}
                else:
                    kind = str(bucket or "day").strip().lower()
                    if kind not in ("day", "week", "month"):
                        kind = "day"
                    try:
                        span = int(count)
                    except (TypeError, ValueError):
                        span = 0
                    data = self.tracer.bucket_totals(kind, span) or {}
            except Exception:
                logger.debug("[multi-decision] 价目走势统计失败", exc_info=True)
                data = {}
        # 用户 m21592：同一个模型同一个渠道在账本里可能有两个 key，图上别拆成两个
        # 模型。先用整段汇总建好「key -> 代表 key」的对照表，再按天归并。
        _prices = {}
        if self.pricing is not None:
            try:
                _prices = self.pricing.all()
            except Exception:
                _prices = {}
        alias = self._model_alias(data.get("all") or {}, extra=_prices.keys())
        # 用户 m04493：价目表按归并后的 key 重排，图上按天计价才查得到价
        price_view = self._canon_prices(_prices, alias)
        # 用户 m20332：价目表里手填的渠道优先（框架不回报 provider 的模型只能靠人补）。
        prov_of = {}
        if self.pricing is not None:
            try:
                prov_of = {str(k): str((v or {}).get("provider") or "")
                           for k, v in _prices.items()}
            except Exception:
                prov_of = {}
        # 价目表里删掉的模型（hidden）在图上也不画：表格已经藏了，图上再冒出来
        # 就成了「两个 deepseek-flash」那种重复图例（用户 m20332 截图里的问题）。
        gone = set()
        if self.pricing is not None:
            try:
                gone = set(self.pricing.hidden())
            except Exception:
                gone = set()
        # 用户 m04493：删除名单也按归并后的 key 算；用户 m04883：只算自己就是代表的
        gone = {k for k in gone if alias.get(k, k) == k}
        # 用户 m21274：柱子按功能分——决策 / JEV 决策 / 回复各自的 token。
        # kinds.decision 里含 JEV，modes.jev 只记 JEV，所以普通决策 = decision - jev。
        def _fn_tokens(bucket, minus=None):
            bucket = bucket if isinstance(bucket, dict) else {}
            minus = minus if isinstance(minus, dict) else {}
            inp = int(bucket.get("input") or 0) - int(minus.get("input") or 0)
            outp = int(bucket.get("output") or 0) - int(minus.get("output") or 0)
            cac = int(bucket.get("cached") or 0) - int(minus.get("cached") or 0)
            inp, outp, cac = max(0, inp), max(0, outp), max(0, cac)
            return {"input": inp, "output": outp, "cached": cac, "tokens": inp + outp,
                    "rounds": max(0, int(bucket.get("rounds") or 0) - int(minus.get("rounds") or 0))}

        def _kinds_out(totals, money):
            """每根柱子除了 token，还要给出这根柱子花了多少钱（用户 m21592）。

            summarize 的 by_kind 只有 decision / reply，JEV 也是 decision，
            所以普通决策 = decision - jev（by_mode 里另存了 jev 的花费）。
            """
            kinds = {
                "decision": _fn_tokens((totals.get("kinds") or {}).get("decision"),
                                       (totals.get("modes") or {}).get("jev")),
                "jev": _fn_tokens((totals.get("modes") or {}).get("jev")),
                "reply": _fn_tokens((totals.get("kinds") or {}).get("reply")),
            }
            by_kind = (money or {}).get("by_kind") or {}
            by_mode = (money or {}).get("by_mode") or {}
            jev_cost = float((by_mode.get("jev") or {}).get("total") or 0.0)
            dec_cost = float((by_kind.get("decision") or {}).get("total") or 0.0)
            rep_cost = float((by_kind.get("reply") or {}).get("total") or 0.0)
            kinds["jev"]["cost"] = round(jev_cost, 4)
            kinds["decision"]["cost"] = round(max(0.0, dec_cost - jev_cost), 4)
            kinds["reply"]["cost"] = round(rep_cost, 4)
            # 用户 m22269 第 2 条：有 token 却没价（模型身份没记下来、或那行被删了）时
            # 别写「—」装没数据，标成「未定价」，前端据此写字。token 为 0 的不算未定价。
            priced = {
                "decision": bool((by_kind.get("decision") or {}).get("priced")),
                "jev": bool((by_mode.get("jev") or {}).get("priced")),
                "reply": bool((by_kind.get("reply") or {}).get("priced")),
            }
            # 用户 m05232：这一格的钱是按「当时的回复模型」估出来的，前端加个 ≈ 标一下。
            estimated = {
                "decision": bool((by_kind.get("decision") or {}).get("estimated")),
                "jev": bool((by_mode.get("jev") or {}).get("estimated")),
                "reply": bool((by_kind.get("reply") or {}).get("estimated")),
            }
            for fn, bucket in kinds.items():
                bucket["priced"] = bool(priced.get(fn)) or int(bucket.get("tokens") or 0) <= 0
                bucket["estimated"] = bool(estimated.get(fn))
            return kinds

        # 用户 m05232：11-03～11-07 的回复行写账时还没记模型名（B3.1.0 之前），整片挂在
        # 「未记录模型」上，图上只能写「未定价」。用户要求按当时的回复模型补价：先找出
        # 每天真正在用的回复模型（有价、回复 token 最多的那个），再给「未记录模型」那几天
        # 用最近一天的单价估算。账本不动，只影响这一次统计。
        def _reply_tokens(bucket):
            total = 0
            for ev in ((bucket or {}).get("usage") or []):
                if isinstance(ev, dict) and str(ev.get("kind") or "reply") == "reply":
                    total += _as_int(ev.get("input"), 0) + _as_int(ev.get("output"), 0)
            return total

        day_reply = []
        for item in (data.get("buckets") or []):
            best = None
            for name, mb in (((item.get("totals") or {}).get("models")) or {}).items():
                if not isinstance(mb, dict) or str(name) in _UNKNOWN_NAMES:
                    continue
                one = price_view.get(str(name)) or {}
                if not (one.get("in") or one.get("out") or one.get("cache")):
                    continue
                tok = _reply_tokens(mb)
                if tok <= 0:
                    continue
                if best is None or tok > best[0]:
                    best = (tok, one)
            day_reply.append(best[1] if best else None)

        points = []
        # 2026-11-18 审查 M10：区间合计以前是拿 all_totals 再 summarize 一遍、而且不传补价，
        # 于是「未记录模型」那几天在柱子上有价（≈），到饼图和合计里却成了未定价，两边对不上。
        # 现在把逐日的计价结果累加起来：合计 = 柱子之和，饼图各模型花费也逐日累加。
        agg = {"cost": 0.0, "priced": False, "currency": "",
               "parts": {}, "by_kind": {}, "by_mode": {}}

        def _agg_bucket(store, name):
            slot = store.get(name)
            if slot is None:
                slot = store[name] = {"total": 0.0, "input": 0, "output": 0, "cached": 0,
                                      "calls": 0, "priced": False, "estimated": False}
            return slot

        def _agg_sum(store, mapping):
            for name, one in (mapping or {}).items():
                one = one if isinstance(one, dict) else {}
                slot = _agg_bucket(store, str(name))
                slot["total"] += float(one.get("total") or 0.0)
                slot["input"] += int(one.get("input") or 0)
                slot["output"] += int(one.get("output") or 0)
                slot["cached"] += int(one.get("cached") or 0)
                slot["calls"] += int(one.get("calls") or 0)
                slot["priced"] = bool(slot["priced"]) or bool(one.get("priced"))
                slot["estimated"] = bool(slot["estimated"]) or bool(one.get("estimated"))

        for idx, item in enumerate(data.get("buckets") or []):
            totals = self._merge_model_totals(item.get("totals") or {}, alias)
            # 这一天有没有「没有模型身份」的回复用量；有才需要补价。
            fb = None
            unknown = (totals.get("models") or {}).get("未记录模型")
            if isinstance(unknown, dict) and _reply_tokens(unknown) > 0:
                for off in range(1, len(day_reply) + 1):
                    near = None
                    for j in (idx - off, idx + off):
                        if 0 <= j < len(day_reply) and day_reply[j]:
                            near = day_reply[j]
                            break
                    if near:
                        fb = {"reply": near}
                        break
            money = None
            if self.pricing is not None:
                try:
                    money = self.pricing.summarize(totals, prices=price_view, fallback=fb)
                except Exception:
                    money = None
            if money:
                agg["cost"] += float(money.get("total") or 0.0)
                agg["priced"] = bool(agg["priced"]) or bool(money.get("priced"))
                agg["currency"] = agg["currency"] or str(money.get("currency") or "")
                for part in (money.get("parts") or []):
                    pname = str(part.get("model") or "")
                    slot = agg["parts"].get(pname)
                    if slot is None:
                        slot = agg["parts"][pname] = {
                            "model": pname, "input": 0, "output": 0, "cached": 0,
                            "cost": 0.0, "unset": True, "estimated": False}
                    slot["input"] += int(part.get("input") or 0)
                    slot["output"] += int(part.get("output") or 0)
                    slot["cached"] += int(part.get("cached") or 0)
                    slot["cost"] += float(part.get("cost") or 0.0)
                    slot["unset"] = bool(slot["unset"]) and bool(part.get("unset"))
                    slot["estimated"] = bool(slot["estimated"]) or bool(part.get("estimated"))
                _agg_sum(agg["by_kind"], money.get("by_kind"))
                _agg_sum(agg["by_mode"], money.get("by_mode"))
            # 每个模型这一天的花费：summarize 的 parts 已经按模型算好了，直接建映射。
            cost_of = {}
            unset_of = {}
            for part in ((money or {}).get("parts") or []):
                name = str(part.get("model") or "")
                cost_of[name] = float(part.get("cost") or 0.0)
                unset_of[name] = bool(part.get("unset"))
            models = []
            for name, mb in (totals.get("models") or {}).items():
                if not isinstance(mb, dict):
                    continue
                if str(name) in gone:
                    continue
                inp = int(mb.get("input") or 0)
                outp = int(mb.get("output") or 0)
                # 用户 m20332：0 token 又没花钱的模型不进图例（账本里可能只留了空行）。
                if inp + outp <= 0 and cost_of.get(str(name), 0.0) <= 0:
                    continue
                models.append({
                    "model": str(name),
                    "model_name": self._display_model_name(str(mb.get("model_name") or name)),
                    "provider_name": (str(mb.get("provider_name") or "").strip()
                                      or prov_of.get(str(name), "")
                                      or self._auto_provider(str(name), mb)),
                    "input": inp,
                    "output": outp,
                    "cached": int(mb.get("cached") or 0),
                    "tokens": inp + outp,
                    "cost": cost_of.get(str(name), 0.0),
                    "unset": unset_of.get(str(name), False),
                })
            models.sort(key=lambda m: m["tokens"], reverse=True)
            points.append({
                "key": str(item.get("key") or ""),
                "label": str(item.get("label") or ""),
                "start": item.get("start"),
                "end": item.get("end"),
                "input": int(totals.get("input") or 0),
                "output": int(totals.get("output") or 0),
                "cached": int(totals.get("cached") or 0),
                "tokens": int(totals.get("total") or 0),
                "rounds": int(totals.get("rounds") or 0),
                "cost": float((money or {}).get("total") or 0.0),
                "priced": bool((money or {}).get("priced")),
                "models": models,
                "kinds": _kinds_out(totals, money),
            })
        # 用户 m08952：折线往左延伸时要接上「真实的前一天」。以前前端拿前两个点反推，
        # 11-08 比 11-09 低，反推出来是个假低点，滚轮翻过去线会跳一下。这里把窗口
        # 前后各一天也算出来一起返回，前端拿它当延伸锚点（只给 label / cost / tokens）。
        edge = {"prev": None, "next": None}
        if key == "window" and points:
            from datetime import datetime as _dt, timedelta as _td

            def _edge_point(day_dt):
                day_txt = day_dt.strftime("%Y-%m-%d")
                try:
                    one = self.tracer.window_totals(day_txt, day_txt) or {}
                except Exception:
                    return None
                buckets = one.get("buckets") or []
                if not buckets:
                    return None
                item = buckets[0]
                totals = self._merge_model_totals(item.get("totals") or {}, alias)
                if int(totals.get("total") or 0) <= 0:
                    return None   # 那天没用量，别把线拉到 0
                fb = None
                unknown = (totals.get("models") or {}).get("未记录模型")
                if isinstance(unknown, dict) and _reply_tokens(unknown) > 0:
                    near = next((x for x in day_reply if x), None)
                    if near:
                        fb = {"reply": near}
                money = None
                if self.pricing is not None:
                    try:
                        money = self.pricing.summarize(totals, prices=price_view, fallback=fb)
                    except Exception:
                        money = None
                return {"label": str(item.get("label") or ""),
                        "start": item.get("start"),
                        "cost": float((money or {}).get("total") or 0.0),
                        "tokens": int(totals.get("total") or 0)}

            try:
                today_dt = _dt.now()
                first_ts = points[0].get("start")
                last_ts = points[-1].get("start")
                if first_ts:
                    edge["prev"] = _edge_point(_dt.fromtimestamp(int(first_ts)) - _td(days=1))
                if last_ts:
                    nxt = _dt.fromtimestamp(int(last_ts)) + _td(days=1)
                    if nxt.date() <= today_dt.date():
                        edge["next"] = _edge_point(nxt)
            except Exception:
                logger.debug("[multi-decision] 价目走势前后一天算失败", exc_info=True)
        # 饼图：整段窗口里各模型的花费占比（跟表格同一套计价，价目里删掉的模型不算）
        parts = []
        total = {"tokens": 0, "rounds": 0, "cost": 0.0, "priced": False}
        currency = ""
        all_totals = self._merge_model_totals(data.get("all") or {}, alias)
        # 2026-11-18 审查 M10：区间合计直接用逐日累加的结果，不再拿 all_totals 重算一遍，
        # 否则补价只落在柱子上、合计与饼图把「未记录模型」当未定价，同页两处数字对不上。
        money_all = None
        if self.pricing is not None:
            agg_parts = sorted(agg["parts"].values(), key=lambda p: (-p["cost"], p["model"]))
            money_all = {
                "total": round(agg["cost"], 4),
                "currency": agg["currency"] or _CURRENCY,
                "priced": bool(agg["priced"]),
                "parts": [dict(p, cost=round(float(p["cost"]), 4)) for p in agg_parts],
                "by_kind": {k: dict(v, total=round(v["total"], 4))
                            for k, v in agg["by_kind"].items()},
                "by_mode": {k: dict(v, total=round(v["total"], 4))
                            for k, v in agg["by_mode"].items()},
            }
            money = money_all
            if money:
                total["cost"] = float(money.get("total") or 0.0)
                total["priced"] = bool(money.get("priced"))
                currency = str(money.get("currency") or "")
                for part in (money.get("parts") or []):
                    pkey = str(part.get("model") or "")
                    # 饼图图例要显示人看得懂的名字：模型桶里带着 model_name / provider_name，
                    # 直接透给前端，省得前端再拿价目表反查（价目表只覆盖当前区间，查不全）。
                    meta = (all_totals.get("models") or {}).get(pkey) or {}
                    parts.append({
                        "model": pkey,
                        "model_name": self._display_model_name(str(meta.get("model_name") or "")),
                        "provider_name": (str(meta.get("provider_name") or "").strip()
                                          or prov_of.get(pkey, "")
                                          or self._auto_provider(pkey, meta)),
                        "cost": float(part.get("cost") or 0.0),
                        "input": int(part.get("input") or 0),
                        "output": int(part.get("output") or 0),
                        "cached": int(part.get("cached") or 0),
                        # 用户 m23442：下面几张饼还要按模型看 token 和调用次数。
                        "tokens": int(part.get("input") or 0) + int(part.get("output") or 0),
                        "calls": int(meta.get("calls") or 0),
                        # 用户 m05865：调用分布还要按模型看，轮数跟「按功能」那张饼一个口径。
                        "rounds": int(meta.get("rounds") or 0),
                        "unset": bool(part.get("unset")),
                    })
        total["tokens"] = int(all_totals.get("total") or 0)
        total["rounds"] = int(all_totals.get("rounds") or 0)
        zone = time.tzname[1 if time.daylight and time.localtime().tm_isdst > 0 else 0]
        return {"ok": True, "range": str(data.get("range") or key or ""),
                "start_day": str(data.get("start_day") or ""),
                "end_day": str(data.get("end_day") or ""),
                "prev": edge["prev"], "next": edge["next"],
                "kinds": _kinds_out(all_totals, money_all),
                "bucket": str(data.get("bucket") or "day"),
                "count": int(data.get("count") or 0),
                "unit": PRICE_UNIT, "currency": currency, "points": points, "parts": parts,
                "total": total, "timezone": zone}

    @_api("POST", "/pricing")
    async def api_pricing_set(self, values: str = ""):
        """保存价目。values 是 JSON：{"models": {"模型名": {"in": 单价, "out": 单价, "cache": 缓存命中单价}}}。"""
        if self.pricing is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        try:
            data = json.loads(values) if values else {}
        except Exception:
            return {"ok": False, "detail": "values 不是合法 JSON"}
        models = data.get("models") if isinstance(data, dict) else data
        try:
            saved = self.pricing.save(models)
        except ValueError as exc:
            return {"ok": False, "detail": str(exc)}
        except Exception:
            logger.exception("[multi-decision] 保存价目失败")
            return {"ok": False, "detail": "保存失败，详情看日志"}
        logger.info(f"[multi-decision] 已保存 {len(saved)} 个模型的价目")
        return {"ok": True, "models": saved}

    @_api("POST", "/pricing/delete")
    async def api_pricing_delete(self, values: str = ""):
        """删掉一个模型的价目（用户本轮第 3 条：价目页模型名左边那个删除按钮）。

        values 是 JSON：{"model": "模型名"}。删除是持久的——模型名写进 pricing.json
        的 hidden，单价一起清掉；不提供「恢复」接口，想恢复就重新填一次价目。
        """
        if self.pricing is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        try:
            data = json.loads(values) if values else {}
        except Exception:
            return {"ok": False, "detail": "values 不是合法 JSON"}
        model = ""
        if isinstance(data, dict):
            model = str(data.get("model") or "").strip()
        if not model:
            return {"ok": False, "detail": "缺少 model"}
        try:
            hidden = self.pricing.hide(model)
        except Exception:
            logger.exception("[multi-decision] 删除价目失败")
            return {"ok": False, "detail": "删除失败，详情看日志"}
        logger.info(f"[multi-decision] 已删除价目 {model}（价目页共隐藏 {len(hidden)} 个模型）")
        return {"ok": True, "hidden": hidden}

    def _cache_kinds(self, sid: str = "") -> list:
        """插件自己产生的缓存，一类一行（用户 m11091 第 11 条；m13250 第 6 条补全）。

        只读统计，不删不改。用户 m13708 第 3/4/5 条把这张表做成了「可配置、可统计、
        可分类清空」：每一类都带上自己的上限（limit + limit_key + limit_unit，前端的
        输入框直接绑这个键）、保留天数（days + days_key）、是否按会话独立
        （per_session）、能否清空（clearable）。

        用户 m22090：带 sid 时每行的 count / bytes 也按这个会话统计，不再返回全局
        总量；不带 sid 才是全局。

        计数口径（用户第 5 条：「一个群那么多快照只有 5 份」）：
        - 请求快照 / 调用轨迹是每会话一个子目录、一秒一份，数文件数即份数；
        - 消息日志 / 窗口快照一天一个 .jsonl、多会话混写，必须数**非空行数**（记录条数），
          以前数文件数只能数出「有几天记录」，看着就像只有 5 份；
        - 中期摘要一个会话一条，所以数是「摘要会话数」；
        - 会话统计是自适应阈值的账本（每会话 <sid>.json 的按天计数 + requests.jsonl /
          tokens.jsonl 两本用量账），条数如实统计。
        """
        out = []

        def _eff(key: str, default=0) -> int:
            """这一类的有效上限：带了 sid 就按该会话的覆盖值算（用户 m18059 第 5 条）。"""
            try:
                if sid:
                    return int(self.eff(key, sid) or 0)
                return int(getattr(self, key, default) or 0)
            except (TypeError, ValueError):
                return int(getattr(self, key, default) or 0)

        def _dm(key: str) -> int:
            """这一类的「私聊默认」上限（用户 m19536 第 4 条）。群聊默认还是 limit 那个值。"""
            try:
                return int(getattr(self, key + "_dm", 0) or 0)
            except (TypeError, ValueError):
                return 0

        def _row(key: str, label: str, unit: str, note: str, count, size,
                 limit=0, limit_key: str = "", limit_unit: str = "",
                 limit_follow: str = "", days=0, days_key: str = "",
                 per_session: bool = False, clearable: bool = True,
                 limit_dm=0, limit_key_dm: str = "", limit_fixed: bool = False,
                 global_only: bool = False, sub: str = "",
                 clear_days: int = 7):
            size = int(size or 0)
            out.append({
                "key": key,
                "label": label,
                "unit": unit,
                "note": note,
                "count": int(count or 0),
                "bytes": size,
                "mb": round(size / 1048576.0, 2),
                "limit": int(limit or 0),
                "limit_key": limit_key,
                "limit_unit": limit_unit,
                "limit_follow": limit_follow,
                # 上限分两套：limit 是群聊默认，limit_dm 是私聊默认（各类默认值见 __init__）
                "limit_dm": int(limit_dm or 0),
                "limit_key_dm": limit_key_dm,
                "days": int(days or 0),
                "days_key": days_key,
                "per_session": bool(per_session),
                "clearable": bool(clearable),
                "limit_fixed": bool(limit_fixed),
                # v4.84（用户 m09738）：这一类的上限 / 天数只按全局算（后端只吃全局值），
                # 选中某个会话时这两格画成只读，别让人以为改了会生效。
                "global_only": bool(global_only),
                # 用户 m14353：数量那一格下面补一行小字（转发图片用它报「N 张图」）。
                "sub": str(sub or ""),
                # 用户 m14386：每类缓存「清理 N 天前」的 N 可以不一样（用量账本用 30 天）。
                "clear_days": int(clear_days or 7),
            })

        # 图片缓存：上限 / 天数由媒体缓存自己那三条管，按张数算
        media = {"count": 0, "bytes": 0}
        try:
            if self.window is not None:
                media = self.window.media_cache_stats(sid) or media
        except Exception:
            pass
        _row("media", "图片缓存", "张",
             "聊天里出现过的图片，原图被清理后还能显示。",
             media.get("count"), media.get("bytes"),
             limit=_eff("media_cache_limit"), limit_key="media_cache_limit", limit_unit="张",
             limit_dm=_dm("media_cache_limit"), limit_key_dm="media_cache_limit_dm",
             days=_eff("media_cache_keep_days"), days_key="media_cache_keep_days")
        if not sid:
            # 用户 m23021：全局把「能归属的聊天图 / 未归属 / 其他图」拆开，
            # 单群视图只显示自己的聊天图，不再把转发图、归档图混进来。
            # 用户 m23831：数量为 0 的行不再占位置，清完就自动消失。
            if media.get("unknown_count"):
                _row("media_unknown", "未归属图片", "张",
                     "找不到所属会话的旧图片，随图片缓存一起清理。",
                     media.get("unknown_count"), media.get("unknown_bytes"),
                     clearable=False)
        # 用户 m24364：推文图也按会话归属，所以单群 / 私聊视图同样要露出这一行，
        # 否则只能改全局默认，改不了这个群自己的上限。数量为 0 也保留，方便先设上限。
        _row("media_other", "其他图片缓存", "张",
             "推文卡片等非聊天图片，超出上限或天数时先删最早的。",
             media.get("other_count"), media.get("other_bytes"),
             limit=_eff("media_other_limit"), limit_key="media_other_limit",
             limit_unit="张/会话",
             limit_dm=_dm("media_other_limit"),
             limit_key_dm="media_other_limit_dm",
             days=_eff("media_other_keep_days"),
             days_key="media_other_keep_days")

        # v4.90（用户 m01133 第 2 条）：回复 / 裁切 / 决策三种快照合成一个「窗口快照」概念。
        # 它们本来就是同一种东西——当时的窗口存档，同一个目录（requests/<sid>/*.json）、
        # 同一个命名，只是存下来的时机不同（回完一轮 / 判完一次 / 裁了一刀）。
        # 以前拆成三行，三行加起来跟走势图上的柱子数永远对不上，用户看不出哪个数才是真的。
        # 现在只留一行：份数线统一走 windows_keep（群 200 / 私 100），天数走 requests_keep_days。
        # 裁切日志（windows/*.jsonl）只是走势图橙色标记的索引，不再单独占一行。
        req_usage = {"all": {"count": 0, "bytes": 0}}
        try:
            if self.request_log is not None:
                req_usage = self.request_log.usage(sid) or req_usage
        except Exception:
            logger.exception("[multi-decision] 统计快照占用失败")
        all_cell = req_usage.get("all") or {}
        # 用户 m09847 第 2 条：裁切日志（windows/*.jsonl）也是窗口存档的一部分，
        # 以前只数 requests/ 里的 .json，这 200 KB 在账本上完全看不见。
        # 一天一个文件、多会话混写，没法按会话拆，所以只在全局视图里加。
        trim_log_bytes = 0
        if not sid:
            try:
                req_dir = getattr(self.request_log, "dir", None)
                win_dir = (Path(req_dir).parent / "windows") if req_dir else None
                if win_dir is not None and win_dir.is_dir():
                    for p in win_dir.glob("*.jsonl"):
                        try:
                            trim_log_bytes += int(p.stat().st_size or 0)
                        except OSError:
                            continue
            except Exception:
                logger.debug("[multi-decision] 统计裁切日志占用失败", exc_info=True)
        _row("requests", "窗口快照", "份",
             "每轮回复、每次决策都存一份当时的窗口，方便回看有没有漏消息。",
             all_cell.get("count"), int(all_cell.get("bytes") or 0) + trim_log_bytes,
             limit=_eff("windows_keep"), limit_key="windows_keep", limit_unit="份/会话",
             limit_dm=_dm("windows_keep"), limit_key_dm="windows_keep_dm",
             days=_eff("requests_keep_days"),
             days_key="requests_keep_days", per_session=True)

        # 中期摘要也进缓存账本（用户 m17828）：设置仍在「窗口」页，这里只统计、
        # 不给单独的上限 / 天数框，避免同一个设置出现两处。
        summary_count = 0
        summary_bytes = 0
        try:
            if self.summaries is not None:
                all_summaries = self.summaries.all() or {}
                if sid:
                    # 用户 m22090：单群设置里只算这个群自己的摘要。
                    want = self._cache_sid_key(sid)
                    for owner, item in all_summaries.items():
                        if self._cache_sid_key(owner) != want:
                            continue
                        summary_count = 1
                        if isinstance(item, str):
                            text = item
                        else:
                            try:
                                text = json.dumps(item, ensure_ascii=False)
                            except Exception:
                                text = str(item)
                        summary_bytes = len(text.encode("utf-8"))
                        break
                else:
                    summary_count = len(all_summaries)
                    summary_path = getattr(self.summaries, "path", None)
                    if summary_path is not None and Path(summary_path).exists():
                        # 按文件实际大小记账，和其它缓存一样算磁盘占用，不按摘要正文字数估。
                        summary_bytes = int(Path(summary_path).stat().st_size or 0)
        except Exception:
            logger.exception("[multi-decision] 统计中期摘要占用失败")
        # v4.84（用户 m09738）：上限与保留天数直接长在「中期摘要」这一行上，
        # 不再在表格下面单列三行（那三行是上一版临时补的，用户要求并进这一行）。
        _row("summaries", "中期摘要", "条",
             "对话太长时把早期内容压成摘要，尽量不丢上下文。",
             summary_count, summary_bytes,
             limit=_eff("summaries_keep"), limit_key="summaries_keep", limit_unit="份",
             limit_dm=_dm("summaries_keep"), limit_key_dm="summaries_keep_dm",
             days=_eff("summaries_keep_days"), days_key="summaries_keep_days",
             # 摘要库只吃全局值（Summaries 构造时拿的是全局 keep / keep_days），
             # 选中某个会话时这两格不能改，画成只读。
             global_only=True)

        # v4.54（用户 m18227）：转发缓存 / 重试记录 / 私聊窗口存档也进缓存账本。
        # 前两类按每个群 / 私聊各自的上限裁（0 = 不限）；
        # 用户 m18819：私聊窗口存档不设个数上限，只统计占用。
        # 用户 m10466：转发图片和转发原图记录本来就是同一条消息的两面，
        # 合成一份 forward_cache.json 存、一个上限、一个清理按钮。
        fwd = {"count": 0, "refs": 0, "bytes": 0, "imgs": 0, "img_bytes": 0}
        try:
            fwd = forward_media_stats(sid) or fwd
        except Exception:
            logger.exception("[multi-decision] 统计转发缓存失败")
        fwd_imgs = int(fwd.get("imgs") or 0)
        _row("forward_cache", "转发缓存", "条",
             "合并转发里的图片和原图记录，翻回去还能看到。",
             fwd.get("count"),
             # 用户 m14353：占用报图片本身的大小，以前报的是索引文件（82 KB），
             # 1,163 MB 的转发图在账本上完全看不见。
             fwd.get("img_bytes") or fwd.get("bytes"),
             limit=_eff("forward_media_keep"), limit_key="forward_media_keep",
             limit_dm=_dm("forward_media_keep"), limit_key_dm="forward_media_keep_dm",
             limit_unit="条/会话", per_session=True,
             days=_eff("forward_media_keep_days"),
             days_key="forward_media_keep_days",
             sub=("{:,} 张图".format(fwd_imgs) if fwd_imgs else ""))

        retry_stats = {"count": 0, "events": 0, "bytes": 0}
        try:
            if self.retry_history is not None:
                retry_stats = self.retry_history.stats(sid) or retry_stats
        except Exception:
            logger.exception("[multi-decision] 统计重试记录失败")
        _row("retry_history", "自动重试记录", "条",
             "自动重试的过程和结果，方便查哪次成功、哪次失败。",
             retry_stats.get("count"), retry_stats.get("bytes"),
             limit=_eff("retry_keep"), limit_key="retry_keep",
             limit_dm=_dm("retry_keep"), limit_key_dm="retry_keep_dm",
             limit_unit="条/会话", per_session=True,
             days=_eff("retry_keep_days"),
             days_key="retry_keep_days")

        dm_win = {"count": 0, "messages": 0, "bytes": 0}
        try:
            if self.window is not None:
                dm_win = self.window.dm_cache_stats(sid) or dm_win
        except Exception:
            logger.exception("[multi-decision] 统计私聊窗口存档失败")
        _row("window_dm", "私聊上下文存档", "个",
             "每个私聊的对话上下文，重启或更新后还能接着聊。",
             dm_win.get("count"), dm_win.get("bytes"))

        # v3.64（用户 m18870）：调用轨迹并进请求快照，不再单列一行
        # 2026-11-18（用户 m13966）：消息日志和会话统计合成一行——这两类都是
        # 「按天滚、不设上限」的账，平时也是一起看；用量账本单独一行，永久保存。
        dec_root = getattr(self._log, "dir", None)
        dec_count, dec_bytes = _jsonl_usage(dec_root, sid=sid) if dec_root else (0, 0)
        stats_count = 0
        stats_bytes = 0
        led_count = 0
        led_bytes = 0
        stats_dir = getattr(self.stats, "dir", None)
        if stats_dir:
            want_stats = self._cache_sid_key(sid) if sid else ""
            try:
                for path in Path(stats_dir).glob("*.json"):
                    if want_stats and self._cache_sid_key(path.stem) != want_stats:
                        continue
                    try:
                        stats_bytes += int(path.stat().st_size or 0)
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        stats_count += len(raw) if isinstance(raw, dict) else 0
                    except Exception:
                        continue
            except Exception:
                pass
            led_count, led_bytes = _jsonl_usage(stats_dir, ".jsonl", sid=sid, subdirs=True)
        _row("logs", "消息日志与统计", "条",
             "每条消息的发言时间、所属会话和触发结果，加上每个会话的消息数、"
             "回复数和 token 消耗，用来支持动态调整功能。不存聊天正文。",
             dec_count + stats_count, dec_bytes + stats_bytes,
             days=_eff("decisions_keep_days"),
             days_key="decisions_keep_days",
             # 合并后保留天数只吃全局值（self.decisions_keep_days），会话里改不了。
             global_only=True)

        # 用量账本：requests.jsonl / tokens.jsonl / archive/*.jsonl，永久保存
        _row("ledger", "用量账本", "条",
             "每轮请求和 token 的流水，价目页的花费、token、命中率都读它。"
             "永久保存，往月的行按月归档。",
             led_count, led_bytes, clear_days=30)
        return out

    def _cache_kinds_cached(self, sid: str = "") -> list:
        """_cache_kinds 的 10 秒缓存版（和 /cache-stats 共用同一份结果）。

        概览卡 5 秒轮询一次，缓存表要扫几万个文件 + 逐个 json.loads，不能每次都重算；
        10 秒和 /cache-stats 自己的缓存同一个节奏，两边拿到的就是同一份数字。
        按 sid 分开存：设置页选中某个群时缓存的是那个群的数字，概览卡要的是全局
        （sid=""），两边互不顶掉，否则每 5 秒就会重扫一遍。
        返回的是副本，调用方随便改。
        """
        now = time.time()
        cache = getattr(self, "_cache_kinds_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._cache_kinds_cache = cache
        # 顺手把过期的清掉，字典不会越攒越大；概览在子线程、缓存页在事件循环，
        # 两边可能同时动这个字典，遍历撞上插入就跳过——缓存而已，不值得抛异常。
        try:
            for key in [k for k, v in cache.items() if (now - v[0]) >= 10]:
                cache.pop(key, None)
        except RuntimeError:
            pass
        hit = cache.get(sid)
        if hit:
            return [dict(row) for row in hit[1]]
        kinds = self._cache_kinds(sid)
        cache[sid] = (now, kinds)
        return [dict(row) for row in kinds]

    def _mem_usage(self) -> dict:
        """概览卡右上角「插件数据」：直接复用缓存记录页那套统计（用户第 4 条）。

        以前这里走 _disk_usage() 把 plugin_data/<插件>/ 整个目录走一遍，media_cache
        那一项把 fw_ / tw_ / archive / derived / 未归属的图全算进去（线上 1086 MB），
        而缓存记录页的「图片缓存」只算能归属到会话的聊天图（697 MB），两边差了
        388 MB，用户看到的就是「概览说 1117 MB、缓存页说 729 MB」。

        现在一个字节都不自己算：parts 就是 _cache_kinds() 的行（名字按
        _MEM_PART_NAMES 映射成前端认的那几个），total 是它们的和，所以概览卡和
        缓存记录页永远同一口径。代价是「插件数据」不再等于目录实际占用——不在
        缓存表里的东西（配置文件、prompt 模板、裁切日志 windows/、以及转发图
        fw_ 的图片本体，缓存表只按索引记 0.03 MB）不计入；这些要么不是缓存，
        要么缓存页本来就不算，两边一致比「目录总大小」更重要。

        性能：_cache_kinds_cached() 有 10 秒缓存，和 /cache-stats 共用，5 秒轮询
        不会每次都扫全目录；真扫的时候也丢线程（调用方 asyncio.to_thread）。
        """
        kinds = self._cache_kinds_cached("")
        parts = _mem_parts_from_kinds(kinds)
        total = sum(int(p.get("bytes") or 0) for p in parts)
        parts.sort(key=lambda x: -int(x.get("bytes") or 0))
        return {"total_mb": round(total / 1048576.0, 1), "parts": parts}

    #: 消息日志 / 窗口快照按天混写，拆「每会话占用」要逐行读盘。目录大到这个尺寸就不拆了
    #: （缓存设置页是点开就看的，不能为了统计卡十几秒），在上面的 partial 里说明哪类没拆。
    _CACHE_SESSION_SCAN_MAX = 32 * 1024 * 1024

    @staticmethod
    def _cache_sid_key(name) -> str:
        """会话名归一：目录名、stats/<sid>.json、jsonl 记录里的 sid 三种写法并成一把键。

        和 state/request_log._safe_sid、state/adaptive._safe_sid 同一套规则
        （非字母数字 → 下划线），所以 gm:1145141974 / gm_1145141974 会并成一行。
        """
        text = normalize_sid(str(name or "").strip())
        # 目录名/文件名已经是「下划线形态」，normalize_sid 只认冒号形态，所以再兜一层：
        # 老数据里有「桃华_gm_1145141974」这种带机器人名的目录名，机器人改名后同群会分成
        # 两行（用户 m13708 反馈的那种「同群两份」）。这里只认 gm_/dm_ 这两段，往前的前缀
        # 一律当机器人名扔掉；认不出来就原样保留，不猜。
        parts = text.split("_")
        for idx in range(len(parts) - 2, -1, -1):
            if parts[idx] in ("gm", "dm") and parts[idx + 1]:
                text = parts[idx] + "_" + parts[idx + 1]
                break
        safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in text).strip(".")
        # 别放行 "." / ".."：这个键会被拼成 stats/<key>.json（审查报告）。
        return safe or "unknown"

    def _cache_known_sids(self) -> set:
        """插件登记过的会话（会话登记簿 + sessions.json），用来解释「怎么没列到这个群」。"""
        out = set()
        reg = getattr(self, "session_registry", None)
        for sid in getattr(reg, "known", None) or ():
            if sid:
                out.add(str(sid))
        path = getattr(getattr(self, "store", None), "path", None)
        if path:
            try:
                raw = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    out.update(str(k) for k in raw if k)
            except Exception:
                pass
        return out

    def _cache_sessions(self, limit: int = 200) -> dict:
        """每个群聊/私聊各占多少缓存（用户 m13708 第 4 条 + task-8 第 2 条）。

        以前只数 requests/ 一个目录，于是「只有统计账本」的会话（比如
        从未成功发过请求的群）、「只有摘要」的会话全都从缓存页消失了——用户
        task-8 第 2 条问的就是「怎么只列了 5 个」。现在把插件自己按会话分的
        六类全算进来：

        - requests：每会话一个子目录，目录名即归一 sid；请求快照与它的调用轨迹存在同一份文件里；
        - stats：stats/<sid>.json（自适应阈值账本，每会话一个文件），requests.jsonl /
          tokens.jsonl 是混写账本，归不到单个会话；
        - summaries：summaries.json 里一个会话一条，按该条文本的 UTF-8 字节数算；
        - decisions / windows：按天一个 .jsonl、多会话混写，逐行认记录里的 sid 再合计
          （体积超过 _CACHE_SESSION_SCAN_MAX 就跳过，返回 partial 里点名）。

        另外把「登记过但暂时没有任何缓存文件」的会话也补成 0 字节行（sources=[]），
        这样页面上能看到全部会话、只是占用为 0，而不是凭空少几个。
        行结构：sid / requests / decisions / windows / stats / summaries /
        bytes / mb / sources。每类内部统一 {count, bytes, mb}。
        """
        def _blank_row(sid: str) -> dict:
            return {
                "sid": sid,
                "requests": {"count": 0, "bytes": 0, "mb": 0.0},
                "decision_snapshots": {"count": 0, "bytes": 0, "mb": 0.0},
                "decisions": {"count": 0, "bytes": 0, "mb": 0.0},
                "windows": {"count": 0, "bytes": 0, "mb": 0.0},
                "stats": {"count": 0, "bytes": 0, "mb": 0.0},
                "summaries": {"count": 0, "bytes": 0, "mb": 0.0},
                "bytes": 0,
                "mb": 0.0,
                "sources": [],
            }

        rows: dict = {}

        def _row(key: str) -> dict:
            row = rows.get(key)
            if row is None:
                row = _blank_row(key)
                rows[key] = row
            return row

        def _set(kind: str, key: str, count, size) -> None:
            # 同一会话可能来自两份同源数据（例如机器人改名前后的 stats/<sid>.json、
            # 分机器人的 prompts 目录），所以这里是累加而不是覆盖。
            size = int(size or 0)
            row = _row(key)
            cell = row[kind]
            total = int(cell["bytes"]) + size
            row[kind] = {
                "count": int(cell["count"]) + int(count or 0),
                "bytes": total,
                "mb": round(total / 1048576.0, 2),
            }
            if kind not in row["sources"]:
                row["sources"].append(kind)

        # ① 回复快照 / 决策快照（v3.64 起调用轨迹也在这份 JSON 的 "trace" 键里）：
        #    每会话一个子目录；两种快照同目录，靠 decision_only 分堆（v4.00，用户 m10782）。
        root = getattr(self.request_log, "dir", None)
        if root:
            try:
                folders = [d for d in Path(root).iterdir() if d.is_dir()]
            except Exception:
                folders = []
            for folder in folders:
                split = {"reply": [0, 0], "decision": [0, 0]}
                try:
                    files = list(folder.glob("*.json"))
                except Exception:
                    files = []
                for path in files:
                    try:
                        size = int(path.stat().st_size or 0)
                    except OSError:
                        continue
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        text = ""
                    slot = split["decision" if '"decision_only": true' in text else "reply"]
                    slot[0] += 1
                    slot[1] += size
                key = self._cache_sid_key(folder.name)
                _set("requests", key, split["reply"][0], split["reply"][1])
                _set("decision_snapshots", key, split["decision"][0], split["decision"][1])

        # ② 会话统计：stats/<sid>.json 每会话一个；两本 jsonl 账本混写，不按会话拆
        stats_dir = getattr(self.stats, "dir", None)
        if stats_dir:
            try:
                stat_files = list(Path(stats_dir).glob("*.json"))
            except Exception:
                stat_files = []
            for path in stat_files:
                try:
                    size = int(path.stat().st_size or 0)
                except OSError:
                    continue
                count = 0
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    count = len(raw) if isinstance(raw, dict) else 0
                except Exception:
                    count = 0
                _set("stats", self._cache_sid_key(path.stem), count, size)

        # ③ 中期摘要：一个会话一条，按该条文本字节数算（值可能是 str 也可能是 dict）
        try:
            if self.summaries is not None:
                for sid, summary in (self.summaries.all() or {}).items():
                    key = self._cache_sid_key(sid)
                    if isinstance(summary, str):
                        text = summary
                    else:
                        try:
                            text = json.dumps(summary, ensure_ascii=False)
                        except Exception:
                            text = str(summary)
                    _set("summaries", key, 1, len(text.encode("utf-8")))
        except Exception:
            pass

        # ④ 消息日志 / 窗口快照：按天混写的 .jsonl，逐行认 sid 再合计
        partial = []
        for kind, log in (("decisions", self._log), ("windows", self._window_log)):
            root = getattr(log, "dir", None)
            if not root:
                continue
            try:
                files = [p for p in Path(root).glob("*.jsonl") if p.is_file()]
                total_size = sum(int(p.stat().st_size or 0) for p in files)
            except Exception:
                continue
            if total_size > self._CACHE_SESSION_SCAN_MAX:
                partial.append(kind)
                continue
            buckets: dict = {}
            for path in files:
                try:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    continue
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    sid = rec.get("sid") if isinstance(rec, dict) else None
                    if not sid:
                        continue
                    bucket = buckets.get(self._cache_sid_key(sid))
                    if bucket is None:
                        bucket = buckets[self._cache_sid_key(sid)] = [0, 0]
                    bucket[0] += 1
                    bucket[1] += len(line.encode("utf-8"))
            for key, (count, size) in buckets.items():
                _set(kind, key, count, size)

        # ⑤ 登记过 / 有设置但暂时没有缓存文件的会话：补 0 字节行，别凭空消失
        try:
            known = self._cache_known_sids()
        except Exception:
            known = set()
        for sid in known:
            _row(self._cache_sid_key(sid))

        items = list(rows.values())
        kinds_all = ("requests", "decisions", "windows", "stats", "summaries")
        for row in items:
            total = sum(int(row[k]["bytes"]) for k in kinds_all)
            row["bytes"] = total
            row["mb"] = round(total / 1048576.0, 2)
        items.sort(key=lambda x: (-int(x["bytes"]), x["sid"]))
        return {
            "sessions": items[: max(0, int(limit))],
            "session_total": len(items),
            "partial": partial,
        }

    @_api("GET", "/cache-stats")
    async def api_cache_stats(self, sid: str = ""):
        """缓存设置页：各类缓存各占多少、保留线是多少（用户 m11091 第 11 条）。

        用户 m13708 第 4 条：「缓存记录还需要记录每个群聊私聊都占用了多少」——
        所以除了按类型汇总，再给一份按会话（群聊/私聊）拆开的 sessions。

        用户 m18059 第 5 条：带 ?sid= 时每行的 limit / days 按这个会话的有效值算
        （单群覆盖 > 全局），不带就是全局值。limits 那份始终是全局值，给全局
        输入框回填用。
        """
        sid = str(sid or "").strip()
        if self.window is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        # v3.66（用户 m19536 第 6 条）：缓存统计要扫盘（几万文件 + 逐个 json.loads），
        # 设置页每切一次页签、每存一次配置都会再来要一次。10 秒缓存够新，也够省。
        cache = getattr(self, "_cache_stats_cache", None)
        if cache and cache[0] == sid and (time.time() - cache[1]) < 10:
            return cache[2]
        try:
            kinds = self._cache_kinds_cached(sid)
        except Exception:
            logger.exception("[multi-decision] 统计缓存失败")
            return {"ok": False, "detail": "统计失败，详情看日志"}
        total_count = sum(int(k.get("count") or 0) for k in kinds)
        total_bytes = sum(int(k.get("bytes") or 0) for k in kinds)
        try:
            per_session = self._cache_sessions(limit=200)
        except Exception:
            logger.exception("[multi-decision] 统计会话缓存失败")
            per_session = {"sessions": [], "session_total": 0}
        payload = {
            "ok": True,
            # 每行的 limit/days 已经是「这个会话的有效值」（sid 为空时 = 全局）
            "sid": sid,
            "kinds": kinds,
            "total": {
                "count": total_count,
                "bytes": total_bytes,
                "mb": round(total_bytes / 1048576.0, 2),
            },
            # 每条：sid / requests{count,bytes,mb} / decisions{...} / windows{...} / bytes / mb
            "sessions": per_session.get("sessions") or [],
            "session_total": int(per_session.get("session_total") or 0),
            # task-8 第 2 条：六类里如果有哪类因为体积太大没拆到会话（decisions/windows），
            # 在这里点名，页面可以提示「这一类的占用没算进会话行」。
            "sessions_partial": per_session.get("partial") or [],
            "limits": {
                "media_cache_limit": int(getattr(self, "media_cache_limit", 0) or 0),
                "media_cache_keep_days": int(getattr(self, "media_cache_keep_days", 0) or 0),
                "media_other_keep_days": int(getattr(self, "media_other_keep_days", 0) or 0),
                # v3.66（用户 m19536 第 4 条）：四个上限的「私聊默认」
                "media_cache_limit_dm": int(getattr(self, "media_cache_limit_dm", 0) or 0),
                "media_other_limit": int(getattr(self, "media_other_limit", 0) or 0),
                "media_other_limit_dm": int(getattr(self, "media_other_limit_dm", 0) or 0),
                "trace_keep_dm": int(getattr(self, "trace_keep_dm", 0) or 0),
                "decision_keep_dm": int(getattr(self, "decision_keep_dm", 0) or 0),
                "windows_keep_dm": int(getattr(self, "windows_keep_dm", 0) or 0),
                # v4.90（用户 m01875 第 1 条）：显示条数也按群聊 / 私聊两套下发
                "window_show": int(getattr(self, "window_show", 0) or 0),
                "window_show_dm": int(getattr(self, "window_show_dm", 0) or 0),
                # 用户 m13708 第 3/4 条：新增 7 个保留键，前端按同名键回填输入框
                "requests_keep_days": int(getattr(self, "requests_keep_days", 0) or 0),
                "decisions_keep_days": int(getattr(self, "decisions_keep_days", 0) or 0),
                "windows_keep_days": int(getattr(self, "windows_keep_days", 0) or 0),
                # 2026-11-18（用户 m13966）：stats_keep_days 现在只是 decisions_keep_days 的别名
                "stats_keep_days": int(getattr(self, "stats_keep_days", 0) or 0),
                # v4.54（用户 m18227）：新增两类按会话的上限
                "forward_media_keep": int(getattr(self, "forward_media_keep", 0) or 0),
                "forward_media_keep_dm": int(getattr(self, "forward_media_keep_dm", 0) or 0),
                "retry_keep": int(getattr(self, "retry_keep", 0) or 0),
                "retry_keep_dm": int(getattr(self, "retry_keep_dm", 0) or 0),
                "forward_media_keep_days": int(getattr(self, "forward_media_keep_days", 0) or 0),
                "retry_keep_days": int(getattr(self, "retry_keep_days", 0) or 0),
                # v3.67（用户 m20117）：中期摘要的三个键挪去窗口设置页；缓存页只统计占用
            },
        }
        self._cache_stats_cache = (sid, time.time(), payload)
        return payload

    @_api("POST", "/cache-clear")
    async def api_cache_clear(self, kind: str = "media", days: int = 0):
        """按类型清缓存（用户 m13708 第 4 条；m21482 加「只清 7 天前」和「全部」）。

        kind：media / media_other / requests / decision_snapshots / trims / logs /
        windows / summaries / forward_cache / retry_history /
        window_dm / ledger；kind="all" 表示除消息日志与统计、用量账本外的
        所有可清理类型一起清。
        days=0 清全部；days>0 只清早于 days 天的记录。

        清 requests **不动** stats/requests.jsonl 与 stats/tokens.jsonl
        两本用量账；清 ledger 才会动它们。
        """
        if self.window is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        kind = str(kind or "media").strip() or "media"
        # 2026-11-18 审查 H02：days 只认「0=全清」与正整数。
        # 以前 max(0, int(days or 0)) 加 except→0，会把 -1、"abc" 都折成 0，
        # 接着 cutoff 为 None 走 clear_all()，一次误传参数就把整类缓存（含账本）清光。
        if days is None or (isinstance(days, str) and not days.strip()):
            days = 0
        # 小数也拒：int(-0.5) 会截成 0，等于又让坏参数走成「全清」。
        if isinstance(days, float) and not days.is_integer():
            return {"ok": False, "kind": kind,
                    "detail": "清理天数必须是整数（0 表示全部）"}
        try:
            days = int(days)
        except (TypeError, ValueError):
            return {"ok": False, "kind": kind,
                    "detail": "清理天数必须是整数（0 表示全部）"}
        if days < 0:
            return {"ok": False, "kind": kind, "detail": "清理天数不能是负数"}
        cutoff = time.time() - days * 86400 if days > 0 else None
        if kind == "traces":
            return {"ok": False, "kind": kind,
                    "detail": "调用轨迹已经并进窗口快照，清「窗口快照」即可"}
        # v4.90（用户 m01133 第 2 条）：三种快照合成一类，缓存表只剩「窗口快照」一行。
        # decision_snapshots / trims / windows 留着当旧名字的别名，老页面点进来照样能清。
        clearable = ("media", "media_other", "requests", "decision_snapshots",
                     "trims", "windows", "summaries", "forward_cache",
                     "retry_history", "window_dm", "logs", "ledger",
                     # 2026-11-18（用户 m13966）：decisions / stats 是合并前的旧名字，
                     # 留着当别名，老页面点进来照样能清。
                     "decisions", "stats")
        # 「全部」不含消息日志与统计、用量账本：这两类是自适应阈值和价目页的账本，
        # 清空会重置自适应与花费统计，只能从它们自己那一行单独清（按钮提示也写着）。
        _keep_out = ("logs", "ledger", "decisions", "stats")
        if kind == "all":
            kinds = [k for k in clearable if k not in _keep_out]
        elif kind in clearable:
            kinds = [kind]
        else:
            return {"ok": False, "kind": kind, "detail": "未知的缓存类型"}

        def _clear_one(name: str) -> int:
            if name == "media":
                out = (self.window.clear_media_cache_before(cutoff)
                       if cutoff is not None else self.window.clear_media_cache())
                return int((out or {}).get("removed") or 0)
            if name == "requests":
                # v4.90：三种快照合成一类，清「窗口快照」就是全清（回复 + 决策 + 裁切），
                # 连裁切日志一起带走——日志是走势图橙色标记的索引，快照没了留着它只会
                # 让图上多出一堆点不开的橙柱。
                n = 0
                if self.request_log:
                    n += int(self.request_log.clear_before(cutoff)
                             if cutoff is not None else self.request_log.clear_all())
                if self._window_log:
                    n += int(self._window_log.clear_before(cutoff)
                             if cutoff is not None else self._window_log.clear_all())
                return n
            if name == "decision_snapshots":
                # 旧名字的别名：现在跟「窗口快照」是同一件事
                if not self.request_log:
                    return 0
                return int(self.request_log.clear_before(cutoff, decisions_only=True)
                           if cutoff is not None else self.request_log.clear_decisions())
            if name == "trims":
                # 旧名字的别名：裁切快照现在也是「窗口快照」的一部分
                if not self.request_log:
                    return 0
                return int(self.request_log.clear_before(cutoff, trims_only=True)
                           if cutoff is not None else self.request_log.clear_trims())
            if name == "windows":
                # 旧名字的别名：裁切日志
                if not self._window_log:
                    return 0
                return int(self._window_log.clear_before(cutoff)
                           if cutoff is not None else self._window_log.clear_all())
            if name == "summaries":
                if not self.summaries:
                    return 0
                return int(self.summaries.clear_before(cutoff)
                           if cutoff is not None else self.summaries.clear_all())
            if name == "forward_cache":
                return int(forward_media_clear(cutoff))
            if name == "retry_history":
                return int(self.retry_history.clear(cutoff)) if self.retry_history else 0
            if name == "window_dm":
                return int(self.window.clear_dm_before(cutoff)
                           if cutoff is not None else self.window.clear_dm_all())
            if name == "decisions":
                # 旧名字的别名：只清消息日志（合并前那一行）
                if not self._log:
                    return 0
                return int(self._log.clear_before(cutoff)
                           if cutoff is not None else self._log.clear_all())
            if name == "logs":
                # 消息日志 + 会话统计（按天统计）一起清；用量账本不在这条路上。
                n = 0
                if self._log:
                    n += int(self._log.clear_before(cutoff)
                             if cutoff is not None else self._log.clear_all())
                if self.stats:
                    n += int(self.stats.clear_stats_before(cutoff)
                             if cutoff is not None else self.stats.clear_stats_all())
                return n
            if name == "ledger":
                if not self.stats:
                    return 0
                return int(self.stats.clear_ledger_before(cutoff)
                           if cutoff is not None else self.stats.clear_ledger_all())
            if name == "media_other":
                cache = _media_cache_root()
                return int(clear_other_media(cache, cutoff)) if cache else 0
            if name == "stats":
                # 旧名字的别名：统计和账本一起清（合并前那一行的行为）
                if not self.stats:
                    return 0
                return int(self.stats.clear_before(cutoff)
                           if cutoff is not None else self.stats.clear_all())
            return 0

        # 清之前先扫一遍体积：删完就不知道腾出多少空间了
        try:
            before = {str(k.get("key")): int(k.get("bytes") or 0) for k in self._cache_kinds()}
        except Exception:
            before = {}
        removed = 0
        failed = []
        for name in kinds:
            try:
                removed += _clear_one(name)
            except Exception:
                failed.append(name)
                logger.exception(f"[multi-decision] 清空缓存失败（kind={name}）")
        # 用清理前后同一口径的体积差算腾出多少；直接拿 before 会把「只清 7 天前」
        # 没删掉的部分也算进去，数字会虚高。
        try:
            after = {str(k.get("key")): int(k.get("bytes") or 0)
                     for k in self._cache_kinds()}
            mb = round(sum(max(0, int(before.get(name) or 0) - int(after.get(name) or 0))
                           for name in kinds) / 1048576.0, 2)
        except Exception:
            mb = 0.0
        self._cache_stats_cache = None
        # 用户第 4 条：概览卡现在也吃这份缓存表，清完必须一起失效，否则最多 10 秒
        # 还显示清理前的体积。
        self._cache_kinds_cache = {}
        # v4.88：清完缓存立刻让磁盘占用重新算一遍，否则概览卡最多 60 秒还显示旧体积。
        _DISK_CACHE["t"] = 0.0
        if failed:
            return {"ok": False, "kind": kind, "removed": removed, "mb": mb,
                    "days": days, "failed": failed,
                    "detail": "部分缓存清理失败，详情看日志"}
        logger.info(f"[multi-decision] 已清理缓存 {kind}（{days or '全部'}）：{removed} 条，约 {mb} MB")
        return {"ok": True, "kind": kind, "removed": removed, "mb": mb, "days": days}

    @_api("GET", "/overrides")
    async def api_overrides(self, sid: str = ""):
        """某个会话的单群配置：全局值 / 覆盖值 / 有效值。"""
        # sid 允许为空：设置页的全局模式也要用它拿 bounds（数字项的上下限）
        return {"ok": True, **self.overrides_payload(sid)}

    @_api("POST", "/overrides")
    async def api_overrides_set(self, sid: str, values: str = "", merge: str = ""):
        """保存单群配置。values 是 JSON 对象；传空对象等于恢复跟随全局。"""
        if not sid:
            return {"ok": False, "detail": "缺少 sid"}
        if self.overrides is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        try:
            data = json.loads(values) if values else {}
        except Exception:
            return {"ok": False, "detail": "values 不是合法 JSON"}
        if not isinstance(data, dict):
            return {"ok": False, "detail": "values 必须是对象"}
        # merge=1：设置页只渲染 schema 里的键，session_enabled 这种「天生每群一份、
        # 不在 schema 里」的覆盖键不在表单里，不合并就会被设置页的保存顺手清掉。
        if str(merge).strip().lower() in ("1", "true", "yes", "on"):
            try:
                data = {**self.overrides.get(sid), **data}
            except Exception:
                pass
        # 值为 None = 「这一项改回跟随全局」。merge 会把旧覆盖并回来，
        # 必须在这里把它们挑出来跳过，否则清空某一项永远清不掉（审查 bug）
        unset = {k for k, v in data.items() if v is None}
        # 只收白名单里的键；值等于全局值时直接不存，保持「只记差异」
        cleaned = {}
        for key, value in data.items():
            if key in unset:
                continue
            if key not in PER_SESSION_KEYS:
                continue
            base = getattr(self, key, None)
            try:
                coerced = _coerce(key, value, base)
            except Exception:
                continue
            # 名字这类文本项留空就是「跟随全局」，别把空串存成覆盖项
            if isinstance(coerced, str) and not coerced.strip():
                continue
            if coerced != base:
                cleaned[key] = coerced
        # 用户 m06706：「决策模式」和「JEV 决策」只能开一个。前端已经互斥，这里再兜一次
        # 直接打接口 / 手改配置的情况——同一份提交里两个都开就拒绝；只开了一个、
        # 另一个是从全局继承来的，就给这个群写一条显式的「关」，别让它变成双开。
        req_adv, req_jev = cleaned.get("advanced_enabled"), cleaned.get("jev_enabled")
        if req_adv is True and req_jev is True:
            return {"ok": False, "code": "MODE_CONFLICT",
                    "detail": "「决策模式」和「JEV 决策」只能开一个"}
        if req_jev is True and self.eff("advanced_enabled", sid):
            cleaned["advanced_enabled"] = False
        if req_adv is True and self.eff("jev_enabled", sid):
            cleaned["jev_enabled"] = False
        was_enabled = self.eff("session_enabled", sid) is not False
        result = self.overrides.set(sid, cleaned)
        if not getattr(self.overrides, "last_save_ok", True):
            return {"ok": False, "code": "SAVE_FAILED",
                    "detail": "写入单群配置失败，这次修改没有保存"}
        now_enabled = self.eff("session_enabled", sid) is not False
        if was_enabled != now_enabled and self.engine is not None:
            try:
                reset = getattr(self.engine, "reset_session_input", None)
                if callable(reset):
                    await reset(sid)
            except Exception:
                logger.exception("[multi-decision] 切换会话时清理框架积压失败")
        # 关掉这个会话 = 它这段时间的消息一条都不会回。窗口里攒着的旧消息如果留着，
        # 下次重新打开时会当成「这次请求的历史」一起发出去（用户 m33778 图3）。
        # 但私聊例外（用户 m06348 第 6 条）：私聊窗口要「开着决策就一直收、一直缓存」，
        # 关开关只是暂停，不清窗口——重新打开时接着用攒下来的上下文。
        try:
            if (self.eff("session_enabled", sid) is False and self.window is not None
                    and str(sid).startswith("gm:")):
                self.window.clear(sid)
                logger.info(f"[multi-decision] {sid} 已关闭，窗口一并清空")
        except Exception:
            logger.exception("[multi-decision] 关会话清窗口失败（已吞掉）")
        logger.info(
            f"[multi-decision] 单群配置 {sid}：{len(cleaned)} 项覆盖"
            if cleaned
            else f"[multi-decision] 单群配置 {sid}：已恢复跟随全局"
        )
        return {"ok": True, "override": result, **self.overrides_payload(sid)}

    # ------------------------------------------------------------ 窗口拉黑短语
    def _phrase_global_items(self) -> list:
        """全局池里的短语：所有群共享的那份（用户 m02693 第 4 条）。"""
        try:
            data = json.loads(self.phrase_filter_list or "[]")
        except Exception:
            data = []
        return [str(it).strip() for it in (data or []) if str(it).strip()]

    def _phrase_own_items(self, sid: str) -> list:
        """本群独立池里额外追加的短语（不含已经在全局池里的词）。"""
        if not sid or self.overrides is None:
            return []
        try:
            raw = self.overrides.value(sid, "phrase_filter_list", None)
        except Exception:
            return []
        if raw is None:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            data = []
        seen = set(self._phrase_global_items())
        out = []
        for it in data or []:
            word = str(it).strip()
            if word and word not in seen and word not in out:
                out.append(word)
        return out

    def _phrase_unblock_items(self, sid: str) -> list:
        """本会话「已解除」的全局短语（用户 m07715）。

        单群 / 私聊可以把全局拉黑的条目对本会话解除，写进 overrides 的
        phrase_filter_unblock，全局池一个字不动；切回全局视图照样能看到它。
        """
        if not sid or self.overrides is None:
            return []
        try:
            raw = self.overrides.value(sid, "phrase_filter_unblock", None)
        except Exception:
            return []
        if raw is None:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            data = []
        out = []
        for it in data or []:
            word = str(it).strip()
            if word and word not in out:
                out.append(word)
        return out

    def _phrase_unblock_write(self, sid: str, items: list) -> None:
        """把本会话的解除名单写回单群覆盖；空了就把这个键删掉。"""
        if not sid or self.overrides is None:
            return
        try:
            cleaned = []
            for it in items or []:
                word = str(it).strip()
                if word and word not in cleaned:
                    cleaned.append(word)
            patch = self.overrides.get(sid)
            if cleaned:
                patch["phrase_filter_unblock"] = json.dumps(cleaned, ensure_ascii=False)
            else:
                patch.pop("phrase_filter_unblock", None)
            self.overrides.set(sid, patch)
        except Exception:
            logger.exception("[multi-decision] 保存本会话解除名单失败")

    def _phrase_effective_items(self, sid: str = "") -> list:
        """生效清单 = 全局池 + 本群独立池 − 本会话解除的。

        老行为是「单群覆盖整份替换全局」：在窗口里拉黑一条只影响这个群，
        设置页的全局清单里也看不见（用户 m02693 第 4 条）。现在反过来——
        窗口拉黑默认写进全局池（所有群一起生效），独立池只当追加项，
        默认就是全局那份；某个群想多拦几句再往自己的池子里加。
        用户 m07715：单群 / 私聊还能把全局的条目「对本会话解除」，
        那份解除名单只减不增，全局池不动。
        """
        out = list(self._phrase_global_items())
        for word in self._phrase_own_items(sid):
            if word not in out:
                out.append(word)
        drop = set(_norm_phrase(w) for w in self._phrase_unblock_items(sid))
        if drop:
            out = [w for w in out if _norm_phrase(w) not in drop]
        return out

    def _global_config_path(self):
        """框架保存插件全局配置的那个文件（data/config/plugins/<id>.json）。"""
        try:
            from pathlib import Path
            data_dir = Path(self.ctx.get_plugin_data_dir())   # data/plugin_data/<id>
            return data_dir.parents[1] / "config" / "plugins" / (PLUGIN_ID + ".json")
        except Exception:
            return None

    def _phrase_global_write(self, items: list) -> bool:
        """把全局池写进框架的插件配置；写不动返回 False，让调用方退回单群写入。"""
        try:
            payload = json.loads(_normalize_phrases(items))
        except Exception:
            return False
        path = self._global_config_path()
        if path is None:
            return False
        try:
            if not path.is_file():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            data["phrase_filter_list"] = json.dumps(payload, ensure_ascii=False)
            # 原子替换：先写同目录临时文件再 os.replace，避免框架正好在读时读到半截。
            # 临时名必须每次唯一：固定 .tmp 时两个写者会撞同一个文件，后写者把前写者
            # 的临时文件挪走，前写者 replace 就抛 FileNotFoundError（写失败还留下垃圾）。
            # 这只解决「同目录撞名/半截文件」，跨进程的读改写覆盖（最后写入者赢）没有解决。
            tmp = path.with_name(path.name + ".tmp-" + os.urandom(4).hex())
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(data, ensure_ascii=False, indent=2))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass
        except Exception:
            logger.exception("[multi-decision] 写全局短语池失败")
            return False
        # 插件自己内存里那份同步更新，engine 立刻按新清单判定。框架内存里的配置
        # 要等它自己 reload，设置页短时间可能还显示旧值——不影响本插件生效。
        self.phrase_filter_list = json.dumps(payload, ensure_ascii=False)
        return True

    def _phrase_own_write(self, sid: str, items: list) -> None:
        """把本群独立池写回单群覆盖；和全局一致（或为空）就删掉这个覆盖键。"""
        if not sid or self.overrides is None:
            return
        try:
            cleaned = _normalize_phrases(items)
            if cleaned == _normalize_phrases(self._phrase_global_items()):
                cleaned = "[]"
            patch = self.overrides.get(sid)
            if cleaned == "[]":
                patch.pop("phrase_filter_list", None)
            else:
                patch["phrase_filter_list"] = cleaned
            self.overrides.set(sid, patch)
        except Exception:
            logger.exception("[multi-decision] 保存本群短语池失败")

    def _phrase_block_payload(self, sid: str = "") -> dict:
        """当前生效的「不触发短语」清单，窗口里的拉黑按钮据此判断是否已拉黑。

        用户 m07715：带上 unblock_items（本会话解除掉的全局词），设置页据此把
        那几枚胶囊画成「已解除」；global_items / own_items 用来区分来源。
        """
        items = self._phrase_effective_items(sid)
        return {
            "enabled": bool(self.eff("phrase_filter_enabled", sid)),
            "items": items,
            "count": len(items),
            "global_count": len(self._phrase_global_items()),
            "own_count": len(self._phrase_own_items(sid)),
            "global_items": self._phrase_global_items(),
            "own_items": self._phrase_own_items(sid),
            "unblock_items": self._phrase_unblock_items(sid),
        }

    @_api("GET", "/phrase-block")
    async def api_phrase_block(self, sid: str = ""):
        """窗口里的拉黑按钮：拿这个会话当前生效的清单。"""
        return {"ok": True, **self._phrase_block_payload(sid)}

    @_api("POST", "/phrase-block")
    async def api_phrase_block_set(self, sid: str, text: str = "", action: str = "add"):
        """把一条消息整条拉黑进「不触发短语」清单，或者解黑。

        用户 m02693 第 4 条：拉黑进全局池——写一次所有群都不再触发；本群
        overrides 里的 phrase_filter_list 退化成「独立池」，只在全局之外追加。
        全局配置写不动（文件不存在/权限不够）时退回老做法：只写本群独立池，
        至少当场生效，返回体里会用 scope 说明这次落在哪儿。
        比对口径和 engine._phrase_hit 一致：整条消息归一化后等于清单里的词。

        用户 m07715 第 4 条：单群 / 私聊能把全局条目「对本会话解除」——
        remove 在带 sid 且词在全局池里时只写解除名单（phrase_filter_unblock），
        全局池一个字不动；restore 把词从解除名单里拿掉（本会话恢复生效）；
        add 永远写全局，若这个词本来被本会话解除过，add 只做本会话恢复。
        sid 为空＝全局视图：直接改全局池，没有解除名单这回事。
        """
        if sid and self.overrides is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        act = str(action or "add").strip().lower()
        if act not in ("add", "remove", "restore"):
            return {"ok": False, "detail": "action 只能是 add / remove / restore"}
        word = _norm_phrase(text)
        # 图片等占位不是文字；只拦添加，历史垃圾项仍须允许解除。
        text_only = re.sub(r"\[(?:图片|表情|语音|视频|文件|image(?:\s+attached)?|sticker(?:\s+attached)?|kira_image_ref|input_image)\]", "", str(text or ""), flags=re.I)
        if not word or (act == "add" and not _norm_phrase(text_only)):
            return {"ok": False, "detail": "这条消息没有可拉黑的文字"}
        items = list(self._phrase_block_payload(sid)["items"])
        unblock = self._phrase_unblock_items(sid)
        unblock_norm = [_norm_phrase(w) for w in unblock]
        if act == "restore":
            if word not in unblock_norm:
                return {"ok": True, "changed": False, "word": word, "scope": "session",
                        **self._phrase_block_payload(sid)}
            self._phrase_unblock_write(sid, [w for w in unblock if _norm_phrase(w) != word])
            logger.info(f"[multi-decision] {sid} 恢复本会话解除的短语：{word}")
            return {"ok": True, "changed": True, "word": word, "scope": "session",
                    **self._phrase_block_payload(sid)}
        if act == "add":
            if word in unblock_norm:
                # 全局池本来就有它，只是本会话解除过：再拉黑一次＝本会话恢复
                self._phrase_unblock_write(sid, [w for w in unblock if _norm_phrase(w) != word])
                logger.info(f"[multi-decision] {sid} 重新拉黑短语（本会话恢复）：{word}")
                return {"ok": True, "changed": True, "word": word, "scope": "session",
                        **self._phrase_block_payload(sid)}
            if word in [_norm_phrase(w) for w in items]:
                return {"ok": True, "changed": False, "word": word, "scope": "global",
                        **self._phrase_block_payload(sid)}
            new_global = self._phrase_global_items() + [word]
            new_own = self._phrase_own_items(sid)
        else:
            if word in unblock_norm:
                return {"ok": True, "changed": False, "word": word, "scope": "session",
                        **self._phrase_block_payload(sid)}
            if sid and word in [_norm_phrase(w) for w in self._phrase_global_items()]:
                # 单群 / 私聊解除全局条目：只记进本会话的解除名单，全局池不动
                self._phrase_unblock_write(sid, unblock + [word])
                logger.info(f"[multi-decision] {sid} 解除全局短语（只对本会话）：{word}")
                return {"ok": True, "changed": True, "word": word, "scope": "session",
                        **self._phrase_block_payload(sid)}
            keep = [w for w in items if _norm_phrase(w) != word]
            if len(keep) == len(items):
                return {"ok": True, "changed": False, "word": word, "scope": "global",
                        **self._phrase_block_payload(sid)}
            new_global = [w for w in self._phrase_global_items() if _norm_phrase(w) != word]
            new_own = [w for w in self._phrase_own_items(sid) if _norm_phrase(w) != word]
        if self._phrase_global_write(new_global):
            if act == "remove" and len(new_own) != len(self._phrase_own_items(sid)):
                self._phrase_own_write(sid, new_own)
            logger.info(f"[multi-decision] {sid} 窗口拉黑短语 {act}（全局池）：{word}")
            return {"ok": True, "changed": True, "word": word, "scope": "global",
                    **self._phrase_block_payload(sid)}
        # ---- 退路：全局配置写不动，就只改这个群的独立池（老行为）
        if not sid:
            return {"ok": False, "detail": "全局配置写不动，详情看日志"}
        try:
            patch = self.overrides.get(sid)
            # 独立池 = 原有的额外词 +（add 时）这次要拉黑的词。new_own 只装「全局之外」
            # 的追加项，add 的新词还在 new_global 里，这里要单独补进来，否则回退后不生效。
            own_list = list(new_own) + ([word] if act == "add" else [])
            cleaned = _normalize_phrases(own_list)
            if cleaned == (getattr(self, "phrase_filter_list", "[]") or "[]") or cleaned == "[]":
                patch.pop("phrase_filter_list", None)
            else:
                patch["phrase_filter_list"] = cleaned
            self.overrides.set(sid, patch)
        except Exception:
            logger.exception("[multi-decision] 窗口拉黑短语保存失败")
            return {"ok": False, "detail": "保存失败，详情看日志"}
        logger.warning(f"[multi-decision] {sid} 窗口拉黑短语 {act}（只写本群，全局配置写不动）：{word}")
        return {"ok": True, "changed": True, "word": word, "scope": "session",
                "detail": "没能写进全局配置，只对这个群生效",
                **self._phrase_block_payload(sid)}

    # ------------------------------------------------------------ 用户黑名单（用户 m06193）
    # 和上面「短语黑名单」不是一回事：短语按整条消息的文字比对，只影响触发判定；
    # 这里按发言人（QQ 号）拦，命中的消息根本不进请求——决策上下文、回复上下文、
    # 待发都剔掉，但窗口页照常显示（默认折叠，点开还能看）。
    def _user_unblock_items(self, sid: str) -> list:
        """本会话「已解除」的全局拉黑 uid（用户 m07715 第 4 条）。

        单群 / 私聊可以把全局拉黑的人对本会话解除，写进 overrides 的
        user_blacklist_unblock，全局名单一个字不动；切回全局视图照样能看到他。
        """
        if not sid or self.overrides is None:
            return []
        try:
            raw = self.overrides.value(sid, "user_blacklist_unblock", None)
        except Exception:
            return []
        if raw is None:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            data = []
        out = []
        for it in data or []:
            uid = str(it).strip()
            if uid and uid not in out:
                out.append(uid)
        return out

    def _user_unblock_write(self, sid: str, uids: list) -> bool:
        """把本会话的解除名单写回单群覆盖；空了就把这个键删掉。写盘失败返回 False。"""
        if not sid or self.overrides is None:
            return True
        try:
            cleaned = []
            for it in uids or []:
                uid = str(it).strip()
                if uid and uid not in cleaned:
                    cleaned.append(uid)
            patch = self.overrides.get(sid)
            if cleaned:
                patch["user_blacklist_unblock"] = json.dumps(cleaned, ensure_ascii=False)
            else:
                patch.pop("user_blacklist_unblock", None)
            self.overrides.set(sid, patch)
            return bool(getattr(self.overrides, "last_save_ok", True))
        except Exception:
            logger.exception("[multi-decision] 保存本会话解除名单失败")
            return False

    def _window_ublk(self, sid: str) -> dict:
        """窗口页折叠用的用户黑名单。

        用户 m13254：名单以前是另一个接口，比消息晚到，刷新那一瞬间会先按
        「谁都没拉黑」画一遍再折起来，看着像内容露了一下。跟着窗口一起返回，
        首屏（含 sessionStorage 直出）就是折好的。出错就当没有，别把窗口接口带崩。
        窗口每 5 秒拉一次，所以只回折叠要用的字段，不带名字和保留消息号。
        """
        try:
            full = self._blacklist_payload(sid)
        except Exception:
            logger.exception("[multi-decision] 取用户黑名单失败（窗口页按不折叠处理）")
            return {}
        return {
            "ok": True,
            "enabled": bool(full.get("enabled")),
            "items": [{"uid": u} for u in (full.get("uids") or [])],
        }

    def _blacklist_payload(self, sid: str = "") -> dict:
        """当前生效的用户黑名单。

        用户 m07715：带上 unblock_items（本会话解除掉的全局条目），设置页据此
        把那几枚胶囊画成「已解除」；global_count 是全局名单原样条数。
        """
        all_items = self.user_blacklist.items() if self.user_blacklist else []
        drop = set(self._user_unblock_items(sid))
        items = [it for it in all_items if str(it.get("uid") or "") not in drop]
        unblock_items = [it for it in all_items if str(it.get("uid") or "") in drop]
        return {
            "ok": True,
            # 用户 m08153：总开关按会话取——单群 / 私聊可以自己关掉。
            "enabled": bool(self.eff("user_blacklist_enabled", sid)),
            "items": items,
            "count": len(items),
            "uids": [str(it.get("uid") or "") for it in items if it.get("uid")],
            "unblock_items": unblock_items,
            "unblock_count": len(unblock_items),
            "global_count": len(all_items),
        }

    def _blacklist_keep_ids(self, uid: str) -> list:
        """拉黑那一刻已经在窗口里的消息 id：这些历史照留，只剔之后的新消息。"""
        try:
            if self.window is not None:
                return self.window.blacklist_keep_ids(uid)
        except Exception:
            logger.exception("[multi-decision] 收集拉黑保留消息失败（按空处理）")
        return []

    @_api("GET", "/user-blacklist")
    async def api_user_blacklist(self, sid: str = ""):
        """窗口页与设置页共用：当前用户黑名单（带 sid 时按本会话解除过滤）。"""
        return self._blacklist_payload(sid)

    @_api("POST", "/user-blacklist")
    async def api_user_blacklist_set(self, uid: str = "", name: str = "", action: str = "add",
                                     sid: str = ""):
        """拉黑 / 解黑一个用户。

        add 时把「此刻已经在窗口里的消息 id」记进 keep：用户 m06193 的口径是
        只剔除拉黑之后的新消息，历史照留。remove 直接整条删掉，历史本来就没动过。

        用户 m07715 第 4 条：单群 / 私聊 remove 一条全局拉黑时只写本会话的解除
        名单（user_blacklist_unblock），全局名单不动；restore 把 uid 从解除名单
        里拿掉（本会话恢复生效）；add 永远写全局，若这个人本来被本会话解除过，
        add 只做本会话恢复。sid 为空＝全局视图。
        """
        uid = str(uid or "").strip()
        if not uid:
            return {"ok": False, "detail": "缺少 uid"}
        if self.user_blacklist is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        if sid and self.overrides is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        act = str(action or "add").strip().lower()
        if act not in ("add", "remove", "restore"):
            return {"ok": False, "detail": "action 只能是 add / remove / restore"}
        unblock = self._user_unblock_items(sid)
        if act == "restore":
            if uid not in unblock:
                return {**self._blacklist_payload(sid), "changed": False, "uid": uid}
            if not self._user_unblock_write(sid, [u for u in unblock if u != uid]):
                return {"ok": False, "code": "SAVE_FAILED",
                        "detail": "写入单群配置失败，这次恢复没有保存"}
            logger.info(f"[multi-decision] {sid} 恢复本会话解除的拉黑：{uid}")
            return {**self._blacklist_payload(sid), "changed": True, "uid": uid}
        if act == "add":
            if uid in unblock:
                # 全局名单本来就有他，只是本会话解除过：再拉黑一次＝本会话恢复
                if not self._user_unblock_write(sid, [u for u in unblock if u != uid]):
                    return {"ok": False, "code": "SAVE_FAILED",
                            "detail": "写入单群配置失败，这次拉黑没有保存"}
                logger.info(f"[multi-decision] {sid} 重新拉黑用户（本会话恢复）：{uid}")
                return {**self._blacklist_payload(sid), "changed": True, "uid": uid}
            keep = self._blacklist_keep_ids(uid)
            added = self.user_blacklist.add(uid, name, keep)
            if not added or not getattr(self.user_blacklist, "last_save_ok", True):
                return {"ok": False, "code": "SAVE_FAILED",
                        "detail": "写入黑名单失败，这次拉黑没有保存"}
            logger.info(f"[multi-decision] 拉黑用户 {uid}（{name or '未知名'}），保留历史 {len(keep)} 条")
        else:
            if sid and self.user_blacklist.get(uid) is not None:
                # 单群 / 私聊解除全局拉黑：只记进本会话的解除名单，全局名单不动
                if not self._user_unblock_write(sid, unblock + [uid]):
                    return {"ok": False, "code": "SAVE_FAILED",
                            "detail": "写入单群配置失败，这次解除没有保存"}
                logger.info(f"[multi-decision] {sid} 解除全局拉黑（只对本会话）：{uid}")
                return {**self._blacklist_payload(sid), "changed": True, "uid": uid}
            changed = self.user_blacklist.remove(uid)
            if not changed and not getattr(self.user_blacklist, "last_save_ok", True):
                return {"ok": False, "code": "SAVE_FAILED",
                        "detail": "写入黑名单失败，这次解黑没有保存"}
            logger.info(f"[multi-decision] 解黑用户 {uid}（{'已移除' if changed else '本来就不在表里'}）")
            return {**self._blacklist_payload(sid), "changed": bool(changed), "uid": uid}
        return {**self._blacklist_payload(sid), "changed": True, "uid": uid}

    @_api("GET", "/request-sessions")
    async def api_request_sessions(self):
        """有请求上下文记录的会话列表。"""
        items = self.request_log.list_sessions() if self.request_log else []
        return {"items": items}

    @_api("GET", "/requests")
    async def api_requests(self, sid: str, limit: int = 20, light: int = 0, trim: int = 0):
        """某会话最近几轮的请求摘要。"""
        # v3.66（用户 m19536 第 6 条）：整表计算以前直接跑在事件循环里，线上要重读
        # 100 份 / 14 MB 的请求快照、实测 2.5 秒。页面每 5 秒轮询它一次，等于把服务
        # 反复堵住两秒——用户点「设置」「首页」时任何请求都排在后面，就是「有时卡很久」。
        # 现在两件事：① 整表计算丢进线程，不占事件循环；② 摘要行走 list_rounds_cached，
        # 目录指纹（文件数 + 最新修改时间）没变就直接复用，只有新落一轮才重算。

        evidence = await self._reply_evidence(sid)
        return await asyncio.to_thread(self._requests_payload, sid, limit, "request",
                                       bool(light), bool(trim), evidence)

    async def _session_key_of(self, sid: str) -> str:
        """插件里的 sid（gm:1145141974）→ 框架记忆用的 session_key（人格:gm:1145141974）。"""
        cache = getattr(self, "_sid_key_cache", None)
        if cache is None:
            cache = self._sid_key_cache = {}
        if sid in cache:
            return cache[sid]
        key = ""
        # 一、记忆服务自己列会话（部署版上下文里没有 session_manager，只有 session_mgr）
        mh = getattr(self.ctx, "message_history", None)
        if mh is not None:
            try:
                got = mh.list_sessions()
                if inspect.isawaitable(got):
                    got = await got
                for item in got or []:
                    cand = str(getattr(item, "session_id", "") or "")
                    if cand == sid or cand.endswith(":" + sid):
                        key = cand
                        break
            except Exception:
                key = ""
        # 二、会话管理器（不同版本名字不一样）
        if not key:
            sm = (getattr(self.ctx, "session_mgr", None)
                  or getattr(self.ctx, "session_manager", None))
            if sm is not None:
                try:
                    if compat.has_sessions_kind():
                        got = sm.get_sessions(include_content=False, kind="im")
                    else:
                        got = sm.get_sessions(include_content=False)
                    if inspect.isawaitable(got):
                        got = await got
                    for item in got or []:
                        if (str(getattr(item, "session_type", "")) + ":"
                                + str(getattr(item, "session_id", ""))) == sid:
                            key = str(getattr(item, "sid", "") or "")
                            break
                except Exception:
                    key = ""
        # 三、最后兜底：人格名 + sid（框架的会话键就是 适配器名:类型:id）
        if not key and ":" in sid:
            try:
                name = self._bot_name(sid)
            except Exception:
                name = ""
            if name:
                key = str(name) + ":" + sid
        if key:
            cache[sid] = key
        return key

    async def _reply_evidence(self, sid: str) -> list:
        """框架记忆里这个会话最近发出去的 bot 消息时间（秒），给走势图兜底用。

        只读，30 秒缓存。框架没提供 message_history、或者查询出错，都返回空列表，
        调用方就退回原来的「查不到记录 = missing」。
        """
        now = time.time()
        cache = getattr(self, "_evidence_cache", None)
        if cache is None:
            cache = self._evidence_cache = {}
        hit = cache.get(sid)
        if hit is not None and now - hit[0] < 30.0:
            return hit[1]
        out = []
        try:
            svc = getattr(self.ctx, "message_history", None)
            key = await self._session_key_of(sid)
            if svc is not None and key:
                page = await svc.list_messages(key, limit=200)
                for rec in getattr(page, "messages", None) or []:
                    if str(getattr(rec, "direction", "") or "") != "outgoing":
                        continue
                    try:
                        ts = float(getattr(rec, "timestamp", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    if ts:
                        out.append(ts)
        except Exception:
            logger.debug("[multi-decision] 读框架消息历史失败（走势图兜底跳过）", exc_info=True)
            out = []
        cache[sid] = (now, out)
        return out

    def _mark_reply_state(self, sid: str, rows: list, evidence=None) -> None:
        """给判定「要回」的决策轮标一句：之后那一轮回复到底怎么了。

        用户 m35302：判定要回不等于真的回了——可能模型跑出空输出，也可能那一轮压根
        没跑起来。走势图据此决定这根柱子画不画、写什么：
          replied → 后面那一轮真的发出去了（那根柱子自己会说话，这根不画）
          empty   → 跑到了、没产出能发的内容（空回复）
          failed  → 跑出错
          missing → 过了宽限也没有对应的回复请求，分不清失败还是空输出，按失败算
          pending → 刚判完、回复还在路上，先别急着画成失败

        evidence 是框架记忆里这个会话最近发出的 bot 消息时间（秒）。用户 m18435
        第 1 条：插件热重载会把在飞的那一轮冲掉，那一轮永远没有请求记录，可回复
        明明发出去了——页面上却盖一根红柱子说失败。记录找不到时拿它兜底。
        """
        if not rows or self.request_log is None:
            return
        if not any(bool(it.get("decision_reply")) for it in rows):
            return
        try:
            # 回复表份数与快照保留份数独立；查归属必须覆盖仍在展示的历史。
            # 只读轻量摘要，避免为了判柱子颜色重算整批请求的 trace/前缀。
            depth = max(200, self._snap_keep_of(sid), self._snap_show_of(sid),
                        self._snap_total_of(sid))
            all_rows = self._cached_rows(sid, depth, light=True)
        except Exception:
            logger.debug("[multi-decision] 回复轮索引失败", exc_info=True)
            return
        cand = []
        scanned_ts = []
        decisions = set()
        for it in all_rows or []:
            if not isinstance(it, dict):
                continue
            try:
                at = float(it.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if at > 0:
                scanned_ts.append(at)
                if bool(it.get("decision_only")):
                    decisions.add(at)
            if bool(it.get("decision_only")) or bool(it.get("trim_only")):
                continue
            if str(it.get("outcome") or "") == "trim":
                continue
            # 用户 m17492 图5/图6：通知回调轮不是「对上一轮决策的回复」，
            # 除非它自己真的回了话，否则不能拿来当命中。
            if bool(it.get("notice_only")) and not bool(it.get("replied")):
                continue
            if not bool(it.get("decision_passed")):
                continue
            try:
                cand.append((float(it.get("ts") or 0), it))
            except (TypeError, ValueError):
                continue
        cand.sort(key=lambda x: x[0])
        # 只有找不到后续回复和框架发言证据时，才用保留边界判断是否未知。
        # 决策往往比对应的首条回复早几秒，不能在匹配之前就把它排除。
        oldest = min(scanned_ts) if scanned_ts else 0.0
        decisions = sorted(decisions)
        now = time.time()
        for it in rows:
            if not bool(it.get("decision_reply")):
                continue
            try:
                t0 = float(it.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            # 下一次判定之后的请求归下一轮，不能让两次决策认领同一回复。
            next_at = next((at for at in decisions if at > t0), 0.0)
            hits = [r1 for t1, r1 in cand
                    if t0 < t1 <= t0 + 300 and (not next_at or t1 < next_at)]
            if len(hits) > 1:
                it["reply_state"] = "unknown"
                continue
            hit = hits[0] if hits else None
            if hit is None:
                # 框架发言兜底也遵守相邻决策边界；明确发出了就不必等宽限期。
                if any(t0 < t1 <= t0 + 300 and (not next_at or t1 < next_at)
                       for t1 in (evidence or ())):
                    it["reply_state"] = "replied"
                    continue
                if (now - t0) < 90:
                    it["reply_state"] = "pending"
                    continue
                it["reply_state"] = "unknown" if not oldest or t0 < oldest else "missing"
                continue
            out = str(hit.get("outcome") or "")
            if bool(hit.get("replied")) or out == "replied":
                it["reply_state"] = "replied"
            elif hit.get("failed") or out in ("failed", "error") or hit.get("error"):
                it["reply_state"] = "failed"
            else:
                it["reply_state"] = "empty"

    def _cached_rows(self, sid: str, limit: int, light: bool = False) -> list:
        """取摘要行（v4.80）。轻量路优先；只认两个参数的假 log（测试夹具）自动回退。"""
        log = getattr(self, "request_log", None)
        getter = getattr(log, "list_rounds_cached", None) if log is not None else None
        if not callable(getter):
            return []
        if light:
            try:
                return list(getter(sid, limit, light=True) or [])
            except TypeError:
                pass
        return list(getter(sid, limit) or [])

    def _snap_keep_of(self, sid: str = "") -> int:
        """「窗口快照」的份数上限（v4.90，用户 m01133 第 2 条）。

        回复轮、决策轮、裁切三种快照合成一类之后，盘上总共只留这么多份，
        走势图也只画这么多根——以前三路各按各的上限取，画出来三百多根，
        跟缓存表里那一行永远对不上。0 = 不限。
        """
        try:
            return max(0, int(self.eff("windows_keep", sid) or 0))
        except Exception:
            return 0

    def _snap_total_of(self, sid: str = "") -> int:
        """这个会话盘上还剩多少份窗口快照（v4.90，用户 m01875 第 1 条）。

        走势图标题要跟缓存表那一行「窗口快照」对得上，就得数同一批文件。前端把
        回复轮、决策轮、裁切标记拼起来数，总会因为「判定要回的决策轮不单独成柱」
        少掉几十份，所以这里直接数目录。
        """
        log = getattr(self, "request_log", None)
        fn = getattr(log, "snap_count", None) if log is not None else None
        if not callable(fn):
            return 0
        try:
            return max(0, int(fn(sid) or 0))
        except Exception:
            return 0

    def _snap_show_of(self, sid: str = "") -> int:
        """「窗口快照」在页面上显示多少条（v4.90，用户 m01875 第 1 条）。

        盘上留多少份由 windows_keep 决定，这里只管走势图和快照列表画多少根——
        两个数分开之后，上限调到 300 也不会让页面一下子长出一百根柱子。0 = 不限。
        """
        try:
            return max(0, int(self.eff("window_show", sid) or 0))
        except Exception:
            return 0

    def _requests_payload(self, sid: str, limit: int = 20, kind: str = "request",
                          light: bool = False, with_trims: bool = False,
                          evidence=None) -> dict:
        """api_requests 的计算体。只读盘与内存，放线程里跑是安全的。"""
        # 用户 m13250 第 2 条：请求页标题要跟着「保留请求数」走，所以 limit<=0 就按保留数给满，
        # 顺便把 keep 一起回给前端（标题写「最近 N 次请求（上限 M 次）」用）。
        # v4.90（发版前审计）：回复记录页的条数上限原来只吃全局 trace_keep，
        # 私聊的 trace_keep_dm 存进去没人读。改成按会话取，私聊那份才生效。
        try:
            keep = int(self.eff("trace_keep", sid) or 0) or self.request_log_rounds
        except Exception:
            keep = self.request_log_rounds
        # v4.80：limit<=0 = 「按上限给满」。这个意图要在进分支前记下来，否则先被
        # 回复上限（100）顶掉，决策分支里就再也看不到「要满」了。
        _wants_all = limit is None or limit <= 0
        if _wants_all:
            # 轻量趋势按窗口快照显示上限取满，冷启动不依赖前端先取得 cap。
            # 回复/决策表的非轻量列表仍遵守各自的记录显示上限。
            if light and (with_trims or str(kind or "request") == "decision"):
                limit = self._snap_show_of(sid) or self._snap_keep_of(sid) or keep
            else:
                limit = keep
        # v3.91（用户 m05749）：决策记录与回复记录同一个目录、靠 decision_only 分账，
        # 所以多取几倍再筛，否则筛完只剩几条。决策项不走请求那一套（没有轨迹 / 重试链）。
        if str(kind or "request") == "decision":
            # v4.80（用户 2026-11-07 第 1 条）：决策快照上限默认是回复的 3 倍，
            # 页面的 keep 与取数深度都要按决策那一份走，否则永远只看得到前 100 条。
            try:
                _dec_keep = int(self.eff("decision_keep", sid) or 0)
            except Exception:
                _dec_keep = 0
            if _dec_keep > 0:
                keep = _dec_keep
                if _wants_all and not light:
                    limit = keep
            take = max(int(limit) * 4, 120)
            rows = self._cached_rows(sid, take, light=bool(light))
            # v3.94（用户 m08398 图7/图8）：决策行照回复记录的列给数据——
            # 真实模型名（配置键换成「供应商 / 模型名」）、token（入/出）、缓存命中。
            labels = {}
            # 用户 m15537：渠道拆成独立一列，模型列只留模型名，这里分开存三份。
            channels = {}
            models_only = {}
            fast_label = ""
            try:
                for m in self._decision_models():
                    val = str(m.get("value") or "")
                    if val:
                        labels[val] = str(m.get("label") or "")
                        channels[val] = str(m.get("provider") or "")
                        models_only[val] = str(m.get("model") or "")
                        if m.get("fast"):
                            fast_label = val
                # 用户 m06706：JEV 用的是 decision 类模型，不在上面那份 llm/vlm 清单里，
                # 不补进来记录页就只剩一串 pid:mid。
                try:
                    for m in self._jev_models():
                        val = str(m.get("value") or "")
                        if val and val not in labels:
                            labels[val] = str(m.get("label") or "")
                            channels[val] = str(m.get("provider") or "")
                            models_only[val] = str(m.get("model") or "")
                except Exception:
                    logger.debug("[multi-decision] JEV 模型名映射失败", exc_info=True)
            except Exception:
                logger.debug("[multi-decision] 决策模型名映射失败", exc_info=True)
            out = []
            for it in rows:
                if not bool(it.get("decision_only")):
                    continue
                it["outcome"] = "decision"
                # 2026-10-31：决策失败（上游 5xx、超时、没按 JSON 回…）以前在这里被
                # 一刀清成 False，结论列就成了「要回」，行尾还按图片退回标，看起来像
                # 一次正常判定。失败/报错必须原样带给页面。
                it["failed"] = bool(it.get("failed"))
                it["error"] = str(it.get("error") or "")
                it["elapsed"] = it.get("decision_elapsed")
                key = str(it.get("decision_model") or "")
                if not key or key in ("fast", "default"):
                    key = fast_label or key
                # 用户 m06706：JEV 转交时 decision_model 是「JEV 模型 → 普通决策模型」，
                # 两段分别换成真名，别让页面显示两串 provider:model。
                _parts = [p.strip() for p in key.split("→")] if key else []
                # 用户 m15537：模型列只留模型名（以前是「供应商 / 模型名」拼在一起）。
                it["model_label"] = (" → ".join(models_only.get(p) or labels.get(p) or (p or "跟随默认")
                                               for p in _parts)
                                     if _parts else "跟随默认")
                # 用户 m00375 第 8 条：走第三方接口时决策模型只有模型名，渠道没标。
                # 用户 m15537：渠道列独立后这里给供应商实例名；查不到才退回「自定义接口」。
                _chans = []
                for _p in (_parts or []):
                    _c = channels.get(_p) or ""
                    # 用户 m01898：第三方接口的页名（比如「阿里」）也要显示出来，
                    # 以前这里一律写「自定义接口」。
                    if not _c and str(_p).startswith("custom:"):
                        _c = self._custom_api_label(str(_p).partition(":")[2])
                    if _c:
                        _chans.append(_c)
                _chan = " → ".join(_chans)
                if not _chan and str(it.get("decision_backend") or "") == "custom":
                    _chan = "自定义接口"
                it["model_channel"] = _chan
                in_tok = int(it.get("input_tokens") or 0)
                cac = int(it.get("cached_tokens") or 0)
                it["tokens"] = {
                    "input": in_tok,
                    "output": int(it.get("output_tokens") or 0),
                    "cached": cac,
                    "cache_hit": (round(cac * 100.0 / in_tok, 1) if in_tok else None),
                }
                # v4.01（用户 m11166 图3）：决策表的「命中预估」以前整列都是「—」，
                # 因为只有回复分支算过它。决策项一样带 prefix_reuse_percent 与
                # gap_from_previous，直接套同一套估算就行。
                try:
                    it["hit_estimate"] = self._hit_estimate(it)
                except Exception:
                    logger.debug("[multi-decision] 决策命中预估失败", exc_info=True)
                    it["hit_estimate"] = None
                # v5.x（用户 m12891）：这家模型从来没回报过缓存，实测那格就别写 0%。
                if self._mark_cache_reported(it) is False:
                    it["tokens"]["cache_hit"] = None
                out.append(it)
            # 用户 m35302：判定要回的那一轮，先看清后面那一轮回复到底有没有发生，
            # 走势图据此决定画不画这根柱子。
            self._mark_reply_state(sid, out, evidence)
            # v4.13（用户 m18728 第 1 条）：决策分支以前直接 return，没走 _attach_round_trims，
            # trims_after 这个字段压根不存在——走势图上决策柱永远拿不到裁切标记，
            # 判要回/判不回之后真正发生的裁切都没法标在决策柱上。这里补上同一套绑定。
            picked = out[:int(limit)]
            self._attach_round_trims(sid, picked)
            _stamp_snap_ids(picked)
            if light:
                # 走势图只读这些字段；其余（图片、轨迹、token、模型名、payload 估算等）
                # 一律不传，避免每 5 秒把几百 KB 的明细重新拉一遍。
                keep_keys = {
                    "name", "ts", "event_id", "outcome", "failed", "error",
                    "replied", "reply_state", "reply_unwrapped",
                    "decision_reply", "decision_reason",
                    "retry_history", "trims_after", "trim_only",
                    "window_size", "window_counted", "window_total",
                    "window_user", "window_pending_count", "window_pending_user",
                    "trim_before", "trim_after", "trim_pending_count",
                    "trim_counted_before", "trim_counted_after",
                    "trim_user_total_before", "trim_user_total_after",
                    "trim_trigger", "snap_id",
                }
                for it in picked:
                    for key in list(it.keys()):
                        if key not in keep_keys:
                            it.pop(key, None)
            # v4.9x（用户 m02038 第 3 条）：列表页不画决策图片、也不画轨迹，这两项占了
            # 整包一半的体积（100 行 660KB → 约 330KB，点开决策记录要等的那一下主要
            # 花在传它和解析它上）。详情走 /request 单独读盘，不受影响。
            for it in picked:
                it.pop("decision_images", None)
                it.pop("_trace", None)
            return {"items": picked, "keep": keep, "kind": "decision",
                    "snap_keep": self._snap_keep_of(sid),
                    "snap_show": self._snap_show_of(sid),
                    "snap_total": self._snap_total_of(sid)}
        # v3.91：决策快照跟请求快照同一个目录，回复记录里不该冒出「进阶决策」那些行，
        # 所以多取一些、把 decision_only 滤掉再截断。
        _with_trims = bool(with_trims)
        # v4.9x（用户 m11086）：决策快照与回复快照同目录，决策轮比回复轮多好几倍。
        # 以前固定按 limit*2 取文件，筛掉决策行后常常只剩三分之一——页头写「最近 100 次」，
        # 表里只有 33 行，往前的回复记录看着像「没了」。起始深度按 4 倍给，还不够就翻倍再取，
        # 直到凑够 _bar_cap 行或把目录读空（list_rounds 自己封顶 1000 份）。
        _take = max(int(limit) * 4, 400)
        # v4.80（用户 2026-11-07 第 3 条）：走势图要的是「盘上还留着的每一次回复/裁切快照」，
        # 回复记录页那套 100 行截断不能套到它身上——盘上还有的快照画不出柱子，两次裁切
        # 中间就像是断了一大片。这里按「回复上限 + 裁切上限（= 窗口快照上限）」给满。
        _bar_cap = int(limit)
        if _with_trims and light:
            try:
                _trim_cap = int(self.eff("windows_keep", sid) or 0)
            except Exception:
                _trim_cap = 0
            _bar_cap = max(int(limit), 1) + max(_trim_cap, 0)
            _take = max(_take, _bar_cap * 3, 1000)
        while True:
            _rows = self._cached_rows(sid, _take, light=bool(light))
            # v4.07（用户 m13302）：窗口裁切快照不是一次真实请求——裁切发生在请求组装之前、
            # 没有模型调用，混在回复记录里既占行也把「本页合计」的命中率带偏。
            # v4.73（用户 m02417）：可这一刀也把裁切快照从走势图上抹掉了——走势图的柱子只认
            # 这份列表，空闲巡检裁的那一次没有对应轮次，橙色只能贴回上一轮，看着像「没生成快照」。
            # 现在走势图那条路带 trim=1 单独要它们（各自成一根青柱，点开是裁前那一份完整 ②），
            # 回复记录列表仍按上面的口径不给。
            items = [it for it in _rows
                     if not bool(it.get("decision_only"))
                     and (_with_trims or (not bool(it.get("trim_only"))
                                          and str(it.get("outcome") or "") != "trim"))]
            # v4.9x（用户 m11086）：带 trim 时裁切行也占名额，可它们稍后会被「已经画成
            # 柱子」那一刀剔掉——先按「上限 + 裁切行数」截，剔完才够 limit 行回复。
            if _with_trims:
                _n_trim = sum(1 for it in items if self._is_trim_row(it))
                items = items[: _bar_cap + _n_trim]
            else:
                items = items[:_bar_cap]
            if len(items) >= _bar_cap or len(_rows) < _take:
                break
            _take *= 2
        # 列表里就把 token、用时与命中带上，不用点开每一轮才知道花了多少
        for it in items:
            # v4.79（2026-11-07 性能）：裁切快照不是一次请求，没有轨迹、没有重试、也没有
            # token。以前这一行照样走下面的 _trace_of 回落，而裁切记录恰恰是唯一不带
            # trace 的那类文件 → read_by_event 把整个会话目录（200 份 / 20MB）重扫一遍，
            # 单行 1.3~1.5 秒；走势图每 5 秒带 trim=1 拉一次，10 份裁切就是十几秒，
            # 还把线程池占满，连带 /window、/decision-rounds 排队。这里直接按裁切行收尾。
            if self._is_trim_row(it):
                it.pop("_trace", None)
                it["failed"] = False
                it["error"] = ""
                it["replied"] = False
                it["elapsed"] = None
                it["elapsed_trigger"] = None
                it["elapsed_debounce"] = None
                it["elapsed_decision"] = None
                it["elapsed_send"] = None
                it["send_count"] = None
                it["elapsed_llm"] = None
                it["steps"] = 0
                continue
            if light:
                # v4.80（2026-11-07 性能）：轻量行走增量缓存，没有 _trace；走势图
                # 不读轨迹，这里绝不能回落 _trace_of —— 那会把整个目录重扫一遍。
                it.pop("_trace", None)
                it["retry_history"] = self._retry_summary(sid, it)
                it["retry_of"] = self._retry_link(sid, it)
                continue
            # v3.66：轨迹跟请求快照同文件，list_rounds 已顺手带出；原来每行还要
            # 按 event_id 把整个目录重扫一遍（read_by_event 最坏 100 份）。
            trace = it.pop("_trace", None)
            if isinstance(trace, dict) and self.tracer is not None:
                try:
                    trace = self.tracer._decorate(trace)
                except Exception:
                    logger.debug("[multi-decision] 轨迹装饰失败", exc_info=True)
            if not isinstance(trace, dict):
                trace = self._trace_of(sid, it)
            it.update(self._round_outcome(it, trace))
            it["retry_history"] = self._retry_summary(sid, it)
            # 这一行如果本身就是「重试轮」，指出它在补哪一轮（用户 2026-10-24 图4）
            it["retry_of"] = self._retry_link(sid, it)
            # v3.19（用户 m41511 第 3 条）：「用时」列直接用轨迹里记的 elapsed（秒）
            el = (trace or {}).get("elapsed")
            it["elapsed"] = el if isinstance(el, (int, float)) else None
            # v3.51（用户 m07385 第 7 条）：用时按「触发 → 请求结束」算，这里同时给出
            # 拆解，前端悬停能看到「防抖等了 X 秒 · 模型 Y 秒」。老轨迹没有就是 None。
            et = (trace or {}).get("elapsed_trigger")
            it["elapsed_trigger"] = et if isinstance(et, (int, float)) else None
            # 用户 m06046 图4：这里以前写的是 elapsed_trigger，把「决策那几秒」也裹进
            # 防抖里，数是错的。用户 m13212 又要把防抖单独显示出来，所以只给纯抖动那段：
            # 优先用引擎量到的 debounce_fixed（触发 → 真正放行），没有就退回
            # debounce - decision_elapsed（轨迹里的 debounce = 请求开始 - 触发时刻）。
            # 两个都取不到、或者算出来接近 0，就仍然是 None（前端画「—」）。
            try:
                _fix = float((trace or {}).get("debounce_fixed") or 0.0)
                _raw = float((trace or {}).get("debounce") or 0.0)
                _dcv = float((trace or {}).get("decision_elapsed") or 0.0)
                # 用户 m13212：链路是「防抖 → 决策 → 回复」，走过决策的那一轮，
                # 防抖只记在决策行，回复行一律留空（显示「—」）；普通模式（没走决策）
                # 才把「触发 → 请求开始」这段等待记在回复行。
                if _dcv > 0:
                    _dbo = 0.0
                elif _fix > 0:
                    _dbo = _fix
                else:
                    _dbo = _raw
                it["elapsed_debounce"] = round(_dbo, 2) if _dbo >= 0.05 else None
            except (TypeError, ValueError):
                it["elapsed_debounce"] = None
            # 用户 m05453：这一轮决策花了多久（判要回才有值，必回那一路是 0）。
            dec = (trace or {}).get("decision_elapsed")
            it["elapsed_decision"] = dec if isinstance(dec, (int, float)) else None
            # 用户 m05691 第 4 条：回复那一段里，有多少秒花在「把几条气泡逐条发出去」上。
            snd = (trace or {}).get("send_elapsed")
            it["elapsed_send"] = snd if isinstance(snd, (int, float)) else None
            cnt = (trace or {}).get("send_count")
            it["send_count"] = cnt if isinstance(cnt, int) else None
            llt = (trace or {}).get("llm_elapsed_seconds")
            it["elapsed_llm"] = llt if isinstance(llt, (int, float)) else None
            tot = (trace or {}).get("totals")
            if tot:
                it["tokens"] = {
                    "input": tot.get("input_tokens") or 0,
                    "output": tot.get("output_tokens") or 0,
                    "cached": tot.get("cached_tokens") or 0,
                    "cache_hit": tot.get("cache_hit"),
                }
            # v3.58（用户 m16875 图4）：token 是这一轮所有步骤的合计——多步（有工具调用）时
            # 第二步会把前面的上下文再发一遍，所以 token 会翻倍而字符不涨，
            # 「字符/token」跨轮比就没有意义了。这里把步数与首步 input 带下去，
            # 前端按首步算比率，并在行里写出「N 步」。
            steps = (trace or {}).get("steps") or []
            it["steps"] = len(steps)
            # v3.59（用户 m17212 第 1 条）：实测命中是整轮各步合计，而「命中预估」是首步跨轮前缀，
            # 多步轮第二步会把前面整段再发一遍，两列天然不同口径。这里把首步命中一起带下去，
            # 前端就能摆成「整轮 X%（首步 Y%）」，单步轮不受影响。
            # 两列都以「第一条 input>0 的步」为准，所以一次扫描同时取出来。
            first_in = 0
            first_hit = None
            for _st in steps:
                try:
                    _v = int(_st.get("input_tokens") or 0)
                except (TypeError, ValueError):
                    _v = 0
                if _v <= 0:
                    continue
                first_in = _v
                try:
                    _c2 = int(_st.get("cached_tokens") or 0)
                except (TypeError, ValueError):
                    _c2 = 0
                first_hit = round(_c2 * 100.0 / _v, 1)
                break
            it["first_input"] = first_in
            it["first_cache_hit"] = first_hit
            # v3.60（用户 task-8 第 4 项）：多步轮的「实测命中」是各步合计，光给整轮数字
            # 看不出是哪一步把命中拉高/拉低的。这里把每步的 token 与命中率一并带下去，
            # 前端在列里按步标注「1步 45.2% 2步 12.0%」。只读拼接，不改轨迹文件；
            # 最多前 8 步（再多就是工具连环调用，标出来也没人看），其余步只体现在 it["steps"]。
            step_stats = []
            for _st3 in steps[:8]:
                if not isinstance(_st3, dict):
                    step_stats.append({"in": 0, "out": 0, "cached": 0, "hit": None})
                    continue
                try:
                    _in3 = int(_st3.get("input_tokens") or 0)
                except (TypeError, ValueError):
                    _in3 = 0
                try:
                    _out3 = int(_st3.get("output_tokens") or 0)
                except (TypeError, ValueError):
                    _out3 = 0
                try:
                    _c3 = int(_st3.get("cached_tokens") or 0)
                except (TypeError, ValueError):
                    _c3 = 0
                step_stats.append({
                    "in": _in3,
                    "out": _out3,
                    "cached": _c3,
                    # 命中率口径和整轮一致：cached / input，in=0 时没有分母，给 None
                    "hit": round(_c3 * 100.0 / _in3, 1) if _in3 > 0 else None,
                })
            it["step_stats"] = step_stats
            # v4.87（用户 m13442 第 2 条）：中转站（反重力 Gemini）不回报 cached_tokens，
            # 上报的 input 只是「没命中的那一截」，所以实测命中列常年 0%，看着像一次都没命中。
            # 这里按「完整 prompt ≈ 文本 token + 图片张数 × 1032」反推：
            #   第 1 步完整 = prompt_tokens_est，命中 = 1 − 上报 ÷ 完整；
            #   第 k 步完整 = 上一步完整 + 本步上报（本步上报就是新增的那一截）。
            # 只在「整轮 cached 全是 0」且「上报明显小于完整（< 75%）」时才认，否则说明
            # 这一轮真没命中、或者 provider 报的就是完整 input（DeepSeek 那种），照旧写 0。
            # 反推值单独放 hit_est，不动实测的 hit。
            _full = int(it.get("prompt_tokens_est") or 0)
            if _full > 0 and step_stats:
                # 反推口径统一在 state/tracer.py（价目页算钱用的是同一份），这里只把结果
                # 摊到每一步：hit_est 是反推命中率，full_est 是这一步的完整 prompt。
                _guess = infer_cached_steps(_full, step_stats, infer_gate(it))
                if _guess:
                    for _sp, _g in zip(step_stats, _guess):
                        if not isinstance(_sp, dict) or not _g:
                            continue
                        _sp["full_est"] = int(_g["full"])
                        _sp["hit_est"] = round(
                            _g["cached"] * 100.0 / _g["full"], 1) if _g["full"] > 0 else 0.0
                    it["hit_inferred"] = True
        # 原错误那一轮补一句「后来重试了几次、成没成、回复落在哪一轮」（用户 2026-10-24 图5）。
        # 只读拼接：请求记录文件一个字节都不改，靠 retry_history.json 里持久化的链。
        for it in items:
            it["retry_outcome"] = self._retry_outcome_of(it, items)
        # 用户 m05205 第 3 条：重试轮要自己说清是第几次重试，否则两行都只写「重试轮」分不清。
        for it in items:
            link = it.get("retry_of")
            if isinstance(link, dict):
                link["attempt_index"] = self._retry_attempt_index(it, items)
        # 上一轮实测命中只放进悬停提示，不再参与计算（v3.19 离线评估：参与反而更差）
        for idx, it in enumerate(items):
            if light:
                # v4.80：轻量路没有前缀列，命中预估需要它，整块跳过。
                continue
            prev_it = items[idx + 1] if idx + 1 < len(items) else None
            prev = None
            if prev_it is not None:
                prev = ((prev_it.get("tokens") or {}).get("cache_hit"))
            it["prev_cache_hit"] = prev
            it["hit_estimate"] = self._hit_estimate(it)
            # v5.x（用户 m12891）：这一轮算实测还是算推算，按记录自己冻结。
            self._mark_cache_reported(it)
            # v3.60（用户 m18059 第 6 条）：逐步命中预估。
            # 第 1 步的前缀就是整轮的公共前缀，直接沿用整轮的 hit_estimate（含间隔/图片校准）；
            # 第 k 步（k>0）的前缀恰好是上一步的整段输入，所以 上一步 in ÷ 本步 in 就是这一步
            # 能命中的比例。任一步 in 拿不到（=0）就没有分母，给 None，前端回退 first_input/
            # first_cache_hit。只读拼接，不改轨迹文件。
            _stats = it.get("step_stats")
            if isinstance(_stats, list) and _stats:
                _prev_in = 0
                for _k, _sp in enumerate(_stats):
                    if not isinstance(_sp, dict):
                        _prev_in = 0
                        continue
                    try:
                        _this_in = int(_sp.get("in") or 0)
                    except (TypeError, ValueError):
                        _this_in = 0
                    _est = _sp.get("hit_est")
                    if _est is not None:
                        # v4.87：反推出来的命中率比「上一步 in ÷ 本步 in」准——中转站只报
                        # 未命中那截时，后者会算出 845% 这种数。
                        _sp["est"] = _est
                    elif _k == 0:
                        _sp["est"] = it.get("hit_estimate")
                    elif _this_in > _prev_in > 0:
                        # 用户 m15537：中转站只报「未命中那截」时，这一步的 in 会比上一步还小，
                        # 「上一步整段输入就是这一步的前缀」这个前提不成立，比例算出来是假的
                        # （以前会算出 845.6%）。这种步留空，前端按有值的步加权平均。
                        _sp["est"] = round(_prev_in * 100.0 / _this_in, 1)
                    else:
                        _sp["est"] = None
                    _prev_in = _this_in
                # v4.9x（用户 m02038 第 2 条）：多步轮的整轮命中预估要按各步加权。
                # 单步轮 hit_estimate 就是前缀复用比例；2 步以上时第 2 步起几乎整段命中，
                # 只写第 1 步的比例会系统性低估（3 步轮实测 63.8%，页面写 43.6%）。
                # 权重用 full_est（这一步完整 prompt 的 token 数），没有就退回 in。
                if len(_stats) > 1:
                    _wsum = 0.0
                    _vsum = 0.0
                    for _sp in _stats:
                        if not isinstance(_sp, dict):
                            continue
                        _v = _sp.get("est")
                        if _v is None:
                            continue
                        try:
                            _w = float(_sp.get("full_est") or _sp.get("in") or 0)
                        except (TypeError, ValueError):
                            _w = 0.0
                        if _w <= 0:
                            continue
                        _wsum += _w
                        _vsum += float(_v) * _w
                    if _wsum > 0:
                        it["hit_estimate_single"] = it.get("hit_estimate")
                        it["hit_estimate"] = round(_vsum / _wsum, 1)
            # 上游没回报缓存时，实测那一格没有意义（写 0% 会被当成一次都没命中）。
            # 反推得出来的（hit_est）照旧显示 ≈，反推不出来的留空，前端画「—」。
            if it.get("cache_reported") is False:
                it["hit_inferred"] = True
                _tk = it.get("tokens")
                if isinstance(_tk, dict):
                    _tk = dict(_tk)
                    _tk["cache_hit"] = None
                    it["tokens"] = _tk
                it["first_cache_hit"] = None
            # v3.22 第 5 条：公式不动，但把「这一轮为什么低/高」算成一句话给悬停用
            it["hit_note"] = self._hit_note(it, prev_it)
        self._attach_round_trims(sid, items)
        _stamp_snap_ids(items)
        # v4.74（用户 m02687）/ v4.75（用户 m02928）：同一刀别画两根柱子——橙色已经挂在
        # 「触发裁切」那根柱子上，写了这次裁了多少；裁切快照自己再占一根青柱，悬停里的
        # 数字一模一样，看着就是重复。被某一轮认领掉的裁切不再单独成柱；没有轮次认领的
        # （走出走势图那段时间的裁切）照旧一根。
        # 认领要看两套轮次：判要回之后才落的那一刀，橙标挂在决策轮上（api_decision_rounds
        # 也调 _attach_round_trims），只扫本页的请求轮会把它漏掉，橙色柱子后面还跟一根
        # 一模一样的青柱。
        if _with_trims:
            try:
                _owned_cuts = self._drawn_cuts(sid, items, limit)
            except Exception:
                logger.debug("[multi-decision] 取已有橙标的裁切失败", exc_info=True)
                _owned_cuts = []
            if _owned_cuts:
                def _cut_drawn_as_bar(_row) -> bool:
                    _name = str(_row.get("name") or "")
                    _ts = float(_row.get("ts") or 0.0)
                    _cb = int(_row.get("trim_counted_before") or 0)
                    for _cut in _owned_cuts:
                        _sn = str(_cut.get("snap_name") or "")
                        if _sn:
                            if _name and _sn == _name:
                                return True
                            continue
                        # 老记录没有 snap_name：按时间与裁前条数配对
                        _cts = float(_cut.get("ts") or 0.0)
                        _ccb = int(_cut.get("counted_before") or 0)
                        if _name and abs(_cts - _ts) <= 5.0 and (not _cb or _ccb == _cb):
                            return True
                    return False
                items = [it for it in items
                         if not (self._is_trim_row(it) and _cut_drawn_as_bar(it))]
        # 用户 m00375 第 8 条：v4.85 之前写的快照没有 reply_provider，回复记录里
        # 同一列就会一半有渠道一半没有。这里按模型名回查一次供应商实例名补上；
        # 查不到就留空，页面照旧只显示模型名，绝不编一个。
        if any(not str(it.get("reply_provider") or "").strip()
               and str(it.get("reply_model") or "").strip() for it in items):
            try:
                _labels = self.tracer.provider_label_map()
            except Exception:
                _labels = {}
            if _labels:
                for it in items:
                    if str(it.get("reply_provider") or "").strip():
                        continue
                    _rm = str(it.get("reply_model") or "").strip()
                    if _rm and _labels.get(_rm):
                        it["reply_provider"] = _labels[_rm]
        if light:
            # 走势图只要这些字段；回复记录页拉全量时再带 messages/raw 等大字段。
            # reply_model / reply_channel 是用户 2026-11-07 要的「这一轮用的模型与
            # 渠道」，两个短字符串，light 也带上，免得两条路拿到的行长得不一样。
            keep_keys = {
                "name", "ts", "event_id", "outcome", "failed", "error",
                "replied", "reply_state", "reply_unwrapped",
                "decision_reply", "decision_reason",
                "retry_history", "trims_after", "trim_only",
                "window_size", "window_counted", "window_total",
                "window_user", "window_pending_count", "window_pending_user",
                "trim_before", "trim_after", "trim_pending_count",
                "trim_counted_before", "trim_counted_after",
                "trim_user_total_before", "trim_user_total_after",
                "trim_trigger", "snap_id",
                "reply_model", "reply_provider", "reply_channel", "reply_text",
            }
            for it in items:
                for key in list(it.keys()):
                    if key not in keep_keys:
                        it.pop(key, None)
        return {"items": items, "sid": sid, "keep": keep,
                "snap_keep": self._snap_keep_of(sid),
                "snap_show": self._snap_show_of(sid),
                "snap_total": self._snap_total_of(sid)}

    def _cache_silent_models(self) -> set:
        """整本账里从来没回报过缓存的模型（v5.x 用户 m12891）。

        「这家不回报 cached_tokens」以前是按「今日 / 最近 24 小时」的窗口现算的
        （state/tracer.py 的 _silent_cache_models：窗口里连 3 轮 cached 全是 0 就算），
        切模型之后旧模型掉出窗口，同一批老记录就会从「推算」翻成「实测 0%」。
        现在改读整本用量账：一个模型要么报过、要么从没报过，跟当前用哪家、
        窗口里还剩谁都没关系。新记录自己带 cache_reported，根本不查这里。
        """
        now = time.time()
        cached = getattr(self, "_silent_cache_cache", None)
        if cached and (now - cached[0]) < 60.0:
            return cached[1]
        out = set()
        path = None
        tracer = getattr(self, "tracer", None)
        if tracer is not None:
            path = getattr(tracer, "ledger", None)
        if path is None:
            path = Path(self.store.dir).parent / "stats" / "tokens.jsonl"
        try:
            seen = {}
            with Path(path).open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    model = str(row.get("model") or "")
                    if not model:
                        continue
                    try:
                        _in = int(row.get("input") or 0)
                        _c = int(row.get("cached") or 0)
                    except (TypeError, ValueError):
                        continue
                    if _in <= 0:
                        continue
                    total, hit = seen.get(model, (0, 0))
                    seen[model] = (total + 1, hit + (1 if _c > 0 else 0))
            for model, (total, hit) in seen.items():
                if total >= 3 and hit == 0:
                    out.add(model)
        except Exception:
            logger.debug("[multi-decision] 读 tokens.jsonl 判不回报缓存的模型失败", exc_info=True)
        self._silent_cache_cache = (now, out)
        return out

    @staticmethod
    def _row_model_of(item: dict) -> str:
        """这一行是哪家模型跑的：回复轮看 reply_model，决策轮看 decision_model。"""
        for key in ("reply_model", "decision_model", "model"):
            val = item.get(key)
            if val:
                return str(val)
        return ""

    def _mark_cache_reported(self, it: dict):
        """这一轮按实测还是按推算，钉在记录上（v5.x 用户 m12891）。

        新记录落盘时就带了 cache_reported（上游到底给没给 cached_tokens）；
        老记录没这个字段，退回「整本账里这家模型有没有报过」——不再看时间窗口，
        所以切换模型之后，老记录原来怎么算还怎么算，新记录按新模型走。

        返回 True / False；没真正发出请求（裁切快照、失败轮）返回 None——那种行
        谈不上「上游回报没回报」，命中列本来就该是「—」。
        """
        _tk = it.get("tokens")
        if not isinstance(_tk, dict) or not _tk.get("input"):
            it["cache_reported"] = None
            return None
        rep = it.get("cache_reported")
        if rep is None:
            rep = self._row_model_of(it) not in self._cache_silent_models()
        it["cache_reported"] = bool(rep)
        if not rep:
            it["cache_unreported"] = True
        return bool(rep)

    @staticmethod
    def _hit_estimate_with_images(item: dict, base=None):
        """含图轮的命中预估：文字按字符前缀比例，图片按「见过几张」折算（v3.53 用户 m08226 第 3 条）。

        114 轮真实记录（带图 69 轮、带新图 31 轮）离线比过几套公式，MAE（pp）：
          只用字符比例 9.36 ｜ 上一版「文字×K + 图片按占比」全替换 11.1 ｜
          本版「文字比例 × 文字占比 + 图片占比 × 已见比例」8.72（带新图 31 轮）、
          多图且多张新图 14 轮 7.59（旧口径 9.37）。
        上一版之所以更差：它把整轮都换成 token 口径，文字部分也跟着漂了；本版只把
        「图片占 input 的那一份」拆出来，文字那份继续用字符比例，因此纯文本轮完全不变。

        返回 None 表示这轮不适用（没新图 / 没图 / 没拿到真实 input_tokens / 文字估计
        已经吃满 input），调用方继续用字符前缀比例。
        """
        try:
            b = float(base if base is not None else item.get("prefix_reuse_percent"))
            tt = float(item.get("prefix_reuse_total_tokens") or 0)
            inp = float((item.get("tokens") or {}).get("input") or 0)
            imgs = int(item.get("image_count") or 0)
            new_imgs = int(item.get("incoming_image_count") or 0)
        except (TypeError, ValueError):
            return None
        if not (tt and inp > 0 and imgs > 0 and new_imgs > 0):
            return None
        k = 1.2  # 粗略 token 估计 → provider 真实文本 token 的标定系数（60 轮反推 1.21）
        img_total = min(inp, max(0.0, inp - tt * k))
        if img_total <= 0:
            # 文字 token 估计已经吃满 input：这轮其实没多少图片份额，交回字符比例
            return None
        text_share = 1.0 - img_total / inp
        img_cached_share = (img_total / inp) * max(0, imgs - new_imgs) / float(imgs)
        return round(100.0 * (b / 100.0 * text_share + img_cached_share), 1)

    @staticmethod
    def _hit_estimate(item: dict):
        """命中预估（v3.19 用户 m41511 第 8 条；v5.x 用户 m11874 重做）。

        离线拿盘上 97 轮实测比过几套公式（MAE，pp）：
          纯前缀字符比例 11.3 ｜ 0.5 前缀 + 0.5 上一轮实测 15.2 ｜
          max(前缀, 上轮) 18.4 ｜ 只用上轮实测 24.9 ｜ 前缀 + 间隔打五折 10.4。
        结论是前缀比例本身就最准，之前两版「校准」都是把它拉偏：
        · 用「前缀 token ÷ provider input_tokens」当基准，分母里含 system prompt 与
          工具定义，和分子不是一个尺度，会把预估系统性压低（线上 57.1% 估成 14.6%）；
        · 和上一轮实测各半平均更糟：上一轮 0% 往往意味着刚把这段写进缓存，本轮恰恰
          会命中，各半平均等于直接砍半。

        v5.x（2026-11-12 用户 m11874「预估的确实不准」）：字符比例在带图轮系统性偏高
        ——图片是 token 但只有 0 字符，system prompt 又占字符大头、token 小头。改用
        「消息条数前缀比例」cache_prefix_len / cache_series_len 做第二把尺子，两者按
        0.3 / 0.7 混合，但条数那一份先和字符比例取 min（条数比例虚高时以字符比例为准）。
        311 轮 DS 决策轮实测：MAE 11.93→7.40、偏差 +5.82→+0.31，中位误差 5.4pp；
        条数比例 ≥ 字符比例的 107 轮用纯字符比例（MAE 6.48、偏差 −0.43），
        低于的 204 轮用 0.3/0.7 混合（MAE 7.88、偏差 +0.70），前后半段各自 7.27 / 7.80。
        同时去掉两处旧校准：
        · 全局 −2pp：混合后偏差只剩 +1.35，再减就过头；
        · 间隔 >3h 打五折：用户 m11874 指出各家缓存已能维持很久，308 轮里 >3h 的样本
          极少，去掉后 MAE 几乎不变。

        含图轮拆成「文字 + 图片」两份（v3.53 用户 m08226 第 3 条）：``assembled`` 的 content
        是纯字符串，图片只活在 native 投影里，字符前缀比例把整张图当 0 字符，于是这轮新加
        一张图时预估会高得离谱（线上 93.9%，实测 65.2%）。现在只把「图片占 input 的那一份」
        单独按「见过几张」折算（公式见 ``_hit_estimate_with_images``），文字那份继续用字符
        比例，所以纯文本轮的预估一个数都不变。含图路径的 −2pp 校准沿用 v3.53 的拟合结果。

        v5.x 三版（2026-11-12 用户 m12441「每条都尽量精准」）：上面两版都还站在字符
        前缀上，而字符前缀本身就是错的尺子（图片 0 字符、system prompt 字符多 token 少、
        一条消息只改后半段也算命中）。现在改由 request_log 按「token 份额」算好——
        分子是消息指纹公共前缀里那些消息的 token，分母是整轮 token，图片按张折算——
        行里带 token_prefix_percent，这里直接用；拿不到才退回下面的字符口径。
        311 轮 DS 决策轮实测：字符口径 11.93pp → 混合口径 7.44pp → 本口径 4.5pp，
        中位误差 2.6pp，残差只剩上游真失效的那两轮。
        """
        # 没有真实请求的轮次（裁切快照、上游失败）没有实测可比，预估写出来只会误导。
        _inp = 0
        _srcs = [item.get("input_tokens")]
        _tok = item.get("tokens")
        if isinstance(_tok, dict):
            _srcs.append(_tok.get("input"))
        _st = item.get("step_stats")
        if isinstance(_st, list):
            _step_sum = 0
            for _sp in _st:
                if isinstance(_sp, dict):
                    try:
                        _step_sum += int(_sp.get("in") or 0)
                    except (TypeError, ValueError):
                        pass
            _srcs.append(_step_sum)
        for _v in _srcs:
            try:
                _inp = max(_inp, int(_v or 0))
            except (TypeError, ValueError):
                pass
        if _inp <= 0:
            return None
        token_est = item.get("token_prefix_percent")
        if token_est is not None:
            try:
                return round(max(0.0, min(100.0, float(token_est))), 1)
            except (TypeError, ValueError):
                pass
        base = item.get("prefix_reuse_percent")
        if base is None:
            return None
        try:
            base = float(base)
        except (TypeError, ValueError):
            return None
        mix = MultiDecisionPlugin._hit_estimate_with_images(item, base)
        if mix is not None:
            base = mix - 2.0  # 含图路径沿用 v3.53 的 −2pp 校准（114 轮拟合时含这一项）
        else:
            # v5.x：消息条数前缀比例做第二把尺子，和字符比例 0.3/0.7 混合。
            try:
                _ser = float(item.get("cache_series_len") or 0)
                _pre = float(item.get("cache_prefix_len") or 0)
            except (TypeError, ValueError):
                _ser = _pre = 0.0
            if _ser > 0:
                _msg = max(0.0, min(100.0, _pre * 100.0 / _ser))
                # v5.x 二版（2026-11-12 用户 m11874）：条数比例高于字符比例时，字符比例
                # 本身更准（107 轮 MAE 6.48、偏差 −0.43）；低于时 0.3/0.7 混合更准
                # （204 轮 MAE 7.88、偏差 +0.70）。取 min 正好是这两簇的分段最优：
                # 311 轮 MAE 7.51→7.40、偏差 +1.33→+0.31，中位误差 5.4pp。
                base = base * 0.3 + min(base, _msg) * 0.7
        return round(max(0.0, min(100.0, base)), 1)

    @staticmethod
    def _hit_note(item: dict, prev_item):
        """命中预估的断因说明（v3.22 第 5 条）：解释这一轮为什么低/高。

        117 轮离线复核过约 20 套公式，字符前缀比例仍是最准的估计器
        （MAE 8.9pp、中位 3.5pp、误差超 10pp 占 25.6%），残差主要来自上游网关
        的账号路由与缓存写入时序，插件侧预测不了。真正能解释偏差的是窗口动作：
        69 轮前缀 <70% 的断点里 45 轮断在 user、22 轮断在 assistant，几乎都在
        窗口第一条 —— 都是裁切/重建造成。所以这里只把这些可观测事实写出来。
        """
        est = item.get("hit_estimate")
        if est is None:
            # v5.x：裁切快照与失败轮根本没发请求，说成「第一轮」会让人以为能估。
            _out = str(item.get("outcome") or "")
            _tin = 0
            try:
                _tin = int((item.get("tokens") or {}).get("input") or 0)
            except (TypeError, ValueError):
                _tin = 0
            if _out in ("trim", "error", "failed") or _tin <= 0:
                return "这一轮没有成功发出请求（裁切或失败），没有实测命中可比"
            return "第一轮：没有上一轮可比，无法估算"
        parts = []
        if item.get("token_prefix_basis") == "system_only":
            # v5.x：这一轮没有和上一轮共享的历史消息，能复用的只有系统提示词那一段。
            parts.append("没有可复用的历史前缀，只按系统提示词那段算")
        cur_w = item.get("window_size")
        prev_w = (prev_item or {}).get("window_size")
        if isinstance(cur_w, int) and isinstance(prev_w, int) and prev_w > 0:
            if cur_w < prev_w:
                if prev_w - cur_w >= max(3, int(prev_w * 0.2)):
                    parts.append("窗口 %d→%d 条（裁切或热重载后重建），开头那段前缀被换掉了"
                                 % (prev_w, cur_w))
                else:
                    parts.append("窗口 %d→%d 条，开头少量消息被移除" % (prev_w, cur_w))
            elif cur_w > prev_w:
                parts.append("窗口 %d→%d 条，只在末尾追加，前缀仍可复用" % (prev_w, cur_w))
            else:
                parts.append("窗口 %d 条，与上一轮相同" % cur_w)
        gap = item.get("gap_from_previous")
        if isinstance(gap, (int, float)) and gap > 3 * 3600:
            # v5.x（用户 m11874）：各家缓存已能维持很久，不再按间隔打折，这里只说明事实。
            parts.append("距上一轮 %.1f 小时，缓存一般仍有效，未按时间打折" % (gap / 3600.0))
        imgs = int(item.get("image_count") or 0)
        new_imgs = int(item.get("incoming_image_count") or 0)
        if imgs > 0 and new_imgs > 0:
            parts.append("本轮新加 %d 张图（共 %d 张），新图那部分 token 不进缓存，"
                         "已按图片占比从预估里扣掉" % (new_imgs, imgs))
        act = (item.get("tokens") or {}).get("cache_hit")
        if item.get("cache_unreported"):
            # v5.x（用户 m12891）：上游没回报 cached_tokens 时不能拿 0% 当实测去比，
            # 反推得出来的标「≈」，反推不出来的直说无法反推。
            _inf = any(isinstance(_sp, dict) and _sp.get("hit_est") is not None
                       for _sp in (item.get("step_stats") or []))
            parts.append("上游没回报 cached_tokens，这一轮命中%s"
                         % ("按上报 token 反推（≈）" if _inf else "无法反推"))
        if isinstance(act, (int, float)):
            if abs(est - act) >= 1.0:
                parts.append("本轮实测 %s%%，预估%s %.1f 个百分点"
                             % (act, "偏高" if est > act else "偏低", abs(est - act)))
            else:
                parts.append("本轮实测 %s%%，基本吻合" % act)
        return "；".join(parts)

    def _retry_summary(self, sid: str, record: dict):
        history = getattr(self, "retry_history", None)
        summary = history.summary(sid, str(record.get("event_id") or "")) if history else None
        if summary is not None:
            return summary
        if record.get("failed"):
            # A legacy failure proves an error, not the identity/result of any retry.
            return {"root_event_id": str(record.get("event_id") or ""),
                    "had_error": True, "attempts": 0, "status": "unknown",
                    "succeeded": None}
        return None

    def _rounds_with_retry(self, sid: str, limit: int = 50) -> list:
        """最近若干轮，每行带上 retry_history（只读，给重试链拼接用）。

        v300/v301/v302 的测试用只实现 read_round 的假 log，取不到列表就当空（不报错）。
        """
        # v4.76：优先用带目录指纹缓存的那份；它返回浅拷贝，改 retry_history 不会弄脏缓存。
        getter = getattr(self.request_log, "list_rounds_cached", None)
        if not callable(getter):
            getter = getattr(self.request_log, "list_rounds", None)
        rows = []
        if callable(getter):
            try:
                try:
                    rows = list(getter(sid, limit, light=True) or [])
                except TypeError:
                    rows = list(getter(sid, limit) or [])
            except Exception:
                rows = []
        for row in rows:
            if isinstance(row, dict):
                row["retry_history"] = self._retry_summary(sid, row)
        return rows

    @staticmethod
    def _retry_outcome_of(record: dict, rows: list):
        """原错误那一轮后来补回来没有：重试了几次、成没成、回复落在哪一轮。

        请求记录是证据、只读（v3.04 契约），所以这里不回写：重试轮的记录本来就单独存在
        rows 里，按 retry_history 的 root_event_id 把两份拼到一起即可（用户 2026-10-24 图4/图5）。
        只有「原错误轮」自己才返回结果；重试轮本人反过来用 retry_of 指回原轮。
        """
        summ = record.get("retry_history") or {}
        if not isinstance(summ, dict):
            return None
        root = str(summ.get("root_event_id") or "")
        own = str(record.get("event_id") or "")
        if not root or not own or root != own:
            return None
        attempts = max(0, int(summ.get("attempts") or 0))
        status = str(summ.get("status") or "")
        if attempts <= 0 and status in ("", "not_retried"):
            return None  # 压根没重试过，别在表格里刷一行废话
        out = {
            "root_event_id": root,
            "attempts": attempts,
            "status": status,
            "succeeded": summ.get("succeeded"),
            "event_id": "",   # 重试轮的 event_id，前端点「看那次回复」用
            "name": "",       # 重试轮的记录文件名，同一个会话下可直接 /request 打开
            "ts": 0,
            "replied": False,
            "reply_bubbles": 0,
        }
        latest = None
        for row in rows or []:
            if str(row.get("event_id") or "") == root:
                continue
            rs = row.get("retry_history") or {}
            if not isinstance(rs, dict):
                continue
            if str(rs.get("root_event_id") or "") != root:
                continue
            if latest is None or float(row.get("ts") or 0) >= float(latest.get("ts") or 0):
                latest = row
        if latest is not None:
            out["event_id"] = str(latest.get("event_id") or "")
            out["name"] = str(latest.get("name") or "")
            out["ts"] = float(latest.get("ts") or 0)
            out["replied"] = bool(latest.get("replied"))
            out["reply_bubbles"] = int(latest.get("reply_bubbles") or 0)
        return out

    @staticmethod
    def _retry_attempt_index(record: dict, rows: list) -> int:
        """这一行是这条出错链里的第几次重试（用户 m05205 第 3 条），算不出给 0。

        只数「root 相同、自己不是 root」的那些轮，按 ts 从小到大排；前端据此写
        「重试失败 X1 / X2」「重试中」「重试成功」。
        """
        summ = record.get("retry_history") or {}
        if not isinstance(summ, dict):
            return 0
        root = str(summ.get("root_event_id") or "")
        own = str(record.get("event_id") or "")
        if not root or not own or root == own:
            return 0
        chain = []
        for row in rows or []:
            rs = row.get("retry_history") or {}
            if not isinstance(rs, dict) or str(rs.get("root_event_id") or "") != root:
                continue
            if str(row.get("event_id") or "") == root:
                continue
            chain.append(row)
        chain.sort(key=lambda x: float(x.get("ts") or 0))
        for idx, row in enumerate(chain):
            if str(row.get("event_id") or "") == own:
                return idx + 1
        return 0

    def _retry_link(self, sid: str, record: dict):
        """这一行是不是「重试轮」，是的话指出它在补哪一轮（用户 2026-10-24 图4）。

        retry_history 里 events 同时装着原轮与重试轮，summary() 两边都能查到；
        只有 root_event_id != 本轮 event_id 时，这一行才是「重试产生的新一轮」。
        """
        summ = record.get("retry_history") or {}
        if not isinstance(summ, dict):
            return None
        root = str(summ.get("root_event_id") or "")
        own = str(record.get("event_id") or "")
        if not root or not own or root == own:
            return None
        name = ""
        try:
            name = self.request_log.name_of(sid, root) if self.request_log else ""
        except Exception:
            name = ""
        return {
            "root_event_id": root,
            "root_name": name or "",
            "attempts": max(0, int(summ.get("attempts") or 0)),
            "succeeded": summ.get("succeeded"),
            "status": str(summ.get("status") or ""),
        }

    @staticmethod
    def _round_outcome(record: dict, trace=None) -> dict:
        """只由本轮稳定身份的结算/轨迹还原结果；老文件不重写。"""
        error = str(record.get("error") or "")
        error = error or str(record.get("last_error") or "")
        error = error or str((trace or {}).get("error") or "")
        for step in (trace or {}).get("steps") or []:
            text = str(step.get("text") or "")
            if text.lstrip().startswith("[ProviderError]"):
                error = error or text
        failed = bool(record.get("failed") or error or (trace or {}).get("failed")
                      or record.get("outcome") in ("error", "failed")
                      or (trace or {}).get("outcome") in ("error", "failed"))
        # v3.87（用户 m04715 第 1 条）：非请求触发的裁切会单独落一份快照（outcome="trim"），
        # 它不是一轮请求。以前这里一律按 replied 拼成 no_reply，于是走势图上那根柱子
        # 变成灰色「未回复」、请求记录里也多出一条像失败的记录。原样透传。
        if not failed and str(record.get("outcome") or "") == "trim":
            return {"outcome": "trim", "failed": False, "error": "", "replied": False}
        # 用户 m17492 图5/图6：别的插件（如 nai 出图）完成通知触发的这一轮不是聊天请求，
        # 原样透传成「工具回调」——别算成空回复，也别让它顶替上一轮的回复状态。
        if not failed and (record.get("notice_only") is True
                           or str(record.get("outcome") or "") == "notice"):
            return {"outcome": "notice", "failed": False, "error": "",
                    "replied": False, "notice_only": True}
        outcome = "error" if failed else ("replied" if record.get("replied") else "no_reply")
        if record.get("outcome") in ("pending", "running") and not failed:
            outcome = "pending"
        return {"outcome": outcome, "failed": failed, "error": error,
                "replied": bool(record.get("replied")) and not failed}

    @staticmethod
    def _is_trim_row(item: dict) -> bool:
        """这一条是不是裁切快照（它自己也占一行，但不是一次请求）。"""
        return str(item.get("outcome") or "") == "trim" or item.get("trim_only") is True

    @staticmethod
    def _round_kind(item: dict) -> str:
        """这一行是决策快照还是请求快照。（实现在 window/manager.py，测试直接用）"""
        return round_kind(item)

    def _trim_owner_by_time(self, rounds: list, cut: dict):
        """event_id 认不出来时，这次裁切该挂在哪一轮。"""
        return trim_owner_of(rounds, cut)

    _CUT_OWNER_GAP = 180.0

    def _resolve_cut_owner(self, universe: list, cut: dict):
        """这一刀归哪一轮。

        v4.75（用户 m02928）：老记录里 event_id 会串到一两个小时前那一轮上去（空闲巡检
        裁的那一刀还记着上一轮的 event_id），以前只认 id、不看时间，就把这刀硬塞给那一
        轮，它自己那根柱子反被当成重复的抹掉。现在 id 对得上还得时间挨着，否则退回按
        时间就近归属（trim_owner_of，自带 180 秒上限）。
        """
        eid = str(cut.get("event_id") or "")
        if eid:
            try:
                cts = float(cut.get("ts") or 0.0)
            except (TypeError, ValueError):
                cts = 0.0
            best = None
            for x in universe:
                if str(x.get("event_id") or "") != eid:
                    continue
                try:
                    rts = float(x.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if rts > cts + 5.0 or (cts - rts) > self._CUT_OWNER_GAP:
                    continue
                if best is None or rts > float(best.get("ts") or 0.0):
                    best = x
            if best is not None:
                return best
        return self._trim_owner_by_time(universe, cut)

    def _drawn_cuts(self, sid: str, items: list, limit: int) -> list:
        """这一页和决策页上已经有橙标的那几刀。

        v4.75（用户 m02928）：去重以前只看本页请求轮手里的 trims_after，可裁切也会挂在
        决策轮上（判要回之后才落刀的），那些刀在决策页画橙、在请求页又留一根青柱。
        这里把两套轮次合起来算同一份归属，谁认领了就算谁画过。
        """
        rounds = [x for x in items if not self._is_trim_row(x)]
        universe = list(rounds)
        dec_page: list = []
        try:
            if self.request_log is not None:
                # v4.80：决策快照加深到 300 后，认领裁切的决策轮也可能在 200 份之外。
                try:
                    _dec_keep = int(self.eff("decision_keep", sid) or 0)
                except Exception:
                    _dec_keep = 0
                _deep = max(int(limit) * 2, 200, (_dec_keep or 0) * 2)
                rows = self._cached_rows(sid, _deep, light=True)
                for x in rows:
                    if self._is_trim_row(x):
                        continue
                    universe.append(x)
                dec_page = [x for x in rows
                            if not self._is_trim_row(x)
                            and self._round_kind(x) == "decision"][:max(int(limit), _dec_keep or 0)]
        except Exception:
            logger.debug("[multi-decision] 取决策轮用于裁切去重失败", exc_info=True)
            dec_page = []
        if self._window_log is None:
            return []

        def _key_of(_item) -> tuple:
            try:
                _ts = round(float(_item.get("ts") or 0.0), 3)
            except (TypeError, ValueError):
                _ts = 0.0
            return (_ts, self._round_kind(_item))

        drawn_keys = {_key_of(x) for x in rounds} | {_key_of(x) for x in dec_page}
        out = []
        for cut in self._window_log.read_recent(1000, sid=sid):
            owner = self._resolve_cut_owner(universe, cut)
            if owner is None:
                continue
            if _key_of(owner) in drawn_keys:
                out.append(cut)
        return out

    def _attach_round_trims(self, sid: str, items: list) -> None:
        """先在后端绑定裁切，前端倒序/翻页不再改变归属。

        v4.58（用户 m29554）：决策页和请求页各调一次，每次手里只有自己那种轮次；
        可「判要回的裁切」是等回复跑完才落刀，只看一种轮次会认错人（决策页把它
        当成决策轮的，请求页根本看不到决策轮）。归属判定改成把两套轮次合起来看，
        认出来的那一轮不在当前页就不标——同一根裁切只会在它真正落刀的那根上标橙。
        """
        ordered = sorted(items, key=lambda x: float(x.get("ts") or 0))
        rounds = [x for x in ordered if not self._is_trim_row(x)]
        for item in ordered:
            item["trims_after"] = []
        if not ordered or self._window_log is None:
            return
        universe = list(rounds)
        try:
            if self.request_log is not None:
                # v4.80：决策轮加深后，裁切可能是 200 份以前的决策轮落下的。
                try:
                    _dk = int(self.eff("decision_keep", sid) or 0)
                except Exception:
                    _dk = 0
                for x in self._cached_rows(sid, max(400, (_dk or 0) * 2), light=True):
                    if self._is_trim_row(x):
                        continue
                    universe.append(x)
        except Exception:
            logger.debug("[multi-decision] 取两套轮次用于裁切归属失败", exc_info=True)
        for cut in self._window_log.read_recent(1000, sid=sid):
            owner = self._resolve_cut_owner(universe, cut)
            if owner is not None and not any(owner is x for x in rounds):
                # 认出来的那一轮不在当前页：换成这一页里同一条（同一时刻、同一类）
                key = (round(float(owner.get("ts") or 0), 3), round_kind(owner))
                owner = next(
                    (x for x in rounds
                     if (round(float(x.get("ts") or 0), 3), round_kind(x)) == key),
                    None,
                )
            if owner is not None:
                owner["trims_after"].append(dict(cut))

    def _totals_of(self, sid: str, item: dict):
        """这一轮的 token 汇总，取不到就返回 None（老记录没有轨迹）。"""
        if self.tracer is None:
            return None
        try:
            eid = str(item.get("event_id") or "")
            if eid:
                rec = self.tracer.read_by_event(sid, eid)
                return (rec or {}).get("totals")
            ts = float(item.get("ts") or 0)
            if ts <= 0:
                return None
            best, best_gap = None, 30.0
            for cand in self.tracer.recent(sid, limit=40):
                gap = abs(float(cand.get("started") or 0) - ts)
                if gap < best_gap:
                    best, best_gap = cand, gap
            if best is None:
                return None
            rec = self.tracer.read(sid, best["name"])
            return (rec or {}).get("totals")
        except Exception:
            return None

    @_api("GET", "/request")
    async def api_request_detail(self, sid: str, name: str):
        """一轮的完整上下文（模型实际看到的内容）+ 这一轮的调用轨迹。"""
        # 决策 / 裁切这类特殊快照缺字段时别把整页打成 500，页面至少能显示原因（v3.91）。
        # v4.76（用户 m05225）：这些同步计算原本全压在事件循环上（实测 1.8–2.6 秒），
        # 期间 5 秒轮询（/window、/decision-rounds、/windows）与 /quote-lookup 一起排队，
        # 看着就像「点一下要等几秒」。拆成 head / 原生消息 / tail 三段，同步段统统丢进线程。
        try:
            stage = await asyncio.to_thread(self._request_detail_head, sid, name)
            if not stage.get("ok"):
                return stage
            owner_rows = await asyncio.to_thread(read_native_rows, stage["raw_sid"], stage["owner_ids"])
            # 只等 0.6 秒：转发内容取不到不该让整页卡三秒，剩下的丢后台（v4.76）。
            if await self._refresh_forward_display_proofs(stage["raw_sid"], owner_rows, budget=0.6):
                self._spawn_forward_proof_refresh(stage["raw_sid"], owner_rows)
            return await asyncio.to_thread(self._request_detail_tail, stage, owner_rows)
        except Exception as exc:
            logger.exception("[multi-decision] 读单轮快照失败 %s/%s", sid, name)
            return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}

    def _request_detail_head(self, sid: str, name: str) -> dict:
        """读快照、算判定与展示投影（除原生消息库之外的全部前置工作）。"""
        media_render_begin()
        data = self.request_log.read_round(sid, name) if self.request_log else None
        if not isinstance(data, dict):
            return {"ok": False, "detail": "记录不存在"}
        if data.get("sid") and normalize_sid(data["sid"]) != normalize_sid(sid):
            return {"ok": False, "detail": "记录会话不匹配"}
        rec = copy.deepcopy(data)
        # v4.26（用户 m22251）：② 的「命中 / 不命中 / 本轮输入」边界改成按上一次回复
        # 的位置切，老快照里存的是旧口径，详情返回前按同一份基准重算一遍。
        try:
            if self.request_log is not None:
                rec.update(self.request_log.recalc_cache(sid, name, data) or {})
        except Exception:
            logger.debug("[multi-decision] 重算缓存边界失败", exc_info=True)
        # Raw request input is independent of every display-only transformation.
        rec["request_input"] = copy.deepcopy({
            key: data.get(key) for key in (
                "messages", "assembled", "system_prompt", "system_static",
                "system_relocated", "user_prompt", "user_prompt_parts",
            )
        })
        rec.update(payload_metrics(data))
        # 用户 m00942 第 4 条：快照标题后面要能点一下复制编号，编号跟柱子 tip 用同一套。
        rec["snap_id"] = _snapshot_id(rec.get("ts"))
        # v4.86（用户 m10425 第 4 条）：轨迹本来就写在这份快照里（列表那条路也是这么取的），
        # 直接取比按 event_id 把整个目录扫一遍快得多——裁切快照一份轨迹都没有，以前
        # 每点一次都要白扫最多 300 份 JSON（实测多花 160ms 上下）。
        trace = None
        _embedded = data.get("trace")
        if isinstance(_embedded, dict) and self.tracer is not None:
            _eid = str(data.get("event_id") or "")
            if _eid and str(_embedded.get("event_id") or "") == _eid:
                try:
                    trace = self.tracer._decorate(copy.deepcopy(_embedded))
                except Exception:
                    logger.debug("[multi-decision] 内嵌轨迹装饰失败", exc_info=True)
                    trace = None
        if trace is None and not self._is_trim_row(data):
            trace = self._trace_of(sid, data)
        rec.update(self._round_outcome(rec, trace))
        rec["retry_history"] = self._retry_summary(sid, rec)
        _rows = self._rounds_with_retry(sid, 50)
        rec["retry_of"] = self._retry_link(sid, rec)
        if isinstance(rec["retry_of"], dict):
            rec["retry_of"]["attempt_index"] = self._retry_attempt_index(rec, _rows)
        # 这一轮如果是「出错原轮」，把后来那次重试的结果与回复挂到下面（只读拼接）
        rec["retry_outcome"] = self._retry_outcome_of(rec, _rows)
        # A normalized SID cannot identify a bot. Use only an original raw SID
        # for this session, preferring the live window; never reverse-guess one.
        raw_sid = ""
        for candidate in ([self.window.raw_sid(sid)] if self.window is not None else []) + [
                data.get("raw_sid"), data.get("session_id"), data.get("sid")]:
            if (isinstance(candidate, str) and len(candidate.split(":")) >= 3
                    and candidate.split(":")[-2] in ("gm", "dm")
                    and normalize_sid(candidate) == normalize_sid(sid)):
                raw_sid = candidate
                break
        who = self.window._bot_name(sid) if self.window is not None else ""
        # 用户本轮第 3 条：② 历史消息只画历史那段。老写法取完整 messages，
        # 本轮那条（stage 已切进 incoming_messages）会在这里再画一遍，
        # 于是 ② 和 ③ 看着像同一条 @ 出现两次。有 history_messages 就优先用它。
        # 只有「没这个字段/类型不对」才退回完整 messages；显式空列表就是空历史，
        # 不能当成缺失再画一遍本轮消息。
        history_source = data.get("history_messages")
        if not isinstance(history_source, list):
            history_source = data.get("messages")
        if not isinstance(history_source, list):
            history_source = []
        history_source = copy.deepcopy(history_source)
        # Proof must precede regroup/split and view_of's compatibility cache fill.
        history_proof = raw_forward_proof_items(history_source)
        try:
            history_source = regroup_images(history_source)
        except Exception as exc:
            logger.debug("[multi-decision] 记录里的图片归位失败: %s", exc)
        # v4.01（用户 m11166 图4）：决策快照的 user_prompt_parts 存的是整段聊天记录
        # （transcript），当「本轮新消息」拼进快照就会多出一条两千多字的大杂烩，
        # 等于把 ② 历史消息整段又抄了一遍。决策轮真正的新消息只有 incoming_messages。
        incoming = []
        if rec.get("decision_only"):
            for line in data.get("incoming_messages") or []:
                # 存的是框架消息（{role, content}），老版本可能是裸字符串，两种都收
                _txt = ((line.get("content") or line.get("text") or "")
                        if isinstance(line, dict) else line)
                if isinstance(_txt, list):
                    _txt = content_text(_txt)
                if isinstance(_txt, str) and _txt.strip():
                    incoming.append({"text": _txt})
        else:
            incoming = [p for p in rec.get("user_prompt_parts") or []
                        if isinstance(p, dict) and p.get("name") == "message"]
            if not incoming and rec.get("user_prompt"):
                incoming = [{"text": rec["user_prompt"]}]
        new_msgs = [{"role": "user", "content": p.get("text") or ""} for p in incoming]
        incoming_proof = (raw_forward_proof_items(data.get("incoming_messages") or [])
                          if rec.get("decision_only") else
                          [dict(p, content=p.get("text") or "") for p in incoming])
        prompt_proof = [dict(p, content=p.get("text") or "") if isinstance(p, dict)
                        else {"content": p or ""} for p in data.get("user_prompt_parts") or []]
        history = expand_packed_messages(history_source)
        new_msgs = expand_packed_messages(new_msgs)
        rec["history_display_messages"] = view_of(history, bot_name=who, raw_sid=raw_sid)
        rec["incoming_display_messages"] = view_of(new_msgs, bot_name=who, raw_sid=raw_sid)
        # 用户 m17434：被拉黑的消息没进上下文，别混进「本轮消息」；顶部那条「未进入上下文」
        # 也只列这一轮范围内的，且不列被拉黑的。
        rec["incoming_messages"] = self._drop_blacklist_rows(rec.get("incoming_messages"), rec.get("skipped"))
        rec["skipped_view"] = self._apply_skips(
            self._reply_skips_view(rec.get("messages") or [], rec.get("skipped"),
                                   float(rec.get("ts") or 0.0)),
            rec["history_display_messages"] + rec["incoming_display_messages"],
            float(rec.get("ts") or 0.0))
        # One-to-one display aliases keep original request indexes stable in the UI.
        rec["display_messages"] = view_of(data.get("messages") or [], bot_name=who, raw_sid=raw_sid)
        rec["display_user_prompt_parts"] = view_of([{"role": "user", "content": (p.get("text") if isinstance(p, dict) else p) or ""}
             for p in rec.get("user_prompt_parts") or []], bot_name=who, raw_sid=raw_sid)
        # Native attachments are injected by the host after ON_LLM_REQUEST.
        # Display their exact recorded projection without rewriting raw prompt text.
        projected_media = None
        if data.get("metric_stage") == "native_projection":
            projected_media = {}
            for part in data.get("native_projection") or []:
                if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                    continue
                projected_media[part["text"]] = [
                    {"path": ref["path"], "mime": ref.get("mime_type") or ""}
                    for ref in part.get("refs") or []
                    if isinstance(ref, dict) and ref.get("type") == "kira_image_ref"
                    and isinstance(ref.get("path"), str)
                ]
            for field in ("incoming_display_messages", "display_user_prompt_parts"):
                for item in rec[field]:
                    raw = item.get("raw")
                    item["media"] = copy.deepcopy(projected_media.get(raw, [])) if isinstance(raw, str) else []
                    item["media_source"] = "native_projection"
        # Project after native_projection's aggregate overlay and before either
        # snapshot captures these aliases. Each source has its own original proof;
        # a cache-filled view (or another alias with the same mid) proves nothing.
        owner_proofs = {
            "history_display_messages": history_proof,
            "incoming_display_messages": incoming_proof,
            "display_messages": raw_forward_proof_items(data.get("messages") or []),
            "display_user_prompt_parts": prompt_proof,
        }
        owner_ids, owner_seen = [], set()
        owner_items = [item for field in owner_proofs for item in rec[field]]
        owner_items += [{"content": item.get("raw") or item.get("full_text") or item.get("text") or ""}
                        for item in (trace or {}).get("sent") or [] if isinstance(item, dict)]
        for item in owner_items:
            mid = display_message_id(item, text_of_content=content_text)
            if mid and mid not in owner_seen:
                owner_seen.add(mid)
                owner_ids.append(mid)
            if len(owner_ids) >= 900:
                break
        render_memo = media_render_state()

        return {"ok": True, "sid": sid, "name": name, "data": data, "rec": rec, "trace": trace,
                "render_memo": render_memo,
                "raw_sid": raw_sid, "who": who, "projected_media": projected_media,
                "owner_proofs": owner_proofs, "owner_ids": owner_ids,
                "history": history, "new_msgs": new_msgs}

    @staticmethod
    def _window_skips_of(messages, skipped, ref_ts: float = 0.0) -> list:
        """只留还在窗口时间范围内的虚线记录（用户 m17018 图2、m16145）。

        跳过记录不随窗口裁切清理，窗口早就翻过去的老消息会一直挂在页面底部。
        窗口里的历史消息没有 ts，只有正文开头的 [23:25]，所以按钟点补成完整时间
        再比；只按 ts 会算成第一条 assistant 回复，比窗口实际起点晚得多，落在起点
        附近的虚线卡就被误删了。留 10 分钟余量。
        记录页的消息没有 ts，调用方用 ref_ts 传这一轮的记录时刻（用户 m17434）。
        """
        rows = [s for s in (skipped or []) if isinstance(s, dict)]
        if not rows:
            return []
        base = 0.0
        # 用户 m45914：参考时刻只从窗口消息里取，别把虚线记录自己的 ts 算进来，
        # 否则一条昨天的记录会把整窗消息都补成昨天，它自己反而留在范围内。
        for it in messages or []:
            try:
                t = float(it.get("ts") or 0.0)
            except Exception:
                t = 0.0
            if t > base:
                base = t
        if base <= 0:
            base = float(ref_ts or 0.0)
        if base <= 0:
            base = time.time()
        ref_ts = base
        oldest_ts = 0.0
        for it in messages or []:
            t = _skip_stamp_of(it, ref_ts)
            if t > 0 and (oldest_ts <= 0 or t < oldest_ts):
                oldest_ts = t
        if oldest_ts <= 0:
            return rows
        return [s for s in rows if _skip_stamp_of(s, ref_ts) >= oldest_ts - 600]

    def _reply_skips_view(self, messages, skipped, ts: float = 0.0) -> list:
        """回复记录页顶部那条「未进入上下文」：只留这一轮范围内的，且不列被拉黑的（用户 m17434）。"""
        rows = self._window_skips_of(messages, skipped, ts)
        return [s for s in rows if str(s.get("reason") or "") != "blacklist"]

    @staticmethod
    def _drop_blacklist_rows(rows, skipped) -> list:
        """被拉黑的消息没进上下文，别混进回复记录的「本轮消息」（用户 m17434）。"""
        mids = {str(s.get("mid") or "") for s in (skipped or [])
                if isinstance(s, dict) and str(s.get("reason") or "") == "blacklist"}
        mids.discard("")
        if not mids or not isinstance(rows, list):
            return rows
        return [m for m in rows
                if display_message_id(m, text_of_content=content_text) not in mids]


    @staticmethod
    def _skip_rows_at(rows, items, ts: float = 0.0) -> list:
        """把「没进上下文」的记录按时间算成快照里的插入位置（用户 m32048 第 1 条、m13127）。

        at = 插到 items 的第几条之前。优先按记录自己的 ts 找位置——挡下来那一刻对应
        消息可能还在待发，after_mid 只指向当时窗口的最后一条，会把它插到更早的地方
        （用户 m13127 图1/图3）。没有 ts 才退回 after_mid。
        锚点跟它落在同一分钟时以锚点为准（用户 m17770）：窗口里的历史消息只有 [HH:MM]，
        补出来的秒数跟着参考时刻走，同一分钟里比不出先后，按 ts 会排到整分钟那批前面。
        锚点已经不在这一份里就整条丢掉——那是别的轮次挡下来的，画到这一轮只会让人
        以为这轮又丢了一条。
        拉黑行例外（用户 m16754）：它的 ts 只有分钟粒度，按 ts 会插到同一分钟所有消息
        前面，所以先用前后两条放行消息的编号定位，锚点都不在才退回 ts。
        """
        if not rows:
            return []
        items = list(items or [])
        pos = {}
        for i, it in enumerate(items):
            mid = display_message_id(it, text_of_content=content_text)
            if mid and mid not in pos:
                pos[mid] = i
        # 用户 m45914：补钟点要用「这一份窗口自己的参考时刻」，不能拿每条虚线记录
        # 自己的 ts 当参考日。否则一条昨天的记录会把整窗消息都补成昨天，它自己
        # 反而落进范围、堆到窗口最底部。
        ref_ts = 0.0
        for it in items:
            v = _skip_ts_of(it)
            if v > ref_ts:
                ref_ts = v
        if ref_ts <= 0:
            ref_ts = float(ts or 0.0) or time.time()
        stamps = [_skip_stamp_of(it, ref_ts) for it in items]
        first_ts = min([s for s in stamps if s > 0], default=0.0)
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            rts = _skip_stamp_of(r, ref_ts)
            at = -1
            anchored = False
            # 用户 m16754：拉黑那条的 ts 只有分钟粒度（raw 行头只到分），按 ts 插会落到
            # 同一分钟所有消息的前面。它一定还在窗口里，改用前后两条放行消息的编号定位：
            # 先看前面那条（紧挨着它，位置最准），前面那条不在快照里才退回后面那条。
            if str(r.get("reason") or "") == "blacklist":
                am = str(r.get("after_mid") or "")
                bm = str(r.get("before_mid") or "")
                if am and am in pos:
                    at = pos[am] + 1
                    anchored = True
                elif bm and bm in pos:
                    at = pos[bm]
                    anchored = True
            if not anchored:
                at = _skip_at_by_ts(rts, stamps)
                # 用户 m17770：同一分钟里虚线卡自己带秒（00:50:09），窗口消息补出来的秒数
                # 跟着参考时刻走（00:50:37），按 ts 就排到了整分钟那批前面。锚点落在同一
                # 分钟时以锚点为准——它才是这条消息真正的前一条。
                am = str(r.get("after_mid") or "")
                if at >= 0 and am and am in pos and rts > 0:
                    ast = stamps[pos[am]]
                    if ast > 0 and int(ast // 60) == int(rts // 60):
                        at = pos[am] + 1
                        anchored = True
            if at < 0:
                am = str(r.get("after_mid") or "")
                if am:
                    if am not in pos:
                        continue
                    at = pos[am] + 1
                else:
                    # 没有锚点（挡下来时窗口还是空的）：只在它确实属于这一轮时才算进来。
                    if not ts or not rts or ts - rts > 3600:
                        continue
                    at = 0
            elif not anchored and first_ts > 0 and rts < first_ts - 60:
                # 比这一份里最早的消息还早一分钟以上：窗口已经翻过去了，画在最前面
                # 会让人以为这一轮又丢了一条（用户 m13127 图4）。
                continue
            row = dict(r)
            row["at"] = at
            out.append(row)
        return out

    def _apply_skips(self, rows, items, ts: float = 0.0) -> list:
        """虚线行算成插入位置，并把被拉黑的消息本体标成虚线（用户 m32048 第 4 条）。

        实时窗里拉黑消息是本体画虚线、不再单独出一张卡；快照要跟它一致，否则同一条
        消息会画两遍（一张正常卡 + 一张虚线卡）。用行自己的 mid 对，不看当前窗口——
        翻几天前的快照时窗口早就翻过去了。
        """
        out = self._skip_rows_at(rows, items, ts)
        bl_mids = {str(r.get("mid") or "") for r in out
                   if str(r.get("reason") or "") == "blacklist"}
        bl_mids.discard("")
        marked = set()
        if bl_mids:
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                mid = display_message_id(it, text_of_content=content_text)
                if mid and mid in bl_mids:
                    it["skip"] = True
                    it["skip_reason"] = "blacklist"
                    marked.add(mid)
        return [r for r in out
                if str(r.get("reason") or "") != "blacklist"
                or str(r.get("mid") or "") not in marked]

    def _request_detail_tail(self, stage: dict, owner_rows) -> dict:
        """原生消息到手后，把快照 / 窗口 / 回复一起投影出来。"""
        media_render_resume(stage.get("render_memo"))
        sid = stage["sid"]
        name = stage["name"]
        data = stage["data"]
        rec = stage["rec"]
        trace = stage["trace"]
        raw_sid = stage["raw_sid"]
        who = stage["who"]
        projected_media = stage["projected_media"]
        owner_proofs = stage["owner_proofs"]
        history = stage["history"]
        new_msgs = stage["new_msgs"]
        def project_request_view(items, proof):
            return project_items(items, raw_sid, owner_rows, public_media=public_media,
                                 text_of_content=content_text, forward_proof_items=proof,
                                 forward_media_for_mid=forward_media_for_mid,
                                 original_media_for_mid=original_media_for_mid,
                                 media_by_digest=media_cache_by_digest,
                                 forward_display_proof_for_mid=forward_display_proof_for_mid)

        for field, proof in owner_proofs.items():
            rec[field] = project_request_view(rec[field], proof)
        # Compatibility fields retain full content; reverse-resolved media is display only.
        rec["messages"] = [dict(m, media=media_list(m.get("content"), raw_sid),
                                **({"who": who} if m.get("role") == "assistant" and who else {}))
                           if isinstance(m, dict) else m for m in rec.get("messages") or []]
        rec["user_prompt_media_parts"] = [
            copy.deepcopy(projected_media.get(p.get("text") if isinstance(p, dict) else p, []))
            if projected_media is not None
            else media_list(p.get("text") if isinstance(p, dict) else p, raw_sid)
            for p in rec.get("user_prompt_parts") or []
        ]
        snapshot_msgs = history + new_msgs
        rec["request_snapshot"] = {
            "ts": rec.get("ts") or 0, "event_id": rec.get("event_id") or "",
            "scope": "request_input",
            "messages": rec["history_display_messages"] + rec["incoming_display_messages"],
            "size": len(snapshot_msgs), "counted": count_messages(snapshot_msgs),
            # v3.23（用户 m45720）：标题要写出「含 bot X 条」，这里先把用户侧条数落盘，
            # bot 条数 = counted - user_counted，前端不用自己猜。
            "user_counted": count_user_messages(snapshot_msgs),
            "history_counted": count_messages(history), "incoming_counted": count_messages(new_msgs),
            # 用户 m32048 第 1 条：这一轮没进上下文的消息（虚线卡）跟着快照一起存，
            # 只是不进上下文；at 按锚点算，页面据此插回原位。
            "skipped": self._apply_skips(
                rec.get("skipped"),
                rec["history_display_messages"] + rec["incoming_display_messages"],
                float(rec.get("ts") or 0.0),
            ),
        }
        eid = str(rec.get("event_id") or "")
        verified = bool(eid and trace and str(trace.get("event_id") or "") == eid
                        and normalize_sid(str(trace.get("sid") or "")) == normalize_sid(sid)
                        and normalize_sid(str(rec.get("sid") or "")) == normalize_sid(sid))
        sent_msgs = []
        sent_meta = []
        if (verified and not rec["failed"] and not trace.get("failed")
                and rec.get("outcome") not in ("pending", "running")
                and trace.get("outcome") not in ("pending", "running", "error", "failed")):
            for item in trace.get("sent") or []:
                if not isinstance(item, dict) or item.get("ok") is not True:
                    continue
                raw = item.get("raw")
                has_raw = isinstance(raw, str) and bool(raw.strip())
                text = raw if has_raw else item.get("full_text", item.get("text") or "")
                if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("[ProviderError]"):
                    continue
                # Old clipped traces cannot supply a truthful complete reply.
                if not has_raw and "full_text" not in item and re.search(r"\n…（共 \d+ 字，已截断）$", text):
                    continue
                # Enrich only the raw display fallback, never the persisted request/trace.
                stamp = ""
                sent_ts = item.get("ts")
                if isinstance(sent_ts, (int, float)) and not isinstance(sent_ts, bool) and sent_ts > 0:
                    try:
                        stamp = time.strftime("%H:%M", time.localtime(sent_ts))
                    except (ValueError, OverflowError, OSError):
                        sent_ts = None
                else:
                    sent_ts = None
                display_raw = text
                complete_raw = has_raw and bool(re.match(
                    r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*<msg\b[^>]*>\s*\[[^\]]+\]", text))
                if not complete_raw:
                    try:
                        sent_name = item.get("name") or item.get("who")
                        if not isinstance(sent_name, str) or not sent_name.strip():
                            sent_name = self.bot_name_for(sid)
                        sent_name = str(sent_name or "").strip()
                    except Exception:
                        sent_name = ""
                    display_raw = _assistant_prefix(text, sent_name, stamp)
                sent_msgs.append({"role": "assistant", "content": text})
                sent_meta.append((display_raw, sent_ts, raw if has_raw else None))
        rec["sent_display_messages"] = view_of(sent_msgs, bot_name=who, raw_sid=raw_sid)
        for display, (display_raw, sent_ts, original_raw) in zip(rec["sent_display_messages"], sent_meta):
            display["raw"] = display_raw
            if original_raw is not None:
                display["raw_original"] = original_raw
            if sent_ts is not None:
                display["ts"] = sent_ts
        rec["sent_display_messages"] = project_request_view(rec["sent_display_messages"], sent_msgs)
        # 用户 m35137：点柱子看的是「这一轮的窗口快照」，以前不分回复轮 / 决策轮，
        # 一律拿 request_snapshot（history + 打包成一整段的 user_prompt）来拼。
        # 回复轮那 11 条待发在 user_prompt 里就是一段文本，拼进快照只剩 1 条；
        # 这一轮写下的决策注记又不在 history 里，整条丢掉 —— 看着像「丢了 11 条和 1 次注记」。
        # 回复轮改用真正发出去的 messages（历史 + 本轮全部新消息，含注记），决策轮不变。
        _ws_disp = rec["request_snapshot"]["messages"]
        _ws_raw = snapshot_msgs
        if not rec.get("decision_only"):
            _ws_all_disp = rec.get("display_messages") or []
            if len(_ws_all_disp) > len(_ws_disp):
                _ws_disp = _ws_all_disp
            _ws_all_raw = data.get("messages") or []
            if isinstance(_ws_all_raw, list) and len(_ws_all_raw) > len(_ws_raw):
                _ws_raw = _ws_all_raw
        rec["window_snapshot"] = dict(rec["request_snapshot"],
            scope="completed_round", messages=_ws_disp + rec["sent_display_messages"],
            size=len(_ws_raw) + len(sent_msgs), counted=count_messages(_ws_raw + sent_msgs),
            user_counted=count_user_messages(_ws_raw + sent_msgs),
            sent_counted=count_messages(sent_msgs), sent_identity_verified=verified)
        # 用户 m32048 第 1 条：窗口快照也带一份虚线卡，位置按这一份的编号序列重算。
        rec["window_snapshot"]["skipped"] = self._apply_skips(
            rec.get("skipped"),
            rec["window_snapshot"]["messages"],
            float(rec.get("ts") or 0.0),
        )
        # v4.01（用户 m11166 图4）：这一轮的判定本身也得落进快照。平时那条注记只由
        # window/manager.py 的 _with_decision_notes 在出口拼，不写盘、不进快照，
        # 于是点紫柱看这一轮的窗口时，「凭什么这么判」的理由完全看不到。
        if rec.get("decision_only"):
            try:
                snap = rec["window_snapshot"]
                try:
                    _stamp = time.strftime("%H:%M", time.localtime(float(rec.get("ts") or 0)))
                except (ValueError, OverflowError, OSError):
                    _stamp = ""
                _why = str(rec.get("decision_reason") or "").strip()
                _say = str(rec.get("decision_say") or "").strip()
                _head = "建议回复" if rec.get("decision_reply") else "建议不回"
                _body = "[决策记录] " + ((_stamp + " ") if _stamp else "") + _head
                if _why:
                    _body += "（" + _why + "）"
                if _say:
                    _body += "；打算说：" + _say
                _note = {"role": "assistant", "name": "对当前聊天消息的决策建议",
                         "content": _body}
                _shown = project_request_view(view_of([_note], bot_name=who, raw_sid=raw_sid), [_note])
                if _shown:
                    item0 = dict(_shown[0])
                    item0["decnote"] = True
                    item0["name"] = "对当前聊天消息的决策建议"
                    item0["who"] = "对当前聊天消息的决策建议"
                    snap["messages"].append(item0)
                    snap["size"] = int(snap.get("size") or 0) + 1
                    snap["counted"] = int(snap.get("counted") or 0) + 1
            except Exception:
                logger.debug("[multi-decision] 决策注记补进快照失败", exc_info=True)
        return {"ok": True, "record": rec, "trace": trace}

    def _trace_of(self, sid: str, data: dict):
        """找这一轮对应的轨迹：优先 event_id，旧记录按时间就近。"""
        if self.tracer is None or not isinstance(data, dict):
            return None
        eid = str(data.get("event_id") or "")
        if eid:
            # 已有稳定身份时绝不能借邻轮轨迹；没有就是缺失，不是未回复。
            #
            # v5.x（用户 m26664 性能）：轨迹就写在这份请求快照自己身上（tracer 的
            # _rounds_folder 就是 requests/<sid>，read_by_event 也是在这批文件里找
            # event_id），而 event_id 在目录里唯一——所以「这份文件里没有 trace」
            # 时，按 event_id 把整个目录重扫一遍必然还是找不到。以前每遇到一轮
            # 失败记录（没有轨迹）就白扫最多 300 份 JSON，实测 0.7~1.3 秒；列表里
            # 五轮失败就是十秒，页面就「点开要加载好一会」。
            return None
        try:
            ts = float(data.get("ts") or 0)
        except Exception:
            return None
        if ts <= 0:
            return None
        # 同一轮的 trace.started 与请求记录的 ts 只差几十毫秒，30 秒足够，也避免串到邻轮
        best, best_gap = None, 30.0
        for item in self.tracer.recent(sid, limit=40):
            gap = abs(float(item.get("started") or 0) - ts)
            if gap < best_gap:
                best, best_gap = item, gap
        if best is None:
            return None
        trace = self.tracer.read(sid, best["name"])
        if trace and normalize_sid(str(trace.get("sid") or "")) == normalize_sid(sid):
            return trace
        return None

    # 用户 m12833 第 2 条：群里发来的 x.com / twitter.com 推文链接，在正文里只是
    # 一行纯文本——点不了、也看不出里面说了什么。浏览器直连 x.com 取不到内容
    # （要登录、还有 CORS），改由插件后端去只读镜像要一份 JSON，前端画成卡片。
    # 只认「status/<纯数字 id>」这种长相，其余链接一概不代理——否则这里就成了
    # 一个任意 URL 的抓取口子。
    _TWEET_URL_RE = re.compile(
        r"^https?://(?:www\.|mobile\.)?(?:x|twitter)\.com"
        r"/(?:[A-Za-z0-9_]{1,20}/)?status(?:es)?/(\d{6,25})",
        re.I,
    )

    def _tweet_img(self, url: str, sid: str = "") -> str:
        """把推文里的图（头像、视频封面）抓一份存进插件自己的 media_cache。

        浏览器直连 pbs.twimg.com 会卡到超时（用户 m12833 实测：curl 通、页面
        20 秒后报 net::ERR_TIMED_OUT），所以这些图只能由后端代取，前端统一走
        /media 那条同源路。取不到就返回空串，卡片上干脆不画这张图，绝不留裂图。
        """
        u = str(url or "").strip()
        if not u.startswith("https://pbs.twimg.com/"):
            return ""
        import hashlib
        import urllib.request as _ur

        from core.utils.path_utils import get_data_path

        folder = Path(str(get_data_path())) / "plugin_data" / PLUGIN_ID / "media_cache"
        key = "tw_" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
        for ext in (".jpg", ".png", ".webp", ".gif"):
            if (folder / (key + ext)).is_file():
                # 命中旧文件也要补记归属：同一张推文图可能被别的群先抓过。
                try:
                    _owners_remember(folder, key + ext, sid, "twitter")
                except Exception:
                    logger.debug("[multi-decision] 记录推文图归属失败", exc_info=True)
                return "plugin_data/%s/media_cache/%s%s" % (PLUGIN_ID, key, ext)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            req = _ur.Request(
                u,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0 Safari/537.36"),
                    "Referer": "https://x.com/",
                },
            )
            with _ur.urlopen(req, timeout=8) as resp:
                blob = resp.read(4 * 1024 * 1024 + 1)
                ctype = str(resp.headers.get("Content-Type") or "").lower()
            if not blob or len(blob) > 4 * 1024 * 1024:
                return ""
            ext = ".jpg"
            if "png" in ctype:
                ext = ".png"
            elif "webp" in ctype:
                ext = ".webp"
            elif "gif" in ctype:
                ext = ".gif"
            (folder / (key + ext)).write_bytes(blob)
            try:
                _owners_remember(folder, key + ext, sid, "twitter")
            except Exception:
                logger.debug("[multi-decision] 记录推文图归属失败", exc_info=True)
            try:
                prune_other_media(folder)
            except Exception:
                logger.debug("[multi-decision] 裁剪推文图缓存失败", exc_info=True)
            return "plugin_data/%s/media_cache/%s%s" % (PLUGIN_ID, key, ext)
        except Exception as exc:
            logger.debug("[multi-decision] 推文配图没取到 %s：%s", u[:80], type(exc).__name__)
            return ""

    def _tweet_remember(self, out: dict, sid: str) -> None:
        """把已缓存推文卡里的图补记到 media_owners（用户 m24364）。

        服务端缓存命中时不会重新走 _tweet_img，图片归属就丢了；这里按返回的
        avatar / media[].thumb 文件名补一次，同一个群重复调用不会重复加。
        """
        if not sid or not isinstance(out, dict) or not out.get("ok"):
            return
        try:
            from core.utils.path_utils import get_data_path

            folder = Path(str(get_data_path())) / "plugin_data" / PLUGIN_ID / "media_cache"
            names = []
            for rel in [out.get("avatar")] + [one.get("thumb") for one in (out.get("media") or [])]:
                name = Path(str(rel or "")).name
                if name.startswith("tw_") and name not in names:
                    names.append(name)
            for name in names:
                _owners_remember(folder, name, sid, "twitter")
        except Exception:
            logger.debug("[multi-decision] 补记推文图归属失败", exc_info=True)

    @_api("GET", "/tweet")
    async def api_tweet(self, u: str = "", sid: str = ""):
        """一条推文的展示数据：作者、正文、图片、互动数。

        前端把正文里的推文链接换成带 data-tw 的骨架卡，渲染完再回来问一次，
        把卡片填满。取不到就返回 ok=False，前端留着「打不开」的样子，不猜内容。
        """
        import json as _json
        import urllib.request as _ur

        m = self._TWEET_URL_RE.match(str(u or "").strip())
        if not m:
            return {"ok": False, "detail": "不是推文链接"}
        tw_id = m.group(1)
        cache = getattr(self, "_tweet_cache", None)
        if cache is None:
            cache = self._tweet_cache = {}
        prev = cache.get(tw_id)
        now = time.time()
        # 成功的一小时够用（同一条推文整页重绘时别再打一次外网）；
        # 失败的只记 90 秒，免得网络一抖就把「取不到」钉死
        if prev and now - prev[0] < (1800 if (prev[1] or {}).get("ok") else 90):
            self._tweet_remember(prev[1], sid)
            return prev[1]

        def _work():
            req = _ur.Request(
                "https://api.fxtwitter.com/status/%s" % tw_id,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0 Safari/537.36"),
                    "Accept": "application/json",
                },
            )
            with _ur.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read(1500000).decode("utf-8", "replace"))
            t = (data or {}).get("tweet") or {}
            au = t.get("author") or {}
            if not t.get("id"):
                return {"ok": False, "detail": "推文数据不完整"}
            media = []
            for one in ((t.get("media") or {}).get("all") or [])[:4]:
                kind = str(one.get("type") or "")
                if kind == "photo":
                    media.append({"kind": "photo",
                                  "thumb": str(one.get("url") or ""),
                                  "url": str(one.get("url") or ""), "dur": 0})
                elif kind in ("video", "gif"):
                    media.append({"kind": "video",
                                  "thumb": str(one.get("thumbnail_url") or ""),
                                  "url": str(one.get("url") or ""),
                                  "dur": int(float(one.get("duration") or 0))})
            when = ""
            try:
                from email.utils import parsedate_to_datetime

                dt = parsedate_to_datetime(str(t.get("created_at") or ""))
                when = dt.astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                when = str(t.get("created_at") or "")[:16]
            out = {
                "ok": True,
                "id": str(t.get("id") or ""),
                "url": str(t.get("url") or ""),
                "name": str(au.get("name") or ""),
                "handle": str(au.get("screen_name") or ""),
                "avatar": str(au.get("avatar_url") or ""),
                "text": str(t.get("text") or ""),
                "when": when,
                "likes": int(t.get("likes") or 0),
                "retweets": int(t.get("retweets") or 0),
                "replies": int(t.get("replies") or 0),
                "views": int(t.get("views") or 0),
                "media": media,
                "sensitive": bool(t.get("possibly_sensitive")),
            }
            # 图一律由后端代取：浏览器直连 pbs.twimg.com 会卡到超时（用户 m12833
            # 实测 curl 通、页面 20 秒后 ERR_TIMED_OUT），取不到的干脆不给 src。
            out["avatar"] = self._tweet_img(out.get("avatar") or "", sid)
            kept = []
            for one in out.get("media") or []:
                one["thumb"] = self._tweet_img(one.get("thumb") or "", sid)
                if one["thumb"]:
                    kept.append(one)
            out["media"] = kept
            return out

        try:
            loop = asyncio.get_running_loop()
            out = await loop.run_in_executor(None, _work)
        except Exception as exc:
            out = {"ok": False, "detail": "取不到推文（%s）" % type(exc).__name__}
        if len(cache) > 300:
            cache.clear()
        cache[tw_id] = (now, out)
        return out

    @_api("GET", "/media")
    async def api_media(self, path: str = "", sid: str = ""):
        """把 session_media 里的图片原样吐出去，页面才能显示别人发的表情和图片。

        path 是框架写进 content 的相对路径（相对 data 目录）。
        只允许读 data/session_media 和插件自己的 data/plugin_data/<插件>/media_cache
        这两个目录，别的一概拒绝。
        """
        rel = str(path or "").strip().replace("\\", "/")
        if not rel:
            return {"ok": False, "detail": "没有指定图片"}
        try:
            from pathlib import Path

            from fastapi import Response
            from core.utils.path_utils import get_data_path, is_within_directory
        except Exception:
            return {"ok": False, "detail": "框架没有提供路径工具"}
        try:
            # get_data_path() 在框架里可能给 str 也可能给 Path（别处都先 str() 过），
            # 这里统一包成 Path 再拼，否则 /media 每次都掉进下面的 except 返回 JSON。
            data_root = Path(get_data_path())
            # 除了框架的 session_media，还放行插件自己留的图片副本目录：
            # 框架把旧图清掉以后页面仍要能显示（见 window/manager.py public_media）。
            roots = [
                (data_root / "session_media").resolve(),
                (data_root / "plugin_data" / PLUGIN_ID / "media_cache").resolve(),
            ]
            target = (data_root / rel).resolve()
        except Exception:
            return {"ok": False, "detail": "路径解析失败"}
        if not any(is_within_directory(root, target) for root in roots):
            return Response(status_code=404, content=b"")
        if not target.is_file():
            # 2026-11-01（用户 m05804）：旧记录里可能留着框架已经清掉的
            # session_media 原路径，而插件 media_cache 还有副本。先让 public_media
            # 按索引/同名副本找一次，找到就改读副本；不能因为原路径没了就画
            # 「图片已清理」。仍然找不到才 404。
            try:
                got = public_media(rel, sid=sid)
            except Exception as exc:
                logger.debug("[multi-decision] 媒体副本回退失败：%s: %s", type(exc).__name__, exc)
                got = {}
            alt = str((got or {}).get("path") or "").strip().replace("\\", "/")
            if alt and alt != rel:
                try:
                    alt_target = (data_root / alt).resolve()
                except Exception:
                    alt_target = None
                if (alt_target is not None
                        and any(is_within_directory(root, alt_target) for root in roots)
                        and alt_target.is_file()):
                    target = alt_target
        if not target.is_file():
            # 404 而不是 200 + JSON：<img> 才认得出加载失败，去画占位；
            # 以前这里返回 JSON，页面上就是一张裂图 + 一大片空白（用户 m01008 图1）。
            try:
                return Response(status_code=404, content=b"")
            except Exception:
                return {"ok": False, "detail": "图片不存在"}
        try:
            if target.stat().st_size > MAX_MEDIA_BYTES:
                logger.warning(f"[multi-decision] 图片超过 {MAX_MEDIA_BYTES} 字节，拒绝读取：{target.name}")
                return Response(status_code=413, content=b"")
            # 读盘放到线程里：这是 async handler，同步读大文件会把整个事件循环卡住，
            # 机器人跟着不回复（审查报告）。
            # 用户 m08158：这里以前少了 import asyncio，一调用就 NameError，
            # 被下面的 except 吞成「图片读取失败」，页面上每张图都显示「图片已清理」。
            # v4.88：局部 import 全删了，统一用模块顶部那一个（局部 import 会让
            # asyncio 变成整个函数的局部名，条件分支没走到时后面就 UnboundLocalError）。

            blob = await asyncio.to_thread(target.read_bytes)
        except Exception as exc:
            logger.warning(f"[multi-decision] 读取图片失败 {target.name}：{type(exc).__name__}: {exc}")
            logger.debug("[multi-decision] 读取图片失败", exc_info=True)
            return {"ok": False, "detail": "图片读取失败（%s）" % type(exc).__name__}
        mime = mimetypes.guess_type(target.name)[0]
        if not mime:
            # 归档图（session_media/archive/<内容哈希>）没有后缀，缓存副本也可能是
            # 老版本留下的 .img 名字，只能看文件头才知道是 png 还是 gif（用户 m11618）。
            mime = _sniff_image_mime(target) or "image/jpeg"
        return Response(
            content=blob,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # v4.49（用户 m25038 第 5 项）：新版 QQ 表情库（gitee 那套）在浏览器里直连会被
    # 防盗链挡成 403，页面上永远退回老版 qzone 小黄脸——用户嫌「这个老的难看」。
    # 只能由插件后端去取一份再吐给页面；取到的图缓存在 media_cache/emoji/ 下，只下过一次。
    #
    # v4.52（用户 m16262 图3/图4）：gitee 那套只到 332，344「大怨种」这类 2023 年以后
    # 的新表情取不到，页面只能退回老版 qzone——而 qzone 的 e{id+100} 只在 0~211 这些老脸
    # 上跟 QQ 编号差 100，再往上就是另一套图（344 → e444 是 404、e344 是「面无表情」），
    # 画出来是张完全不相干的脸。改成按编号依次试三档，都取不到才 404 让页面退回文字：
    #   ① gitee 新版库：0~287 是「编号@2x.gif」，288~332 是「编号.哈希.png」（要查表）
    #   ② koishijs/QFace：编号 0~507 全覆盖，每个编号一个目录，apng 动图 / png 静态图
    _EMOJI_BASE = "https://gitee.com/yxkfw/emoji-emotions/raw/master/QQ/gif/"
    _EMOJI_HASH = {
        "277": "277.164cfcbd3a9ed49e213c.png", "288": "288.b9c3521b5a275ec1f99b.png", "289": "289.fea51cee3cee3f64e22b.png",
        "293": "293.dd95c4897fb462da9bf9.png", "294": "294.f7da3a3e27e120f5a554.png", "295": "295.4c0882fcf9326dd3b886.png",
        "296": "296.147ec1f57412a25b686b.png", "297": "297.f6389ef2a0e40c258b4a.png", "298": "298.53b6380a3fd0d1f33f0f.png",
        "299": "299.f88131c1a25c32c74a42.png", "300": "300.a01a1fa3f0888a542f36.png", "301": "301.f03fb11b30a1134a44f7.png",
        "303": "303.7b59cd0db92c1dab250f.png", "304": "304.78267a6de9c4286adc50.png", "305": "305.5f4fb2c80e5e8c2d0879.png",
        "311": "311.25aec566822eb85e5466.png", "312": "312.8a3bb90b393b6a6c08d9.png", "313": "313.6978dfe67d54c4f473bc.png",
        "314": "314.854272f991ebf01ea4ad.png", "315": "315.afcfbd9c97efa723356a.png", "316": "316.0472d78bd4ef2fb58e85.png",
        "317": "317.bde106ab796218d610c6.png", "318": "318.573b9dfceaed34db3ecc.png", "319": "319.626ae5579cd5bb58ed2a.png",
        "320": "320.b42fcc8f8b133798e0e3.png", "321": "321.15379466db718b3cb1d5.png", "322": "322.a1b2dd1f0423a4c78383.png",
        "323": "323.7b71b1a44244d62294d5.png", "324": "324.bc33e8fc449f270277ba.png", "325": "325.aa5af7e2d0ae8cc4dfff.png",
        "326": "326.f5d0e503a33d09c881cf.png", "327": "327.3a61e9f0dfca7ce3f34d.png", "328": "328.cc59a3ccd4dd6bd03808.png",
        "329": "329.c8b9312b18fb30755dd5.png", "330": "330.1b8ef33119f538852eae.png", "331": "331.701c7029724f91184a92.png",
        "332": "332.7777dfcc2dbf80a2024c.png",
    }
    _EMOJI_QFACE = ("https://cdn.jsdelivr.net/gh/koishijs/QFace@master/"
                    "public/assets/qq_emoji/%d/%s/%d.png")
    _EMOJI_QFACE_RAW = ("https://raw.githubusercontent.com/koishijs/QFace/master/"
                        "public/assets/qq_emoji/%d/%s/%d.png")
    _EMOJI_MAX = 2 * 1024 * 1024

    @staticmethod
    def _emoji_mime(data):
        """看文件头认图，别拿 .gif 的名字当准（新版库里 288 以后全是 png）。"""
        if not data:
            return ""
        if data.startswith(b"GIF8"):
            return "image/gif"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return ""

    @classmethod
    def _emoji_urls(cls, n):
        """按优先级列出这个编号可能的表情图地址（前面取不到才试后面的）。"""
        out = [cls._EMOJI_BASE + "%d@2x.gif" % n]
        hashed = cls._EMOJI_HASH.get(str(n))
        if hashed:
            out.append(cls._EMOJI_BASE + hashed)
        for tmpl in (cls._EMOJI_QFACE, cls._EMOJI_QFACE_RAW):
            out.append(tmpl % (n, "apng", n))
            out.append(tmpl % (n, "png", n))
        return out

    async def _emoji_fetch(self, n):
        """依次试各图源，返回 (字节, mime)；全都不行返回 (b"", "")。"""
        cap = self._EMOJI_MAX

        def _grab(url):
            import urllib.request

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
                    "Referer": "https://gitee.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read(cap + 1)
            if not data or len(data) > cap:
                return b""
            return data

        for url in self._emoji_urls(n):
            try:
                data = await asyncio.to_thread(_grab, url)
            except Exception as exc:
                logger.debug(f"[multi-decision] 取表情 {n} 失败（{url}）：{type(exc).__name__}: {exc}")
                continue
            mime = self._emoji_mime(data)
            if data and mime:
                return data, mime
        return b"", ""

    @_api("GET", "/emoji")
    async def api_emoji(self, id: str = ""):
        """按表情编号取表情图；取不到返回 404，页面自己退回文字。"""
        try:
            n = int(str(id or "").strip())
        except Exception:
            n = -1
        if n < 0 or n > 999:
            return {"ok": False, "detail": "表情编号不对"}
        try:
            from pathlib import Path

            from fastapi import Response
            from core.utils.path_utils import get_data_path
        except Exception:
            return {"ok": False, "detail": "框架没有提供路径工具"}
        target = Path(get_data_path()) / "plugin_data" / PLUGIN_ID / "media_cache" / "emoji" / ("e%d.gif" % n)
        blob = b""
        try:
            if target.is_file() and target.stat().st_size > 0:
                blob = await asyncio.to_thread(target.read_bytes)
        except Exception:
            blob = b""
        mime = self._emoji_mime(blob)
        if not mime:
            blob, mime = await self._emoji_fetch(n)
            if blob:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(blob)
                except Exception:
                    pass
        if not blob or not mime:
            try:
                return Response(status_code=404, content=b"", headers={"Cache-Control": "no-store"})
            except Exception:
                return {"ok": False, "detail": "没有这个表情"}
        return Response(
            content=blob,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=604800"},
        )
