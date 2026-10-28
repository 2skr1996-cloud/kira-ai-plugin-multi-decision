# -*- coding: utf-8 -*-
"""按时段统计「消息条数 / 触发轮数 / 回复次数」，给动态调整算每个时段的平均数据。

用户 m23596 第 8 条：动态调整改按「每个时段的平均数据」来估，不再只看总量。

数据源是 decisions/*.jsonl（一天一个文件、多会话混写）。所有口径都跟
state/decision_log.py 对齐，不另立一套：

- 消息条数：DecisionLog._message_weight(rec)
  （trigger / round_end 是事件不算消息；老的 trigger 行没写「已记消息」的
  还要按 reason 补一条，全都交给那个函数，这里不重复判断）
- 触发轮数：rec.get("result") == "round_end"
- 回复次数：round_end 且 rec.get("replied") 为真
- 会话过滤：rec.get("sid") == sid；sid 传空串表示全部会话一起算
  （老日志里有「机器人名:gm:xxx」这种带前缀的 sid，跟 hour_histogram 一样
  不在这里再做归一，调用方传归一后的 id 即可）

时间一律按本机时区（跟 decision_log 写文件、按天分文件同口径）。
一天 48 格，slot = tm_hour * 2 + (tm_min >= 30)。

这个模块只读，不写任何文件、不改任何状态；整个对外方法都 try/except 包住，
出错就返回空统计，绝不影响业务。
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: 一天切 48 格（每格 30 分钟）。
SLOT_COUNT = 48
SLOT_SECONDS = 1800
#: 结果缓存保留多久（秒）。这个函数在每轮结算时被调用，30 秒足够挡住连续几轮。
CACHE_TTL = 30.0
#: 观察窗口最多往前看多少天（decision_log 默认保留 56 天，留点余量）。
MAX_WINDOW_DAYS = 120
#: 单文件解析结果缓存的上限（文件 × 会话）；超过就整体丢掉重来。
FILE_CACHE_LIMIT = 512
#: 星期分桶至少要有这么多天有数据才敢用，否则退回最近 N 天。
MIN_WEEKDAY_SAMPLES = 4
#: 算消息条数时排除的「事件」行（只有 log 上没有 _message_weight 时才用到，
#: 正常情况口径以 DecisionLog._message_weight 为准）。
EVENT_RESULTS = ("trigger", "round_end")


class BandStats:
    """按时段统计三类计数，并折算成「每天平均」「回复率」。

    只依赖 DecisionLog 实例的 `dir`（目录）和 `_message_weight`（口径），
    不碰它的写侧、不 import 它，方便在测试里塞一个假的 log。
    """

    SLOT_COUNT = SLOT_COUNT
    SLOT_SECONDS = SLOT_SECONDS
    CACHE_TTL = CACHE_TTL
    MAX_WINDOW_DAYS = MAX_WINDOW_DAYS
    FILE_CACHE_LIMIT = FILE_CACHE_LIMIT
    MIN_WEEKDAY_SAMPLES = MIN_WEEKDAY_SAMPLES

    def __init__(self, log: Any, now_fn: Optional[Callable[[], float]] = None) -> None:
        self.log = log
        self._now_fn = now_fn or time.time
        # (sid, days, weekday) -> (过期时间, 汇总结果)；存汇总，取用时深拷贝
        self._cache: dict = {}
        # (路径, mtime, 大小, sid) -> 48 格计数；同一天的文件只解析一次
        self._file_cache: dict = {}

    # ------------------------------------------------------------- 对外接口

    def slots(self, sid: str, days: int, weekday: Optional[int] = None,
              now: Optional[float] = None) -> dict:
        """最近 days 天、每 30 分钟一格的汇总。

        weekday 传 0-6（周一到周日）就只取「那个星期几」的天。
        days 返回的是**实际观察到的天数**（目录里真有文件、且符合星期筛选的天数），
        不是传进来的窗口天数——用户明确要求「哪怕没满 7 天也照用」，
        所以分母要按真实天数来，不能写死 7。
        """
        try:
            return self._slots(sid, days, weekday, now)
        except Exception:
            return {"slots": [{"m": 0, "t": 0, "r": 0} for _ in range(SLOT_COUNT)],
                    "days": 0, "window": 0, "active_days": 0, "day_keys": []}

    def band_stats(self, sid: str, bands: Iterable[Any],
                   weekday_bucket: bool = False, weekday_window_days: int = 56,
                   week_days: int = 7, now: Optional[float] = None) -> dict:
        """把时段的各项计数合并成「每段一行」，输出顺序与传入一致。

        bands 每项可以是 (s, e) / (s, e, v) 元组，也可以是 {"s":.., "e":..} 字典。
        跨零点的段（如 23:00-01:00 写成 s=46, e=2）按环形合并。
        """
        try:
            return self._band_stats(sid, bands, weekday_bucket,
                                    weekday_window_days, week_days, now)
        except Exception:
            return {"source": "week7", "days": 0, "active_days": 0,
                    "bands": self._empty_bands(bands)}

    # ------------------------------------------------------------- 内部实现

    def _band_stats(self, sid: str, bands: Iterable[Any], weekday_bucket: bool,
                    weekday_window_days: int, week_days: int,
                    now: Optional[float]) -> dict:
        now_ts = self._now(now)
        sid = str(sid or "")
        day7 = self._norm_days(week_days, 7)
        wide = self._norm_days(weekday_window_days, 56)
        data = None
        source = "week7"
        if weekday_bucket:
            try:
                today_wd = int(time.localtime(now_ts).tm_wday)
            except Exception:
                today_wd = None
            if today_wd is not None:
                cand = self.slots(sid, wide, weekday=today_wd, now=now_ts)
                if int(cand.get("active_days") or 0) >= self.MIN_WEEKDAY_SAMPLES:
                    data = cand
                    source = "weekday"
        if data is None:
            data = self.slots(sid, day7, now=now_ts)
            source = "week7"
        cells = data.get("slots") or []
        days_used = max(1, int(data.get("days") or 0))  # 实际观察天数，不是有数据的天数
        rows = self._rows_from_cells(cells, bands, days_used)
        return {"source": source, "days": int(data.get("days") or 0),
                "active_days": int(data.get("active_days") or 0), "bands": rows}

    def _rows_from_cells(self, cells: list, bands: Iterable[Any],
                         days_used: int) -> list:
        """把 48 格计数按段合并成「每段一行」（band_stats 与 weekday_band_stats 共用）。"""
        rows = []
        for item in (bands or []):
            s, e = self._band_bounds(item)
            m = t = r = 0
            for idx in self._band_range(s, e):
                if idx >= len(cells):
                    continue
                cell = cells[idx] or {}
                m += int(cell.get("m") or 0)
                t += int(cell.get("t") or 0)
                r += int(cell.get("r") or 0)
            rows.append({
                "s": s, "e": e,
                "m": m, "t": t, "r": r,
                "m_day": round(m / days_used, 2),
                "t_day": round(t / days_used, 2),
                "r_day": round(r / days_used, 2),
                # 每轮触发里回了几次（触发之后没回就是 0%，不算噪声）
                "reply_rate": round(r / t * 100.0, 1) if t else None,
                # 每条消息被回复的概率
                "msg_rate": round(r / m * 100.0, 1) if m else None,
            })
        return rows

    def weekday_band_stats(self, sid: str, bands: Iterable[Any],
                           window_days: int = 56,
                           now: Optional[float] = None) -> dict:
        """按星期几分别算一遍「每段一行」（0 = 周一 … 6 = 周日）。

        用户 m03323：动态调整面板开了「使用星期数据」之后，上面那四行「每天平均」
        要换成「每个星期几各自多少」——统一的目标只适合没开星期数据的时候。
        每个星期几各扫一遍自己的桶（wide 天窗口），天数分母用该桶实际凑到的天数。
        """
        try:
            now_ts = self._now(now)
            wide = self._norm_days(window_days, 56)
            out = []
            for wd in range(7):
                data = self.slots(sid, wide, weekday=wd, now=now_ts)
                cells = data.get("slots") or []
                days_used = max(1, int(data.get("days") or 0))
                out.append({
                    "wd": wd,
                    "days": int(data.get("days") or 0),
                    "active_days": int(data.get("active_days") or 0),
                    "bands": self._rows_from_cells(cells, bands, days_used),
                })
            return {"source": "weekday", "week": out}
        except Exception:
            return {"source": "weekday", "week": []}

    def _slots(self, sid: str, days: int, weekday: Optional[int],
               now: Optional[float]) -> dict:
        now_ts = self._now(now)
        days = self._norm_days(days, 7)
        try:
            wd = None if weekday is None else int(weekday) % 7
        except (TypeError, ValueError):
            wd = None
        # 缓存键带上「今天」：窗口是按天算的，跨天不能串；
        # 同一天内数据刷新、30 秒后再读就是缓存的本意。
        try:
            today = time.strftime("%Y-%m-%d", time.localtime(now_ts))
        except (TypeError, ValueError, OSError, OverflowError):
            today = ""
        key = (str(sid or ""), days, wd, today)
        cached = self._cache_get(key, now_ts)
        if cached is not None:
            return cached
        cells = [[0, 0, 0] for _ in range(SLOT_COUNT)]
        day_keys = []
        active_days = 0
        for path in self._files_in_window(days, now_ts):
            if wd is not None and self._weekday_of(path.stem) != wd:
                continue
            day_cells = self._scan_file(path, str(sid or ""))
            if day_cells is None:
                continue
            day_keys.append(path.stem)
            m = t = r = 0
            for idx, cell in enumerate(day_cells):
                if not (cell[0] or cell[1] or cell[2]):
                    continue
                m += cell[0]
                t += cell[1]
                r += cell[2]
                agg = cells[idx]
                agg[0] += cell[0]
                agg[1] += cell[1]
                agg[2] += cell[2]
            if m or t:
                active_days += 1
        day_keys.sort()
        result = {
            "slots": [{"m": c[0], "t": c[1], "r": c[2]} for c in cells],
            "days": len(day_keys),
            "window": days,
            "active_days": active_days,
            "day_keys": day_keys,
        }
        self._cache_put(key, result, now_ts)
        return result

    def _scan_file(self, path: Path, sid: str) -> Optional[list]:
        """把一个天文件的计数扫进 48 格；同一天的文件只解析一次。"""
        try:
            st = path.stat()
        except Exception:
            return None
        key = (str(path), round(float(st.st_mtime), 3), int(st.st_size), sid)
        hit = self._file_cache.get(key)
        if hit is not None:
            return hit
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        cells = [[0, 0, 0] for _ in range(SLOT_COUNT)]
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue  # 半行、坏行直接跳过，别让一条日志毁掉整天的统计
            if not isinstance(rec, dict):
                continue
            if sid and rec.get("sid") != sid:
                continue
            try:
                ts = float(rec.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                continue
            try:
                lt = time.localtime(ts)
            except (TypeError, ValueError, OSError, OverflowError):
                continue
            idx = (lt.tm_hour * 3600 + lt.tm_min * 60) // SLOT_SECONDS
            if not 0 <= idx < self.SLOT_COUNT:
                continue
            cell = cells[idx]
            cell[0] += self._weight(rec)
            if rec.get("result") == "round_end":
                cell[1] += 1
                if rec.get("replied"):
                    cell[2] += 1
        if len(self._file_cache) >= self.FILE_CACHE_LIMIT:
            self._file_cache.clear()
        self._file_cache[key] = cells
        return cells

    def _weight(self, rec: dict) -> int:
        """这条日志算几条消息——口径一律用 DecisionLog 的那个类方法。"""
        fn = getattr(self.log, "_message_weight", None)
        if callable(fn):
            try:
                return int(fn(rec) or 0)
            except Exception:
                return 0
        return 0 if rec.get("result") in EVENT_RESULTS else 1

    def _files_in_window(self, days: int, now: float) -> list:
        """窗口内真正存在的天文件，按日期升序。

        先按日期算出窗口里该有哪些天，再从目录尾部取一段名单筛，
        不遍历整个目录（这个目录会攒到五六十个文件）。
        """
        folder = getattr(self.log, "dir", None)
        if folder is None:
            return []
        try:
            folder = Path(folder)
        except Exception:
            return []
        if not folder.is_dir():
            return []
        wanted = set()
        for i in range(days):
            try:
                wanted.add(time.strftime("%Y-%m-%d", time.localtime(now - i * 86400.0)))
            except (TypeError, ValueError, OSError, OverflowError):
                continue
        if not wanted:
            return []
        try:
            candidates = sorted(folder.glob("*.jsonl"), reverse=True)[: max(days * 2, days + 8)]
        except Exception:
            return []
        picked = [p for p in candidates if p.stem in wanted]
        picked.sort(key=lambda p: p.stem)
        return picked

    @staticmethod
    def _weekday_of(day_key: str) -> Optional[int]:
        try:
            return int(time.strptime(day_key, "%Y-%m-%d").tm_wday)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _band_bounds(item: Any) -> tuple:
        """取出段的两端；字典、元组、列表都认，坏数据当整天空着。"""
        s = e = 0
        try:
            if isinstance(item, dict):
                s, e = item.get("s", 0), item.get("e", 0)
            else:
                s, e = item[0], item[1]
        except Exception:
            s = e = 0
        try:
            s = int(s)
        except (TypeError, ValueError):
            s = 0
        try:
            e = int(e)
        except (TypeError, ValueError):
            e = 0
        return s, e

    @staticmethod
    def _band_range(s: int, e: int) -> list:
        """段 [s, e) 覆盖的格号；e <= s 时按跨零点环形算。"""
        span = e - s if e > s else e - s + SLOT_COUNT
        span = max(1, min(int(span), SLOT_COUNT))
        return [(s + i) % SLOT_COUNT for i in range(span)]

    @classmethod
    def _empty_bands(cls, bands: Iterable[Any]) -> list:
        rows = []
        for item in (bands or []):
            s, e = cls._band_bounds(item)
            rows.append({"s": s, "e": e, "m": 0, "t": 0, "r": 0,
                         "m_day": 0.0, "t_day": 0.0, "r_day": 0.0,
                         "reply_rate": None, "msg_rate": None})
        return rows

    def _cache_get(self, key: tuple, now: float) -> Optional[dict]:
        hit = self._cache.get(key)
        if not hit:
            return None
        try:
            expire, payload = hit
        except Exception:
            self._cache.pop(key, None)
            return None
        if now >= expire:
            self._cache.pop(key, None)
            return None
        # 存的是汇总原始数据，取用时深拷贝，免得调用方改了把缓存带坏
        return copy.deepcopy(payload)

    def _cache_put(self, key: tuple, payload: dict, now: float) -> None:
        if len(self._cache) > 64:
            self._cache.clear()
        try:
            self._cache[key] = (now + self.CACHE_TTL, copy.deepcopy(payload))
        except Exception:
            pass

    def _now(self, now: Optional[float]) -> float:
        if now is not None:
            try:
                return float(now)
            except (TypeError, ValueError):
                pass
        try:
            return float(self._now_fn())
        except Exception:
            return time.time()

    def _norm_days(self, value: Any, default: int) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            days = default
        if days <= 0:
            days = default
        return max(1, min(days, self.MAX_WINDOW_DAYS))
