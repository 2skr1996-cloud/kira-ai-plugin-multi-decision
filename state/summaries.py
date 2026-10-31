"""中期摘要的落盘存储：每个会话一段文本。

摘要必须持久化，否则插件一重载、历史就断档（窗口本体是内存的，
可以靠框架历史重建，摘要重建不了）。
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from pathlib import Path

from core.plugin import logger

#: v3.67（用户 m20117）：超上限不立刻删——刚更新过的摘要先留 5 分钟缓冲，
#: 之后被下一次触发的裁剪带走（刚压完就被删等于这轮白压）。
PRUNE_GRACE_SECONDS = 300


class Summaries:
    """sid -> {text, ts, count, lines}。count 是累计被压进来的消息条数。"""

    def __init__(self, data_dir, filename: str = "summaries.json",
                 keep: int = 0, keep_days: int = 0):
        self.path = Path(data_dir) / filename
        self._data: dict[str, dict] = {}
        self._backup_required = False
        # 用户 m13708 第 4/5 条：摘要也要有上限和保留天数。
        # keep = 最多留多少个会话的摘要（每个群聊/私聊各一条），0 = 不限；
        # keep_days = 摘要最多留多少天，0 = 不限。
        try:
            self.keep = max(0, int(keep or 0))
        except (TypeError, ValueError):
            self.keep = 0
        try:
            self.keep_days = max(0, int(keep_days or 0))
        except (TypeError, ValueError):
            self.keep_days = 0
        self._load()
        # 老数据可能早就超过新设的上限，开机先按设置剪一次（用户第 5 条：
        # 「早期记录可以先清掉」）。默认 0/0 = 不删，行为和以前一样。
        if self._prune():
            self._save()

    def _prune(self) -> int:
        """按保留天数与保留会话数删摘要，返回删掉的会话数。

        口径：每个会话（群聊/私聊）在表里只有一条摘要，entry 里带 ts
        （最后更新时间）。所以
        ① keep_days > 0：最后更新早于 keep_days 天的直接丢（ts 缺失的当
           「不知道多久」，不按天数删，免得把老数据一开机就清空）；
        ② keep > 0：按 ts 从新到旧留最近 keep 个会话，其余丢掉——不是按
           插入顺序，因为老数据是历史累积的，插入顺序不等于新旧。
        """
        removed = 0
        if self.keep_days > 0:
            cutoff = time.time() - self.keep_days * 86400
            for sid in list(self._data.keys()):
                try:
                    ts = float(self._data[sid].get("ts") or 0.0)
                except (TypeError, ValueError, AttributeError):
                    ts = 0.0
                if ts > 0 and ts < cutoff:
                    self._data.pop(sid, None)
                    removed += 1
        if self.keep > 0 and len(self._data) > self.keep:
            def _ts(sid: str) -> float:
                try:
                    return float(self._data[sid].get("ts") or 0.0)
                except (TypeError, ValueError, AttributeError):
                    return 0.0
            order = sorted(self._data.keys(), key=_ts, reverse=True)
            # v3.67（用户 m20117）：刚更新过的先留着，别一压完就被删。
            grace_cut = time.time() - PRUNE_GRACE_SECONDS
            for sid in order[self.keep:]:
                if _ts(sid) >= grace_cut:
                    continue
                self._data.pop(sid, None)
                removed += 1
        return removed

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("summary root is not an object")
        except FileNotFoundError:
            return
        except Exception as exc:
            # 整份不可读时，后续保存必须先保全原文件，不能把空表直接覆盖回去。
            self._backup_required = True
            logger.error(f"[multi-decision] 读取中期摘要失败（{type(exc).__name__}），保存前需备份原文件")
            return

        for index, (sid, item) in enumerate(raw.items(), 1):
            field = "entry"
            try:
                if not isinstance(item, dict):
                    raise TypeError("summary entry is not an object")
                field = "text"
                if not item.get("text"):
                    continue
                text = str(item["text"])
                field = "ts"
                ts = float(item.get("ts") or 0.0)
                if not math.isfinite(ts):
                    raise ValueError("summary timestamp is not finite")
                field = "count"
                count = int(item.get("count") or 0)
                field = "lines"
                lines = int(item.get("lines") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                # 不输出 sid、正文、原始数值或异常正文，避免日志泄漏会话内容。
                logger.warning(f"[multi-decision] 跳过中期摘要第{index}项，字段{field}无效（{type(exc).__name__}）")
                continue
            self._data[str(sid)] = {"text": text, "ts": ts, "count": count, "lines": lines}

    def _save(self):
        if self._backup_required:
            try:
                original = self.path.read_bytes()
                backup = self.path.with_name(f"{self.path.name}.corrupt-{uuid.uuid4().hex}.bak")
                # 独占创建，重名/读取/写入/同步任一步失败都不得覆盖损坏原件。
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._backup_required = False
            except Exception as exc:
                logger.error(f"[multi-decision] 中期摘要原文件备份失败（{type(exc).__name__}），拒绝本次保存")
                return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存中期摘要失败")

    def all(self) -> dict:
        return {k: dict(v) for k, v in self._data.items()}

    def get(self, sid: str) -> dict:
        item = self._data.get(str(sid))
        return dict(item) if item else {"text": "", "ts": 0.0, "count": 0, "lines": 0}

    def text(self, sid: str) -> str:
        item = self._data.get(str(sid))
        return str(item.get("text") or "") if item else ""

    def set(self, sid: str, text: str, count: int = 0, lines: int = 0) -> dict:
        sid = str(sid or "").strip()
        if not sid:
            return {}
        text = str(text or "").strip()
        if not text:
            self._data.pop(sid, None)
            self._save()
            return {}
        item = {
            "text": text,
            "ts": time.time(),
            "count": max(0, int(count or 0)),
            "lines": max(0, int(lines or 0)),
        }
        self._data[sid] = item
        # 写一条就顺手看看要不要按上限/天数剪（用户 m13708 第 4 条）。
        # 清理只改内存，保存一次就够——以前清理命中时会连写两遍同样的内容。
        self._prune()
        self._save()
        return dict(item)

    def clear(self, sid: str) -> bool:
        sid = str(sid or "").strip()
        if not sid or sid not in self._data:
            return False
        self._data.pop(sid, None)
        self._save()
        return True

    def clear_all(self) -> int:
        n = len(self._data)
        self._data.clear()
        self._save()
        return n

    def info(self, sid: str) -> dict:
        """给页面用的摘要概况。"""
        item = self.get(sid)
        text = item.get("text") or ""
        return {
            "text": text,
            "chars": len(text),
            "lines": item.get("lines") or 0,
            "count": item.get("count") or 0,
            "ts": item.get("ts") or 0.0,
        }
