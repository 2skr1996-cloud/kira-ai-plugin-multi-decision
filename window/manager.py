"""窗口管理器：采集 → 快照 → 裁切。

铁律：只有「真空闲」才裁切。空闲判定由决策引擎提供
（没有 agent 在跑 + 队列为空 + 没有抖动计时器），这里只负责在
被允许的那一刻做裁切动作。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
import functools
import types
import time
from html import escape
from pathlib import Path
from typing import Any, List, Optional

from core.logging_manager import get_logger

from ..state.sids import normalize_sid
from .buffer import (WindowState, clone_message, ids_of, merge_framework_messages,
                     merge_trusted_messages, split_emoji_marks)
from .split import expand_packed_messages, regroup_images, with_owned_images
from .summary import body_of, build_lines, drop_overlap, merge_summary
from .trim import (
    count_messages,
    count_user_messages,
    is_cut_boundary,
    trim_by_count,
    weight_of,
)

try:  # 测试桩里没有这个模块，退回 dict
    from core.agent.message import OpenAIMessage
except Exception:  # pragma: no cover
    OpenAIMessage = None

logger = get_logger("multi_decision", "orange")

# 窗口状态只在内存里，热重载（改代码后点重载）会连状态一起清空，页面上的绿柱
# 和实时条数就没了，要等到这个会话下次触发 agent 才重建。
# 这里挂一个假模块当寄存处：它不在插件目录里，reload 只重建插件自己的模块，
# 不会动它，于是窗口能跨热重载活下来。进程真重启时 sys.modules 整个换掉，
# 它跟着消失 —— 所以不会把上一次运行的历史接进来（v2.41）。
_KEEP_MODULE = "_kira_mdp_window_keepalive"


def _keep():
    """取（或第一次创建）同进程共用的窗口寄存处。"""
    mod = sys.modules.get(_KEEP_MODULE)
    if mod is None:
        mod = types.ModuleType(_KEEP_MODULE)
        mod.states = {}
        sys.modules[_KEEP_MODULE] = mod
    return mod

# 我们注入到本轮消息末尾的观察窗块。它会跟着 user 消息一起被框架写进会话记忆，
# 于是下一轮的历史里就多出一份「上一轮看到的观察窗」。最新一份已经在本轮消息里了，
# 历史里那些是纯冗余，发给模型前剥掉。
_OBSERVE_RE = re.compile(r"<group_observe>.*?</group_observe>\s*", re.S)

# 框架给每条群消息拼的前缀：`[group_name: 花体群名 group_id: 1145141974 user_nickname: 某某, user_id: 123]`。
# 群名基本是花体/emoji 乱码，群号对模型判断「谁在说话、说了什么」毫无用处，
# 而它每轮都跟着历史重复一遍。只留 user_nickname / user_id。
_GROUP_RE = re.compile(r"group_name:\s*.*?\s*group_id:\s*-?\d+\s*")

# 字数口径：表情 / 艾特 / 图片这些标记不算字。以前直接 len(text)，
# 一条 7 个字的回复带着 [Emoji 21] 就显示 17 字（用户 m33778 图2）。
_CHAR_MARK_RE = re.compile(
    r"\[Emoji\s+[^\]]*\]|\[At\s+[^\]]*\]|\[(?:Image|Sticker|Voice|Video|File) attached\]"
    r"|\[kira_image_ref\]|\[图片\]|\[表情\]|\[语音\]|\[视频\]|\[文件\]",
    re.I,
)


def _strip_lead_meta(text: str) -> str:
    """剥掉行首那一串元数据：[22:32] [message_id: ...] [user_nickname: ...] | 正文。

    这些是框架给的定位信息，不该算进「这条说了多少字」——用户 m33994 图3
    里三条 OK / 打游戏去 / 关闭所有浏览器 全显示 79 字，就是整条元数据被算进去了。
    括号要配平着剥（Reply ID 那种套了内层括号的也算一段）。
    """
    s = str(text or "")
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in " \t\r\n" or ch == "|":
            i += 1
            continue
        if ch != "[":
            break
        depth, j, end = 0, i, -1
        while j < n:
            c = s[j]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end < 0:
            break
        i = end + 1
    return s[i:]


def visible_chars(text) -> int:
    return len(_CHAR_MARK_RE.sub("", _strip_lead_meta(text)).strip())


def _get(msg, key, default=None):
    """dict 与 pydantic 对象都能取字段。"""
    if isinstance(msg, dict):
        return msg.get(key, default)
    return getattr(msg, key, default)


def text_of_content(content) -> str:
    """把消息 content 转成可读文本。

    多模态消息的 content 是 [{type:'text',text:...}, {type:'image_url',...}] 这样的数组，
    直接 str() 出来是一坨 Python 字典表示，页面上没法看，模型也不可能这么读。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                kind = str(item.get("type") or "")
                if kind == "text":
                    parts.append(str(item.get("text") or ""))
                elif kind in ("image_url", "image"):
                    parts.append("[图片]")
                elif kind:
                    parts.append("[" + kind + "]")
            elif item is not None:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _jsonable(value):
    """把 content 转成能原样进 JSON 的结构（原文档要看到的就是这一份）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    for name in ("model_dump", "dict"):
        fn = getattr(value, name, None)
        if callable(fn):
            try:
                return _jsonable(fn())
            except Exception:
                continue
    return str(value)


_MEDIA_PH_RE = re.compile(r"\[(?:Image|Sticker) attached\]")


def _has_image_part(content) -> bool:
    if not isinstance(content, (list, tuple)):
        return False
    return any(isinstance(p, dict) and p.get("type") in IMAGE_TYPES for p in content)


def _dedupe_media_placeholder(text: str) -> str:
    """文本里的 [Image attached] 和后面的图片 part 是同一张图，展示只留一份。

    群聊带图消息的正文固定以 [Image attached] 占位，图片本身又作为
    kira_image_ref 挂在这条 content 上，两处都会渲染成 [图片]，页面上就是
    张数翻倍（用户 m26768「莫名多出来」的观感来源之一）。
    """
    if not _MEDIA_PH_RE.search(text):
        return text
    text = _MEDIA_PH_RE.sub("", text).rstrip()
    text = re.sub(r"[|][ 	]*$", "", text).rstrip()
    return text


#: 框架贴在正文里的图片占位，以及 _LITERAL_MEDIA 转出来的中文标记
#: 有缩略图时这些都算重复：页面上先是一张图，紧接着又一行 [图片]（用户 m29859）
_MEDIA_TAG_RE = re.compile(r"[|｜]?[ \t]*\[(?:Image attached|Sticker attached|图片|表情)\]")


#: 多模态 part 的类型名会被 text_of_content 原样写进文本（如 [kira_image_ref]）。
#: 有缩略图时它就是纯噪音，而且前端会把它当成正文里的图片占位，
#: 于是把「引用里那张图」误判成本条正文自己的图、画到卡片外面（用户 m01680）。
_PART_TYPE_TAG_RE = re.compile(
    r"[|｜]?[ \t]*\[(?:kira_image_ref|input_image|image_url|input_audio|input_video|input_file)\]"
)


#: 引用 / 合并转发在文本里的开头。这两个片段里的 [Image attached] 不是本条正文的占位，
#: 而是「被引用那条消息带图」的唯一线索，剥掉前端就分不清图该画进引用块还是卡外。
_QUOTE_SPAN_OPENERS = ("[Reply ID:", "[Forward")


def _protected_quote_spans(text: str) -> list:
    """引用 / 合并转发片段在文本里的区间 [start, end)，按中括号配平取整段。"""
    spans = []
    n = len(text)
    i = 0
    while i < n:
        if not text.startswith(_QUOTE_SPAN_OPENERS, i):
            i += 1
            continue
        depth = 0
        j = i
        while j < n:
            ch = text[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= n:
            break  # 没收口，整段都不动
        spans.append((i, j + 1))
        i = j + 1
    return spans


def _drop_media_tags(text: str) -> str:
    """正文里的图片占位全去掉——缩略图已经把它表达清楚了。

    对照档要的是 [图片] 两个字（前端自己按 media 张数补），原文档走 raw，
    所以这里只影响美化档的展示文本，不动发给模型的 content。

    但引用 / 合并转发片段里的占位要留着（用户 m01680「引用的美化还是不正常」）：
    框架会把被引用那条的图一并塞进本条消息的 media，前端靠这段占位判断图归引用块。
    """
    if not text or "[" not in text:
        return text

    def _strip(seg: str) -> str:
        # 占位（[Image attached] 等）和 part 类型名（[kira_image_ref] 等）都属于
        # 「缩略图已经表达了的东西」，正文里一并去掉
        return _PART_TYPE_TAG_RE.sub("", _MEDIA_TAG_RE.sub("", seg))

    spans = _protected_quote_spans(text)
    if spans:
        pieces, pos = [], 0
        for start, end in spans:
            pieces.append(_strip(text[pos:start]))
            pieces.append(text[start:end])
            pos = end
        pieces.append(_strip(text[pos:]))
        text = "".join(pieces)
    else:
        text = _strip(text)
    return re.sub(r"[|｜][ \t]*$", "", text).rstrip()


#: 框架把图片按 message_id 落在 data/session_media/<sha256(sid)>/<sha256(mid)>/ 下。
#: 窗口里存的是框架记忆的纯文本，图片 part 早没了，只能拿 message_id 反查（v2.85）。
_MEDIA_ROOT = "session_media"
_MID_RE = re.compile(r"\[message_id:\s*([^\]]{1,200})\]")
_MEDIA_ID_CACHE: dict = {}


def _media_paths_alive(items) -> bool:
    """缓存里的反查结果是否仍然命中真实文件（0 字节占位也当作已没了）。"""
    if not items:
        return False
    root = _data_root()
    if root is None:
        return False
    for it in items:
        try:
            f = (root / str(it.get("path") or "")).resolve()
            if not f.is_file() or f.stat().st_size <= 0:
                return False
        except OSError:
            return False
    return True


def _find_live_copy(rel: str) -> Optional[Path]:
    """按文件名在 session_media 里找同一个文件的活副本（找不到返回 None）。

    框架把图落在 <session_media>/<会话哈希>/<消息哈希>/<文件>，会话清理只删自己
    那一份。同一条消息在别的会话目录里常常还留着一份同名副本，用户 m05828 图1
    那个「引用自己的图显示已清理、上面那张却好好的」就是这种情况。
    """
    root = _data_root()
    if root is None:
        return None
    parts = str(rel or "").replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] != _MEDIA_ROOT:
        return None
    name = parts[-1]
    if not name:
        return None
    mid = parts[-2] if len(parts) >= 3 else ""
    base = root / _MEDIA_ROOT
    try:
        for d in base.iterdir():
            if not d.is_dir() or d.name == "archive":
                continue
            for cand in ((d / mid / name) if mid else None, d / name):
                if cand is None:
                    continue
                try:
                    if cand.is_file() and cand.stat().st_size > 0:
                        return cand
                except OSError:
                    continue
    except OSError:
        return None
    return None


def _media_by_ids(content, raw_sid: str = "") -> list:
    """按 content 里的 [message_id: X] 反查框架落盘的图片，返回相对 data 的路径。

    raw_sid 只是加速（框架用「人格名:会话」当 session_id 建目录）；拿不到就
    把所有会话目录都扫一遍，反正 session_media 下只有几个。
    """
    text = text_of_content(content)
    if not text or "[message_id:" not in text:
        return []
    ids = _MID_RE.findall(text)
    if not ids:
        return []
    try:
        from core.utils.path_utils import get_data_path

        base = Path(get_data_path()) / _MEDIA_ROOT
    except Exception:
        return []
    dirs = []
    if raw_sid:
        dirs.append(hashlib.sha256(str(raw_sid).encode("utf-8")).hexdigest())
    try:
        if base.is_dir():
            for d in sorted(base.iterdir(), key=lambda x: x.name):
                if d.is_dir() and d.name != "archive" and d.name not in dirs:
                    dirs.append(d.name)
    except OSError:
        pass
    out = []
    for mid in ids:
        mid = mid.strip()
        key = (mid, tuple(dirs))
        found = _MEDIA_ID_CACHE.get(key)
        # 用户 m05828 第 1 条：缓存里存的是「反查那一刻的路径」，框架事后清掉某个
        # 会话目录的副本时，这里还留着那条死路径 → 页面显示「图片已清理」，
        # 可同一张图明明还躺在别的会话目录里。命中缓存也必须先确认文件还在。
        if found and not _media_paths_alive(found):
            found = None
            _MEDIA_ID_CACHE.pop(key, None)
        if found is None:
            found = []
            mid_hash = hashlib.sha256(mid.encode("utf-8")).hexdigest()
            for sid_hash in dirs:
                folder = base / sid_hash / mid_hash
                try:
                    if not folder.is_dir():
                        continue
                    for f in sorted(folder.iterdir()):
                        # 框架对转发卡里没下载下来的图会落一个 0 字节占位文件，
                        # 反查出来只会让页面显示一张裂图，直接跳过（用户 m33994 图1）。
                        try:
                            if not f.is_file() or f.stat().st_size <= 0:
                                continue
                        except OSError:
                            continue
                        found.append(
                            {
                                "path": "%s/%s/%s/%s" % (_MEDIA_ROOT, sid_hash, mid_hash, f.name),
                                "mime": mimetypes.guess_type(f.name)[0] or "",
                            }
                        )
                    if found:
                        # 同一条消息可能被好几个会话目录各存一份，取第一个就够
                        break
                except OSError:
                    continue
            if not found:
                # 目录里扫不到不等于图没了：框架会把旧图挪进 session_media/archive
                # 或整个删掉，而插件在收消息那一刻就留过副本（用户本轮图2：23:22
                # 刚收的图，23:47 就显示「图片已清理」，其实缓存里好好的）。
                found = _media_by_mid_index(mid)
            # 媒体可能稍后才下载完成，不缓存空结果。
            if found:
                if len(_MEDIA_ID_CACHE) >= 2048:
                    _MEDIA_ID_CACHE.clear()
                _MEDIA_ID_CACHE[key] = found
        out.extend(found)
    return out


def _media_by_mid_index(mid: str) -> list:
    """按消息哈希回插件自己的索引里找副本（原图已被框架挪走/清掉时的兜底）。

    框架落图的形状是 session_media/<会话哈希>/<消息哈希>/<文件>，_index.json 的
    byPath 记的就是这个原路径；所以拿 message_id 的 sha256 去 byPath 里找中间那
    一段，就能认回「这条消息的图」。命中说明插件当年确实存过副本，页面应当显示
    缩略图，而不是「图片已清理」（用户本轮图2）。
    """
    cache = _media_cache_root()
    root = _data_root()
    if cache is None or root is None:
        return []
    mid_hash = hashlib.sha256(str(mid).encode("utf-8")).hexdigest()
    try:
        by_path = _index_load(cache).get("byPath") or {}
    except Exception:
        return []
    out = []
    seen = set()
    for rel_key, name in by_path.items():
        if ("/" + mid_hash + "/") not in str(rel_key):
            continue
        name = str(name or "")
        if not name or name in seen:
            continue
        path = cache / name
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        seen.add(name)
        try:
            rel = path.relative_to(root).as_posix()
        except Exception:
            continue
        out.append({"path": rel, "mime": mimetypes.guess_type(name)[0] or ""})
    if out:
        # 命中的副本刚被页面用到，把写入时间推一下，别让裁剪把它当最老的删掉。
        for item in out:
            _touch_cache_file(cache / Path(item["path"]).name)
    return out


# 框架会在会话清理时删掉 session_media 里的旧图（core/chat/session_media_manager），
# 但记录里那个路径还挂着，页面就只剩一张裂图（用户 m01008 图1）。
# 所以第一次见到图片就复制一份到插件自己的缓存目录：以后原图被清掉、
# 或者在快照里回看，也都还能显示。前端拿到的仍是「相对 data 的路径」，
# 只是换成 plugin_data/<插件>/media_cache/...，/media 同样放行这个目录。
_PLUGIN_ID = "kira-ai-plugin-multi-decision"
_MEDIA_CACHE_DIR = "media_cache"
_MEDIA_COPY_LIMIT = 8 * 1024 * 1024
_MEDIA_CACHE_LIMIT = 2000
#: 用户 m11091 第 11 条：图片缓存的另外两条保留线，0 = 不限
_MEDIA_CACHE_KEEP_DAYS = 0
_MEDIA_CACHE_MAX_MB = 0
#: 一个会话留几条决策记录（v3.91，用户 m05834）。只留最近几条，窗口里拼的又只取
#: 最后两条——决策是「辅助判断」，不是流水账，攒多了只会把历史挤长。
DECISION_KEEP = 20
#: v3.67（用户 m20117）：超张数 / 超容量不立刻删——新收的图先留缓冲，
#: 之后被下一次复制触发的清理带走（不然刚发的一张图可能马上就没了）。
#: 用户 m06046 图2：原来只护 5 分钟，刚决策完回头一看图没了；改成 2 小时。
_MEDIA_PRUNE_GRACE = 7200.0
#: 用户 m18059 第 4 条：图片缓存三条线按会话取值的回调。
#: 签名 fn(sid) -> (张数, 天数, 容量MB)；WindowManager 初始化时注册，拿不到就用模块级值。
_MEDIA_SESSION_LIMITS = None


def set_media_cache_limit(n) -> None:
    """把配置里的「图片缓存上限」落到模块级（用户 m10749 第 5 条）。

    口径（用户本轮）：**每个群组各自的上限**，不是整个缓存目录的总上限——
    总上限 = 这个值 × 群组数量，_prune_media_cache 按群组桶分别裁。
    缓存裁剪发生在模块级函数里（_store_cache / _cache_media_file 深路径），
    拿不到插件实例，所以由 WindowManager 初始化时把值写进来。0 / 非法值
    回落默认 600，免得配置写坏了把缓存全删光。
    """
    global _MEDIA_CACHE_LIMIT
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = 0
    _MEDIA_CACHE_LIMIT = v if v > 0 else 600
# 私聊（dm:）窗口的独立存档（用户 m06348 第 6 条）：群聊的窗口只活在进程里，
# 重启就从框架记忆重新攒；私聊要保持「只要开着决策就一直收、一直缓存」，
# 所以单独落一份盘，重启后接回来。
_DM_WINDOW_FILE = "window_dm.json"
_DM_WINDOW_SAVE_GAP = 3.0
_DM_WINDOW_MAX = 80
_DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;,]{1,80});base64,(?P<data>[A-Za-z0-9+/=\s]{64,})$")


def _data_root() -> Optional[Path]:
    try:
        from core.utils.path_utils import get_data_path

        return Path(get_data_path())
    except Exception:
        return None


def _media_cache_root() -> Optional[Path]:
    root = _data_root()
    return None if root is None else root / "plugin_data" / _PLUGIN_ID / _MEDIA_CACHE_DIR


def _dm_window_file() -> Optional[Path]:
    """私聊窗口存档的位置：plugin_data/<插件>/window_dm.json。"""
    root = _data_root()
    if root is None:
        return None
    return root / "plugin_data" / _PLUGIN_ID / _DM_WINDOW_FILE


def _media_cache_files(cache: Path) -> list:
    """缓存目录里的图片副本（跳过 _index.json 这类下划线开头的元数据）。"""
    try:
        return [f for f in cache.iterdir() if f.is_file() and not f.name.startswith("_")]
    except Exception:
        return []


def set_media_cache_retention(days, max_mb) -> None:
    """图片缓存的两条保留线（用户 m11091 第 11 条）：只留最近 N 天 / 最多 N MB。

    0 或非法值 = 不限。跟 set_media_cache_limit 一样落到模块级：裁剪发生在
    拿不到插件实例的模块级函数里，由 WindowManager 初始化时把配置写进来。
    """
    global _MEDIA_CACHE_KEEP_DAYS, _MEDIA_CACHE_MAX_MB
    try:
        _MEDIA_CACHE_KEEP_DAYS = max(0, int(days or 0))
    except (TypeError, ValueError):
        _MEDIA_CACHE_KEEP_DAYS = 0
    try:
        _MEDIA_CACHE_MAX_MB = max(0, int(max_mb or 0))
    except (TypeError, ValueError):
        _MEDIA_CACHE_MAX_MB = 0


def set_media_session_limits(fn) -> None:
    """注册「按会话取图片缓存三条线」的回调（用户 m18059 第 4 条）。

    缓存裁剪在模块级函数里，拿不到插件实例，所以由 WindowManager 初始化时把
    plugin.eff 包一层传进来：fn(sid) -> (张数, 天数, 容量MB)。
    """
    global _MEDIA_SESSION_LIMITS
    _MEDIA_SESSION_LIMITS = fn


def _media_sid_key(sid: str) -> str:
    """比对用的会话名：gm:1145141974 / gm_1145141974 / 机器人名:gm:1145141974 归一成同一串。"""
    text = str(sid or "").strip()
    if ":" in text or "_" in text:
        # 取最后两段（gm/dm + 号），老数据前面可能带机器人名
        parts = [p for p in re.split(r"[:_]", text) if p]
        for idx in range(len(parts) - 2, -1, -1):
            if parts[idx] in ("gm", "dm") and parts[idx + 1]:
                return parts[idx] + "_" + parts[idx + 1]
    return re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()


def _media_sid_of_rel(rel_key: str) -> str:
    """从图片的相对路径里取出会话 id。

    缓存写入时 rel_key 形如 session_media/<sid>/<文件名>，也见过前面还带几段
    目录的，所以找最后一个 "session_media" 后面那一段。
    """
    key = str(rel_key or "").replace("\\", "/").strip()
    if not key:
        return ""
    parts = [p for p in key.split("/") if p]
    for idx in range(len(parts) - 2, -1, -1):
        if parts[idx] == "session_media":
            return parts[idx + 1]
    return ""


def _media_limits_for(rel_key: str):
    """这条图片属于哪个会话、按它的有效设置该用哪三条线；问不到就返回 None。"""
    fn = _MEDIA_SESSION_LIMITS
    if fn is None:
        return None
    sid = _media_sid_of_rel(rel_key)
    if not sid:
        return None
    try:
        return fn(sid)
    except Exception:
        logger.debug("[multi-decision] 取会话图片缓存上限失败", exc_info=True)
        return None


def _media_group_of_rel(rel_key: str) -> str:
    """这张图属于哪个群组：取原路径 session_media/<群组>/... 里的那一段。

    用户本轮：media_cache_limit 是「每个群组各自的上限」，不是整个缓存目录的总
    上限（总上限 = 这个值 × 群组数）。缓存目录是扁平的——文件名是内容 sha1，各群
    共用同一份副本——所以归属只能看 _index.json 里记的原路径。框架现在的实际形状
    是 session_media/<群组哈希>/<消息哈希>/<文件哈希>.<后缀>，里面没有可读的
    gm:/dm:，但同一个群组永远是同一段哈希，拿它当群组键正好。
    路径里找不到 session_media 的（data URL 落盘的、老数据）返回 ""，单独算一个
    「无归属」桶，不跟任何群组抢额度。
    """
    return _media_sid_of_rel(rel_key)


def _media_cache_groups(cache: Path, names=None) -> dict:
    """缓存文件名 -> 引用它的群组集合（同一张图被多个群引用就有多个群）。"""
    groups: dict = {}
    try:
        data = _index_load(cache)
        by_path = data.get("byPath") or {}
    except Exception:
        return groups
    for rel_key, name in by_path.items():
        name = str(name or "")
        if not name or (names is not None and name not in names):
            continue
        groups.setdefault(name, set()).add(_media_group_of_rel(rel_key))
    return groups


def _touch_cache_file(path: Path) -> None:
    """把缓存文件的写入时间刷成现在（用户本轮：刚决策完的新图不该立刻被清）。

    裁剪按 mtime 排序，而「按内容去重命中」时文件根本不重写（_cache_media_file
    里 digest 已存在 / target 已存在都直接 return）。于是一张三天前收过、刚才又
    被发出或被引用的图，mtime 还停在三天前，排在「最老」那一头，下一次裁剪第一
    个删它。所以凡事这次真被用上的缓存文件，都刷一下 mtime，让排序依据变成
    「最后一次写入」。
    """
    try:
        os.utime(path, None)
    except OSError:
        pass


def _index_forget_files(cache: Path, names) -> None:
    """缓存文件被裁掉后，把索引里指向它们的条目一起删掉。

    老实现裁完文件不碰索引：实测一份 1471 条 byPath 的 _index.json 里有 404 条
    指向早已不存在的文件，_index_lookup 每次都要白查一圈才落空。
    """
    gone = {str(n) for n in (names or []) if n}
    if not gone:
        return
    data = _index_load(cache)
    changed = False
    for hold in ("byPath", "byHash"):
        items = data.get(hold) or {}
        for key, name in list(items.items()):
            if str(name or "") in gone:
                items.pop(key, None)
                changed = True
        data[hold] = items
    # 会话归属那一层同理：文件没了就别留着「这个会话用过它」的记录。
    for sid_key, items in list((data.get("bySid") or {}).items()):
        if not isinstance(items, dict):
            continue
        for key, name in list(items.items()):
            if str(name or "") in gone:
                items.pop(key, None)
                changed = True
        if not items:
            data["bySid"].pop(sid_key, None)
    if changed:
        # 这里是「文件已经删了」，索引必须立刻跟着落盘：攒 20 次再写的话，
        # 中途重启就会带着一批指向空文件的条目出来。
        _MEDIA_INDEX_DIRTY[str(cache)] = _MEDIA_INDEX_DIRTY.get(str(cache), 0) + 1
        _index_save(cache, force=True)


def _prune_media_cache(cache: Path, limits=None) -> None:
    """按张数 / 天数 / 容量三条线裁图片缓存，**每个群组各自算各自的**。

    用户原话：「全局设置的是每个群组的上限 不是总的上限  总的上限=全局设置*群组
    数量」。所以张数那条线（limit）是「每个群组各自保留多少张」，整个缓存目录最多
    = limit × 群组数；天数 / 容量两条线同样按群组算。
    一个缓存文件可能被多个群引用（同内容去重后只有一份），只有「引用它的每个群都
    超了额度」才真的删，否则留着——别的群还在用。
    limits 传 (张数, 天数, 容量MB) 时覆盖模块级三条线（用户 m18059 第 4 条：
    写这张图时按它所属会话的有效设置裁）。
    """
    limit, keep_days, max_mb = _MEDIA_CACHE_LIMIT, _MEDIA_CACHE_KEEP_DAYS, _MEDIA_CACHE_MAX_MB
    if limits is not None:
        try:
            _l, _d, _m = limits
            limit = int(_l) if int(_l or 0) > 0 else 2000
            keep_days = max(0, int(_d or 0))
            max_mb = max(0, int(_m or 0))
        except (TypeError, ValueError):
            limit, keep_days, max_mb = _MEDIA_CACHE_LIMIT, _MEDIA_CACHE_KEEP_DAYS, _MEDIA_CACHE_MAX_MB
    try:
        files = _media_cache_files(cache)
        if not files:
            return
        now = time.time()
        grace_cut = now - _MEDIA_PRUNE_GRACE
        stamps: dict = {}
        sizes: dict = {}
        for f in files:
            try:
                st = f.stat()
                stamps[f] = float(st.st_mtime)   # 写入时间（_touch_cache_file 会刷新）
                sizes[f] = int(st.st_size or 0)
            except Exception:
                stamps[f] = 0.0
                sizes[f] = 0
        order = sorted(files, key=lambda f: stamps.get(f, 0.0))
        owners = _media_cache_groups(cache, {f.name for f in files})
        buckets: dict = {}
        for f in order:
            for group in (owners.get(f.name) or {""}):
                buckets.setdefault(group, []).append(f)
        # 每个群组各自裁：一个文件可能被多个群引用，只有「引用它的每个群都超了」
        # 才真删（别的群还在用）。被别的群保下来的文件不算这个群的可裁对象，
        # 所以要多跑几轮——第 2 轮起跳过「已经证明删不掉」的那些，用它后面更新的
        # 独占文件补上额度，保证每个群最后还是落到自己的上限上。
        alive = set(files)
        stuck: set = set()

        def _plan(live):
            """这个群组按写入时间从旧到新该删哪些。

            live 是这个群组「还在的全部文件」（含本轮删不掉、被别人保下来的）；
            额度要按全部算，候选只在删得掉的那部分里挑。
            """
            drop = set()
            if len(live) > limit:
                overflow = len(live) - limit
                for f in live:
                    if overflow <= 0:
                        break
                    if f in stuck or stamps.get(f, 0.0) >= grace_cut:
                        continue       # 删不掉的 / 保护期内新收的，跳过接着挑下一张
                    drop.add(f)
                    overflow -= 1
            if max_mb > 0:
                budget = max_mb * 1048576
                total = sum(sizes.get(f, 0) for f in live if f not in drop)
                for f in live:
                    if total <= budget:
                        break
                    if f in drop or f in stuck or stamps.get(f, 0.0) >= grace_cut:
                        continue
                    drop.add(f)
                    total -= sizes.get(f, 0)
            return drop

        removed = []
        for _round in range(8):
            votes: dict = {}
            for group, bucket in buckets.items():
                votes[group] = _plan([f for f in bucket if f in alive])
            voted = set().union(*votes.values()) if votes else set()
            if not voted:
                break
            gone = set()
            for f in voted:
                groups = owners.get(f.name) or {""}
                if all(f in (votes.get(g) or set()) for g in groups):
                    gone.add(f)
            if not gone:
                break          # 剩下的都是「有群还在用」的，这一轮到此为止
            for f in gone:
                try:
                    f.unlink(missing_ok=True)
                    removed.append(f.name)
                    alive.discard(f)
                except OSError:
                    stuck.add(f)
            stuck |= (voted - gone)     # 这轮被保下来的，下一轮别再拿它占额度
        if keep_days > 0:
            deadline = now - keep_days * 86400
            for f in order:
                # 用户本轮：天数这条线以前不看保护期——刚收进来的一张图，只要 mtime 是旧的
                # （从别处复制过来、或老索引里的条目）当场就被判超期删掉。跟张数 / 容量
                # 两条线一样，两小时内的不动。
                if stamps.get(f, 0.0) >= grace_cut:
                    continue
                if f in alive and stamps.get(f, 0.0) and stamps[f] < deadline:
                    try:
                        f.unlink(missing_ok=True)
                        removed.append(f.name)
                        alive.discard(f)
                    except OSError:
                        continue
        if removed:
            _index_forget_files(cache, removed)
    except Exception:
        pass


# 缓存的命名与索引（用户 m02080）：
# 1) 文件名用「内容 sha1 + 后缀」——同一张图不管框架落盘几次、路径有几个，
#    缓存里只留一份；2) 另存 _index.json 记住「原路径 → 缓存文件」，
#    框架把 session_media 清掉之后，老路径照样能找回这张图。
_MEDIA_INDEX_FILE = "_index.json"
_MEDIA_INDEX_LIMIT = 4000
_MEDIA_INDEX_SID_LIMIT = 2000
_MEDIA_INDEX_SIDS = 200
_MEDIA_INDEX: dict = {}
_MEDIA_INDEX_DIRTY: dict = {}


def _index_load(cache: Path) -> dict:
    key = str(cache)
    if key in _MEDIA_INDEX:
        return _MEDIA_INDEX[key]
    data = {"byPath": {}, "byHash": {}, "bySid": {}}
    try:
        raw = json.loads((cache / _MEDIA_INDEX_FILE).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name in ("byPath", "byHash"):
                src = raw.get(name)
                if isinstance(src, dict):
                    data[name] = {str(k): str(v) for k, v in src.items() if k and v}
            # 「会话 -> 原路径 -> 缓存文件」：老索引里没这一段，读到就当空。
            by_sid = raw.get("bySid")
            if isinstance(by_sid, dict):
                buckets = {}
                for key, val in by_sid.items():
                    if isinstance(val, dict):
                        buckets[str(key)] = {str(a): str(b) for a, b in val.items() if a and b}
                data["bySid"] = buckets
    except Exception:
        pass
    _MEDIA_INDEX[key] = data
    return data


def _index_save(cache: Path, force: bool = False) -> None:
    """把索引写回磁盘。攒够 20 次变更或 force 才落盘，热路径上不做 IO。"""
    key = str(cache)
    data = _MEDIA_INDEX.get(key)
    if data is None:
        return
    if not force and _MEDIA_INDEX_DIRTY.get(key, 0) < 20:
        return
    for name in ("byPath", "byHash"):
        items = data.get(name) or {}
        if len(items) > _MEDIA_INDEX_LIMIT:
            data[name] = dict(list(items.items())[-_MEDIA_INDEX_LIMIT:])
    # 会话归属那一层按会话各自设上限，别让一个话多的群把索引撑爆。
    buckets = data.get("bySid") or {}
    if len(buckets) > _MEDIA_INDEX_SIDS:
        data["bySid"] = dict(list(buckets.items())[-_MEDIA_INDEX_SIDS:])
        buckets = data["bySid"]
    for key, items in list(buckets.items()):
        if len(items) > _MEDIA_INDEX_SID_LIMIT:
            buckets[key] = dict(list(items.items())[-_MEDIA_INDEX_SID_LIMIT:])
    try:
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / (_MEDIA_INDEX_FILE + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache / _MEDIA_INDEX_FILE)
        _MEDIA_INDEX_DIRTY[key] = 0
    except Exception:
        pass


def _index_remember(cache: Path, rel_key: str, name: str, digest: str,
                    sid: str = "") -> None:
    key = str(cache)
    data = _index_load(cache)
    changed = False
    if rel_key and data["byPath"].get(rel_key) != name:
        data["byPath"][rel_key] = name
        changed = True
    if digest and data["byHash"].get(digest) != name:
        data["byHash"][digest] = name
        changed = True
    # 用户本轮：删会话时按会话清缓存一直清不掉——缓存路径里那段是框架的群组哈希，
    # 不是 gm:/dm:，两边对不上。所以写入那一刻就把「哪个会话用过这张图」记下来。
    sid_key = _media_sid_key(sid) if sid else ""
    if sid_key and rel_key and name:
        bucket = data.setdefault("bySid", {}).setdefault(sid_key, {})
        if bucket.get(str(rel_key)) != name:
            bucket[str(rel_key)] = name
            changed = True
    if changed:
        _MEDIA_INDEX_DIRTY[key] = _MEDIA_INDEX_DIRTY.get(key, 0) + 1
        _index_save(cache)


def _index_lookup(cache: Path, rel_key: str = "", digest: str = "") -> str:
    """按原路径或内容哈希查缓存文件，命中且文件还在就返回相对 data 的路径。"""
    root = _data_root()
    if root is None:
        return ""
    data = _index_load(cache)
    for name in ((data["byPath"].get(rel_key) if rel_key else ""),
                 (data["byHash"].get(digest) if digest else "")):
        if not name:
            continue
        target = cache / name
        try:
            if target.is_file() and target.stat().st_size > 0:
                return target.relative_to(root).as_posix()
        except OSError:
            continue
    return ""


def _find_by_basename(rel: str) -> str:
    """同一张图换了路径重新落盘时，按文件名把老副本认回来（用户 m02993 图11）。

    框架重收一张图，会把同一个文件写到新的 session_media/<新会话哈希>/<新消息哈希>/
    <同一文件名> 下，rel_key 跟着变；_index_lookup(新路径) 必然落空（索引记的是老
    路径），页面就显示「图片已清理」——可缓存里那份副本明明还在（缓存文件名是内容
    sha1，跟路径无关）。这里拿文件名回查 byPath，命中且文件还在就认成这张图。
    找不到返回 ""。
    """
    cache = _media_cache_root()
    root = _data_root()
    if cache is None or root is None:
        return ""
    base = str(rel or "").replace("\\", "/").strip().rstrip("/").rsplit("/", 1)[-1]
    if not base:
        return ""
    try:
        by_path = _index_load(cache).get("byPath") or {}
    except Exception:
        return ""
    for old_rel, name in by_path.items():
        name = str(name or "")
        if not name:
            continue
        old_base = str(old_rel or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if old_base != base:
            continue
        cand = cache / name
        try:
            if cand.is_file() and cand.stat().st_size > 0:
                return cand.relative_to(root).as_posix()
        except OSError:
            continue
    return ""


def _touch_media_ref(rel: str) -> None:
    """这一段还在窗口里显示 → 把它的缓存副本 mtime 刷一下（用户 m02993 图11）。

    裁剪按 mtime 从旧到新挑，而 mtime 原来只有「真的被取用」时才刷新（public_media
    → _cache_media_file）；可 <img loading=lazy> 让屏幕外的图根本不会去请求 /media，
    索引命中那条分支（原图没了、只剩副本）也不刷。于是窗口里好好显示着的图 mtime
    停在上次真取用的那一刻，下一次裁剪第一个删它，用户看到的就是「刚还在的图突然
    不见了」。窗口渲染时对所有 ref 统一刷一遍，跟浏览器有没有真去取无关。
    """
    cache = _media_cache_root()
    root = _data_root()
    if cache is None or root is None:
        return
    key = str(rel or "").replace("\\", "/").strip().lstrip("/")
    if not key:
        return
    target = None
    if "/media_cache/" in ("/" + key):
        # public_media 已经给的就是缓存里的副本，直接刷它
        target = root / key
    else:
        hit = _index_lookup(cache, rel_key=key)
        if hit:
            target = root / hit
    if target is None:
        return
    try:
        st = target.stat()
    except OSError:
        return
    if st.st_size <= 0:
        return
    # 十分钟内刚刷过就不用再刷：窗口每几秒重画一次，别把时间花在 utime 上。
    # 十分钟远小于 _MEDIA_PRUNE_GRACE，被显示的图始终待在保护期里。
    if time.time() - float(st.st_mtime) < 600:
        return
    _touch_cache_file(target)



def ensure_media_file(rel_key: str) -> bool:
    """确认 session_media 里的原图还在；不在就用插件副本还原回去。

    框架的 resolve_media_references 只认 <data>/session_media 下的真实文件，
    文件被框架的会话媒体清理删掉后它会抛 ValueError，而这一抛在框架里既没有
    日志也没有兜底，整轮请求就那么挂着（用户 m05205「请求都发不出去」）。
    所以发请求前必须保证引用指向的文件存在：有副本就写回去，没有就别发这个引用。
    """
    root = _data_root()
    if root is None or not rel_key:
        return False
    rel = str(rel_key).replace("\\", "/").strip()
    if not rel:
        return False
    target = (root / rel).resolve()
    try:
        if target.is_file() and target.stat().st_size > 0:
            return True
    except OSError:
        pass
    cache = _media_cache_root()
    if cache is None:
        return False
    src: Optional[Path] = None
    hit = _index_lookup(cache, rel_key=rel)
    if hit:
        src = root / hit
    if src is None or not src.is_file():
        # 升级前的旧命名：sha1(原路径) + 后缀，老副本还在的话也能还原
        try:
            hits = [f for f in cache.glob(hashlib.sha1(rel.encode("utf-8")).hexdigest() + ".*")
                    if f.is_file() and f.stat().st_size > 0]
            src = hits[0] if hits else None
        except OSError:
            src = None
    if src is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".mdrestore")
        shutil.copyfile(src, tmp)
        os.replace(tmp, target)
        _touch_cache_file(src)   # 刚还原一次=这张副本还在用，别让裁剪先删它
        return True
    except Exception:
        try:
            (target.parent / (target.name + ".mdrestore")).unlink(missing_ok=True)
        except Exception:
            pass
        return False


def sanitize_native_refs(messages) -> tuple:
    """发请求前扫一遍原生图片引用，返回 (保留条数, 摘掉的条数)。

    能靠副本还原的留着（图片照发），彻底找不回来的换成 [图片] 文本占位——
    宁可少一张图，也不能让框架解析引用时静默整轮失败。
    """
    kept = 0
    dropped = 0
    for msg in messages or []:
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if not isinstance(content, list):
            continue
        parts = []
        changed = False
        for part in content:
            if isinstance(part, dict) and part.get("type") == "kira_image_ref":
                if ensure_media_file(part.get("path") or ""):
                    kept += 1
                    parts.append(part)
                else:
                    dropped += 1
                    changed = True
                    parts.append({"type": "text", "text": "[图片]"})
                continue
            parts.append(part)
        if changed:
            if isinstance(msg, dict):
                msg["content"] = parts
            else:
                try:
                    msg.content = parts
                except Exception:
                    pass
    return kept, dropped


def _store_cache(name: str, blob: bytes) -> str:
    """把一张图落进插件缓存，返回相对 data 的路径；失败返回空串。"""
    cache = _media_cache_root()
    root = _data_root()
    if cache is None or root is None or not blob:
        return ""
    try:
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if target.is_file() and target.stat().st_size == len(blob):
            # 命中也算「这次用上了」：刷写入时间，别让刚决策完的图排到最老那一头
            _touch_cache_file(target)
            return target.relative_to(root).as_posix()
        tmp = cache / (name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
        _touch_cache_file(target)
        _prune_media_cache(cache)
        return target.relative_to(root).as_posix()
    except Exception:
        return ""


def _file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_media_file(source: Path, rel_key: str, sid: str = "") -> str:
    """把 session_media 里的原图复制一份到插件缓存（按内容去重）。

    文件名是内容 sha1，所以同一张图被框架落到多个路径时缓存里也只有一份；
    原路径记进 _index.json，原图被清理后老路径还能找回这张图（用户 m02080）。
    """
    cache = _media_cache_root()
    root = _data_root()
    if cache is None or root is None:
        return ""
    try:
        if not source.is_file():
            return ""
        size = source.stat().st_size
        if size <= 0 or size > _MEDIA_COPY_LIMIT:
            return ""
        digest = _file_sha1(source)
        if not digest:
            return ""
        # 内容一样但后缀不同（.jpg / .jpeg）也当作同一张，先认已有的那份
        for f in cache.glob(digest + ".*") if cache.is_dir() else []:
            try:
                if f.is_file() and f.stat().st_size == size:
                    _index_remember(cache, rel_key, f.name, digest, sid)
                    # 去重命中=这张图刚又被用了：写入时间往前推（本轮「新图被误删」）
                    _touch_cache_file(f)
                    return f.relative_to(root).as_posix()
            except OSError:
                continue
        suffix = source.suffix or ".img"
        name = digest + suffix
        target = cache / name
        if target.is_file() and target.stat().st_size == size:
            _index_remember(cache, rel_key, name, digest, sid)
            _touch_cache_file(target)
            return target.relative_to(root).as_posix()
        cache.mkdir(parents=True, exist_ok=True)
        tmp = cache / (name + ".tmp")
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
        _touch_cache_file(target)
        _index_remember(cache, rel_key, name, digest, sid)
        # 这张图属于哪个会话，就按那个会话的有效设置裁（用户 m18059 第 4 条）
        _prune_media_cache(cache, _media_limits_for(rel_key))
        return target.relative_to(root).as_posix()
    except Exception:
        return ""


def _cache_data_url(url: str) -> dict:
    """图片 part 偶尔是内联的 data URL，落成缓存文件再交给前端。"""
    m = _DATA_URL_RE.match(url or "")
    if not m:
        return {}
    try:
        blob = base64.b64decode(m.group("data"), validate=False)
    except Exception:
        return {}
    if not blob or len(blob) > _MEDIA_COPY_LIMIT:
        return {}
    ext = mimetypes.guess_extension(m.group("mime") or "") or ".img"
    rel = _store_cache(hashlib.sha1(url.encode("utf-8")).hexdigest() + ext, blob)
    return {"path": rel, "mime": m.group("mime") or ""} if rel else {}


def public_media(path: str, mime: str = "", sid: str = "") -> dict:
    """把一条媒体引用变成前端能直接显示的形状。

    - http(s) 链接：原样给 url，前端直连
    - 真实存在的文件：留一份缓存副本，仍返回原 path（页面最省事）
    - 原图已被框架清理、缓存里还有：返回缓存 path
    - 都没有：missing=1，前端画「图片已清理」占位，不再出一张裂图
    """
    rel = str(path or "").strip()
    if not rel:
        return {}
    if rel.startswith(("http://", "https://")):
        return {"url": rel, "mime": mime}
    if rel.startswith("data:"):
        return _cache_data_url(rel) or {"url": rel, "mime": mime}
    root = _data_root()
    if root is None:
        return {"path": rel, "mime": mime}
    try:
        target = (root / rel.replace("\\", "/")).resolve()
    except Exception:
        return {"missing": 1, "path": rel, "mime": mime}
    # 0 字节是框架下载失败时落的占位文件，当真图返回只会裂图，
    # 所以一律当「原图没了」处理，去缓存里找或标 missing。
    try:
        size = target.stat().st_size if target.is_file() else 0
    except OSError:
        size = 0
    if size > 0:
        # 优先给插件自己的副本路径：快照里存的就是这一刻的字符串，
        # 框架过后清掉 session_media，回看历史快照也还能显示出图。
        cached = _cache_media_file(target, rel, sid)
        return {"path": cached or rel, "mime": mime}
    cache = _media_cache_root()
    if cache is not None:
        # 1) 索引里按原路径找（内容哈希命名的新缓存）
        hit = _index_lookup(cache, rel_key=rel)
        if hit:
            # 索引命中同样是「这张图这次被用上了」：刷一下 mtime，否则它一直是
            # 最旧的那一批，下次裁剪第一个删（用户 m02993 图11）。
            try:
                _touch_cache_file(cache / Path(hit).name)
            except Exception:
                pass
            return {"path": hit, "mime": mime}
        # 2) 旧命名 sha1(路径) 兜底：升级前留下的副本还能接着用
        try:
            name = hashlib.sha1(rel.encode("utf-8")).hexdigest()
            for f in cache.glob(name + ".*"):
                if f.is_file() and f.stat().st_size > 0:
                    return {"path": f.relative_to(root).as_posix(), "mime": mime}
        except Exception:
            pass
        # 3) 换了路径重新落盘：同一个文件名在索引里还指着活副本（用户 m02993 图11）。
        #    命中就把新路径绑进索引，下次 _index_lookup 直接命中，不用再扫一遍。
        by_base = _find_by_basename(rel)
        if by_base:
            try:
                _index_remember(cache, rel, Path(by_base).name, "")
            except Exception:
                pass
            return {"path": by_base, "mime": mime}
    # 4) 同一个文件被框架落到别的会话目录下还活着：捞过来复制进插件缓存，
    #    以后就算那份也被清掉，这里还有副本（用户 m05828 第 1 条）。
    alt = _find_live_copy(rel)
    if alt is not None:
        try:
            cached = _cache_media_file(alt, rel, sid)
        except Exception:
            cached = ""
        if cached:
            return {"path": cached, "mime": mime}
        try:
            return {"path": alt.relative_to(root).as_posix(), "mime": mime}
        except Exception:
            pass
    return {"missing": 1, "path": rel, "mime": mime}


# 用户 m02993 图11：框架有时把图片消息整条换成纯文本，正文只剩「| [图片]」，
# 既没有 image part、按 message_id 也反查不到文件（负数 id 的消息框架压根没
# 落过盘），media 列表就是空的——页面连占位都不画，看上去就像图被吞了。
_BARE_MEDIA_RE = re.compile(r"^(?:\[(?:图片|表情|动画表情|闪照|Image attached)\])+$")


def _bare_media_placeholder(content) -> bool:
    """正文除了「| [图片]」这种标记就什么都没有 = 有图，但文件没留下来。"""
    text = text_of_content(content)
    if not text:
        return False
    body = str(text).rsplit("|", 1)[-1].strip()
    if not body:
        return False
    return bool(_BARE_MEDIA_RE.match(body.replace(" ", "")))


def media_list(content, raw_sid: str = "") -> list:
    """把 content 里的图片 part 摘成前端能直接用的列表（相对路径 + mime）。

    页面上要显示别人发的表情和图片，光有 [图片] 两个字没用；
    真正的文件框架已经落在 data/session_media 下，这里只报路径。
    """
    if not isinstance(content, (list, tuple)):
        # 窗口里的 user 已经拆成纯文本了，没有 part 可看，只能按 message_id 反查
        found = _media_by_ids(content, raw_sid)
        if found:
            # 用户 m02993 图11：这条分支以前只报路径、不碰缓存，于是「窗口里正显示着
            # 的图」和「缓存里最旧的图」是两回事；这里统一刷 mtime，让正在显示的不被裁。
            for it in found:
                if isinstance(it, dict):
                    _touch_media_ref(it.get("path") or "")
            return found
        # 用户 m02993 图11：反查不到、正文又只剩「| [图片]」时给个缺失占位，
        # 否则前端什么也不画，那条消息看上去就像图被吞了。
        if _bare_media_placeholder(content):
            return [{"missing": 1, "path": "", "mime": ""}]
        return []
    out = []
    for part in content:
        item = None
        if isinstance(part, dict):
            item = part
        else:
            try:
                item = {"type": getattr(part, "type", ""), "path": getattr(part, "path", "")}
            except Exception:
                item = None
        if not isinstance(item, dict):
            continue
        if item.get("type") not in IMAGE_TYPES:
            continue
        path = item.get("path") or ""
        if not path:
            url = item.get("image_url")
            if isinstance(url, dict):
                path = str(url.get("url") or "")
            elif isinstance(url, str):
                path = url
        path = str(path or "")
        if not path:
            continue
        out.append({"path": path, "mime": str(item.get("mime_type") or "")})
    if not out:
        out = _media_by_ids(content, raw_sid)
    # 同一张图可能被好几条引到；去重后统一交给 public_media：
    # 存在的顺手留副本、丢了的回缓存、彻底没有的标 missing（前端画占位）。
    public, seen = [], set()
    for it in out:
        if not isinstance(it, dict):
            continue
        ref = public_media(it.get("path") or "", str(it.get("mime") or ""), raw_sid)
        if not ref:
            continue
        key = ref.get("path") or ref.get("url") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        public.append(ref)


    if not public and _bare_media_placeholder(content):
        # 用户 m02993 图11：正文只剩「| [图片]」又拿不到文件时，至少让页面画个
        # 「图片已清理」占位，别让一条「| [图片]」看上去像图被吞了。
        return [{"missing": 1, "path": "", "mime": ""}]
    # 用户 m02993 图11：这一屏的图都还在用，统一刷 mtime（含索引命中那条分支）。
    for one in public:
        if isinstance(one, dict):
            _touch_media_ref(one.get("path") or "")
    return public


def refs_media(refs, sid: str = "") -> list:
    """把待发项里存着的图片引用直接转成页面能用的列表（v4.01，用户 m11166 图4）。

    待发项的 content 是插件自己拼的纯文本，里面常常一条 message_id 都没有
    （或者打包了好几条），with_owned_images 不会附加、按 message_id 也反查不到，
    所以 ③ 里那两条图只能显示「[图片]」两个字。push_pending 时框架给的原引用
    还在，直接用它走同一条 public_media 管道。
    """
    out, seen = [], set()
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or "")
        if not path:
            continue
        try:
            one = public_media(path, str(ref.get("mime_type") or ref.get("mime") or ""), sid)
        except Exception:
            one = None
        if not isinstance(one, dict):
            continue
        key = one.get("path") or one.get("url") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(one)
    return out


# 原路径 → 已经试过几次。备份成功就不再碰（热路径上省一次 copy），
# 没成功的最多重试几次：框架有时先把消息给插件、图晚一拍才落盘（用户 m02080）。
_MEDIA_BACKUP_SEEN: dict = {}
_MEDIA_BACKUP_RETRY = 3


def _backup_media(content, raw_sid: str = "") -> None:
    """消息一进窗口就把图片复制一份，不等页面或记录来取。

    框架会在会话清理时删掉 session_media 里的旧图（用户 m01008 图1），
    等页面打开再算副本就已经来不及了。这里只借 public_media 的副作用，
    返回结果丢掉；同一个引用只处理一次，热路径上就是一次 dict 查找。
    """
    if not isinstance(content, (list, tuple)):
        return
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in IMAGE_TYPES:
            continue
        path = str(part.get("path") or "")
        if not path:
            url = part.get("image_url")
            path = str((url.get("url") if isinstance(url, dict) else url) or "")
        if not path:
            continue
        tried = _MEDIA_BACKUP_SEEN.get(path, 0)
        if tried >= _MEDIA_BACKUP_RETRY:
            continue
        if len(_MEDIA_BACKUP_SEEN) >= 4096:
            _MEDIA_BACKUP_SEEN.clear()
            tried = 0
        try:
            ref = public_media(path, str(part.get("mime_type") or ""), raw_sid)
        except Exception:
            ref = {}
        if ref and not ref.get("missing"):
            _MEDIA_BACKUP_SEEN[path] = _MEDIA_BACKUP_RETRY
        else:
            _MEDIA_BACKUP_SEEN[path] = tried + 1


def _tool_calls_of(calls) -> list:
    """把 tool_calls 洗成前端能直接读的形状：[{id, name, args}]。

    args 原样给（通常是一串 JSON 文本），解析放在前端做——这里不猜参数含义。
    """
    out = []
    try:
        seq = list(calls or [])
    except TypeError:
        return out
    for c in seq[:8]:
        if isinstance(c, dict):
            fn = c.get("function") or {}
            cid = c.get("id") or ""
        else:
            fn = getattr(c, "function", None) or {}
            cid = getattr(c, "id", "") or ""
        if isinstance(fn, dict):
            nm = fn.get("name") or ""
            ar = fn.get("arguments")
        else:
            nm = getattr(fn, "name", "") or ""
            ar = getattr(fn, "arguments", "")
        if not isinstance(ar, str):
            ar = _jsonable(ar)
        out.append({"id": str(cid)[:60], "name": str(nm)[:60], "args": ar if isinstance(ar, str) else ""})
    return out


def _tool_name(msg) -> str:
    """tool 消息自己带着工具名（name 字段），窗口里的卡片靠它写人话。"""
    try:
        if isinstance(msg, dict):
            return str(msg.get("name") or "")[:60]
        return str(getattr(msg, "name", "") or "")[:60]
    except Exception:
        return ""


# v4.01（用户 m11166 图1/图2）：决策注记在快照里也要按注记的长相出现，
# 不能顶着 bot 的名字、正文还带头里的「[决策记录] HH:MM」。
_DEC_NOTE_NAME = "对当前聊天消息的决策建议"
# 用户 m04617 图1：这个名字改过（旧的是「对以上聊天记录的决策建议」），
# 老快照里还留着旧名，认注记时两个都得认，不能把历史记录当成 bot 说过的话。
_DEC_NOTE_NAME_OLD = "对以上聊天记录的决策建议"
_DEC_NOTE_HEAD = re.compile(r"^\[决策记录\]\s*(\d{1,2}:\d{2})?\s*")


def view_of(msgs, limit: int = 0, bot_name: str = "", raw_sid: str = "") -> list:
    """把一份消息列表转成可 JSON 化的精简结构（列表展示用）。

    窗口状态和「最近一轮请求」里存的消息结构不同（一个可能是对象、一个是 dict），
    统一走这里，前端拿到的形状才一致。
    """
    try:
        seq = list(msgs or [])
    except TypeError:
        return []
    if limit and limit > 0:
        seq = seq[-limit:]
    out = []
    for msg in seq:
        if isinstance(msg, dict):
            role = msg.get("role") or ""
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            tool_call_id = msg.get("tool_call_id") or ""
        else:
            role = getattr(msg, "role", "") or ""
            content = getattr(msg, "content", None)
            tool_calls = getattr(msg, "tool_calls", None)
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
        text = text_of_content(content)
        media = media_list(content, raw_sid)
        if media:
            text = _drop_media_tags(text)
        item = {
            "role": role,
            "text": text,
            "chars": visible_chars(text),
            "tools": len(tool_calls or []),
            "tool_call_id": str(tool_call_id)[:60],
            "raw": _jsonable(content),
            "media": media,
            "name": _tool_name(msg),
            "tc": _tool_calls_of(tool_calls),
        }
        # 决策注记：名字换成注记名，正文剥掉「[决策记录] HH:MM」这一截，
        # 时刻单拎成 at，前端按普通一条 assistant 消息画（用户 m11166 图2）。
        if role == "assistant" and (item["name"] in (_DEC_NOTE_NAME, _DEC_NOTE_NAME_OLD)
                                    or text.startswith("[决策记录]")
                                    or bool(_get(msg, "dec"))):
            _m = _DEC_NOTE_HEAD.match(text)
            if _m:
                text = text[_m.end():]
                item["text"] = text
                item["chars"] = visible_chars(text)
                item["raw"] = text
                if _m.group(1):
                    item["at"] = _m.group(1)
            else:
                item["raw"] = text
                # v4.05（用户 m12604）：正文不再带「[决策记录] HH:MM」，
                # 时间从消息自己的 ts 取。
                _ts = _get(msg, "ts")
                try:
                    if _ts:
                        item["at"] = time.strftime("%H:%M", time.localtime(float(_ts)))
                except Exception:
                    pass
            item["decnote"] = True
            item["name"] = _DEC_NOTE_NAME
            item["who"] = _DEC_NOTE_NAME
        # 谁说的：只给 assistant 用，前端拿它当卡片标题。
        # 名字**不能**塞进 content——模型会照着历史格式学，把 [桃华] 写进
        # 自己的输出里（用户 m29392 图1 就是这么来的）。role 才是给模型看的信号。
        if role == "assistant" and bot_name and not item.get("decnote"):
            item["who"] = bot_name
        # v4.22（用户 m21783）：文件卡信息带出到页面（只影响显示）
        _files = msg.get("files") if isinstance(msg, dict) else getattr(msg, "files", None)
        if _files:
            item["files"] = [dict(f) for f in _files if isinstance(f, dict)]
        out.append(item)
    return out


def strip_group_tags(text: str) -> str:
    """去掉消息前缀里的 `group_name: … group_id: …`。"""
    if not text or "group_name:" not in text:
        return text
    return _GROUP_RE.sub("", text)


# 框架把多模态项转成文本时，图片/音频会留下这种字面量标签，
# 模型看了只会当噪音，换成中文标记（v2.28）
_LITERAL_MEDIA = (
    ("[kira_image_ref]", "[图片]"),
    ("[input_image]", "[图片]"),
    ("[image_url]", "[图片]"),
    ("[input_audio]", "[语音]"),
)


# 用户 task-8 第 1 条：合并转发在框架历史里只剩一个「有头没正文」的行，
#   [15:36] [message_id: -1237075442] [user_nickname: 吴中生, user_id: 1050076370] |
# 框架摘不出转发内容时 message_str 就长这样（正文是空的，只剩一根竖线）。
# 模型和页面都只看见一根竖线，既不知道这里有转发、也不知道内容没取到。
# 这里只在「生成窗口/快照文本」时补一个占位，已落盘的请求文件一个字节都不动，
# 正常消息（竖线后面有正文的）也不碰。
_EMPTY_FORWARD_RE = re.compile(r"(?m)^(.*\[message_id:[^\]\n]*\][^\n|]*?)\s*\|\s*$")
FORWARD_EMPTY_PLACEHOLDER = "[合并转发/内容未获取]"


def _fill_empty_forward(text: str) -> str:
    """给「只有头、正文为空」的消息行补上转发占位（用户 task-8 第 1 条）。"""
    if "[message_id:" not in text:
        return text

    def _sub(m):
        return m.group(1) + " | " + FORWARD_EMPTY_PLACEHOLDER

    return _EMPTY_FORWARD_RE.sub(_sub, text)


def _clean(text: str) -> str:
    if "<group_observe>" in text:
        text = _OBSERVE_RE.sub("", text)
    text = strip_group_tags(text)
    if "[" in text:
        for old, new in _LITERAL_MEDIA:
            if old in text:
                text = text.replace(old, new)
    text = _fill_empty_forward(text)
    return text.rstrip()


def clean_content(content):
    """把 content 里的群标识洗掉。str 与多模态 list 都要处理。

    多模态消息的 content 是 [{"type": "text", "text": "..."}, {"type": "image_url", ...}]，
    只洗 str 会漏掉所有带图的消息——实测群聊历史里 20 条有 10 条是这种形态。
    图片本身原样保留（v2.23 起不再换占位，413 的真因是历史条数失控，见计数修复）。
    """
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, (list, tuple)):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(_clean(part))
                continue
            if isinstance(part, dict):
                item = dict(part)
                text = item.get("text")
                if isinstance(text, str):
                    item["text"] = _clean(text)
                out.append(item)
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str):
                try:
                    item = part.model_copy(update={"text": _clean(text)})
                except Exception:
                    item = part
                out.append(item)
                continue
            out.append(part)
        return out
    return content


_ERROR_RE = re.compile(r"^\s*\[ProviderError\]")
# 模型看到历史里 assistant 都带【名字】，自己输出时也会跟着写一个；
# 再叠上我们加的就会变成【樱井桃华】【桃华】。加之前先剥掉已有的。
_OLD_TAG_RE = re.compile(r"^\s*【[^】]{1,24}】\s*")
#: <msg ...> 开头（机器人回复的框架格式）
_MSG_HEAD_RE = re.compile(r"^\s*(<msg\b[^>]*>)\s*")
_TS_RE = re.compile(
    r"\[([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+(\d{1,2}:\d{2})(?::\d{2})?(?:\s+[A-Z][a-z]{2})?\]"
)
_MONTHS = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
    "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def _assistant_prefix(text: str, name: str = "", stamp: str = "") -> str:
    """Normalize only recognized header metadata; never consume body [brackets]."""
    if not isinstance(text, str) or not text.strip():
        return text
    time_head = re.compile(r"^\s*(\[(?:\d{1,2}:\d{2}(?::\d{2})?|[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s+[A-Z][a-z]{2})?)\])\s*")
    name_head = re.compile(r"^\s*\[" + re.escape(name) + r"\]\s*") if name else None
    # A different square-bracket name is metadata only when a structural tag
    # follows it (optionally after a timestamp). Plain [正文] remains body text.
    structured_name = re.compile(
        r"^\s*\[[^\]\n]{1,24}\]\s*(?=(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)*<[A-Za-z][\w:-]*(?:\s|/?>))")
    found_time = ""

    def peel(value, legacy=False):
        nonlocal found_time
        while True:
            match = time_head.match(value)
            if match:
                found_time = found_time or match.group(1)
            else:
                match = name_head.match(value) if name_head else None
                if not match and legacy:
                    match = _OLD_TAG_RE.match(value)
                if not match:
                    match = structured_name.match(value)
            if not match:
                return value
            value = value[match.end():]

    body = peel(text, legacy=True)
    head = _MSG_HEAD_RE.match(body)
    if head:
        rest = peel(body[head.end():])
        body = head.group(1) + (("[" + name + "] ") if name else "") + rest
    elif name:
        body = "[" + name + "] " + body
    prefix = found_time or ("[" + stamp + "]" if stamp else "")
    return (prefix + " " if prefix else "") + body


def _strip_bot_tag(text: str) -> str:
    """Legacy helper: strip only the unambiguous old corner-bracket header."""
    return _OLD_TAG_RE.sub("", text, count=1) if isinstance(text, str) else text


def _stamp_bot_tag(text: str, tag: str) -> str:
    """Place one known name after MSG and one existing time before MSG."""
    return _assistant_prefix(text, tag.strip().removeprefix("[").removesuffix("]"))


def is_error_message(msg) -> bool:
    """上游报错被框架当成 assistant 回复存进了历史（[ProviderError] …）。"""
    return bool(_ERROR_RE.match(text_of_content(_get(msg, "content")) or ""))


def strip_error_messages(messages) -> List[Any]:
    """把上游报错那种「假回复」剔掉——它对模型没有信息量，纯烧 token。"""
    return [m for m in messages if not is_error_message(m)]


def shrink_timestamp(text: str) -> str:
    """[Oct 19 2026 23:52 Mon] -> [23:52]。

    只要时分就够了：日期、星期几在动态段（time）里本来就有，模型不缺这个锚点。
    每条从 23 个字符压到 7 个，20 条历史就是 320 个字符（用户 m21394）。
    """
    if not isinstance(text, str) or "[" not in text:
        return text

    def rep(m):
        if not _MONTHS.get(m.group(1)):
            return m.group(0)
        return "[%s]" % (m.group(4),)

    return _TS_RE.sub(rep, text)


def _map_text(content, fn):
    """对 content 里的文本段应用 fn（str 与多模态数组都处理）。"""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, (list, tuple)):
        out = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                item = dict(part)
                item["text"] = fn(item["text"])
                out.append(item)
            else:
                out.append(part)
        return out
    return content


def shrink_content(content):
    """对单条消息的 content 压时间戳（str 与多模态数组都能吃）。"""
    return _map_text(content, shrink_timestamp)


def shrink_timestamps(messages) -> List[Any]:
    """只改发出去的那一份：窗口本体保留原样，页面上还能看到完整时间。"""
    out = []
    for msg in messages:
        content = _get(msg, "content")
        new = _map_text(content, shrink_timestamp)
        if new is content:
            out.append(msg)
            continue
        if isinstance(msg, dict):
            item = dict(msg)
            item["content"] = new
            out.append(item)
        else:
            try:
                out.append(msg.model_copy(update={"content": new}))
            except Exception:
                out.append(msg)
    return out


_TIME_HEAD_RE = re.compile(r"^\s*\[(\d{1,2}:\d{2})\]")
_MSG_TAG_HEAD_RE = re.compile(r"^\s*(<msg\b[^>]*>)\s*")


def _first_text_of(content) -> str:
    """取消息正文的第一段文本（多模态数组就取第一个文本段）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip():
                return part["text"]
    return ""


def clock_of(messages) -> List[str]:
    """每条消息的「HH:MM」：user 自己带，assistant 取前后最近一条 user 的。

    与 stamp_assistant_time 同一套口径，只是这里不写回正文——决策记录要按时间
    找它促成的哪一条 bot 回复（v3.94 用户 m08398 图3）。
    """
    own = []
    for msg in messages:
        t = ""
        if str(_get(msg, "role") or "") == "user":
            mm = _TIME_HEAD_RE.match(_first_text_of(_get(msg, "content")))
            if mm:
                t = mm.group(1)
        own.append(t)
    out = []
    for i in range(len(own)):
        if own[i]:
            out.append(own[i])
            continue
        stamp = ""
        for j in range(i - 1, -1, -1):
            if own[j]:
                stamp = own[j]
                break
        if not stamp:
            for j in range(i + 1, len(own)):
                if own[j]:
                    stamp = own[j]
                    break
        out.append(stamp)
    return out


def _minute_axis(clocks) -> List[Any]:
    """把一串「HH:MM」摊成单调的分钟数，跨零点自动接上（v3.96）。

    直接拿字符串比大小，23:55 会被判成比 00:12 晚，判定不回的注记就跑到
    整段历史最前面去了（真机踩过）。这里遇到时间往回退就当进了第二天。
    """
    out = []
    prev = None
    day = 0
    for c in clocks:
        txt = str(c or "")
        if ":" not in txt:
            out.append(None)
            continue
        try:
            h, m = txt.split(":")[:2]
            v = int(h) * 60 + int(m)
        except Exception:
            out.append(None)
            continue
        if prev is not None and v < prev:
            day += 1440
        prev = v
        out.append(v + day)
    return out




def is_decision_note(msg) -> bool:
    """这条消息是不是我们自己塞进上下文的决策记录（v3.97，用户 m09615 图6~图8）。

    决策记录是以 assistant 身份进上下文的，框架下一轮又把它当自己的记忆吐回来，
    于是同一条决策在窗口里越攒越多、旧的还排到新的前面。凡是 assistant 且名字
    就是那句建议、或者正文以「[决策记录]」开头的，一律认成注记：入窗前剔掉，
    出口再按最新的决策重新插一条。
    """
    try:
        role = str(_get(msg, "role") or "")
        name = str(_get(msg, "name") or "")
        text = str(text_of_content(_get(msg, "content")) or "")
    except Exception:
        return False
    if role != "assistant":
        return False
    if bool(_get(msg, "dec")):
        return True
    if name in ("对当前聊天消息的决策建议", "对以上聊天记录的决策建议"):
        return True
    t = text.lstrip()
    if t.startswith("[决策记录]"):
        return True
    # v4.05（用户 m12604）：正文不再带前缀，靠开头的「判定要回／判定不回」认。
    return t.startswith("判定要回") or t.startswith("判定不回")


def stamp_assistant_time(messages) -> List[Any]:
    """给 assistant 消息补上时间（用户 m30009 图4）。

    框架存下来的 assistant 只有 <msg message_id="X">[名字] <text>…</text></msg>，
    没有时间；user 每条都带 [HH:MM]。bot 的回复永远紧跟在触发它的那批 user 之后，
    所以拿「前面最近一条 user 的时间」当它的时间；窗口开头那几条前面没有 user，
    就退而用后面最近一条 user 的时间。

    只在出口补，窗口本体不动 —— 每轮按同样输入算，结果一样，前缀不会抖。
    """
    def first_text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, (list, tuple)):
            return next((part["text"] for part in content if isinstance(part, dict)
                         and isinstance(part.get("text"), str) and part["text"].strip()), "")
        return ""

    times = []
    for msg in messages:
        content = _get(msg, "content")
        t = ""
        if str(_get(msg, "role") or "") == "user":
            mm = _TIME_HEAD_RE.match(first_text(content))
            if mm:
                t = mm.group(1)
        times.append(t)
    out = []
    for i, msg in enumerate(messages):
        role = str(_get(msg, "role") or "")
        content = _get(msg, "content")
        text = first_text(content)
        if role != "assistant" or not text.strip():
            out.append(msg)
            continue
        # v4.03（用户 m11947）：注记自带「[决策记录] HH:MM」，再补一个时间就成了
        # 「[21:34] [决策记录] 21:34 …」，而且按前后 user 推出来的时间每轮可能不同。
        if is_decision_note(msg) or _TIME_HEAD_RE.match(text):
            out.append(msg)
            continue
        stamp = ""
        for j in range(i - 1, -1, -1):
            if times[j]:
                stamp = times[j]
                break
        if not stamp:
            for j in range(i + 1, len(times)):
                if times[j]:
                    stamp = times[j]
                    break
        if not stamp:
            out.append(msg)
            continue
        # 时间戳放最前面，和 user 的「[23:52] [message_id: …]」一个位置（用户 m30252）
        newtext = "[" + stamp + "] " + text
        newc = newtext
        if isinstance(content, (list, tuple)):
            newc = list(content)
            for index, part in enumerate(newc):
                if isinstance(part, dict) and part.get("text") == text:
                    newc[index] = dict(part, text=newtext)
                    break
        if isinstance(msg, dict):
            item = dict(msg)
            item["content"] = newc
            out.append(item)
        else:
            try:
                out.append(msg.model_copy(update={"content": newc}))
            except Exception:
                out.append(msg)
    return out


IMAGE_TYPES = ("image_url", "image", "kira_image_ref", "input_image")
IMAGE_PLACEHOLDER = "[图片]"


def _part_type(part):
    if isinstance(part, dict):
        return part.get("type")
    return getattr(part, "type", None)


def _has_image(content) -> bool:
    if not isinstance(content, (list, tuple)):
        return False
    return any(_part_type(p) in IMAGE_TYPES for p in content)


def _replace_images(msg):
    """把一条消息里的图片项换成文本占位，其余项原样。"""
    content = _get(msg, "content")
    parts = []
    for part in content:
        if _part_type(part) in IMAGE_TYPES:
            parts.append({"type": "text", "text": IMAGE_PLACEHOLDER})
        else:
            parts.append(part)
    if isinstance(msg, dict):
        out = dict(msg)
        out["content"] = parts
        return out
    try:
        return msg.model_copy(update={"content": parts})
    except Exception:
        return msg


def _with_content(msg, parts):
    """换掉一条消息的 content，dict 与 pydantic 对象都支持。"""
    if isinstance(msg, dict):
        out = dict(msg)
        out["content"] = parts
        return out
    try:
        return msg.model_copy(update={"content": parts})
    except Exception:
        return msg


def strip_old_images(messages, keep: int = 128) -> List[Any]:
    """只保留最近 keep 张原图，更早的换成「[图片]」。

    思路照搬 MaiBot 的 `limit_latest_images_in_messages`：按「张」从新往旧数，
    其余换文本占位。它默认留 128 张（`visual.max_image_num`），这里也默认 128 ——
    框架发图前会把超过 1MB 的图压缩，正常群聊根本摸不到这个上限，历史内容几乎不变，
    前缀缓存也就不会因为「图被换掉」而断（v2.30 之前按「条带图消息」算、默认 2 条，
    每来一张新图就可能改写一条历史，白白重算）。

    窗口本体不动，只改发出去的那一份，所以页面上还能看到原图。
    """
    if keep < 0:
        return list(messages)
    out = list(messages)
    kept = 0
    for i in range(len(out) - 1, -1, -1):
        content = _get(out[i], "content")
        if not _has_image(content):
            continue
        parts = list(content)
        changed = False
        # 同一条消息里也按「后面的更新」算
        for j in range(len(parts) - 1, -1, -1):
            part = parts[j]
            if _part_type(part) not in IMAGE_TYPES:
                continue
            if kept >= keep:
                parts[j] = {"type": "text", "text": IMAGE_PLACEHOLDER}
                changed = True
                continue
            kept += 1
        if changed:
            out[i] = _with_content(out[i], parts)
    return out


def strip_observe(messages) -> List[Any]:
    """复制一份消息，去掉历史里残留的观察窗块和每条消息的群标识（不改原对象）。

    只改发出去的那一份，不动 WindowState 本体——指纹按原样算，不会误判重锚。
    """
    out = []
    for msg in messages or []:
        cur = clone_message(msg)
        if isinstance(cur, dict):
            content = cur.get("content")
            new = clean_content(content)
            if new is not content:
                cur["content"] = new
        else:
            content = getattr(cur, "content", None)
            new = clean_content(content)
            if new is not content:
                try:
                    cur.content = new
                except Exception:
                    pass
        out.append(cur)
    return out


class WindowManager:
    def __init__(self, plugin, window_log=None, summaries=None):
        self.plugin = plugin
        self.log = window_log
        self.summaries = summaries
        self._states: dict[str, WindowState] = {}
        # 归一 sid → 框架原始 sid（带人格前缀）。图片按 message_id 反查要用它
        self._raw_sids: dict[str, str] = {}
        self._llm_warned = False
        # 热重载后把上一次实例留下的窗口接回来（同一个进程才有）
        self._keep = _keep()
        # 图片缓存上限来自插件配置（用户 m10749 第 5 条），热重载后重新生效
        set_media_cache_limit(getattr(plugin, "media_cache_limit", 0))
        # 用户 m11091 第 11 条：天数 / 容量两条保留线，同样跟着配置走
        set_media_cache_retention(
            getattr(plugin, "media_cache_keep_days", 0),
            getattr(plugin, "media_cache_max_mb", 0),
        )
        # 用户 m18059 第 4 条：图片缓存三条线也按会话取值（单群覆盖 > 全局）。
        # 上面 set_* 落的是全局默认值，写图时按图所属会话的有效设置覆盖。
        eff = getattr(plugin, "eff", None)
        if callable(eff):
            set_media_session_limits(
                lambda sid: (
                    eff("media_cache_limit", sid),
                    eff("media_cache_keep_days", sid),
                    eff("media_cache_max_mb", sid),
                )
            )
        self._restore()
        # 私聊窗口另有落盘存档：进程真重启、热重载都接得回来（用户 m06348 第 6 条）
        self._dm_saved_at = 0.0
        self._restore_dm()

    # ------------------------------------------------------------------ 状态

    def state(self, sid: str) -> WindowState:
        st = self._states.get(sid)
        if st is None:
            st = WindowState(sid=sid)
            self._states[sid] = st
        return st

    def _restore(self) -> int:
        """从寄存处接回窗口。返回接回来的会话数。"""
        saved = getattr(self._keep, "states", None) or {}
        if not saved:
            return 0
        n = 0
        for sid, data in list(saved.items()):
            try:
                st = WindowState.from_dict(data)
                # 旧版会话 id 前面带着机器人名字，恢复时一并归一，
                # 否则改名那一刻的窗口会以旧 id 再冒出来一条。
                key = normalize_sid(sid)
                old = self._states.get(key)
                if old is not None and len(old.messages) >= len(st.messages):
                    continue
                st.sid = key  # 对象里记的也要跟着改，否则列表页拿的是旧 id
                self._states[key] = st
                n += 1
            except Exception as exc:  # pragma: no cover - 坏数据就丢掉
                logger.warning("窗口恢复失败 %s: %s", sid, exc)
        # 恢复出来的老窗口里可能还留着「一条 assistant 塞了好几段 <msg>」的形态
        # （v2.57 才开始拆）。热重载之后顺手拆开，不用等下一轮 collect。
        self.migrate_splits()
        return n

    def migrate_splits(self) -> int:
        """把窗口里没拆开的打包消息就地拆开，返回被改动的会话数。

        两种情况：user 一条塞十几条群消息（v2.40 起在 collect 里拆），
        assistant 一条塞好几段 <msg>（v2.57 起）。老窗口、热重载恢复的窗口
        都可能还留着旧形态，条数和页面会一起出错。
        """
        from .split import expand_packed_messages

        n = 0
        for st in list(self._states.values()):
            try:
                flat = expand_packed_messages(st.messages)
            except Exception as exc:  # pragma: no cover - 坏数据别拖垮恢复
                logger.warning("窗口拆包失败 %s: %s", getattr(st, "sid", "?"), exc)
                continue
            if len(flat) != len(st.messages):
                st.messages = flat
                n += 1
        return n

    # ------------------------------------------------------------ 私聊存档

    def _dm_snapshot(self) -> dict:
        """当前所有私聊（dm:）窗口的可序列化快照。"""
        out = {}
        for sid, st in self._states.items():
            if not str(sid).startswith("dm:"):
                continue
            try:
                out[sid] = st.to_dict()
            except Exception:  # pragma: no cover - 坏状态就跳过
                continue
        return out

    def _save_dm(self, force: bool = False) -> None:
        """把私聊窗口写到 window_dm.json（原子替换 + 限频）。

        为什么单独存：用户 m06348 第 6 条要私聊的上下文窗口「只要开着决策就一直收、
        一直缓存」，和群聊的区别是不能在重载 / 开关时被清空。群聊窗口原来只挂在
        进程内的寄存处（_keep），进程一重启就没了；私聊这份必须落盘。
        限频是因为窗口每收一条消息都会触发 _save()，每轮写一次盘没必要。
        """
        path = _dm_window_file()
        if path is None:
            return
        now = time.time()
        if not force and (now - getattr(self, "_dm_saved_at", 0.0)) < _DM_WINDOW_SAVE_GAP:
            return
        data = self._dm_snapshot()
        if len(data) > _DM_WINDOW_MAX:
            # 私聊太多时只留攒得最满的那些（条数≈活跃度），避免文件无限长
            keep = sorted(data.items(), key=lambda kv: len(kv[1].get("messages") or []),
                          reverse=True)[:_DM_WINDOW_MAX]
            data = dict(keep)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            self._dm_saved_at = now
        except Exception:
            logger.debug("[multi-decision] 私聊窗口存档失败（已吞掉）", exc_info=True)

    def _restore_dm(self) -> int:
        """从 window_dm.json 接回私聊窗口，返回接回的会话数。"""
        path = _dm_window_file()
        if path is None:
            return 0
        try:
            if not path.exists():
                return 0
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("[multi-decision] 私聊窗口存档读不出来（已吞掉）", exc_info=True)
            return 0
        if not isinstance(data, dict):
            return 0
        n = 0
        for sid, raw in data.items():
            key = normalize_sid(str(sid or ""))
            if not key.startswith("dm:") or not isinstance(raw, dict):
                continue
            try:
                st = WindowState.from_dict(raw)
            except Exception:
                continue
            old = self._states.get(key)
            # 内存里（刚热重载接回来的）更全就别被盘上的旧版本覆盖
            if old is not None and len(old.messages) >= len(st.messages):
                continue
            st.sid = key
            self._states[key] = st
            n += 1
        if n:
            logger.info(f"[multi-decision] 接回 {n} 个私聊窗口")
        return n

    def _save(self) -> None:
        """把当前所有窗口写进寄存处（纯内存，热重载时用）。"""
        out = {}
        for sid, st in self._states.items():
            try:
                out[sid] = st.to_dict()
            except Exception:  # pragma: no cover
                continue
        self._keep.states = out
        # 私聊另外落盘（内部自带限频，每轮不会真写盘）
        self._save_dm()

    def forget(self, sid: str) -> bool:
        """丢掉某个会话的内存窗口（删会话记录时用）。"""
        return self._states.pop(str(sid or "").strip(), None) is not None

    def size(self, sid: str) -> int:
        st = self._states.get(sid)
        return st.size() if st else 0

    def counted(self, sid: str) -> int:
        """计入条数（不含工具往返），页面上的「30/60」按这个算。"""
        st = self._states.get(sid)
        return count_messages(st.messages) if st else 0

    def counted_user(self, sid: str) -> int:
        """只数用户侧（role=user）的计入条数——bot 自己的回复不算。

        v3.16（用户 m40583）：页面上「当前 N 条」要跟「还会进窗口的群消息」
        对得上，所以把 bot 的回复排除掉；待发那部分由 pending_counted 补上。
        """
        st = self._states.get(sid)
        if st is None:
            return 0
        n = 0
        try:
            for m in list(st.messages or []):
                role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "")
                if str(role or "") != "user":
                    continue
                n += weight_of(m)
        except Exception:  # pragma: no cover
            return 0
        return n

    def pending_counted(self, sid: str) -> int:
        """待发里攒着的条数（按 message_id 数，一条打包消息可能有十多条）。

        这些消息下一轮就会进窗口，用户看「窗口多少条」时应该把它们算上，
        否则明明有十条待发，页面还写着 0 条。
        """
        st = self._states.get(sid)
        if st is None:
            return 0
        # 口径和窗口一致：按 message_id 数（一条打包消息里可能有十几条）。
        # 图片/贴纸那种没正文也没 raw 的，本身也是一条，按 1 算；
        # mark=bot 的是 bot 自己的回复，已经在记忆里，不重复计。
        n = 0
        try:
            for it in list(getattr(st, "pending", []) or []):
                if not isinstance(it, dict):
                    continue
                if str(it.get("mark") or "") == "bot":
                    continue
                # v3.72（用户 m33207 第 3 条）：bot 的待发是 note_sent 推进来的，
                # sender 一定是空串（群消息必有发送者）。mark 万一没写上（发送后
                # 异常中断），也不该把 bot 自己的话算成「下一轮还会进窗口的 user」。
                if not str(it.get("sender") or "").strip():
                    continue
                raw = str(it.get("raw") or "")
                c = raw.count("[message_id:")
                n += c if c else 1
        except Exception:
            return len(st.pending)
        return n

    def snapshot_info(self, sid: str) -> dict:
        st = self._states.get(sid)
        if st is None:
            return {"sid": sid, "size": 0, "counted": 0}
        info = st.snapshot()
        # 真实消息条数：一条 user 里可能装着十几条群消息，页面上要能看出差别
        info["counted"] = count_messages(st.messages)
        info["pending_counted"] = self.pending_counted(sid)
        info["counted_total"] = info["counted"] + info["pending_counted"]
        return info

    def list_states(self) -> list:
        """所有窗口的概况，给「请求上下文」页列会话用。"""
        out = []
        for sid, st in self._states.items():
            info = st.snapshot()
            info["chars"] = self._chars(st.messages)
            info["counted"] = count_messages(st.messages)
            info["pending_counted"] = self.pending_counted(sid)
            # 待发也要算进窗口条数：它们下一轮就会真的发出去
            info["counted_total"] = info["counted"] + info["pending_counted"]
            out.append(info)
        out.sort(key=lambda x: x.get("size", 0), reverse=True)
        return out

    def raw_sid(self, sid: str) -> str:
        """归一 sid 对应的框架原始 sid（图片按 message_id 反查要用）。"""
        return self._raw_sids.get(sid) or ""

    def messages_of(self, sid: str, limit: int = 0) -> list:
        """当前窗口里的消息，转成可 JSON 化的精简结构（观察窗用）。"""
        st = self._states.get(sid)
        if st is None:
            return []
        # 顺手把挂错消息的图片归位（v2.54，用户 m26465）：窗口本体一次治干净，
        # 之后 collect / snapshot 都不用再兜。
        fixed = regroup_images(st.messages)
        if fixed is not st.messages:
            st.messages = fixed
        msgs = strip_observe(st.messages)
        msgs = strip_error_messages(msgs)
        # v4.03（用户 m11947）：注记就在窗口本体里，页面照着画，不再重插。
        msgs = shrink_timestamps(msgs)
        msgs = self._prefix_assistant(msgs, sid)
        msgs = stamp_assistant_time(msgs)
        out = []
        summary = self._summary_head(sid)
        if summary is not None:
            # 摘要也是真正发出去的一条消息，展示时按同样顺序排在最前面
            text = self.summary_text(sid)
            out.append({
                "role": "summary",
                "text": text,
                "chars": visible_chars(text),
                "tools": 0,
                "tool_call_id": "",
            })
        view = view_of(msgs, limit, bot_name=self._bot_name(sid),
                       raw_sid=self._raw_sids.get(sid) or "")
        # v4.03（用户 m11947）：注记在 msgs 里，view_of 已经按 decnote 画好，直接返回。
        return out + view

    # ------------------------------------------------------------------ 观察窗

    # -------------------------------------------------- 进阶决策的记录（v3.91）
    def push_decision(self, sid: str, rec: dict) -> dict:
        """把一次决策记下来（用户 m05834：回与不回都要进上下文）。

        这里只写 WindowState.decisions 这本账，committed=False；真正那条消息由
        commit_decision 紧跟着 append 进 messages（v4.05，用户 m12604：它就是
        agent 输出的一条普通 assistant 消息，只是没进群）。分两步是让「记了但
        还没落地」在账本上看得出来，重启后也不会重复落地。
        """
        st = self.state(sid)
        item = {
            "ts": float(rec.get("ts") or time.time()),
            "reply": bool(rec.get("reply")),
            "reason": str(rec.get("reason") or "")[:400],
            "say": str(rec.get("say") or "")[:400],
            "model": str(rec.get("model") or ""),
            "failed": bool(rec.get("failed")),
            "committed": False,
        }
        st.decisions.append(item)
        if len(st.decisions) > DECISION_KEEP:
            del st.decisions[:len(st.decisions) - DECISION_KEEP]
        return item

    def commit_decision(self, sid: str) -> dict:
        """把还没落地的判定逐条 append 成普通 assistant 消息（v4.05，用户 m12604）。

        用户原话：「决策也要作为普通消息参与上下文请求的，他就是一条消息」
        「他就是 agent 输出的消息 只是没进群里」。

        所以这里不做任何特殊处理：一次判定 = 一条消息，追加到窗口末尾，之后就跟
        群消息一样待在原地——不合并、不搬位置、不在裁切时特殊照顾，轮到自己被裁
        就跟着一起走。旧实现（v4.03／v4.04）会先剔掉旧注记再补一条、裁切时又摘出来
        重落，用户看到的就是「之前的决策被清掉、上次的又被拉到最底下」。

        只有「还没 commit 过」的才追加（d[committed]），所以同一份窗口反复 commit
        （热重载、重复调用、框架回吐）都不会长出新的一条。
        """
        st = self._states.get(sid)
        if st is None:
            return {}
        rows = [d for d in (getattr(st, "decisions", None) or []) if isinstance(d, dict)]
        fresh = [d for d in rows if not d.get("committed")]
        if not fresh:
            return {}
        last = None
        for d in fresh:
            note = self._decision_msg(d)
            if not note:
                continue
            st.messages.append(note)
            d["committed"] = True
            d["at_index"] = len(st.messages) - 1
            last = note
        return last or {}

    # v3.93（用户 m07570 图3）：这条记录以 assistant 身份进上下文，名字就是
    # 「对当前聊天消息的决策建议」——它代表 bot 自己上一轮的判断，挂在最后一条
    # 群消息之前；用 user 身份塞会被模型当成群里有人说了话。
    DECISION_NOTE_NAME = "对当前聊天消息的决策建议"

    def settle_decision_notes(self, sid: str) -> int:
        """注记待在「它判断的那批消息」后面（用户 m04617 图1），但不许越过下一轮。

        判完要回之后，注记先落进窗口末尾；紧接着本轮的群消息（或者同一个人接着
        发的第二张图）才并进来，就排到了注记后面——这里把注记挪回那批消息之后。

        用户 m05691 第 5 条：以前一律把注记推到窗口最后，判「不回」那一轮写下的
        注记被后来进来的消息顶到末尾，于是和下一轮的注记贴成一张卡，看起来像
        「上一次的决策结果拼到了这一次前面」。现在按时刻切：只把注记挪到比它更早
        的那批消息后面，注记之后新到（更晚）的消息一律排在它后面。
        """
        st = self._states.get(sid)
        if st is None:
            return 0
        msgs = st.messages
        if not msgs:
            return 0
        idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if is_decision_note(msgs[i]):
                idx = i
                break
        if idx < 0 or idx == len(msgs) - 1:
            return 0
        rows = [d for d in (getattr(st, "decisions", None) or []) if isinstance(d, dict)]
        # 分界时刻 = 这条注记记下的决策时刻：它一定晚于「被它判断的那批消息」，
        # 也一定早于那之后才进来的新消息。
        cut = float(rows[-1].get("ts") or 0.0) if rows else 0.0
        if cut <= 0:
            cut = float(_get(msgs[idx], "ts") or 0.0)
        if cut <= 0:
            return 0
        tip = idx
        for j in range(idx + 1, len(msgs)):
            if _get(msgs[j], "role") != "user":
                break
            ts_j = float(_get(msgs[j], "ts") or 0.0)
            # 没时间戳的多半是后合成的消息，按「比注记更晚」处理，别被拖到注记前面去。
            if ts_j <= 0 or ts_j > cut:
                break
            tip = j
        if tip == idx:
            return 0
        note = msgs.pop(idx)
        msgs.insert(tip, note)
        if rows:
            rows[-1]["at_index"] = tip
        return 1

    def decision_note_of(self, sid: str) -> dict:
        """页面要展示的那一条（跟真正发出去的同源，不另拼一份）。"""
        note = self._decision_note(sid)
        if not note:
            return {}
        st = self._states.get(sid)
        rows = [d for d in (getattr(st, "decisions", None) or []) if isinstance(d, dict)]
        return {
            "role": note.get("role") or "assistant",
            "name": note.get("name") or self.DECISION_NOTE_NAME,
            "content": note.get("content") or "",
            "ts": float(rows[-1].get("ts") or 0) if rows else 0.0,
            "n": 1,
        }

    def _decision_msg(self, d: dict):
        """一条判定 → 一条 assistant 消息（v4.05，用户 m12604）。

        正文就是一句「判定不回（理由）」，跟 agent 自己说的一句话没两样；身份靠
        role=assistant + 消息名「对当前聊天消息的决策建议」标出来。时间放进 msg[ts]，
        不再写进正文——正文多一截「[决策记录] HH:MM」，模型会照着学，用户也说那是
        旧格式。
        """
        if not isinstance(d, dict):
            return None
        body = self._decision_body(d)
        if not body:
            return None
        return {
            "role": "assistant",
            "name": self.DECISION_NOTE_NAME,
            "content": body,
            "dec": True,
            "ts": float(d.get("ts") or 0),
        }

    def _decision_note(self, sid: str):
        """最后一条判定对应的那条消息（页面与兼容旧调用用）。"""
        st = self._states.get(sid)
        if st is None:
            return None
        rows = [d for d in (st.decisions or []) if isinstance(d, dict)]
        if not rows:
            return None
        return self._decision_msg(rows[-1])

    @staticmethod
    def _decision_clock(d: dict) -> str:
        """这条决策的「HH:MM」——页面左侧那一列时间用它（v3.97，用户 m09615 图2）。"""
        try:
            return time.strftime("%H:%M", time.localtime(float(d.get("ts") or 0)))
        except Exception:
            return ""

    @staticmethod
    def _decision_body(d: dict) -> str:
        """决策正文，不带「[决策记录] 时间」前缀（页面按普通消息那样摆）。"""
        body = "判定要回" if d.get("reply") else "判定不回"
        why = str(d.get("reason") or "").strip()
        if why:
            body += "（" + why[:200] + "）"
        if d.get("say"):
            body += "；打算说：" + str(d.get("say"))[:200]
        if d.get("failed"):
            body += "；这次决策没跑通，按兜底放行"
        return body

    def _decision_line(self, d: dict) -> str:
        """真正进上下文的那一行：模型得一眼看出这是决策，所以带前缀和时间。"""
        stamp = self._decision_clock(d)
        return "[决策记录] " + (stamp + " " if stamp else "") + self._decision_body(d)

    def push_pending(self, sid: str, sender: str, name: str, text: str,
                     media: str = "", ts: float = 0.0, raw: str = "",
                     media_refs=None, files=None) -> dict:
        """记一条「还没发出去」的消息，让窗口随时可看。

        真正进窗口要等下一轮请求采集，所以这些先挂在这里；
        采集时并入窗口并清空。
        """
        # 用户 m02693 第 3 条：纯转发、纯引用这类消息 extract_text 摘不出正文
        # （只认 Text/At/Emoji），media_kind 也认不出它们，旧判据会把整条扔掉，
        # 待发里就少一条。只要框架给了原文（raw 里有 [Forward …] / [Reply …]），
        # 前端就能渲染成转发卡/引用块，所以 raw 也算「有内容」。
        if not text and not media and not str(raw or "").strip():
            return {}
        st = self.state(sid)
        item = {
            "ts": float(ts or time.time()),
            "sender": str(sender or ""),
            "name": str(name or ""),
            # 存全文，不截断：页面要能看完整一条。群里的「一条消息」可能被框架
            # 合成成一整批（十几到四十条），截了就只能看前半截。
            # 首尾空格要洗掉：框架给的正文常带一个（' 贴贴DDL' / 'xswl '），
            # 页面按字符数显示就会多算 1（用户 m32956 图1 说 6 其实 5）。
            # raw 不能洗，那是要原样发给模型的那一行。
            "text": str(text or "").strip(),
            # 框架格式化好的那一行（带 [message_id: …] 和时间戳）。下一轮采集时
            # 直接拿它当窗口消息用，格式和框架记忆里的一模一样，去重才对得上。
            "raw": str(raw or ""),
            "media": str(media or ""),
            # 图片/表情在本地缓存好的引用（[{path, mime}]），页面拿它出缩略图
            "media_refs": [dict(r) for r in (media_refs or []) if isinstance(r, dict)],
            # v4.22（用户 m21783）：文件元素的文件名/大小，只给页面画文件卡。
            # 正文里依旧是 [File attached]→[文件]，不进上下文，缓存前缀不受影响。
            "files": [dict(f) for f in (files or []) if isinstance(f, dict)],
            # mark：这条最终被怎么处理（计入 / 连发 / 太短 / 跳过…），
            # 由决策层在判定完回填，页面逐条显示（v2.26）
            "mark": "",
        }
        # 待发队列里挂的图当场留副本：等下一轮采集才预热，图可能已被框架清掉
        for ref in item.get("media_refs") or []:
            if isinstance(ref, dict) and ref.get("path"):
                try:
                    public_media(str(ref.get("path") or ""),
                                 str(ref.get("mime_type") or ref.get("mime") or ""), sid)
                except Exception:
                    pass
        st.pending.append(item)
        if len(st.pending) > 200:
            del st.pending[:len(st.pending) - 200]
        return item

    def mark_pending(self, sid: str, item, mark: str) -> None:
        """给刚 push 的那条打标：计入 / 连发 / 太短 / 跳过…（v2.26）"""
        if not isinstance(item, dict):
            return
        item["mark"] = str(mark or "")

    def pending_of(self, sid: str) -> list:
        st = self._states.get(sid)
        return list(st.pending) if st else []

    def _pending_messages(self, st, exclude_ids=None) -> list:
        """把待发列表转成能进窗口的消息。

        框架的记忆只在「一轮 agent 跑完」时更新，所以没触发的那批群消息
        永远不会出现在 framework_messages 里。之前 collect() 直接 clear()，
        这些消息就彻底没了（v2.28 修）。这里按框架原文格式补进来，
        内容与框架记忆一致，下一轮真进记忆时按 message_id 也对得上、不会重复。
        """
        skip = set(exclude_ids or ())
        out = []
        for it in list(getattr(st, "pending", []) or []):
            if not isinstance(it, dict):
                continue
            # 用户 m11091 第 5 条：bot 在「失败 / 卡住」的那一轮发出去的话，框架
            # 记忆里根本没有（它只在结算成功时才写记忆），重试轮的快照就漏了这一条。
            # 这里按 assistant 补进窗口，格式跟框架记忆里的 assistant 一样，只是还
            # 没有 message_id；等框架后来把原件写进记忆，buffer 的 echo_key 会按正文
            # 把这条占位换成原件，既不重复也不丢（见 window/buffer.py）。
            if str(it.get("mark") or "") == "bot":
                body = str(it.get("text") or "").strip()
                # v3.55（用户 m14082 根因）：正文里的字面表情标记（[Emoji 128]）
                # 要按框架原件的结构挪到 <text> 外面变成 <emoji>128</emoji>。
                # 否则占位正文永远比原件多一个标记，echo_key 认领不回来，这条
                # 回显就在窗口里永久多一份，历史摆着成对先例教模型重复发送。
                body, emoji_tags = split_emoji_marks(body)
                if not body and not emoji_tags:
                    continue
                stamp = ""
                try:
                    stamp = time.strftime("[%H:%M]", time.localtime(float(it.get("ts") or 0)))
                except Exception:
                    stamp = ""
                who = str(it.get("name") or it.get("sender") or "")
                mid = it.get("message_id")
                mid = str(mid if mid is not None else "").strip()
                msg_tag = ('<msg message_id="%s">' % escape(mid, quote=True)) if mid else "<msg>"
                out.append({
                    "role": "assistant",
                    "content": "%s %s[%s] <text>%s</text>%s</msg>"
                               % (stamp, msg_tag, who, body, emoji_tags),
                })
                continue
            raw = str(it.get("raw") or "").strip()
            # 本轮就要作为 user_prompt 发出去的那批，不要再并进窗口：
            # 否则窗口尾部是它、框架又追加一份原文，模型连着看两遍（用户 m25834）
            if raw and skip:
                ids = ids_of([{"role": "user", "content": raw}])
                # 交集就算重复：一条待发可能打包了多条消息，只要有一条是本轮
                # 要发的，就不能再并进窗口（v2.48，用户 m25834）
                if ids & skip:
                    st.skipped_incoming += 1
                    continue
            if not raw:
                body = str(it.get("text") or "").strip()
                if not body and (it.get("media_refs") or it.get("media_path")):
                    # v3.98（用户 m10169 图3/图4）：只有图没有正文的待发也不能丢，
                    # 补个占位让它进窗口（图靠 media_refs 挂上去）。
                    body = "[图片]"
                if not body:
                    continue
                stamp = ""
                try:
                    stamp = time.strftime("[%H:%M]", time.localtime(float(it.get("ts") or 0)))
                except Exception:
                    stamp = ""
                who = str(it.get("name") or it.get("sender") or "")
                raw = "%s [user_nickname: %s] | %s" % (stamp, who, body)
            content = with_owned_images(raw, it.get("media_refs"), self.raw_sid(st.sid))
            _backup_media(content, self.raw_sid(st.sid))
            msg = {"role": "user", "content": content}
            # v4.22：文件卡信息挂在消息上（_as_openai 只取 role/content 等固定字段，
            # 多出来的键不会发给模型）
            if it.get("files"):
                msg["files"] = [dict(f) for f in it["files"] if isinstance(f, dict)]
            out.append(msg)
        return out

    # ------------------------------------------------------------------ 采集

    def collect(self, sid: str, framework_messages, skip_history=None, incoming_ids=None,
                raw_sid: str = "") -> int:
        """把框架这次给的历史并进窗口，返回新增条数。

        skip_history：群聊冷启动用（用户 m24996）。重启后群聊不从框架记忆里
        捞旧历史，窗口从本轮消息重新开始；私聊照旧拿框架历史当起点。

        incoming_ids：本轮 user_prompt 里即将发出去的那批 message_id。待发里
        命中的不再并进窗口，避免本轮消息在请求里出现两遍（用户 m25834）。
        """
        if raw_sid:
            self._raw_sids[sid] = raw_sid
        # 先给图片留副本：框架清理 session_media 之后再来取就没了（用户 m01008）
        for m in framework_messages or []:
            _backup_media(_get(m, "content"))
        st = self.state(sid)
        if skip_history is None:
            skip_history = sid.startswith("gm:")
        # v3.85（用户 m04418 第 2 条）：撤掉「重启后回填条数」这条设置。群聊冷启动
        # 一律不灌框架历史（用户 m24996）；私聊靠 window_dm.json 存档接回，本来也不
        # 需要回填，所以这个开关没有存在意义了。
        if skip_history and not st.messages:
            # 群聊冷启动：框架记忆里的历史一条都不要，重启前残留的待发也一并丢掉
            if st.pending:
                st.pending.clear()
            logger.debug(f"[multi-decision] {sid} 群聊冷启动，不灌框架历史")
            return 0
        # 入窗之前先洗一遍：剥群标识、剥观察窗残留、把图片换成 [图片]。
        # 不洗的话图片会一直躺在内存里，而且每轮快照都带着走。
        clean = strip_observe(framework_messages)
        # 上游报错被框架当成 assistant 回复存进历史，这种「假回复」不进窗口
        clean = strip_error_messages(clean)
        # v3.97（用户 m09615 图6~图8）：决策注记是 assistant 身份进上下文的，
        # 框架会把它当历史吐回来；窗口本体里已经有自己那条（commit_decision），
        # 再并进来就成了同一条决策出现好几遍，这里一律剔掉。
        clean = [m for m in clean if not is_decision_note(m)]
        # 待发里攒着的群消息框架记忆里没有（没触发 agent 就不写记忆），
        # 必须在清空之前自己并进窗口，否则它们会凭空消失（v2.28）
        st.skipped_incoming = 0
        extra = self._pending_messages(st, incoming_ids)
        # 本地接收的待发是可信增量；框架历史则必须先证明与当前窗口衔接。
        # 不把两种来源提前拼起来，否则失锚时会错误复活框架里已裁走的旧回复。
        # v4.03（用户 m11947）：窗口末条很可能就是决策注记，而框架历史里没有它，
        # 直接拿末条当锚点永远对不上，只能退回「只补最后一条」——每轮丢消息。
        # 锚点改成最后一条非注记消息。
        anchor = None
        for _m in reversed(st.messages):
            if not is_decision_note(_m):
                anchor = _m
                break
        added = merge_framework_messages(
            st, clean, seed_limit=0, anchor_msg=anchor,
            require_anchor=sid.startswith("gm:"), trusted_messages=extra,
        )
        # 框架 assemble 会把整批图片都挂到最后一条 user 上（用户 m26465），
        # 入窗后按 path 里的 message_id 哈希把图挪回原主。只修被改到的消息。
        fixed = regroup_images(st.messages)
        if fixed is not st.messages:
            st.messages = fixed
        if incoming_ids:
            logger.info(
                "[multi-decision] %s collect: 本轮消息 %d 条，待发跳过 %d 条，窗口新增 %d 条，待发共 %d 条",
                sid, len(incoming_ids), getattr(st, "skipped_incoming", 0), added,
                len(st.pending),
            )
        self.settle_decision_notes(sid)
        if st.pending:
            # 这一轮把攒着的消息带走了，观察窗里不必再重复显示
            st.pending.clear()
        if added:
            logger.debug(
                f"[multi-decision] {sid} 窗口新增 {added} 条，当前 {st.size()} 条"
            )
        return added

    def fold_pending(self, sid: str) -> int:
        """把攒着的待发并进窗口历史，返回并入条数（v3.96，用户 m09331）。

        决策接管之后，连着几轮都判「不回」时不会有真实请求：框架的
        _on_llm_request 不跑，collect() 也就永远不执行，于是待发越攒越多
        （真机上从 00:12 一路堆到 00:28 共 19 条），页面上的窗口一直是空的，
        看着像没生效。判不回就是这一轮的终点——这里按 collect 同一套格式把待发
        并进窗口再清空：这一轮就地定格，下一轮从空待发重新攒。
        """
        st = self._states.get(sid)
        if st is None:
            return 0
        try:
            extra = self._pending_messages(st)
        except Exception:
            logger.exception("[multi-decision] %s 取待发失败（这一轮不并入）", sid)
            extra = []
        # 去重口径与 collect 那条路完全一致：merge_trusted_messages 内部复用
        # 主路的五道关（编号、内容指纹、占位与原件互斥、占位升级原件、并完再
        # 收口成对重复）。以前这里自己写了一套精简版，只比 message_id 和正文
        # 全等，于是主路修过的两个重复 bug（v3.37 同一条回复显示两遍、v3.54
        # 模型学着成对地写）在这条路上原地复现。
        added = merge_trusted_messages(st, extra)
        if added:
            fixed = regroup_images(st.messages)
            if fixed is not st.messages:
                st.messages = fixed
        self.settle_decision_notes(sid)
        if st.pending:
            st.pending.clear()
        if added:
            logger.info("[multi-decision] %s 判定不回：%d 条待发就地并入窗口，当前 %d 条",
                        sid, added, st.size())
        return added

    def commit_round(self, sid: str, prompts, media_by_id=None) -> int:
        """把本轮真正发出去的消息记进窗口（v2.53：每轮都记）。

        这条消息已经发进请求了，对下一轮来说它就是历史。以前只在群聊冷启动
        时 commit，正常轮次的本轮消息要等下一轮 collect 从框架记忆里捞回来；
        这中间的间隙里它们既不在窗口历史、也不在待发（collect 已经把 pending
        清空了），页面上就是「两轮之间凭空少了几条」（用户 m26251）。

        commit 发生在 snapshot 之后，本轮请求里不会多出这几分内容；下一轮
        collect 用 message_id / (role, content) 去重，也不会重复。
        """
        st = self.state(sid)
        have = ids_of(st.messages)
        out = []
        for p in prompts or []:
            if (getattr(p, "name", "") or "") != "message":
                continue
            content = getattr(p, "content", "")
            if content in ("", None):
                continue
            refs = [r for mid in ids_of([{"role": "user", "content": content}])
                    for r in (media_by_id or {}).get(mid, [])]
            content = with_owned_images(content, refs, self.raw_sid(sid))
            _backup_media(content, self.raw_sid(sid))
            msg = self._as_openai({"role": "user", "content": content})
            ids = ids_of([msg])
            if ids and ids <= have:
                continue
            out.append(msg)
            have |= ids
        if not out:
            return 0
        st.messages.extend(clone_message(m) for m in out)
        logger.debug(f"[multi-decision] {sid} 本轮 {len(out)} 条消息并入窗口")
        self.settle_decision_notes(sid)
        return len(out)

    def normalize(self, messages, sid: str = "") -> List[Any]:
        """窗口还没接管时，按窗口的格式把框架历史整理一遍（v2.32）。

        窗口为空（v2.31 不灌历史）的那几轮，request.messages 一直是框架原文，
        时间戳是 `[Oct 20 2026 12:36 Tue]`；等窗口攒起来接管后又变成 `[12:36]`，
        中间每一条的前缀都变了，缓存整段作废。所以即使还没接管，
        也要按同一套规则处理（去观察窗残留 / 去报错 / 压时间戳 / 限图 / 加名字）。
        """
        msgs = strip_observe(list(messages or []))
        msgs = strip_error_messages(msgs)
        # v3.97：旧注记可能还残留在窗口里，先清掉，出口再按最新重插一遍
        msgs = [m for m in msgs if not is_decision_note(m)]
        msgs = expand_packed_messages(msgs)
        # 拆包会把整包图片都挂到最后一条，按 message_id 哈希挪回原主（v2.54）
        msgs = regroup_images(msgs)
        msgs = shrink_timestamps(msgs)
        msgs = strip_old_images(msgs, self._int_eff("history_images", sid, 128))
        # bot 自己说的话也要带时间与名字（用户 m30009 / m32754）：框架存的
        # assistant 只有 <msg message_id="…">，既没时间也没名字，模型分不清谁说的。
        # 顺序不能反：名字要落在 <msg ...> 之后，时间要落在最前面。
        msgs = self._prefix_assistant(msgs, sid)
        msgs = stamp_assistant_time(msgs)
        return msgs

    def snapshot(self, sid: str) -> List[Any]:
        """给 agent 看的窗口快照：当前全部内容，只读不裁。

        中期摘要（如果启用且有内容）作为最前面的一条 user 消息插进去，
        它**不计入** window_keep 的条数，裁切只看真实历史。
        """
        st = self._states.get(sid)
        if st is None or not st.messages:
            return []
        msgs = strip_observe(st.messages)
        msgs = strip_error_messages(msgs)
        # v4.03（用户 m11947）：注记现在真写进窗口本体了（commit_decision），
        # 出口不再剔旧、也不再按分钟轴重插——重插一次位置就可能变一次，前缀全作废。
        # 窗口本体存的是框架原始打包形态（一条 user 装着十几条群消息），
        # 发出去的历史必须和本轮一样是单条：页面上不会一坨，去重时按
        # message_id 也对得上（v2.39，用户 m23841）。
        msgs = expand_packed_messages(msgs)
        # 拆包会把整包图片都挂到最后一条，按 message_id 哈希挪回原主（v2.54）
        msgs = regroup_images(msgs)
        msgs = shrink_timestamps(msgs)
        msgs = strip_old_images(msgs, self._int_eff("history_images", sid, 128))
        # bot 自己说的话也要带时间与名字（用户 m32754）：这两个函数原来只挂在
        # messages_of / normalize 上，真正发出去的 snapshot 漏了，请求原文里
        # assistant 既没有 [HH:MM] 也没有 [桃华]。
        msgs = self._prefix_assistant(msgs, sid)
        msgs = stamp_assistant_time(msgs)
        head = self._summary_head(sid)
        if head is not None:
            msgs = [head] + msgs
        # v4.03（用户 m11947）：决策注记已经是窗口本体里的一条真实消息
        # （commit_decision 一次性写进去），出口只做格式，不再往中间插。
        # 出口统一兜底：窗口里任何一条是 dict 都会让框架 assemble_prompt 崩
        return self._fix_tool_pairs([self._as_openai(m) for m in msgs])

    def decision_snapshot(self, sid: str) -> List[Any]:
        """决策阶段读到的上下文：实时窗口 + 还没发出去的待发（v3.95，用户 m09171）。

        决策接管的是「这一轮要不要开口」，那它读的就必须是这一轮真正的那批消息。
        待发攒着的群消息框架记忆里还没有（没跑完一轮 agent 框架不写记忆），窗口
        本体也还没并进来；以前只给 snapshot，窗口一空就读到空记录，于是判出
        「没人说话」。这里按 collect 时同一套格式把待发接在窗口后面。
        """
        base = self.snapshot(sid)
        st = self._states.get(sid)
        if st is None:
            return base
        try:
            pend = self._pending_messages(st)
        except Exception:
            logger.exception("[multi-decision] 取待发失败（决策按窗口判）")
            return base
        if not pend:
            return base
        pend = shrink_timestamps(pend)
        pend = self._prefix_assistant(pend, sid)
        pend = stamp_assistant_time(pend)
        out = list(base)
        for m in pend:
            out.append(self._as_openai(m))
        return out

    def decision_incoming(self, sid: str) -> list:
        """决策这一轮真正新来的那批：还挂在「待发」里的消息（v3.97，用户 m09615 图9）。

        决策快照以前把整段 transcript（历史＋待发）当成 ③，跟 ② 是同一份东西，
        页面上一眼就是「本轮消息把历史又叠了一遍」，转发消息还会被拆成好几条。
        这里单独给出待发那一批，③ 只画它，条数才和真正新来的对得上。
        """
        st = self._states.get(sid)
        if st is None:
            return []
        try:
            pend = self._pending_messages(st)
        except Exception:
            logger.exception("[multi-decision] 取待发失败（决策快照的 ③ 留空）")
            return []
        if not pend:
            return []
        pend = shrink_timestamps(pend)
        pend = self._prefix_assistant(pend, sid)
        pend = stamp_assistant_time(pend)
        # 待发里每条 user 的原始图片引用，顺序和 pend 里的 user 项一一对应
        refs_seq = []
        for it in list(getattr(st, "pending", []) or []):
            if not isinstance(it, dict) or str(it.get("mark") or "") == "bot":
                continue
            if (not str(it.get("raw") or "").strip()
                    and not str(it.get("text") or "").strip()
                    and not (it.get("media_refs") or it.get("media_path"))):
                continue
            refs_seq.append(it.get("media_refs") or [])
        out = []
        raw_sid = self._raw_sids.get(sid) or ""
        ui = 0
        for m in pend:
            content = _jsonable(_get(m, "content"))
            role = str(_get(m, "role") or "user")
            item = {"role": role, "content": content}
            # v4.01（用户 m11166 图4）：③ 要跟窗口里的卡片一个长相。只存 role+content 时
            # 图片那条只能显示「[图片]」四个字，窗口里同一批消息是有缩略图的。
            try:
                media = media_list(content, raw_sid)
            except Exception:
                media = []
            if role == "user":
                refs = refs_seq[ui] if ui < len(refs_seq) else []
                ui += 1
                if not media and refs:
                    media = refs_media(refs, raw_sid)
            if media:
                item["media"] = media
            nm = _get(m, "name") or ""
            if nm:
                item["name"] = str(nm)
            out.append(item)
        return out

    @staticmethod
    def _as_openai(msg):
        """把窗口里可能残留的 dict 消息换成框架的消息对象。

        早期 commit_round 存的是 {"role": ..., "content": ...} 这种纯字典。
        框架 assemble_prompt 里有一句 self.messages[0].role，字典没有 .role
        就直接抛 AttributeError；而事件总线的 except 把异常整个吞掉，一行
        日志都不打。表现就是：投递成功、没有 processing message、没有
        Running agent、没有请求记录、会话 busy 永久锁死，重启才恢复（v2.46）。

        字段一个都不能丢（v2.51）：assistant 的 tool_calls 和 tool 消息的
        tool_call_id 是配对关系，只留 role/content 就变成「有工具结果、没工具
        调用」，上游直接 400 tool_call_mismatch，而且重试无效——用户 m26107
        在群 1145141974 撞上的就是这个。
        """
        if OpenAIMessage is None or not isinstance(msg, dict):
            return msg
        try:
            return OpenAIMessage(
                role=str(msg.get("role") or "user"),
                content=msg.get("content"),
                reasoning_content=str(msg.get("reasoning_content") or ""),
                tool_calls=list(msg.get("tool_calls") or []),
                tool_call_id=msg.get("tool_call_id") or None,
                name=msg.get("name") or None,
            )
        except Exception:
            return msg

    @staticmethod
    def _fix_tool_pairs(messages: List[Any]) -> List[Any]:
        """保证 tool_calls 和 tool 结果一一对应（v2.51）。

        上游对工具记录极严：assistant 声明了 tool_calls 却没有对应结果、
        或者有 tool 结果却找不到对应的调用，整个请求直接 400
        tool_call_mismatch，重试也没用（用户 m26107）。窗口裁切是按条裁的，
        很容易把一对拆开；老版本 _as_openai 又把字段洗掉了。所以发出去之前
        在这里补一次：孤立的 tool 消息丢掉，找不到结果的调用从 tool_calls
        里摘掉（摘空就当普通 assistant 消息）。
        """

        def g(msg, key, default=None):
            if isinstance(msg, dict):
                return msg.get(key, default)
            return getattr(msg, key, default)

        def with_calls(msg, calls):
            if isinstance(msg, dict):
                out = dict(msg)
                out["tool_calls"] = calls
                return out
            try:
                return msg.model_copy(update={"tool_calls": calls})
            except Exception:
                return msg

        known = set()
        for m in messages or []:
            if str(g(m, "role", "")) != "assistant":
                continue
            for c in g(m, "tool_calls") or []:
                cid = g(c, "id")   # 元素可能是 dict，也可能是对象，统一走 g
                if cid:
                    known.add(str(cid))
        solved = set()
        for m in messages or []:
            if str(g(m, "role", "")) == "tool" and g(m, "tool_call_id"):
                solved.add(str(g(m, "tool_call_id")))

        out = []
        for m in messages or []:
            role = str(g(m, "role", ""))
            if role == "tool":
                cid = g(m, "tool_call_id")
                if not cid or str(cid) not in known:
                    continue
                out.append(m)
                continue
            calls = g(m, "tool_calls") if role == "assistant" else None
            if calls:
                keep = [c for c in calls if str(g(c, "id") or "") in solved]
                if len(keep) != len(calls):
                    m = with_calls(m, keep)
                out.append(m)
                continue
            out.append(m)
        return out

    def _bot_name(self, sid: str = "") -> str:
        """这个会话里 bot 叫什么：单群手填 > 探测到的昵称 > 全局手填。"""
        name = ""
        try:
            name = str(self.plugin.bot_name_for(sid) or "").strip()
        except Exception:
            name = ""
        if not name:
            # 老版本 plugin 没有 bot_name_for：退回按会话探测到的昵称
            try:
                names = getattr(self.plugin, "bot_names", None)
                if isinstance(names, dict):
                    name = str(names.get(str(sid or "")) or "").strip()
            except Exception:
                name = ""
        return name

    def _prefix_assistant(self, messages: List[Any], sid: str = "") -> List[Any]:
        """给机器人自己的历史消息前面加上「【名字】」。

        只改发出去的那一份，窗口本体不动。用户在消息里本来就有
        `[Oct 19 2026 16:31 Mon] [user_nickname: X] |` 这一串前缀，
        assistant 侧只有 `<msg message_id="...">`，两边长得不像，
        长对话里模型容易把自己说过的话当成别人说的。
        """
        # 名字按会话取：群里别人看到的是群名片，私聊看到的是 QQ 昵称，
        # 同一个 bot 在两个地方名字可以不一样，全局单值会互相覆盖。
        #
        # 取值统一走 plugin.bot_name_for：单群手填 > 该会话探测到的昵称 > 全局手填。
        # 用户可以在设置里自己填名字（框架只给 sender.nickname、拿不到群名片），
        # 所以不能再只看 bot_names。取不到就原样返回，历史里已有的前缀保持不动。
        name = self._bot_name(sid)
        if not name:
            # 名字只从配置里取（单群手填 > 全局「机器人昵称」 > 适配器探测），
            # 配置里一处都没有就不加前缀——不编一个假名字（用户 m32887「别硬写入」）。
            return list(messages or [])
        tag = "[" + name + "] "
        out = []
        for msg in messages or []:
            if str(_get(msg, "role", "")) != "assistant":
                out.append(msg)
                continue
            # v4.03（用户 m11947）：注记的名字是「对当前聊天消息的决策建议」，
            # 再套一层 [桃华] 之后正文就不再以 [决策记录] 开头，认不出来了。
            if is_decision_note(msg):
                out.append(msg)
                continue
            cur = clone_message(msg)
            content = _get(cur, "content", "")
            if isinstance(content, str):
                body = content
                if body.strip():
                    content = _stamp_bot_tag(body, tag)
            elif isinstance(content, (list, tuple)):
                content = list(content)
                done = False
                for i, part in enumerate(content):
                    if done or not isinstance(part, dict):
                        continue
                    if isinstance(part.get("text"), str):
                        part = dict(part)
                        body = part["text"]
                        if body.strip():
                            part["text"] = _stamp_bot_tag(body, tag)
                            content[i] = part
                            done = True
            else:
                out.append(cur)
                continue
            if isinstance(cur, dict):
                cur["content"] = content
            else:
                try:
                    cur.content = content
                except Exception:
                    pass
            out.append(cur)
        return out

    # ------------------------------------------------------------------ 摘要

    def _int_eff(self, key: str, sid: str, default: int) -> int:
        try:
            v = self.plugin.eff(key, sid)
            return int(v)
        except Exception:
            return default

    def _summary_on(self, sid: str) -> bool:
        try:
            return bool(self.plugin.eff("summary_enabled", sid))
        except Exception:
            return False

    def summary_text(self, sid: str) -> str:
        if self.summaries is None:
            return ""
        return self.summaries.text(sid)

    def summary_info(self, sid: str) -> dict:
        if self.summaries is None:
            return {"text": "", "chars": 0, "lines": 0, "count": 0, "ts": 0.0}
        info = self.summaries.info(sid)
        info["enabled"] = self._summary_on(sid)
        return info

    def _summary_head(self, sid: str):
        """摘要消息对象；没有摘要或开关关着返回 None。"""
        if not self._summary_on(sid):
            return None
        text = self.summary_text(sid)
        if not text:
            return None
        if OpenAIMessage is not None:
            try:
                return OpenAIMessage(role="user", content=text)
            except Exception:
                pass
        return {"role": "user", "content": text}

    def _append_summary(self, sid: str, cut_messages: List[Any]) -> Optional[dict]:
        """把这次被裁掉的消息压进摘要。返回 {chars, lines, count} 或 None。"""
        if self.summaries is None or not self._summary_on(sid):
            return None
        try:
            line_chars = int(self.plugin.eff("summary_line_chars", sid))
        except (TypeError, ValueError):
            line_chars = 40
        try:
            max_chars = int(self.plugin.eff("summary_max_chars", sid))
        except (TypeError, ValueError):
            max_chars = 300
        new_lines = build_lines(cut_messages, max(8, line_chars))
        if not new_lines:
            return None
        old = self.summaries.text(sid)
        fresh = drop_overlap(body_of(old), new_lines)
        if not fresh:
            # 这批消息摘要里已经有了（第二次裁切常常重叠），不重复记
            return None
        text = merge_summary(old, fresh, max(60, max_chars))
        if not text:
            return None
        prev = self.summaries.get(sid)
        item = self.summaries.set(
            sid, text,
            count=int(prev.get("count") or 0) + len(fresh),
            lines=len(body_of(text)),
        )
        logger.info(
            f"[multi-decision] {sid} 中期摘要 +{len(new_lines)} 行，"
            f"现 {len(text)} 字 / {item.get('lines', 0)} 行"
        )
        return {"chars": len(text), "lines": int(item.get("lines") or 0),
                "count": int(item.get("count") or 0)}

    def clear_summary(self, sid: str) -> bool:
        if self.summaries is None:
            return False
        return self.summaries.clear(sid)

    # -------------------------------------------------------------- 图片缓存

    def media_cache_stats(self) -> dict:
        """插件替群友图片留的副本占了多少（用户 m10749 第 5 条）。

        只统计 media_cache 这一个目录：session_media 是框架的，插件既管不着
        也不该删。
        """
        cache = _media_cache_root()
        out = {"ok": True, "count": 0, "bytes": 0, "mb": 0.0,
               "limit": _MEDIA_CACHE_LIMIT, "limit_total": _MEDIA_CACHE_LIMIT,
               "groups": 1, "dir": ""}
        if cache is None:
            return out
        out["dir"] = str(cache)
        files = _media_cache_files(cache)
        total = 0
        for f in files:
            try:
                total += int(f.stat().st_size or 0)
            except Exception:
                continue
        out["count"] = len(files)
        out["bytes"] = total
        out["mb"] = round(total / 1048576.0, 1)
        # 「上限」是每个群组各自的上限（用户本轮），所以再报一个算出来的总配额
        # = 每群上限 × 群组数，页面拿它可以提示「现在最多能存多少」。
        try:
            owners = _media_cache_groups(cache, {f.name for f in files})
            groups = {g for gs in owners.values() for g in gs}
        except Exception:
            groups = set()
        out["groups"] = len(groups) or 1
        out["limit_total"] = int(_MEDIA_CACHE_LIMIT) * int(out["groups"])
        return out

    def clear_media_cache(self) -> dict:
        """清空图片缓存：删副本 + 删路径索引。

        索引必须一起删：留着就会指向已经不存在的文件，白让 public_media
        多查一圈。框架的 session_media 不动。
        """
        cache = _media_cache_root()
        if cache is None:
            return {"ok": False, "detail": "取不到数据目录"}
        freed = 0
        removed = 0
        for f in _media_cache_files(cache):
            try:
                freed += int(f.stat().st_size or 0)
                f.unlink(missing_ok=True)
                removed += 1
            except Exception:
                continue
        try:
            (cache / _MEDIA_INDEX_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        _MEDIA_INDEX.pop(str(cache), None)
        _MEDIA_INDEX_DIRTY.pop(str(cache), None)
        logger.info(f"[multi-decision] 已清空图片缓存：{removed} 张 / {freed} 字节")
        return {"ok": True, "removed": removed, "mb": round(freed / 1048576.0, 1)}

    def purge_media_cache(self, sid: str) -> dict:
        """删掉某个会话留在图片缓存里的副本（用户 m18059 第 1 条）。

        删会话时要一起清，否则缓存名片里会留一个「0 请求、只有几 MB 图片」的
        幽灵会话。缓存文件按内容 sha1 命名、各会话共用一份，所以只能按
        _index.json 里记下的原路径判归属（session_media/<sid>/...）：
        同一张图还被别的会话引用时，只删本会话的索引项、文件留着；
        最后一个引用也没了才删文件。
        """
        out = {"ok": True, "removed": 0, "bytes": 0, "mb": 0.0, "paths": 0}
        cache = _media_cache_root()
        target = _media_sid_key(sid)
        if cache is None:
            out.update(ok=False, detail="取不到数据目录")
            return out
        if not target:
            return out
        data = _index_load(cache)
        by_path = data.get("byPath") or {}
        by_hash = data.get("byHash") or {}
        by_sid = data.get("bySid") or {}
        pairs = []
        # 优先按写入时记下的会话归属删（缓存路径里那段是框架的群组哈希，认不出 gm:/dm:，
        # 以前按路径判归属永远一条都对不上，删会话时一张图也清不掉）。
        bucket = by_sid.get(target) or {}
        if bucket:
            for rel_key, name in list(bucket.items()):
                pairs.append((rel_key, str(name or by_path.get(rel_key) or "")))
            by_sid.pop(target, None)
        if not pairs:
            for rel_key, name in list(by_path.items()):
                if _media_sid_key(_media_sid_of_rel(rel_key)) == target:
                    pairs.append((rel_key, str(name or "")))
        if not pairs:
            return out
        for rel_key, _name in pairs:
            by_path.pop(rel_key, None)
        # 还有别的会话路径指向同一个缓存文件吗？有就留着文件
        still = set(by_path.values())
        for rel_key, name in pairs:
            if not name or name in still:
                continue
            path = cache / name
            try:
                size = int(path.stat().st_size or 0)
                path.unlink()
                out["removed"] += 1
                out["bytes"] += size
            except Exception:
                continue
        # 指向已删文件的 byHash 一起清掉，别让 public_media 白查一圈
        alive = {f.name for f in _media_cache_files(cache)}
        for digest, name in list(by_hash.items()):
            if name not in alive:
                by_hash.pop(digest, None)
        data["byPath"] = by_path
        data["byHash"] = by_hash
        data["bySid"] = by_sid
        try:
            _index_save(cache, force=True)
        except Exception:
            pass
        out["paths"] = len(pairs)
        out["mb"] = round(out["bytes"] / 1048576.0, 1)
        logger.info(
            f"[multi-decision] 已清掉 {sid} 的图片缓存：{out['removed']} 张 / {out['mb']}MB"
        )
        return out

    # ------------------------------------------------------------------ 裁切

    def maybe_trim(self, sid: str, trigger: str = "idle_check",
                   event_id: str = "") -> Optional[dict]:
        """超阈值就裁到保留条数。

        两条线：①攒够 window_trim_threshold 条 user（默认 60）就裁；②攒够
        「阈值 + window_buffer」条 user（默认 70）强制裁，不看最小裁切条数。
        两条线都只数 user，保留条数（window_keep，默认 30）数全量计入消息。
        v3.89（用户 m05321 第 2 条）：保留条数里要先扣掉「待发」——待发下一轮就会
        并进窗口，历史还按满 keep 留的话，裁完立刻又挨着触发线。
        v3.71（用户 m23596 第 3 条）：触发线把「待发」也算进来——休息时段待发
        堆着不走，窗口内的 user 永远到不了阈值，上下文就一直膨胀。

        两处调用：一轮结束确认真空闲时（trigger=idle_check，兜「之后没人再发
        消息」），以及每轮请求组装之前（trigger=before_request，用户 m03343）。
        请求前那处虽然在一轮中间，但只按窗口尾部算条数、不碰进行中的轮次状态；
        它保证发出去的请求就是保留条数。event_id 是调用方手里已知的轮次身份，
        请求前裁切时这轮还没 flush，不传就会记成上一轮。

        这里只做串联：判该不该裁（_trim_gate，只读窗口）→ 动手裁（_trim_apply，
        改窗口）→ 记账（_trim_record，写日志 / 中期摘要 / 裁切快照）。三段拆开
        之前就是一串顺序语句，顺序与早退条件都没动。
        """
        plan = self._trim_gate(sid, trigger)
        if plan is None:
            return None
        done = self._trim_apply(sid, plan)
        if done is None:
            return None
        return self._trim_record(sid, plan, done, trigger, event_id)

    def _trim_gate(self, sid: str, trigger: str) -> Optional[dict]:
        """判这一轮要不要裁。只读窗口，不碰任何状态；返回 None＝不裁。

        配置与两条阈值线、待发口径、以及 v3.72 的「窗口没有可裁余量」全都挡在
        这里，出去之后就不再有早退。
        """
        plugin = self.plugin
        if not plugin.eff("window_enabled", sid):
            return None
        st = self._states.get(sid)
        if st is None:
            return None

        keep = max(1, int(plugin.eff("window_keep", sid)))
        threshold = max(keep + 1, int(plugin.eff("window_trim_threshold", sid)))
        # 「30/60 指的是积累，不算工具调用」：阈值与保留都按计入条数算，
        # 否则一次工具往返算三条，窗口很快就假满了。
        # v3.23（用户 m45332）：**触发**只看用户消息。bot 自己的回复也算 1 条
        # 的话，攒够 60 条里可能有一半是 bot 说的，裁切会提前触发。
        # 用户 m02080：**保留**改成全量口径。原来说保留 30 条，实际留了 30 条
        # user 再加十几条 bot，屏幕上就是 40 多条。现在 keep 指「保留窗口里
        # 总共还剩几条计入消息」，bot 回复和工具往返都占名额。
        before = st.size()
        counted_before = count_messages(st.messages)
        # v3.71（用户 m23596 第 3 条）：休息时段 bot 待发堆着不走，窗口一直不裁。
        # 待发那些消息下一轮就会整批进窗口，可触发线原来只数窗口内的 user，
        # 于是「窗口 40 条 user + 待发 25 条」永远到不了 60，上下文一直膨胀。
        # 现在触发线按「窗口内用户 N + 待发 M = 合计 K」算；裁切本身仍只动窗口
        # （待发要等下一轮采集才进窗口），记录里 user_before 仍是窗口内口径，
        # 合计另记 pending_before / user_total_before，页面看窗口还是原来那个数。
        user_before = count_user_messages(st.messages)
        try:
            pending_before = int(self.pending_counted(sid) or 0)
        except Exception:  # pragma: no cover - 统计失败绝不能挡住裁切
            pending_before = 0
        user_total_before = user_before + pending_before
        # v3.52（用户 m08226 第 7 条）：裁切缓冲。概率设得很低的群可能几十条消息
        # 都不触发请求，只按触发阈值走窗口会无限长，所以给一条「不看触发阈值」的
        # 强制裁线。用户 m11091 第 4/6 条：这条线原来拿 counted_before（含 bot 回复
        # 与工具往返）去比 keep + buffer，30+10 实际四十来条就裁——用户在快照里
        # 看到「55 条 user 就裁了」就是这么来的。缓冲跟触发阈值同一个口径，
        # 按 **user 条数** 算：到「阈值 + 缓冲」（默认 60+10=70 条 user）才强制裁，
        # 窗口整体长度仍由 keep 那条线兜住，不会无限长。
        # v3.72（用户 m33207 第 3 条）：窗口已经只剩保留条数时，待发堆得再多也裁不动。
        # 之前不看这个：窗口 30 条 + 待发 37 条 = 67 一样判「该裁」，可 trim_by_count
        # 一条也裁不掉（cut=0），于是每 30 秒往裁切记录里写一条空记录——10/26 早上
        # 07 点一个小时就写了 56 条，趋势图上看到的是「24 条 user 也裁了」。
        # 窗口没有可裁余量就直接跳过，等下一轮待发真进了窗口再裁。
        if counted_before <= keep:
            return None
        buffer_extra = max(0, int(plugin.eff("window_buffer", sid) or 0))
        # v3.75（用户本轮第 5 条）：**不是请求前**的裁切要等到「阈值 + 缓冲」才动手。
        # 用户原话：「没说还没触发过的消息到 60 条 user 就直接进行裁切啊，只有触发
        # 第 70 条缓冲才允许非触发下裁切」。请求前那一次（before_request）必须把
        # 窗口压到 keep 才发得出去，仍旧按 60 走；idle_check / buffer 这类「没人说话
        # 时顺手裁」的动作，攒不到 70 就别动——否则窗口里的 60 条还没进过一次请求
        # 就被裁掉，快照上一根柱子都没有。
        # v4.11（用户 m17596 第 1 条）：决策判要回之后的那一次（trigger="decision"）
        # 也按阈值 60 走——用户原话「决策执行的时候也可以触发裁切 就是按设置的60来」。
        # 它跟 idle_check 一样不是请求前裁切，所以仍旧单独落一份裁切快照（橙柱）。
        need = threshold if trigger in ("before_request", "decision") else threshold + buffer_extra
        if user_total_before < need:
            return None
            # v3.66（用户 m19536 第 7 条）：「最小裁切条数」撤掉了。原来这里还有一道
            # 「本次能裁的条数 < min_to_trim 就不裁」，阈值与保留贴得近时会把裁切
            # 永久卡死；现在阈值一旦到了就按 keep 裁，裁多少算多少。

        if str(getattr(plugin, "trim_strategy", "mechanical")) == "llm" and not self._llm_warned:
            self._llm_warned = True
            logger.warning("[multi-decision] trim_strategy=llm 尚未实现，本次及后续仍按机械裁切执行")
        protect = bool(plugin.eff("protect_tool_groups", sid))
        before_chars = self._chars(st.messages)
        before_msgs = list(st.messages)
        return {
            "st": st,
            "keep": keep,
            "protect": protect,
            "before": before,
            "counted_before": counted_before,
            "user_before": user_before,
            "pending_before": pending_before,
            "user_total_before": user_total_before,
            "before_chars": before_chars,
            "before_msgs": before_msgs,
        }

    def _trim_apply(self, sid: str, plan: dict) -> Optional[dict]:
        """按 plan 真的裁窗口。返回 None＝一条也没裁掉（空裁，不记账）。

        这一段只改 st.messages 与 st.framework_seen_ids，不写任何记录。
        """
        st = plan["st"]
        keep = plan["keep"]
        protect = plan["protect"]
        before = plan["before"]
        counted_before = plan["counted_before"]
        user_before = plan["user_before"]
        before_msgs = plan["before_msgs"]
        # v4.30：裁切只移除正文，不遗忘已接收身份（含尚未被框架回传的发送回显）。
        seen = dict.fromkeys(st.framework_seen_ids)
        for mid in sorted(ids_of(before_msgs)):
            seen.pop(mid, None)
            seen[mid] = None
        st.framework_seen_ids = list(seen)[-4096:]
        # v3.89（用户 m05321 第 2 条）：保留条数要**把待发一起算**。
        # 用户原话：「刚才70条触发的时候，是保留了30条真历史+剩下接近20条待发的，
        # 导致下次窗口距离裁切线依旧很近，应该是计算待发的情况下裁切，也就是大概剩
        # 10条左右的历史+20条左右的待发」。原来 keep 只按窗口算，裁完 30 条历史，
        # 待发那 20 条下一轮照样并进来 → 立刻 50 条，离 70 条只差 20，很快就又裁。
        # 现在历史只留 keep - 待发条数；待发本身就超过 keep 时留 1 条（历史不能空）。
        try:
            pend_msgs = self._pending_messages(st)
        except Exception:  # pragma: no cover - 待发坏了也不能挡住裁切
            logger.exception("[multi-decision] 生成待发消息失败，保留条数退回原口径")
            pend_msgs = []
        pend_all = count_messages(pend_msgs)
        keep_hist = max(1, keep - pend_all)
        hist_before = before_msgs
        # user_only=False：keep 按全量计入消息算（用户 m02080）
        cut = trim_by_count(st.messages, keep_hist, protect=protect, user_only=False)
        # v4.11（用户 m17596 第 1 条后半）：裁完窗口第一条要是 assistant，说明它前面
        # 那条 user 已经被裁走了，模型读到的开头就是「我上一句说了什么」——上下文从
        # 半截对话开始。再往前多裁一条，让窗口从 user 开始（用户原话「如果裁切后
        # 第一条是 assistant 的消息 需要多裁一条」）。
        cut_extra = 0
        if cut > 0 and len(st.messages) > 1:
            _head = st.messages[0]
            try:
                _role = _head.get("role") if isinstance(_head, dict) else getattr(_head, "role", "")
            except Exception:
                _role = ""
            if str(_role or "") == "assistant":
                st.messages.pop(0)
                cut_extra = 1
        cut_msgs = hist_before[:cut + cut_extra] if (cut > 0 or cut_extra > 0) else []
        # cut==0 但条数没少、计入条数变少了 = 只把窗口第一条（一条装着十几条群
        # 消息的打包 user）劈了一刀。被劈掉的那半段也是真实消息，得进中期摘要，
        # 否则摘要出现缺口（审查 bug）
        if cut <= 0 and before_msgs and (
            count_messages(st.messages) < counted_before
            or count_user_messages(st.messages) < user_before
        ):
            cut_msgs = [before_msgs[0]]
        after = st.size()
        counted_after = count_messages(st.messages)
        user_after = count_user_messages(st.messages)
        after_chars = self._chars(st.messages)
        # v3.72（用户 m33207 第 3 条）：一条都没裁掉就别留记录。以前这种「空裁」
        # 照样写一条（cut=0、before == after），页面上一堆「24 条就裁了」，
        # 实际窗口一点没变。真劈了一刀（cut_msgs 非空）的情况不算空裁。
        if (cut <= 0 and after == before and counted_after == counted_before
                and user_after == user_before and not cut_msgs):
            logger.debug(
                f"[multi-decision] {sid} 裁切空转（窗口 {before} 条已到保留 "
                f"{keep}，待发 {pend_all} 条），跳过"
            )
            return None
        return {
            "cut": cut,
            "cut_extra": cut_extra,
            "cut_msgs": cut_msgs,
            "after": after,
            "counted_after": counted_after,
            "user_after": user_after,
            "after_chars": after_chars,
            "pend_msgs": pend_msgs,
            "pend_all": pend_all,
            "keep_hist": keep_hist,
        }

    def _trim_record(self, sid: str, plan: dict, done: dict, trigger: str,
                     event_id: str) -> dict:
        """记账：状态计数、裁切记录、轮次身份、中期摘要、快照、一行 info 日志。

        这里不改窗口内容，只写。返回值就是 maybe_trim 的返回值（record）。
        """
        plugin = self.plugin
        st = plan["st"]
        keep = plan["keep"]
        protect = plan["protect"]
        before = plan["before"]
        counted_before = plan["counted_before"]
        user_before = plan["user_before"]
        pending_before = plan["pending_before"]
        user_total_before = plan["user_total_before"]
        keep_hist = done["keep_hist"]
        pend_msgs = done["pend_msgs"]
        pend_all = done["pend_all"]
        before_chars = plan["before_chars"]
        before_msgs = plan["before_msgs"]
        cut = done["cut"]
        cut_extra = done["cut_extra"]
        cut_msgs = done["cut_msgs"]
        after = done["after"]
        counted_after = done["counted_after"]
        user_after = done["user_after"]
        after_chars = done["after_chars"]
        now = time.time()
        st.trimmed += 1

        record = {
            "ts": now,
            "sid": sid,
            "before": before,
            "after": after,
            "counted_before": counted_before,
            "counted_after": counted_after,
            # v3.23：真正触发裁切的是这两个（只数用户消息），counted_* 仍记全量留档
            "user_before": user_before,
            "user_after": user_after,
            # v3.71（用户 m23596 第 3 条）：触发时把待发一起算了，这两个字段说明
            # 多出来的那部分是待发；user_before / user_after 仍是窗口内口径。
            "pending_before": pending_before,
            "user_total_before": user_total_before,
            # v3.89（用户 m05321 第 2 条）：这次裁切实际按多少条历史留的
            # （= window_keep - 待发计入条数），以及待发一共几条原始项
            "keep_hist": keep_hist,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "cut_index": cut + cut_extra,
            # 切点是不是「直接按原始条数切」的位置；不是就说明被工具往返或计入口径挪过
            # cut 和 before 都是原始条数口径，keep 是计入条数口径。只在切点比
            # 基准更靠前（保护逻辑多切了）时才算被调整过，否则这字段恒为 True、
            # 记下来也没信息量（审查 bug）
            "tool_group_adjusted": bool(protect and cut > 0 and cut < max(0, before - keep)),
            "trigger": trigger,
            "summary_generated": False,
        }
        # final 先落盘请求、再裁切；把稳定事件身份一起记下来。
        request_log = getattr(plugin, "request_log", None)
        if event_id:
            # 请求前裁切：这轮还没 flush，直接用调用方给的轮次身份，
            # 免得面板把这次裁切错标成上一轮（用户 m03343）
            record["event_id"] = event_id
            record["round_ts"] = now
        elif request_log is not None:
            try:
                latest = request_log.list_rounds(sid, 1)
                if latest and float(latest[0].get("ts") or 0) <= now:
                    record["event_id"] = latest[0].get("event_id") or ""
                    record["round_ts"] = latest[0].get("ts") or 0
            except Exception:
                logger.exception("[multi-decision] 关联裁切轮次失败")
        info = self._append_summary(sid, cut_msgs)
        if info:
            record["summary_generated"] = True
            record["summary_chars"] = info["chars"]
        if self.log is not None:
            self.log.write(record)
        # v3.75（用户本轮第 5 条）：这种「没人说话时顺手裁」不挂在任何一次请求上，
        # 以前只留一行窗口日志，趋势图上就看不见——窗口在「实时快照」那根柱子里
        # 突然变短。现在单独落一份快照，列表里多一次独立记录。
        if trigger != "before_request":
            try:
                rlog = getattr(plugin, "request_log", None)
                if rlog is not None:
                    # v3.88（用户 m05012 第 2 条）：快照条数按它自己的内容算，不硬拔高。
                    # v3.89（用户 m05321 第 1 条）：快照 = 触发那一刻的完整内容，即
                    # **裁前窗口 + 攒着的待发**，待发那几项逐条标 pending（页面显示
                    # 「待发」+ 虚线框）。用户原话：「触发时候，当前这些消息应该变成
                    # 快照，待发也要算进快照里，你可以在快照里标注这些是待发」。
                    # 裁切本身只动窗口，所以下一轮的实时窗才是裁后的 keep 条。
                    snap = list(before_msgs) + list(pend_msgs)
                    view = view_of(snap, raw_sid=self.raw_sid(sid))
                    if pend_msgs:
                        for it in view[max(0, len(view) - len(pend_msgs)):]:
                            it["pending"] = True
                    rlog.write_trim_snapshot(
                        sid, snap, record, view=view, pending_n=len(pend_msgs)
                    )
            except Exception:
                logger.debug(
                    "[multi-decision] 写裁切快照失败（已吞掉）", exc_info=True
                )
        logger.info(
            f"[multi-decision] {sid} 窗口裁切 {before} → {after} 条"
            f"（窗口内用户 {user_before} + 待发 {pending_before} = 合计 {user_total_before}，"
            f"历史留 {keep_hist} 条、待发 {pend_all} 条，"
            f"裁后用户 {user_after}，计 {counted_before} → {counted_after}，{trigger}）"
        )
        return record

    def _chars(self, messages: List[Any]) -> int:
        """按「真正发出去的文字」算字符数。

        多模态消息的 content 是数组，以前走 str(content) 会把 Python 字典表示算进去，
        一条带图消息能虚报几百字，裁切记录里的「字符 X → Y」就失真。
        """
        total = 0
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
            total += len(text_of_content(content))
        return total

    # ------------------------------------------------------------------ 收尾

    def clear(self, sid: str) -> None:
        self._states.pop(sid, None)

    def shutdown(self) -> None:
        # 退出前把私聊窗口强制落盘一次（限频会挡掉最后这几秒的变更）
        try:
            self._save_dm(force=True)
        except Exception:
            logger.debug("[multi-decision] 退出前私聊窗口落盘失败（已吞掉）", exc_info=True)
        self._states.clear()
        # 退出前把媒体索引强制落盘：不然刚备份的副本重启后找不回来
        cache = _media_cache_root()
        if cache is not None:
            _index_save(cache, force=True)


# 任何改窗口的动作之后都刷一次寄存处。
# 包在方法外面而不是逐个里加，避免以后新增分支忘记存。
def _persist(fn):
    @functools.wraps(fn)
    def wrap(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._save()
    return wrap


for _name in ("push_pending", "mark_pending", "collect", "normalize",
              "maybe_trim", "commit_decision", "_append_summary", "clear_summary",
              "forget", "clear"):
    setattr(WindowManager, _name, _persist(getattr(WindowManager, _name)))
