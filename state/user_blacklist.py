"""用户黑名单：被拉黑的 QQ 号，他的新消息不再进请求上下文（用户 m06193）。

和「短语黑名单」不是一回事：短语黑名单按整条消息的文字比对，只影响触发判定；
这里按发言人（QQ 号）拦，命中的消息**根本不进请求**——决策上下文、回复上下文、
待发都剔掉，但窗口页照常显示（默认折叠，点开还能看）。

存 data/plugin_data/<id>/user_blacklist.json，不写框架的插件配置：设置页保存时
会把框架内存里没有的键覆盖掉，这类「运行时清单」放自己目录更稳。

结构：
    {"items": [{"uid": "123", "name": "某某", "ts": 1794..., "keep": ["mid", ...]}]}

keep 是「拉黑那一刻已经在窗口里的消息 id」。用户 m06193 的口径是只剔除拉黑之后
的新消息，历史照留，所以过滤时按 message_id 白名单放行；没有 message_id 的消息
一律放行（宁可多留一条，也别把历史整段删掉）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.plugin import logger


class UserBlacklist:
    """uid -> {name, ts, keep}。只影响请求上下文，不影响页面显示。"""

    def __init__(self, data_dir, filename: str = "user_blacklist.json"):
        self.path = Path(data_dir) / filename
        self._items: list[dict] = []
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                items = raw.get("items") if isinstance(raw, dict) else raw
                if isinstance(items, list):
                    out = []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        uid = str(it.get("uid") or "").strip()
                        if not uid:
                            continue
                        keep = it.get("keep")
                        out.append({
                            "uid": uid,
                            "name": str(it.get("name") or "").strip(),
                            "ts": float(it.get("ts") or 0) or 0.0,
                            "keep": [str(x) for x in (keep or []) if str(x).strip()],
                        })
                    self._items = out
        except Exception:
            logger.exception("[multi-decision] 读取用户黑名单失败（忽略，用空表）")
            self._items = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps({"items": self._items}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存用户黑名单失败")

    # ------------------------------------------------------------------ 读
    def items(self) -> list[dict]:
        return [dict(it) for it in self._items]

    def uids(self) -> set[str]:
        return {str(it.get("uid") or "") for it in self._items if it.get("uid")}

    def get(self, uid: str) -> dict | None:
        uid = str(uid or "").strip()
        for it in self._items:
            if str(it.get("uid") or "") == uid:
                return dict(it)
        return None

    def keep_of(self, uid: str) -> set[str]:
        it = self.get(uid)
        return set(it.get("keep") or []) if it else set()

    # ------------------------------------------------------------------ 写
    def add(self, uid: str, name: str = "", keep=None) -> dict:
        """拉黑一个 uid。已经在表里就只补名字 / 并 keep，不重置时间。"""
        uid = str(uid or "").strip()
        if not uid:
            return {}
        keep = [str(x) for x in (keep or []) if str(x).strip()]
        cur = self.get(uid)
        if cur:
            cur["name"] = str(name or "").strip() or cur.get("name") or ""
            merged = list(dict.fromkeys(list(cur.get("keep") or []) + keep))
            cur["keep"] = merged
            self._items = [cur if str(it.get("uid")) == uid else it for it in self._items]
        else:
            cur = {
                "uid": uid,
                "name": str(name or "").strip(),
                "ts": time.time(),
                "keep": list(dict.fromkeys(keep)),
            }
            self._items.append(cur)
        self._save()
        return dict(cur)

    def remove(self, uid: str) -> bool:
        uid = str(uid or "").strip()
        before = len(self._items)
        self._items = [it for it in self._items if str(it.get("uid") or "") != uid]
        if len(self._items) == before:
            return False
        self._save()
        return True
