"""按模型自学习的 prompt token 折算系数。

背景（用户 m10783）：反推缓存命中要拿「完整 prompt 有多大」当分母。文本按
「CJK 1 字 1 token」估、图片按每张 1032 估，这两个数都是 Gemini 实测出来的；
换一家模型就不准（GPT-4o 中文约 0.6~0.7 token/字，DeepSeek 约 0.6，Qwen 约 0.7，
图片 token 也各不相同）。写死常数等于每换一次模型就手调一次。

这里改成按模型名自己学。依据是：中转站不回报 cached_tokens 时，**没命中的那一轮
上报的 input 就是完整 prompt**，于是

    input ≈ k × (默认文本系数 × 文本 token + 默认单图 token × 图片张数)

k 就是这个模型相对默认口径的整体缩放。攒够样本后取 k 的中位数，样本不足时用默认值。

为什么不分别拟合文本系数和单图 token：真实记录里文本量和图片张数高度相关
（58 轮实测 corr=0.82），两参数最小二乘会被共线性带飞——实测 OLS 的 MAE 0.055
反而比默认值的 0.047 更差；只估一个 k 是 0.046，稳得多。

存储：<插件数据目录>/token_calib.json，删掉就会从默认值重新学。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

#: 默认文本系数：Gemini 实测中文约 1 字 0.83 token，_approx_tokens 的「CJK 1 字 1 token」
#: 乘这个系数才是真实 token 数。样本够的模型会被自学习系数覆盖，这里只是冷启动值。
DEFAULT_TEXT_SCALE = 1.0 / 1.2
#: 默认单张图片 token 数：Gemini 按 768×768 切 tile、每 tile 258 token，实测 1,025～1,033。
DEFAULT_IMAGE_TOKENS = 1032.0

#: 少于这么多轮样本就先用默认值（几轮噪声拟合出来的系数比默认值更差）。
MIN_SAMPLES = 5
#: 每个模型最多留这么多轮样本，只保留最近的。
MAX_SAMPLES = 60
#: 先验混合权重：拟合值按 n/(n+2)、默认值按 2/(n+2) 混合，样本多了先验自然消失。
PRIOR_WEIGHT = 2.0
#: 拟合结果夹在默认值的这个倍数区间里，防止几轮异常数据把系数带飞。
SCALE_MIN = 0.5
SCALE_MAX = 1.5

#: 反推门槛：上报值小于「完整估算 × 门槛」才认命中。门槛按这个模型样本的噪声定，
#: 门槛 = 1 − max(GATE_FLOOR, GATE_SIGMA × 残差 σ)，夹在 [GATE_MIN, 1] 之间。
#: 估得准（噪声小）就敢认 10% 这种小命中，估得不准就退回保守口径。
GATE_SIGMA = 2.5
GATE_FLOOR = 0.02
GATE_MIN = 0.85
#: 没样本时的门槛，跟以前写死的 0.75 一致。
GATE_DEFAULT = 0.75
#: 按模型自学习开关：开着时每个模型按自己上报的 token 数校准系数与门槛，
#: 关掉则系数与门槛都退回默认值（DEFAULT_TEXT_SCALE / GATE_DEFAULT）。
LEARN_ENABLED = True


def _norm(model: Any) -> str:
    return str(model or "").strip()[:80]


def _fit(samples: list) -> Optional[tuple]:
    """按样本估整体缩放 k，再乘回默认的「文本 / 图片」拆分。

    samples 是 [(text, imgs, input), ...]。取 input / 默认估算 的中位数：中位数
    不怕个别「部分命中」的轮次（它们上报的 input 只是没命中那截，会偏小）。
    """
    if len(samples) < MIN_SAMPLES:
        return None
    ratios = _ratios(samples)
    if len(ratios) < MIN_SAMPLES:
        return None
    k = _median(ratios)
    if not math.isfinite(k) or k <= 0:
        return None
    return DEFAULT_TEXT_SCALE * k, DEFAULT_IMAGE_TOKENS * k


def _ratios(samples: list) -> list:
    """每条样本的 input / 默认估算。"""
    out = []
    for text, imgs, inp in samples:
        base = DEFAULT_TEXT_SCALE * text + DEFAULT_IMAGE_TOKENS * imgs
        if base > 0 and inp > 0:
            out.append(inp / base)
    return out


def _median(values: list) -> float:
    values = sorted(values)
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0


def _spread(samples: list) -> float:
    """样本 ratio 的稳健标准差（MAD × 1.4826），用来定反推门槛。"""
    ratios = _ratios(samples)
    if len(ratios) < MIN_SAMPLES:
        return 0.0
    med = _median(ratios)
    return _median([abs(r - med) for r in ratios]) * 1.4826


def _blend(value: float, default: float, n: int) -> float:
    """夹到合理区间，再按样本数跟默认值混合。"""
    lo, hi = default * SCALE_MIN, default * SCALE_MAX
    value = min(hi, max(lo, value))
    return (value * n + default * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)


class TokenCalib:
    """按模型名存样本、拟合系数。读的时候按 mtime 自动重载，写只由 Tracer 触发。"""

    def __init__(self, path):
        self.path = Path(path)
        self._models: dict = {}
        self._mtime: Optional[int] = None
        self._lock = threading.Lock()
        self._load()

    # ---------------------------------------------------------------- 读
    def _load(self) -> None:
        try:
            st = self.path.stat()
        except OSError:
            return
        if self._mtime == st.st_mtime_ns:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        models = raw.get("models") if isinstance(raw, dict) else None
        if isinstance(models, dict):
            self._models = models
        self._mtime = st.st_mtime_ns

    def factors(self, model: str) -> tuple:
        """这个模型当前的 (文本系数, 每张图 token 数)；没样本就给默认值。"""
        if not LEARN_ENABLED:
            return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
        key = _norm(model)
        if not key:
            return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
        self._load()
        entry = self._models.get(key)
        if not isinstance(entry, dict):
            return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
        try:
            f = float(entry.get("f"))
            g = float(entry.get("g"))
        except (TypeError, ValueError):
            return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
        if not (math.isfinite(f) and math.isfinite(g)) or f <= 0 or g < 0:
            return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
        return f, g

    def samples(self, model: str) -> int:
        entry = self._models.get(_norm(model))
        return int(entry.get("n") or 0) if isinstance(entry, dict) else 0
    def gate(self, model: str) -> float:
        """这个模型的反推门槛：上报值小于「完整估算 × 门槛」才认命中。"""
        if not LEARN_ENABLED:
            return GATE_DEFAULT
        key = _norm(model)
        if not key:
            return GATE_DEFAULT
        self._load()
        entry = self._models.get(key)
        if not isinstance(entry, dict):
            return GATE_DEFAULT
        try:
            spread = float(entry.get("spread") or 0.0)
        except (TypeError, ValueError):
            return GATE_DEFAULT
        if not math.isfinite(spread) or spread <= 0:
            return GATE_DEFAULT
        return min(1.0, max(GATE_MIN, 1.0 - max(GATE_FLOOR, GATE_SIGMA * spread)))

    # ---------------------------------------------------------------- 写
    def observe(self, model, text_tokens, image_count, input_tokens, key: str = "") -> None:
        """记一个「没命中」的标定点，并重算这个模型的系数。

        key 是这一轮的标识（sid:event_id）。同一轮被反复读到（每次翻页都会重算）
        时只记一次，否则样本会被常看的那几轮挤满。
        """
        if not LEARN_ENABLED:
            return
        name = _norm(model)
        try:
            text = int(text_tokens)
            imgs = int(image_count)
            inp = int(input_tokens)
        except (TypeError, ValueError):
            return
        if not name or text <= 0 or inp <= 0 or imgs < 0:
            return
        tag = str(key or "")[:120]
        with self._lock:
            self._load()
            entry = self._models.get(name)
            if not isinstance(entry, dict):
                entry = {}
            rows = entry.get("samples")
            if not isinstance(rows, list):
                rows = []
            if tag and any(isinstance(r, list) and len(r) > 3 and r[3] == tag for r in rows):
                return
            rows.append([text, imgs, inp, tag])
            del rows[:-MAX_SAMPLES]
            entry["samples"] = rows
            entry["n"] = len(rows)
            fit = _fit([tuple(r[:3]) for r in rows if isinstance(r, list) and len(r) >= 3])
            if fit:
                entry["f"] = round(_blend(fit[0], DEFAULT_TEXT_SCALE, len(rows)), 6)
                entry["g"] = round(_blend(fit[1], DEFAULT_IMAGE_TOKENS, len(rows)), 3)
                entry["spread"] = round(_spread([tuple(r[:3]) for r in rows
                                                 if isinstance(r, list) and len(r) >= 3]), 4)
            entry["updated"] = int(time.time())
            self._models[name] = entry
            self._save()

    def _save(self) -> None:
        tmp: Optional[Path] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=str(self.path.parent),
                                        prefix=self.path.name + ".", suffix=".tmp")
            os.close(fd)
            tmp = Path(name)
            tmp.write_text(json.dumps({"version": 1, "models": self._models},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            tmp = None
            try:
                self._mtime = self.path.stat().st_mtime_ns
            except OSError:
                self._mtime = None
        except Exception:
            pass
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass


_STORE: Optional[TokenCalib] = None
_STORE_LOCK = threading.Lock()


def configure(path) -> TokenCalib:
    """由 Tracer 初始化时调用一次；同一个路径重复调用不会重建。"""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None or _STORE.path != Path(path):
            _STORE = TokenCalib(path)
        return _STORE


def factors(model: str) -> tuple:
    store = _STORE
    if store is None:
        return DEFAULT_TEXT_SCALE, DEFAULT_IMAGE_TOKENS
    return store.factors(model)


def gate(model: str) -> float:
    store = _STORE
    if store is None:
        return GATE_DEFAULT
    return store.gate(model)


def observe(model, text_tokens, image_count, input_tokens, key: str = "") -> None:
    store = _STORE
    if store is None:
        return
    store.observe(model, text_tokens, image_count, input_tokens, key)
