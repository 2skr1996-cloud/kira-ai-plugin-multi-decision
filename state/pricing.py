"""模型价目表：手填每个模型每百万 token 的价格，用来算「最近 24 小时花了多少钱」。

为什么插件自己存价目：框架和供应商都不提供「本次调用花了多少钱」，token 数
有了、单价只有用户知道（各家中转/官方渠道价格还常不一样）。所以这里只做两件
事——把用过的模型记下来，让用户填单价；再拿 token 乘单价出金额（用户 m02080 第 4 条）。

v3.38 起每个模型多一个「缓存命中价」cache：命中缓存的输入 token 比普通输入便宜，
只填 in/out 时命中部分照样按输入价算（不偷偷少算钱）。用户 m03616 第 2 条。

v3.44（用户本轮第 3 条）起价目页每行左侧有删除按钮：删掉的模型记进 hidden，
单价一起清掉——这样它既不会在价目页重新冒出来（哪怕最近 24 小时还在用），
也不会被 summarize 拿去算钱或列进「未定价」。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_FILE = "pricing.json"
PRICE_UNIT = "元 / 百万 token"
_CURRENCY = "元"


def _as_price(value: Any) -> float:
    """单价只接受非负数字；填歪了当 0（等于没填价，不参与计算）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num or num in (float("inf"), float("-inf")):
        return 0.0
    return round(max(0.0, num), 6)


def _as_int(value: Any) -> int:
    """token 计数只接受整数；填歪了当 0（脏数据不能让整张花费卡片算不出来）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _clean_hidden(value: Any) -> List[str]:
    """hidden 只收模型名：去空白、去空串、去重保序；脏数据当没有。"""
    out: List[str] = []
    if isinstance(value, (list, tuple, set)):
        for item in value:
            name = str(item or "").strip()[:120]
            if name and name not in out:
                out.append(name)
    return out


def _clean_peak(value: Any) -> dict:
    """峰价显式扩展；多段列表权威，空价继承，0 是免费。"""
    if not isinstance(value, dict):
        raise ValueError("高峰设置必须是一组完整设置")
    out = {"enabled": False, "in": None, "out": None, "cache": None,
           "start": "", "end": "", "weekends": False}
    for key in ("enabled", "weekends"):
        item = value.get(key, False)
        if not isinstance(item, bool):
            raise ValueError("高峰与周末开关必须是开启或关闭")
        out[key] = item
    def clock(item):
        if not isinstance(item, str):
            raise ValueError("高峰时间请填写 00:00–23:59")
        item = item.strip()
        if item and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", item):
            raise ValueError("高峰时间请填写 00:00–23:59")
        return item
    if "periods" in value:
        periods = value["periods"]
        if not isinstance(periods, list):
            raise ValueError("高峰时段必须是起止时间列表")
        out["periods"] = []
        for period in periods:
            if not isinstance(period, dict):
                raise ValueError("每个高峰时段都要填写开始和结束时间")
            start, end = clock(period.get("start", "")), clock(period.get("end", ""))
            # 用户 m00153：起止都没填的那一段等于不存在（前端固定画两行，空行直接丢掉）。
            if not start and not end:
                continue
            if not start or not end or start == end:
                raise ValueError("每个高峰时段请填写不同的开始和结束时间")
            out["periods"].append({"start": start, "end": end})
        # 用户 m00153：一行最多两段，多出来的截掉（老客户端也不会越界）。
        out["periods"] = out["periods"][:2]
        if out["enabled"] and not out["periods"]:
            raise ValueError("开启高峰后请至少填写一个完整时段")
        if out["periods"]:
            out.update(out["periods"][0])  # 仅供旧客户端显示首段，不另计一次
    else:
        for key in ("start", "end"):
            out[key] = clock(value.get(key, ""))
        if out["enabled"] and (not out["start"] or not out["end"] or out["start"] == out["end"]):
            raise ValueError("开启高峰后请填写不同的开始和结束时间")
    for key in ("in", "out", "cache"):
        item = value.get(key)
        if item is None or (isinstance(item, str) and not item.strip()):
            continue
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("高峰单价必须是非负数字，留空表示沿用普通价格") from None
        if isinstance(item, bool) or not math.isfinite(number) or number < 0:
            raise ValueError("高峰单价必须是非负数字，留空表示沿用普通价格")
        out[key] = round(number, 6)
    return out


def _event_period(peak: dict, timestamp: Any) -> tuple:
    """只看这次调用的记录时刻；多个时段取并集，不重复计峰。"""
    try:
        stamp = float(timestamp)
        if isinstance(timestamp, bool) or not math.isfinite(stamp) or stamp <= 0:
            return False, True
        local = time.localtime(stamp)
    except (TypeError, ValueError, OverflowError, OSError):
        return False, True
    if not peak.get("enabled") or (local.tm_wday >= 5 and not peak.get("weekends")):
        return False, False
    periods = peak["periods"] if "periods" in peak else [peak]
    minute = local.tm_hour * 60 + local.tm_min
    for period in periods:
        start, end = period["start"], period["end"]
        a = int(start[:2]) * 60 + int(start[3:])
        b = int(end[:2]) * 60 + int(end[3:])
        if a <= minute < b if a < b else minute >= a or minute < b:
            return True, False
    return False, False


class Pricing:
    """价目表落盘在插件数据目录的 pricing.json。

    结构：{"models": {"模型名": {"in": 输入单价, "out": 输出单价, "cache": 缓存命中单价}},
          "hidden": ["删掉的模型名", ...], "updated": 时间戳}
    单价单位统一「元 / 百万 token」：各家官网刊例都是这个口径，用户抄数最省事。
    cache 为 0（旧文件没有这个字段时也当 0）表示「没填缓存价」，命中部分按输入价算。
    hidden 里的模型等于「用户从价目页删掉了」：单价比清掉，页面与统计都不再出现。
    """

    def __init__(self, data_dir: Any, filename: str = DEFAULT_FILE):
        self.path = Path(str(data_dir)) / filename
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, dict]] = None
        # hidden 与 models 一起缓存在内存里，落盘时两份一起写
        self._hidden_cache: Optional[List[str]] = None

    # ------------------------------------------------------------ 读写

    def _read(self) -> Dict[str, dict]:
        """读盘上的价目表（models + hidden 一起读，只读一次）。"""
        cache = self._cache
        if cache is not None:
            return cache
        models: Dict[str, dict] = {}
        hidden: List[str] = []
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
                if isinstance(raw, dict):
                    hidden = _clean_hidden(raw.get("hidden"))
                    src = raw.get("models")
                else:
                    src = None
                if isinstance(src, dict):
                    for name, item in src.items():
                        key = str(name or "").strip()
                        if not key or not isinstance(item, dict):
                            continue
                        models[key] = {
                            "in": _as_price(item.get("in")),
                            "out": _as_price(item.get("out")),
                            # 旧价目文件没有 cache 字段：当 0，等于「没填缓存价」
                            "cache": _as_price(item.get("cache")),
                        }
                        if item.get("peak") is not None:
                            try:
                                models[key]["peak"] = _clean_peak(item["peak"])
                            except ValueError:
                                pass  # 旧文件中损坏的扩展不抹掉其他模型和普通三价
        except Exception:
            models = {}
            hidden = []
        self._cache = models
        self._hidden_cache = hidden
        return models

    def _load(self) -> Dict[str, dict]:
        """老名字，等同于 _read（保留给旧调用方）。"""
        return self._read()

    def _write(self, models: Dict[str, dict], hidden: List[str]) -> None:
        """原子替换成功后才发布缓存；失败必须通知调用方，不能假报已保存。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(
                {"models": models, "hidden": list(hidden), "updated": int(time.time())},
                ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        self._cache = copy.deepcopy(models)
        self._hidden_cache = list(hidden)

    def hidden(self) -> List[str]:
        """价目页删掉的模型名（按删除先后），返回副本。"""
        with self._lock:
            self._read()
            return list(self._hidden_cache or [])

    def all(self) -> Dict[str, dict]:
        """当前价目（模型名 → {"in","out","cache"}），返回副本，调用方随便改。

        hidden 里的模型不返回：前端拿这份清单直接渲染，删掉的就不会再冒出来。
        """
        with self._lock:
            models = self._read()
            gone = set(self._hidden_cache or [])
            return {k: copy.deepcopy(v) for k, v in models.items() if k not in gone}

    def hide(self, name: Any) -> List[str]:
        """删掉一个模型的价目：单价清空、模型名记进 hidden。返回最新的 hidden 列表。

        只删价目页那一条，不动历史轨迹——已经花掉的钱还是已经花掉了。
        """
        key = str(name or "").strip()[:120]
        with self._lock:
            models = dict(self._read())
            hidden = list(self._hidden_cache or [])
            if not key:
                return hidden
            if key not in hidden:
                hidden.append(key)
            models.pop(key, None)
            self._write(models, hidden)
            return list(hidden)

    def save(self, models: Any) -> Dict[str, dict]:
        """整表保存；旧客户端省略 peak 时保留扩展，显式 null 才清除。"""
        clean: Dict[str, dict] = {}
        provided = set()
        if isinstance(models, dict):
            for name, item in models.items():
                key = str(name or "").strip()[:120]
                if not key:
                    continue
                row = item if isinstance(item, dict) else {}
                clean[key] = {"in": _as_price(row.get("in")),
                              "out": _as_price(row.get("out")),
                              "cache": _as_price(row.get("cache"))}
                if "peak" in row:
                    provided.add(key)
                    if row["peak"] is not None:
                        clean[key]["peak"] = _clean_peak(row["peak"])
        with self._lock:
            previous = self._read()
            for key in clean:
                old_peak = previous.get(key, {}).get("peak")
                if key not in provided and old_peak is not None:
                    clean[key]["peak"] = copy.deepcopy(old_peak)
                elif ("peak" in clean[key] and "periods" not in clean[key]["peak"]
                      and isinstance(old_peak, dict) and "periods" in old_peak):
                    # 旧单段客户端只编辑首段；只有显式 periods 才能删除后续段。
                    peak = clean[key]["peak"]
                    first = [{"start": peak["start"], "end": peak["end"]}] if (peak["start"] or peak["end"]) else []
                    # 禁用且空端点的旧表单没有表达删段；重复保存也必须保全列表。
                    remaining = old_peak["periods"][1:] if first else old_peak["periods"]
                    peak["periods"] = first + copy.deepcopy(remaining)
                    clean[key]["peak"] = _clean_peak(peak)
            hidden = list(self._hidden_cache or [])
            self._write(clean, hidden)
            return copy.deepcopy(clean)

    # ------------------------------------------------------------ 算钱

    def summarize(self, totals: Any) -> Dict[str, Any]:
        """逐调用计价；业务轮汇总不再次叠加，旧记录只作近似估算。"""
        prices = self.all()
        gone = set(self.hidden())
        stats = (totals or {}).get("models") if isinstance(totals, dict) else {}
        stats = stats if isinstance(stats, dict) else {}
        parts, unset = [], []
        total = normal_total = peak_total = 0.0
        any_approximate = any_unknown = False
        # v4.68（用户 m02472）：花费卡片要把「回复 / 决策」分开显示，逐事件按 kind 再记一份。
        by_kind: Dict[str, Dict[str, Any]] = {}

        def _kind_bucket(kind: str) -> Dict[str, Any]:
            return by_kind.setdefault(str(kind or "reply"), {
                "cost": 0.0, "input": 0, "output": 0, "cached": 0, "calls": 0})
        for name, bucket in stats.items():
            if str(name) in gone or not isinstance(bucket, dict):
                continue
            price = prices.get(str(name)) or {}
            pin = float(price.get("in") or 0)
            pout = float(price.get("out") or 0)
            pcache = float(price.get("cache") or 0)
            peak = price.get("peak") or {}
            usage = bucket.get("usage")
            events = usage if isinstance(usage, list) else [dict(bucket, timestamp_source="legacy_round")]
            # 这个模型填没填价，事件循环里就要用（分 kind 的花费要标出「未定价」）
            is_unset = str(name) not in prices
            inp = outp = cached = 0
            normal_cost = peak_cost = 0.0
            approximate = unknown_time = False
            part_kinds: Dict[str, float] = {}
            for event in events:
                if not isinstance(event, dict):
                    continue
                a = max(0, _as_int(event.get("input")))
                b = max(0, _as_int(event.get("output")))
                c = min(a, max(0, _as_int(event.get("cached"))))
                inp += a
                outp += b
                cached += c
                is_peak, missing_time = _event_period(peak, event.get("ts"))
                unknown_time = unknown_time or missing_time
                approximate = approximate or missing_time or event.get("timestamp_source") != "request_start"
                rate_in, rate_out = pin, pout
                rate_cache = pcache if pcache > 0 else pin
                if is_peak:
                    if peak.get("in") is not None:
                        rate_in = peak["in"]
                    if peak.get("out") is not None:
                        rate_out = peak["out"]
                    # 普通 cache=0 保留旧「未填」语义，继承时跟随实际输入价。
                    rate_cache = pcache if pcache > 0 else rate_in
                    if peak.get("cache") is not None:
                        rate_cache = peak["cache"]
                cost = ((a - c) * rate_in + c * rate_cache + b * rate_out) / 1000000.0
                kb = _kind_bucket(str(event.get("kind") or "reply"))
                kb["priced"] = bool(kb.get("priced")) or not is_unset
                kb["cost"] += cost
                kb["input"] += a
                kb["output"] += b
                kb["cached"] += c
                kb["calls"] += 1
                part_kinds[str(event.get("kind") or "reply")] = part_kinds.get(str(event.get("kind") or "reply"), 0.0) + cost
                if is_peak:
                    peak_cost += cost
                else:
                    normal_cost += cost
            if not (inp or outp):
                continue
            cost = normal_cost + peak_cost
            total += cost
            normal_total += normal_cost
            peak_total += peak_cost
            any_approximate = any_approximate or approximate
            any_unknown = any_unknown or unknown_time
            if is_unset:
                unset.append(str(name))
            parts.append({"model": str(name), "input": inp, "output": outp,
                          "cached": cached, "cost": round(cost, 4), "priced": True,
                          "unset": is_unset, "normal_cost": round(normal_cost, 4),
                          "peak_cost": round(peak_cost, 4), "approximate": bool(approximate),
                          "unknown_time": bool(unknown_time),
                          "reply_cost": round(part_kinds.get("reply", 0.0), 4),
                          "decision_cost": round(part_kinds.get("decision", 0.0), 4)})
        parts.sort(key=lambda x: (-x["cost"], x["model"]))
        kinds_out = {k: {"total": round(v["cost"], 4), "input": v["input"], "output": v["output"],
                        "cached": v["cached"], "calls": v["calls"],
                        "priced": bool(v.get("priced"))} for k, v in by_kind.items()}
        return {"total": round(total, 4), "currency": _CURRENCY, "unit": PRICE_UNIT,
                "priced": any(p["priced"] for p in parts), "parts": parts,
                "by_kind": kinds_out,
                "unpriced": [], "unset": unset, "normal_cost": round(normal_total, 4),
                "peak_cost": round(peak_total, 4), "approximate": bool(any_approximate),
                "unknown_time": bool(any_unknown)}

