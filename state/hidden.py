"""隐藏会话：把不关心的群 / 私聊从日志页拿掉。

只影响展示，不删日志、不删状态。恢复入口在设置页。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.plugin import logger


class Hidden:
    """sid 集合。删除等于隐藏，日志文件原样保留。"""

    def __init__(self, data_dir, filename: str = "hidden.json"):
        self.path = Path(data_dir) / filename
        self._data: set = set()
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self._data = {str(x).strip() for x in raw if str(x).strip()}
        except Exception:
            logger.exception("[multi-decision] 读取隐藏会话失败（忽略，用空表）")
            self._data = set()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(sorted(self._data), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存隐藏会话失败")

    def all(self) -> list:
        return sorted(self._data)

    def contains(self, sid: str) -> bool:
        return str(sid or "").strip() in self._data

    def add(self, sid: str) -> list:
        sid = str(sid or "").strip()
        if sid:
            self._data.add(sid)
            self._save()
        return self.all()

    def remove(self, sid: str) -> list:
        sid = str(sid or "").strip()
        if sid and sid in self._data:
            self._data.discard(sid)
            self._save()
        return self.all()
