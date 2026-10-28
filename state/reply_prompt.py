"""回复提示词兼容层：优先委托 PromptLayouts，旧路径按 sid 缓存请求原文。

缓存与旧快照仅描述插件处理过的运行请求，不是框架官方默认模板或 provider wire。
预览按 Prompt.to_string() 的原文顺序连接，不额外剥除或插入段落分隔。
"""

from __future__ import annotations

import time

from core.logging_manager import get_logger

try:
    from core.prompt_manager import Prompt
except Exception:  # 测试桩 / 老版本框架没有这个模块
    Prompt = None

# 与插件其它模块同款两参数写法：真机 get_logger(name, color) 少给 color 会 TypeError，
# 整个插件在热重载时报 "Import error: get_logger() missing 1 required positional argument: 'color'"。
logger = get_logger("multi_decision", "orange")

#: 人格段的段名。真机快照里是 persona（role / persona / attention / output / …）。
PERSONA_NAME = "persona"


class ReplyPrompt:
    """新提示词服务入口及旧配置/快照兼容；指定 sid 时绝不跨会话借用。"""

    NAME = "chat_rules"

    def __init__(self, plugin):
        self.plugin = plugin
        self._parts = []   # 未指定 sid 时兼容最近一次；指定 sid 只读对应缓存
        self._at = 0.0
        self._source_sid = ""
        self._parts_by_sid = {}
        self._at_by_sid = {}

    #: 段名 → 中文标签。设置页左栏按这个顺序摆卡片，右栏折叠头也用这个。
    LABELS = [
        ("role", "角色设定"),
        ("persona", "角色扮演"),
        ("attention", "注意事项"),
        ("output", "输出要求"),
        ("format", "输出格式"),
        ("accounts", "你的身份"),
        ("tools", "工具说明"),
        ("memory", "记忆"),
        ("time", "时间"),
    ]

    @classmethod
    def label_of(cls, name: str) -> str:
        for key, lab in cls.LABELS:
            if key == name:
                return lab
        return "注意事项提示词" if name == cls.NAME else str(name or "")

    def _overrides(self) -> dict:
        """用户改过的段（设置页左栏那些卡片）。读不出来就当没改。"""
        try:
            raw = self.plugin._decision_get("reply_seg_overrides", "")
        except Exception:
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        raw = str(raw or "").strip()
        if not raw:
            return {}
        try:
            import json
            got = json.loads(raw)
        except Exception:
            logger.debug("[multi-decision] reply_seg_overrides 不是合法 JSON，按空处理")
            return {}
        return got if isinstance(got, dict) else {}

    # ---------------------------------------------------------------- 配置
    def _rules(self) -> str:
        """用户填的聊天规则。空串就是「不插这一段」。"""
        try:
            return str(self.plugin._decision_get("decision_chat_rules", "") or "")
        except Exception:
            logger.debug("[multi-decision] 读聊天规则失败（按空处理）", exc_info=True)
            return ""

    # ---------------------------------------------------------------- 取文本
    @staticmethod
    def text_of(p) -> str:
        """一段 Prompt 的纯文本。

        先走 to_string()（会渲染模板），失败再退回 content；content 在某些段上是
        list（v3.91 的决策快照就这么踩过），所以拼接要考虑。
        """
        try:
            out = p.to_string()
            if isinstance(out, str):
                return out
        except Exception:
            pass
        for attr in ("content", "text", "value"):
            v = getattr(p, attr, None)
            if isinstance(v, str):
                return v
            if isinstance(v, (list, tuple)):
                try:
                    return "".join(str(x) for x in v)
                except Exception:
                    pass
        try:
            return str(p)
        except Exception:
            return ""

    # ---------------------------------------------------------------- 注入
    def apply(self, request, sid: str = "") -> bool:
        """插一段聊天规则到人格后面，顺手把这一轮的 system 抄下来。

        幂等：同名段先删再插，重试链路里反复调也不会叠成两段。
        """
        layouts = getattr(self.plugin, "prompt_layouts", None)
        if layouts is not None:
            return layouts.apply_reply(request, sid)
        prompts = getattr(request, "system_prompt", None)
        if not isinstance(prompts, list):
            return False
        try:
            ov = self._overrides()
            swapped_seg = False
            if ov and Prompt is not None:
                swapped = []
                for p in prompts:
                    nm = str(getattr(p, "name", "") or "")
                    text0 = ov.get(nm)
                    if nm in ov and isinstance(text0, str):
                        try:
                            swapped.append(Prompt(text0, name=nm, source="system", persist=False))
                            swapped_seg = True
                            continue
                        except Exception:
                            logger.debug("[multi-decision] 覆盖段 %s 失败（用原文）", nm)
                    swapped.append(p)
                if swapped:
                    prompts = swapped
            kept = [p for p in prompts if (getattr(p, "name", "") or "") != self.NAME]
            text = self._rules()
            pos = 0
            for i, p in enumerate(kept):
                if (getattr(p, "name", "") or "") == PERSONA_NAME:
                    pos = i + 1
                    break
            if text.strip() and Prompt is not None:
                try:
                    kept.insert(pos, Prompt(text, name=self.NAME, source="system", persist=False))
                except Exception:
                    logger.exception("[multi-decision] 造聊天规则段失败（本轮不加）")
            if swapped_seg or len(kept) != len(prompts) or text.strip():
                setattr(request, "system_prompt", kept)
            self.note(kept, sid)
            return True
        except Exception:
            logger.exception("[multi-decision] 注入聊天规则失败（已吞掉）")
            return False

    def note(self, prompts, sid: str = "") -> None:
        """按 sid 保存 to_string 原文；无 sid 的旧调用仍保留独立兼容槽。"""
        if not isinstance(prompts, (list, tuple)):
            return
        rows = []
        for p in prompts:
            name = str(getattr(p, "name", "") or "")
            body = self.text_of(p)
            if not body:
                continue
            rows.append({"name": name, "content": body, "ours": name == self.NAME})
        if rows:
            key = str(sid or "").strip()
            at = time.time()
            self._parts_by_sid[key] = rows
            self._at_by_sid[key] = at
            self._parts = rows
            self._at = at
            self._source_sid = key

    @classmethod
    def _replacement_text(cls, text: str, name: str) -> str:
        """与 apply 新建段走同一渲染入口，保留框架真实 end。"""
        if Prompt is not None:
            try:
                return cls.text_of(Prompt(text, name=name, source="system", persist=False))
            except Exception:
                logger.debug("[multi-decision] 预览构造段 %s 失败（保留正文）", name)
        return text

    # ---------------------------------------------------------------- 快照兜底
    def _snapshot_system(self, sid: str = "") -> tuple:
        """只读同会话回复快照；未指定 sid 的兼容预览显式报告来源。"""
        log = getattr(getattr(self, "plugin", None), "request_log", None)
        if log is None:
            return "", {}
        key = str(sid or "").strip()
        if key:
            data = self._snap_read(log, key)
            return str(data.get("system_prompt") or ""), data
        latest = {}
        latest_at = float("-inf")
        try:
            for row in (log.list_sessions() or [])[:8]:
                name = str(row.get("sid") or "").strip()
                if not name:
                    continue
                one = self._snap_read(log, name)
                if not one:
                    continue
                try:
                    at = float(one.get("ts") or 0)
                except (TypeError, ValueError):
                    at = 0.0
                if at > latest_at:
                    latest, latest_at = one, at
        except Exception:
            logger.debug("[multi-decision] 读取回复快照会话列表失败", exc_info=True)
        return str(latest.get("system_prompt") or ""), latest

    @staticmethod
    def _snap_read(log, sid: str) -> dict:
        """在同 sid 最近记录中跳过决策/裁切；兼容只有 last_snapshot 的旧桩。"""
        listing = getattr(log, "list_rounds", None)
        try:
            if callable(listing):
                try:
                    rows = listing(sid, 200) or []
                except TypeError:
                    rows = listing(sid) or []
            else:
                one = log.last_snapshot(sid) or {}
                rows = [one] if one else []
        except Exception:
            logger.debug("[multi-decision] 读取同会话回复快照目录失败", exc_info=True)
            return {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            try:
                full = log.read_round(sid, name) if name else row
            except Exception:
                logger.debug("[multi-decision] 读取回复快照 %s 失败", name, exc_info=True)
                continue
            if not isinstance(full, dict):
                continue
            source_sid = str(full.get("sid") or sid or "").strip()
            if source_sid != sid:
                continue
            outcome = str(full.get("outcome") or row.get("outcome") or "").lower()
            if (outcome in ("decision", "trim") or full.get("trim_only")
                    or row.get("trim_only")
                    or (not outcome and full.get("trigger_reason") == "decision")):
                continue
            system = full.get("system_prompt")
            if not isinstance(system, str) or not system.strip():
                continue
            data = dict(full)
            data["source_sid"] = source_sid
            return data
        return {}

    @classmethod
    def _insert_rules(cls, text: str, rules: str) -> str:
        """把聊天规则插到 Persona 那一节的末尾（真机注入的位置就是这个）。"""
        if not (rules or "").strip():
            return text
        rendered = cls._replacement_text(rules, cls.NAME)
        marker = "## 角色扮演（Persona）"
        i = text.find(marker)
        if i < 0:
            return text + rendered
        j = text.find("\n## ", i + len(marker))
        if j < 0:
            return text + rendered
        pos = j + 1
        return text[:pos] + rendered + text[pos:]

    @staticmethod
    def _heads(text: str) -> list:
        """按标题切节供展示，保留标题和换行，使各节连接仍等于原文。"""
        out = []
        cur = None
        for line in text.splitlines(keepends=True):
            if line.startswith("## "):
                if cur is not None:
                    out.append(cur)
                cur = {"name": line[3:].strip(), "chars": 0, "text": ""}
            elif cur is None:
                cur = {"name": "system", "chars": 0, "text": ""}
            cur["text"] += line
            cur["chars"] += len(line)
        if cur is not None:
            out.append(cur)
        return out

    def _preview_from_snapshot(self, sid: str, rules: str, injected: bool) -> dict:
        """没有内存记录时的预览：快照原文为主，我们那一段按当前配置写。"""
        text, snap = self._snapshot_system(sid)
        if text.strip():
            body = self._insert_rules(text, rules) if injected else text
            rows = self._heads(body)
            at = ""
            try:
                ts = float(snap.get("ts") or 0)
                if ts:
                    at = time.strftime("%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                at = ""
            return {
                "ok": True,
                "sid": str(sid or ""),
                "system": body,
                "chars": len(body),
                "parts": [{"name": it["name"], "label": self.label_of(it["name"]),
                           "chars": it["chars"], "text": it.get("text", ""),
                           "edited": False, "ours": False} for it in rows],
                "at": at,
                "source": "snapshot",
                "source_sid": str(snap.get("source_sid") or ""),
                "injected": injected,
                "persona_at": next((i for i, it in enumerate(rows)
                                    if "Persona" in it["name"] or "角色扮演" in it["name"]), -1),
                "rules_at": next((i for i, it in enumerate(rows) if "聊天规则" in it["name"]), -1),
            }
        rows = []
        if injected:
            rows.append({"name": self.NAME, "content": self._replacement_text(rules, self.NAME), "ours": True})
        system = "".join(str(it.get("content") or "") for it in rows)
        return {
            "ok": True,
            "sid": str(sid or ""),
            "system": system,
            "chars": len(system),
            "parts": [{"name": it.get("name", ""), "label": self.label_of(it.get("name", "")),
                       "chars": len(str(it.get("content") or "")),
                       "text": str(it.get("content") or ""),
                       "edited": False, "ours": bool(it.get("ours"))} for it in rows],
            "at": "",
            "source": "empty",
            "source_sid": "",
            "overrides": sorted(self._overrides().keys()),
            "injected": injected,
            "persona_at": -1,
            "rules_at": 0 if rows else -1,
        }

    # ---------------------------------------------------------------- 预览
    def preview(self, sid: str = "") -> dict:
        """拼一份给人看的完整 system。我们那一段用当前配置重写。"""
        layouts = getattr(self.plugin, "prompt_layouts", None)
        if layouts is not None:
            return layouts.preview_reply(sid)
        key = str(sid or "").strip()
        rules = self._rules()
        injected = bool(rules.strip())
        cached = self._parts_by_sid.get(key, []) if key else self._parts
        at = self._at_by_sid.get(key, 0.0) if key else self._at
        source_sid = key if key else self._source_sid
        rows = [dict(x) for x in cached]
        if not rows:
            return self._preview_from_snapshot(sid, rules, injected)
        ov = self._overrides()
        if ov:
            for it in rows:
                nm = it.get("name", "")
                if nm in ov and isinstance(ov.get(nm), str) and nm != self.NAME:
                    it["content"] = self._replacement_text(ov[nm], nm)
                    it["edited"] = True
        replaced = False
        for it in rows:
            if it.get("name") == self.NAME:
                it["content"] = self._replacement_text(rules, self.NAME)
                it["ours"] = True
                replaced = True
        if injected and not replaced:
            pos = 0
            for i, it in enumerate(rows):
                if it.get("name") == PERSONA_NAME:
                    pos = i + 1
                    break
            rows.insert(pos, {"name": self.NAME, "content": self._replacement_text(rules, self.NAME), "ours": True})
        if not injected:
            rows = [it for it in rows if it.get("name") != self.NAME]
        rows = [it for it in rows if str(it.get("content") or "")]
        system = "".join(str(it.get("content") or "") for it in rows)
        return {
            "ok": True,
            "sid": str(sid or ""),
            "system": system,
            "chars": len(system),
            "parts": [{"name": it.get("name", ""), "label": self.label_of(it.get("name", "")),
                       "chars": len(str(it.get("content") or "")),
                       "text": str(it.get("content") or ""),
                       "edited": bool(it.get("edited")),
                       "ours": bool(it.get("ours"))} for it in rows],
            "overrides": sorted(ov.keys()),
            "at": time.strftime("%m-%d %H:%M:%S", time.localtime(at)) if at else "",
            "source": "live" if at else "empty",
            "source_sid": source_sid,
            "injected": injected,
            "persona_at": next((i for i, it in enumerate(rows) if it.get("name") == PERSONA_NAME), -1),
            "rules_at": next((i for i, it in enumerate(rows) if it.get("name") == self.NAME), -1),
        }
