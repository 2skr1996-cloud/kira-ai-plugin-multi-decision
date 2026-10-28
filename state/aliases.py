"""会话别名：给群/私聊起个看得懂的名字。

框架的会话 id 是 `适配器名:类型:数字`（如 `桃华:gm:1145141974`），日志页上认不出是哪个群，
所以允许在页面上改个名字，存在插件数据目录的 aliases.json 里。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.plugin import logger


class Aliases:
    """sid -> 备注名。名字只影响展示，不影响任何判定。"""

    def __init__(self, data_dir, filename: str = "aliases.json"):
        self.path = Path(data_dir) / filename
        self._data: dict[str, str] = {}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {str(k): str(v) for k, v in raw.items() if v}
        except Exception:
            logger.exception("[multi-decision] 读取会话别名失败（忽略，用空表）")
            self._data = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存会话别名失败")

    def all(self) -> dict[str, str]:
        return dict(self._data)

    def get(self, sid: str, default: str = "") -> str:
        return self._data.get(str(sid), default)

    def set(self, sid: str, alias: str) -> dict[str, str]:
        sid = str(sid or "").strip()
        alias = str(alias or "").strip()
        if not sid:
            return self.all()
        if alias:
            self._data[sid] = alias
        else:
            self._data.pop(sid, None)
        self._save()
        return self.all()
