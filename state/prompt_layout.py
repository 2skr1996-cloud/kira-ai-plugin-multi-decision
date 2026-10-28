"""有序提示词装配：预览与实际请求同源，原生 Prompt 不重建、不接管发送流程。"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .sids import normalize_sid

logger = logging.getLogger(__name__)
KINDS = {"decision", "reply"}
# custom = 引用「自定义模板库」里的一条；realtime_marker / dynamic_marker 只标位置、不注入 system。
TYPES = {"text", "custom", "identity", "framework", "decision_prompt",
         "decision_format", "persona", "tools", "realtime_marker", "dynamic_marker",
         "dynamic", "system_dynamic"}
DYNAMIC = {"sessions", "chat_env", "memory", "time"}
# 用户 m02159 图3：身份段两边本来就是同一个文件，命名规则跟决策那边统一——去掉英文括注。
LABELS = {"role": "角色设定", "persona": "角色扮演", "chat_rules": "注意事项提示词",
          "attention": "注意事项", "output": "输出要求", "format": "输出格式",
          "accounts": "你的身份", "tools": "工具说明", "memory": "记忆",
          "time": "时间", "sessions": "会话信息", "chat_env": "聊天环境"}
# 用户 m01209（图1/图3）：模块标题一律等于所选模板名，注入时格式才整齐。
TYPE_LABELS = {"text": "自写文本", "custom": "自定义模板",
               "decision_prompt": "决策提示词", "decision_format": "输出规则",
               "identity": "你的身份", "persona": "角色扮演",
               "tools": "工具说明", "realtime_marker": "实时窗口",
               "dynamic_marker": "动态尾段", "dynamic": "动态段",
               "system_dynamic": "系统动态段"}
# 身份段由框架自动填好，只让看、不让改（用户图6）。
READONLY_TYPES = {"identity", "system_dynamic"}
# 只有这几种存正文；其余都是「引用」，保存时不落正文，所以永远覆盖不了默认模板。
BODY_TYPES = {"text", "custom", "realtime_marker", "dynamic"}
# 只标位置、不进 system 的两种占位（用户 m02159 图1）：实时窗口 + 动态段。
# dynamic_marker 留在 TYPES 里只为兼容旧文档，读到就丢掉（用户 m02993：动态段改成真模板）。
MARKER_TYPES = {"realtime_marker", "dynamic", "system_dynamic"}
DYNAMIC_NOTE = ("（这一段不注入固定 system：会话信息、聊天环境、记忆、时间会由框架拼在"
                "实时窗下方，实时窗本身又接在系统提示词后面。）")
# 用户 m02993：动态段默认就是这段「强化格式遵守」——拼在本轮聊天记录最后，紧贴模型开口的位置，
# 专门提醒模型把回复包进 <msg>（记录里出现过只回裸文本、发不出去的情况）。
DYNAMIC_TEMPLATE_DEFAULT = (
    "【格式强化 · 每一条都要照做】\n"
    "1. 你的全部输出必须包在 <msg> 里，一个气泡一个标签：\n"
    "   <msg message_id=\"\"><text>要说的话</text></msg>\n"
    "2. 标签外面多写的字发不出去——写在外面等于这一轮白说。\n"
    "3. 想说几句就写几个 <msg>，别把几句话塞进同一个 <text>。\n"
    "4. 直接开口说话：不要解释为什么这么回，也不要复述这段规则。\n"
    # 用户 m04617 图8：光说「要包 <msg>」不够——这一轮要是真不该开口，
    # 也得给模型一个说得清、插件认得出来的写法，不然它就会硬答一句。
    "5. 确实不该开口时（判断错了、群里已经说完、话题插不进），只输出一个空壳\n"
    "   <msg></msg>，一个字都别多写——空壳代表「这条不发」，群里不会出现任何东西。\n"
)
DYNAMIC_TEMPLATE_NAME = "强化格式遵守"
# 用户 m02159 图1：实时窗口这一段要看得见「聊天记录会被拼在这里」，所以给一段假示例。
REALTIME_NOTE = (
    "这一段是模型看到的现场聊天记录：真实请求里由框架自动拼在这里，不是我们发出去的。\n"
    "它接在系统提示词的后面，条数由「窗口」里的上限决定；下面是示例，只用来看清位置。\n"
    "\n"
    "[19:02:11] 群友甲：这枪后坐力也太大了\n"
    "[19:02:19] 群友乙：压枪懂不懂\n"
    "[19:02:25] 群友甲：我这手机新买的，别教我身法\n"
)


def module(mid, typ, label, key="", content="", enabled=True):
    return {"id": mid, "type": typ, "label": label, "key": key, "content": content, "enabled": enabled}


_CN2KEY = {v: k for k, v in LABELS.items()}


def seg_key(name):
    """用户 m02993 图3：身份段两边本来就是同一份文件，名字也要统一。

    框架给的分段名有时带英文括注（「你的身份（Identity）」），有时直接是英文 key
    （「accounts」）。这里一律折成英文 key，界面上就只剩一个「你的身份」。
    """
    raw = str(name or "").strip()
    if raw in _CN2KEY:
        return _CN2KEY[raw]
    base = raw.split("（")[0].split("(")[0].strip()
    return _CN2KEY.get(base, raw)


def type_label(typ, key=""):
    """模块名 = 模板名。引用型模块的名字由类型（框架段由 key）决定，用户改不动。"""
    if typ == "framework":
        k = seg_key(key)
        return LABELS.get(k, k or "框架原段")
    return TYPE_LABELS.get(str(typ or ""), str(typ or ""))


def tail_order(modules):
    """用户 m02993 图1：实时窗口永远排在系统模板下面、动态段上面，动态段永远在最后。

    这两块标的是「位置」，拖到中间会让页面顺序和真实请求的顺序对不上，
    所以保存和读取时都强制归位，不靠玩家自己摆。
    """
    keep, realtime, sysdyn, dyn = [], [], [], []
    for m in modules:
        typ = m.get("type")
        if typ == "realtime_marker":
            realtime.append(m)
        elif typ == "system_dynamic":
            sysdyn.append(m)
        elif typ == "dynamic":
            dyn.append(m)
        else:
            keep.append(m)
    # 用户 m04343 第 3 条：动态段（强化格式遵守）排在系统动态段上面——它才是最贴近聊天记录的
    # 那一段；系统动态段是框架自己往 system 末尾拼的内容，位置在它上面一格。
    return keep + realtime + dyn + sysdyn


def normalize_layout(raw, kind):
    if kind not in KINDS or not isinstance(raw, dict):
        raise ValueError("提示词文档或类型不合法")
    if raw.get("version", 1) != 1 or raw.get("mode", "modules") not in ("modules", "text"):
        raise ValueError("不支持的提示词文档版本或模式")
    text, modules = raw.get("text", ""), raw.get("modules", [])
    if not isinstance(text, str) or len(text) > 200000 or not isinstance(modules, list) or len(modules) > 80:
        raise ValueError("提示词最多 80 块，单段文本最多 20 万字符")
    clean, ids = [], set()
    for i, m in enumerate(modules):
        if not isinstance(m, dict) or m.get("type") not in TYPES:
            raise ValueError("模块类型不合法")
        typ = m["type"]
        if (kind == "decision" and typ == "framework") or (kind == "reply" and typ in ("decision_prompt", "decision_format")):
            raise ValueError("模块不适用于这一类提示词")
        if typ == "decision_format" and kind == "decision":
            # 用户 m04343 第 4 条：输出规则不再是「只读引用」，而是决策插件自带的一份默认模板
            # 正文——旧文档读进来直接换成文本块，正文没存过就补上默认那份。
            from .decision_phase import DEFAULT_DECISION_FORMAT
            typ = "text"
            if not str(m.get("content") or "").strip():
                m = dict(m, content=DEFAULT_DECISION_FORMAT)
        if typ == "dynamic_marker":
            # 旧文档里的动态尾段占位：动态段现在是真模板，读到就丢（用户 m02993）。
            continue
        mid = m.get("id", "module-%d" % i)
        if not isinstance(mid, str) or not mid or len(mid) > 100 or mid in ids:
            raise ValueError("模块 id 为空、过长或重复")
        ids.add(mid)
        raw_key, body = m.get("key", ""), m.get("content", "")
        if not isinstance(raw_key, str) or not isinstance(body, str) or len(body) > 200000:
            raise ValueError("引用键与正文必须是文本")
        if not isinstance(m.get("enabled", True), bool):
            raise ValueError("模块启停值必须是布尔值")
        # 只有自写文本存正文；引用型只记「引用到哪」，所以保存不可能覆盖默认模板（用户图2）。
        key = raw_key[:120] if typ in ("framework", "persona", "custom") else ""
        body = body if typ in BODY_TYPES else ""
        if typ in READONLY_TYPES:
            key, body = "", ""
        # 名字跟着模板走（用户图1/图3）；只有自写文本的名字是玩家自己起的，保留下来
        # （用户 m02159 图2 拆出来的「什么时候接 / 什么时候别接 / 节奏与分寸」就靠这个）。
        raw_label = str(m.get("label") or "").strip()
        label = raw_label[:120] if (typ in ("text", "dynamic") and raw_label) else type_label(typ, key)
        clean.append(module(mid, typ, label, key, body, m.get("enabled", True)))
    clean = tail_order(clean)
    out = {"version": 1, "mode": raw.get("mode", "modules"), "text": text, "modules": clean}
    if len(json.dumps(out, ensure_ascii=False)) > 500000:
        raise ValueError("提示词文档总长超过 50 万字符")
    return out


class PromptLayouts:
    def __init__(self, plugin, data_dir):
        self.plugin = plugin
        self.path = Path(data_dir) / "prompt_layouts.json"
        self.source_path = Path(data_dir) / "prompt_sources.json"
        self._data = self._load(self.path, {"version": 1, "layouts": {}})
        self._sources = self._load(self.source_path, {})
        self._snapshot_checked = set()
        self._native_pending = {}
        self._applied = {}
        # 同一个 request 反复套动态段（重试链路）时要先还原上一次，别叠成两遍。
        self._applied_user = {}
        self._official = self._load(Path(__file__).with_name("kira_default_templates.json"), {})
        self.tpl_path = Path(data_dir) / "prompt_templates.json"
        self._tpl = self._load(self.tpl_path, {"version": 1, "items": []})
        # 用户 m02159 图3：「工具说明」段里的勾选单独存一份，勾了才注入、才允许模型调用。
        self.tools_path = Path(data_dir) / "prompt_tools.json"
        self._tools = self._load(self.tools_path, {"version": 1, "names": []})

    @staticmethod
    def _load(path, default):
        if not path.is_file():
            return copy.deepcopy(default)
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(got, dict):
                raise ValueError("根节点不是对象")
            return got
        except Exception:
            logger.exception("[multi-decision] 提示词数据读取失败，不以空数据覆盖：%s", path)
            raise

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                tmp.unlink()

    # ---------- 自定义模板库（用户图5：任意模板以卡片展示，两边都能引用） ----------

    def templates(self):
        out = []
        for it in list((self._tpl or {}).get("items") or []):
            if not isinstance(it, dict):
                continue
            tid, name = str(it.get("id") or ""), str(it.get("name") or "")
            if not tid or not name:
                continue
            out.append({"id": tid, "name": name, "content": str(it.get("content") or ""),
                        "kind": str(it.get("kind") or "any"),
                        "at": float(it.get("at") or 0), "ut": float(it.get("ut") or 0)})
        out.sort(key=lambda x: (-x["ut"], x["name"]))
        return out

    def template_text(self, tid):
        want = str(tid or "")
        if not want:
            return ""
        return next((t["content"] for t in self.templates() if t["id"] == want), "")

    def save_template(self, name, content, tid=""):
        name, content = str(name or "").strip(), str(content or "")
        if not name:
            raise ValueError("模板名称必填")
        if len(name) > 60:
            raise ValueError("模板名称最多 60 字")
        if len(content) > 200000:
            raise ValueError("模板正文最多 20 万字符")
        now = time.time()
        items = [dict(t) for t in ((self._tpl or {}).get("items") or []) if isinstance(t, dict)]
        want = str(tid or "").strip()
        hit = next((t for t in items if str(t.get("id") or "") == want), None) if want else None
        if hit is None:
            want = "ct-%d-%s" % (int(now * 1000), os.urandom(3).hex())
            items.insert(0, {"id": want, "name": name, "content": content, "kind": "any", "at": now, "ut": now})
        else:
            hit.update({"name": name, "content": content, "ut": now})
        new = {"version": 1, "items": items}
        self._write(self.tpl_path, new)
        self._tpl = new
        return {"id": want, "name": name, "content": content}

    def delete_template(self, tid):
        want = str(tid or "")
        items = [dict(t) for t in ((self._tpl or {}).get("items") or [])
                 if isinstance(t, dict) and str(t.get("id") or "") != want]
        new = {"version": 1, "items": items}
        self._write(self.tpl_path, new)
        self._tpl = new
        return True

    def selected_tools(self):
        """提示词页勾选的工具；没存过就是 None（跟着设置页那份走）。"""
        got = (self._tools or {}).get("names")
        if not isinstance(got, list):
            return None
        return [str(x) for x in got if str(x).strip()]

    def set_tools(self, names):
        clean = []
        for x in (names or []):
            name = str(x).strip()[:80]
            if name and name not in clean:
                clean.append(name)
        new = {"version": 1, "names": clean[:50]}
        self._write(self.tools_path, new)
        self._tools = new
        return clean

    def _relabel(self, doc):
        """用户 m01209 图1：模块名一直跟着所选模板名走，不让名字和内容对不上。"""
        names = {t["id"]: t["name"] for t in self.templates()}
        for m in doc.get("modules", []):
            typ, key = m.get("type"), str(m.get("key") or "")
            if typ == "custom":
                # 自定义模板：名字就是模板名；模板被删了才退回原名字。
                m["label"] = str(names.get(key) or m.get("label") or TYPE_LABELS["custom"])[:120]
            elif typ in ("text", "dynamic") and str(m.get("label") or "").strip():
                m["label"] = str(m.get("label")).strip()[:120]
            else:
                m["label"] = type_label(typ, key)[:120]
        return doc

    def revision(self):
        raw = json.dumps(self._data, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _scope(sid):
        return normalize_sid(str(sid or "")) if sid else "*"

    def _config(self, key, default=""):
        return self.plugin._decision_get(key, default)

    @staticmethod
    def _text(p):
        if isinstance(p, dict):
            return str(p.get("text", p.get("content", "")) or "")
        fn = getattr(p, "to_string", None)
        return str(fn() if callable(fn) else getattr(p, "content", "") or "")

    def capture_native(self, request, sid):
        """抄进入本插件前的模板，不渲染尚待绑定的 format，不写用户消息。"""
        sid = normalize_sid(sid)
        if not sid:
            return
        prompts = getattr(request, "system_prompt", None)
        if not isinstance(prompts, (list, tuple)):
            return
        rows = []
        for p in prompts:
            content = getattr(p, "content", "")
            if isinstance(content, str):
                rows.append({"name": str(getattr(p, "name", "") or ""), "template": content,
                             "end": getattr(p, "end", ""),
                             "render_template": bool(getattr(p, "render_template", False))})
        self._native_pending[sid] = rows

    def _framed_dynamic(self, row, kind="reply"):
        """这一段是不是框架自己会搬进本轮消息末尾。

        用户 m06046：框架在 assemble_prompt 阶段会按 bot 配置把 sessions / chat_env /
        time（以及 memory）挪到最后一条 user 消息后面。回复侧这些段已经在请求里了，
        插件再抄进「动态段」就是同一份内容发两遍。
        """
        name = str((row or {}).get("name") or "")
        if name in ("sessions", "chat_env", "time"):
            return getattr(self.plugin, "dynamic_prompt_position", "latest_user") == "latest_user"
        if name == "memory":
            return getattr(self.plugin, "memory_prompt_position", "system") == "latest_user"
        return False

    def _dynamic(self, name):
        if name in ("sessions", "chat_env", "time"):
            return getattr(self.plugin, "dynamic_prompt_position", "latest_user") == "latest_user"
        if name == "memory":
            return getattr(self.plugin, "memory_prompt_position", "system") == "latest_user"
        return False

    def _rows(self, request):
        rows = []
        for attr in ("system_prompt", "user_prompt"):
            for p in getattr(request, attr, []) or []:
                nm = str(getattr(p, "name", "") or "")
                if attr == "user_prompt" and nm not in DYNAMIC:
                    continue
                try:
                    text = self._text(p)
                except Exception:
                    logger.warning("[multi-decision] 提示词段 %s 尚未可渲染", nm)
                    continue
                dynamic = attr == "user_prompt" or self._dynamic(nm)
                rows.append({"name": nm, "text": text, "location": "dynamic" if dynamic else "system"})
        return rows

    def capture_reply(self, request, sid):
        sid = normalize_sid(sid)
        if not sid:
            return
        rows, old = self._rows(request), self._sources.get(sid, {})
        src = {"rows": rows, "native": self._native_pending.pop(sid, old.get("native", [])),
               "source": "同会话框架请求（插件整理后、分段覆盖前）", "at": time.time()}
        self._sources[sid] = src
        if rows != old.get("rows") or src["native"] != old.get("native"):
            try:
                self._write(self.source_path, self._sources)
            except Exception:
                logger.exception("[multi-decision] 保存提示词素材失败，当前轮仅保留内存素材")

    def _ensure_source(self, sid):
        sid = normalize_sid(sid)
        if sid in self._sources or sid in self._snapshot_checked or not sid:
            return self._sources.get(sid, {})
        self._snapshot_checked.add(sid)
        log = getattr(self.plugin, "request_log", None)
        if log is None:
            return {}
        try:
            for item in log.list_rounds(sid, 200):
                if item.get("decision_only") or item.get("trim_only") or item.get("outcome") in ("decision", "trim"):
                    continue
                data = log.read_round(sid, item.get("name", "")) or {}
                if data.get("sid") and normalize_sid(data["sid"]) != sid:
                    continue
                if data.get("decision_only") or data.get("trim_only") or data.get("outcome") in ("decision", "trim"):
                    continue
                text = data.get("system_static", data.get("system_prompt", ""))
                if not isinstance(text, str) or not text:
                    continue
                metadata = [p for p in data.get("system_parts", []) if not p.get("relocated")]
                rows, pos = [], 0
                if metadata and sum(int(p.get("chars") or 0) for p in metadata) == len(text):
                    for p in metadata:
                        n = int(p.get("chars") or 0)
                        rows.append({"name": str(p.get("name") or ""), "text": text[pos:pos+n], "location": "system"})
                        pos += n
                else:
                    # 无可证明的分段边界不猜拆、不冒充官方模板。
                    rows = [{"name": "snapshot_system", "text": text, "location": "system"}]
                self._sources[sid] = {"rows": rows, "native": [], "source": "同会话真实回复快照（非官方原始模板）", "at": data.get("ts", 0)}
                return self._sources[sid]
        except Exception:
            logger.exception("[multi-decision] 同会话回复素材读取失败：%s", sid)
        return {}

    @staticmethod
    def _keyed(rows):
        totals, seen = {}, {}
        out = copy.deepcopy(rows)
        for row in out:
            name = row["name"]
            totals[name] = totals.get(name, 0) + 1
        for row in out:
            name = row["name"]
            seen[name] = seen.get(name, 0) + 1
            row["key"] = name if totals[name] == 1 else "%s#%d" % (name, seen[name])
        return out

    def _base_rows(self, sid):
        return self._keyed(self._ensure_source(sid).get("rows", []))

    def _reply_overrides(self):
        raw = self._config("reply_seg_overrides", "")
        try:
            got = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            return got if isinstance(got, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _legacy_text(text, name):
        from .reply_prompt import ReplyPrompt
        return ReplyPrompt._replacement_text(text, name)

    def default_layout(self, kind, sid="", migrate=True):
        modules = []
        if kind == "decision":
            # 用户 m02159 图2：默认不再放「决策提示词」那一大块，而是把判断规则拆成三块
            # 细化好的小规则，直接按顺序拼在左侧。
            from .decision_phase import DECISION_SKELETONS, DEFAULT_DECISION_FORMAT
            # 用户 m04343 第 6 条：决策这边「角色扮演」也是第一块（跟回复页一样，人设先定调子）。
            # 用户 m02159 图3：角色扮演段锁住、可留空、可移动；决策这边默认空人格。
            modules.append(module("decision-persona", "persona", TYPE_LABELS["persona"], ""))
            for skey, sname, stext in DECISION_SKELETONS:
                modules.append(module("decision-skel-" + skey, "text", sname, "", stext))
            # 用户 m04343 第 4 条：输出规则不是框架原段、也不是只读引用——它是决策插件自己写的
            # 一份默认模板正文，跟上面三块小模块一个待遇：直接改，改完就算自定义。
            modules.append(module("decision-format", "text", TYPE_LABELS["decision_format"], "",
                                  DEFAULT_DECISION_FORMAT))
            # 用户 m02993 图7：工具段默认跟在身份段下面，两边初始模板只差这几块。
            modules.append(module("decision-identity", "identity", TYPE_LABELS["identity"]))
            # 用户 m02159 图3：工具不单独占一条模板，勾在「工具说明」这一段下面；没勾整段不注入。
            modules.append(module("decision-tools", "tools", TYPE_LABELS["tools"], ""))
        else:
            ov = self._reply_overrides() if migrate else {}
            rows = self._base_rows(sid)
            for i, row in enumerate(rows):
                if row.get("location") != "system":
                    continue
                name, key = row["name"], row["key"]
                # 用户 m02159 图1/图3：记忆 / 时间这类动态段不单独成块；身份、人格、工具
                # 各自并进规定的几块里（你的身份 / 角色扮演 / 工具说明）。
                if seg_key(name) in DYNAMIC:
                    continue
                if seg_key(name) == "accounts":
                    modules.append(module("reply-identity", "identity", TYPE_LABELS["identity"]))
                    continue
                if seg_key(name) == "persona":
                    if isinstance(ov.get(name), str):
                        modules.append(module("reply-persona-text", "text", type_label("text"), "",
                                              self._legacy_text(ov[name], name)))
                    else:
                        modules.append(module("reply-persona", "persona", TYPE_LABELS["persona"]))
                    continue
                if seg_key(name) == "tools":
                    modules.append(module("reply-tools", "tools", TYPE_LABELS["tools"]))
                    continue
                typ = "text" if isinstance(ov.get(name), str) else "framework"
                body = self._legacy_text(ov[name], name) if typ == "text" else ""
                modules.append(module("reply-%s-%d" % (key, i), typ, type_label(typ, key), key, body))
            if not rows:
                # 未取得本会话素材时只给原段引用，不把官方未绑定模板直接发送。
                for key in self._official.get("default_order", []):
                    if key not in DYNAMIC and key not in ("accounts", "persona", "tools"):
                        modules.append(module("reply-default-" + key, "framework", type_label("framework", key), key))
                modules.append(module("reply-persona", "persona", TYPE_LABELS["persona"]))
                modules.append(module("reply-identity", "identity", TYPE_LABELS["identity"]))
                modules.append(module("reply-tools", "tools", TYPE_LABELS["tools"]))
        # 用户 m02159 图1：工具说明和动态尾段之间插一块「实时窗口」，写明聊天记录拼在这里。
        modules.append(module("realtime-tail", "realtime_marker", TYPE_LABELS["realtime_marker"], "", REALTIME_NOTE))
        # 用户 m05691：决策只判断「回不回」，一个气泡都不产出，格式强化对它没有意义——决策页不再有这块。
        if kind != "decision":
            # 用户 m02993：动态段是真正的模板（默认「强化格式遵守」），不再是空占位。
            modules.append(module("dynamic-tail", "dynamic", DYNAMIC_TEMPLATE_NAME, "", DYNAMIC_TEMPLATE_DEFAULT))
        # 用户 m04343 第 3 条：动态段紧挨聊天记录（实时窗口下面第一格），系统动态段排在它后面
        # ——系统动态段是框架自己拼在 system 末尾的那些内容（会话信息 / 聊天环境 / 记忆 / 时间）。
        # 用户 m08902 第 2 条：发布版的默认提示词要跟他现在实际在用的一致。
        # 他线上是「回复页开、决策页关」：回复的那一刻才需要把框架的会话/记忆/时间搬进本条消息，
        # 决策只判断回不回，搬一遍纯属重复计费。所以默认值按 kind 分开给。
        modules.append(module("sysdyn-tail", "system_dynamic", TYPE_LABELS["system_dynamic"], "", "",
                              kind != "decision"))
        return self._relabel({"version": 1, "mode": "modules", "text": "", "modules": modules})

    def layout(self, kind, sid=""):
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision 或 reply")
        all_scopes, scope = self._data.get("layouts", {}), self._scope(sid)
        own = all_scopes.get(scope, {}).get(kind)
        if own is not None:
            return self._relabel(normalize_layout(own, kind)), False
        inherited = all_scopes.get("*", {}).get(kind) if scope != "*" else None
        if inherited is not None:
            return self._relabel(normalize_layout(inherited, kind)), True
        return self._relabel(self.default_layout(kind, sid)), bool(sid)

    def save(self, kind, sid, layout, expected_revision):
        if not isinstance(expected_revision, str) or expected_revision != self.revision():
            return {"ok": False, "code": "REVISION_CONFLICT", "error": "提示词已变更，请重新读取后再保存；当前草稿未覆盖"}
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision 或 reply")
        clean = self._relabel(normalize_layout(layout, kind)) if layout is not None else None
        new = copy.deepcopy(self._data)
        scope = self._scope(sid)
        values = new.setdefault("layouts", {}).setdefault(scope, {})
        if clean is None:
            values.pop(kind, None)
        else:
            values[kind] = clean
        if not values:
            new["layouts"].pop(scope, None)
        self._write(self.path, new)
        self._data = new  # 失败不修改内存，也不报告假成功。
        return {"ok": True, "revision": self.revision()}

    def identity_text(self, sid):
        """只取本会话的回复身份；不拼时间/会话数、不向其它会话兜底。"""
        rows = self._base_rows(sid)
        raw = next((r["text"] for r in rows if seg_key(r["name"]) == "accounts" and "你的身份" in r["text"]), "")
        layout, _ = self.layout("reply", sid)
        if layout["mode"] == "text":
            match = re.search(r"(?m)^## 你的身份（Identity）[^\n]*\n[\s\S]*?(?=^## |\Z)", layout["text"])
            return match.group(0) if match else ""
        for m in layout["modules"]:
            if m["enabled"] and m["type"] == "text" and not m["key"]:
                match = re.search(r"(?m)^## 你的身份（Identity）[^\n]*\n[\s\S]*?(?=^## |\Z)", m["content"])
                if match:
                    return match.group(0)
            if m["enabled"] and m["key"] == "accounts":
                if m["type"] == "text":
                    return m["content"]
                if m["type"] == "framework":
                    return raw
            if m["enabled"] and m["type"] == "identity":
                return raw
        return ""

    async def _context(self, sid, opt=None, layout=None):
        dp = self.plugin.decision_phase
        opt = opt or self.plugin.decision_options()
        persona = ""
        if opt.persona:
            try:
                persona = await dp.persona_text(opt.persona)
            except Exception:
                logger.exception("[multi-decision] 取已选人格失败，当前装配不加人格")
        # 用户图3：决策侧不再只认「设置里选的人格」，而是每个 persona 模块各自指向一个人格文件。
        personas = {}
        for m in (layout or {}).get("modules", []) if isinstance(layout, dict) else []:
            name = str(m.get("key") or "") if m.get("type") == "persona" else ""
            if not name or name in personas:
                continue
            try:
                personas[name] = await dp.persona_text(name)
            except Exception:
                logger.exception("[multi-decision] 读人格文件失败：%s", name)
                personas[name] = ""
        return {"persona": persona, "personas": personas, "tools": self._tools_text(opt), "opt": opt}

    def _tools_text(self, opt=None):
        """用户 m02159 图3：勾了哪个工具就写哪个，两边共用同一份勾选（prompt_tools.json）。"""
        dp = self.plugin.decision_phase
        try:
            opt = opt or self.plugin.decision_options()
            picked = dp.pick_tools(opt.tools)
        except Exception:
            logger.exception("[multi-decision] 取工具清单失败，这一段留空")
            return ""
        if not picked:
            return ""
        try:
            text = dp.tools_help(picked)
            from .decision_phase import TOOL_HEAD
            return (TOOL_HEAD % max(1, opt.tool_rounds)) + "\n" + text
        except Exception:
            logger.exception("[multi-decision] 拼工具说明失败")
            return ""

    def _render(self, kind, sid, layout, context, rows=None):
        layout = normalize_layout(layout, kind)
        if layout["mode"] == "text":
            txt = layout["text"]
            parts = [{"id": "full-text", "label": "单段文本", "type": "text", "name": "custom_system", "text": txt, "chars": len(txt)}] if txt else []
            return {"ok": True, "system": txt, "parts": parts, "chars": len(txt), "warnings": [], "source": "同源单段装配"}
        source_rows = self._base_rows(sid) if rows is None else self._keyed(rows)
        by_name = {row["key"]: row for row in source_rows}
        for row in source_rows:
            if row["name"] not in by_name:
                by_name[row["name"]] = dict(row, text="".join(r["text"] for r in source_rows if r["name"] == row["name"]))
        parts, warnings = [], []
        for m in layout["modules"]:
            # 用户 m06721 图1：关掉「启用」的段一律不进预览，系统动态段也不例外——
            # 以前为了让卡片能看见内容，这一格关着也照常渲染，结果开关关了右边还留着整段字。
            if not m["enabled"]:
                continue
            typ, key, text = m["type"], m["key"], ""
            if typ == "text":
                text = m["content"]
            elif typ == "custom":
                text = self.template_text(key)
                if not text:
                    warnings.append(m["label"] + "指向的自定义模板已被删除，这一段不注入")
                    continue
            elif typ == "decision_prompt":
                from .decision_phase import DEFAULT_DECISION_PROMPT
                text = "【判断规则】\n" + (context["opt"].prompt or DEFAULT_DECISION_PROMPT).strip()
            elif typ == "decision_format":
                # 用户图3：输出规则独立成块，不再混在决策词里。
                from .decision_phase import DEFAULT_DECISION_FORMAT
                text = DEFAULT_DECISION_FORMAT
            elif typ == "identity":
                # 用户 m02993 图3：两边的身份段本来就是同一份文件，取同一处正文。
                text = self.identity_text(sid)
            elif typ == "persona":
                # 用户 m02159 图3：角色扮演段只认「人格文件」。决策不选就留空；回复不选就沿用
                # 框架给的那一段原身（保持和以前一样，不凭空造人设）。
                if key:
                    text = (context.get("personas") or {}).get(key, "")
                    if not text:
                        warnings.append(m["label"] + "指向的人格文件读不到，这一段不注入")
                        continue
                    if not text.startswith("【人设】"):
                        text = "【人设】\n" + text
                elif kind == "decision":
                    text = context.get("persona", "")
                    if text and not text.startswith("【人设】"):
                        text = "【人设】\n" + text
                else:
                    text = next((r["text"] for _k, r in by_name.items() if seg_key(r["name"]) == "persona"), "")
            elif typ == "tools":
                # 用户 m02159 图3：工具名单不单独成项，统一注进「工具说明」这一段下面；
                # 勾过就按勾的写，没勾过才沿用框架原段（回复轮框架自己会给一段）。
                text = context.get("tools") or next((r["text"] for _k, r in by_name.items() if seg_key(r["name"]) == "tools"), "")
            elif typ == "framework":
                text = by_name.get(key, {}).get("text", "")
                if by_name.get(key, {}).get("location") == "dynamic":
                    warnings.append(m["label"] + "保持在动态尾段，不进入固定 system")
                    continue
            elif typ == "realtime_marker":
                # 用户 m02159 图1：实时窗口只是位置说明 + 一段假聊天记录，不进 system。
                parts.append({"id": m["id"], "label": m["label"], "name": "realtime", "type": typ,
                              "text": m["content"] or REALTIME_NOTE, "chars": 0, "dynamic": True})
                continue
            elif typ == "system_dynamic":
                # 用户 m03879：框架自己拼在 system 末尾的动态段（会话信息 / 聊天环境 / 记忆 / 时间）
                # 单独留一格给他看：正文照抄本轮的动态段，但不再往 system 塞（它会跟框架的重复）。
                dyn_rows = [r for r in source_rows
                            if r.get("location") == "dynamic" and str(r.get("text") or "").strip()]
                # 用户 m06046 图2/图4：回复侧这些内容框架自己会搬到本轮消息末尾
                # （dynamic_prompt_position / memory_prompt_position 都是 latest_user），插件再抄一遍
                # 就等于同一段发两遍——真机请求里「## 核心记忆 / ## 时间信息」确实出现了两次。
                # 所以回复侧只标注「框架已注入」，不重复拼；决策侧是插件自己造的请求，
                # 没有框架兜底，照旧真注入（那才是决策能看到的现场信息）。
                framed_rows, inject_rows = [], list(dyn_rows)
                if kind == "reply":
                    framed_rows = [r for r in dyn_rows if self._framed_dynamic(r)]
                    inject_rows = [r for r in dyn_rows if not self._framed_dynamic(r)]
                # 用户 m06428 图2：这一格原来显示 0 字、点开空白，可请求里明明有记忆 / 时间——
                # 正文要照实写出来（框架自己搬走的那几段也是本段的来源），只是 inject_text
                # 才排除它们，免得同一段真发两遍。
                text = "\n\n".join(r["text"] for r in framed_rows + inject_rows)
                inject_text = "\n\n".join(r["text"] for r in inject_rows)
                # 用户 m05691：这一段默认不开启；勾上就把这些内容拼到本轮消息最下面（跟回复页
                # 的动态段一个位置），排在格式强化之后——它是这一轮真正该看的现场信息。
                parts.append({"id": m["id"], "label": m["label"], "name": "system_dynamic", "type": typ,
                              "text": text, "chars": len(text), "dynamic": True,
                              "inject": bool(m["enabled"]) and bool(inject_text),
                              "inject_text": inject_text,
                              "framed": [r["name"] for r in framed_rows],
                              "sources": [r["name"] for r in dyn_rows]})
                continue

            elif typ == "dynamic":
                # 用户 m02993：动态段是能改的模板，正文拼在本轮 user 消息的最后，不进 system。
                text = m["content"] or DYNAMIC_TEMPLATE_DEFAULT
                parts.append({"id": m["id"], "label": m["label"], "name": "dynamic", "type": typ,
                              "text": text, "chars": len(text), "dynamic": True, "inject": True})
                continue
            if text == "":
                # 用户 m02159：人格留空、工具没勾都是正常状态（模块上就写着会跳过），不该刷警告。
                if typ not in BODY_TYPES and typ not in ("persona", "tools"):
                    warnings.append(m["label"] + "暂缺同会话来源，未借用其它会话或生成替身")
                continue
            name = by_name.get(key, {}).get("name", key) or ({"identity": "accounts", "persona": "persona", "tools": "tools"}.get(typ)) or m["id"]
            parts.append({"id": m["id"], "label": m["label"], "name": name, "type": typ, "text": text, "chars": len(text)})
        # 回复引用已含 Prompt.end；用户自写文本也是精确字串，不在第二处另造分隔符。
        # dynamic_marker 只是位置提示，不参与 system 拼接。
        system = ("\n\n" if kind == "decision" else "").join(p["text"] for p in parts if not p.get("dynamic"))
        return {"ok": True, "system": system, "parts": parts, "chars": len(system), "warnings": warnings,
                "source": self._ensure_source(sid).get("source", "暂无同会话回复素材")}

    async def assemble_decision(self, sid, opt=None, layout=None):
        doc = self.layout("decision", sid)[0] if layout is None else layout
        return self._render("decision", sid, doc, await self._context(sid, opt, doc))

    def preview_reply(self, sid="", layout=None):
        doc = self.layout("reply", sid)[0] if layout is None else layout
        return dict(self._render("reply", sid, doc, {"tools": self._tools_text()}), sid=sid)

    def apply_reply(self, request, sid=""):
        if not isinstance(getattr(request, "system_prompt", None), list):
            return False
        sid = normalize_sid(sid)
        previous = self._applied.get(sid)
        if previous and previous[0] is request and request.system_prompt is previous[2]:
            request.system_prompt = previous[1]
        self.capture_reply(request, sid)
        doc, _ = self.layout("reply", sid)
        from .reply_prompt import Prompt
        if Prompt is None:
            raise RuntimeError("框架没有 Prompt，不能安全应用模块")
        base, original = list(request.system_prompt), {}
        refs = self._keyed([{"name": str(getattr(p, "name", "") or "")} for p in base])
        for p, ref in zip(base, refs):
            original.setdefault(ref["name"], []).append(p)
            if ref["key"] != ref["name"]:
                original[ref["key"]] = [p]
        # 真实请求只从本轮对象取原段；绝不用持久化历史素材填补缺失原段。
        personas = {}
        for m in doc["modules"]:
            pname = str(m.get("key") or "") if m.get("type") == "persona" else ""
            if pname:
                personas[pname] = self.plugin.decision_phase.persona_text_now(pname)
        tools_text = self._tools_text()
        rendered, output = self._render("reply", sid, doc, {"personas": personas, "tools": tools_text},
                                        rows=self._rows(request)), []
        mods = {m["id"]: m for m in doc["modules"]}
        for p in rendered["parts"]:
            if p.get("dynamic"):
                # 用户 m02993：动态段不进 system，下面单独拼到本轮 user 末尾。
                continue
            m = mods.get(p["id"], {})
            key = m.get("key", "") or {"persona": "persona", "tools": "tools", "identity": "accounts"}.get(m.get("type"), "")
            if m.get("type") == "tools" and tools_text:
                # 用户 m02159 图3：勾过工具就按勾的那份重写「工具说明」这一段。
                output.append(Prompt(p["text"], name="tools", source="system", persist=False,
                                     render_template=False, end=""))
            elif m.get("type") == "persona" and m.get("key"):
                # 用户 m02159 图3：人格段自选的文件不在框架原段里，必须按新正文重造一段 Prompt。
                output.append(Prompt(p["text"], name="persona", source="system", persist=False,
                                     render_template=False, end=""))
            elif m.get("type") in ("framework", "persona", "tools", "identity"):
                output.extend(original.get(key, []))  # kwargs/render/end/persist 原样保留。
            else:
                output.append(Prompt(p["text"], name=p["name"], source="system", persist=False, render_template=False, end=""))
        selected = {p["name"] for p in rendered["parts"]}
        overrides = self._reply_overrides()
        for p in base:
            name = str(getattr(p, "name", "") or "")
            if self._dynamic(name) and name not in selected:
                # 旧自定义动态段仍交给框架迁到 user，不把历史动态快照冻结进 system。
                if isinstance(overrides.get(name), str):
                    p = Prompt(overrides[name], name=name, source="system", persist=False)
                output.append(p)
        request.system_prompt = output
        self._applied[sid] = (request, base, output)
        # 用户 m06428 图2：系统动态段展示要写全（含框架自己搬的那几段），但真拼进 user 的
        # 只能是插件该拼的那部分，所以这里读 inject_text，没有才退回 text。
        dyn_text = "\n\n".join(p.get("inject_text", p["text"]) for p in rendered["parts"]
                                if p.get("inject") and (p.get("inject_text") or p.get("text")))
        try:
            self._inject_user_dynamic(request, dyn_text)
        except Exception:
            logger.exception("[multi-decision] 动态段注入失败（本轮按没有动态段继续）")
        if len(self._applied) > 64:
            self._applied.pop(next(iter(self._applied)))
        note = getattr(getattr(self.plugin, "reply_prompt", None), "note", None)
        if callable(note):
            note(output, sid)
        return True

    def _inject_user_dynamic(self, request, text):
        """把动态段模板拼进本轮 user 消息的最末尾（就在动态段里，紧贴模型开口的位置）。

        幂等：同一个 request 反复套（重试链路）时先还原上一次，不会叠成两遍。
        """
        body = (text or "").strip()
        if not body:
            return False
        from .reply_prompt import Prompt as _Prompt
        if _Prompt is None:
            return False
        ups = getattr(request, "user_prompt", None)
        if not (isinstance(ups, list) and ups):
            return False
        prev = self._applied_user.get(id(request))
        if prev and prev[0] is request:
            try:
                request.user_prompt = list(prev[1])
                ups = request.user_prompt
            except Exception:
                logger.debug("[multi-decision] 还原上一轮动态段失败，按新的一份注入", exc_info=True)
        before = list(ups)
        try:
            piece = _Prompt(body, name="dynamic_tail", source="system", persist=False, render_template=False, end="")
            out, placed = [], False
            # 用户 m06428 图1：设置页里「强化格式遵守」排在「系统动态段」上面，注入顺序也必须是
            # 这个——先强化格式，再由框架的 <system_reminder>（记忆 / 时间）收尾，最后一条才是
            # 系统动态段。以前插在关闭标记之后，强化格式反而成了整条 user 的尾巴。
            for p in ups:
                if not placed and (getattr(p, "name", "") or "") == "dynamic_context_start":
                    out.append(piece)
                    placed = True
                out.append(p)
            if not placed:
                out.append(piece)
            request.user_prompt = out
            self._applied_user[id(request)] = (request, before)
            if len(self._applied_user) > 64:
                self._applied_user.pop(next(iter(self._applied_user)))
            return True
        except Exception:
            logger.exception("[multi-decision] 造动态段 Prompt 失败")
            return False

    async def payload(self, kind, sid="", draft=None):
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision 或 reply")
        context_sid = normalize_sid(sid) if sid else self.plugin._decision_pick_sid()
        doc, inherited = self.layout(kind, sid or context_sid)
        if not sid:
            own = self._data.get("layouts", {}).get("*", {}).get(kind)
            doc = normalize_layout(own, kind) if own is not None else self.default_layout(kind, context_sid)
            inherited = False
        if draft is not None:
            doc = normalize_layout(draft, kind)
        context = await self._context(context_sid, None, doc)
        preview = self._render(kind, context_sid, doc, context)
        reply_rows = {}
        for row in self._base_rows(context_sid):
            reply_rows[row["name"]] = reply_rows.get(row["name"], "") + row["text"]
        identity = self.identity_text(context_sid) if kind == "decision" else reply_rows.get("accounts", "")
        persona_text = context["persona"] if kind == "decision" else reply_rows.get("persona", "")
        tools_text = context["tools"] if kind == "decision" else reply_rows.get("tools", "")
        # 用户 m02159 图3：自己的模板在前、默认模板在中、框架原段一律排在最后。
        catalog = [{"type": "text", "key": "", "label": TYPE_LABELS["text"], "text": "",
                    "source": "自己写", "available": True, "group": "custom", "suffix": "自定义"}]
        for tpl in self.templates():
            catalog.append({"type": "custom", "key": tpl["id"], "label": tpl["name"], "text": tpl["content"],
                            "source": "自定义模板", "available": True, "group": "custom", "tag": "自定义",
                            "suffix": "自定义"})
        dp = self.plugin.decision_phase
        # 原段（框架现成的那几段）留到最后再拼，保证下拉里它们永远排在默认模板后面。
        tail = []
        if kind == "decision":
            from .decision_phase import DEFAULT_DECISION_FORMAT, DECISION_SKELETONS
            # 用户 m02159 图2：「决策提示词」整块不再出现在下拉里，改用下面三块细化规则。
            # 用户 m04343 第 4 条：输出规则跟三块小模块一样是「默认模板」——改正文就变自定义。
            catalog.append({"type": "decision_format", "key": "", "label": TYPE_LABELS["decision_format"],
                            "text": DEFAULT_DECISION_FORMAT, "source": "决策插件自带的默认输出规则",
                            "available": True, "group": "default", "tag": "判断格式",
                            "suffix": "默认模板", "skeleton": True})
            for skey, sname, stext in DECISION_SKELETONS:
                catalog.append({"type": "text", "key": "skel-" + skey, "label": sname, "text": stext,
                                "source": "可拆的小模块骨架", "available": True, "group": "custom",
                                "tag": "默认模板", "suffix": "默认模板", "skeleton": True})
        else:
            src = self._ensure_source(context_sid)
            native = {x["name"]: x for x in src.get("native", [])}
            for row in self._base_rows(context_sid):
                name, key = row["name"], row["key"]
                # 用户 m02159 图1：记忆 / 时间这类动态段不进下拉；身份 / 人格 / 工具已各有专块。
                if seg_key(name) in DYNAMIC or seg_key(name) in ("accounts", "persona", "tools"):
                    continue
                tail.append({"type": "framework", "key": key, "label": type_label("framework", key),
                             "text": row["text"], "template": native.get(key, {}).get("template", ""),
                             "source": src.get("source", ""), "available": True, "group": "default",
                             "tag": "框架原段", "suffix": "原段",
                             "note": "原段引用，真实请求取本轮原文"})
        # 用户 m02159 图3：人格段的下拉不是「选模板」而是「选人格」——两边都只列人格文件。
        try:
            for item in await dp.persona_options():
                catalog.append({"type": "persona", "key": str(item.get("name") or ""),
                                "label": TYPE_LABELS["persona"], "text": str(item.get("preview") or ""),
                                "source": "人设页面", "available": True, "group": "default",
                                "tag": str(item.get("name") or ""), "suffix": "人格"})
        except Exception:
            logger.exception("[multi-decision] 列人格文件失败")
        catalog.append({"type": "tools", "key": "", "label": TYPE_LABELS["tools"], "text": tools_text,
                        "source": "勾选注入：清单写在这一段下面",
                        "available": True, "group": "default", "tag": "工具", "suffix": "工具"})
        catalog.append({"type": "identity", "key": "", "label": TYPE_LABELS["identity"], "text": identity,
                        "source": "框架自动填写，只读" if kind == "decision" else "同会话框架身份原段",
                        "available": True, "group": "default", "tag": "只读", "suffix": "只读"})
        # 用户 m02159 图3：原段一律排在默认模板后面（下拉里永远垫底）。
        catalog.extend(tail)
        catalog.append({"type": "realtime_marker", "key": "", "label": TYPE_LABELS["realtime_marker"],
                        "text": REALTIME_NOTE, "source": "位置说明，不注入", "available": True,
                        "group": "default", "tag": "不注入", "suffix": "位置"})
        catalog.append({"type": "system_dynamic", "key": "", "label": TYPE_LABELS["system_dynamic"],
                        "text": "", "source": "默认关着；启用后拼在本轮消息最下面", "available": True,
                        "group": "default", "tag": "位置", "suffix": "位置"})
        if kind != "decision":
            # 用户 m05691：决策不需要格式强化（它不产出气泡），所以这一项只在回复页出现。
            catalog.append({"type": "dynamic", "key": "", "label": DYNAMIC_TEMPLATE_NAME,
                            "text": DYNAMIC_TEMPLATE_DEFAULT, "source": "动态段：拼在本轮聊天记录最后",
                            "available": True, "group": "default", "tag": "动态段", "suffix": "动态段"})
        choices, picked = [], []
        try:
            # 用户 m02159 图3：工具是全局一份（prompt_tools.json），决策和回复两边都勾同一份，
            # 所以两个页面都要给勾选框，不能只有决策页能勾。
            choices = dp.tool_catalog()
            picked = list(context["opt"].tools)
        except Exception:
            logger.exception("[multi-decision] 列工具失败")
        return {"ok": True, "kind": kind, "sid": sid, "context_sid": context_sid, "layout": doc,
                "default_layout": self.default_layout(kind, context_sid, migrate=False), "catalog": catalog,
                "revision": self.revision(), "inherited": inherited, "preview": preview,
                "native_templates": self._ensure_source(context_sid).get("native", []),
                "official_reference": copy.deepcopy(self._official),
                "tool_choices": choices, "tools_selected": picked,
                "templates": self.templates()}
