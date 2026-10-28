"""最近 24 小时的结算口径：消息日志与请求记录合并成一份（v3.48）。

为什么要有这个模块（用户 2026-10-24 第 1 条）：
概览卡顶上一排数字原来来自两个不同来源，谁都不全，于是出现
「群聊 177 次 / 回复 90 次」这种两个口径混排的场面——

* 消息日志 decisions/*.jsonl 的 round_end 只记「结算过」的轮：看门狗强制释放、
  请求异常终止的那些轮压根没有这一行，所以那边 failed 恒 0、replied 恒等于轮数；
* 请求记录 requests/<sid>/*.json 每会话只留 keep（默认 100）份，24 小时跑过
  100 轮之后最前面的全被裁掉，rounds 和 replied 都被截在这个上限上。

这里把两边按「同一轮」的身份合起来：key 优先 event_id，缺失时退化成
(sid, int(ts))；同 key 取并集——replied / failed 任一方为真即为真，
只有请求侧存在的轮（看门狗、异常终止，没有 round_end）也算进轮数与失败。
silent 只留给「既没回复、也没失败」的轮：失败轮绝不算不回复。

只做合并与计数，不读盘、不改任何状态：调用方把
DecisionLog.recent_round_rows(24) 与 RequestLog.recent_round_rows(24) 递进来。
"""

from __future__ import annotations

# 每一档计数共用的字段名。「messages」不在这里：合并口径只管轮次，消息条数仍归消息日志。
_COUNTERS = ("rounds", "replied", "failed", "retried", "silent")


def _blank() -> dict:
    """一档空计数（总数 / 某会话 / 群聊 / 私聊 都用同一个形状）。"""
    return {name: 0 for name in _COUNTERS}


def round_key(row: dict) -> tuple:
    """同一轮的身份：有事件号用事件号，没有就用 (sid, 秒级时刻)。

    秒级够用：一轮最少也要几秒，同一会话不可能同一秒开两轮。做成公开函数是给
    测试和排查用的（想核对某两行是不是同一轮，直接比这个 key）。
    """
    sid = str((row or {}).get("sid") or "")
    event_id = str((row or {}).get("event_id") or "")
    if event_id:
        return (sid, event_id)
    try:
        return (sid, "ts:%d" % int(float((row or {}).get("ts") or 0.0)))
    except (TypeError, ValueError):
        return (sid, "ts:0")


def _absorb(rounds: dict, row) -> None:
    """把一行（消息日志的 round_end 或请求记录的一轮）并进 rounds[key]。"""
    if not isinstance(row, dict):
        return
    key = round_key(row)
    cur = rounds.get(key)
    if cur is None:
        cur = {"sid": "", "ts": 0.0, "replied": False, "failed": False, "retried": False}
        rounds[key] = cur
    sid = str(row.get("sid") or "")
    if sid and not cur["sid"]:
        cur["sid"] = sid
    try:
        ts = float(row.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    # 消息日志的 ts 是结算时刻、请求记录的是发起时刻（实测差十几秒），
    # 取早的那个当这一轮的时刻，不影响计数，只是看着顺。
    if ts and (not cur["ts"] or ts < cur["ts"]):
        cur["ts"] = ts
    cur["replied"] = cur["replied"] or bool(row.get("replied"))
    cur["failed"] = cur["failed"] or bool(row.get("failed"))
    # 决策侧字段叫 is_retry，请求侧叫 retried，两边都认
    cur["retried"] = cur["retried"] or bool(row.get("is_retry") or row.get("retried"))


def merge_rounds(decision_rows, request_rows) -> dict:
    """把两边的轮次合并成一份 24 小时口径。

    返回 {
        "rounds":      并集后的轮数（只有请求侧存在的看门狗轮也算）
        "replied":     至少一方说回复过的轮数
        "failed":      至少一方说失败的轮数（含失败但有回复的轮）
        "retried":     重试出来的轮数（决策 is_retry 或请求 retried）
        "silent":      既没回复也没失败的轮数（失败轮绝不落这里）
        "success_rate": replied / rounds * 100，一位小数；没有轮次给 None
        "by_kind":     {"groups": {...}, "privates": {...}}（按 sid 前缀 gm:/dm: 分堆）
        "by_session":  {sid: {...}}，形状同上面的计数
    }

    容错：入参不是 dict 的行直接跳过，绝不抛——这是给 5 秒轮询的 /overview 用的。
    """
    rounds: dict = {}
    for row in decision_rows or ():
        _absorb(rounds, row)
    for row in request_rows or ():
        _absorb(rounds, row)
    out = _blank()
    out["success_rate"] = None
    by_session: dict = {}
    by_kind = {"groups": _blank(), "privates": _blank()}
    for cur in rounds.values():
        sid = cur["sid"]
        replied = bool(cur["replied"])
        failed = bool(cur["failed"])
        # 失败轮不是「不回复」：只有既没回复也没失败才算真不回复
        silent = (not replied) and (not failed)
        buckets = [out, by_session.setdefault(sid, _blank())]
        if sid.startswith("gm:"):
            buckets.append(by_kind["groups"])
        elif sid.startswith("dm:"):
            buckets.append(by_kind["privates"])
        for agg in buckets:
            agg["rounds"] += 1
            if replied:
                agg["replied"] += 1
            if failed:
                agg["failed"] += 1
            if cur["retried"]:
                agg["retried"] += 1
            if silent:
                agg["silent"] += 1
    out["by_kind"] = by_kind
    out["by_session"] = by_session
    if out["rounds"]:
        out["success_rate"] = round(out["replied"] * 100.0 / out["rounds"], 1)
    return out
