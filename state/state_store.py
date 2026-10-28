"""会话状态的落盘与恢复。

热重载插件（改配置、点快捷开关、保存设置页）会重建插件实例，内存里的
SessionState 全部清空。表现出来就是「攒到 9 条的计数突然回到 1」——
用户会以为计数坏了。

这里把可恢复字段存进 sessions.json，重载后接上。写入做节流，
避免每条消息都碰一次磁盘。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.plugin import logger


class StateStore:
    """sid -> 可恢复字段。空表时也会写出一个 {}，方便排查。"""

    def __init__(self, data_dir, filename: str = "sessions.json", save_interval: float = 3.0):
        self.path = Path(data_dir) / filename
        try:
            self.save_interval = max(0.5, float(save_interval))
        except (TypeError, ValueError):
            self.save_interval = 3.0
        self._last_save = 0.0
        self._dirty = False

    # ------------------------------------------------------------------ 读

    def load(self) -> dict:
        try:
            if not self.path.exists():
                return {}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return {}
            out = {}
            for sid, data in raw.items():
                sid = str(sid or "").strip()
                if sid and isinstance(data, dict):
                    out[sid] = data
            return out
        except Exception:
            logger.exception("[multi-decision] 读取会话状态失败（忽略，按空表启动）")
            return {}

    # ------------------------------------------------------------------ 写

    def save(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
            self._last_save = time.time()
            self._dirty = False
        except Exception:
            logger.exception("[multi-decision] 保存会话状态失败")

    def save_throttled(self, data: dict) -> None:
        """按 save_interval 节流；被节流掉的写会在下次调用时补上。"""
        self._dirty = True
        now = time.time()
        if now - self._last_save < self.save_interval:
            return
        self.save(data)

    def flush(self, data: dict) -> None:
        """关闭前强制写一次。"""
        if self._dirty or not self.path.exists():
            self.save(data)

    def drop(self, sid: str, data: dict = None) -> bool:
        """把某个会话从 sessions.json 里去掉（删会话时用）。

        能拿到内存全表就传进来（data 参数）。自己 load() 再改再写回是错的：
        save_throttled 有 3 秒节流，别的会话刚改的计数还在内存里没落盘，
        拿磁盘旧表改完写回去会把它们一起盖掉，而且 _dirty 被 save() 置 False，
        之后不补写就永久丢了（审查 bug）。
        """
        sid = str(sid or "").strip()
        if not sid:
            return False
        table = dict(data) if isinstance(data, dict) else self.load()
        if sid not in table:
            return False
        table.pop(sid, None)
        self.save(table)
        return True

    @property
    def dirty(self) -> bool:
        return self._dirty
