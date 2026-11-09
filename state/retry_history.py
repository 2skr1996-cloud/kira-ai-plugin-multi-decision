"""Explicit retry chains, separate from immutable request input records."""
from __future__ import annotations

import json
import time
from pathlib import Path

from core.logging_manager import get_logger
from .sids import normalize_sid

logger = get_logger("multi_decision", "orange")
RETRY_META = "_multi_decision_retry_v304"


class RetryHistory:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "retry_history.json"
        self.chains = {}
        # v4.54（用户 m18227）：每个会话保留多少条重试链，由插件注入；None = 不限。
        self._limit_fn = None
        # v4.55（用户 m19940）：每个会话保留多少天，由插件注入；None = 不按天清理。
        self._days_fn = None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") == 1 and isinstance(data.get("chains"), dict):
                for sid, roots in data["chains"].items():
                    if not isinstance(roots, dict):
                        continue
                    valid = {}
                    for root, rec in roots.items():
                        if (not isinstance(rec, dict) or rec.get("root_event_id") != root
                                or not isinstance(rec.get("events"), list)
                                or not isinstance(rec.get("tokens"), dict)
                                or type(rec.get("attempts")) is not int
                                or rec["attempts"] < 0 or rec.get("had_error") is not True
                                or rec.get("status") not in ("scheduled", "running",
                                    "succeeded", "failed", "cancelled", "not_retried", "unknown")
                                or "succeeded" not in rec):
                            continue
                        valid[root] = rec
                    if valid:
                        self.chains[normalize_sid(sid)] = valid
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("[multi-decision] 读取重试历史失败")
        # A restart cannot establish the result of an in-flight request.
        for roots in self.chains.values():
            for rec in roots.values():
                if rec.get("status") in ("scheduled", "running"):
                    rec["status"] = "unknown"
                    rec["succeeded"] = None

    def set_limit_fn(self, fn):
        """注入「这个会话留多少条链」的回调；传 None 恢复不限。"""
        self._limit_fn = fn

    def set_keep_days_fn(self, fn):
        """注入「这个会话留多少天」的回调；传 None 不按天清理。"""
        self._days_fn = fn

    def _trim(self):
        """按天数 / 条数裁一遍；返回是否真的删过东西。"""
        fn = self._limit_fn
        days_fn = self._days_fn
        if not callable(fn) and not callable(days_fn):
            return False
        changed = False
        for sid in list(self.chains.keys()):
            roots = self.chains.get(sid) or {}
            protected = {root for root, rec in roots.items()
                         if isinstance(rec, dict)
                         and rec.get("status") in ("scheduled", "running")}
            if callable(days_fn):
                try:
                    days = int(days_fn(sid) or 0)
                except Exception:
                    days = 0
                if days > 0:
                    now = time.time()
                    for root in list(roots.keys()):
                        if root in protected:
                            continue
                        rec = roots.get(root)
                        if not isinstance(rec, dict):
                            continue
                        ts = rec.get("created_at")
                        if not isinstance(ts, (int, float)):
                            # 老记录没有时间戳：从首次读到开始算一个完整周期。
                            rec["created_at"] = int(now)
                            changed = True
                            continue
                        if now - float(ts) > days * 86400:
                            roots.pop(root, None)
                            changed = True
            if not callable(fn):
                if not roots:
                    self.chains.pop(sid, None)
                continue
            try:
                limit = int(fn(sid) or 0)
            except Exception:
                limit = 0
            if not roots:
                self.chains.pop(sid, None)
                continue
            if limit <= 0 or len(roots) <= limit:
                continue
            # 正在重试的链先保住，再按插入顺序丢最旧的，留下的就是最近的那几条。
            for root in list(roots.keys()):
                if len(roots) <= limit:
                    break
                if root in protected:
                    continue
                roots.pop(root, None)
                changed = True
            if not roots:
                self.chains.pop(sid, None)
        return changed

    def clear(self, cutoff=None) -> int:
        """清重试链，返回删掉的链数（用户 m21482）。

        cutoff 为 None 清全部；给了 cutoff 就只删 created_at 早于它的链。
        正在重试 / 已排队的链先保住，避免清掉还在跑的状态。
        没有 created_at 的旧链在按天清理时保留。
        """
        removed = 0
        for sid in list(self.chains.keys()):
            roots = self.chains.get(sid) or {}
            for root in list(roots.keys()):
                rec = roots.get(root)
                if not isinstance(rec, dict):
                    continue
                if rec.get("status") in ("scheduled", "running"):
                    continue
                if cutoff is not None:
                    ts = rec.get("created_at")
                    if not isinstance(ts, (int, float)):
                        continue
                    if float(ts) >= float(cutoff):
                        continue
                roots.pop(root, None)
                removed += 1
            if not roots:
                self.chains.pop(sid, None)
        if removed:
            self._save()
        return removed

    def prune(self):
        """按当前设置清一次；有变化才落盘。缓存页 / 重启后都会走这里。"""
        try:
            if self._trim():
                self._save()
        except Exception:
            logger.exception("[multi-decision] 清理重试历史失败")

    def stats(self, sid: str = ""):
        """重试链条数 / 事件数 / 占用；sid 非空时只算这个会话（用户 m22090）。"""
        self.prune()
        chains = events = 0
        by_sid = {}
        for owner, roots in (self.chains or {}).items():
            if not isinstance(roots, dict):
                continue
            by_sid[owner] = len(roots)
            chains += len(roots)
            for rec in roots.values():
                if isinstance(rec, dict) and isinstance(rec.get("events"), list):
                    events += len(rec["events"])
        target = normalize_sid(str(sid or "").strip()) if sid else ""
        if target:
            roots = self.chains.get(target) or {}
            if not isinstance(roots, dict):
                roots = {}
            subset_events = sum(len(rec.get("events") or [])
                                for rec in roots.values() if isinstance(rec, dict))
            return {"count": len(roots), "events": subset_events,
                    "bytes": len(json.dumps({"version": 1, "chains": {target: roots}},
                                            ensure_ascii=False).encode("utf-8")),
                    "sids": by_sid}
        try:
            size = int(self.path.stat().st_size or 0)
        except Exception:
            size = 0
        return {"count": chains, "events": events, "bytes": size, "sids": by_sid}

    def _save(self):
        try:
            self._trim()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"version": 1, "chains": self.chains},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存重试历史失败")

    def _get(self, sid, root):
        return self.chains.get(normalize_sid(sid), {}).get(str(root))

    def error(self, sid, root, event_id):
        if not root or not event_id:
            return
        roots = self.chains.setdefault(normalize_sid(sid), {})
        rec = roots.setdefault(str(root), {"root_event_id": str(root), "had_error": True,
            "attempts": 0, "status": "not_retried", "succeeded": False,
            "events": [str(root)], "tokens": {}, "current_event_id": str(root),
            "created_at": int(time.time())})
        rec.setdefault("created_at", int(time.time()))
        if str(event_id) not in rec["events"]:
            return
        rec["had_error"] = True
        self._save()

    def status(self, sid, root, status):
        rec = self._get(sid, root)
        if rec is None or rec["status"] in ("succeeded", "cancelled"):
            return
        if status == "cancelled" and rec["status"] not in ("scheduled", "running"):
            return  # Starting a fresh request must not replace a known terminal failure.
        rec["status"] = status
        rec["succeeded"] = (True if status == "succeeded" else False
                              if status in ("failed", "not_retried") else None)
        self._save()

    def start(self, sid, root, token):
        rec = self._get(sid, root)
        if rec is None or rec["status"] not in ("scheduled", "running"):
            return False
        if token not in rec["tokens"]:
            rec["tokens"][token] = ""
            rec["attempts"] += 1
            rec["current_event_id"] = ""
            rec["status"] = "running"
            rec["succeeded"] = None
            self._save()
        return True

    def bind(self, sid, root, token, event_id):
        rec = self._get(sid, root)
        if not event_id or rec is None or rec["status"] != "running":
            return False
        if token not in rec["tokens"] or rec["tokens"][token] not in ("", str(event_id)):
            return False
        # Only the most recently started automatic attempt may take ownership.
        if token != next(reversed(rec["tokens"]), None):
            return False
        rec["tokens"][token] = str(event_id)
        rec["current_event_id"] = str(event_id)
        if str(event_id) not in rec["events"]:
            rec["events"].append(str(event_id))
        self._save()
        return True

    def finish(self, sid, event_id, failed):
        for root, rec in self.chains.get(normalize_sid(sid), {}).items():
            if event_id != rec.get("current_event_id"):
                continue
            if rec["status"] in ("cancelled", "succeeded"):
                return
            if not failed:
                self.status(sid, root, "succeeded")
            return

    def purge(self, sid):
        removed = self.chains.pop(normalize_sid(sid), None)
        if removed is not None:
            self._save()
        return len(removed or {})

    def summary(self, sid, event_id):
        if not event_id:
            return None
        for rec in self.chains.get(normalize_sid(sid), {}).values():
            if str(event_id) in rec.get("events", []):
                return {key: rec[key] for key in ("root_event_id", "had_error",
                    "attempts", "status", "succeeded")}
        return None

    # -------------------------------------------------- 概览卡用：重试轮一览（v3.46）

    def retry_events(self) -> set:
        """所有链路里「重试轮」的 event_id 集合。

        events[0] 永远是最初那轮（轮本身出了问题才建链），events[1:] 才是重试出来的轮；
        概览卡要回答的正是「这一轮算不算重试」，所以只取尾巴那几段，原轮不算。
        没数据（或文件坏掉在 __init__ 里被跳过）就返回空 set，
        调用方直接当空集合用，不用判 None。
        """
        out = set()
        try:
            for roots in (self.chains or {}).values():
                if not isinstance(roots, dict):
                    continue
                for rec in roots.values():
                    if not isinstance(rec, dict):
                        continue
                    events = rec.get("events")
                    if not isinstance(events, list):
                        continue
                    for event_id in events[1:]:
                        if event_id:
                            out.add(str(event_id))
        except Exception:
            logger.exception("[multi-decision] 统计重试事件失败")
            return out
        return out
