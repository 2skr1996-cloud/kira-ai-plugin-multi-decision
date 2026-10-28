"""节奏模式：从「抽一个数死等」改成「每条的概率越等越急」。

v3.79（用户 m02095）先做过一版「按间隔排队 + 对数正态抖动」：抽一个 need，然后老实
数到第 need 条才触发。均值虽然等于 100/p，但用户 m05012 指出这个手感是错的——
抽到 20 就意味着第 1~19 条概率全是 0，看着像「待发堆了二十多条、一动不动」；
抽到 51 更离谱（v3.87 才把那次的 51 收掉）。

现在改成「每条一个递增的命中概率」：

    N = 100 / p              这一段平均隔几条回一次（没变，BandTuner 靠它记账）
    M = round(N * mult)      最长隔几条（mult 由「节奏松紧」决定，默认 1.5）
    F(k) = (k / M) ** alpha  到第 k 条为止累计中过的概率
    h(k) = 1 - (1 - F(k)) / (1 - F(k-1))    第 k 条这一条的概率

alpha 由「平均间隔必须等于 N」反解（二分），所以：
  · 平均依旧是 N 条一次，概率模式 / 曲线模式 / 动态调整的账都不变；
  · 最长只等 M 条（默认 1.5N = p 10% 时 15 条），不会再出现「等 51 条」；
  · 每条都有机会、越等越急（不是硬等）。p=10%、M=15 时大约：第 1 条 0.9%、
    第 5 条 4.8%、第 10 条 13%、第 13 条 30%、第 15 条 100%，累计到第 10 条约 49%。

均值锁死 N + 最长只到 M 这两条一摆，前段概率必然很小（数学上没得选：质量得集中在
后半段才凑得出平均 N 条）。想让它前段更活跃，只能把「节奏松紧」调大（M 变大）。

「节奏松紧」（sigma，0~120 除以 100 后传进来）控制 M 是 N 的几倍：
0 → 1.0 倍（就是按时刻表，每条都等到第 N 条）、65 → 1.5 倍（默认）、120 → 2.0 倍。

状态跟着 (sid, 段) 走，只存「已经数了几条」，落盘 data/band_rhythm.json，重启接着数。
"""

import json
import logging
import os
import random
import time

logger = logging.getLogger(__name__)

# 写盘节流（秒）：每条消息都写盘太浪费，命中时立即写一遍
SAVE_INTERVAL = 30.0

# 「节奏松紧」映射到「最长等待是平均间隔的几倍」（v3.88，用户 m05012）
# 下界就是 1.0：松紧 0 时最长 = 平均 = N，也就是「每条都等到第 N 条」
MULT_MIN = 1.0
MULT_MID = 1.5
MULT_MAX = 2.0
SIGMA_MID = 0.65      # 设置页默认 65 → 1.5 倍
SIGMA_MAX = 1.2       # 设置页 120 → 2.0 倍

# hazard 表缓存：{(N, M): (hs, alpha)}，纯函数算出来的，进程内复用
_CURVES: dict = {}


def mult_of(sigma) -> float:
    """节奏松紧 → 最长等待倍数。0 → 1.0（按时刻表），65 → 1.5，120 → 2.0。"""
    try:
        s = float(sigma or 0.0)
    except (TypeError, ValueError):
        s = 0.0
    s = max(0.0, min(SIGMA_MAX, s))
    if s <= SIGMA_MID:
        return MULT_MIN + (s / SIGMA_MID) * (MULT_MID - MULT_MIN)
    return MULT_MID + ((s - SIGMA_MID) / (SIGMA_MAX - SIGMA_MID)) * (MULT_MAX - MULT_MID)


def hazard_table(n_avg: int, max_wait: int):
    """(hs, alpha)。hs[k] = 已经过了 k-1 条、这一条命中的概率，k 从 1 到 max_wait。"""
    n_avg = max(1, int(n_avg or 1))
    max_wait = max(n_avg, int(max_wait or n_avg))
    key = (n_avg, max_wait)
    got = _CURVES.get(key)
    if got is not None:
        return got

    # 松紧 0（最长 = 平均）：直接给阶跃表——前面每条都不中，第 M 条必中。
    # 不能交给下面的二分：alpha 封顶 80 时 (k/M)**80 只是「非常小」而不是 0，
    # 第 9 条还留着 0.02% 的机会提前中，「0 = 按时刻表」就不严格了。
    if max_wait <= n_avg:
        hs = [0.0] * (max_wait + 1)
        hs[max_wait] = 1.0
        _CURVES[key] = (hs, 0.0)
        if len(_CURVES) > 200:
            _CURVES.clear()
        return hs, 0.0

    def expect(alpha: float) -> float:
        total = 0.0
        for k in range(1, max_wait + 1):
            total += 1.0 - ((k - 1) / float(max_wait)) ** alpha
        return total

    # F 的指数越大，质量越靠后、平均间隔越长；二分到「平均正好等于 n_avg」
    lo, hi = 0.05, 80.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if expect(mid) < n_avg:
            lo = mid
        else:
            hi = mid
    alpha = (lo + hi) / 2.0

    hs = [0.0] * (max_wait + 1)
    for k in range(1, max_wait + 1):
        f0 = ((k - 1) / float(max_wait)) ** alpha
        f1 = 1.0 if k >= max_wait else (k / float(max_wait)) ** alpha
        if f0 >= 1.0 or f1 >= 1.0:
            hs[k] = 1.0
        else:
            hs[k] = max(0.0, min(1.0, 1.0 - (1.0 - f1) / (1.0 - f0)))
    hs[max_wait] = 1.0
    _CURVES[key] = (hs, alpha)
    if len(_CURVES) > 200:
        _CURVES.clear()
        _CURVES[key] = (hs, alpha)
    return hs, alpha


class BandRhythm:
    """按段数条数的节奏器。engine 判定时调 roll()，其它什么都不用管。"""

    def __init__(self, path, now_fn=None):
        self.path = str(path)
        self.now_fn = now_fn or time.time
        # {sid: {"12-20": {"n": 3, "p": 10.0, "ts": 1792...}}}
        self._data: dict = {}
        self._dirty = False
        self._saved_at = 0.0
        self._load()

    # ---------------------------------------------------------------- 落盘
    @staticmethod
    def _key(start, end) -> str:
        return "%d-%d" % (int(start), int(end))

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._data = {}
        except Exception:
            logger.exception("[multi-decision] 读节奏状态失败，按空的处理")
            self._data = {}

    def _save(self, force: bool = False) -> None:
        now = float(self.now_fn())
        if not self._dirty:
            return
        if not force and now - self._saved_at < SAVE_INTERVAL:
            return
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            self._dirty = False
            self._saved_at = now
        except Exception:
            logger.exception("[multi-decision] 写节奏状态失败（已忽略）")

    def flush(self) -> None:
        """停插件 / 热重载前把没落盘的进度写掉。"""
        self._save(force=True)

    # ---------------------------------------------------------------- 读取
    def _rec(self, sid: str, key: str) -> dict:
        bucket = self._data.setdefault(str(sid or ""), {})
        rec = bucket.get(key)
        if not isinstance(rec, dict):
            rec = {}
            bucket[key] = rec
        return rec

    def progress(self, sid: str, start, end) -> dict:
        """给页面 / 日志看：这一段数到第几条了、这一条的概率是多少。"""
        rec = (self._data.get(str(sid or "")) or {}).get(self._key(start, end)) or {}
        out = {"n": 0, "need": 0, "p": 0.0, "chance": 0.0, "mult": MULT_MID}
        try:
            p = float(rec.get("p") or 0.0)
            n = int(rec.get("n") or 0)
        except (TypeError, ValueError):
            return out
        out.update({"n": n, "p": p})
        if p > 0:
            n_avg = max(1, int(round(100.0 / p)))
            try:
                mult = float(rec.get("mult") or MULT_MID)
            except (TypeError, ValueError):
                mult = MULT_MID
            max_wait = max(n_avg, int(round(n_avg * mult)))
            hs, _alpha = hazard_table(n_avg, max_wait)
            out["need"] = max_wait
            out["mult"] = round(mult, 2)
            out["chance"] = round(hs[min(max_wait, max(1, n))] * 100.0, 1)
        return out

    # ---------------------------------------------------------------- 判定
    def roll(self, sid: str, start, end, p, sigma: float = 0.65,
             quiet: int = 0, now=None) -> dict:
        """这一条要不要触发。

        返回 {"hit": bool, "chance": float, "n": int, "need": int}：
        chance 是「这一条本身的命中概率」，n 是已经数到第几条（命中后归零），
        need 是这一段最长会等到第几条（= M）。
        """
        out = {"hit": False, "chance": 0.0, "n": 0, "need": 0}
        try:
            p = float(p or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        if p <= 0:
            return out

        key = self._key(start, end)
        rec = self._rec(sid, key)
        ts = float(now if now is not None else self.now_fn())

        if p >= 100:
            # 100% 不用数，每条都算中
            rec.update({"n": 0, "p": 100.0, "ts": ts})
            self._dirty = True
            self._save(force=True)
            return {"hit": True, "chance": 100.0, "n": 0, "need": 1}

        n_avg = max(1, int(round(100.0 / p)))
        mult = mult_of(sigma)
        max_wait = max(n_avg, int(round(n_avg * mult)))
        hs, _alpha = hazard_table(n_avg, max_wait)

        n = int(rec.get("n") or 0) + 1
        if n > max_wait:
            n = max_wait
        chance = hs[n] * 100.0

        hit = False
        if n <= max(0, int(quiet or 0)):
            # 静默期：刚回过，前几条就算数到了也不触发
            hit = False
        elif n >= max_wait:
            hit = True
        else:
            try:
                hit = random.random() < hs[n]
            except Exception:
                hit = False

        if hit:
            rec.update({"n": 0, "p": round(p, 2), "ts": ts, "mult": round(mult, 2)})
            self._dirty = True
            self._save(force=True)
            return {"hit": True, "chance": chance, "n": 0, "need": max_wait}

        rec.update({"n": n, "p": round(p, 2), "ts": ts, "mult": round(mult, 2)})
        self._dirty = True
        self._save()
        out.update({"chance": chance, "n": n, "need": max_wait})
        return out

    # ---------------------------------------------------------------- 清理
    def forget(self, sid: str) -> None:
        if self._data.pop(str(sid or ""), None) is not None:
            self._dirty = True
            self._save(force=True)

    def clear_all(self) -> None:
        self._data = {}
        self._dirty = True
        self._save(force=True)
