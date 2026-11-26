"""私聊昵称缓存（用户 m19461 第 5 条）。

私聊消息宿主不给昵称：`messages.sender_name` 是空串，框架原文里
`[user_nickname: , user_id: 512179630]` 昵称位空着，插件只能退回 QQ 号，
页面上整条消息的发送者就显示成一串数字。

这里把「问适配器要到的昵称」按 QQ 号存下来，落盘在 plugin_data 下的
nicknames.json：下次拼原文时直接用，重启后也还在。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

#: 最多记这么多人，超了按最久没更新的淘汰。
MAX_NAMES = 2000

#: 昵称最长留这么多字（QQ 昵称本身不会太长，防脏数据撑爆页面）。
NAME_MAX = 64


class NickNames:
    """QQ 号 → 昵称的缓存，落盘在 plugin_data 下的 nicknames.json。"""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "nicknames.json"
        self._data = self._load()

    def _load(self):
        if not self.path.is_file():
            return {"version": 1, "names": {}}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(got, dict) or not isinstance(got.get("names"), dict):
                raise ValueError("根节点不是对象")
            return got
        except Exception:
            logger.exception("[multi-decision] 私聊昵称缓存读取失败，按空缓存继续：%s", self.path)
            return {"version": 1, "names": {}}

    def get(self, uid):
        """这个号记着的昵称，没有就空串。"""
        names = self._data.get("names")
        item = names.get(str(uid or "").strip()) if isinstance(names, dict) else None
        if not isinstance(item, dict):
            return ""
        return str(item.get("name") or "").strip()

    def set(self, uid, name):
        """记一个昵称，返回是否真的变了（没变就不写盘）。"""
        uid = str(uid or "").strip()
        name = str(name or "").strip()[:NAME_MAX]
        if not uid or not name or name == uid:
            return False
        names = self._data.get("names")
        if not isinstance(names, dict):
            names = self._data["names"] = {}
        old = names.get(uid)
        if isinstance(old, dict) and str(old.get("name") or "") == name:
            old["at"] = time.time()
            return False
        names[uid] = {"name": name, "at": time.time()}
        self._prune(names)
        self.save()
        return True

    def _prune(self, names):
        if len(names) <= MAX_NAMES:
            return
        rows = sorted(names.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
        for key, _ in rows[:len(names) - MAX_NAMES]:
            names.pop(key, None)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[multi-decision] 私聊昵称缓存保存失败：%s", self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
