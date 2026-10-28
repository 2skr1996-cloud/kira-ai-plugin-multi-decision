"""会话级运行时状态。

一个 sid 一份。计数器、退避、累计统计这些会落盘（见 state_store.py），
热重载插件后接着算；忙碌标记、抖动计时器、队列这类瞬时状态不落盘。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SessionState:
    sid: str

    # —— 计数 ——
    counter: int = 0
    last_sender_id: str = ""
    last_msg_ts: float = 0.0

    # —— 抖动（触发后等安静 S 秒）——
    debounce_deadline: float = 0.0
    debounce_task: Optional[object] = None
    # v3.51（用户 m07385 第 7 条）：触发那一刻的墙钟。请求用时要算「触发 → 请求结束」
    # 整段（含前面的防抖等待，不含回复吐字），_execute 取走后清零；不落盘。
    trigger_wall: float = 0.0
    # 用户 m05427：抖动的口径是「固定 S 秒；这 S 秒里又攒够一次触发，就再叠 S 秒」，
    # 所以这一轮第一次触发的时刻、已经叠上的秒数都要记着（deadline = 两者之和）。
    debounce_first: float = 0.0
    debounce_jitter: float = 0.0
    # 用户 m05453：抖动 / 决策 / 回复是三段，这一格只放「抖动真的等了几秒」。
    debounce_spent: float = 0.0

    # —— 队列与忙碌 ——
    queued: bool = False
    busy: bool = False
    busy_since: float = 0.0
    # 用户 m05124 第 5 条：本轮已经被触发过（正在抖动等，或已经派活）的时间戳。
    # 这段时间进来的消息按「运行期间」拦进缓冲，不能补进这一轮；0 表示没有正在跑的一轮。
    round_claimed: float = 0.0

    # —— 退避 ——
    reject_streak: int = 0
    backoff_until: float = 0.0

    # —— 出错与自动重试（v2.71）——
    last_error: str = ""          # 最近一次请求失败的摘要
    last_error_at: float = 0.0
    retry_at: float = 0.0         # 0 表示没有排着的重试
    retry_count: int = 0          # 本轮已重试几次，结算成功清零

    # —— 最近一次「没触发」的原因（v2.82）——
    # counting / too_short / burst_merged / rest_skip / backoff_skip / quota_skip / muted
    # 页面上直接显示成「攒数中 8/10」「太短跳过」，用户不用再猜是不是卡死
    last_mark: str = ""
    last_mark_ts: float = 0.0

    # —— 自适应 ——
    adaptive_n: int = 0        # 0 表示还没算过，用 100/target 兜底

    # —— 静默触发 ——
    last_silent_ts: float = 0.0  # 上次静默冒泡的时间，防止反复触发
    # v4.28（用户 m22969）：兜底开关打开的那一刻。群已经安静一小时的会话一开开关，
    # 以前倒计时立刻是「静默 0 秒」并马上冒泡，现在从打开那一刻重新数。
    silent_armed_ts: float = 0.0
    # 用户 m11091 第 1 条：冒泡过就在左栏标签上打个勾，这样一眼能看出「这次安静
    # 到底补没补过」。两套兜底分开记：静默兜底与休息兜底计时不一样。
    # 只在收到新消息时清（_on_message 里），倒计时归零不算用过。
    silent_fired_ts: float = 0.0
    rest_fired_ts: float = 0.0

    # —— 配额（按自然小时滚动）——
    hour_key: str = ""         # "2026-10-18-21"，换小时就清零
    hour_replies: int = 0      # 本小时已回复轮数
    hour_messages: int = 0     # 本小时收到的计数消息数（只做统计，配额不再按占比算）

    # —— 配额（按自然日滚动，v3.52 用户 m08226 第 5 条）——
    # 用户把配额玩法收敛成「每小时 / 每天」两个开关，两个都能单独封顶。
    day_key: str = ""          # "2026-10-18"，跨天就清零
    day_replies: int = 0       # 今天已回复轮数

    # —— 统计 ——
    total_messages: int = 0
    total_triggers: int = 0
    total_replies: int = 0
    last_trigger_ts: float = 0.0
    last_trigger_reason: str = ""
    last_trigger_count: int = 0
    # 触发那一刻实际用的概率（0-100，只有概率/曲线口径有值）。用户 m15045：
    # 请求页要把「这轮是掷到多少点才中的」写出来，否则概率模式的触发只显示
    # 一个光秃秃的条数，看不出跟设定的 10% 有没有关系。
    last_trigger_prob: float = 0.0
    last_reply_ts: float = 0.0

    def roll_hour(self, now: float) -> None:
        """跨小时就把配额计数器清零。"""
        import time as _time

        key = _time.strftime("%Y-%m-%d-%H", _time.localtime(now))
        if key != self.hour_key:
            self.hour_key = key
            self.hour_replies = 0
            self.hour_messages = 0

    def roll_day(self, now: float) -> None:
        """跨天就把按天配额清零（0 点重置，跟人类直觉一致）。"""
        import time as _time

        key = _time.strftime("%Y-%m-%d", _time.localtime(now))
        if key != self.day_key:
            self.day_key = key
            self.day_replies = 0

    def is_idle(self) -> bool:
        """真空闲：没有 agent 在跑、队列为空、没有抖动计时器。"""
        return (not self.busy) and (not self.queued) and self.debounce_deadline <= 0

    # —— 持久化 ——
    #
    # 只存「重载后还想接着算」的字段。busy / queued / debounce_deadline /
    # debounce_task 是瞬时状态，恢复成「还在忙」会让插件永远不干活。

    PERSIST_FIELDS = (
        "counter",
        "last_sender_id",
        "last_msg_ts",
        "reject_streak",
        "backoff_until",
        "adaptive_n",
        "last_silent_ts",
        "silent_fired_ts",
        "rest_fired_ts",
        "hour_key",
        "hour_replies",
        "hour_messages",
        "day_key",
        "day_replies",
        "total_messages",
        "total_triggers",
        "total_replies",
        "last_trigger_ts",
        "last_trigger_reason",
        "last_trigger_count",
        "last_trigger_prob",
        "last_reply_ts",
    )

    def to_dict(self) -> dict:
        out = {}
        for name in self.PERSIST_FIELDS:
            value = getattr(self, name, None)
            if isinstance(value, (int, float, str, bool)) or value is None:
                out[name] = value
        return out

    @classmethod
    def from_dict(cls, sid: str, data: dict) -> "SessionState":
        st = cls(sid=sid)
        if not isinstance(data, dict):
            return st
        for name in cls.PERSIST_FIELDS:
            if name not in data:
                continue
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            # 按字段自己的类型收。sessions.json 被手工改过或旧版本类型变过
            # （"counter": "12"）时，字符串进了 int 字段，之后 counter += 1
            # 抛 TypeError 被外层 except 吞掉 —— 这个会话从此不计数、也没日志（审查 bug）
            cur = getattr(st, name, None)
            try:
                if isinstance(cur, bool):
                    value = bool(value)
                elif isinstance(cur, int):
                    value = int(float(value))
                elif isinstance(cur, float):
                    value = float(value)
                elif isinstance(cur, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            try:
                setattr(st, name, value)
            except Exception:
                pass
        return st

    def snapshot(self) -> dict:
        return {
            "sid": self.sid,
            "counter": self.counter,
            "busy": self.busy,
            "queued": self.queued,
            "reject_streak": self.reject_streak,
            "backoff_until": self.backoff_until,
            "total_messages": self.total_messages,
            "total_triggers": self.total_triggers,
            "total_replies": self.total_replies,
            "adaptive_n": self.adaptive_n,
            "last_silent_ts": self.last_silent_ts,
            "silent_fired_ts": self.silent_fired_ts,
            "rest_fired_ts": self.rest_fired_ts,
            "hour_replies": self.hour_replies,
            "hour_messages": self.hour_messages,
            "day_replies": self.day_replies,
            "last_trigger_reason": self.last_trigger_reason,
            "last_trigger_count": self.last_trigger_count,
            "last_trigger_prob": self.last_trigger_prob,
            # 瞬时字段也带上：/overview 的会话卡片靠它显示「忙了多久 / 是否在重试 /
            # 上次报什么错」，不然页面只能看到忙不忙（审查 bug）
            "busy_since": self.busy_since,
            "retry_at": self.retry_at,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }
