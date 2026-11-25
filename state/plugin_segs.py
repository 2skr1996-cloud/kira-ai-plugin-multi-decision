"""插件注入段清单（用户 m20722：别的插件往请求里塞的段，自动建卡）。

回复侧重拼 system_prompt 时只认布局里的段，别的插件在 llm_request 钩子里追加的段
会被静默丢掉。逐个插件适配不现实，所以这里做通用机制：第一次见到不认识的段名就
记下来，布局侧按这份清单补一张只读卡，位置和启停归提示词页管。

只记名字、字数与一小段正文样本——正文每轮由那个插件现给，样本只用来在预览里
顶位（用户 m09215：切到没发过请求的会话时，卡片不该空成 0 字）。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")

#: 用户 m20722：框架自己的段名，不算插件注入。
FRAMEWORK_NAMES = frozenset({
    "role", "persona", "chat_rules", "attention", "output", "format",
    "accounts", "tools", "memory", "time", "sessions", "chat_env",
    "dynamic_tail", "dynamic_context_start", "dynamic_context_end", "message",
})

#: 名字只认这种形态：字母数字加 _ . : -，最长 64。
#: 快照里出现过「### 怎么回答」「## 你的身份（Identity）」这种被误当段名的正文片段，
#: 不卡一道的话会建出一堆垃圾卡。
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

#: 最多建这么多张卡；超出的名字只走兜底保留，不再建卡。
MAX_SEGS = 12

#: 用户 m09215：正文样本最多存这么多字，够预览看个大概就行。
SAMPLE_MAX = 4000


class PluginSegs:
    """插件注入段的登记表，落盘在 plugin_data 下的 plugin_segs.json。"""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "plugin_segs.json"
        self._data = self._load()

    def _load(self):
        if not self.path.is_file():
            return {"version": 1, "segs": {}}
        try:
            got = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(got, dict) or not isinstance(got.get("segs"), dict):
                raise ValueError("根节点不是对象")
            return got
        except Exception:
            logger.exception("[multi-decision] 插件注入段清单读取失败，按空清单继续：%s", self.path)
            return {"version": 1, "segs": {}}

    @staticmethod
    def is_plugin_name(name):
        """这个名字像不像「别的插件注入的段」。"""
        raw = str(name or "").strip()
        if not raw or raw in FRAMEWORK_NAMES:
            return False
        # 提示词模块自己的 id（pm-xxx）不是插件段。
        if raw.startswith("pm-"):
            return False
        return bool(_NAME_RE.match(raw))

    def note(self, name, chars=0, location="system", text=""):
        """记一次「这一轮见到了这个段」。返回是否写了盘。

        用户 m23037：location 记的是它注入在哪——system_prompt 还是 user_prompt。
        两处都要建卡，但位置与「关掉以后从哪儿摘」不一样，所以得分开记。

        用户 m09215：顺手把正文样本存下来。预览是按会话取正文的，切到没发过请求的
        会话就空成 0 字；有样本就能顶位，卡片不再写「还没抓到正文」。
        """
        raw = str(name or "").strip()
        if not self.is_plugin_name(raw):
            return False
        loc = "user" if str(location or "").strip().lower() == "user" else "system"
        sample = str(text or "")[:SAMPLE_MAX]
        segs = self._data.setdefault("segs", {})
        now = time.time()
        item = segs.get(raw)
        if not isinstance(item, dict):
            if len(segs) >= MAX_SEGS:
                logger.warning("[multi-decision] 插件注入段已满 %d 个，%s 只走兜底保留、不再建卡",
                               MAX_SEGS, raw)
                return False
            segs[raw] = {"name": raw, "first_seen": now, "last_seen": now,
                         "chars": int(chars or 0), "hits": 1, "location": loc,
                         "sample": sample, "sample_at": now}
            self.save()
            return True
        old = int(item.get("chars") or 0)
        old_sample = str(item.get("sample") or "")
        item["last_seen"] = now
        item["hits"] = int(item.get("hits") or 0) + 1
        item["chars"] = int(chars or 0)
        # 这一轮没拿到正文（渲染失败之类）时别把上一次的样本冲掉。
        if sample:
            item["sample"] = sample
        # 注入位置换了（插件改了实现）也得落盘，不然卡片还按老位置找正文。
        moved = str(item.get("location") or "system") != loc
        item["location"] = loc
        # 字数变化超过 10% 才写盘，别每轮都刷一次文件。
        if moved or old <= 0 or abs(item["chars"] - old) > max(1, old // 10):
            item["sample_at"] = now
            self.save()
            return True
        # 样本从无到有、或者隔了一分钟才换一次，也值得落盘（重启后预览还有东西看）。
        if sample and (not old_sample or now - float(item.get("sample_at") or 0) > 60):
            item["sample_at"] = now
            self.save()
            return True
        return False

    def names(self):
        """按第一次见到的顺序给出所有已登记的段名。"""
        segs = self._data.get("segs")
        if not isinstance(segs, dict):
            return []
        rows = [v for v in segs.values()
                if isinstance(v, dict) and self.is_plugin_name(v.get("name"))]
        rows.sort(key=lambda v: (float(v.get("first_seen") or 0), str(v.get("name") or "")))
        return [str(v["name"]) for v in rows]

    def info(self, name):
        segs = self._data.get("segs")
        item = segs.get(str(name or "")) if isinstance(segs, dict) else None
        return dict(item) if isinstance(item, dict) else {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[multi-decision] 插件注入段清单保存失败：%s", self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
