"""消息日志（原「决策日志」）：按天写 JSONL，只记统计要用的字段，不存正文。

v3.65（用户 m19238）：用户要求「只留统计要用的信息」。落盘的是
{ts, sid, result} 再加 reason / event_id / replied / failed / is_retry /
message_logged 里那些有值的；正文、发送者、媒体类型、计数、错误文本一律不写。
读侧只用这些（见 _message_weight / stats_recent / recent_round_rows /
hour_histogram），日志页也不再展示内容。历史文件已按同一口径重写
（2026-10-25 迁移，1.11 MB → 435 KB）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

#: v3.67（用户 m20117）：超份数不立刻删——刚写下的记录先留 5 分钟缓冲，
#: 之后被下一次触发的裁剪带走（免得刚发生的几轮还没看就从页面消失）。
PRUNE_GRACE_SECONDS = 300


class DecisionLog:
    """把每一次事件（收消息 / 触发 / 结算）写成一行 JSON，按天分文件。"""

    #: 按会话裁剪的节流秒数：写日志在热路径上，同一个会话最多 60 秒扫一次
    SID_TRIM_MIN_SECONDS = 60.0

    def __init__(self, data_dir, retention_days: int = 60, subdir: str = "decisions",
                 keep: int = 0, keep_of=None, days_of=None):
        self.dir = Path(data_dir) / subdir
        try:
            self.retention_days = max(1, int(retention_days or 60))
        except (TypeError, ValueError):
            self.retention_days = 60
        # 用户 m18059 第 3/4 条：窗口快照（windows_keep）与「按会话独立的天数」
        # 都落在这一层。keep=0 表示不分份数，只按天数；回调没传就退回实例值。
        try:
            self.keep = max(0, int(keep or 0))
        except (TypeError, ValueError):
            self.keep = 0
        self._keep_of = keep_of
        self._days_of = days_of
        self._last_sid_trim: dict[str, float] = {}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("[multi-decision] 无法创建消息日志目录")
        self._last_purge = 0.0

    def write(self, record: dict) -> None:
        try:
            ts = record.get("ts") or time.time()
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            path = self.dir / f"{day}.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("[multi-decision] 写消息日志失败")
            return
        # 追加已经成功，后面的清理再出错也不能让调用方以为这次没写进去。
        try:
            self._maybe_purge()
            self._trim_sid(str(record.get("sid") or ""))
        except Exception:
            logger.exception("[multi-decision] 写日志后的清理失败（本次写入已成功）")

    # ------------------------------------------------------------------ 读取

    def days(self, limit: int = 90) -> list:
        """有日志的日期（YYYY-MM-DD），倒序。日志页的日期下拉用。"""
        try:
            limit = max(1, min(int(limit or 90), 400))
        except (TypeError, ValueError):
            limit = 90
        try:
            files = sorted(self.dir.glob("*.jsonl"), reverse=True)
        except Exception:
            return []
        return [p.stem for p in files[:limit]]

    def read_recent(self, limit: int = 200, sid: str = "", result: str = "", day: str = "") -> list:
        """倒序返回最近的记录，供日志页读取。

        day 传 "YYYY-MM-DD" 只读那一天；传空读全部（按文件倒序）。
        """
        try:
            limit = max(1, min(int(limit or 200), 5000))
        except (TypeError, ValueError):
            limit = 200
        out = []
        if day:
            path = self.dir / f"{day}.jsonl"
            files = [path] if path.exists() else []
        else:
            try:
                files = sorted(self.dir.glob("*.jsonl"), reverse=True)
            except Exception:
                return out
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if sid and rec.get("sid") != sid:
                    continue
                if result and rec.get("result") != result:
                    continue
                out.append(rec)
                if len(out) >= limit:
                    return out
        return out

    def last_seen(self, days: int = 7) -> dict:
        """每个会话最近一条日志的 ts（用户 m19536 第 9 条：左栏按最近活跃排）。

        只倒着翻最近 days 天：更老的活跃时间对排序没意义，还能少读盘。
        静音（session_enabled=false）时引擎直接跳过、不写日志行，所以「静音
        不算活跃」是天然的——不用另加判断。

        结果缓存 30 秒：这个函数在每次打开首页时被调一次。
        """
        try:
            days = max(1, min(int(days or 7), 60))
        except (TypeError, ValueError):
            days = 7
        now = time.time()
        cache = getattr(self, "_last_seen_cache", None)
        if cache and cache[0] == days and now - cache[1] < 30:
            return dict(cache[2])
        out = {}
        try:
            files = sorted(self.dir.glob("*.jsonl"), reverse=True)[:days]
        except Exception:
            return out
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sid = str(rec.get("sid") or "")
                if not sid or sid in out:
                    continue
                try:
                    ts = float(rec.get("ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if ts > 0:
                    out[sid] = ts
        self._last_seen_cache = (days, now, dict(out))
        return out

    # 这些 result 是「事件」，不是「收到一条消息」。统计消息条数时要排除。
    EVENT_RESULTS = ("trigger", "round_end")
    # 用户 m00347：reason="wake" 是「触发」那一路（被叫到算一次普通触发），
    # 它同样代表真收到一条消息，不列进来今日消息条数就会少算。
    MESSAGE_TRIGGER_REASONS = frozenset(("mention", "wake_word", "nickname", "count", "private", "private_count", "wake"))

    @classmethod
    def _message_weight(cls, rec: dict) -> int:
        """Legacy triggers omitted the receipt row; new triggers explicitly mark it logged."""
        result = rec.get("result")
        if result == "trigger":
            return int(rec.get("message_logged") is not True
                       and rec.get("reason") in cls.MESSAGE_TRIGGER_REASONS)
        return int(result not in cls.EVENT_RESULTS)

    def stats_today(self) -> dict:
        """今天的统计，给概览卡片用。

        messages 才是「今天一共收到多少条群消息」（含被过滤掉的太短/连发/
        退避跳过），per_reply 就是用户要的「平均多少条消息回一次」。
        rounds 是「进了几次 agent」——两者不是一回事。
        """
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        # 直接读当天的文件：read_recent(3000) 只倒着取 3000 条，
        # 一天超过 3000 行（每轮 trigger+round_end 两条）今日统计就开始少算，
        # 越忙越不准（审查 bug）
        records = []
        try:
            day_path = self.dir / (time.strftime("%Y-%m-%d") + ".jsonl")
            if day_path.exists():
                for line in day_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if (rec.get("ts") or 0) >= start:
                        records.append(rec)
        except Exception:
            records = self.read_recent(3000)
        total = replied = rejected = triggered = messages = 0
        # v3.45（用户 2026-10-24 第 2 条）：卡片要能看见失败与重试，否则失败轮
        # 没写 round_end 时成功率会假成 100%。口径：failed = round_end 里 failed 为真；
        # retried = round_end 里 is_retry 为真（这一轮是重试产生的）。
        failed = retried = silent = 0
        by_session: dict = {}
        for rec in records:
            if (rec.get("ts") or 0) < start:
                break
            total += 1
            sid = rec.get("sid") or ""
            agg = by_session.setdefault(
                sid,
                {"total": 0, "messages": 0, "rounds": 0, "replied": 0, "rejected": 0,
                 "failed": 0, "retried": 0, "silent": 0},
            )
            agg["total"] += 1
            result = rec.get("result")
            if result == "round_end":
                triggered += 1
                agg["rounds"] += 1
                if rec.get("replied"):
                    replied += 1
                    agg["replied"] += 1
                else:
                    rejected += 1
                    agg["rejected"] += 1
                    # v3.47（用户 2026-10-24 第 2 条）：失败轮不是「不回复」。
                    # 真不回复 = 既没回复、也没失败；失败但有回复的轮已算进 replied，不重复算。
                    if not rec.get("failed"):
                        silent += 1
                        agg["silent"] += 1
                if rec.get("failed"):
                    failed += 1
                    agg["failed"] += 1
                if rec.get("is_retry"):
                    retried += 1
                    agg["retried"] += 1
            weight = self._message_weight(rec)
            messages += weight
            agg["messages"] += weight
        return self._pack(total, messages, triggered, replied, rejected, by_session,
                          failed=failed, retried=retried, silent=silent)

    @staticmethod
    def _pack(total, messages, rounds, replied, rejected, by_session,
              failed: int = 0, retried: int = 0, silent: int = 0) -> dict:
        """汇总成卡片要的形状。

        v3.45（用户 2026-10-24 第 2 条）：新增 failed（round_end 且 failed 为真，
        含看门狗强制释放、异常终止）与 retried（round_end 且 is_retry 为真）。
        rounds 仍是 round_end 的条数，含义不变。
        """
        return {
            "total": total,
            "messages": messages,
            "rounds": rounds,
            "replied": replied,
            "rejected": rejected,
            "failed": failed,
            "retried": retried,
            # v3.47：真不回复（既没 replied 也没 failed），failed 轮绝不落进这里
            "silent": silent,
            "per_reply": round(messages / replied, 1) if replied > 0 else None,
            "by_session": by_session,
        }

    def _window_records(self, start: float) -> list:
        """滚动窗口内的原始记录（跨零点时把昨天那个文件也读进来）。

        stats_recent 和 recent_round_rows 共用这一份读盘逻辑：按天切文件意味着
        只看今天会漏掉前一天的量（0 点一过数字就归零的老问题）。
        """
        now = time.time()
        records = []
        try:
            days = sorted({
                time.strftime("%Y-%m-%d", time.localtime(now)),
                time.strftime("%Y-%m-%d", time.localtime(start)),
            })
            for day in days:
                day_path = self.dir / (day + ".jsonl")
                if not day_path.exists():
                    continue
                for line in day_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if (rec.get("ts") or 0) >= start:
                        records.append(rec)
        except Exception:
            return [r for r in self.read_recent(3000) if (r.get("ts") or 0) >= start]
        return records

    def stats_recent(self, hours: int = 24) -> dict:
        """最近 N 小时的统计（v3.19）。

        概览卡片从「今天」改成「最近 24 小时」：按自然日切的话，0 点一过
        昨天的量全掉，用户看到的数字会莫名其妙归零。聚合口径和 stats_today 完全一致，
        只是起点换成滚动窗口；跨零点时要把昨天那个文件也读进来。
        """
        start = time.time() - max(1, int(hours or 24)) * 3600
        # 读盘这段抽成 _window_records：24 小时合并口径（state/round_stats.py）
        # 也要同一份「跨零点的两天」，别复制第二份读盘实现。
        records = self._window_records(start)
        total = replied = rejected = triggered = messages = 0
        # v3.45（用户 2026-10-24 第 2 条）：口径与 stats_today 保持一致，
        # failed = round_end 且 failed 为真，retried = round_end 且 is_retry 为真。
        failed = retried = silent = 0
        by_session: dict = {}
        # v3.20（用户 m41961）：卡片副行要分「群聊 / 私聊」。会话 id 归一化后是
        # gm:<群号> / dm:<对方 id>（state/sids.py），按前缀归堆即可。
        by_kind = {
            "groups": {"messages": 0, "rounds": 0, "replied": 0, "failed": 0, "retried": 0,
                       "silent": 0},
            "privates": {"messages": 0, "rounds": 0, "replied": 0, "failed": 0, "retried": 0,
                         "silent": 0},
        }
        for rec in records:
            if (rec.get("ts") or 0) < start:
                continue
            total += 1
            sid = rec.get("sid") or ""
            agg = by_session.setdefault(
                sid,
                {"total": 0, "messages": 0, "rounds": 0, "replied": 0, "rejected": 0,
                 "failed": 0, "retried": 0, "silent": 0},
            )
            agg["total"] += 1
            kind = None
            if sid.startswith("gm:"):
                kind = by_kind["groups"]
            elif sid.startswith("dm:"):
                kind = by_kind["privates"]
            if rec.get("result") == "round_end":
                triggered += 1
                agg["rounds"] += 1
                if kind is not None:
                    kind["rounds"] += 1
                if rec.get("replied"):
                    replied += 1
                    agg["replied"] += 1
                    if kind is not None:
                        kind["replied"] += 1
                else:
                    rejected += 1
                    agg["rejected"] += 1
                    # v3.47（用户 2026-10-24 第 2 条）：失败轮不是「不回复」。
                    # 真不回复 = 既没回复、也没失败；失败但有回复的轮已算进 replied，不重复算。
                    if not rec.get("failed"):
                        silent += 1
                        agg["silent"] += 1
                        if kind is not None:
                            kind["silent"] += 1
                if rec.get("failed"):
                    failed += 1
                    agg["failed"] += 1
                    if kind is not None:
                        kind["failed"] += 1
                if rec.get("is_retry"):
                    retried += 1
                    agg["retried"] += 1
                    if kind is not None:
                        kind["retried"] += 1
            weight = self._message_weight(rec)
            messages += weight
            agg["messages"] += weight
            if kind is not None:
                kind["messages"] += weight
        out = self._pack(total, messages, triggered, replied, rejected, by_session,
                         failed=failed, retried=retried, silent=silent)
        out["hours"] = max(1, int(hours or 24))
        out["groups"] = by_kind["groups"]
        out["privates"] = by_kind["privates"]
        return out

    def recent_round_rows(self, hours: int = 24) -> list:
        """最近 N 小时里每一轮结算（round_end）的极简形状，只读，给合并口径用。

        v3.48（用户 2026-10-24 第 1 条）：概览卡顶上一排数字要的是「最近 24 小时
        真发生了多少轮」，不能一个数吃请求记录（被 keep=100 截断）、另一个数吃
        消息日志（只写结算成功的轮）。state/round_stats.merge_rounds 把这里的
        round_end 与请求记录/请求账本按 event_id 合起来，同一轮只算一次。

        只取 result == "round_end"；窗口按滚动的 hours 小时算，跨零点会把
        昨天的文件一起读进来（_window_records）。
        """
        try:
            hours = max(1, int(hours or 24))
        except (TypeError, ValueError):
            hours = 24
        start = time.time() - hours * 3600
        out = []
        for rec in self._window_records(start):
            if rec.get("result") != "round_end":
                continue
            try:
                ts = float(rec.get("ts") or 0.0)
            except (TypeError, ValueError):
                continue
            if ts < start:
                continue
            out.append({
                "event_id": str(rec.get("event_id") or ""),
                "sid": str(rec.get("sid") or ""),
                "ts": ts,
                "replied": bool(rec.get("replied")),
                "failed": bool(rec.get("failed")),
                "is_retry": bool(rec.get("is_retry")),
            })
        return out

    def hour_histogram(self, days: int = 7, sid: str = "", only_groups: bool = False) -> dict:
        """最近 N 天每小时收到多少条消息（不含触发/结算事件）。

        休息时段体检用：把「休息时段」压到这张 24 小时分布上，
        就能看出用户是不是把休息设在了群里最热闹的那几个小时。
        """
        try:
            days = max(1, min(int(days or 7), 60))
        except (TypeError, ValueError):
            days = 7
        now = time.time()
        hours = [0] * 24
        # v3.28：时段曲线按 30 分钟一格，48 格；hours 继续留给休息时段体检用
        slots = [0] * 48
        # v3.75（用户本轮第 2 条）：时段曲线要多画一条「bot 回复」——
        # 用户消息在 slots，bot 的每一次成功回复（round_end 且 replied）单独累计到这里，
        # 同 30 分钟一格、同 7 天窗口，两条线的尺子一样才比得出来。
        bot_slots = [0] * 48
        bot_total = 0
        total = 0
        for i in range(days):
            day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            path = self.dir / f"{day}.jsonl"
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
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
                if sid and rec.get("sid") != sid:
                    continue
                # v3.31（用户 m48323 第 9 条）：时段曲线只统计群聊，私聊不掺进来
                if only_groups and not str(rec.get("sid") or "").startswith("gm:"):
                    continue
                weight = self._message_weight(rec)
                try:
                    lt = time.localtime(float(rec.get("ts") or 0))
                except (TypeError, ValueError, OSError):
                    continue
                slot = lt.tm_hour * 2 + (1 if lt.tm_min >= 30 else 0)
                if not weight:
                    # 结算行不是「收到一条消息」，但 replied 的那一轮就是 bot 回了一次
                    if str(rec.get("result") or "") == "round_end" and rec.get("replied"):
                        bot_slots[slot] += 1
                        bot_total += 1
                    continue
                hours[lt.tm_hour] += weight
                slots[slot] += weight
                total += weight
        return {"hours": hours, "slots": slots, "total": total,
                "bot_slots": bot_slots, "bot_total": bot_total, "days": days}

    def usage(self) -> dict:
        """按天文件的真实占用：条数 = 非空行数，不是文件数（用户 m13708 第 5 条）。

        消息日志/窗口快照一天一个 .jsonl、多会话混写，一个群一整天的记录也
        只在同一个文件里。缓存页以前数的是文件个数，于是「一个群那么多快照
        只有 5 份」——其实是「只有 5 天有记录」。这里按行数算才和用户看到的
        条数对得上，bytes 供缓存页显示占用。
        """
        count = 0
        size = 0
        try:
            files = list(self.dir.glob("*.jsonl"))
        except Exception:
            files = []
        for path in files:
            try:
                size += int(path.stat().st_size or 0)
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if line.strip():
                            count += 1
            except Exception:
                continue
        return {"count": count, "bytes": size, "mb": round(size / 1048576.0, 2)}

    def clear_all(self) -> int:
        """删掉全部按天日志，返回删掉的记录条数（用户 m13708 第 4 条）。

        缓存设置页的「清空」：整个目录的 .jsonl 一起删，不看会话。
        """
        removed = int(self.usage().get("count") or 0)
        try:
            files = list(self.dir.glob("*.jsonl"))
        except Exception:
            return 0
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        return removed

    def purge_session(self, sid: str) -> int:
        """删掉某个会话的全部记录，返回删除条数。

        日志按天一个文件、多会话混写，只能逐行筛掉再整体重写；
        筛空的文件直接删掉，否则日期下拉里会留下空的一天。
        """
        sid = str(sid or "").strip()
        if not sid:
            return 0
        try:
            files = sorted(self.dir.glob("*.jsonl"))
        except Exception:
            return 0
        removed = 0
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            keep = []
            hit = 0
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    keep.append(line)
                    continue
                if str(rec.get("sid") or "") == sid:
                    hit += 1
                else:
                    keep.append(line)
            if not hit:
                continue
            removed += hit
            try:
                if keep:
                    tmp = path.with_suffix(".jsonl.tmp")
                    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
                    tmp.replace(path)
                else:
                    path.unlink()
            except Exception:
                logger.exception(f"[multi-decision] 清理会话日志失败：{path}")
        return removed

    def keep_for(self, sid: str = "") -> int:
        """这个会话该留几份记录；0 = 不限（用户 m18059 第 3/4 条）。"""
        if self._keep_of is not None:
            try:
                return max(0, int(self._keep_of(sid) or 0))
            except Exception:
                logger.debug("[multi-decision] 取会话保留份数失败", exc_info=True)
        return self.keep

    def days_for(self, sid: str = "") -> int:
        """这个会话该留多少天的记录；0 = 不限。口径同 keep_for。"""
        if self._days_of is not None:
            try:
                return max(0, int(self._days_of(sid) or 0))
            except Exception:
                logger.debug("[multi-decision] 取会话保留天数失败", exc_info=True)
        return self.retention_days

    def _trim_sid(self, sid: str) -> None:
        """按会话裁这一个 sid 的记录（用户 m18059 第 3 条：窗口快照份数上限）。

        日志按天一个 .jsonl、多会话混写，份数上限只能逐行算：从最新的一天往回
        数这个会话的行，超过 keep 份或早于 days 天的删掉，别人的记录一个字节不动。
        超过份数的行有 5 分钟宽限（PRUNE_GRACE_SECONDS），刚写下的不会立刻消失。
        keep<=0 且天数跟全局一样时直接跳过——全局的 _maybe_purge 已经管了。
        """
        sid = str(sid or "").strip()
        if not sid:
            return
        keep = self.keep_for(sid)
        days = self.days_for(sid)
        if keep <= 0 and (days <= 0 or days == self.retention_days):
            return
        now = time.time()
        last = self._last_sid_trim.get(sid, 0.0)
        if last and now - last < self.SID_TRIM_MIN_SECONDS:
            return
        self._last_sid_trim[sid] = now
        cutoff = now - days * 86400 if days > 0 else 0.0
        try:
            files = sorted(self.dir.glob("*.jsonl"), reverse=True)
        except Exception:
            return
        seen = 0
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            keep_lines: list[str] = []
            dropped = 0
            # 同一天的文件里越靠后越新：从尾部往回数这个会话的行
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    keep_lines.append(line)
                    continue
                # 合法 JSON 但不是对象（[]、null、数字）或 ts 读不成数字：这行看不懂，
                # 原样留着当证据，不让它把整次清理打断、也不替用户删掉。
                if not isinstance(rec, dict):
                    keep_lines.append(line)
                    continue
                if str(rec.get("sid") or "") != sid:
                    keep_lines.append(line)
                    continue
                try:
                    ts_rec = float(rec.get("ts") or 0.0)
                except (TypeError, ValueError):
                    keep_lines.append(line)
                    continue
                seen += 1
                too_old = bool(cutoff) and ts_rec < cutoff
                # v3.67（用户 m20117）：超份数的先看时间——5 分钟内新写下的留着，
                # 下一轮裁剪再删；超过保留天数的照旧立即删。
                too_many = (
                    bool(keep) and seen > keep
                    and ts_rec < now - PRUNE_GRACE_SECONDS
                )
                if too_old or too_many:
                    dropped += 1
                    continue
                keep_lines.append(line)
            if not dropped:
                continue
            keep_lines.reverse()
            try:
                if keep_lines:
                    tmp = path.with_suffix(".jsonl.tmp")
                    tmp.write_text("\n".join(keep_lines) + "\n", encoding="utf-8")
                    tmp.replace(path)
                else:
                    path.unlink(missing_ok=True)
            except Exception:
                logger.exception(f"[multi-decision] 按会话裁剪日志失败：{path}")

    def _maybe_purge(self) -> None:
        now = time.time()
        if now - self._last_purge < 3600:
            return
        self._last_purge = now
        cutoff = now - self.retention_days * 86400
        try:
            for path in self.dir.glob("*.jsonl"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
        except Exception:
            logger.exception("[multi-decision] 清理过期日志失败")
