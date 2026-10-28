"""按会话覆盖配置：某个群想单独设阈值/节奏，就存在这里。

只存「与全局不同」的键，其余跟随全局；全局改了，没覆盖的项自动跟着变。
落盘 plugin_data/<plugin_id>/overrides.json，结构 {sid: {key: value}}。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")


class Overrides:
    """每个会话一份配置补丁。"""

    def __init__(self, data_dir, filename: str = "overrides.json"):
        self.path = Path(data_dir) / filename
        self._data: dict[str, dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> dict:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {
                        str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)
                    }
        except Exception:
            logger.exception("[multi-decision] 读取会话配置覆盖失败，按空处理")
            self._data = {}
        return self._data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[multi-decision] 保存会话配置覆盖失败")

    # ------------------------------------------------------------------ 查询

    def all(self) -> dict:
        return {sid: dict(cfg) for sid, cfg in self._data.items()}

    def get(self, sid: str) -> dict:
        return dict(self._data.get(str(sid)) or {})

    def has(self, sid: str) -> bool:
        return bool(self._data.get(str(sid)))

    def value(self, sid: str, key: str, default=None):
        cfg = self._data.get(str(sid))
        if cfg and key in cfg:
            return cfg[key]
        return default

    # ------------------------------------------------------------------ 写入

    def set(self, sid: str, values: dict) -> dict:
        """整份替换某个会话的覆盖；values 为空等于恢复跟随全局。"""
        sid = str(sid)
        clean = {str(k): v for k, v in (values or {}).items()}
        if clean:
            self._data[sid] = clean
        else:
            self._data.pop(sid, None)
        self._save()
        return self.get(sid)

    def clear(self, sid: str) -> None:
        self._data.pop(str(sid), None)
        self._save()

    def clear_all(self) -> None:
        self._data.clear()
        self._save()
