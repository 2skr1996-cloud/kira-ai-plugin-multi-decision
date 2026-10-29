"""多样决策 —— KiraAI 群聊触发判定与上下文窗口。

决策模块接管群聊的「要不要回 / 什么时候回」；
窗口模块接管上下文历史（自己存、增量合并、真空闲才裁）。
WebUI 页面挂在左侧边栏「多样决策」：群聊 / 私聊 / 上下文（观察窗）。
设置单独一页（/settings），从日志页右上角的齿轮进去。

装饰器必须写在本文件里：框架靠模块所在目录的 manifest.json 反查 plugin_id，
子模块里定义的钩子会找不到插件身份。
"""

from __future__ import annotations

import copy
import ctypes
import os
import json
import mimetypes
import re
import time
from pathlib import Path

from core.plugin import BasePlugin, logger, on, Priority, register

from .decision.engine import DecisionEngine, _norm_phrase
from .decision.output_guard import dedupe_reply, wrap_plain_reply
from .state.adaptive import AdaptiveStats
from .state.aliases import Aliases
from .state.band_stats import BandStats
from .state.band_rhythm import BandRhythm
from .state.band_tune import BandTuner
from .state.decision_phase import (
    DEFAULT_CHAT_RULES,
    DEFAULT_DECISION_PROMPT,
    TOOL_HEAD,
    DecisionPhase,
    options_from_config,
)
from .state.decision_log import DecisionLog
from .state.hidden import Hidden
from .state.overrides import Overrides
from .state.pricing import PRICE_UNIT, Pricing
from .state.reply_prompt import PERSONA_NAME, ReplyPrompt
from .state.prompt_layout import PromptLayouts
from .state.request_log import RequestLog, payload_metrics
from .state.retry_history import RetryHistory
from .state.round_stats import merge_rounds
from .state.session_registry import SessionRegistry
from .state.sids import normalize_sid
from .state.state_store import StateStore
from .state.summaries import Summaries
from .state.tracer import Tracer
from .window.manager import (
    WindowManager,
    view_of,
    media_list,
    public_media,
    _media_by_ids,
    _assistant_prefix,
    _sniff_image_mime,
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


def _sid_ok(sid: str) -> bool:
    """sid 能不能安全地拿去拼路径。空串、含路径分隔符、含 ".."、超长一律否。"""
    s = str(sid or "").strip()
    if not s or not _SID_RE.match(s):
        return False
    if "/" in s or "\\" in s or ".." in s:
        return False
    return True

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


def _disk_usage() -> dict:
    """插件自己写出来的日志/缓存占了多少磁盘（用户 m30009 图5：要目录占用，不是进程内存）。"""
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
            if os.path.isdir(p):
                for dirpath, _dirs, files in os.walk(p):
                    for f in files:
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
            else:
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
            total += size
            # v3.72（用户 m24260 第 11 条）：也带上字节数——几十 KB 的目录（窗口快照）
            # 四舍五入到一位小数会变成 0.0 MB，前端要靠 bytes 显示成 KB。
            parts.append({"name": name, "mb": round(size / 1048576.0, 1), "bytes": size})
    except Exception:
        return {}
    parts.sort(key=lambda x: -x.get("bytes", 0))
    return {"total_mb": round(total / 1048576.0, 1), "parts": parts[:8]}


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
    "decision_persona",
    "decision_chat_rules",
    "decision_prompt",
    "decision_tools",
    "decision_model",
    "decision_fallback_model",
    "decision_retry",
    "decision_retry_delay",
    "decision_fail_switch",
    "decision_fallback_cooldown",
    "decision_timeout",
    "decision_fail_action",
    "decision_tool_rounds",
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
    "silent_trigger_enabled",
    "silent_trigger_minutes",
    "bands_enabled",
    "rest_silent_enabled",
    "rest_silent_minutes",
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
    "window_enabled",
    "window_keep",
    "history_images",
    "window_trim_threshold",
    "protect_tool_groups",
    "summary_enabled",
    "summary_max_chars",
    "summary_line_chars",
    # v3.60（用户 m18059 第 4 条）：缓存保留线也按会话独立——大群刷得快、小群
    # 一周没人说话，用一套全局上限总有一头不对。这里只放「每会话」的口径，
    # 全局值仍是默认值；清理时按各自有效的这份裁。
    "trace_keep",              # 每会话请求快照/轨迹份数
    "requests_keep_days",      # 请求快照保留天数（0 = 不限）
    "decisions_keep_days",     # 消息日志保留天数
    "windows_keep_days",       # 窗口快照保留天数
    "windows_keep",            # 窗口快照份数（0 = 不限）
    "media_cache_limit",       # 图片缓存张数（每个群组各自的上限，非全目录总上限）
    "media_cache_keep_days",   # 图片缓存保留天数
    # v3.66（用户 m19536 第 4 条）：上限拆成「群聊默认 / 私聊默认」两套——私聊的人少、
    # 刷得慢，用群聊那一套等于白占盘。这四个是私聊默认值，群聊仍用上面那些原键。
    "media_cache_limit_dm",    # 图片缓存张数（私聊默认）
    "trace_keep_dm",           # 每会话请求快照份数（私聊默认）
    # v4.00（用户 m10782）：决策快照单独一类缓存，份数线自己一套
    "decision_keep",           # 每会话决策快照份数
    "decision_keep_dm",        # 每会话决策快照份数（私聊默认）
    "windows_keep_dm",         # 窗口快照份数（私聊默认）
    "summaries_keep_dm",       # 中期摘要上限（私聊默认）
)

# 整数项的取值范围，覆盖值也要守规矩（前端提交什么都有可能）。
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_BOUNDS = {
    "trigger_threshold": (1, None),
    "trigger_probability": (0, 100),
    "adaptive_step": (1, None),
    "adaptive_tune_minutes": (1, 120),
    "adaptive_rhythm_sigma": (0, 120),
    # v3.91（用户 m05749）：决策的重试 / 切换 / 超时也要守住范围，
    # 别让人填出 0.001 秒这种把主模型必定判死的值。
    "decision_retry": (0, 5),
    "decision_retry_delay": (0, 60),
    "decision_fail_switch": (1, 10),
    "decision_fallback_cooldown": (0, 86400),
    "decision_timeout": (5, 600),
    "decision_tool_rounds": (0, 5),
    "jitter_seconds": (0, None),
    "jitter_max_rounds": (1, 20),
    "burst_merge_seconds": (0, None),
    "short_msg_chars": (0, None),
    "silent_trigger_minutes": (1, 1440),
    "rest_silent_minutes": (1, 1440),
    "rest_probability": (0, 100),
    "band_rest_probability": (0, 100),
    # v3.72（用户 m24260 第 9 条）：回看改成「周」，最少 4 周
    "adaptive_weekday_window_weeks": (4, 52),
    "quota_max_per_hour": (1, None),
    "quota_max_per_day": (1, None),
    "backoff_start_rejects": (1, None),
    "backoff_base_seconds": (1, None),
    "backoff_max_seconds": (1, None),
    "backoff_bypass_count": (1, None),
    "retry_max": (0, 10),
    "retry_delay": (0, 600),
    "watchdog_seconds": (30, 600),
    "window_keep": (1, None),
    "history_images": (0, 500),
    "window_trim_threshold": (1, None),
    # v3.52（用户 m08226 第 7 条）：裁切缓冲，0 = 关掉强制裁切
    "window_buffer": (0, None),
    "media_cache_limit": (1, 100000),
    # v3.66（用户 m19536 第 4 条）：私聊那一套默认值，范围跟群聊一致；0 = 不限的项保留 0
    "media_cache_limit_dm": (0, 100000),
    "trace_keep_dm": (1, 1000),
    "decision_keep_dm": (1, 1000),
    "windows_keep_dm": (0, 1000),
    "summaries_keep_dm": (0, 1000),
    # 用户 m11091 第 11 条：图片缓存的两条保留线，0 = 不限
    "media_cache_keep_days": (0, None),
    # 用户 m18059 第 3/4 条：缓存保留线可以单群覆盖，覆盖值一样要守住范围
    "trace_keep": (1, 1000),
    "decision_keep": (1, 1000),
    "windows_keep": (0, 1000),
    # 用户 m13708 第 3/4 条：每类缓存各自的保留天数 / 上限，这类全局键同样要守住范围。
    # 天数类一律 0-3650（0 = 不限）；消息日志/窗口快照是按天文件，至少要留 1 天。
    "requests_keep_days": (0, 3650),
    # v3.66（用户 m19536 第 3 条）：消息日志与会话统计是自适应阈值的原料，最少留 56 天，
    # 只能往高调——调低了自适应就成天在「样本不够」里打转。
    "decisions_keep_days": (56, 3650),
    "windows_keep_days": (1, 3650),
    "summaries_keep": (0, 1000),
    "summaries_keep_days": (0, 3650),
    "stats_keep_days": (56, 3650),
    "summary_max_chars": (60, 4000),
    "summary_line_chars": (8, 200),
}

_ENUMS = {
    "trigger_mode": ("count", "probability", "adaptive"),
}


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


def _jsonl_usage(folder, suffix: str = ".jsonl") -> tuple:
    """数一个目录下 *.jsonl 的「记录条数」（非空行）与总字节，返回 (count, bytes)。

    用户 m13708 第 5 条：消息日志/窗口快照是一天一个文件、多会话混写，数文件
    个数会变成「有几天记录」——用户看到的就是「一个群那么多快照只有 5 份」。
    所以缓存页这类一律按非空行数算条数。
    """
    count = 0
    size = 0
    try:
        items = list(Path(folder).glob("*" + suffix))
    except Exception:
        return (0, 0)
    for path in items:
        try:
            size += int(path.stat().st_size or 0)
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
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
# 前端已把这两项从表单撤下 / 标为「已锁定」，这里再兜一层，防止旧配置文件、
# 单群 overrides 或直接调接口把它关掉。
_LOCKED_ON = frozenset({"window_enabled", "dynamic_to_tail"})


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


class MultiDecisionPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        # 进阶决策（v3.91，用户 m05749）：初始化时的配置快照，给 _decision_get 兜底用
        # （运行中的 ctx.config 拿不到时退回这里）。
        self._cfg_snapshot = dict(cfg) if isinstance(cfg, dict) else {}
        self._takeover_checked_at = 0.0
        self._takeover_owned = False
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
            4, min(52, _as_int(cfg.get("adaptive_weekday_window_weeks"), 8))
        )
        # v3.28（用户 m47321）：时段曲线。24 小时切 48 格，每段一个触发概率。
        # 空串 = 没设置（界面按「未覆盖的时间用触发概率」显示）。目前只存不用，
        # 等动态模式方案定下来再接进判定。
        # v3.33（用户 m51068 第 8/9 条）：曲线本身变成开关——开着才参与判定，
        # 并且接管普通模式的阈值/概率与休息时段（曲线已经把一天切成不同概率）。
        # v3.39（用户本轮）：只接管回复概率，休息时段的兜底窗口照用。
        self.bands_enabled = _as_bool(cfg.get("bands_enabled"), False)
        self.time_bands = _normalize_bands(cfg.get("time_bands"))

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
        # 用户 m09403：必回默认关——睡觉时被叫到就走普通判定（该不该回看概率 / 模型），
        # 不再跳过判断硬回；触发仍默认开，跟改之前一样算一次触发。
        self.rest_wake_trigger = _as_bool(cfg.get("rest_wake_trigger"), True)
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
        # v3.44（用户本轮第 2 条）：连发合并同样加开关，默认关——关着时同一人连发
        # 每条都照常计数；开了才按 burst_merge_seconds 合并。
        self.burst_merge_enabled = _as_bool(cfg.get("burst_merge_enabled"), False)
        self.burst_merge_seconds = max(0, _as_int(cfg.get("burst_merge_seconds"), 15))
        self.short_msg_chars = max(0, _as_int(cfg.get("short_msg_chars"), 5))

        # v3.33（用户 m51068 第 11 条）：字数阈值下线，换成「整条就是清单里的短语」
        # 才不计入触发——「哈哈」「草」照样能挡，但「好」「嗯」这种真在接话的
        # 短句不再被一起吞掉。
        self.phrase_filter_enabled = _as_bool(cfg.get("phrase_filter_enabled"), True)
        self.phrase_filter_list = _normalize_phrases(cfg.get("phrase_filter_list"))

        # v3.33（第 6 条）：静默兜底等待时长重新开放成设置项，默认 3 分钟。
        self.silent_trigger_enabled = _as_bool(cfg.get("silent_trigger_enabled"), True)
        self.silent_trigger_minutes = max(1, _as_int(cfg.get("silent_trigger_minutes"), 3))
        # v3.33（第 10 条）：休息时段单独一套静默兜底，默认不开。
        self.rest_silent_enabled = _as_bool(cfg.get("rest_silent_enabled"), False)
        self.rest_silent_minutes = max(1, _as_int(cfg.get("rest_silent_minutes"), 3))

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

        self.rest_mode_enabled = _as_bool(cfg.get("rest_mode_enabled"), False)
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

        self.backoff_enabled = _as_bool(cfg.get("backoff_enabled"), True)
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

        self.window_keep = max(1, _as_int(cfg.get("window_keep"), 30))
        # v3.52（用户 m08226 第 7 条）：一直没触发请求时，窗口攒到「保留 + 缓冲」
        # 就强制裁一次；0 = 关掉这个强制裁切，只按阈值走。
        self.window_buffer = max(0, _as_int(cfg.get("window_buffer"), 10))
        # 图片缓存上限（用户 m10749 第 5 条）：插件替群友图片留的副本，**每个群组各自**
        # 留几张（用户本轮：「全局设置的是每个群组的上限 不是总的上限，总的上限=全局设置*
        # 群组数量」）。所以这个值不是整个缓存目录的天花板，实际总配额 = 本值 × 群组数，
        # 由 window/manager.py 的 _prune_media_cache 按群组桶分别裁。
        # 用户 m06046 图2：600 张（那时是全目录共享一份）两天就攒满，每次收新图都会顺手
        # 删掉一张旧的，记录里刚出现过的图跟着没。默认放到 2000，配合 2 小时的保护期。
        self.media_cache_limit = max(1, _as_int(cfg.get("media_cache_limit"), 2000))
        # 用户 m11091 第 11 条：图片缓存只留最近 N 天 / 最多 N MB，0 = 不限
        self.media_cache_keep_days = max(0, _as_int(cfg.get("media_cache_keep_days"), 56))
        # v3.66（用户 m19536 第 5 条）：「缓存容量上限」这个总上限撤掉了——群聊默认 +
        # 私聊默认两套加起来就是总上限。配置里残留的旧值不再读取，属性留 0 给老代码兜底。
        self.media_cache_max_mb = 0
        # 只保留最近几张原图，更早的换成 [图片]。框架发图前会把超过 1MB 的图压缩，
        # 所以正常群聊几乎不会摸到上限；设 128 张和 MaiBot 的 max_image_num 一致，
        # 好处是历史内容几乎不变，前缀缓存不会因为「图被换掉」而断。
        self.history_images = max(0, _as_int(cfg.get("history_images"), 128))
        self.window_trim_threshold = max(1, _as_int(cfg.get("window_trim_threshold"), 60))
        # v3.85（用户 m04418 第 2 条）：撤掉「重启后回填条数」这条设置——群聊窗口
        # 热重载由寄存处接回、私聊另有 window_dm.json 存档，都不需要从框架记忆里回填。
        # 冷启动一律不灌历史，见 window/manager.py 的 collect()。
        self.protect_tool_groups = _as_bool(cfg.get("protect_tool_groups"), True)
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
        self.trace_keep_dm = _bounded_int(
            "trace_keep_dm", cfg.get("trace_keep_dm"), 50
        )
        # 用户 m13708 第 3/4 条：每一类缓存的保留天数都做成设置项，语义统一
        # 「0 = 不限」（消息日志/窗口快照按天文件，至少要留 1 天）。默认值和以前
        # 的实际行为对齐，升级后不会突然删数据。
        # v3.66（用户 m19536 第 1 条）：保留天数默认统一 56 天（0 仍可手动填 = 不限）
        self.requests_keep_days = _bounded_int(
            "requests_keep_days", cfg.get("requests_keep_days"), 56
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
        self.windows_keep = _bounded_int(
            "windows_keep", cfg.get("windows_keep"), 300
        )
        self.windows_keep_dm = _bounded_int(
            "windows_keep_dm", cfg.get("windows_keep_dm"), 60
        )
        # v4.00（用户 m10782）：决策快照单独一类缓存，份数线自己一套。
        self.decision_keep = _bounded_int(
            "decision_keep", cfg.get("decision_keep"), 100
        )
        self.decision_keep_dm = _bounded_int(
            "decision_keep_dm", cfg.get("decision_keep_dm"), 50
        )
        # 中期摘要（用户第 5 条：早期记录可以先清掉）：最多留多少个会话的摘要，
        # 以及最多留多少天，0 = 不限。
        self.summaries_keep = _bounded_int(
            "summaries_keep", cfg.get("summaries_keep"), 0
        )
        self.summaries_keep_days = _bounded_int(
            "summaries_keep_days", cfg.get("summaries_keep_days"), 56
        )
        self.summaries_keep_dm = _bounded_int(
            "summaries_keep_dm", cfg.get("summaries_keep_dm"), 0
        )
        # 会话统计（自适应阈值 / 用量账本）文件保留天数。v3.66（用户 m19536 第 1/3 条）：
        # 默认 56 天、下限锁 56，和消息日志一样只能往高调。
        self.stats_keep_days = _bounded_int(
            "stats_keep_days", cfg.get("stats_keep_days"), 56
        )
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
        # 用户 m13708 第 4/5 条：摘要也按「会话数上限 + 保留天数」剪
        self.summaries = Summaries(
            data_dir,
            keep=self.summaries_keep,
            keep_days=self.summaries_keep_days,
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
        )
        self.retry_history = RetryHistory(data_dir)
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
        try:
            if self.window is not None:
                self.window.shutdown()
            if self.request_log is not None:
                self.request_log.shutdown()
            if self.stats is not None:
                self.stats.shutdown()
            if self.band_rhythm is not None:
                self.band_rhythm.flush()
        except Exception:
            logger.exception("[multi-decision] 关闭窗口模块失败")
        self._release_builtin_chat()
        logger.info("[multi-decision] terminated")

    def _release_builtin_chat(self):
        """插件被停用/卸载时把 default-chat 放开，避免群里彻底没人回。

        只恢复「我们置的位」：如果加载时 default-chat 本来就是 _terminating=True
        （它自己被用户停用），那不是我们干的，动了等于擅自把它激活。
        """
        if not self._takeover_owned:
            return
        try:
            instance = self.ctx.get_plugin_inst("default-chat")
        except Exception:
            instance = None
        if instance is None:
            self._takeover_owned = False
            return
        try:
            instance._terminating = False
            logger.info("[multi-decision] 已放开 default-chat（_terminating=False）")
        except Exception:
            logger.exception("[multi-decision] 放开 default-chat 失败")
        finally:
            self._takeover_owned = False

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
        if getattr(instance, "_terminating", False):
            logger.info("[multi-decision] default-chat 已停用，无需接管")
            return
        try:
            instance._terminating = True
            self._takeover_owned = True
            self._takeover_checked_at = time.time()
            logger.info("[multi-decision] 已接管 default-chat（置 _terminating=True）")
        except Exception:
            logger.exception("[multi-decision] 接管 default-chat 失败")

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
        if getattr(instance, "_terminating", False):
            return
        try:
            instance._terminating = True
            self._takeover_owned = True
            logger.info("[multi-decision] default-chat 被复位，已重新接管")
        except Exception:
            logger.exception("[multi-decision] 重新接管 default-chat 失败")

    # ------------------------------------------------------------ 钩子

    @on.im_message(priority=Priority.HIGH)
    async def _on_im_message(self, event):
        await self.engine.handle_message(event)

    @on.llm_request(priority=Priority.MEDIUM)
    async def _on_llm_request(self, event, request, tag_set):
        # 仅抄原模板，不渲染尚未绑定的 format；素材严格按当前 sid 隔离。
        layouts = getattr(self, "prompt_layouts", None)
        if layouts is not None:
            layouts.capture_native(request, self._sid_of(event))
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
        await self._trace_begin(event, request)

    @on.llm_response(priority=Priority.MEDIUM)
    async def _on_llm_response(self, event, llm_resp):
        # 先记轨迹：轨迹里留模型的原样输出，收口之后也能看出是它自己写了两遍。
        if self.tracer is not None:
            try:
                self.tracer.step(event, llm_resp)
            except Exception:
                logger.exception("[multi-decision] 轨迹记录（响应）异常（已吞掉）")
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

    @on.tool_result(priority=Priority.LOW)
    async def _on_tool_result(self, event, tool_result):
        if self.tracer is None:
            return
        try:
            self.tracer.tool(event, tool_result)
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（工具）异常（已吞掉）")

    @on.message_sent(priority=Priority.LOW)
    async def _on_message_sent(self, event, action, result):
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
        await self.engine.handle_final(event, final_result)
        if self.tracer is None:
            return
        try:
            self.tracer.finish(event, final_result)
        except Exception:
            logger.exception("[multi-decision] 轨迹记录（收尾）异常（已吞掉）")

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
            self.tracer.begin(event, request, trigger_at=trig, debounce=jit, decision=dec)
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
            "trace_keep",
            "decision_keep",
            "windows_keep",
            "summaries_keep",
            "trigger_probability",
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
        # v3.66：上限项在私聊里默认走「私聊默认」那一套（群聊两成），群聊仍用原键。
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
        _step(
            "aliases",
            lambda: (self.aliases.set(sid, "") or 1)
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
    def _decision_get(self, key, default=None):
        """读进阶决策的配置项。

        先读运行中的 ctx.config（框架里存完就生效，不用热重载插件），拿不到再退回
        初始化时的 cfg 快照。这些键都是全局键、不按群覆盖——决策提示词讲的是整套
        流程的规则，按群各存一份没人维护得过来。
        """
        try:
            cfg = getattr(self.ctx, "config", None)
            if isinstance(cfg, dict) and key in cfg:
                return cfg[key]
        except Exception:
            pass
        try:
            snap = getattr(self, "_cfg_snapshot", None) or {}
            if key in snap:
                return snap[key]
        except Exception:
            pass
        return default

    def decision_options(self):
        """把当前配置摊成 DecisionOptions（每轮现读，改完立即生效）。

        用户 m02159 图3：提示词页「工具说明」段里勾的工具才是真正注入、也允许模型调用的
        那份；勾过就以它为准，从没勾过（None）才沿用设置页里那份。
        """
        try:
            opt = options_from_config(lambda k, d=None: self._decision_get(k, d))
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
                    out.append({
                        "value": "%s:%s" % (pid, mid),
                        "label": "%s / %s" % (pname, str(item.get("model_name") or mid)),
                        "kind": kind,
                    })
        defaults = data.get("models") if isinstance(data.get("models"), dict) else {}
        fast = str(defaults.get("default_fast_llm") or "")
        main = str(defaults.get("default_llm") or "")
        for it in out:
            it["fast"] = bool(fast) and it["value"] == fast
            it["main"] = bool(main) and it["value"] == main
        return out

    def _decision_messages(self, sid: str) -> list:
        """决策要读的那份聊天记录（窗口快照）。取不到就当空。"""
        try:
            return self.window.snapshot(sid) or []
        except Exception:
            logger.exception("[multi-decision] 取窗口快照失败（决策按空记录处理）")
            return []

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
        opt = self.decision_options()
        layouts = getattr(self, "prompt_layouts", None)
        if layouts is not None:
            compiled = await layouts.assemble_decision(sid, opt)
            msgs = self._decision_messages(sid)
            tools = dp.pick_tools(opt.tools)
            return dict(compiled, sid=sid, transcript=dp.transcript(msgs),
                        tools=[{"name": str(getattr(t, "name", "")), "description": str(getattr(t, "description", ""))} for t in tools],
                        persona_name=opt.persona, messages=len(msgs), identity=bool(layouts.identity_text(sid)))
        persona = ""
        if opt.persona:
            try:
                persona = await dp.persona_text(opt.persona)
            except Exception:
                logger.exception("[multi-decision] 预览取人格失败")
        tools = dp.pick_tools(opt.tools)
        help_text = dp.tools_help(tools)
        if tools:
            help_text = (TOOL_HEAD % max(1, opt.tool_rounds)) + "\n" + help_text
        ident = self._decision_identity()
        parts = dp.build_system(opt, persona, help_text, ident)
        msgs = self._decision_messages(sid)
        return {
            "ok": True,
            "sid": sid,
            "parts": parts,
            "system": "\n\n".join(parts),
            "transcript": dp.transcript(msgs),
            "tools": [{"name": str(getattr(t, "name", "")),
                       "description": str(getattr(t, "description", ""))} for t in tools],
            "persona": persona,
            "persona_name": opt.persona,
            "messages": len(msgs),
            "identity": bool(ident),
        }

    @_api("GET", "/prompt-layout")
    async def api_prompt_layout(self, kind: str = "decision", sid: str = ""):
        try:
            return await self.prompt_layouts.payload(kind, str(sid or "").strip())
        except (ValueError, TypeError) as exc:
            return {"ok": False, "code": "INVALID_LAYOUT", "error": str(exc)}

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

    @_api("POST", "/prompt-tools")
    async def api_prompt_tools(self, body: dict):
        """提示词页「工具说明」段里的勾选（用户 m02159 图3）：勾了才注入、才允许模型调用。"""
        try:
            names = self.prompt_layouts.set_tools(body.get("names") or [])
            return {"ok": True, "names": names, "layout_revision": self.prompt_layouts.revision()}
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
        opt = self.decision_options()
        return {
            "ok": True,
            "sid": sid,
            "personas": personas,
            "tools": dp.tool_catalog(),
            "models": self._decision_models(),
            "config": {
                "persona": opt.persona,
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
            got = await self.decision_phase.decide(
                sid, self._decision_messages(sid), self.decision_options())
        except Exception as exc:
            logger.exception("[multi-decision] 决策测试失败")
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc), "result": {}}
        return {"ok": True, "result": got}

    # 注意：/decisions 这个路由老早被「消息日志」占了（下面那个 api_decisions），
    # 同名再挂一个只会被后注册的盖掉——日志里能看到 /decisions 注册了两遍。
    # 决策记录换成 /decision-rounds（v3.91，用户 m05749）。
    @_api("GET", "/decision-rounds")
    async def api_decision_rounds(self, sid: str, limit: int = 20):
        """决策记录：跟回复记录同一套模板，只是 ① 换成决策用的提示词。"""
        import asyncio

        return await asyncio.to_thread(self._requests_payload, sid, limit, "decision")

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
    async def api_overview(self):
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
                # 换线程安全；asyncio 走局部 import 是为了不动 main.py 顶部的导入区。
                import asyncio

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
                import asyncio

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
                today["cost"] = self.pricing.summarize(today.get("tokens") or {})
        except Exception:
            logger.debug("[multi-decision] 计算最近 24 小时花费失败", exc_info=True)
        mem = _disk_usage()
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
        return {
            "items": items,
            "total": len(items),
            "date": day,
            "days": self._window_log.days() if self._window_log else [],
            "keep": keep,
        }

    @_api("GET", "/quote-lookup")
    async def api_quote_lookup(self, sid: str = "", ids: str = ""):
        """按 message_id 回查更早的原话（用户 m02993 图10）。

        窗口只留最近几十条，被引用的那条经常已经滑出去了——以前前端只能写
        「引用了更早的一条消息」。请求记录里每一轮都存着当轮的完整消息，翻最近
        几十轮就能把原话捞回来；同一个 id 只扫一次，结果进缓存。
        """
        out: dict = {}
        if not sid or not ids or self.request_log is None:
            return {"ok": True, "items": out}
        cache = getattr(self, "_quote_cache", None)
        if cache is None:
            cache = self._quote_cache = {}
        want: list = []
        for x in str(ids).split(","):
            k = x.strip()
            if k and k not in want:
                want.append(k)
        want = want[:12]
        try:
            _rsid = self.window.raw_sid(sid) if self.window is not None else ""
        except Exception:
            _rsid = ""
        missing = [k for k in want if (sid, k) not in cache]
        if missing:
            left = set(missing)
            # 两种形态：用户行「[17:46] [message_id: -357681150] [user_nickname: 田野, …] | 正文」
            # 与 bot 行「[17:37] <msg message_id="1888530223">[昵称] <text>正文</text></msg>」。
            # 以前只认前者，引用 bot 自己的话就永远查不回来（用户 m11986）。
            pat = re.compile(r'\[message_id:\s*([-\d]+)\]|<msg\s+message_id="(-?\d+)"', re.I)
            xml_pat = re.compile(
                r'<msg\s+message_id="(-?\d+)"[^>]*>\s*\[([^\]]*)\]\s*<text>([\s\S]*?)</text>',
                re.I,
            )
            try:
                rounds = self.request_log.list_rounds(sid, limit=80) or []
            except Exception:
                rounds = []
            for row in rounds:
                if not left:
                    break
                name = str((row or {}).get("name") or "")
                if not name:
                    continue
                try:
                    rec = self.request_log.read_round(sid, name) or {}
                except Exception:
                    continue
                for m in rec.get("messages") or []:
                    if isinstance(m, dict):
                        txt = str(m.get("text") or m.get("content") or m.get("raw") or "")
                    else:
                        txt = str(getattr(m, "content", "") or "")
                    if "message_id" not in txt:
                        continue
                    hit = ""
                    for mm in pat.finditer(txt):
                        mid = mm.group(1) or mm.group(2)
                        if mid in left:
                            hit = mid
                            break
                    if not hit:
                        continue
                    nm = re.search(r"\[user_nickname:\s*([^,\]]+)", txt)
                    tm = re.match(r"\s*\[(\d{1,2}:\d{2})\]", txt)
                    xm = xml_pat.search(txt)
                    if xm and xm.group(1) == hit:
                        # bot 自己发的那行进不了上面那套切法：它没有「 | 」，
                        # 昵称在方括号里、正文包在 <text> 里（用户 m11986）。
                        who = xm.group(2).strip()
                        body = xm.group(3).strip()
                    else:
                        who = nm.group(1).strip() if nm else ""
                        body = txt
                        sp = re.split(r"\]\s*\|\s*", txt, 1)
                        if len(sp) > 1:
                            body = sp[1]
                    # 用户 m12256：被引用那条自己也带图时（大椰果 18:21 发图、哦豁 18:37 引用），
                    # 引用块里要画出那张图，不能只画一个「图片」占位。请求快照的行里带着 media
                    # （request_log._decorate_rows 按 message_id 补的），这里顺手过一遍
                    # public_media：副本落进插件缓存，框架之后清理 session_media 也不丢。
                    med: list = []
                    if isinstance(m, dict):
                        refs = m.get("media") or []
                        for ref in (refs[:6] if isinstance(refs, list) else []):
                            if not isinstance(ref, dict):
                                continue
                            try:
                                got = public_media(
                                    str(ref.get("path") or ""),
                                    str(ref.get("mime") or ""),
                                    _rsid,
                                )
                            except Exception:
                                got = {}
                            if got and (got.get("path") or got.get("url")):
                                med.append(got)
                    cache[(sid, hit)] = {
                        "who": who,
                        "ts": tm.group(1) if tm else "",
                        "body": body.strip(),
                        "media": med,
                    }
                    left.discard(hit)
            if len(cache) > 4000:
                cache.clear()
        for k in want:
            got = cache.get((sid, k))
            if got:
                # 快照那行没带图（图片是更早一轮发的，或者框架没补上），
                # 再按 message_id 去 session_media / 插件索引 / 归档库认一次。
                if not got.get("media"):
                    refound = _quote_media_by_mid(k, _rsid)
                    if refound:
                        got = dict(got)
                        got["media"] = refound
                out[k] = got
        return {"ok": True, "items": out}

    @_api("GET", "/window")
    async def api_window(self, sid: str = ""):
        """某个会话当前的上下文窗口（观察窗，只读）。"""
        if not sid or self.window is None:
            return {"ok": False, "detail": "没有这个会话的窗口"}
        messages = self.window.messages_of(sid)
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
            "pending": self.window.pending_of(sid),
            "summary": self.window.summary_info(sid),
            "bot_qq": self.bot_qq,
            # v3.91（用户 m05749）：当前窗口顶上那条「最近一次判定」直接吃这个，
            # 不用再单独拉一次决策列表（异步拉容易赶不上本次绘制）。
            "last_decision": self._last_decision_of(sid),
            # v3.93（用户 m07570 图3）：决策记录真的进了上下文（assistant 身份），
            # 窗口页末尾也要能看见这一条，不然只有顶上一条横幅，像没生效。
            "decision_note": (
                self.window.decision_note_of(sid) if self.window is not None else {}
            ),
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

    @_api("GET", "/context-sessions")
    async def api_context_sessions(self):
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
        items = sorted(merged.values(), key=lambda r: r.get("last_ts") or 0, reverse=True)
        # v3.66（用户 m19536 第 9 条）：「最近活跃」= 消息日志里最后一次收到消息的时间。
        # 静音的会话不写日志行，所以静音不算活跃；跟请求时间取大的那个来排。
        act = {}
        try:
            if self._log is not None:
                act = self._log.last_seen(7) or {}
        except Exception:
            act = {}
        # v3.23（用户 m45332 第5条）：会话卡上的「实际概率 / 攒到第几条」要用
        # 最近 24 小时的回复次数与用户消息数（stats_recent 的 by_session，
        # messages 只算收到的消息行、不含 trigger/round_end 事件行）。
        by_r24 = {}
        try:
            by_r24 = (self._recent24() or {}).get("by_session") or {}
        except Exception:
            by_r24 = {}
        for it in items:
            sid = it.get("sid") or ""
            # 热重载/重启会清空内存里的窗口状态，左栏就会显示「窗口 0 条」、右栏却有内容。
            # 这里用最近一轮请求发出去的历史顶上，两边数字才对得上。
            if (not it.get("window") and self.request_log is not None
                    and self.eff("session_enabled", sid) is not False):
                try:
                    last = self.request_log.last_snapshot(sid)
                except Exception:
                    last = None
                if last:
                    it["snapshot_counted"] = count_messages(last.get("messages") or [])
                    it["stale"] = True
                    if not it.get("last_ts"):
                        it["last_ts"] = last.get("ts") or 0
            # 配置胶囊「决策开 · 窗口 30/60 · 模式 count · 阈值 10」就显示在这，
            # 每项带上自己的有效配置，前端不再需要另拉 overview
            it["eff"] = self._eff_summary(sid)
            # v3.53（用户 m08226 第 2 条）：左栏「静默 / 休兜」标签要倒计时，这里也给一份
            # 下一次自动冒泡的绝对时刻，前端每秒减就行（和 /overview 同一个口径）。
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
            it["muted"] = self.eff("session_enabled", sid) is False
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
            agg = by_r24.get(sid) or {}
            it["r24_messages"] = int(agg.get("messages") or 0)
            it["r24_replied"] = int(agg.get("replied") or 0)
            it["last_active"] = max(
                float(it.get("last_ts") or 0.0),
                float(act.get(_merge_sid_key(sid)) or 0.0),
            )
        # v3.66（用户 m19536 第 9 条）：左栏按谁最近活跃谁排第一；前端本来就按
        # 「群聊 / 私聊」分两段渲染，排好序后两段各自有序，不用前端再排。
        items.sort(key=lambda r: r.get("last_active") or 0, reverse=True)
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
        try:
            if self.tracer is not None:
                totals = self.tracer.recent_totals(span) or {}
                seen = totals.get("models") or {}
                if self.pricing is not None:
                    cost = self.pricing.summarize(totals)
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
        rows = []
        # 两边取并集：价目里有但最近没用到的也要列出来，不然想改价还得重新跑一遍
        for name in sorted(set(models) | set(seen)):
            if name in gone:
                continue
            price = models.get(name) or {}
            stat = seen.get(name) or {}
            # 价目表里没有的模型按 0 元算（用户 2026-10-24 第 5 条：「识别到了新的
            # 也默认成0元」）：前端看到 null 会显示成空、看起来像「没定价」，给 0
            # 就直观多了——用户照着 0 改数就行。
            rows.append(
                {
                    "model": name,
                    "in": _as_price(price.get("in")),
                    "out": _as_price(price.get("out")),
                    # 缓存命中的单价单列一项（用户 m03616 第 1 条）：命中部分按它算钱
                    "cache": _as_price(price.get("cache")),
                    "input": int(stat.get("input") or 0),
                    "output": int(stat.get("output") or 0),
                    "cached": int(stat.get("cached") or 0),
                    "rounds": int(stat.get("rounds") or 0),
                }
            )
        return {"ok": True, "unit": PRICE_UNIT, "hours": span, "rows": rows, "cost": cost}

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
                 limit_dm=0, limit_key_dm: str = ""):
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
                # 上限分两套：limit 是群聊默认，limit_dm 是私聊默认（默认为群聊的两成）
                "limit_dm": int(limit_dm or 0),
                "limit_key_dm": limit_key_dm,
                "days": int(days or 0),
                "days_key": days_key,
                "per_session": bool(per_session),
                "clearable": bool(clearable),
            })

        # 图片缓存：上限 / 天数由媒体缓存自己那三条管，按张数算
        media = {"count": 0, "bytes": 0}
        try:
            if self.window is not None:
                media = self.window.media_cache_stats() or media
        except Exception:
            pass
        _row("media", "图片缓存", "张",
             "群友发过的图片副本，原图被框架删了也能显示；上限是每个群组各自的上限",
             media.get("count"), media.get("bytes"),
             limit=_eff("media_cache_limit"), limit_key="media_cache_limit", limit_unit="张",
             limit_dm=_dm("media_cache_limit"), limit_key_dm="media_cache_limit_dm",
             days=_eff("media_cache_keep_days"), days_key="media_cache_keep_days")

        # v4.00（用户 m10782）：「请求快照」改名「回复快照」，决策快照单独一行。
        # 两者同目录（requests/<sid>/*.json）、同命名，靠记录里的 decision_only 区分，
        # 所以计数要走 request_log.usage() 拆一遍，不能再 _path_usage 一把梭。
        req_usage = {"reply": {"count": 0, "bytes": 0},
                     "decision": {"count": 0, "bytes": 0}}
        try:
            if self.request_log is not None:
                req_usage = self.request_log.usage() or req_usage
        except Exception:
            logger.exception("[multi-decision] 统计快照占用失败")
        reply_cell = req_usage.get("reply") or {}
        dec_cell = req_usage.get("decision") or {}
        _row("requests", "回复快照", "份",
             "每轮回复的完整消息和调用轨迹，查漏消息、查裁切时看",
             reply_cell.get("count"), reply_cell.get("bytes"),
             limit=_eff("trace_keep"), limit_key="trace_keep", limit_unit="份/会话",
             limit_dm=_dm("trace_keep"), limit_key_dm="trace_keep_dm",
             days=_eff("requests_keep_days"),
             days_key="requests_keep_days", per_session=True)
        # 决策快照与回复快照共用 requests_keep_days 那条天数线（同一个目录、同一批文件）
        _row("decision_snapshots", "决策快照", "份",
             "每次决策模型判一遍就留一份，看它当时读到什么、凭什么这么判",
             dec_cell.get("count"), dec_cell.get("bytes"),
             limit=_eff("decision_keep"), limit_key="decision_keep", limit_unit="份/会话",
             limit_dm=_dm("decision_keep"), limit_key_dm="decision_keep_dm",
             days=_eff("requests_keep_days"),
             days_key="requests_keep_days", per_session=True)

        win_root = getattr(self._window_log, "dir", None)
        win_count, win_bytes = _jsonl_usage(win_root) if win_root else (0, 0)
        _row("windows", "窗口快照", "条",
             "每轮窗口怎么变的：新进几条、裁掉几条、待发什么",
             win_count, win_bytes,
             # 用户 m18059 第 3 条：窗口快照补一条份数线（0 = 不限），前端按这个键做输入框
             limit=_eff("windows_keep"), limit_key="windows_keep", limit_unit="条/会话",
             limit_dm=_dm("windows_keep"), limit_key_dm="windows_keep_dm",
             days=_eff("windows_keep_days"),
             days_key="windows_keep_days", per_session=True)

        # v3.67（用户 m20117）：中期摘要不再进缓存账本——它是窗口那边的功能，
        # 上限 / 天数三个键已经挪到「窗口设置」页，这里只留真正占缓存盘的五类。
        # v3.64（用户 m18870）：调用轨迹并进请求快照，不再单列一行
        # v3.66（用户 m19536 第 2 条）：消息日志挪到会话统计上面——这两类都不设上限、
        # 只按天数滚，排在一起看更顺。
        dec_root = getattr(self._log, "dir", None)
        dec_count, dec_bytes = _jsonl_usage(dec_root) if dec_root else (0, 0)
        _row("decisions", "消息日志", "条",
             "每条消息、每次触发和结算各记一行，只记时间、会话、结果",
             dec_count, dec_bytes,
             days=_eff("decisions_keep_days"),
             days_key="decisions_keep_days", per_session=True)

        # 会话统计：<sid>.json 里是「一天一条计数」，加上两本用量账的行数
        stats_count = 0
        stats_bytes = 0
        stats_dir = getattr(self.stats, "dir", None)
        if stats_dir:
            try:
                for path in Path(stats_dir).glob("*.json"):
                    try:
                        stats_bytes += int(path.stat().st_size or 0)
                        raw = json.loads(path.read_text(encoding="utf-8"))
                        stats_count += len(raw) if isinstance(raw, dict) else 0
                    except Exception:
                        continue
            except Exception:
                pass
            led_count, led_bytes = _jsonl_usage(stats_dir, ".jsonl")
            stats_count += led_count
            stats_bytes += led_bytes
        _row("stats", "会话统计", "条",
             "自适应阈值要用的账：收了多少条、回了多少次、花了多少 token",
             stats_count, stats_bytes,
             days=_eff("stats_keep_days"),
             days_key="stats_keep_days", clearable=False)
        return out

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
            kinds = self._cache_kinds(sid)
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
            # task-8 第 2 条：五类里如果有哪类因为体积太大没拆到会话（decisions/windows），
            # 在这里点名，页面可以提示「这一类的占用没算进会话行」。
            "sessions_partial": per_session.get("partial") or [],
            "limits": {
                "media_cache_limit": int(getattr(self, "media_cache_limit", 0) or 0),
                "media_cache_keep_days": int(getattr(self, "media_cache_keep_days", 0) or 0),
                # v3.66（用户 m19536 第 4 条）：四个上限的「私聊默认」
                "media_cache_limit_dm": int(getattr(self, "media_cache_limit_dm", 0) or 0),
                "trace_keep_dm": int(getattr(self, "trace_keep_dm", 0) or 0),
                "decision_keep_dm": int(getattr(self, "decision_keep_dm", 0) or 0),
                "windows_keep_dm": int(getattr(self, "windows_keep_dm", 0) or 0),
                # 用户 m13708 第 3/4 条：新增 7 个保留键，前端按同名键回填输入框
                "requests_keep_days": int(getattr(self, "requests_keep_days", 0) or 0),
                "decisions_keep_days": int(getattr(self, "decisions_keep_days", 0) or 0),
                "windows_keep_days": int(getattr(self, "windows_keep_days", 0) or 0),
                "stats_keep_days": int(getattr(self, "stats_keep_days", 0) or 0),
                # v3.67（用户 m20117）：中期摘要的三个键挪去窗口设置页，不再进缓存账本
            },
        }
        self._cache_stats_cache = (sid, time.time(), payload)
        return payload

    @_api("POST", "/cache-clear")
    async def api_cache_clear(self, kind: str = "media"):
        """按类型清空插件自己的缓存（用户 m13708 第 4 条：每类都能单独清）。

        kind：media / requests / decision_snapshots / decisions / windows / summaries。
        默认 media，老前端不传参数时行为和以前一样（只清图片副本，不碰框架的
        session_media）。统一返回 {ok, kind, removed, mb}，removed 的口径和
        _cache_kinds 的 unit 一致（张 / 份 / 条 / 个会话）。

        两条硬规矩：
        ① 清 requests **不动** stats/requests.jsonl 与 stats/tokens.jsonl
           两本用量账——否则概览里的「24 小时请求数 / token 数」会凭空归零；
        ② stats 不清：它是自适应阈值的账本，删了阈值要从头学起。
        """
        if self.window is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        kind = str(kind or "media").strip() or "media"
        if kind == "stats":
            return {
                "ok": False,
                "kind": kind,
                "detail": "会话统计是自适应阈值的账本，不提供清空",
            }
        # 清之前先扫一遍体积：删完就不知道腾出多少空间了
        try:
            before = {str(k.get("key")): int(k.get("bytes") or 0) for k in self._cache_kinds()}
        except Exception:
            before = {}
        try:
            if kind == "media":
                out = self.window.clear_media_cache() or {}
                return {
                    "ok": bool(out.get("ok", True)),
                    "kind": kind,
                    "removed": int(out.get("removed") or 0),
                    "mb": round(float(out.get("mb") or 0.0), 2),
                }
            if kind == "requests":
                # v4.00（用户 m10782）：决策快照已单列一类，清「回复快照」时留着它
                removed = int(self.request_log.clear_all(only_reply=True)) if self.request_log else 0
            elif kind == "decision_snapshots":
                removed = int(self.request_log.clear_decisions()) if self.request_log else 0
            elif kind == "traces":
                return {"ok": False, "kind": kind,
                        "detail": "调用轨迹已经并进回复快照，清「回复快照」即可"}
            elif kind == "decisions":
                removed = int(self._log.clear_all()) if self._log else 0
            elif kind == "windows":
                removed = int(self._window_log.clear_all()) if self._window_log else 0
            elif kind == "summaries":
                removed = int(self.summaries.clear_all()) if self.summaries else 0
            else:
                return {"ok": False, "kind": kind, "detail": "未知的缓存类型"}
        except Exception:
            logger.exception(f"[multi-decision] 清空缓存失败（kind={kind}）")
            return {"ok": False, "kind": kind, "detail": "清空失败，详情看日志"}
        mb = round(int(before.get(kind) or 0) / 1048576.0, 2)
        logger.info(f"[multi-decision] 已清空缓存 {kind}：{removed} 条，约 {mb} MB")
        return {"ok": True, "kind": kind, "removed": removed, "mb": mb}

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
        was_enabled = self.eff("session_enabled", sid) is not False
        result = self.overrides.set(sid, cleaned)
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

    def _phrase_effective_items(self, sid: str = "") -> list:
        """生效清单 = 全局池 + 本群独立池。

        老行为是「单群覆盖整份替换全局」：在窗口里拉黑一条只影响这个群，
        设置页的全局清单里也看不见（用户 m02693 第 4 条）。现在反过来——
        窗口拉黑默认写进全局池（所有群一起生效），独立池只当追加项，
        默认就是全局那份；某个群想多拦几句再往自己的池子里加。
        """
        out = list(self._phrase_global_items())
        for word in self._phrase_own_items(sid):
            if word not in out:
                out.append(word)
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
            # 原子替换：先写同目录临时文件再 os.replace，避免框架正好在读时读到半截
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
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
        """当前生效的「不触发短语」清单，窗口里的拉黑按钮据此判断是否已拉黑。"""
        items = self._phrase_effective_items(sid)
        return {
            "enabled": bool(self.eff("phrase_filter_enabled", sid)),
            "items": items,
            "count": len(items),
            "global_count": len(self._phrase_global_items()),
            "own_count": len(self._phrase_own_items(sid)),
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
        """
        if not sid:
            return {"ok": False, "detail": "缺少 sid"}
        if self.overrides is None:
            return {"ok": False, "detail": "插件还没初始化完"}
        act = str(action or "add").strip().lower()
        if act not in ("add", "remove"):
            return {"ok": False, "detail": "action 只能是 add 或 remove"}
        word = _norm_phrase(text)
        if not word:
            return {"ok": False, "detail": "这条消息没有可拉黑的文字"}
        items = list(self._phrase_block_payload(sid)["items"])
        if act == "add":
            if word in [_norm_phrase(w) for w in items]:
                return {"ok": True, "changed": False, "word": word, "scope": "global",
                        **self._phrase_block_payload(sid)}
            new_global = self._phrase_global_items() + [word]
            new_own = self._phrase_own_items(sid)
        else:
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

    @_api("GET", "/request-sessions")
    async def api_request_sessions(self):
        """有请求上下文记录的会话列表。"""
        items = self.request_log.list_sessions() if self.request_log else []
        return {"items": items}

    @_api("GET", "/requests")
    async def api_requests(self, sid: str, limit: int = 20):
        """某会话最近几轮的请求摘要。"""
        # v3.66（用户 m19536 第 6 条）：整表计算以前直接跑在事件循环里，线上要重读
        # 100 份 / 14 MB 的请求快照、实测 2.5 秒。页面每 5 秒轮询它一次，等于把服务
        # 反复堵住两秒——用户点「设置」「首页」时任何请求都排在后面，就是「有时卡很久」。
        # 现在两件事：① 整表计算丢进线程，不占事件循环；② 摘要行走 list_rounds_cached，
        # 目录指纹（文件数 + 最新修改时间）没变就直接复用，只有新落一轮才重算。
        import asyncio

        return await asyncio.to_thread(self._requests_payload, sid, limit)

    def _requests_payload(self, sid: str, limit: int = 20, kind: str = "request") -> dict:
        """api_requests 的计算体。只读盘与内存，放线程里跑是安全的。"""
        # 用户 m13250 第 2 条：请求页标题要跟着「保留请求数」走，所以 limit<=0 就按保留数给满，
        # 顺便把 keep 一起回给前端（标题写「最近 N 次请求（上限 M 次）」用）。
        keep = self.request_log_rounds
        if limit is None or limit <= 0:
            limit = keep
        # v3.91（用户 m05749）：决策记录与回复记录同一个目录、靠 decision_only 分账，
        # 所以多取几倍再筛，否则筛完只剩几条。决策项不走请求那一套（没有轨迹 / 重试链）。
        if str(kind or "request") == "decision":
            take = max(int(limit) * 4, 120)
            rows = self.request_log.list_rounds_cached(sid, take) if self.request_log else []
            # v3.94（用户 m08398 图7/图8）：决策行照回复记录的列给数据——
            # 真实模型名（配置键换成「供应商 / 模型名」）、token（入/出）、缓存命中。
            labels = {}
            fast_label = ""
            try:
                for m in self._decision_models():
                    val = str(m.get("value") or "")
                    if val:
                        labels[val] = str(m.get("label") or "")
                        if m.get("fast"):
                            fast_label = val
            except Exception:
                logger.debug("[multi-decision] 决策模型名映射失败", exc_info=True)
            out = []
            for it in rows:
                if not bool(it.get("decision_only")):
                    continue
                it["outcome"] = "decision"
                it["failed"] = False
                it["error"] = ""
                it["elapsed"] = it.get("decision_elapsed")
                key = str(it.get("decision_model") or "")
                if not key or key in ("fast", "default"):
                    key = fast_label or key
                it["model_label"] = labels.get(key) or (key if key else "跟随默认")
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
                out.append(it)
            # v4.13（用户 m18728 第 1 条）：决策分支以前直接 return，没走 _attach_round_trims，
            # trims_after 这个字段压根不存在——走势图上决策柱永远拿不到裁切标记，
            # 判要回/判不回之后真正发生的裁切都没法标在决策柱上。这里补上同一套绑定。
            picked = out[:int(limit)]
            self._attach_round_trims(sid, picked)
            return {"items": picked, "keep": keep, "kind": "decision"}
        # v3.91：决策快照跟请求快照同一个目录，回复记录里不该冒出「进阶决策」那些行，
        # 所以多取一些、把 decision_only 滤掉再截断。
        _take = max(int(limit) * 2, 200)
        items = self.request_log.list_rounds_cached(sid, _take) if self.request_log else []
        # v4.07（用户 m13302）：窗口裁切快照不是一次真实请求——裁切发生在请求组装之前、
        # 没有模型调用，混在回复记录里既占行也把「本页合计」的命中率带偏。它仍在
        # 走势柱上（橙色柱子）和当前窗口页看得到，这里只负责别让它占回复记录。
        items = [it for it in items
                 if not bool(it.get("decision_only"))
                 and not bool(it.get("trim_only"))
                 and str(it.get("outcome") or "") != "trim"][:int(limit)]
        # 列表里就把 token、用时与命中带上，不用点开每一轮才知道花了多少
        for it in items:
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
            first_in = 0
            for _st in steps:
                try:
                    _v = int(_st.get("input_tokens") or 0)
                except (TypeError, ValueError):
                    _v = 0
                if _v > 0:
                    first_in = _v
                    break
            it["first_input"] = first_in
            # v3.59（用户 m17212 第 1 条）：实测命中是整轮各步合计，而「命中预估」是首步跨轮前缀，
            # 多步轮第二步会把前面整段再发一遍，两列天然不同口径。这里把首步命中一起带下去，
            # 前端就能摆成「整轮 X%（首步 Y%）」，单步轮不受影响。
            first_hit = None
            for _st2 in steps:
                try:
                    _i2 = int(_st2.get("input_tokens") or 0)
                except (TypeError, ValueError):
                    _i2 = 0
                if _i2 > 0:
                    try:
                        _c2 = int(_st2.get("cached_tokens") or 0)
                    except (TypeError, ValueError):
                        _c2 = 0
                    first_hit = round(_c2 * 100.0 / _i2, 1)
                    break
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
            prev_it = items[idx + 1] if idx + 1 < len(items) else None
            prev = None
            if prev_it is not None:
                prev = ((prev_it.get("tokens") or {}).get("cache_hit"))
            it["prev_cache_hit"] = prev
            it["hit_estimate"] = self._hit_estimate(it)
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
                    if _k == 0:
                        _sp["est"] = it.get("hit_estimate")
                    elif _this_in > 0 and _prev_in > 0:
                        _sp["est"] = round(_prev_in * 100.0 / _this_in, 1)
                    else:
                        _sp["est"] = None
                    _prev_in = _this_in
            # v3.22 第 5 条：公式不动，但把「这一轮为什么低/高」算成一句话给悬停用
            it["hit_note"] = self._hit_note(it, prev_it)
        self._attach_round_trims(sid, items)
        return {"items": items, "sid": sid, "keep": keep}

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
        """命中预估（v3.19 用户 m41511 第 8 条）。

        离线拿盘上 97 轮实测比过几套公式（MAE，pp）：
          纯前缀字符比例 11.3 ｜ 0.5 前缀 + 0.5 上一轮实测 15.2 ｜
          max(前缀, 上轮) 18.4 ｜ 只用上轮实测 24.9 ｜ 前缀 + 间隔打五折 10.4。
        结论是前缀比例本身就最准，之前两版「校准」都是把它拉偏：
        · 用「前缀 token ÷ provider input_tokens」当基准，分母里含 system prompt 与
          工具定义，和分子不是一个尺度，会把预估系统性压低（线上 57.1% 估成 14.6%）；
        · 和上一轮实测各半平均更糟：上一轮 0% 往往意味着刚把这段写进缓存，本轮恰恰
          会命中，各半平均等于直接砍半。
        唯一有效的修正是间隔：provider 的 KV 缓存会过期，距上一轮超过 3 小时打五折
        （3h 阈值在 97 轮样本上 MAE 10.1pp、误差超 20pp 的只占 12.4%，比 1h/6h/12h 都好）。

        含图轮拆成「文字 + 图片」两份（v3.53 用户 m08226 第 3 条）：``assembled`` 的 content
        是纯字符串，图片只活在 native 投影里，字符前缀比例把整张图当 0 字符，于是这轮新加
        一张图时预估会高得离谱（线上 93.9%，实测 65.2%）。现在只把「图片占 input 的那一份」
        单独按「见过几张」折算（公式见 ``_hit_estimate_with_images``），文字那份继续用字符
        比例，所以纯文本轮的预估一个数都不变。

        再叠一个全局校准 −2pp（v3.53）：114 轮真实记录里字符口径整体偏高 5.0pp，
        减 2pp 后偏差 2.8pp、MAE 9.36→8.76（带新图 31 轮 9.70→7.71，多图多新图 14 轮 7.59）。
        """
        base = item.get("prefix_reuse_percent")
        if base is None:
            return None
        try:
            base = float(base)
        except (TypeError, ValueError):
            return None
        mix = MultiDecisionPlugin._hit_estimate_with_images(item, base)
        if mix is not None:
            base = mix
        base -= 2.0  # 全局校准：114 轮实测字符口径整体偏高 5pp，减 2pp 后 MAE 最优
        gap = item.get("gap_from_previous")
        if isinstance(gap, (int, float)) and gap > 3 * 3600:
            base *= 0.5
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
            return "第一轮：没有上一轮可比，无法估算"
        parts = []
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
            parts.append("距上一轮 %.1f 小时，超过 3 小时按缓存过期打五折" % (gap / 3600.0))
        imgs = int(item.get("image_count") or 0)
        new_imgs = int(item.get("incoming_image_count") or 0)
        if imgs > 0 and new_imgs > 0:
            parts.append("本轮新加 %d 张图（共 %d 张），新图那部分 token 不进缓存，"
                         "已按图片占比从预估里扣掉" % (new_imgs, imgs))
        act = (item.get("tokens") or {}).get("cache_hit")
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
        getter = getattr(self.request_log, "list_rounds", None)
        rows = []
        if callable(getter):
            try:
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
        outcome = "error" if failed else ("replied" if record.get("replied") else "no_reply")
        if record.get("outcome") in ("pending", "running") and not failed:
            outcome = "pending"
        return {"outcome": outcome, "failed": failed, "error": error,
                "replied": bool(record.get("replied")) and not failed}

    def _attach_round_trims(self, sid: str, items: list) -> None:
        """先在后端绑定裁切，前端倒序/翻页不再改变归属。"""
        ordered = sorted(items, key=lambda x: float(x.get("ts") or 0))
        by_event = {str(x.get("event_id")): x for x in ordered if x.get("event_id")}
        for item in ordered:
            item["trims_after"] = []
        if not ordered or self._window_log is None:
            return
        for cut in self._window_log.read_recent(1000, sid=sid):
            eid = str(cut.get("event_id") or "")
            owner = by_event.get(eid) if eid else None
            if eid and owner is None:
                continue
            if not eid:
                ts = float(cut.get("ts") or 0)
                for idx, item in enumerate(ordered):
                    start = float(item.get("ts") or 0)
                    end = float(ordered[idx + 1].get("ts") or 0) if idx + 1 < len(ordered) else float("inf")
                    if start <= ts < end:
                        owner = item
                        break
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
        try:
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
            trace = self._trace_of(sid, data)
            rec.update(self._round_outcome(rec, trace))
            rec["retry_history"] = self._retry_summary(sid, rec)
            _rows = self._rounds_with_retry(sid, 50)
            rec["retry_of"] = self._retry_link(sid, rec)
            if isinstance(rec["retry_of"], dict):
                rec["retry_of"]["attempt_index"] = self._retry_attempt_index(rec, _rows)
            # 这一轮如果是「出错原轮」，把后来那次重试的结果与回复挂到下面（只读拼接）
            rec["retry_outcome"] = self._retry_outcome_of(rec, _rows)
            raw_sid = self.window.raw_sid(sid) if self.window is not None else ""
            who = self.window._bot_name(sid) if self.window is not None else ""
            # 用户本轮第 3 条：② 历史消息只画历史那段。老写法取完整 messages，
            # 本轮那条（stage 已切进 incoming_messages）会在这里再画一遍，
            # 于是 ② 和 ③ 看着像同一条 @ 出现两次。有 history_messages 就优先用它。
            history_source = copy.deepcopy(
                data.get("history_messages") or data.get("messages") or []
            )
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
                    if isinstance(_txt, str) and _txt.strip():
                        incoming.append({"text": _txt})
            else:
                incoming = [p for p in rec.get("user_prompt_parts") or []
                            if isinstance(p, dict) and p.get("name") == "message"]
                if not incoming and rec.get("user_prompt"):
                    incoming = [{"text": rec["user_prompt"]}]
            new_msgs = [{"role": "user", "content": p.get("text") or ""} for p in incoming]
            history = expand_packed_messages(history_source)
            new_msgs = expand_packed_messages(new_msgs)
            rec["history_display_messages"] = view_of(history, bot_name=who, raw_sid=raw_sid)
            rec["incoming_display_messages"] = view_of(new_msgs, bot_name=who, raw_sid=raw_sid)
            # One-to-one display aliases keep original request indexes stable in the UI.
            rec["display_messages"] = view_of(data.get("messages") or [], bot_name=who, raw_sid=raw_sid)
            rec["display_user_prompt_parts"] = view_of(
                [{"role": "user", "content": (p.get("text") if isinstance(p, dict) else p) or ""}
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
            rec["window_snapshot"] = dict(rec["request_snapshot"],
                scope="completed_round", messages=rec["request_snapshot"]["messages"] + rec["sent_display_messages"],
                size=len(snapshot_msgs) + len(sent_msgs), counted=count_messages(snapshot_msgs + sent_msgs),
                user_counted=count_user_messages(snapshot_msgs + sent_msgs),
                sent_counted=count_messages(sent_msgs), sent_identity_verified=verified)
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
                    _head = "判定要回" if rec.get("decision_reply") else "判定不回"
                    _body = "[决策记录] " + ((_stamp + " ") if _stamp else "") + _head
                    if _why:
                        _body += "（" + _why + "）"
                    if _say:
                        _body += "；打算说：" + _say
                    _note = {"role": "assistant", "name": "对当前聊天消息的决策建议",
                             "content": _body}
                    _shown = view_of([_note], bot_name=who, raw_sid=raw_sid)
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
        except Exception as exc:
            logger.exception("[multi-decision] 读单轮快照失败 %s/%s", sid, name)
            return {"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}

    def _trace_of(self, sid: str, data: dict):
        """找这一轮对应的轨迹：优先 event_id，旧记录按时间就近。"""
        if self.tracer is None or not isinstance(data, dict):
            return None
        eid = str(data.get("event_id") or "")
        if eid:
            # 已有稳定身份时绝不能借邻轮轨迹；没有就是缺失，不是未回复。
            trace = self.tracer.read_by_event(sid, eid)
            if (trace and str(trace.get("event_id") or "") == eid
                    and normalize_sid(str(trace.get("sid") or "")) == normalize_sid(sid)):
                return trace
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

    def _tweet_img(self, url: str) -> str:
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
            return "plugin_data/%s/media_cache/%s%s" % (PLUGIN_ID, key, ext)
        except Exception as exc:
            logger.debug("[multi-decision] 推文配图没取到 %s：%s", u[:80], type(exc).__name__)
            return ""

    @_api("GET", "/tweet")
    async def api_tweet(self, u: str = ""):
        """一条推文的展示数据：作者、正文、图片、互动数。

        前端把正文里的推文链接换成带 data-tw 的骨架卡，渲染完再回来问一次，
        把卡片填满。取不到就返回 ok=False，前端留着「打不开」的样子，不猜内容。
        """
        import asyncio
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
            out["avatar"] = self._tweet_img(out.get("avatar") or "")
            kept = []
            for one in out.get("media") or []:
                one["thumb"] = self._tweet_img(one.get("thumb") or "")
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
    async def api_media(self, path: str = ""):
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
        if not any(is_within_directory(root, target) for root in roots) or not target.is_file():
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
            # 用户 m08158：这里以前少了 import asyncio（别处都是局部 import），
            # 一调用就 NameError，被下面的 except 吞成「图片读取失败」，
            # 页面上每张图都显示「图片已清理」。
            import asyncio

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
