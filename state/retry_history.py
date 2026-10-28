"""Explicit retry chains, separate from immutable request input records."""
from __future__ import annotations

import json
from pathlib import Path

from core.logging_manager import get_logger
from .sids import normalize_sid

logger = get_logger("multi_decision", "orange")
RETRY_META = "_multi_decision_retry_v304"


class RetryHistory:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "retry_history.json"
        self.chains = {}
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

    def _save(self):
        try:
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
            "events": [str(root)], "tokens": {}, "current_event_id": str(root)})
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
