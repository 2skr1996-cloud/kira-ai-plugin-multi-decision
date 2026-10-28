"""自适应触发的统计底座。

只做两件事：按天记「收到多少条 / 回了几次」，再据此反推阈值 N。
落盘在 data/stats/<sid>.json，跨重启保留；只留最近 window_days 天。

设计口径（与框架文档 3.4 一致）：
- 回复率 = 回复次数 / 收到的消息条数，目标是 adaptive_target_percent%。
- 观测天数不足 advanced_min_days 时不做高级调整，退回基础公式 100/target。
- 调整一次最多 adaptive_step，夹在 [1, 200]，避免抖动。

按星期分桶（可选，默认关）：
- 周末群里人多、工作日晚上才热闹，混在一起算平均会把两种作息抹平。
- 开启后额外统计「同一个星期几」的回复率，样本够就优先用它，不够退回全窗口。
- 因此数据保留天数要拉长（weekday_window_days 默认 56 天），否则每个星期几只有 1 天样本。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _safe_sid(sid: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(sid)) or "unknown"


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class AdaptiveStats:
    def __init__(self, data_dir, window_days: int = 7, min_days: int = 30,
                 save_interval: float = 60.0, weekday_bucket: bool = False,
                 weekday_window_days: int = 56, keep_days: int = 0):
        self.dir = Path(data_dir) / "stats"
        self.window_days = max(1, int(window_days))
        self.min_days = max(0, int(min_days))
        self.weekday_bucket = bool(weekday_bucket)
        self.weekday_window_days = max(7, int(weekday_window_days))
        # 落盘保留天数：分桶时要留住 8 周，否则每个星期几只剩 1 天样本
        self.retain_days = (
            max(self.window_days, self.weekday_window_days)
            if self.weekday_bucket else self.window_days
        )
        self.save_interval = max(5.0, float(save_interval))
        # 用户 m13708 第 4 条：整个会话统计文件（每个群聊/私聊一份）的保留天数。
        # 0 = 不限（默认，行为和以前一样，不会突然删数据）。
        try:
            self.keep_days = max(0, int(keep_days or 0))
        except (TypeError, ValueError):
            self.keep_days = 0
        self._last_file_prune = 0.0
        self._data: dict[str, dict[str, dict]] = {}
        self._dirty: set[str] = set()
        self._last_save = 0.0
        self._closed = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ 落盘

    def _path(self, sid: str) -> Path:
        return self.dir / f"{_safe_sid(sid)}.json"

    def _bucket(self, sid: str) -> dict:
        days = self._data.get(sid)
        if days is None:
            days = {}
            path = self._path(sid)
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        days = {k: v for k, v in raw.items() if isinstance(v, dict)}
                except Exception:
                    days = {}
            self._data[sid] = days
        return days

    def _touch(self, sid: str) -> None:
        self._dirty.add(sid)
        now = time.time()
        if now - self._last_save >= self.save_interval:
            self.flush()

    def flush(self) -> None:
        # 顺手按保留天数剪旧会话文件（用户 m13708 第 4 条）；keep_days=0 时是空操作
        self.prune_files()
        if self._closed or not self._dirty:
            self._last_save = time.time()
            return
        for sid in list(self._dirty):
            days = self._data.get(sid)
            if days is None:
                continue
            self._prune(days)
            path = self._path(sid)
            tmp = path.with_suffix(".json.tmp")
            try:
                tmp.write_text(
                    json.dumps(days, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                os.replace(tmp, path)
            except Exception:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
        self._dirty.clear()
        self._last_save = time.time()

    def shutdown(self) -> None:
        self.flush()
        self._closed = True

    def _prune(self, days: dict) -> None:
        if len(days) <= self.retain_days:
            return
        keep = sorted(days.keys())[-self.retain_days:]
        for key in list(days.keys()):
            if key not in keep:
                days.pop(key, None)

    def prune_files(self, force: bool = False) -> int:
        """按 stats_keep_days 删掉长期没动的会话统计文件，返回删了几个（用户 m13708 第 4 条）。

        口径：
        - 只删 stats/<sid>.json（每个群聊/私聊一份的自适应账本）；
          **不动** requests.jsonl / tokens.jsonl 这两本用量账——页面上「24 小时
          请求数 / token 数」靠它们，剪统计文件不该让这些数字归零。
        - 按文件 mtime 算「多久没动过」，keep_days=0 表示不限，什么都不做。
        - 每小时内最多扫一次目录，避免每个群里每句话都去 listdir；
          force=True 跳过这个闸门（插件初始化时用一次）。
        内存里已经载入的会话跳过不删，删了下次写盘又会被重建，白删。
        """
        if self.keep_days <= 0:
            return 0
        now = time.time()
        if not force and (now - self._last_file_prune) < 3600:
            return 0
        self._last_file_prune = now
        cutoff = now - self.keep_days * 86400
        live = {_safe_sid(k) for k in self._data}
        removed = 0
        try:
            files = list(self.dir.glob("*.json"))
        except Exception:
            return 0
        for path in files:
            try:
                if path.stem in live:
                    continue
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------------ 记录

    def record_message(self, sid: str, ts: float = None) -> None:
        ts = time.time() if ts is None else ts
        days = self._bucket(sid)
        day = days.setdefault(_day_key(ts), {"m": 0, "r": 0, "t": 0})
        day["m"] = int(day.get("m", 0)) + 1
        self._touch(sid)

    def record_round(self, sid: str, replied: bool, ts: float = None) -> None:
        ts = time.time() if ts is None else ts
        days = self._bucket(sid)
        day = days.setdefault(_day_key(ts), {"m": 0, "r": 0, "t": 0})
        day["t"] = int(day.get("t", 0)) + 1
        if replied:
            day["r"] = int(day.get("r", 0)) + 1
        self._touch(sid)

    # ------------------------------------------------------------------ 读数

    def summary(self, sid: str, now: float = None, weekday: int = None,
                days_span: int = None) -> dict:
        """窗口内的汇总：消息数 / 回复次数 / 回合数 / 有数据的天数 / 回复率。

        weekday 不为 None 时只统计该星期几（周一=0），days_span 指定回看多少天，
        默认用配置的 stats_window_days。
        """
        now = time.time() if now is None else now
        days = self._bucket(sid)
        span = max(1, int(days_span if days_span is not None else self.window_days))
        cutoff = now - span * 86400
        messages = replies = rounds = 0
        active_days = 0
        for key, day in days.items():
            try:
                day_ts = time.mktime(time.strptime(key, "%Y-%m-%d"))
            except Exception:
                continue
            if day_ts + 86400 < cutoff:
                continue
            if weekday is not None and time.localtime(day_ts).tm_wday != int(weekday):
                continue
            m = int(day.get("m", 0))
            r = int(day.get("r", 0))
            t = int(day.get("t", 0))
            messages += m
            replies += r
            rounds += t
            if m or t:
                active_days += 1
        rate = (replies / messages * 100.0) if messages else 0.0
        return {
            "messages": messages,
            "replies": replies,
            "rounds": rounds,
            "active_days": active_days,
            "reply_rate": round(rate, 2),
            "span_days": span,
            "weekday": weekday,
        }

    def suggest_threshold(self, sid: str, target_percent: int, current: int,
                          step: int, now: float = None) -> int:
        """按实际回复率把阈值 N 往目标推一格，返回新 N。"""
        target = max(1, min(100, int(target_percent)))
        base = max(1, round(100.0 / target))
        stats = self.summary(sid, now)
        source = "all"
        # 按星期分桶：同一个星期几攒够样本才用，否则退回全窗口
        if self.weekday_bucket:
            now_ts = time.time() if now is None else now
            wd = time.localtime(now_ts).tm_wday
            bucket = self.summary(
                sid, now, weekday=wd, days_span=self.weekday_window_days
            )
            min_bucket_days = max(2, self.min_days // 7)
            if bucket["active_days"] >= min_bucket_days and bucket["messages"] >= 20:
                stats = bucket
                source = "weekday"
        # 样本不足：不猜，直接用基础公式。分桶分支的样本门槛已在上面校验过
        if source != "weekday":
            if stats["active_days"] < self.min_days or stats["messages"] < 20:
                return base
        cur = max(1, int(current or base))
        rate = stats["reply_rate"]
        step = max(1, int(step))
        if rate < target - 2:
            cur -= step          # 回得太少 → 更容易触发
        elif rate > target + 2:
            cur += step          # 回得太多 → 更难触发
        return max(1, min(200, cur))
