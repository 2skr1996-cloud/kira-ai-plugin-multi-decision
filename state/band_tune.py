"""动态调整：按每个时段的平均回复数据，自动微调那一段的触发概率。

用户 m23596 第 5/8 条：目标不再是「全局回复率」，而是每个时段各自的回复次数 /
回复概率。观察量全部来自 state/band_stats.py（按段、按天的平均值）：

    M = 消息条数/天、T = 触发轮数/天、R = 回复次数/天
    q = R / T                      每轮触发里真回出去的比例（拒绝回复会压低它）
    R* = 目标回复次数/天          设了 tr 就用 tr；设了 tp 就用 tp/100*M；都设取小的
    p* = R* / (M * q) * 100        让「每天回 R* 次」成立需要的触发概率

每次结算只朝 p* 挪一格（最多 adaptive_step 个百分点），样本不足 / 冷却中 / 这一段
没设目标就不动。调过的概率存在 data/band_tune.json，**不写回配置**——用户在设置页
看到的「配置概率」永远是原值，调过的那份只用于判定与展示（「配置 X% → 现在 Y%」）。

冷却默认 10 分钟（settings 里的「调参间隔」可改）：一轮一调会在几轮里被随机波动带着
跑，慢一点更稳（用户要「更精细算法」，稳比快重要）。

v3.84（用户 m04084，三条取舍落地）：
  1. 按星期分桶——开了「使用星期数据」后，调参结果存在「段@周几」上（键加 @wd 后缀），
     周一调的不会覆盖周二那份；找不到当天那份时回落到全周共用那份。
  2. 手动接管——用户在段上亲手改了概率，这一段就标成 manual，下一轮不再往老目标拉回；
     想恢复自动调整，改这一段的「次数/天」或「概率/条」目标即可（目标一变就解锁）。
  3. 调参机会——判定时就调一次（engine._tune_tick），不再只能等「触发并结算」，
     冷却改由 cooldown 参数传入（main.py 的 adaptive_tune_minutes）。
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# 同一段两次调整之间至少隔这么久（秒）
COOLDOWN_SECONDS = 600.0
# 样本下限：这一段的日均消息太少、或日均触发轮数太少，就先不调
MIN_MESSAGES_PER_DAY = 20.0
MIN_ROUNDS_PER_DAY = 3.0
# 每轮回复率的下限：接上决策 AI 之后拒绝回复会很多，q 太小会让 p* 飞到天上
MIN_REPLY_RATE = 0.05


class BandTuner:
    """按段自动调概率。engine 每次结算调一次 observe()，判定时读 prob()。"""

    def __init__(self, path, stats, now_fn=None):
        self.path = str(path)
        self.stats = stats          # BandStats 实例（只读它的 band_stats()）
        self.now_fn = now_fn or time.time
        # {sid: {"12-20": {"p": 12.5, "v": 20, "ts": 1792920000.0}}}
        self._data: dict = {}
        self._load()

    # ---------------------------------------------------------------- 落盘
    @staticmethod
    def _key(start, end, wd=None) -> str:
        # v3.84（用户 m04084 第 1 条）：带 @周几 的是「那一个星期」的调参结果，
        # 不带的是全周共用那份（老数据、或没开星期数据时用的）。
        base = "%d-%d" % (int(start), int(end))
        if wd is None:
            return base
        return "%s@%d" % (base, int(wd) % 7)

    def _rec(self, sid: str, start, end, weekday=None):
        """这一段的调参记录：(命中的键, 记录)；没有就 (None, None)。

        开了星期数据时先找「段@今天」，找不到再回落到全周那份——某一周还没调过、
        或者刚打开星期数据的时候，老结果还能接着用。
        """
        bucket = self._data.get(str(sid or "")) or {}
        keys = []
        if weekday is not None:
            keys.append(self._key(start, end, weekday))
        keys.append(self._key(start, end))
        for key in keys:
            rec = bucket.get(key)
            if isinstance(rec, dict):
                return key, rec
        return None, None

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._data = {}
        except Exception:
            logger.exception("[multi-decision] 读动态调整状态失败，按空的处理")
            self._data = {}

    def _save(self) -> None:
        try:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[multi-decision] 写动态调整状态失败（已忽略）")

    # ---------------------------------------------------------------- 读取
    def prob(self, sid: str, start, end, v=None, weekday=None):
        """这一段现在该用多少概率（%）。没调过、被手动接管、或配置值被改过 → None（用配置值）。

        v 传了配置里的概率时，会比对调参时记下的原值：用户一旦在设置页改了这一段，
        旧的调参结果就作废（否则改了配置页面上还显示老数字，用户会以为没保存）。

        v3.84（用户 m04084 第 2 条）：作废的同时把这一段标成 manual——他亲手改的概率
        就是他要的值，下一轮不再往老目标拉回去。想恢复自动调整，改目标（次数/天 或
        概率/条）即可，observe() 看到目标变了会把 manual 摘掉。
        """
        try:
            key, rec = self._rec(sid, start, end, weekday)
            if not isinstance(rec, dict):
                return None
            p = rec.get("p")
            if p is None:
                return None
            if v is not None and abs(float(rec.get("v", -1)) - float(v)) > 0.01:
                if not rec.get("manual") and key:
                    rec = dict(rec)
                    rec.update({"p": float(v), "v": float(v), "manual": True,
                                "ts": float(self.now_fn())})
                    self._data.setdefault(str(sid or ""), {})[key] = rec
                    self._save()
                    logger.info(
                        "[multi-decision] %s %02d:%02d 段被手工改成 %.1f%%，暂停自动调整"
                        "（改这一段的次数/概率目标可恢复）",
                        str(sid or ""), int(start) // 2, (int(start) % 2) * 30, float(v),
                    )
                return None
            if rec.get("manual"):
                return None
            return max(1.0, min(100.0, float(p)))
        except Exception:
            return None

    def is_manual(self, sid: str, start, end, v=None, weekday=None) -> bool:
        """这一段是不是被用户亲手改过概率、已经暂停自动调了（页面显示「手动」用）。"""
        try:
            _, rec = self._rec(sid, start, end, weekday)
            if not isinstance(rec, dict) or not rec.get("manual"):
                return False
            if v is not None and abs(float(rec.get("v", -1)) - float(v)) > 0.01:
                return False
            return True
        except Exception:
            return False

    def snapshot(self, sid: str) -> dict:
        """给页面用：{("12-20"): 当前调过的概率}（只有调过的段）。"""
        out = {}
        for key, rec in (self._data.get(str(sid or "")) or {}).items():
            try:
                out[key] = float(rec.get("p"))
            except (TypeError, ValueError):
                continue
        return out

    # ---------------------------------------------------------------- 调整
    def observe(self, sid, bands, sig, replied, step=2, weekday_bucket=False,
                weekday_window_days=56, week_days=7, min_days=0, cooldown=None):
        """按这一段的目标挪一格概率。返回新概率，没动就回 None。

        replied 只是「这一轮回没回」，不直接参与计算——目标看的是这一段最近几天的
        平均数据（一轮的随机性太大），这里留着是为了日志与将来做即时修正。

        v3.84（用户 m04084 第 3 条）：不再只由「触发结算」调用，判定时也会调一次
        （engine._tune_tick），所以频次要靠 cooldown 挡——不传就用默认 COOLDOWN_SECONDS。
        """
        if not bands or not sig:
            return None
        try:
            start, end = int(sig[0]), int(sig[1])
        except (TypeError, ValueError, IndexError):
            return None
        band = None
        for it in bands:
            if it[0] == start and it[1] == end:
                band = it
                break
        if band is None:
            return None
        v = float(band[2])
        try:
            want_replies = int(band[3] or 0)
        except (TypeError, ValueError):
            want_replies = 0
        try:
            want_rate = float(band[4] or 0)
        except (TypeError, ValueError):
            want_rate = 0.0
        if want_replies <= 0 and want_rate <= 0:
            return None                     # 这一段没设目标，不自动调
        if self.stats is None:
            return None
        now = float(self.now_fn())
        sid_key = str(sid or "")
        bucket = self._data.setdefault(sid_key, {})
        # v3.84（用户 m04084 第 1 条）：开了星期数据就按今天周几分开存，每天各调各的
        wd_use = None
        if weekday_bucket:
            try:
                wd_use = int(time.localtime(now).tm_wday)
            except Exception:
                wd_use = None
        key = self._key(start, end, wd_use)
        old_rec = bucket.get(key) or {}
        # v3.84（用户 m04084 第 2 条）：这一段被用户手动接管了就不再自动调；
        # 目标（次数/天、概率/条）一改就当作「重新授权自动调整」，下面照常走。
        try:
            same_goal = (abs(float(old_rec.get("tr", -1) or 0) - float(want_replies)) <= 0.001
                         and abs(float(old_rec.get("tp", -1) or 0) - float(want_rate)) <= 0.001)
        except (TypeError, ValueError):
            same_goal = False
        if old_rec:
            if old_rec.get("manual") and same_goal:
                return None                 # 手动接管中，别拉回去
            try:
                if abs(float(old_rec.get("v", -1)) - v) > 0.01:
                    # 配置里的概率被改过 → 用户接管这一段，标成手动，这一轮不调
                    bucket[key] = {"p": v, "v": round(v, 1), "manual": True,
                                   "tr": want_replies, "tp": want_rate,
                                   "ts": float(old_rec.get("ts") or 0)}
                    self._save()
                    return None
            except (TypeError, ValueError):
                pass
        try:
            cap = COOLDOWN_SECONDS if cooldown is None else float(cooldown)
            if old_rec and now - float(old_rec.get("ts") or 0) < cap:
                return None                 # 冷却中
        except (TypeError, ValueError):
            pass

        try:
            data = self.stats.band_stats(
                sid,
                bands,
                weekday_bucket=bool(weekday_bucket),
                weekday_window_days=int(weekday_window_days or 56),
                week_days=int(week_days or 7),
                now=now,
            )
        except Exception:
            logger.exception("[multi-decision] 按时段统计失败，这轮不调概率")
            return None
        if not isinstance(data, dict):
            return None
        days = int(data.get("days") or 0)
        if min_days and days < int(min_days):
            return None                     # 观察天数还不够（用户可设 0 = 有数据就调）
        row = None
        for r in data.get("bands") or []:
            try:
                if int(r.get("s")) == start and int(r.get("e")) == end:
                    row = r
                    break
            except (TypeError, ValueError):
                continue
        if row is None:
            return None
        try:
            m_day = float(row.get("m_day") or 0)
            t_day = float(row.get("t_day") or 0)
            r_day = float(row.get("r_day") or 0)
        except (TypeError, ValueError):
            return None
        if m_day < MIN_MESSAGES_PER_DAY or t_day < MIN_ROUNDS_PER_DAY:
            return None                     # 样本太少，调了也是噪声
        q = (r_day / t_day) if t_day > 0 else 0.0
        if q < MIN_REPLY_RATE:
            q = MIN_REPLY_RATE
        wants = []
        if want_replies > 0:
            wants.append(float(want_replies))
        if want_rate > 0:
            wants.append(want_rate / 100.0 * m_day)
        if not wants:
            return None
        target_replies = min(wants)
        p_star = target_replies / (max(1.0, m_day) * q) * 100.0
        p_star = max(1.0, min(100.0, p_star))
        p_old = self.prob(sid, start, end, v=v, weekday=wd_use)
        if p_old is None:
            p_old = v
        step = max(1, int(step or 1))
        delta = max(-step, min(step, p_star - p_old))
        if abs(delta) < 0.05:
            return None                     # 已经到位，不写盘
        p_new = round(max(1.0, min(100.0, p_old + delta)), 1)
        bucket[key] = {"p": p_new, "v": round(v, 1), "ts": now,
                       "manual": False, "tr": want_replies, "tp": want_rate}
        self._save()
        logger.info(
            "[multi-decision] %s 动态调整 %02d:%02d 段 %.1f%% → %.1f%%"
            "（目标 %s 次/天、q=%.2f、M=%.1f）",
            sid_key, start // 2, (start % 2) * 30, p_old, p_new,
            ("%.1f" % target_replies), q, m_day,
        )
        return p_new

    def forget(self, sid: str) -> None:
        """删会话/清记录时把这一份调参结果也扔掉。"""
        if self._data.pop(str(sid or ""), None) is not None:
            self._save()

    def clear_all(self) -> None:
        self._data = {}
        self._save()
