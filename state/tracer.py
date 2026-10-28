"""按回合记录 agent 调用的全过程。

一轮 = 一次 ON_LLM_REQUEST 到 ON_FINAL_RESULT。记下来的东西：

- （v3.63 起不再抄请求：messages/system/工具清单都在请求快照 requests/<sid>/<ts>.json 里）
- 每一步：模型的文字输出、思维链、工具调用、token（输入/输出/缓存命中）、耗时
- 工具：每个工具返回了什么（ON_TOOL_RESULT 单独记一份，因为它比 steps 早）
- 发出：最终发出去的每条消息与结果

用途是把「模型这一轮到底看到了什么、想了什么、调了什么、花了多少」落到一个
json 里，出问题能直接摊开看，不用再靠猜。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from core.logging_manager import get_logger

from .sids import normalize_sid

# v3.47：账本/统计的异常一律只记日志，不影响业务
logger = get_logger("multi_decision", "orange")

MAX_TEXT = 8000
MAX_TOOL = 8000

# 读不到模型名时统计里用的名字（历史记录没有这个字段，但不能让它凭空消失）
MODEL_UNKNOWN = "未记录模型"

# v3.47（用户 2026-10-24 第 3/4 条）：24 小时 token/用时的账本只保留这么多天。
# traces 每个会话只留 keep 份（默认 60），账本是另一份流水、不受 keep 影响，
# 只按时间滚动清理，重写时原子替换。
LEDGER_KEEP_DAYS = 30


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clone_totals(out: dict) -> dict:
    """浅拷贝 + 单独拷 models 那一层：缓存里的嵌套 dict 不能被调用方改掉。"""
    d = dict(out)
    d["models"] = {k: dict(v) for k, v in (out.get("models") or {}).items()}
    return d

_BAD = re.compile(r"[^0-9A-Za-z_.-]+")


def _safe(name: str) -> str:
    s = _BAD.sub("_", str(name or "unknown"))
    return s[:60] or "unknown"


def chain_text(chain: Any) -> str:
    """把 ON_MESSAGE_SENT 的 action 拍平成纯文本（v2.63，从 engine 搬过来）。

    MessageChain 只有 __repr__、没有 to_text，直接 str() 会得到
    MessageChain([Text('...')]) 这种对象字面量，页面上「最后实际发出」那一栏
    全是这个（用户 m29392 图1）。按元素拍平：Text 取正文，Reply 丢掉。
    """
    if chain is None:
        return ""
    if isinstance(chain, str):
        return chain.strip()
    items = getattr(chain, "message_list", None)
    if items is None:
        raw = getattr(chain, "text", None)
        if isinstance(raw, str):
            return raw.strip()
        return str(chain).strip()
    out: List[str] = []
    try:
        seq = list(items)
    except Exception:
        seq = []
    for el in seq:
        if el is None:
            continue
        try:
            rep = getattr(el, "repr", None)
        except Exception:
            rep = None
        if not isinstance(rep, str):
            rep = str(el)
        if rep.startswith("[Reply"):
            continue
        out.append(rep)
    return "".join(out).strip()


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _model_of(obj: Any) -> str:
    """这一轮用的是哪个模型（v3.37，用户 m02080 第 4 条要按模型算钱）。

    框架不同版本的 LLMResponse 字段名不一样，能读到哪个用哪个；都读不到就返回
    空串，统计里记成「未记录模型」，绝不随便填个默认名去算钱。
    """
    for key in ("model", "model_name", "model_id"):
        try:
            val = _attr(obj, key, "")
        except Exception:
            val = ""
        if val:
            return str(val)[:80]
    return ""


def _default_model_of(path: Any) -> str:
    """从框架配置 system_config.json 里读「默认模型」的展示名（用户 m03616 第 1 条）。

    框架的 LLMResponse 只有 token 之类字段，没有 model/model_name/model_id，
    所以轨迹里模型名长期是空串、价目表只能显示「未记录模型」。改读框架自己的
    默认模型：models.default_llm 形如 "provider_id:model_id"，对应
    providers[pid].model_config.llm[mid].model_name；取不到 model_name 就退回
    model_id（至少能跟供应商后台对上号）。只读这一条链，绝不碰 provider_config
    里的密钥等其他字段。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return ""
    models = raw.get("models")
    default = models.get("default_llm") if isinstance(models, dict) else ""
    pid, sep, mid = str(default or "").strip().partition(":")
    if not (sep and pid and mid):
        return ""
    providers = raw.get("providers")
    provider = providers.get(pid) if isinstance(providers, dict) else None
    cfg = provider.get("model_config") if isinstance(provider, dict) else None
    llms = cfg.get("llm") if isinstance(cfg, dict) else None
    item = llms.get(mid) if isinstance(llms, dict) else None
    name = item.get("model_name") if isinstance(item, dict) else ""
    return str(name or mid).strip()[:80]


def content_text(content: Any) -> str:
    """把消息 content 拉成纯文本：多模态只留文字，图片记占位。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        out: List[str] = []
        for item in content:
            if isinstance(item, dict):
                kind = item.get("type")
                if kind == "text":
                    out.append(str(item.get("text", "")))
                elif kind == "kira_image_ref":
                    # 框架把图落盘后，同一条消息的 text 里已经写了 [Image attached]；
                    # 这里再补一个 [kira_image_ref] 就变成「一个图两行标记」，页面上
                    # 看着像凭空多出来一堆图（用户 m29575）
                    continue
                elif kind in ("image_url", "image", "input_image"):
                    out.append("[图片]")
                elif kind:
                    out.append("[%s]" % kind)
            else:
                out.append(str(item))
        return "".join(out)
    return str(content)


def clip(text: str, limit: int = MAX_TEXT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（共 %d 字，已截断）" % len(text)


def _msg_view(msg: Any) -> dict:
    role = _attr(msg, "role", "") or ""
    text = content_text(_attr(msg, "content", ""))
    view = {"role": role, "chars": len(text), "text": clip(text)}
    tool_calls = _attr(msg, "tool_calls", None)
    if tool_calls:
        view["tool_calls"] = [
            {
                "id": _attr(tc, "id", "") or "",
                "name": ((_attr(tc, "function", {}) or {}).get("name") if isinstance(_attr(tc, "function", {}), dict) else ""),
            }
            for tc in tool_calls
        ]
    if _attr(msg, "tool_call_id", None):
        view["tool_call_id"] = _attr(msg, "tool_call_id")
    return view


def _prompt_view(p: Any) -> dict:
    text = _attr(p, "content", "")
    if not isinstance(text, str):
        text = content_text(text)
    return {
        "name": _attr(p, "name", "") or "",
        "chars": len(text),
        "text": clip(text),
    }


def _tool_name_of(call: dict) -> str:
    fn = call.get("function") if isinstance(call, dict) else None
    return (fn or {}).get("name", "") if isinstance(fn, dict) else ""


def _tool_args_of(call: dict) -> str:
    fn = call.get("function") if isinstance(call, dict) else None
    raw = (fn or {}).get("arguments", "") if isinstance(fn, dict) else ""
    return clip(str(raw or ""), 2000)


def timing_fields(rec: dict) -> dict:
    """Separate pipeline wall time from provider-reported LLM step durations."""
    def seconds(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value) and value >= 0:
                return float(value)
        return None

    wall = seconds(rec.get("elapsed"))
    if wall is None:
        started, ended = seconds(rec.get("started")), seconds(rec.get("ended"))
        if started is not None and ended is not None and ended >= started:
            wall = ended - started
    steps = rec.get("steps") or []
    durations = [seconds(step.get("time_consumed")) for step in steps]
    complete = bool(durations) and all(value is not None for value in durations)
    llm = sum(value for value in durations if value is not None)
    consistent = wall is not None and llm <= wall + 0.02
    return {
        "pipeline_elapsed_seconds": round(wall, 2) if wall is not None else None,
        "llm_elapsed_seconds": round(llm, 2) if durations else None,
        "non_llm_elapsed_seconds": round(max(0.0, wall - llm), 2)
            if complete and consistent else None,
        "llm_timing_complete": complete,
        "timing_inconsistent": wall is not None and not consistent,
        "timing_measurement": "pipeline_wall_clock_vs_provider_llm_seconds",
    }


class Tracer:
    """内存里按 event_id 攒一轮，回合结束时落成一个 json。"""

    def __init__(self, data_dir, enabled: bool = True, keep: int = 60,
                 keep_days: int = 0, keep_of=None, days_of=None):
        # v3.64（用户 m18870）：轨迹并进请求快照，不再单独建 traces/ 目录。
        # 读写都以 requests/<sid>/<ts>.json 里的 "trace" 键为准；份数/天数也只按
        # trace_keep 裁一次（同一份文件，没法各裁各的）。
        self.dir = Path(data_dir) / "traces"   # 只给旧数据的清理逻辑认路，读写都不用
        self.requests_dir = Path(data_dir) / "requests"
        # 请求快照的读写端：main.py 建好 RequestLog 之后注入（见 main.py 初始化）
        self.store = None
        # 没有请求快照可挂的轮数（异常路径），只做自检计数，不写盘
        self.dropped = 0
        self.enabled = bool(enabled)
        self.keep = max(1, int(keep or 60))
        # 用户 m13708 第 3 条：份数之外再加一条天数线（设置里的 traces_keep_days）。
        # 0 = 不限，只按每个会话的份数滚动。
        try:
            self.keep_days = max(0, int(keep_days or 0))
        except (TypeError, ValueError):
            self.keep_days = 0
        # 用户 m18059 第 4 条：缓存保留线按会话独立。keep_of(sid)/days_of(sid) 是
        # main.py 传进来的回调（读这个群的有效设置），没传就退回实例值。
        self._keep_of = keep_of
        self._days_of = days_of
        self._open: Dict[tuple[str, str], dict] = {}
        self._refs: Dict[tuple[str, str], list] = {}
        # 模型名兜底的缓存（见 default_model_name）：数据目录 + 解析到的配置路径 + mtime
        self._data_dir = Path(data_dir)
        # v3.47（用户 2026-10-24 第 3/4 条）：24 小时 token/用时的账本。
        # traces 会被 _prune 按 keep 裁掉，只读 traces 最多就能看到 60 轮，
        # 「请求平均用时」和「最近24小时 token」于是都被 60 截断（不是真实值）。
        # 账本跟 traces/ 并列放在 stats/ 下：<data>/stats/tokens.jsonl。
        self.ledger = self._data_dir / "stats" / "tokens.jsonl"
        self._ledger_cache: Optional[tuple] = None
        self._ledger_trim_at = 0.0
        self._cfg_path: Optional[Path] = None
        self._cfg_mtime: Optional[float] = None
        self._cfg_model: str = ""
        # v3.48（用户 m06761 第 1 条）：账本是这版才有的，升级当天 24 小时窗口里
        # 早先那些轮只剩 traces 还留着。第一次起来时把盘上轨迹回填进账本，
        # 「请求平均用时」「最近24小时 token」的轮数立刻能超过 keep=60 的截断。
        self._backfill_ledger()

    #: 等不到 finish 的条目超过这个秒数就当僵尸清掉
    #: （框架事件总线吞异常、看门狗 180s 强制释放 busy 都会走到这条路上）
    STALE_SECONDS = 900.0

    def _sweep_open(self) -> None:
        """清僵尸条目：一轮 ON_LLM_REQUEST 之后等不到 ON_FINAL_RESULT 时，
        _open[key] 里那份整请求（几十条消息、每条 clip 到 8000 字符）会一直挂着，
        反复失败内存单调增长（审查 bug）。
        """
        if not self._open:
            return
        now = time.time()
        for key, rec in list(self._open.items()):
            try:
                started = float(rec.get("started") or 0.0)
            except Exception:
                started = 0.0
            if not started or now - started > self.STALE_SECONDS:
                self._open.pop(key, None)
                self._refs.pop(key, None)

    # ------------------------------------------------------------ 生命周期

    def begin(self, event: Any, request: Any, trigger_at: float = 0.0,
              debounce: float = 0.0, decision: float = 0.0) -> Optional[dict]:
        """开始记一轮。

        trigger_at 是这一轮「被点起来」的墙钟（v3.51，用户 m07385 第 7 条）：
        请求用时要按「触发 → 请求结束」算，含前面的防抖等待，不含回复吐字。
        取不到就传 0，finish 时退回纯请求耗时。
        """
        if not self.enabled:
            return None
        self._sweep_open()
        key = self._key(event)
        started = time.time()
        try:
            trig = float(trigger_at or 0.0)
        except (TypeError, ValueError):
            trig = 0.0
        # 会话 id 一律按插件口径归一（去掉机器人名前缀）。
        # 轨迹目录名就是从这来的，读的时候 api 传的也是归一 id，
        # 两边不一致就会「记录明明写了、页面却读不到」。
        rec = {
            "sid": normalize_sid(self._sid(event)),
            "event_id": str(_attr(event, "event_id", "") or ""),
            "started": started,
            "ended": 0.0,
            "triggered_at": trig if 0 < trig <= started else 0.0,
            "debounce": round(started - trig, 2) if 0 < trig <= started else 0.0,
            # 用户 m05453：抖动 / 决策 / 回复分三段。抖动与决策由引擎量好传进来，
            # 这一份 rec 里只留「请求本身」的耗时；老记录没有这两个值就退回老算法。
            "debounce_fixed": float(debounce or 0.0),
            "decision_elapsed": float(decision or 0.0),
            # v3.63（用户 m18492 第 9 条）：轨迹里不再抄任何请求内容。
            # 请求快照（requests/<sid>/<ts>.json）已经有 messages/sections/原文，
            # 页面 ④ 轨迹只读 steps/tools/totals，这一份是纯重复写入。
            "steps": [],
            "tools": [],
            "sent": [],
        }
        self._open[key] = rec
        self._refs[key] = []
        return rec

    def step(self, event: Any, llm_resp: Any) -> None:
        if not self.enabled:
            return
        key = self._key(event)
        rec = self._open.get(key)
        if rec is None:
            return
        text = _attr(llm_resp, "text_response", "") or ""
        reasoning = _attr(llm_resp, "reasoning_content", "") or ""
        calls = list(_attr(llm_resp, "tool_calls", None) or [])
        inp = _attr(llm_resp, "input_tokens", None)
        cached = _attr(llm_resp, "cached_tokens", None)
        # 框架 LLMResponse 没有 model 字段，_model_of 读不到就用框架配置里的默认模型
        # 兜底，否则轨迹 model 一直是空串、花费卡片只能显示「未记录模型」
        # （用户 m03616 第 1 条）
        model = _model_of(llm_resp) or self.default_model_name()
        rec["steps"].append(
            {
                "index": _attr(llm_resp, "agent_step_index", len(rec["steps"])),
                "model": model,
                "ts": time.time(),
                "text": clip(text),
                "reasoning": clip(reasoning),
                "tool_calls": [{"name": _tool_name_of(c), "args": _tool_args_of(c)} for c in calls],
                "input_tokens": inp,
                "output_tokens": _attr(llm_resp, "output_tokens", None),
                "cached_tokens": cached,
                "cache_hit": round(cached / inp * 100, 1) if (cached and inp) else None,
                "time_consumed": _attr(llm_resp, "time_consumed", None),
                "llm_elapsed_seconds": _attr(llm_resp, "time_consumed", None),
            }
        )
        # 工具结果在这一步之后才被填进同一个对象，先留引用，收尾时再读
        self._refs[key].append(llm_resp)

    def tool(self, event: Any, tool_result: Any) -> None:
        if not self.enabled:
            return
        rec = self._open.get(self._key(event))
        if rec is None:
            return
        text = _attr(tool_result, "text", "") or ""
        if not text:
            try:
                text = tool_result.assemble_result() if hasattr(tool_result, "assemble_result") else ""
            except Exception:
                text = ""
        atts = _attr(tool_result, "attachments", None) or []
        rec["tools"].append(
            {
                "ts": time.time(),
                "text": clip(str(text or ""), MAX_TOOL),
                "attachments": len(atts),
            }
        )

    def sent(self, event: Any, action: Any, result: Any) -> None:
        if not self.enabled:
            return
        rec = self._open.get(self._key(event))
        if rec is None:
            return
        text = ""
        try:
            text = chain_text(action)
        except Exception:
            text = ""
        rec["sent"].append(
            {
                "ts": time.time(),
                "ok": bool(_attr(result, "ok", False)),
                "err": str(_attr(result, "err", "") or ""),
                "text": clip(text, 2000),
                "full_text": text,
                "raw": text,
                "chars": len(text),
            }
        )

    def finish(self, event: Any, final_result: Any = None) -> Optional[dict]:
        if not self.enabled:
            return None
        key = self._key(event)
        rec = self._open.pop(key, None)
        refs = self._refs.pop(key, [])
        if rec is None:
            return None
        # 把每步的 tool_results 补进来（引用在这一步之后被填好）
        by_index = {}
        for idx, resp in enumerate(refs):
            step_index = _attr(resp, "agent_step_index", idx)
            results = []
            for r in (_attr(resp, "tool_results", None) or []):
                results.append(
                    {
                        "name": _attr(r, "name", "") or "",
                        "tool_call_id": _attr(r, "tool_call_id", "") or "",
                        "chars": len(content_text(_attr(r, "content", ""))),
                        "content": clip(content_text(_attr(r, "content", "")), MAX_TOOL),
                    }
                )
            by_index[step_index] = results
        for step in rec["steps"]:
            step["tool_results"] = by_index.get(step.get("index"), [])
        rec["ended"] = time.time()
        rec["elapsed"] = round(rec["ended"] - rec["started"], 2)
        # 用户 m05691 第 4 条：「流水线总耗时十几秒」里有一大块不是模型也不是插件慢，
        # 而是几条气泡被逐条发出去的间隔（每条之间隔四秒多）。单独量这一段，页面才解释得清。
        _stamps = []
        for _item in (rec.get("sent") or []):
            if isinstance(_item, dict):
                try:
                    _t = float(_item.get("ts") or 0.0)
                except (TypeError, ValueError):
                    _t = 0.0
                if _t > 0:
                    _stamps.append(_t)
        rec["send_elapsed"] = round(max(_stamps) - min(_stamps), 2) if len(_stamps) > 1 else 0.0
        rec["send_count"] = len(_stamps)
        # v3.51（用户 m07385 第 7 条）：页面上的「请求用时」按触发算 ——
        # 触发 → 防抖等待 → 请求结束。吐字发生在 final 之后，本来就不在里面。
        trig = float(rec.get("triggered_at") or 0.0)
        if trig and 0 < trig <= rec["started"]:
            rec["elapsed_trigger"] = round(rec["ended"] - trig, 2)
        else:
            rec["elapsed_trigger"] = rec["elapsed"]
        # 用户 m05453：防抖这一格只放「抖动等了多久」。引擎量好的值优先，
        # 取不到（重试轮 / 老记录）才退回「请求开始 - 触发」的老算法。
        fix = float(rec.get("debounce_fixed") or 0.0)
        if fix > 0:
            rec["debounce"] = round(fix, 2)
        elif trig and 0 < trig <= rec["started"]:
            rec["debounce"] = round(rec["started"] - trig, 2)
        else:
            rec["debounce"] = 0.0
        rec.update(timing_fields(rec))
        totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        for step in rec["steps"]:
            for k in totals:
                v = step.get(k)
                if isinstance(v, int):
                    totals[k] += v
        totals["cache_hit"] = (
            round(totals["cached_tokens"] / totals["input_tokens"] * 100, 1)
            if totals["input_tokens"]
            else None
        )
        rec["totals"] = totals
        # 本轮模型：取步数最多的那个（正常一轮只会出现一个，重试换模型才会多）
        model_hits: Dict[str, int] = {}
        for step in rec["steps"]:
            name = str(step.get("model") or "")
            if name:
                model_hits[name] = model_hits.get(name, 0) + 1
        rec["model"] = max(model_hits, key=model_hits.get) if model_hits else ""
        final_steps = list(getattr(final_result, "step_results", None) or [])
        if final_result is not None:
            rec["step_results"] = len(final_steps)
        errors = [str(_attr(step, "raw_output", "") or "") for step in final_steps]
        errors.extend(str(step.get("text") or "") for step in rec["steps"])
        error = next((text for text in errors if text.lstrip().startswith("[ProviderError]")), "")
        rec["failed"] = bool(error)
        rec["error"] = clip(error, 500)
        rec["replied"] = any(item.get("ok") for item in rec["sent"])
        rec["outcome"] = "failed" if error else "replied" if rec["replied"] else "no_reply"
        self._merge(rec)
        # v3.47：落盘后顺手往 24 小时账本追加一行。统计口径以账本为准——
        # traces 会被 _prune 按 keep 裁掉，数字不能跟着丢。
        self._append_ledger(rec)
        return rec

    # ------------------------------------------------------------ 落盘

    def _merge(self, rec: dict) -> Optional[str]:
        """把这一轮的轨迹挂到请求快照上（v3.64，用户 m18870：两份合成一份）。

        请求快照先落盘（engine 结算时 flush），轨迹随 finish 后到，所以交给
        RequestLog.attach_trace：还没 flush 的直接挂进 pending、一次写完；
        已经 flush 的读回文件补一个 "trace" 键再原子替换。
        """
        if self.store is None:
            self.dropped += 1
            return None
        try:
            state = self.store.attach_trace(
                str(rec.get("sid") or ""), str(rec.get("event_id") or ""), rec)
        except Exception:
            logger.debug("[multi-decision] 合并调用轨迹失败", exc_info=True)
            state = ""
        if not state:
            # 极少数异常轮没有请求快照（没 stage 或被 discard），轨迹只能丢
            self.dropped += 1
            return None
        return state

    def _rounds_root(self) -> Path:
        """请求快照根目录（轨迹就存在这里）。"""
        return self.requests_dir

    def _rounds_folder(self, sid: str) -> Path:
        """某个会话的请求快照目录；目录名转义规则必须和 RequestLog 一致。"""
        from .request_log import _safe_sid
        return self.requests_dir / _safe_sid(sid)

    def keep_for(self, sid: str = "") -> int:
        """这个会话该留几份轨迹（用户 m18059 第 4 条：缓存设置按会话独立）。

        优先问 main.py 传进来的回调（单群覆盖 > 全局），没有回调就退回实例值。
        """
        if self._keep_of is not None:
            try:
                value = int(self._keep_of(sid) or 0)
                if value > 0:
                    return value
            except Exception:
                logger.debug("[multi-decision] 取会话轨迹保留份数失败", exc_info=True)
        return self.keep

    def days_for(self, sid: str = "") -> int:
        """这个会话该留多少天的轨迹；0 = 不限。口径同 keep_for。"""
        if self._days_of is not None:
            try:
                return max(0, int(self._days_of(sid) or 0))
            except Exception:
                logger.debug("[multi-decision] 取会话轨迹保留天数失败", exc_info=True)
        return self.keep_days

    # ------------------------------------------------------------ 24 小时账本

    def _append_ledger(self, rec: dict) -> None:
        """往 24 小时账本追加一行。写失败只吞异常：统计坏了不能影响业务。"""
        try:
            totals = rec.get("totals") or {}
            row = {
                "ts": round(float(rec.get("ended") or time.time()), 3),
                "sid": str(rec.get("sid") or ""),
                "event_id": str(rec.get("event_id") or ""),
                "key": self._ledger_key(rec),
                "model": str(rec.get("model") or ""),
                "input": _as_int(totals.get("input_tokens")),
                "output": _as_int(totals.get("output_tokens")),
                "cached": _as_int(totals.get("cached_tokens")),
                "elapsed": float(rec.get("elapsed") or 0.0),
                # v3.51（用户 m07385 第 7 条）：含防抖的整段用时与模型自己耗时，
                # 老行没有这两个键，读的时候按 0 处理。
                "elapsed_trigger": float(rec.get("elapsed_trigger") or 0.0),
                "debounce": float(rec.get("debounce") or 0.0),
                "llm": float(rec.get("llm_elapsed_seconds") or 0.0),
            }
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._ledger_cache = None
        except Exception:
            logger.debug("[multi-decision] 写 24 小时 token 账本失败", exc_info=True)
            return
        self._trim_ledger()

    @staticmethod
    def _ledger_key(rec: dict) -> str:
        """账本与轨迹共用的去重键：有 event_id 用它，没有就用落盘文件名那套毫秒戳。"""
        eid = str(rec.get("event_id", "") or "").strip()
        if eid:
            return eid
        try:
            return "ts:%d" % int(float(rec.get("started") or 0) * 1000)
        except (TypeError, ValueError):
            return ""

    def _rewrite_ledger(self, rows: List[str]) -> None:
        """原子替换账本内容（tmp + os.replace）。"""
        tmp = self.ledger.parent / (self.ledger.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write("".join(text + "\n" for text in rows))
            os.replace(tmp, self.ledger)
            self._ledger_cache = None
            # 数字跟着变了：5 秒缓存一起失效，否则清完会话记录卡片还挂着旧数
            self._recent_cache = None
            self._today_cache = None
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _trim_ledger(self) -> None:
        """账本按时间滚动清理：只留最近 LEDGER_KEEP_DAYS 天，别让它无限长。

        清理要把整份文件重写一遍，所以最多每小时一次，不跟着每轮追加都跑。
        """
        now = time.time()
        if now - self._ledger_trim_at < 3600:
            return
        self._ledger_trim_at = now
        try:
            if not self.ledger.exists():
                return
            cutoff = now - LEDGER_KEEP_DAYS * 86400
            keep: List[str] = []
            dropped = 0
            with self.ledger.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        ts = float(json.loads(text).get("ts") or 0)
                    except Exception:
                        dropped += 1  # 坏行顺手扔掉
                        continue
                    if ts >= cutoff:
                        keep.append(text)
                    else:
                        dropped += 1
            if dropped:
                self._rewrite_ledger(keep)
        except Exception:
            logger.debug("[multi-decision] 清理 24 小时 token 账本失败", exc_info=True)

    def _drop_ledger_sid(self, sid: str) -> None:
        """删掉某个会话在账本里的行：清空会话记录后，统计里不该还留着已删的轮。"""
        try:
            if not self.ledger.exists():
                return
            sid = str(sid or "")
            keep: List[str] = []
            dropped = 0
            with self.ledger.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except Exception:
                        dropped += 1
                        continue
                    if str(row.get("sid") or "") == sid:
                        dropped += 1
                    else:
                        keep.append(text)
            if dropped:
                self._rewrite_ledger(keep)
        except Exception:
            logger.debug("[multi-decision] 清理账本里的会话行失败", exc_info=True)

    def _ledger_rows(self) -> List[dict]:
        """读账本里的全部行（按 mtime + 大小缓存，追加/清理后置空）。"""
        try:
            stat = self.ledger.stat()
        except OSError:
            return []
        mark = (stat.st_mtime_ns, stat.st_size)
        cached = self._ledger_cache
        if cached and cached[0] == mark:
            return cached[1]
        rows: List[dict] = []
        try:
            with self.ledger.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except Exception:
            return rows
        self._ledger_cache = (mark, rows)
        return rows

    def _backfill_ledger(self) -> None:
        """把盘上还留着的 traces 回填进账本，只跑一次（升级当天补齐 24 小时窗口）。"""
        try:
            marker = self.ledger.parent / "tokens.backfill"
            if marker.exists():
                return
            known = {str(row.get("key") or "") for row in self._ledger_rows()}
            rows: List[str] = []
            root = self._rounds_root()
            dirs = list(root.iterdir()) if root.exists() else []
            for d in dirs:
                try:
                    if not d.is_dir():
                        continue
                except OSError:
                    continue
                for fp in d.glob("*.json"):
                    try:
                        payload = json.loads(fp.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    # v3.64：轨迹挂在请求快照的 "trace" 键里
                    rec = payload.get("trace") if isinstance(payload, dict) else None
                    if not isinstance(rec, dict):
                        continue
                    key = self._ledger_key(rec)
                    if not key or key in known:
                        continue
                    known.add(key)
                    totals = rec.get("totals") or {}
                    try:
                        ts = round(float(rec.get("ended") or rec.get("started") or 0.0), 3)
                    except (TypeError, ValueError):
                        ts = 0.0
                    if not ts:
                        continue
                    rows.append(json.dumps({
                        "ts": ts,
                        "sid": str(rec.get("sid") or ""),
                        "event_id": str(rec.get("event_id") or ""),
                        "key": key,
                        "model": str(rec.get("model") or ""),
                        "input": _as_int(totals.get("input_tokens")),
                        "output": _as_int(totals.get("output_tokens")),
                        "cached": _as_int(totals.get("cached_tokens")),
                        "elapsed": float(rec.get("elapsed") or 0.0),
                        "elapsed_trigger": float(rec.get("elapsed_trigger") or 0.0),
                        "debounce": float(rec.get("debounce") or 0.0),
                        "llm": float(rec.get("llm_elapsed_seconds") or 0.0),
                    }, ensure_ascii=False))
            if rows:
                self.ledger.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(rows) + "\n")
                self._ledger_cache = None
                self._recent_cache = None
                self._today_cache = None
            marker.write_text(str(len(rows)), encoding="utf-8")
        except Exception:
            logger.debug("[multi-decision] 回填 24 小时账本失败（不影响业务）", exc_info=True)

    # ------------------------------------------------------------ 读

    def _model_name(self, model: Any, memo: dict) -> str:
        """空模型名 → 框架默认模型（懒加载一次）；配置也读不到才退回「未记录模型」。

        v3.43：老轨迹里有一部分 rec["model"] 是空的（那时 step() 还没兜底），
        它们其实就是当时的默认模型，不该和真有名字的同名模型分成两份算钱。
        """
        name = str(model or "").strip()
        if name:
            return name
        if "fallback" not in memo:
            try:
                memo["fallback"] = self.default_model_name() or ""
            except Exception:
                memo["fallback"] = ""
        return memo["fallback"] or MODEL_UNKNOWN

    def _add_round(self, out: dict, row: dict, memo: dict) -> None:
        """把一轮的数字并进汇总：账本行与轨迹记录都走这里，口径只有一份。"""
        inp = _as_int(row.get("input"))
        outp = _as_int(row.get("output"))
        cch = _as_int(row.get("cached"))
        out["input"] += inp
        out["output"] += outp
        out["cached"] += cch
        out["rounds"] += 1
        bucket = out["models"].setdefault(
            self._model_name(row.get("model"), memo),
            {"input": 0, "output": 0, "cached": 0, "rounds": 0},
        )
        bucket["input"] += inp
        bucket["output"] += outp
        bucket["cached"] += cch
        bucket["rounds"] += 1
        # 用户 m11091 第 9 条：平均用时看**流水线总耗时**（elapsed）。v3.51 曾改成
        # elapsed_trigger（触发→请求结束，含防抖等待），但页面同一行里显示的时长
        # 是 elapsed，两个数不一样，看着像同一轮有两个用时。老账本/老轨迹没有
        # elapsed 时才退回 elapsed_trigger，不能因为升级就把它们全丢掉。
        elapsed = row.get("elapsed") or row.get("elapsed_trigger")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed > 0:
            memo["elapsed_sum"] = memo.get("elapsed_sum", 0.0) + float(elapsed)
            out["elapsed_rounds"] += 1

    def _round_rows(self, start: float) -> Iterator[dict]:
        """窗口内的每一轮：先读账本，再用 traces 补（同一个 key 只算一次）。

        v3.47（用户 2026-10-24 第 3/4 条）：traces 每个会话只留 keep 份（默认 60），
        所以 rounds / avg_elapsed 一直卡在 60 —— 被裁掉的轮根本读不到。
        账本是新加的，升级当天窗口里更早的轮只存在于 traces 里，两边都有的轮
        按 key 去重，不能双算；账本上线前的历史因此仍然算得到。
        """
        seen = set()
        for row in self._ledger_rows():
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts < start:
                continue
            key = str(row.get("key") or row.get("event_id") or "")
            if key:
                seen.add(key)
            yield {
                "input": _as_int(row.get("input")),
                "output": _as_int(row.get("output")),
                "cached": _as_int(row.get("cached")),
                "elapsed": row.get("elapsed"),
                "elapsed_trigger": row.get("elapsed_trigger"),
                "debounce": row.get("debounce"),
                "llm": row.get("llm"),
                "model": row.get("model"),
            }
        try:
            root = self._rounds_root()
            dirs = list(root.iterdir()) if root.exists() else []
        except Exception:
            return
        for d in dirs:
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            for f in d.glob("*.json"):
                try:
                    if f.stat().st_mtime < start:
                        continue
                    payload = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                rec = payload.get("trace") if isinstance(payload, dict) else None
                if not isinstance(rec, dict):
                    continue
                t = rec.get("totals") or {}
                if not t:
                    continue
                # 没有 event_id 的轨迹：账本记的是 "ts:<毫秒>"，这里必须用同一口径，
                # 不能拿文件名凑——快照的 ts 是 stage 时刻，跟轨迹的 started 差几十毫秒，
                # 差一点就对不上，同一轮会被算两次（审查发现）。
                key = self._ledger_key(rec) if not str(rec.get("event_id") or "").strip() else str(rec.get("event_id") or "").strip()
                if key and key in seen:
                    continue
                yield {
                    "input": _as_int(t.get("input_tokens")),
                    "output": _as_int(t.get("output_tokens")),
                    "cached": _as_int(t.get("cached_tokens")),
                    "elapsed": rec.get("elapsed"),
                    "elapsed_trigger": rec.get("elapsed_trigger"),
                    "debounce": rec.get("debounce"),
                    "llm": rec.get("llm_elapsed_seconds"),
                    "model": rec.get("model"),
                }

    def today_totals(self) -> dict:
        """今天的 token 汇总（输入 / 输出 / 缓存复用），给概览卡片用。

        v3.47：口径与 recent_totals 一样走 24 小时账本（traces 只补历史）。
        结果缓存 5 秒：日志页 5 秒自动刷一次，不能每次都去读几百个 JSON。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        cache = getattr(self, "_today_cache", None)
        if cache and cache[1] == today and (now - cache[0]) < 5.0:
            return dict(cache[2])
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        out = {"input": 0, "output": 0, "cached": 0, "total": 0, "rounds": 0, "cache_hit": None}
        try:
            # v3.47：口径跟 recent_totals 一样——账本优先、traces 补历史并去重，
            # 不再只扫 traces（轮数一多就被 keep 截断，今日数字也跟着少）。
            for row in self._round_rows(start):
                out["input"] += _as_int(row.get("input"))
                out["output"] += _as_int(row.get("output"))
                out["cached"] += _as_int(row.get("cached"))
                out["rounds"] += 1
        except Exception:
            logger.debug("[multi-decision] 统计今日 token 失败", exc_info=True)
        out["total"] = out["input"] + out["output"]
        if out["input"]:
            out["cache_hit"] = round(out["cached"] * 100.0 / out["input"], 1)
        self._today_cache = (now, today, dict(out))
        return out

    def recent_totals(self, hours: int = 24) -> dict:
        """最近 N 小时的 token 与用时汇总（v3.19）。

        概览卡片从「今天」改成「最近 24 小时」：按自然日切的话，0 点一过
        昨天的量全掉，用户看到的数字会莫名其妙归零。起点换成滚动窗口就没这问题。
        avg_elapsed 是每轮 elapsed 的算术平均（秒），给「请求平均用时」卡片用。

        v3.47（用户 2026-10-24 第 3/4 条）：数字以 24 小时账本为准（不受 trace
        keep=60 影响），traces 只用来补账本上线前的历史，并按 event 去重。
        rounds / models / avg_elapsed / elapsed_rounds 字段名与含义不变。
        """
        now = time.time()
        start = now - max(1, int(hours or 24)) * 3600
        cache = getattr(self, "_recent_cache", None)
        if cache and cache[1] == start and (now - cache[0]) < 5.0:
            return _clone_totals(cache[2])
        # models：按模型分开的同一批数字，给「花费」卡片算钱用（v3.37）
        out = {"input": 0, "output": 0, "cached": 0, "total": 0, "rounds": 0,
               "cache_hit": None, "avg_elapsed": None, "elapsed_rounds": 0, "models": {}}
        # memo 放跨轮的状态：默认模型名（懒加载一次）与 elapsed 总和，
        # 全量放进去会被 _clone_totals 拷给调用方，不该出现在返回结构里。
        memo: dict = {}
        try:
            for row in self._round_rows(start):
                self._add_round(out, row, memo)
        except Exception:
            logger.debug("[multi-decision] 统计最近 token 失败", exc_info=True)
        out["total"] = out["input"] + out["output"]
        if out["input"]:
            out["cache_hit"] = round(out["cached"] * 100.0 / out["input"], 1)
        if out["elapsed_rounds"]:
            out["avg_elapsed"] = round(memo.get("elapsed_sum", 0.0) / out["elapsed_rounds"], 1)
        self._recent_cache = (now, start, _clone_totals(out))
        return _clone_totals(out)

    def list_sessions(self) -> List[dict]:
        """有轨迹的会话（数据就在 requests/<sid>/ 的请求快照里）。"""
        out: List[dict] = []
        root = self._rounds_root()
        if not root.exists():
            return out
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            files = sorted(d.glob("*.json"))
            if not files:
                continue
            latest = files[-1]
            try:
                real_sid = str(json.loads(latest.read_text(encoding="utf-8")).get("sid") or "")
            except Exception:
                real_sid = ""
            try:
                last_ts = latest.stat().st_mtime
            except OSError:
                last_ts = 0
            out.append({"dir": d.name, "sid": real_sid or d.name,
                        "count": len(files), "last_ts": last_ts})
        out.sort(key=lambda x: x["last_ts"], reverse=True)
        return out

    def read_by_event(self, sid: str, event_id: str, window: int = 300) -> Optional[dict]:
        """按 event_id 找这一轮的轨迹：事件号就写在请求快照里，扫最近 window 份。"""
        eid = str(event_id or "").strip()
        if not eid:
            return None
        folder = self._rounds_folder(sid)
        try:
            files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        except Exception:
            return None
        for path in files[: max(1, int(window))]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("event_id") or "") != eid:
                continue
            rec = data.get("trace")
            if isinstance(rec, dict):
                return self._decorate(rec)
        return None

    @staticmethod
    def _decorate(rec: dict) -> dict:
        rec.update(timing_fields(rec))
        for step in rec.get("steps") or []:
            step["llm_elapsed_seconds"] = step.get("time_consumed")
        return rec

    def read(self, sid: str, name: str) -> Optional[dict]:
        """读一轮的轨迹。name 是请求快照的文件名（轨迹就存它里面）。"""
        safe_name = Path(str(name)).name
        if not safe_name.endswith(".json"):
            return None
        path = self._rounds_folder(sid) / safe_name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        rec = data.get("trace")
        return self._decorate(rec) if isinstance(rec, dict) else None

    def purge(self, sid: str) -> int:
        """轨迹并进请求快照后，删盘只有一处（RequestLog.purge），这里只清内存。"""
        self._drop_ledger_sid(sid)
        want = normalize_sid(str(sid or ""))
        for key, rec in list(self._open.items()):
            try:
                if normalize_sid(str(rec.get("sid") or "")) == want:
                    self._open.pop(key, None)
                    self._refs.pop(key, None)
            except Exception:
                continue
        return 0

    def clear_all(self) -> int:
        """清请求快照时轨迹跟着一起没，这里只清内存里的引用。"""
        self._refs.clear()
        return 0

    def recent(self, sid: str, limit: int = 20) -> List[dict]:
        folder = self._rounds_folder(sid)
        try:
            files = sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        except Exception:
            return []
        want = max(1, int(limit))
        items: List[dict] = []
        # 多扫几份：早先那些没挂上轨迹的轮（异常路径）要跳过
        for path in files[: want * 3]:
            if len(items) >= want:
                break
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            rec = data.get("trace")
            if not isinstance(rec, dict):
                continue
            items.append(
                {
                    "name": path.name,
                    "started": rec.get("started", 0),
                    "elapsed": rec.get("elapsed", 0),
                    **timing_fields(rec),
                    "steps": len(rec.get("steps") or []),
                    "tools": len(rec.get("tools") or []),
                    "sent": len(rec.get("sent") or []),
                    "totals": rec.get("totals") or {},
                    "event_id": rec.get("event_id", ""),
                    "sid": rec.get("sid", ""),
                    "failed": bool(rec.get("failed")),
                    "outcome": rec.get("outcome", ""),
                    "error": rec.get("error", ""),
                }
            )
        return items

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _key(event: Any) -> tuple[str, str]:
        sid = normalize_sid(Tracer._sid(event))
        eid = str(_attr(event, "event_id", "") or id(event))
        return (sid, eid)

    @staticmethod
    def _sid(event: Any) -> str:
        sid = _attr(event, "sid", "") or ""
        if sid:
            return sid
        try:
            return event.session.sid
        except Exception:
            return ""

    # ------------------------------------------------------------ 模型名兜底

    def _config_path(self) -> Optional[Path]:
        """框架配置文件位置：<data>/config/system_config.json。

        插件数据目录形如 <data>/plugin_data/<plugin_id>，所以正常情况是
        data_dir.parent.parent；为稳妥（换个布局也能用）从自身向上最多 3 层找。
        找到的路径记住，文件还在就不用每次重新找。
        """
        cached = self._cfg_path
        if cached is not None and cached.is_file():
            return cached
        self._cfg_path = None
        for base in [self._data_dir] + list(self._data_dir.parents)[:3]:
            candidate = base / "config" / "system_config.json"
            if candidate.is_file():
                self._cfg_path = candidate
                return candidate
        return None

    def default_model_name(self) -> str:
        """框架配置里写的默认模型名，读不到就是空串（绝不抛异常影响业务）。

        按文件 mtime 缓存：同一个文件没改过就不重复解析 json，step() 每步调用也
        不会一直读盘。任何异常（文件没了、json 坏了、结构对不上）都返回空串，
        让轨迹继续记成「未记录模型」。
        """
        try:
            path = self._config_path()
            if path is None:
                self._cfg_mtime = None
                self._cfg_model = ""
                return ""
            mtime = path.stat().st_mtime
            if self._cfg_mtime == mtime:
                return self._cfg_model
            name = _default_model_of(path)
            self._cfg_mtime = mtime
            self._cfg_model = name
            return name
        except Exception:
            return ""
