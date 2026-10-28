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

import json
import os
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
        """整表落盘：tmp 写完再 os.replace，中途断电不会留下半截价目。"""
        self._cache = models
        self._hidden_cache = list(hidden)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"models": models, "hidden": list(hidden), "updated": int(time.time())},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:
            pass

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
            return {k: dict(v) for k, v in models.items() if k not in gone}

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
        """整表覆盖保存。传进来不是 dict 就当空表（等于清空价目）。

        hidden 保留原样：删除是单独的接口，不能被设置页的一次整表保存顺手复活。
        """
        clean: Dict[str, dict] = {}
        if isinstance(models, dict):
            for name, item in models.items():
                key = str(name or "").strip()[:120]
                if not key:
                    continue
                if isinstance(item, dict):
                    clean[key] = {
                        "in": _as_price(item.get("in")),
                        "out": _as_price(item.get("out")),
                        "cache": _as_price(item.get("cache")),
                    }
                else:
                    clean[key] = {"in": 0.0, "out": 0.0, "cache": 0.0}
        with self._lock:
            self._read()                       # 先把盘上的 hidden 读进来
            hidden = list(self._hidden_cache or [])
            self._write(clean, hidden)
            return {k: dict(v) for k, v in clean.items()}

    # ------------------------------------------------------------ 算钱

    def summarize(self, totals: Any) -> Dict[str, Any]:
        """按模型算最近这段时间的金额。

        totals 是 Tracer.recent_totals() 的结果（里面有 models 分桶）。

        v3.45（用户 2026-10-24 第 5 条）：价目表里**有这一项**就算已定价，值是 0
        也算（免费/送量模型很常见，不能把用户的 0 当成「没填」）；价目表里没有的
        模型默认按 0 元算，不再进 unpriced（用户原话：「识别到了新的也默认成0元」），
        只在 parts 里留一个软标记 `unset`，前端想提示「这个模型还没设价」时用得上。
        价目页删掉的（hidden）既不算钱也不进明细：用户的意思就是「别管它」。

        cache（缓存命中单价）只在填了 > 0 时才参与计算：命中那部分 token 按
        cache 价算、其余按 in 价算；没填就整段按 in 价算——命中本来就该便宜，
        但用户没给数就不能替他猜一个更低的价（用户 m03616 第 2 条）。
        """
        prices = self.all()
        gone = set(self.hidden())
        stats = (totals or {}).get("models") if isinstance(totals, dict) else {}
        stats = stats if isinstance(stats, dict) else {}
        parts = []
        total = 0.0
        unset = []
        for name, bucket in stats.items():
            if str(name) in gone:
                # 价目页删掉的模型：既不算钱，也不进「未定价」（用户本轮第 3 条）
                continue
            if not isinstance(bucket, dict):
                continue
            inp = _as_int(bucket.get("input"))
            outp = _as_int(bucket.get("output"))
            if not (inp or outp):
                continue
            # 命中数不能超过输入总量（脏数据/旧记录 clamp 一下，否则会算出负数金额）
            c = min(max(0, _as_int(bucket.get("cached"))), inp)
            price = prices.get(str(name)) or {}
            pin = float(price.get("in") or 0)
            pout = float(price.get("out") or 0)
            pcache = float(price.get("cache") or 0)
            if pcache > 0:
                cost = ((inp - c) * pin + c * pcache + outp * pout) / 1000000.0
            else:
                # 没填缓存价：维持老算法，命中部分按普通输入价算（不偷偷少算钱）
                cost = (inp * pin + outp * pout) / 1000000.0
            # 「已定价」= 价目表里有这个模型的条目（值为 0 也算）；没有条目就按 0 元记。
            # 所以这里一律 priced=True，进 unpriced 的情况已经不存在了；没进价目表的
            # 用 unset 软标记提示一下（不影响算钱）。
            priced = True
            is_unset = str(name) not in prices
            total += cost
            if is_unset:
                unset.append(str(name))
            parts.append(
                {
                    "model": str(name),
                    "input": inp,
                    "output": outp,
                    "cached": c,
                    "cost": round(cost, 4),
                    "priced": priced,
                    "unset": is_unset,
                }
            )
        parts.sort(key=lambda x: (-x["cost"], x["model"]))
        return {
            "total": round(total, 4),
            "currency": _CURRENCY,
            "unit": PRICE_UNIT,
            "priced": any(p["priced"] for p in parts),
            "parts": parts,
            # 兼容旧前端：新语义下每个模型都按价目表（或默认 0 元）算得出钱，
            # 不再有「算不出价」的模型，所以这个清单恒为空。
            "unpriced": [],
            # 软标记：这些模型没在价目表里，是按默认 0 元记的（只做提示，不算钱）
            "unset": unset,
        }
