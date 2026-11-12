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

import copy
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from core.logging_manager import get_logger

from . import token_calib
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
    except (TypeError, ValueError, OverflowError):
        return 0


def _clone_totals(out: dict) -> dict:
    """Usage lists are nested; callers must not mutate cached evidence."""
    return copy.deepcopy(out)


def _blank_kind() -> dict:
    """单个 kind（回复 / 决策）的同一批数字（v4.68，用户 m02472）。"""
    return {"rounds": 0, "calls": 0, "input": 0, "output": 0, "cached": 0,
            "cache_hit": None, "elapsed_sum": 0.0, "elapsed_rounds": 0, "avg_elapsed": None}


# ---------------------------------------------------------------- prompt 估算

#: 中日韩字符与全角标点：一个字符约等于一个 token（英文按 4 字符 1 token 折）。
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")

#: 单张图片在 provider 侧折算的 token 数（v4.87 用户 m13442 第 2 条）。
#: Gemini 按 768×768 切 tile、每 tile 258 token，聊天里这些图都够 4 tile，
#: 45 轮真实记录反推出来稳定在 1,025～1,033，取 1,032。跟图片字节数无关
#: （381KB 和 232KB 的图都是这个数），所以不用按体积估。
IMAGE_TOKENS = int(token_calib.DEFAULT_IMAGE_TOKENS)   # v4.9x：默认值，实际按模型取

#: 反推命中时，首步上报值要小于完整 prompt 的这个比例才认（否则说明 provider
#: 报的就是完整 input，这一轮真没命中）。
INFER_MAX_RATIO = 0.75

#: 多步轮里，第 2 步的上报要小于第 1 步的这个比例，才认定 provider 报的是「增量」
#: （用户 m02038 第 2 条）。第 1 步的 prompt 是全新的，本来就不可能命中，中转站
#: 报的「未命中那截」就等于完整 prompt；只有第 2 步明显变小才说明它报的是增量。
#: 反过来，每步都报完整 input 的 provider（DeepSeek 那种）第 2 步只会更大。
INFER_DELTA_RATIO = 0.9

#: 一个模型至少要有这么多轮「cached 全是 0」的记录，才认定它不回报缓存命中。
INFER_MIN_ROUNDS = 3

#: 文本 token 的实测折算系数（v4.9x 用户 m10585 第 1 条）：_approx_tokens 按
#: 「CJK 1 字 1 token、其余 4 字 1 token」估，但 Gemini 侧实测只有它的 1/1.2。
#: 6 条有真值的回复轮（11:51 / 12:46 / 17:43 / 17:44 / 17:51 / 19:01）按此折算后，
#: 完整 prompt 与「上报 + 缓存读取」的误差 ≤1.1%，反推命中误差 ≤0.2pp。
TEXT_TOKEN_SCALE = token_calib.DEFAULT_TEXT_SCALE   # v4.9x：默认值，实际按模型取


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def _flat_of(record: dict) -> str:
    """序列化记录里的消息投影（命中预估与 prompt 估算共用同一份文本）。"""
    try:
        return json.dumps(
            record.get("assembled") or record.get("messages") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        return ""


def _record_model(record: dict) -> str:
    """取这条记录用的模型名：决策轮是 decision_model，回复轮是 reply_model，轨迹行是 model。"""
    if not isinstance(record, dict):
        return ""
    for key in ("decision_model", "reply_model", "model"):
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    trace = record.get("trace")
    if isinstance(trace, dict):
        val = trace.get("model")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def infer_gate(record: dict) -> float:
    """这条记录的反推门槛：按这个模型样本的噪声定（见 token_calib.gate）。"""
    return token_calib.gate(_record_model(record))


def _prompt_parts(record: dict, flat: str = "") -> tuple:
    """(文本 token 原始估算, 图片张数)。文本按「CJK 1 字 1 token」算，还没乘系数。"""
    text = _approx_tokens(flat or _flat_of(record))
    text += _approx_tokens(str(record.get("system_prompt") or ""))
    try:
        imgs = int(record.get("image_count") or 0)
    except (TypeError, ValueError):
        imgs = 0
    return text, max(0, imgs)


def _est_from_parts(text: int, imgs: int, model: str) -> int:
    """按这个模型学到的系数折算：文本 × 系数 + 图片张数 × 单图 token。"""
    scale, per_img = token_calib.factors(model)
    return int(round(text * scale)) + int(round(imgs * per_img))


def _prompt_tokens_est(record: dict, flat: str = "") -> int:
    """整轮 prompt 的 token 估计：文本 × 本模型系数 + 图片张数 × 本模型单图 token。

    v4.9x（用户 m10781）：系数不再写死。Gemini 实测文本是 1/1.2、单图 1032，
    但换一家模型（GPT-4o 中文约 0.6~0.7 token/字、DeepSeek 约 0.6）就不一样，
    所以按模型名从 token_calib 取，没样本的模型退回默认值。
    """
    text, imgs = _prompt_parts(record, flat)
    return _est_from_parts(text, imgs, _record_model(record))


def infer_cached_steps(full_est: int, steps: list,
                       gate: Optional[float] = None) -> Optional[List[dict]]:
    """中转站不回报 cached_tokens 时，按完整 prompt 估计反推每一步的「完整输入 / 命中」。

    v4.87（用户 m13442 第 2 条）：反重力 Gemini 这类中转站上报的 input 只是
    「没命中的那一截」，cached_tokens 恒为 0，于是记录页的实测命中常年 0%、
    价目页也按全价算钱。这里按「完整 prompt ≈ 文本 token + 图片张数 × 1032」反推：

      第 1 步完整 = full_est，命中 = 完整 − 上报（上报 ≥ 完整 75% 时判为真未命中）；
      第 k 步完整 = 上一步完整 + 本步上报（本步上报就是新增的那一截），命中 = 上一步完整。

    v4.9x（用户 m02038 第 2 条）：多步轮的首步上报本来就等于完整 prompt——新 prompt
    第一次发出去不可能命中，中转站报的「未命中那截」就是全部。以前这里一律判成
    「provider 报的是完整 input，真没命中」直接放弃，于是 2 步以上的轮次从第 2 步起
    命中全丢（3 步轮实测 63.8%，页面只写 43.6%）。现在多步轮改看第 2 步：它明显
    小于第 1 步就说明报的是增量，按「第 1 步完整 = 首步上报、命中 0」往下推。

    steps 是 [{"in": int, "cached": int}, ...]。返回与 steps 等长的列表，
    每项是 {"full": int, "cached": int}，该步拿不到分母时为 None；整轮不适用
    （没有完整估计 / 有任一步报了 cached / 首步上报 ≥ 完整 × 门槛且第 2 步没变小）时返回 None。

    v4.9x（用户 m11048）：门槛不再写死 0.75，改由 token_calib 按这个模型的样本噪声
    给（估得准就敢认 10% 这种小命中），没样本的模型仍用 0.75。
    """
    limit = INFER_MAX_RATIO if gate is None else float(gate)
    if full_est <= 0 or not steps:
        return None
    for step in steps:
        if not isinstance(step, dict):
            return None
        if _as_int(step.get("cached")) > 0:
            return None
    first_in = _as_int(steps[0].get("in"))
    if first_in <= 0:
        return None
    if first_in >= full_est * limit:
        # 多步轮：第 2 步明显小于第 1 步 → 报的是增量，可以反推（见 docstring）。
        second_in = _as_int(steps[1].get("in")) if len(steps) > 1 else 0
        if second_in <= 0 or second_in >= first_in * INFER_DELTA_RATIO:
            return None
        out_delta: List[Optional[dict]] = [{"full": first_in, "cached": 0}]
        cur_delta = first_in
        for step in steps[1:]:
            in_tokens = _as_int(step.get("in"))
            if in_tokens <= 0:
                out_delta.append(None)
                continue
            if in_tokens >= cur_delta:
                out_delta.append({"full": in_tokens, "cached": 0})
                cur_delta = in_tokens
            else:
                out_delta.append({"full": cur_delta + in_tokens, "cached": cur_delta})
                cur_delta += in_tokens
        return out_delta
    out: List[Optional[dict]] = []
    cur = int(full_est)
    for index, step in enumerate(steps):
        in_tokens = _as_int(step.get("in"))
        if in_tokens <= 0:
            out.append(None)
            continue
        if index == 0:
            out.append({"full": cur, "cached": max(0, cur - in_tokens)})
        elif in_tokens >= cur:
            out.append({"full": in_tokens, "cached": 0})
            cur = in_tokens
        else:
            out.append({"full": cur + in_tokens, "cached": cur})
            cur += in_tokens
    return out


def _finish_kinds(out: dict) -> None:
    """算分 kind 的命中率与平均用时；elapsed_sum 是中间量，不往外抛。"""
    kinds = out.get("kinds")
    if not isinstance(kinds, dict):
        return
    for bucket in kinds.values():
        if not isinstance(bucket, dict):
            continue
        # cached 全是 0 时不要写成「命中 0%」：回复这一路框架回的 LLMResponse 不带
        # cached_tokens（决策是我们自己发的请求，所以有数），0 表示「没记录」不是「没命中」。
        if bucket.get("input") and bucket.get("cached"):
            bucket["cache_hit"] = round(bucket["cached"] * 100.0 / bucket["input"], 1)
        if bucket.get("elapsed_rounds"):
            bucket["avg_elapsed"] = round(bucket["elapsed_sum"] / bucket["elapsed_rounds"], 1)
        bucket.pop("elapsed_sum", None)

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


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
        return result if math.isfinite(result) and result > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def usage_identity(obj: Any) -> dict:
    """Keep recorded identity; a display name is never provider evidence."""
    pid = str(_attr(obj, "provider_id", "") or "").strip()
    mid = str(_attr(obj, "model_id", "") or "").strip()
    ref = str(_attr(obj, "model_ref", "") or "").strip()
    if ref and ":" in ref and not (pid and mid):
        pid, mid = ref.split(":", 1)
    raw = _attr(obj, "model", "")
    raw = raw.strip() if isinstance(raw, str) else ""
    name = str(_attr(obj, "model_name", "") or "").strip()
    known = bool(pid and mid)
    # A bare internal ID can be resolved by Tracer only with a unique exact key.
    model = (pid + ":" + mid) if known else raw if raw else name if not mid else ""
    return {"model": model, "model_name": name or raw or mid,
            "provider_id": pid if known else "", "provider_name": str(_attr(obj, "provider_name", "") or "") if known else "",
            "model_id": mid, "identity_source": str(_attr(obj, "identity_source", "") or "recorded") if known else "unknown"}


def _cached_reported(obj: Any) -> bool:
    """这个响应到底有没有回报 cached_tokens（v5.x 用户 m12891）。

    「没回报」和「回报 0」是两件事：框架的 LLMResponse 在没有缓存字段时把
    cached_tokens 留成 None（core/provider/openai_compatible.py 里就是
    getattr(prompt_details, "cached_tokens", None)），落盘时被 _as_int 抹成 0，
    记录页只能把它显示成「实测 0%」。这里在归一化的同时把这条信号留下来：
    以后判断「这一轮该显示实测还是推算」只看记录自己，不再看当前用哪个模型、
    也不看窗口里还有谁。
    """
    flag = _attr(obj, "cached_reported", None)
    if isinstance(flag, bool):
        return flag
    probe = _attr(obj, "cached", None)
    if probe is None:
        probe = _attr(obj, "cached_tokens", None)
    return probe is not None


def usage_event(obj: Any, ts: float = 0.0, timestamp_source: str = "legacy_round") -> dict:
    """Normalize evidence without inventing tokens or a request timestamp."""
    inp = max(0, _as_int(_attr(obj, "input", _attr(obj, "input_tokens", 0))))
    outp = max(0, _as_int(_attr(obj, "output", _attr(obj, "output_tokens", 0))))
    cached = min(inp, max(0, _as_int(_attr(obj, "cached", _attr(obj, "cached_tokens", 0)))))
    return {**usage_identity(obj), "ts": _number(_attr(obj, "ts", ts)),
            "timestamp_source": str(_attr(obj, "timestamp_source", "") or timestamp_source),
            "input": inp, "output": outp, "cached": cached,
            "cached_reported": _cached_reported(obj)}


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


def _provider_label_of(path: Any, provider_id: str = "", model_name: str = "") -> str:
    """模型属于哪个「API 渠道」（用户 m00375 第 6 条）。

    价目表与决策记录里的「渠道」以前只认 LLMResponse 上的 provider_name，而框架
    的响应对象根本没有这个字段，第三方接口（custom:xxx）更是压根不在框架配置里，
    于是整列都是「来源未记录」。这里按两种情况补：
      1. provider_id 在配置里 —— 用那个供应商的显示名（用户自己起的实例名）；
      2. 只有模型名 —— 在配置里按 model_name 精确找，唯一命中才认，同名不猜。
    都找不到就返回空串，页面照旧写「来源未记录」，绝不编一个。
    只读 providers 的 name 与 model_config.*.model_name，绝不碰密钥等其他字段。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    provs = raw.get("providers") if isinstance(raw, dict) else None
    if not isinstance(provs, dict):
        return ""
    pid = str(provider_id or "").strip()
    if pid:
        prov = provs.get(pid)
        if isinstance(prov, dict):
            return str(prov.get("name") or prov.get("provider_name") or pid).strip()[:60]
        return ""
    want = str(model_name or "").strip()
    if not want:
        return ""
    hits = []
    for _pid, prov in provs.items():
        if not isinstance(prov, dict):
            continue
        mcfg = prov.get("model_config")
        if not isinstance(mcfg, dict):
            continue
        found = False
        for group in mcfg.values():
            if found or not isinstance(group, dict):
                continue
            for item in group.values():
                if isinstance(item, dict) and str(item.get("model_name") or "").strip() == want:
                    found = True
                    break
        if found:
            hits.append(str(prov.get("name") or _pid).strip())
    hits = [h for h in hits if h]
    return hits[0][:60] if len(set(hits)) == 1 else ""


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
        # v4.90（用户 m02939 第 2 条）：这一轮真正拿去发请求的模型名，由 main.py 在
        # ON_LLM_REQUEST 里问 provider_mgr 要到后传进来（见 begin 的 model_hint）。
        self._hints: Dict[tuple[str, str], str] = {}
        # 模型名兜底的缓存（见 default_model_name）：数据目录 + 解析到的配置路径 + mtime
        self._data_dir = Path(data_dir)
        # v4.9x（用户 m10781）：按模型自学习的 token 折算系数，存插件数据目录。
        token_calib.configure(self._data_dir / "token_calib.json")
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
        # 用户 m00375 第 8 条：模型名 → API 渠道显示名，按配置文件 mtime 缓存。
        self._cfg_label_mtime: Optional[float] = None
        self._cfg_label_map: Optional[Dict[str, str]] = None
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
                self._hints.pop(key, None)

    # ------------------------------------------------------------ 生命周期

    def begin(self, event: Any, request: Any, trigger_at: float = 0.0,
              debounce: float = 0.0, decision: float = 0.0,
              model_hint: str = "") -> Optional[dict]:
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
        self._hints[key] = str(model_hint or "").strip()[:80]
        return rec

    def _identity(self, obj: Any) -> dict:
        """Resolve internal IDs only through an exact, unique config key."""
        identity = usage_identity(obj)
        mid = identity["model_id"]
        if not mid or identity["provider_id"]:
            return identity
        try:
            path = self._config_path()
            raw = json.loads(path.read_text(encoding="utf-8")) if path else {}
            hits = []
            for pid, provider in (raw.get("providers") or {}).items():
                models = (provider.get("model_config") or {}).get("llm") or {}
                if mid in models and isinstance(models[mid], dict):
                    hits.append((str(pid), provider, models[mid]))
            if len(hits) == 1:
                pid, provider, model = hits[0]
                identity.update(model=pid + ":" + mid, provider_id=pid,
                                provider_name=str(provider.get("name") or provider.get("provider_name") or ""),
                                model_name=identity["model_name"] or str(model.get("model_name") or mid),
                                identity_source="config_unique_id")
        except Exception:
            pass
        return identity

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
        identity = self._identity(llm_resp)
        # v4.90（用户 m02939 第 2 条）：框架的 LLMResponse 只有 token，没有模型身份，
        # 回复轮的轨迹一直是空身份，价目表把回复用量全记成「未记录模型」——用户把那一行
        # 删掉之后，回复花费就彻底看不见了。这里补的是「这一轮真正拿去发请求的模型」：
        # main.py 在 ON_LLM_REQUEST 里问 provider_mgr 要到的那个客户端（跟框架
        # BuildRequestStage 的兜底是同一个），不是拿配置文件里的默认值猜历史归属。
        if not str(identity.get("model") or "").strip():
            hint = str(self._hints.get(key) or "").strip()
            if hint:
                identity = {**identity, "model": hint,
                            "model_name": identity.get("model_name") or hint,
                            "identity_source": "request_model"}
        observed = time.time()
        first = not rec["steps"]
        token = usage_event(llm_resp)
        rec["steps"].append(
            {
                "index": _attr(llm_resp, "agent_step_index", len(rec["steps"])),
                **identity,
                "ts": observed,
                "usage_ts": rec["started"] if first else observed,
                "timestamp_source": "request_hook" if first else "response_observed",
                "text": clip(text),
                "reasoning": clip(reasoning),
                "tool_calls": [{"name": _tool_name_of(c), "args": _tool_args_of(c)} for c in calls],
                "input_tokens": token["input"],
                "output_tokens": token["output"],
                "cached_tokens": token["cached"],
                # v5.x（用户 m12891）：上游这一响应有没有回报缓存命中。落盘存下来，
                # 老记录没有这个键，读的时候按模型全历史判（见 main.py）。
                "cached_reported": token["cached_reported"],
                "cache_hit": round(token["cached"] / token["input"] * 100, 1) if token["input"] else None,
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
        self._hints.pop(key, None)
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
        final_steps = list(_attr(final_result, "step_results", None) or [])
        final_by_index: dict = {}
        for item in final_steps:
            index = _attr(item, "step_index", None)
            if index is not None:
                final_by_index.setdefault(index, []).append(item)
        for step in rec["steps"]:
            matches = final_by_index.get(step.get("index"), [])
            if len(matches) == 1:
                identity = self._identity(matches[0])
                # Never replace actual response identity with weaker final metadata.
                if identity["model"] and not step.get("provider_id"):
                    step.update(identity)
        rec["kind"] = "reply"
        rec["usage"] = self._step_usage(rec)
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
        # v5.x（用户 m12891）：整轮「上游有没有回报缓存」一起落盘——只要有一个付过费
        # 的步骤没回报，这一轮的 cached 就不能当实测用。
        _paid = [step for step in rec["steps"] if _as_int(step.get("input_tokens")) > 0]
        totals["cache_reported"] = bool(_paid) and all(
            bool(step.get("cached_reported")) for step in _paid)
        rec["totals"] = totals
        # 本轮模型：取步数最多的那个（正常一轮只会出现一个，重试换模型才会多）
        model_hits: Dict[str, int] = {}
        for step in rec["steps"]:
            name = str(step.get("model") or "")
            if name:
                model_hits[name] = model_hits.get(name, 0) + 1
        rec["model"] = max(model_hits, key=model_hits.get) if model_hits else ""
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

    @staticmethod
    def _kind(row: dict) -> str:
        if row.get("kind") in ("decision", "reply"):
            return row["kind"]
        return "decision" if row.get("decision_only") or str(row.get("event_id") or row.get("key") or "").startswith("dec-") else "reply"

    def _row_key(self, row: dict) -> tuple:
        event = str(row.get("event_id") or row.get("key") or "").strip()
        if not event:
            event = self._ledger_key(row)
        return (normalize_sid(str(row.get("sid") or "")), self._kind(row), event)

    @staticmethod
    def _usage_totals(usage: list) -> tuple:
        return tuple(sum(item.get(key, 0) for item in usage) for key in ("input", "output", "cached"))

    @staticmethod
    def _counts(row: dict) -> tuple:
        return (max(0, _as_int(row.get("input"))), max(0, _as_int(row.get("output"))),
                min(max(0, _as_int(row.get("input"))), max(0, _as_int(row.get("cached")))))

    def _step_usage(self, rec: dict) -> list:
        result = []
        for step in rec.get("steps") or []:
            if not isinstance(step, dict):
                continue
            event = {**step, **self._identity(step)}
            event["ts"] = step.get("usage_ts", step.get("ts", 0))
            source = "response_observed" if _number(event["ts"]) else "legacy_round"
            if not _number(event["ts"]):
                event["ts"] = rec.get("ended") or rec.get("started") or 0
            result.append(usage_event(event, timestamp_source=source))
        return result

    def _trace_row(self, rec: dict) -> dict:
        totals = rec.get("totals") or {}
        row = {**usage_identity(rec), "ts": _number(rec.get("ended") or rec.get("started")),
               "sid": normalize_sid(str(rec.get("sid") or "")), "kind": self._kind(rec),
               "event_id": str(rec.get("event_id") or ""), "key": self._ledger_key(rec),
               "input": totals.get("input_tokens"), "output": totals.get("output_tokens"),
               "cached": totals.get("cached_tokens"), "elapsed": rec.get("elapsed"),
               "elapsed_trigger": rec.get("elapsed_trigger"), "debounce": rec.get("debounce"),
               "llm": rec.get("llm_elapsed_seconds")}
        row["input"], row["output"], row["cached"] = self._counts(row)
        explicit = isinstance(rec.get("usage"), list)
        usage = [usage_event(item) for item in rec["usage"] if isinstance(item, dict)] if explicit else self._step_usage(rec)
        if self._usage_totals(usage) == self._counts(row) and (usage or explicit):
            row["usage"] = usage
        else:
            row["usage"] = [usage_event(row)]
        return row

    def _snapshot_trace_row(self, rec: dict, payload: dict) -> dict:
        """快照行 = 轨迹行 + 完整 prompt 估计（反推缓存命中要用）。

        v4.87（用户 m13442 第 2 条）：中转站不回报 cached_tokens 时，只能拿
        「完整 prompt 有多大」去反推命中。估算要 json.dumps 整段消息，几十万字符，
        所以只在 cached 为 0（有可能要反推）时才算，其余行不付这个成本。

        v4.9x（用户 m10781）：cached 为 0 且首步上报就是完整 prompt 的轮次，顺手
        当标定点喂给 token_calib，按模型学「文本系数 / 单图 token」。
        """
        row = self._trace_row(rec)
        if _as_int(row.get("cached")) <= 0:
            text, imgs = _prompt_parts(payload)
            est = _est_from_parts(text, imgs, _record_model(payload))
            row["prompt_est"] = est
            self._observe_calib(row, payload, text, imgs, est)
        return row

    def _observe_calib(self, row: dict, payload: dict, text: int, imgs: int, est: int) -> None:
        """首步上报 ≥ 完整估算 × INFER_MAX_RATIO 时，上报值就是完整 prompt，可当标定点。

        低于这个比例说明 provider 报的是「没命中那截」，拿它当完整 prompt 会把系数
        学小，所以只收冷轮。
        """
        if est <= 0 or text <= 0:
            return
        usage = row.get("usage") or []
        first = _as_int(usage[0].get("input")) if usage and isinstance(usage[0], dict) else 0
        if first <= 0:
            first = _as_int(row.get("input"))
        if first <= 0 or first < est * INFER_MAX_RATIO:
            return
        sid = str(row.get("sid") or "")
        eid = str(row.get("event_id") or row.get("started") or "")
        token_calib.observe(_record_model(payload), text, imgs, first,
                            f"{sid}:{eid}" if eid else "")

    def _snapshot_row(self, payload: dict) -> Optional[dict]:
        if not isinstance(payload, dict):
            return None
        original = payload.get("trace")
        if payload.get("decision_only"):
            rec = copy.deepcopy(original) if isinstance(original, dict) else {}
            totals = rec.get("totals") or {}
            rec.update(sid=payload.get("sid", rec.get("sid", "")),
                       event_id=payload.get("event_id", rec.get("event_id", "")),
                       kind="decision", started=payload.get("ts", 0), ended=payload.get("ts", 0),
                       model=payload.get("decision_model") or "")
            rec["totals"] = {key: payload.get(key, totals.get(key, 0)) for key in
                             ("input_tokens", "output_tokens", "cached_tokens")}
            # v5.x（用户 m12891）：决策这一轮上游有没有回报缓存，跟着 totals 一起带下去。
            rec["totals"]["cache_reported"] = payload.get("cache_reported")
            if isinstance(payload.get("usage"), list):
                rec["usage"] = copy.deepcopy(payload["usage"])
            if not isinstance(rec.get("usage"), list):
                # Old decision traces have neither provider evidence nor request times.
                rec["steps"] = []
            return self._snapshot_trace_row(rec, payload)
        if not isinstance(original, dict) or not isinstance(original.get("totals"), dict):
            return None
        rec = dict(original)
        rec.setdefault("sid", payload.get("sid") or "")
        rec.setdefault("event_id", payload.get("event_id") or "")
        return self._snapshot_trace_row(rec, payload)

    def _snapshot_rows(self, since: float = 0.0) -> Iterator[dict]:
        """遍历请求快照里的轨迹行。

        v4.88：两处提速，都是「别重复读同一份文件」——
        ① since>0 时先看文件名（毫秒时间戳），明显早于窗口的直接跳过，不读不解析；
        ② 解析结果按 (mtime_ns, size) 记忆，文件没改过就不再 json.loads。
        概览卡片每 5 秒刷一次，以前每次都把几百份快照（几十 MB）整读一遍。
        """
        try:
            folders = list(self._rounds_root().iterdir())
        except OSError:
            return
        memo = getattr(self, "_snap_rows_cache", None)
        if memo is None:
            memo = self._snap_rows_cache = {}
        for folder in folders:
            if not folder.is_dir():
                continue
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for ent in entries:
                name = ent.name
                if not name.endswith(".json"):
                    continue
                stem = name[:-5]
                if since > 0 and stem.isdigit() and int(stem) / 1000.0 < since - 60:
                    continue
                try:
                    st = ent.stat()
                except OSError:
                    continue
                hit = memo.get(ent.path)
                if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
                    if hit[2] is not None:
                        yield hit[2]
                    continue
                row = None
                try:
                    row = self._snapshot_row(json.loads(Path(ent.path).read_text(encoding="utf-8")))
                except Exception:
                    row = None
                if len(memo) >= 4096:
                    memo.clear()
                memo[ent.path] = (st.st_mtime_ns, st.st_size, row)
                if row is not None:
                    yield row

    def decision(self, sid: str, rec: dict) -> None:
        """Persist one business decision, with evidence from every actual response."""
        if not self.enabled:
            return
        stamp = _number(rec.get("ts"))
        event_id = "dec-%d" % int(stamp * 1000)
        usage = [usage_event(item) for item in rec.get("usage", []) if isinstance(item, dict)]
        trace = {**usage_identity(rec), "sid": normalize_sid(sid), "kind": "decision",
                 "event_id": event_id, "started": stamp, "ended": stamp,
                 "elapsed": rec.get("elapsed"),
                 "totals": {key: rec.get(key, 0) for key in ("input_tokens", "output_tokens", "cached_tokens")}}
        if "usage" in rec:
            trace["usage"] = usage
        self._append_ledger(trace)

    def _append_ledger(self, rec: dict) -> None:
        """往 24 小时账本追加一行。写失败只吞异常：统计坏了不能影响业务。"""
        try:
            row = self._trace_row(rec)
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._ledger_cache = None
            self._recent_cache = None
            self._today_cache = None
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
        """Backfill snapshots once; the read projection itself never mutates history."""
        try:
            marker = self.ledger.parent / "tokens.backfill"
            if marker.exists():
                return
            known = {self._row_key(row) for row in self._ledger_rows()}
            rows: List[str] = []
            for row in self._snapshot_rows():
                key = self._row_key(row)
                if not row.get("ts") or key in known:
                    continue
                known.add(key)
                rows.append(json.dumps(row, ensure_ascii=False))
            if not rows:
                return  # Do not mark an empty first boot as a completed upgrade backfill.
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            if rows:
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
        """Historical missing identity stays missing, regardless of today's default."""
        return str(model or "").strip() or MODEL_UNKNOWN

    def _valid_usage(self, row: dict) -> Optional[list]:
        raw = row.get("usage")
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            return None
        usage = [usage_event(item, row.get("ts", 0)) for item in raw]
        return usage if self._usage_totals(usage) == self._counts(row) else None

    def _silent_cache_models(self, rows: List[dict]) -> set:
        """哪些模型从来不回报 cached_tokens（中转站只报未命中的那一截）。

        v4.87（用户 m13442 第 2 条）：反重力 Gemini 这类中转站把 input 报成「没命中的
        那截」，cached_tokens 恒为 0，价目页于是把命中部分也按全价算钱。判据是
        「这个模型在窗口里至少跑了 INFER_MIN_ROUNDS 轮，且每一轮的 cached 都是 0」——
        只要有一轮报了非 0，就说明上游会回报，这一批一律不反推。宁可少算，
        也不能把「真没命中」当成「命中」去少收钱。
        """
        seen: Dict[str, List[int]] = {}
        for row in rows:
            usage = row.get("usage")
            if not isinstance(usage, list):
                continue
            for item in usage:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("model") or "").strip() or MODEL_UNKNOWN
                stat = seen.setdefault(name, [0, 0])
                stat[0] += 1
                if _as_int(item.get("cached")) > 0:
                    stat[1] += 1
        return {name for name, stat in seen.items()
                if stat[0] >= INFER_MIN_ROUNDS and stat[1] == 0}

    def _with_inferred_cache(self, row: dict, usage: list, silent: set) -> list:
        """把「不回报缓存」的模型这一轮的每步 input 抬到完整 prompt、cached 填反推值。

        只在这一轮所有步都没报 cached、且模型在 silent 里时才动；反推不出来就原样返回。
        input 必须一起抬：pricing.summarize 里 c = min(a, cached)，只填 cached 不抬
        input 的话 c 会被夹到 a，成本还是不对。
        """
        if not usage:
            return usage
        if any(_as_int(item.get("cached")) > 0 for item in usage):
            return usage
        if not any((str(item.get("model") or "").strip() or MODEL_UNKNOWN) in silent
                   for item in usage):
            return usage
        full = _as_int(row.get("prompt_est"))
        if full <= 0:
            return usage
        steps = [{"in": _as_int(item.get("input")), "cached": 0} for item in usage]
        inferred = infer_cached_steps(full, steps, token_calib.gate(_record_model(row)))
        if not inferred:
            return usage
        out = []
        for item, guess in zip(usage, inferred):
            if not guess:
                out.append(item)
                continue
            fixed = dict(item)
            fixed["input"] = int(guess["full"])
            fixed["cached"] = int(guess["cached"])
            fixed["cache_inferred"] = True
            out.append(fixed)
        return out

    def _add_round(self, out: dict, row: dict, memo: dict, silent: Optional[set] = None) -> None:
        """A business round is global; model buckets count distinct participation.

        silent 是「不回报缓存命中」的模型名集合（见 _silent_cache_models）：这些模型
        的 cached 恒为 0，命中时按完整 prompt 估计反推，价目页才不会把命中部分按全价算钱。
        """
        usage = self._valid_usage(row)
        if usage is None:
            usage = [usage_event(row)]
        if silent:
            usage = self._with_inferred_cache(row, usage, silent)
        inp = sum(_as_int(item.get("input")) for item in usage)
        outp = sum(_as_int(item.get("output")) for item in usage)
        cached = sum(_as_int(item.get("cached")) for item in usage)
        out["input"] += inp
        out["output"] += outp
        out["cached"] += cached
        out["rounds"] += 1
        kind = self._kind(row)
        out[kind + "_rounds"] += 1
        # v4.68（用户 m02472）：概览卡片要按「回复 / 决策」分开看次数、用时、命中率，
        # 所以同一批数字再按 kind 存一份。key 用 _kind() 的返回值，未知 kind 也照样建桶。
        kinds = out.setdefault("kinds", {})
        if not isinstance(kinds, dict):
            kinds = out["kinds"] = {}
        kb = kinds.setdefault(kind, _blank_kind())
        kb["rounds"] += 1
        kb["input"] += inp
        kb["output"] += outp
        kb["cached"] += cached
        participating = set()
        for item in usage:
            name = self._model_name(item.get("model"), memo)
            bucket = out["models"].setdefault(name, {
                **{key: item.get(key, "") for key in ("model", "model_name", "provider_id", "provider_name", "model_id", "identity_source")},
                "input": 0, "output": 0, "cached": 0, "rounds": 0,
                "decision_rounds": 0, "reply_rounds": 0, "calls": 0, "usage": []})
            if bucket["identity_source"] != item["identity_source"]:
                bucket["identity_source"] = "mixed"
            for key in ("input", "output", "cached"):
                bucket[key] += item[key]
            bucket["calls"] += 1
            out["calls"] += 1
            kb["calls"] += 1
            # 打上 kind 再进模型桶：花费卡片要按回复 / 决策分开算钱（pricing.summarize 读这个字段）。
            item["kind"] = kind
            bucket["usage"].append(copy.deepcopy(item))
            if name not in participating:
                participating.add(name)
                bucket["rounds"] += 1
                bucket[kind + "_rounds"] += 1
        elapsed = row.get("elapsed") or row.get("elapsed_trigger")
        if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and _number(elapsed):
            memo["elapsed_sum"] = memo.get("elapsed_sum", 0.0) + float(elapsed)
            out["elapsed_rounds"] += 1
            kb["elapsed_sum"] += float(elapsed)
            kb["elapsed_rounds"] += 1

    def _round_rows(self, start: float) -> Iterator[dict]:
        """Deduplicate sid/kind/event; enrich old rows only on exact token equality."""
        snapshots = {}
        for row in self._snapshot_rows(start):
            if _number(row.get("ts")) >= start:
                snapshots.setdefault(self._row_key(row), row)
        seen = set()
        for original in self._ledger_rows():
            if _number(original.get("ts")) < start:
                continue
            key = self._row_key(original)
            if key in seen:
                continue
            seen.add(key)
            row = copy.deepcopy(original)
            usage = self._valid_usage(row)
            snapshot = snapshots.get(key)
            if snapshot is not None and self._counts(row) == self._counts(snapshot):
                if usage is None:
                    usage = self._valid_usage(snapshot)
                # v4.87：账本行不带 prompt_est（那是读快照时才算的），反推缓存命中要用，
                # 所以只要快照对得上就把估计值补过来。
                if _as_int(row.get("prompt_est")) <= 0:
                    row["prompt_est"] = _as_int(snapshot.get("prompt_est"))
            row["usage"] = usage if usage is not None else [usage_event(row)]
            yield row
        for key, row in snapshots.items():
            if key not in seen:
                seen.add(key)
                yield row

    def today_totals(self) -> dict:
        """今天的 token 汇总（输入 / 输出 / 缓存复用），给概览卡片用。

        v3.47：口径与 recent_totals 一样走 24 小时账本（traces 只补历史）。
        结果缓存 5 秒：日志页 5 秒自动刷一次，不能每次都去读几百个 JSON。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        cache = getattr(self, "_today_cache", None)
        if cache and cache[1] == today and (now - cache[0]) < 5.0:
            return _clone_totals(cache[2])
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        out = {"input": 0, "output": 0, "cached": 0, "total": 0, "rounds": 0, "cache_hit": None,
               "decision_rounds": 0, "reply_rounds": 0, "calls": 0, "models": {}, "elapsed_rounds": 0,
               "kinds": {"reply": _blank_kind(), "decision": _blank_kind()}}
        memo: dict = {}
        try:
            # v3.47：口径跟 recent_totals 一样——账本优先、traces 补历史并去重，
            # 不再只扫 traces（轮数一多就被 keep 截断，今日数字也跟着少）。
            # v4.87：先收齐窗口内的行，才能按模型判断「谁不回报缓存命中」，
            # 再逐轮累加（反推要改 input/cached，必须在求和之前做）。
            rows = list(self._round_rows(start))
            silent = self._silent_cache_models(rows)
            for row in rows:
                self._add_round(out, row, memo, silent)
        except Exception:
            logger.debug("[multi-decision] 统计今日 token 失败", exc_info=True)
        out["total"] = out["input"] + out["output"]
        if out["input"]:
            out["cache_hit"] = round(out["cached"] * 100.0 / out["input"], 1)
        _finish_kinds(out)
        self._today_cache = (now, today, _clone_totals(out))
        return _clone_totals(out)

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
        hours = max(1, int(hours or 24))
        start = now - hours * 3600
        cache = getattr(self, "_recent_cache", None)
        # v4.88：缓存键以前存的是 start（每次重算出来的 float），永远对不上缓存里的旧值，
        # 等于没有缓存——概览卡片每 5 秒就把几百份快照整读一遍。改成存 hours；
        # 新数据落账本时 _append_ledger 会把缓存置空，所以 TTL 放宽到 30 秒也不会看到旧数。
        if cache and cache[1] == hours and (now - cache[0]) < 30.0:
            return _clone_totals(cache[2])
        # models：按模型分开的同一批数字，给「花费」卡片算钱用（v3.37）
        out = {"input": 0, "output": 0, "cached": 0, "total": 0, "rounds": 0,
               "cache_hit": None, "avg_elapsed": None, "elapsed_rounds": 0, "models": {},
               "decision_rounds": 0, "reply_rounds": 0, "calls": 0,
               "kinds": {"reply": _blank_kind(), "decision": _blank_kind()}}
        # memo 放跨轮的状态：默认模型名（懒加载一次）与 elapsed 总和，
        # 全量放进去会被 _clone_totals 拷给调用方，不该出现在返回结构里。
        memo: dict = {}
        try:
            rows = list(self._round_rows(start))
            silent = self._silent_cache_models(rows)
            for row in rows:
                self._add_round(out, row, memo, silent)
        except Exception:
            logger.debug("[multi-decision] 统计最近 token 失败", exc_info=True)
        out["total"] = out["input"] + out["output"]
        if out["input"]:
            out["cache_hit"] = round(out["cached"] * 100.0 / out["input"], 1)
        if out["elapsed_rounds"]:
            out["avg_elapsed"] = round(memo.get("elapsed_sum", 0.0) / out["elapsed_rounds"], 1)
        _finish_kinds(out)
        self._recent_cache = (now, hours, _clone_totals(out))
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
            # v4.88：文件名就是毫秒时间戳，按名字倒序等价于按 mtime 倒序，
            # 但省掉几百次 path.stat()（Windows 上每次约 2ms，474 份就是 1 秒）。
            files = sorted(folder.glob("*.json"), reverse=True)
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
            # v4.88：同上，文件名即毫秒时间戳，按名字倒序即可，不必逐个 stat。
            files = sorted(folder.glob("*.json"), reverse=True)
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

    def provider_label(self, provider_id: str = "", model_name: str = "") -> str:
        """模型键 → API 渠道显示名（用户 m00375 第 6 条）。读不到就是空串。"""
        try:
            path = self._config_path()
            if path is None:
                return ""
            return _provider_label_of(path, provider_id, model_name)
        except Exception:
            return ""

    def provider_label_map(self) -> Dict[str, str]:
        """模型名 → API 渠道显示名，按配置文件 mtime 缓存。

        用户 m00375 第 8 条：回复记录里老快照没存 reply_provider，列表接口按模型名
        回填一次；一次列表上百行，不能每行都重新读一遍配置。同名模型挂在两个供应商
        下面就不猜（跟 _provider_label_of 一样，宁可空着）。
        """
        try:
            path = self._config_path()
            if path is None:
                return {}
            mtime = path.stat().st_mtime
            if self._cfg_label_mtime == mtime and self._cfg_label_map is not None:
                return self._cfg_label_map
            raw = json.loads(path.read_text(encoding="utf-8")) or {}
            provs = raw.get("providers") if isinstance(raw, dict) else None
            out: Dict[str, str] = {}
            if isinstance(provs, dict):
                for _pid, prov in provs.items():
                    if not isinstance(prov, dict):
                        continue
                    label = str(prov.get("name") or _pid).strip()[:60]
                    mcfg = prov.get("model_config")
                    if not label or not isinstance(mcfg, dict):
                        continue
                    for group in mcfg.values():
                        if not isinstance(group, dict):
                            continue
                        for item in group.values():
                            if not isinstance(item, dict):
                                continue
                            mn = str(item.get("model_name") or "").strip()
                            if not mn:
                                continue
                            if mn in out and out[mn] != label:
                                out[mn] = ""
                            else:
                                out[mn] = label
            self._cfg_label_mtime = mtime
            self._cfg_label_map = out
            return out
        except Exception:
            return {}
