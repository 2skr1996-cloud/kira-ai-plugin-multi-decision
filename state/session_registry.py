"""新会话登记簿：老会话照旧，新冒出来的会话默认关掉插件。

用户 m45332：「如果 bot 接收到新的私聊或者群聊消息，也就是被陌生人拉进了新群，
或者新的用户私聊，进入左侧列表后默认关闭功能，避免 bot 被骚扰浪费 token」。

这里的做法是记一份「见过哪些会话」（sessions_seen.json）：
插件初始化时把盘上现有的会话全部登记成老会话（它们保持用户原来的开关），
之后再第一次见到的 sid 才判为新会话、由调用方把 session_enabled 写 False。
机器人改名会让 sid 换前缀，所以存的都是归一化后的 sid（gm:号码）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable, Set

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")


class SessionRegistry:
    """记得见过的会话，用第一个出现那一刻判断「新」还是「旧」。"""

    def __init__(self, data_dir, filename: str = "sessions_seen.json"):
        self.path = Path(data_dir) / filename
        self.known: Set[str] = set()
        # 没有登记文件时（第一次升级到这个版本）不能把现存会话全当新的，
        # 否则用户开着的那几个群会突然集体静音——靠这个标记走「先登记一遍」。
        self.bootstrapped = False
        self.load()

    # ------------------------------------------------------------------ 读写

    def load(self) -> Set[str]:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.known = {str(x) for x in (raw.get("sids") or []) if x}
                    self.bootstrapped = bool(raw.get("bootstrapped"))
                elif isinstance(raw, list):  # 兼容只有 sid 列表的旧格式
                    self.known = {str(x) for x in raw if x}
                    self.bootstrapped = True
        except Exception:
            logger.exception("[multi-decision] 读取会话登记簿失败，按空处理")
            self.known = set()
            self.bootstrapped = False
        return self.known

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "bootstrapped": self.bootstrapped,
                        "updated": time.time(),
                        "sids": sorted(self.known),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[multi-decision] 保存会话登记簿失败")

    # ------------------------------------------------------------------ 判定

    def bootstrap(self, sids: Iterable[str]) -> int:
        """把现存会话全登记成老会话，返回这次新登记了几个。"""
        added = 0
        for sid in sids or []:
            sid = str(sid or "")
            if sid and sid not in self.known:
                self.known.add(sid)
                added += 1
        if added or not self.bootstrapped:
            self.bootstrapped = True
            self.save()
        return added

    def is_known(self, sid: str) -> bool:
        return str(sid or "") in self.known

    def remember(self, sid: str) -> bool:
        """登记一个会话。返回 True 表示它是第一次出现（调用方应默认关掉）。"""
        sid = str(sid or "")
        if not sid or sid in self.known:
            return False
        self.known.add(sid)
        self.save()
        return True
