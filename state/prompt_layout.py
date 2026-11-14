"""有序提示词装配：预览与实际请求同源，原生 Prompt 不重建、不接管发送流程。"""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from .sids import bot_name, normalize_sid

logger = logging.getLogger(__name__)
# 用户 m06706：JEV 是第三类提示词（jev），跟决策 / 回复各存各的，谁也覆盖不了谁。
KINDS = {"decision", "reply", "jev"}


# custom = 引用「自定义模板库」里的一条；realtime_marker / dynamic_marker 只标位置、不注入 system。
# JEV 页的判断规则跟决策页一样是「自写文本」块，正文就在布局里，不另设引用型。
TYPES = {"text", "custom", "identity", "framework", "decision_prompt",
         "decision_format", "persona", "tools", "realtime_marker", "dynamic_marker",
         "dynamic", "system_dynamic", "system_memory", "system_time",
         # 用户 m21698：默认记忆插件追加的两段也进布局，跟别的段一样能看见、能挪。
         "plugin_memory", "plugin_memory_ctx"}
DYNAMIC = {"sessions", "chat_env", "memory", "time"}
#: 用户 m11025：这两段框架的环境信息在真实请求里已被 _drop_noisy_prompts 删掉
#: （decision/engine.py 的 _NOISY_PROMPT_NAMES），系统动态段再显示出来就是
#: 「早就删掉的东西又回来了」。预览和真实请求必须是同一份。
NOISY_NAMES = ("sessions", "chat_env")
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
               "system_dynamic": "系统动态段", "system_memory": "记忆段",
               "system_time": "时间段",
               "plugin_memory": "记忆插件注入",
               "plugin_memory_ctx": "记忆插件 · 本轮上下文"}
# 身份段由框架自动填好，只让看、不让改（用户图6）。
READONLY_TYPES = {"identity", "system_dynamic", "system_memory", "system_time",
                  "plugin_memory", "plugin_memory_ctx"}
# 只有这几种存正文；其余都是「引用」，保存时不落正文，所以永远覆盖不了默认模板。
BODY_TYPES = {"text", "custom", "realtime_marker", "dynamic"}
# 只标位置、不进 system 的两种占位（用户 m02159 图1）：实时窗口 + 动态段。
# dynamic_marker 留在 TYPES 里只为兼容旧文档，读到就丢掉（用户 m02993：动态段改成真模板）。
MARKER_TYPES = {"realtime_marker", "dynamic", "system_dynamic", "system_memory", "system_time",
                "plugin_memory", "plugin_memory_ctx"}

# 用户 m10789：format 模板里有 {{ message_types }} / {{ root_tags }}，真实请求要等框架在 assemble
# 阶段把标签清单填进去才渲染得出来。预览里先填一份：能用上最近一次真实请求的值就用真的，
# 没有就先写一句说明，别让整段消失（原来 StrictUndefined 一抛，整段 format 就没了）。
_TAG_FILL = {"message_types": "", "root_tags": ""}
_TAG_FILL_NOTE = "（这一段由框架按当前语言与适配器支持的标签自动填写）"
DYNAMIC_NOTE = ("（这一段不注入固定 system：会话信息、聊天环境、记忆、时间会由框架拼在"
                "实时窗下方，实时窗本身又接在系统提示词后面。）")
# 用户 m02993：动态段默认就是这段「强化格式遵守」——拼在本轮聊天记录最后，紧贴模型开口的位置，
# 专门提醒模型把回复包进 <msg>（记录里出现过只回裸文本、发不出去的情况）。
DYNAMIC_TEMPLATE_DEFAULT = (
    "## 格式强化（Format）\n"
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

# 用户 m23028：记忆插件那两段的正文是插件在请求时现算的（只读类型不落正文），抓不到真实文本时
# 卡片会空成 0 字。这两份默认模板只在预览里顶位——真发请求时没有真实正文就不注入。
PLUGIN_MEMORY_DEFAULT = (
    "使用 memory_add 保存稳定事实（semantic）、重要经历（episodic）或明确的互动约定（procedural）。\n"
    "保留原语言，不编造事实、经历、偏好或人格设定。owner_type 为 user、group 或 self。\n"
    "群聊里关于某人的信息仍属于 user，群体共同信息属于 group；self 属于当前人格。\n"
    "新记忆仅在当前会话可见。批次有多个发言者时，用户记忆必须指定 user_id，所有写入、修改和删除\n"
    "必须指定 source_message_id。标识须从收到的消息或下方上下文复制，不能根据昵称猜测用户。\n"
    "添加可能重复的内容前，以及需要回忆但上下文不足时，使用 memory_search。\n"
    "使用简短的原语言关键词检索，偶尔跨语言时自行翻译关键词。空查询列出可见记忆，offset 用于分页；\n"
    "include_inactive 可找回已完成、已取消、已被替代或已过期的记忆，memory_id 可获取单条可见记忆。\n"
    "更新和删除使用稳定 memory_id 及查询返回的 revision；发生冲突后重新读取。\n"
    "core=true 仅用于少量稳定且必要的信息，它是加载偏好，不是内容分类。\n"
    "计划和临时状态可用 status 和 expires_at（Unix 秒）管理；记下计划不等于设置提醒。\n"
    "优先更新已有事实，同时保留有意义且标明日期的经历。"
)
# 本轮上下文没抓到时的骨架：形状跟插件每轮追加的那份 JSON 一致。
PLUGIN_MEMORY_CTX_DEFAULT = (
    '{\n'
    '  "persona_id": "default",\n'
    '  "session_id": "group:1145141974",\n'
    '  "sources": [\n'
    '    {"source_message_id": "63164553", "user_id": "3212133758"}\n'
    '  ]\n'
    '}'
)

# 用户 m13442 第 4 条：框架 output 段里把「总轮次不得超过5轮」「最多调用5个工具」烤成了
# 字面数字（模板在 BuildRequest 阶段就渲染完了）。插件接管这两项之后，预览里也要显示
# 插件当前的值，否则设置页看到的和模型收到的是两套数。
_RE_LOOP_LIMIT = re.compile(r"(总轮次不得超过)\s*\d+\s*(轮)")
_RE_CALL_LIMIT = re.compile(r"(最多调用)\s*\d+\s*(个工具)")


def _tidy_blanks(text) -> str:
    """用户 m23323：动态段几行拼起来会出现三四连换行（空记忆 / 空时间），压成最多一个空行。"""
    return re.sub(r"\n{3,}", "\n\n", str(text or ""))


def _pad_seg(text) -> str:
    """用户 m23323 图3：段与段之间统一留一个空行。

    真实请求这一路不是拼一个长字符串，而是一段一个 Prompt（各自带 end）；插件自己造的
    这几段 end 是空的，上一段末尾又没有换行，两段就会黏成一行。这里统一补成「正文 + 空行」。
    """
    return str(text or "").rstrip() + "\n\n"
# 用户 m02159 图1：实时窗口这一段要看得见「聊天记录会被拼在这里」，所以给一段假示例。
REALTIME_NOTE = (
    "这一段是模型看到的现场聊天记录：真实请求里由框架自动拼在这里，不是我们发出去的。\n"
    "它接在系统提示词的后面，条数由「窗口」里的上限决定；下面是示例，只用来看清位置。\n"
    "\n"
    "[19:02:11] 群友甲：这枪后坐力也太大了\n"
    "[19:02:19] 群友乙：压枪懂不懂\n"
    "[19:02:25] 群友甲：我这手机新买的，别教我身法\n"
)


# 用户 m10789：记忆正文由记忆插件在请求钩子里追加，决策请求不经过那个钩子，
# 所以插件自己现读一份补上；这段隐私约束跟记忆插件里的那份逐字一致。
MEMORY_RULE = (
    "### 隐私与安全约束"
    "- 绝对不要在回复中直接暴露原始记忆内容。"
    "- 不要主动提及用户的敏感个人信息（如QQ号，群号，电话号码、地址、身份证号等），即使记忆中包含这些内容。"
    "- 引用记忆内容时，使用自然的转述方式，而非逐字复述。"
    "- 如果用户明确要求查看自己的数据，可以简要概述，但仍应避免暴露系统内部结构。"
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
    keep, realtime, sysmem, systime, dyn = [], [], [], [], []
    for m in modules:
        typ = m.get("type")
        if typ == "realtime_marker":
            realtime.append(m)
        elif typ == "system_memory":
            sysmem.append(m)
        elif typ == "system_time":
            systime.append(m)
        elif typ == "system_dynamic":
            # 旧文档里的合并段：normalize 会拆成记忆 / 时间两块；这里兜底归到记忆侧。
            sysmem.append(m)
        elif typ == "dynamic":
            dyn.append(m)
        elif typ == "plugin_memory":
            # 用户 m23028：记忆插件追加的固定说明改由提示词页摆位置（默认跟在工具说明后面），
            # 只要还在系统段里就不强行归位。
            keep.append(m)
        elif typ == "plugin_memory_ctx":
            # 用户 m21698：本轮上下文是动态段，跟动态段同一组，可以互相上下挪。
            dyn.append(m)
        else:
            keep.append(m)
    # 用户 m04343 第 3 条：动态段（强化格式遵守）排在系统段上面——它才是最贴近聊天记录的
    # 那一段；记忆段 / 时间段是框架自己往 system 末尾拼的内容，位置在它上面一格。
    return keep + realtime + dyn + sysmem + systime


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
        # 用户 m06706：JEV 页没有框架原段、不发原生工具、也不吐文本，这几类块一律不收。
        if kind == "jev" and typ in ("framework", "tools", "decision_prompt", "decision_format"):
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
        if typ == "system_dynamic":
            # 旧文档里的合并段：拆成记忆段 + 时间段，id 各加后缀，启停沿用旧值。
            enabled = m.get("enabled", True)
            for suffix, new_typ in (("-mem", "system_memory"), ("-time", "system_time")):
                new_id = mid + suffix
                n = 2
                while new_id in ids:
                    new_id = mid + suffix + "-" + str(n)
                    n += 1
                ids.add(new_id)
                clean.append(module(new_id, new_typ, TYPE_LABELS[new_typ], "", "", enabled))
            continue
        # 只有自写文本存正文；引用型只记「引用到哪」，所以保存不可能覆盖默认模板（用户图2）。
        key = raw_key[:120] if typ in ("framework", "persona", "custom") else ""
        body = body if typ in BODY_TYPES else ""
        if typ in READONLY_TYPES:
            key, body = "", ""
        # 名字跟着模板走（用户图1/图3）；只有自写文本的名字是玩家自己起的，保留下来
        # （用户 m02159 图2 拆出来的「什么时候接 / 什么时候别接 / 节奏与分寸」就靠这个）。
        raw_label = str(m.get("label") or "").strip()
        label = raw_label[:120] if (typ in ("text", "dynamic") and raw_label) else type_label(typ, key)
        enabled = m.get("enabled", True)
        # v4.84（用户 m09179 第 2 条）：以前决策 / JEV 页的人格段没选文件就强制停用，
        # 界面上表现成「一打勾就被取消」。现在允许单纯打勾——没选文件时这一段是空的，
        # 等于什么都没注入（v4.89 用户 m14715 第 1 条：旧 decision_persona 已删除）。
        clean.append(module(mid, typ, label, key, body, enabled))
    # 用户 m25796：保存 / 读取时不再按类型强制归位——记忆段、时间段、插件段的位置由提示词页
    # 自己决定，一保存就复原等于白挪。默认顺序仍由 default_layout 给。
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
        # 用户 m10789：装配用的原段一律「此刻现造」，这一格放本次现取的结果（按 sid）。
        self._live = {}
        # 用户 m23323：人格正文在这一轮装配前 await 预取的那一份（同步装配时兜底用）。
        self._personas_pre = {}
        self._official = self._load(Path(__file__).with_name("kira_default_templates.json"), {})
        # 用户 m23323：默认提示词本地存一份，装配只读这一份，不再「请求时才现取」。
        # 框架以后换了默认词，用设置页工具栏的「同步框架默认」重新抓一次写回这里；
        # 玩家自己改过的正文存在 prompt_layouts.json，跟这份默认互不影响。
        self.defaults_path = Path(data_dir) / "prompt_defaults.json"
        self._defaults = self._load(self.defaults_path,
                                   {"version": 1, "captured_at": 0, "order": [], "segments": {}, "meta": {}})
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

    # ---------- 提示词页工具勾选：决策链一份、回复链一份（用户 m11103） ----------

    def selected_tools(self):
        """决策链勾选的工具；没存过就是 None（跟着设置页那份走）。"""
        got = (self._tools or {}).get("names")
        if not isinstance(got, list):
            return None
        return [str(x) for x in got if str(x).strip()]

    def reply_selected_tools(self):
        """回复链勾选的工具；还没在提示词页勾过就是 None（这时回复链仍挂框架全量）。"""
        got = (self._tools or {}).get("reply_names")
        if not isinstance(got, list):
            return None
        return [str(x) for x in got if str(x).strip()]

    def reply_known_tools(self):
        """回复链见过的工具名（用户 m11533：回复链新工具自动勾上、决策链手动勾）。

        记这一份是为了分清两件事：名字在 known 里却不在勾选里＝用户自己取消的，不再自动加回；
        名字连 known 都没有＝后来新装的插件带的工具，下次出现就自动勾上。
        """
        got = (self._tools or {}).get("reply_known")
        if not isinstance(got, list):
            return None
        return [str(x) for x in got if str(x).strip()]

    @staticmethod
    def _clean_tool_names(names, cap):
        clean = []
        for x in (names or []):
            name = str(x).strip()[:80]
            if name and name not in clean:
                clean.append(name)
        return clean[:cap]

    def _save_tools(self, names, reply_names, reply_known=None):
        """v3 起一个文件装三份：names 管决策链，reply_names 管回复链，reply_known 记回复链见过的工具。"""
        new = {"version": 3, "names": names}
        if reply_names is not None:
            new["reply_names"] = reply_names
            known = reply_known
            if known is None:
                known = self.reply_known_tools()
            if known is not None:
                new["reply_known"] = known
        self._write(self.tools_path, new)
        self._tools = new

    def set_tools(self, names):
        clean = self._clean_tool_names(names, 50)
        self._save_tools(clean, self.reply_selected_tools())
        return clean

    def set_reply_tools(self, names):
        clean = self._clean_tool_names(names, 80)
        # known 要把「以前勾过的」也算进去：v2 老文件没有 known，用户取消一个工具时
        # 若只记当前勾着的，那个被取消的下一轮就会被当成新工具自动加回来。
        known = self._clean_tool_names(
            list(self.reply_known_tools() or []) + list(self.reply_selected_tools() or []) + clean, 80)
        self._save_tools(list(self.selected_tools() or []), clean, known)
        return clean

    def reply_tools_for(self, available=None):
        """回复链真正要挂的工具名；None＝还没初始化（保持框架全量，不过滤）。

        用户 m11533 定的口径：回复链新工具自动勾上，决策链要手动勾。
        - 从没初始化过：把当时框架里的全部工具名写进去（默认全选，升级后行为不变）。
        - 已经勾过：框架里新冒出来的名字（不在 reply_known 里）自动补进勾选；
          用户自己取消过的名字在 reply_known 里，不会被自动加回来。
        """
        got = self.reply_selected_tools()
        avail = self._clean_tool_names(available, 80)
        if got is None:
            if not avail:
                return None
            return self.set_reply_tools(avail)
        if not avail:
            return got
        known = self.reply_known_tools()
        if not known:
            # 没有 known（v2 老文件）或 known 是空表：当时勾着的都算见过；
            # 空表＝用户主动全取消，也按「都见过」处理，别把刚取消的全加回来。
            known = list(got) if got else list(avail)
        fresh = [n for n in avail if n not in known]
        if not fresh:
            return got
        merged = self._clean_tool_names(list(got) + fresh, 80)
        known = self._clean_tool_names(list(known) + fresh, 80)
        self._save_tools(list(self.selected_tools() or []), merged, known)
        logger.info("[multi-decision] 回复工具清单自动补上 %d 个新工具：%s",
                    len(fresh), ",".join(fresh))
        return merged

    @staticmethod
    def _ensure_persona(kind, doc):
        """用户 m28586 第 1 条：回复页的「角色扮演」整块被弄丢过（保存后人格段凭空消失）。

        这里在读的时候兜底：只要有模块编排、却一块 persona 都没有，就按默认位置补一块空白人格段
        ——决策页排第一（人设先定调子），回复页紧跟在「角色设定」后面。补出来的这一块跟默认的
        一样只读：正文永远取人设文件，界面上改不动。
        """
        if not isinstance(doc, dict) or doc.get("mode") != "modules":
            return doc
        mods = doc.get("modules")
        if not isinstance(mods, list):
            return doc
        def has_persona(m):
            if not isinstance(m, dict):
                return False
            if str(m.get("type") or "") == "persona":
                return True
            # 旧布局的 persona 可能是文本覆盖，或框架原段里的 persona。
            if str(m.get("id") or "") == "reply-persona-text":
                return True
            return (str(m.get("type") or "") == "framework"
                    and seg_key(m.get("key")) == "persona")

        if any(has_persona(m) for m in mods):
            return doc
        insert_at = 0
        if kind == "reply":
            for i, m in enumerate(mods):
                if isinstance(m, dict) and str(m.get("type") or "") == "framework":
                    insert_at = i + 1
                    break
        used = {str(m.get("id") or "") for m in mods if isinstance(m, dict)}
        # 用户 m06706：JEV 页跟决策页一样，人设排第一块。
        prefix = ("reply-persona" if kind == "reply"
                  else ("jev-persona" if kind == "jev" else "decision-persona"))
        mid = prefix
        if mid in used:
            mid = mid + "-restored"
            n = 2
            while mid in used:
                mid = "%s-restored%d" % (prefix, n)
                n += 1
        out = dict(doc)
        new_mods = list(mods)
        # 用户 m16925：补出来的空人格段同样默认停用（回复页例外：空人格会回落到框架原文）。
        new_mods.insert(insert_at, module(mid, "persona", TYPE_LABELS["persona"], "", "", kind == "reply"))
        # 用户 m25796：补人格段只插这一块，不再整份重排（重排会抹掉用户自己挪过的位置）。
        out["modules"] = new_mods
        return out

    @staticmethod
    def _ensure_decision_format(kind, doc):
        """用户 m36396：老布局里没有「强化格式遵守」这一块，读的时候补上，默认开着。

        只补决策页（回复页本来就有），也只补一块；补出来的位置由 tail_order 决定
        ——紧挨聊天记录，在系统动态段前面，跟回复页一个位置。
        """
        if kind != "decision" or not isinstance(doc, dict) or doc.get("mode") != "modules":
            return doc
        mods = doc.get("modules")
        if not isinstance(mods, list):
            return doc
        if any(str(m.get("type") or "") == "dynamic" for m in mods if isinstance(m, dict)):
            return doc
        used = {str(m.get("id") or "") for m in mods if isinstance(m, dict)}
        mid = "decision-format-hard"
        n = 2
        while mid in used:
            mid = "decision-format-hard-%d" % n
            n += 1
        from .decision_phase import DECISION_FORMAT_HARD
        out = dict(doc)
        # 用户 m25796：补出来的「强化格式遵守」插在系统段（记忆 / 时间）前面，不再整份重排——
        # 重排会把用户自己挪过的记忆段 / 时间段 / 插件段位置一起抹掉。
        new_mods = list(mods)
        at = len(new_mods)
        for i, m in enumerate(new_mods):
            if isinstance(m, dict) and str(m.get("type") or "") in ("system_memory", "system_time", "system_dynamic"):
                at = i
                break
        new_mods.insert(at, module(mid, "dynamic", DYNAMIC_TEMPLATE_NAME, "", DECISION_FORMAT_HARD))
        out["modules"] = new_mods
        return out

    @staticmethod
    def _ensure_decision_role(kind, doc):
        """2026-11-08（J）：最上面那块「角色设定」老布局里没有，读的时候补上。

        跟 persona 一样是兜底：存过布局的会话（包括按群单独存过的那一份）也能看到
        「我不是 bot，我只负责判断」这一句。正文可以改，改完 id 不变就不再补。
        决策页与 JEV 页各有一块，正文不一样（JEV 只给概率、不写回复内容）。
        """
        if kind not in ("decision", "jev") or not isinstance(doc, dict) or doc.get("mode") != "modules":
            return doc
        mods = doc.get("modules")
        if not isinstance(mods, list):
            return doc
        for m in mods:
            if not isinstance(m, dict):
                continue
            if str(m.get("label") or "").strip() == "角色设定":
                return doc
        if kind == "jev":
            from .jev_phase import JEV_ROLE as role_text
            base = "jev-role"
        else:
            from .decision_phase import DECISION_ROLE as role_text
            base = "decision-role"
        used = {str(m.get("id") or "") for m in mods if isinstance(m, dict)}
        mid, n = base, 2
        while mid in used:
            mid = "%s-%d" % (base, n)
            n += 1
        out = dict(doc)
        out["modules"] = [module(mid, "text", "角色设定", "", role_text)] + list(mods)
        return out

    def _prepared(self, kind, doc):
        """读布局时统一兜底：缺人格补人格、缺格式强化补格式强化、缺角色设定补角色设定。"""
        return self._relabel(self._ensure_plugin_segments(
            kind, self._ensure_decision_role(
                kind, self._ensure_decision_format(kind, self._ensure_persona(kind, doc)))))

    def _ensure_plugin_segments(self, kind, doc):
        """用户 m21698：默认记忆插件开着时，把它追加的两段并进布局。

        固定说明（「使用 memory_add …」那一大段）跟在工具说明后面，只读、位置可以挪；
        本轮上下文（每轮都变的 {"persona_id"…}）放进动态段，只读、可以上下挪。
        插件关掉时这两段从布局里摘掉，页面上不留空卡。
        """
        if kind != "reply" or not isinstance(doc, dict) or doc.get("mode") != "modules":
            return doc
        mods = doc.get("modules")
        if not isinstance(mods, list):
            return doc
        on = self._memory_plugin_on()
        has_static = any(str(m.get("type") or "") == "plugin_memory" for m in mods if isinstance(m, dict))
        has_ctx = any(str(m.get("type") or "") == "plugin_memory_ctx" for m in mods if isinstance(m, dict))
        if not on:
            if not (has_static or has_ctx):
                return doc
            out = dict(doc)
            out["modules"] = [m for m in mods
                              if str((m or {}).get("type") or "") not in ("plugin_memory", "plugin_memory_ctx")]
            return out
        if has_static and has_ctx:
            return doc
        used = {str(m.get("id") or "") for m in mods if isinstance(m, dict)}
        new_mods = list(mods)
        for typ, base_id, done in (("plugin_memory", "plugin-memory", has_static),
                                   ("plugin_memory_ctx", "plugin-memory-ctx", has_ctx)):
            if done:
                continue
            mid, n = base_id, 2
            while mid in used:
                mid = "%s-%d" % (base_id, n)
                n += 1
            used.add(mid)
            new_mods.append(module(mid, typ, TYPE_LABELS[typ], "", ""))
        # 用户 m23028：新补的这两块默认落在「工具说明」后面（固定说明）与动态段末尾（本轮上下文），
        # 之后由提示词页自己摆。
        if not has_static:
            added = [m for m in new_mods if str(m.get("type") or "") == "plugin_memory"]
            if added:
                rest = [m for m in new_mods if str(m.get("type") or "") != "plugin_memory"]
                at = len(rest)
                for i, m in enumerate(rest):
                    if str(m.get("type") or "") == "tools":
                        at = i + 1
                new_mods = rest[:at] + added + rest[at:]
                new_mods = rest[:at] + added + rest[at:]
        # 用户 m25796：本轮上下文补在动态段末尾（系统段前面），不再靠整份重排归位。
        if not has_ctx:
            added = [m for m in new_mods if str(m.get("type") or "") == "plugin_memory_ctx"]
            if added:
                rest = [m for m in new_mods if str(m.get("type") or "") != "plugin_memory_ctx"]
                at = len(rest)
                for i, m in enumerate(rest):
                    if str(m.get("type") or "") in ("system_memory", "system_time", "system_dynamic"):
                        at = i
                        break
                new_mods = rest[:at] + added + rest[at:]
        # 用户 m25796：补插件段只插这两块，不再整份重排。
        out["modules"] = new_mods
        return out

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
        try:
            return str(fn() if callable(fn) else getattr(p, "content", "") or "")
        except Exception:
            if str(getattr(p, "name", "") or "") != "format":
                raise
            kw = getattr(p, "kwargs", None)
            if not isinstance(kw, dict):
                return str(getattr(p, "content", "") or "")
            filled = {"message_types": _TAG_FILL.get("message_types") or _TAG_FILL_NOTE,
                      "root_tags": _TAG_FILL.get("root_tags") or ""}
            added = {k: v for k, v in filled.items() if k not in kw}
            kw.update(added)
            try:
                return str(fn())
            finally:
                for k in added:
                    kw.pop(k, None)

    @staticmethod
    def _remember_tag_fill(request):
        """记下真实请求里框架填给 format 段的标签清单，预览时直接用真的。"""
        for p in getattr(request, "system_prompt", None) or []:
            if str(getattr(p, "name", "") or "") != "format":
                continue
            kw = getattr(p, "kwargs", None)
            if isinstance(kw, dict):
                mt = kw.get("message_types")
                if isinstance(mt, str) and mt.strip():
                    _TAG_FILL["message_types"] = mt
                rt = kw.get("root_tags")
                if isinstance(rt, str):
                    _TAG_FILL["root_tags"] = rt
            return

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

    # 用户 m10624：提示词页不该去「挑一个跑过的会话」才有内容。
    # 没跑过真实请求的会话，界面用这份出厂样例量尺寸；真实请求永远只取本轮原文，
    # 绝不把样例当原文拼进 system（kira_default_templates.json 的 notes 明令禁止冒充）。
    # 用户 m10789：原来这里放「出厂样例」的来源角标，现在预览一律取请求这一刻的框架模板，
    # 只有现取失败才退回插件自带文案，所以不再需要这个常量。

    def _sample_identity(self, sid):
        sid = normalize_sid(sid)
        group = sid.startswith("gm:")
        plugin = self.plugin
        def pick(attr):
            try:
                return str(getattr(plugin, attr, "") or "").strip()
            except Exception:
                return ""
        def eff(key):
            try:
                val = plugin.eff(key, sid)
            except Exception:
                return ""
            if isinstance(val, (list, tuple)):
                return "、".join(str(x).strip() for x in val if str(x).strip())
            return str(val or "").strip()
        qq = pick("bot_qq") or "（未设置）"
        try:
            name = str(plugin.bot_name_for(sid) or "").strip()
        except Exception:
            name = ""
        nick = eff("nicknames") or "（未设置）"
        admin_name = pick("admin_name") or "（未设置）"
        # admin_list 是 JSON 字符串（[{"name": .., "qq": ..}]），别把这段 JSON 直接写进身份段。
        admin_id = pick("admin_id")
        if not admin_id:
            raw_list = eff("admin_list")
            items = None
            if raw_list[:1] == "[":
                try:
                    items = json.loads(raw_list)
                except Exception:
                    items = None
            if isinstance(items, list):
                admin_id = "、".join(str(x.get("qq") or "").strip() for x in items
                                       if isinstance(x, dict) and str(x.get("qq") or "").strip())
            else:
                admin_id = raw_list
        admin_id = admin_id or "（未设置）"
        where = "群里" if group else "好友里"
        lines = ["## 你的身份（Identity）",
                 "- 当前平台：`QQ`",
                 "- 当前聊天类型：`%s`" % ("GroupMessage" if group else "FriendMessage"),
                 "- 你的账号 ID：`%s`" % qq,
                 "- 你在%s的昵称：%s" % (where, name or "（未设置）"),
                 "- 你在%s的外号：%s" % (where, nick),
                 "- 管理员名字：%s" % admin_name,
                 "- 管理员 QQ 号：%s" % admin_id]
        return chr(10).join(lines) + chr(10)

    @staticmethod
    def _now_text():
        """框架同款时间串（Oct 29 2026 01:53 Thu），时区跟框架配置走。"""
        tz = None
        try:
            cfg = getattr(getattr(self.plugin, "ctx", None), "config", None)
            tz = cfg.get_timezone() if cfg is not None and hasattr(cfg, "get_timezone") else None
        except Exception:
            tz = None
        try:
            now = datetime.now(tz) if tz else datetime.now()
        except Exception:
            now = datetime.now()
        return now.strftime("%b %d %Y %H:%M %a")

    def _live_time_text(self):
        return "## 时间信息（Time）" + chr(10) + "当前时间是：" + self._now_text() + chr(10)

    def _core_memory_text(self):
        """现读核心记忆（跟记忆插件同一个文件），读不到就当这个会话没有记忆。"""
        try:
            from core.utils.path_utils import get_data_path
            path = Path(get_data_path()) / "memory" / "core.txt"
            if not path.is_file():
                return ""
            lines = [str(ln).rstrip(chr(10)) for ln in path.read_text(encoding="utf-8").splitlines()]
        except Exception:
            return ""
        if not lines:
            return ""
        body = "".join("[%d] %s" % (i, ln) + chr(10) for i, ln in enumerate(lines))
        return body + MEMORY_RULE

    def _captured_memory_text(self, sid):
        """真实回复请求里记忆插件追加过的那段（prompt_sources.json 抄下来的）。

        用户 m20353：新版记忆插件不再写 core.txt，预览 / 决策链只读 core.txt 就只剩标题。
        这里退回上一轮真实回复请求里抄下来的记忆段（规则 + 记录），比只剩标题准。
        """
        src = self._sources.get(normalize_sid(sid)) or {}
        for row in src.get("rows") or []:
            if seg_key(row.get("name")) == "memory":
                text = str(row.get("text") or "").strip()
                if text and text != "## 核心记忆（Core Memory）":
                    return text
        return ""

    def _fill_memory(self, rows, sid=""):
        """框架那份 memory 段只有标题，正文是记忆插件在请求钩子里追加的，这里补上。"""
        core = self._core_memory_text()
        captured = "" if core else self._captured_memory_text(sid)
        if not core and not captured:
            return rows
        out = []
        for row in rows:
            if seg_key(row.get("name")) == "memory" and "### 隐私与安全约束" not in str(row.get("text") or ""):
                if core:
                    row = dict(row, text=str(row.get("text") or "") + chr(10) + core)
                else:
                    row = dict(row, text=captured)
            out.append(row)
        return out

    def _captured_tools_extra(self, sid):
        """上一轮真实回复请求里，记忆插件追加在 tools 段末尾的那段（含每轮上下文）。"""
        def pick(src):
            for row in (src or {}).get("rows") or []:
                if seg_key(row.get("name")) == "tools":
                    got = self._tools_extra_text(row.get("text"))
                    if got:
                        return got
            return ""
        got = pick(self._sources.get(normalize_sid(sid)))
        if got:
            return got
        for src in self._sources.values():
            got = pick(src)
            if got:
                return got
        return ""

    def _memory_plugin_on(self):
        """默认记忆插件（kira_plugin_simple_memory）现在开着没有。

        用户 m20823 第 5 条：开着它才会往「工具说明」段追加说明，提示词页要跟着多一段。
        读框架的插件开关文件，读不到就当没开。
        """
        try:
            from core.utils.path_utils import get_config_path
            path = Path(get_config_path()) / "plugins.json"
            if not path.is_file():
                return False
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
            return bool(raw.get("kira_plugin_simple_memory"))
        except Exception:
            return False

    def plugin_segments(self, kind, sid=""):
        """别的插件在请求时自己加进来的段（目前只有默认记忆），给提示词页显示用。

        用户 m20823 第 5 条：开了默认记忆，提示词拼装页要能看见它加的那段，
        不然页面上永远只有框架那几段，看不出实际发出去的 system 多了什么。
        """
        if kind != "reply" or not self._memory_plugin_on():
            return []
        static, dynamic = self._split_tools_extra(self._captured_tools_extra(sid))
        out = []
        if static:
            out.append({"name": "plugin_memory", "label": "记忆插件注入", "tag": "默认段",
                        "text": static, "chars": len(static), "dynamic": False,
                        "source": "默认记忆插件开启时自动追加到「工具说明」段末尾（系统段）"})
        if dynamic:
            out.append({"name": "plugin_memory_ctx", "label": "记忆插件 · 本轮上下文", "tag": "动态段",
                        "text": dynamic, "chars": len(dynamic), "dynamic": True,
                        "source": "每轮都变，拼在本轮消息末尾，不进 system"})
        return out

    @staticmethod
    def _with_plugin_parts(preview, segs):
        """把插件注入的段并进预览：固定的接在「工具说明」后面，动态的只列不拼进 system。"""
        parts = list(preview.get("parts") or [])
        at = len(parts)
        for i, p in enumerate(parts):
            if str(p.get("type") or "") == "tools" or str(p.get("name") or "") == "tools":
                at = i + 1
                break
        for s in segs:
            part = {"id": "plugin-" + s["name"], "label": s["label"], "name": s["name"],
                    "type": "plugin", "text": s["text"], "chars": s["chars"],
                    "plugin": True, "tag": s.get("tag", "")}
            if s.get("dynamic"):
                part["dynamic"] = True
                parts.append(part)
            else:
                parts.insert(at, part)
                at += 1
        chunks = [str(p.get("text") or "").rstrip() for p in parts
                  if not p.get("dynamic") and str(p.get("text") or "").strip()]
        system = "\n\n".join(chunks)
        out = dict(preview)
        out["parts"] = parts
        out["system"] = system
        out["chars"] = len(system)
        return out

    def _live_rows_local(self, sid):
        """插件自带的现成一份：静态段抄出厂模板，动态段现算（身份 / 时间 / 记忆）。"""
        rows = []
        # 用户 m23323：静态段先读自己家那份本地默认副本，缺哪个再用出厂参考副本补。
        segs = self._defaults_segments()
        if segs:
            order = [str(x) for x in ((self._defaults or {}).get("order") or [])] or list(segs.keys())
            for k in order:
                txt = str(segs.get(k) or "")
                if k in self.STATIC_SEGS and txt.strip():
                    rows.append({"name": k, "text": txt, "location": "system"})
        had = {seg_key(r["name"]) for r in rows}
        for tpl in (self._official.get("templates") or []):
            name = str((tpl or {}).get("name") or "")
            text = (tpl or {}).get("content")
            if name in self.STATIC_SEGS and name not in had and isinstance(text, str) and text.strip():
                rows.append({"name": name, "text": text, "location": "system"})
        rows.append({"name": "persona", "text": "", "location": "system"})
        rows.append({"name": "accounts", "text": self._sample_identity(sid), "location": "system"})
        rows.append({"name": "tools", "text": "## Tools 说明" + chr(10) + chr(10), "location": "system"})
        rows.append({"name": "memory", "text": "## 核心记忆（Core Memory）" + chr(10) + self._core_memory_text(),
                     "location": "dynamic"})
        rows.append({"name": "time", "text": self._live_time_text(), "location": "dynamic"})
        return rows

    # ------------------------------------------------------------------
    # 本地默认提示词（用户 m23323）
    # 默认词在自己家存一份，装配只读这一份，不再依赖请求时现取框架当前模板。
    # 框架以后换了默认词，用设置页工具栏的「同步框架默认」重新抓一次覆盖。
    # ------------------------------------------------------------------
    STATIC_SEGS = ("role", "attention", "output", "format")

    def _defaults_segments(self):
        d = self._defaults if isinstance(self._defaults, dict) else {}
        segs = d.get("segments")
        return segs if isinstance(segs, dict) else {}

    def defaults_info(self):
        d = self._defaults if isinstance(self._defaults, dict) else {}
        segs = self._defaults_segments()
        return {"ok": True, "path": str(self.defaults_path),
                "captured_at": int(d.get("captured_at") or 0),
                "source": str(d.get("source") or ""),
                "count": len([k for k, v in segs.items() if str(v or "").strip()]),
                "order": [str(x) for x in (d.get("order") or [])],
                "missing": [k for k in self.STATIC_SEGS if not str(segs.get(k) or "").strip()]}

    def _defaults_text(self, key):
        """本地默认副本里这一段的正文字；没有就回空串（由调用方再退其它来源）。"""
        return str(self._defaults_segments().get(str(key or "")) or "")

    def _defaults_stale(self, rows):
        """框架这几段跟本地副本不一样就算过期（只看静态段：人格 / 身份 / 工具随会话变，不算）。"""
        segs = self._defaults_segments()
        if not segs:
            return True
        by = {}
        for row in rows or []:
            k = seg_key((row or {}).get("name"))
            txt = str((row or {}).get("text") or "")
            if k and txt.strip():
                by[k] = txt
        return any(k in by and str(segs.get(k) or "") != by[k] for k in self.STATIC_SEGS)

    def save_defaults(self, segments, order=None, source=""):
        clean = {}
        for k, v in (segments or {}).items():
            txt = str(v or "")
            if str(k or "").strip() and txt.strip():
                clean[str(k)] = txt
        if not clean:
            return {"ok": False, "error": "没有可保存的默认段"}
        data = {"version": 1, "captured_at": int(time.time()), "source": str(source or ""),
                "order": [str(x) for x in (order or list(clean.keys()))], "segments": clean}
        try:
            self.defaults_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.defaults_path.with_name(self.defaults_path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.defaults_path)
        except Exception:
            logger.exception("[multi-decision] 写本地默认提示词失败")
            return {"ok": False, "error": "写文件失败，详见日志"}
        self._defaults = data
        return {"ok": True, "count": len(clean), "captured_at": data["captured_at"]}

    def capture_defaults(self, rows, source="", force=True):
        """把框架这一刻给的 system 段抄进本地副本；动态段（记忆 / 时间）不存，请求时现算。"""
        segments, order = {}, []
        for row in rows or []:
            name = str((row or {}).get("name") or "")
            if not name or self._dynamic(name) or str((row or {}).get("location") or "") == "dynamic":
                continue
            txt = str((row or {}).get("text") or "")
            if not txt.strip():
                continue
            k = seg_key(name)
            if k == "accounts":
                continue  # 身份每群不一样，现拼，不存成默认
            segments[k] = txt
            order.append(k)
        if not segments:
            return {"ok": False, "error": "框架这一次没给到可保存的段"}
        return self.save_defaults(segments, order, source)

    TOKEN_NAMES = ("persona", "identity", "tools", "realtime", "memory", "time",
                   "sessions", "chat_env", "accounts", "tools_note")

    def _token_value(self, key, kind, sid, context, by_name):
        k = str(key or "").strip().lower()
        ctx = context or {}
        if k in ("persona",):
            key_pick = str(ctx.get("persona_key") or "")
            if key_pick:
                return str((ctx.get("personas") or {}).get(key_pick) or "")
            return str(ctx.get("persona") or next(
                (r["text"] for r in by_name.values() if seg_key(r["name"]) == "persona"), ""))
        if k in ("identity", "accounts"):
            return str(ctx.get("identity") or self.identity_text(sid))
        if k in ("tools", "tools_note"):
            return str(ctx.get("tools") or next(
                (r["text"] for r in by_name.values() if seg_key(r["name"]) == "tools"), ""))
        if k == "realtime":
            return REALTIME_NOTE
        if k in ("memory", "time", "sessions", "chat_env", "session"):
            return str(next((r["text"] for r in by_name.values() if seg_key(r["name"]) == k), ""))
        if k in ("bot", "name", "self"):
            # 2026-11-08（J）：{{bot}} 换成这个会话里 bot 自己的昵称，没设就退全局名。
            return bot_name(self.plugin, sid)
        return ""

    def fill_tokens(self, text, kind, sid, context, by_name):
        """把 {{persona}} {{identity}} {{tools}} {{memory}} {{time}} 这类框换成这一轮的真实内容。"""
        body = str(text or "")
        if "{{" not in body:
            return body
        return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z_0-9]*)\s*\}\}",
                      lambda mo: (self._token_value(mo.group(1), kind, sid, context, by_name)
                                  or mo.group(0)), body)

    async def _live_chat_env(self, sid):
        """预览里没有事件可用：会话标题问会话管理器，平台 / 适配器从配置里挑第一个。"""
        env = {"platform": "QQ", "adapter": "",
               "chat_type": "GroupMessage" if sid.startswith("gm:") else "DirectMessage",
               "self_id": "", "session_title": sid, "session_description": ""}
        ctx = getattr(self.plugin, "ctx", None)
        try:
            env["self_id"] = str(getattr(self.plugin, "bot_qq", "") or "")
        except Exception:
            pass
        try:
            sm = getattr(ctx, "session_mgr", None)
            if sm is not None:
                sess = await sm.get_session(sid, ensure_session=False)
                env["session_title"] = (str(getattr(sess, "session_title", "") or "") or sid)
                env["session_description"] = str(getattr(sess, "session_description", "") or "")
        except Exception:
            logger.debug("[multi-decision] 取会话标题失败，聊天环境按 sid 显示", exc_info=True)
        try:
            adapters = (ctx.config.get("adapters", {}) or {}) if ctx is not None else {}
            for aid, ent in adapters.items():
                if isinstance(ent, dict):
                    env["platform"] = str(ent.get("platform") or env["platform"])
                    env["adapter"] = str(ent.get("name") or aid or "")
                    break
        except Exception:
            pass
        return env

    async def refresh_live(self, sid):
        """用户 m10789：请求这一刻现取框架当前那 11 段；拿不到就现场造一份。"""
        sid = normalize_sid(sid)
        if not sid:
            return []
        ctx = getattr(self.plugin, "ctx", None)
        pm = getattr(ctx, "prompt_mgr", None) if ctx is not None else None
        rows, source = None, ""
        if pm is not None:
            try:
                env = await self._live_chat_env(sid)
                prompts = await pm.get_agent_prompt(env)
                got = []
                for p in prompts or []:
                    name = str(getattr(p, "name", "") or "")
                    if not name:
                        continue
                    try:
                        text = self._text(p)
                    except Exception:
                        continue
                    got.append({"name": name, "text": text,
                                "location": "dynamic" if self._dynamic(name) else "system"})
                if got:
                    rows, source = got, "框架当前模板 · 请求时现取"
            except Exception:
                logger.exception("[multi-decision] 现取框架提示词失败，改用插件自带的一份")
                rows = None
        if not rows:
            rows, source = self._live_rows_local(sid), "插件自带文案 + 实时变量"
        rows = self._fill_memory(rows, sid)
        # 2026-11-08（J）：页面上不再留「同步框架默认」按钮，改成这里自动对齐——
        # 框架换了默认词，第一次请求就顺手抄一份新的；取不到框架那份（走的自带文案）时不动本地副本。
        live_source = source.startswith("框架当前模板")
        if not self._defaults_segments() or (live_source and self._defaults_stale(rows)):
            try:
                info = self.capture_defaults(
                    rows, source="框架当前模板 · 自动同步" if live_source else source)
                if info.get("ok"):
                    logger.info("[multi-decision] 框架默认提示词已同步到本地：%d 段", info.get("count"))
            except Exception:
                logger.exception("[multi-decision] 抄本地默认提示词失败，继续用现取的那份")
        self._live[sid] = {"rows": rows, "at": time.time(), "source": source}
        return rows

    def _base_rows(self, sid, sample=False):
        # 用户 m10789：装配只认「此刻现造」这一份——这次的现取结果优先，没有就现场造；
        # 绝不再拿上一轮请求的原文快照冒充当前（那份只留在记录页看历史）。
        live = self._live.get(normalize_sid(sid))
        if live and live.get("rows"):
            return self._keyed(live["rows"])
        return self._keyed(self._live_rows_local(sid))

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
            from .decision_phase import (DECISION_FORMAT_HARD, DECISION_ROLE, DECISION_SKELETONS,
                                         DEFAULT_DECISION_FORMAT)
            # 用户 m04343 第 6 条：决策这边「角色扮演」也是第一块（跟回复页一样，人设先定调子）。
            # 用户 m02159 图3：角色扮演段锁住、可留空、可移动；决策这边默认空人格。
            # 用户 m16925：空人格本来就不会注入，默认别再勾「启用」；选了人格文件才自动打开。
            # 2026-11-08（J）：最上面先来一块「角色设定」——判断的人不是 bot 本身。
            modules.append(module("decision-role", "text", "角色设定", "", DECISION_ROLE))
            modules.append(module("decision-persona", "persona", TYPE_LABELS["persona"], "", "", False))
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
        elif kind == "jev":
            # 用户 m06706：JEV 页默认四块——人设先定调子，判断规则紧接着（正文就在布局里，
            # 提示词页直接改），再是「你是谁」和聊天记录的落点。JEV 不发工具、不吐文本，
            # 所以没有工具段、也没有格式强化段。
            # 用户 m16925：JEV 页同样，空人格默认不启用。
            # 2026-11-08（J）：JEV 也分段——角色设定打头，再五段通用规则（跟决策页同一个长相）。
            # 判断规则·模板1 / 模板2 仍留在「来源」下拉里，想用整块的老稿随时能换回来。
            from .jev_phase import JEV_ROLE, JEV_SKELETONS
            modules.append(module("jev-role", "text", "角色设定", "", JEV_ROLE))
            modules.append(module("jev-persona", "persona", TYPE_LABELS["persona"], "", "", False))
            for skey, sname, stext in JEV_SKELETONS:
                modules.append(module("jev-skel-" + skey, "text", sname, "", stext))
            modules.append(module("jev-identity", "identity", TYPE_LABELS["identity"]))
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
        # 用户 m02993：动态段是真正的模板（默认「强化格式遵守」），不再是空占位。
        # 用户 m36396：决策侧也补同一块，正文换成决策自己的「只准吐一个 JSON」——回复那块讲的是 <msg> 包气泡。
        # 两边位置一致，都贴在本轮消息最后一条。
        if kind == "decision":
            modules.append(module("decision-format-hard", "dynamic", DYNAMIC_TEMPLATE_NAME, "",
                                  DECISION_FORMAT_HARD))
        elif kind == "jev":
            # 用户 m06706：JEV 只吐一个选择，不吐文本，「强化格式遵守」那段对它没意义；
            # 位置照留一块空的（默认关着），真要补一句判断提醒就在这里写。
            modules.append(module("jev-dynamic", "dynamic", TYPE_LABELS["dynamic"], "", "", False))
        else:
            modules.append(module("dynamic-tail", "dynamic", DYNAMIC_TEMPLATE_NAME, "", DYNAMIC_TEMPLATE_DEFAULT))
        # 用户 m04343 第 3 条：动态段紧挨聊天记录（实时窗口下面第一格），系统动态段排在它后面
        # ——系统动态段是框架自己拼在 system 末尾的那些内容（会话信息 / 聊天环境 / 记忆 / 时间）。
        # 用户 m08902 第 2 条：发布版的默认提示词要跟他现在实际在用的一致。
        # 他线上是「回复页开、决策页关」：回复的那一刻才需要把框架的会话/记忆/时间搬进本条消息，
        # 决策只判断回不回，搬一遍纯属重复计费。所以默认值按 kind 分开给。
        modules.append(module("sysmem-tail", "system_memory", TYPE_LABELS["system_memory"], "", "",
                              kind not in ("decision", "jev")))
        modules.append(module("systime-tail", "system_time", TYPE_LABELS["system_time"], "", "",
                              kind not in ("decision", "jev")))
        # 用户 m25796：默认稿仍按「系统段 → 实时窗 → 动态段 → 记忆段 → 时间段」给，
        # 但只在新建默认稿时归位；用户存过的顺序原样保留。
        return self._relabel({"version": 1, "mode": "modules", "text": "", "modules": tail_order(modules)})

    def layout(self, kind, sid=""):
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision、reply 或 jev")
        all_scopes, scope = self._data.get("layouts", {}), self._scope(sid)
        own = all_scopes.get(scope, {}).get(kind)
        if own is not None:
            return self._prepared(kind, normalize_layout(own, kind)), False
        inherited = all_scopes.get("*", {}).get(kind) if scope != "*" else None
        if inherited is not None:
            return self._prepared(kind, normalize_layout(inherited, kind)), True
        return self._prepared(kind, self.default_layout(kind, sid)), bool(sid)

    def _saved(self, kind, sid=""):
        """这一作用域到底有没有存过布局——自己的，或从全局继承下来的。

        2026-11-08（J）：layout() 第三分支把「回落全局默认」也报成 inherited，
        拿它当「存过旧稿」会让没存过布局的会话也弹「默认稿更新了」。
        """
        all_scopes, scope = self._data.get("layouts", {}), self._scope(sid)
        if all_scopes.get(scope, {}).get(kind) is not None:
            return True
        return scope != "*" and all_scopes.get("*", {}).get(kind) is not None

    def _looks_default(self, kind, doc):
        """这份布局是不是「就是默认稿」——块名都在默认稿里，没有自己加的模块。

        2026-11-08（J）：只有这种布局才提示「默认稿更新了」。自己加过模块的（比如回复页
        自写的「表达风格」「信息获取与求证」）不提示——点恢复默认会把它们整份换掉。
        正文改过也算默认稿（只是旧版本），所以只按块名认，不比正文。
        """
        try:
            base = self.default_layout(kind, "", migrate=False)
        except Exception:
            logger.debug("[multi-decision] 取默认稿失败，按自定义布局处理", exc_info=True)
            return False
        names = {str(m.get("label") or "").strip() for m in (base.get("modules") or [])
                 if isinstance(m, dict) and m.get("type") in ("text", "custom")}
        names.add("角色设定")
        for m in (doc or {}).get("modules") or []:
            if not isinstance(m, dict) or not m.get("enabled", True):
                continue
            if m.get("type") not in ("text", "custom"):
                continue
            if m.get("type") == "custom":
                return False
            if str(m.get("label") or "").strip() not in names:
                return False
        return True


    def save(self, kind, sid, layout, expected_revision):
        if not isinstance(expected_revision, str) or expected_revision != self.revision():
            return {"ok": False, "code": "REVISION_CONFLICT", "error": "提示词已变更，请重新读取后再保存；当前草稿未覆盖"}
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision、reply 或 jev")
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
        # 2026-11-08（J）：记下这份布局是按哪一版默认稿存的。默认稿以后一改，设置页就能
        # 提示「默认提示词更新了，推荐点恢复默认」；「已关闭提示」记的也是版本号，
        # 关掉以后同一版默认稿不再提示（换一版默认稿才会再提醒一次）。
        stamps = new.setdefault("notices", {}).setdefault(scope, {})
        old = stamps.get(kind) if isinstance(stamps.get(kind), dict) else {}
        stamps[kind] = {"built_for": self.default_fp(kind),
                        "seen": str((old or {}).get("seen") or "")}
        self._write(self.path, new)
        self._data = new  # 失败不修改内存，也不报告假成功。
        return {"ok": True, "revision": self.revision()}

    def default_fp(self, kind):
        """默认稿的指纹：默认布局里每块的名字与正文。默认稿一改，指纹就跟着变。"""
        try:
            mods = self.default_layout(kind, "", migrate=False).get("modules", [])
        except Exception:
            logger.debug("[multi-decision] 算默认稿指纹失败", exc_info=True)
            return ""
        raw = json.dumps([[str(m.get("id") or ""), str(m.get("label") or ""),
                           str(m.get("content") or "")] for m in mods], ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    def _notice(self, kind, scope, stored, doc=None):
        """这一作用域存过旧稿、而默认稿已经改过时，页面上挂一条提示（可关，关掉不再提）。

        2026-11-08（J）：只对「看起来就是默认稿」的布局提示——自己加过模块的布局
        点恢复默认会被整份换掉，不能劝人点。
        """
        if kind not in KINDS:
            return {"show": False, "fp": ""}
        cur = self.default_fp(kind)
        if not stored or not cur:
            return {"show": False, "fp": cur}
        if doc is not None and not self._looks_default(kind, doc):
            return {"show": False, "fp": cur}
        stamp = self._data.get("notices", {}).get(scope, {}).get(kind)
        stamp = stamp if isinstance(stamp, dict) else {}
        if str(stamp.get("seen") or "") == cur:
            return {"show": False, "fp": cur}
        return {"show": str(stamp.get("built_for") or "") != cur, "fp": cur}

    def dismiss_notice(self, kind, sid=""):
        """2026-11-08（J）：关掉「默认稿更新」提示——只关这一版默认稿，换一版还会再提一次。"""
        if kind not in KINDS:
            raise ValueError("kind 必须是 decision、reply 或 jev")
        scope, cur = self._scope(sid), self.default_fp(kind)
        new = copy.deepcopy(self._data)
        stamps = new.setdefault("notices", {}).setdefault(scope, {})
        old = stamps.get(kind) if isinstance(stamps.get(kind), dict) else {}
        stamps[kind] = {"built_for": str((old or {}).get("built_for") or ""), "seen": cur}
        self._write(self.path, new)
        self._data = new
        return {"ok": True, "revision": self.revision()}

    def identity_text(self, sid, sample=False):
        """只取本会话的回复身份；不拼时间/会话数、不向其它会话兜底。"""
        rows = self._base_rows(sid, sample)
        raw = next((r["text"] for r in rows if seg_key(r["name"]) == "accounts" and "你的身份" in r["text"]), "")
        if not raw:
            # 用户 m10789：框架给的那行「社交账号信息（Accounts）」只有平台和账号 ID，没有昵称 /
            # 外号 / 管理员，群里模型会认不出人。所以这一份仍按当前配置现拼（左侧卡片显示的就是它），
            # 请求时也用它——预览和真实请求必须是同一份。
            raw = self._sample_identity(sid)
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

    async def _context(self, sid, opt=None, layout=None, persona=None):
        dp = self.plugin.decision_phase
        opt = opt or self.plugin.decision_options(sid)
        # 用户 m06706：JEV 那边已经取过一次人格，别再取第二遍（两边必须是同一份正文）。
        # v4.89（用户 m14715 第 1 条）：决策页不再有「设置里那份人格」，人设只由人格模块
        # 各自指向的文件决定；没传 persona 就是空的。
        if persona is None:
            persona = ""
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
        # 用户 m06706：JEV 的 opt 里没有 tools 字段，别为了拼一段用不上的工具说明去炸一次异常。
        tools = self._tools_text(opt) if hasattr(opt, "tools") else ""
        return {"persona": persona, "personas": personas, "tools": tools, "opt": opt}

    def _tools_text(self, opt=None, sid="", names=None, head=None, rounds=None):
        """用户 m02159 图3：勾了哪个工具就写哪个。

        用户 m11103：决策链与回复链各有一份名单——names 给了就按它写（回复链走这条），
        没给就按 opt.tools 写（决策链走这条）。
        用户 m13442 第 3 条：抬头与轮数由调用方给（head / rounds），回复链不再借用决策的轮数。
        """
        dp = self.plugin.decision_phase
        try:
            if names is None:
                opt = opt or self.plugin.decision_options(sid)
                names = getattr(opt, "tools", None)
            picked = dp.pick_tools(names)
        except Exception:
            logger.exception("[multi-decision] 取工具清单失败，这一段留空")
            return ""
        if not picked:
            return ""
        try:
            text = dp.tools_help(picked)
            from .decision_phase import TOOL_HEAD
            if rounds is None:
                rounds = getattr(opt, "tool_rounds", None)
                if rounds is None:
                    try:
                        rounds = self.plugin.decision_options(sid).tool_rounds
                    except Exception:
                        rounds = 2
            return ((head or TOOL_HEAD) % max(1, int(rounds or 2))) + "\n" + text
        except Exception:
            logger.exception("[multi-decision] 拼工具说明失败")
            return ""

    def _reply_tools_text(self, sid=""):
        """回复链只写它真正挂的那几个工具；还没勾过（None）就空着，跟以前一样。

        用户 m13442 第 3 条：轮数取「回复侧最大循环轮数 − 1」——最后那一轮要留给
        「看完工具结果再写回复」，不能把轮数全给工具。
        """
        names = self.reply_selected_tools()
        if names is None:
            return ""
        from .decision_phase import REPLY_TOOL_HEAD
        rounds = 5
        try:
            limits = self.plugin.reply_tool_limits(sid) or {}
            rounds = int(limits.get("rounds") or 5)
        except Exception:
            logger.exception("[multi-decision] 读回复工具参数失败，工具说明按框架默认 5 轮写")
        return self._tools_text(sid=sid, names=names, head=REPLY_TOOL_HEAD,
                                rounds=max(1, rounds - 1))

    def _sysseg_part(self, kind, m, source_rows, seg_name, typ):
        """系统段里按记忆 / 时间分开的一格；正文、注入、framed 的算法与旧合并段一致。"""
        dyn_rows = [r for r in source_rows
                    if r.get("location") == "dynamic" and str(r.get("text") or "").strip()
                    and str(r.get("name") or "") not in NOISY_NAMES
                    and seg_key(r.get("name")) == seg_name]
        framed_rows, inject_rows = [], list(dyn_rows)
        if kind == "reply":
            framed_rows = [r for r in dyn_rows if self._framed_dynamic(r)]
            inject_rows = [r for r in dyn_rows if not self._framed_dynamic(r)]
        text = _tidy_blanks("\n\n".join(r["text"] for r in framed_rows + inject_rows))
        inject_text = _tidy_blanks("\n\n".join(r["text"] for r in inject_rows))
        return {"id": m["id"], "label": m["label"], "name": typ, "type": typ,
                "text": text, "chars": len(text), "dynamic": True,
                "inject": bool(m["enabled"]) and bool(inject_text),
                "inject_text": inject_text,
                "framed": [r["name"] for r in framed_rows],
                "sources": [r["name"] for r in dyn_rows]}

    def _sysmem_part(self, kind, m, source_rows):
        return self._sysseg_part(kind, m, source_rows, "memory", "system_memory")

    def _systime_part(self, kind, m, source_rows):
        return self._sysseg_part(kind, m, source_rows, "time", "system_time")

    def _sysdyn_part(self, kind, m, source_rows):
        """用户 m10297 图1：系统动态段这一格的正文——开着进 parts，关着进 inactive，算法只有这一份。"""
        # 用户 m03879：框架自己拼在 system 末尾的动态段（会话信息 / 聊天环境 / 记忆 / 时间）
        # 单独留一格给他看：正文照抄本轮的动态段，但不再往 system 塞（它会跟框架的重复）。
        dyn_rows = [r for r in source_rows
                    if r.get("location") == "dynamic" and str(r.get("text") or "").strip()
                    and str(r.get("name") or "") not in NOISY_NAMES]
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
        text = _tidy_blanks("\n\n".join(r["text"] for r in framed_rows + inject_rows))
        inject_text = _tidy_blanks("\n\n".join(r["text"] for r in inject_rows))
        # 用户 m05691：这一段默认不开启；勾上就把这些内容拼到本轮消息最下面（跟回复页
        # 的动态段一个位置），排在格式强化之后——它是这一轮真正该看的现场信息。
        return {"id": m["id"], "label": m["label"], "name": "system_dynamic", "type": "system_dynamic",
                "text": text, "chars": len(text), "dynamic": True,
                "inject": bool(m["enabled"]) and bool(inject_text),
                "inject_text": inject_text,
                "framed": [r["name"] for r in framed_rows],
                "sources": [r["name"] for r in dyn_rows]}

    def _dynamic_part(self, m):
        """用户 m02993：动态段是能改的模板，正文拼在本轮 user 消息的最后，不进 system。"""
        # 用户 m36396：决策那一块正文空着时不能回落到回复版的 <msg> 那段——按模块 id 认人。
        from .decision_phase import DECISION_FORMAT_HARD
        _mid = str(m.get("id") or "")
        # 用户 m06706：JEV 只吐一个选择、不吐文本，格式强化那段对它没意义，正文空就真的空着。
        _fallback = (DECISION_FORMAT_HARD if _mid.startswith("decision-")
                     else ("" if _mid.startswith("jev-") else DYNAMIC_TEMPLATE_DEFAULT))
        text = m["content"] or _fallback
        # 用户 m10297 图1：关掉启用时 inject 也要跟着假，否则关掉的模板照样拼进 user 末尾。
        return {"id": m["id"], "label": m["label"], "name": "dynamic", "type": "dynamic",
                "text": text, "chars": len(text), "dynamic": True,
                "inject": bool(m["enabled"])}

    def _plugin_texts(self, sid, context=None):
        """用户 m21698：记忆插件追加的两段正文——固定说明 + 每轮都变的上下文。

        真实请求里由 apply_reply 从这一轮的 tools 段现摘传进来；预览时读上一轮抓到的素材。
        """
        ctx = context or {}
        static, dynamic = ctx.get("plugin_memory"), ctx.get("plugin_memory_ctx")
        if static is None and dynamic is None:
            static, dynamic = self._split_tools_extra(self._captured_tools_extra(sid))
        return str(static or ""), str(dynamic or "")

    def _plugin_part(self, m, sid, context=None):
        """记忆插件那两段的 part：固定说明进 system，本轮上下文进动态段。

        用户 m23028：抓不到真实正文时用默认模板顶位，卡片不再空成 0 字；这时 part 带
        sample_only=True，apply_reply 会跳过它，真发请求不会注入默认模板。
        """
        static, dynamic = self._plugin_texts(sid, context)
        typ = str(m.get("type") or "")
        live = dynamic if typ == "plugin_memory_ctx" else static
        default = PLUGIN_MEMORY_CTX_DEFAULT if typ == "plugin_memory_ctx" else PLUGIN_MEMORY_DEFAULT
        text = str(live or default)
        part = {"id": m["id"], "label": m["label"], "name": typ, "type": typ,
                "text": text, "chars": len(text), "sample_only": not bool(live)}
        if typ == "plugin_memory_ctx":
            part["dynamic"] = True
            part["inject"] = bool(m.get("enabled", True)) and bool(live)
            part["inject_text"] = str(live or "")
        return part
    def _render(self, kind, sid, layout, context, rows=None, sample=False):
        layout = normalize_layout(layout, kind)
        if layout["mode"] == "text":
            txt = layout["text"]
            parts = [{"id": "full-text", "label": "单段文本", "type": "text", "name": "custom_system", "text": txt, "chars": len(txt)}] if txt else []
            return {"ok": True, "system": txt, "parts": parts, "chars": len(txt), "warnings": [], "source": "同源单段装配"}
        source_rows = self._keyed(rows) if rows is not None else self._base_rows(sid, sample)
        by_name = {row["key"]: row for row in source_rows}
        for row in source_rows:
            if row["name"] not in by_name:
                by_name[row["name"]] = dict(row, text="".join(r["text"] for r in source_rows if r["name"] == row["name"]))
        parts, warnings, inactive = [], [], []
        for m in layout["modules"]:
            # 用户 m06721 图1：关掉「启用」的段一律不进预览，系统动态段也不例外——
            # 以前为了让卡片能看见内容，这一格关着也照常渲染，结果开关关了右边还留着整段字。
            # 用户 m10297 图1：可关掉之后左边卡片又变成「0 字 / 空白」——这两格（系统动态段 /
            # 动态段）本来就是「给你看这一轮会收到什么」的展示位，所以另记一份 inactive：
            # 不注入、不进右侧预览拼接，但卡片照实显示它的字数与正文。
            if not m["enabled"]:
                if m["type"] == "system_memory":
                    inactive.append(self._sysmem_part(kind, m, source_rows))
                elif m["type"] == "system_time":
                    inactive.append(self._systime_part(kind, m, source_rows))
                elif m["type"] == "system_dynamic":
                    inactive.append(self._sysdyn_part(kind, m, source_rows))
                elif m["type"] == "dynamic":
                    inactive.append(self._dynamic_part(m))
                elif m["type"] in ("plugin_memory", "plugin_memory_ctx"):
                    inactive.append(self._plugin_part(m, sid, context))
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
                # 用户 m10789：优先插件现拼的那份（昵称 / 外号 / 管理员都在），没有才退框架原文。
                text = self.identity_text(sid, sample)
                if not text:
                    text = next((r["text"] for _k, r in by_name.items()
                                 if seg_key(r["name"]) == "accounts"), "")
            elif typ == "persona":
                # 用户 m02159 图3：角色扮演段只认「人格文件」。决策不选就留空；回复不选就沿用
                # 框架给的那一段原身（保持和以前一样，不凭空造人设）。
                if key:
                    text = (context.get("personas") or {}).get(key, "")
                    if not text:
                        warnings.append(m["label"] + "指向的人格文件读不到，这一段不注入")
                        continue
                    # v4.84（用户 m09179 第 4 条）：人格正文不再加「【人设】」抬头，
                    # 人设文件自己就有标题，多这一行只是噪音。
                elif kind in ("decision", "jev"):
                    # 用户 m06706：JEV 页跟决策页一样，不选人格就留空，不凭空造人设。
                    text = context.get("persona", "")
                else:
                    text = next((r["text"] for _k, r in by_name.items() if seg_key(r["name"]) == "persona"), "")
            elif typ == "tools":
                # 用户 m02159 图3：工具名单不单独成项，统一注进「工具说明」这一段下面；
                # 勾过就按勾的写，没勾过才沿用框架原段（回复轮框架自己会给一段）。
                text = context.get("tools") or next((r["text"] for _k, r in by_name.items() if seg_key(r["name"]) == "tools"), "")
            elif typ == "framework":
                # 用户 m23323：本地默认副本里有就用它，装配不再被「这一刻框架给没给」牵着走。
                text = self._defaults_text(key) or by_name.get(key, {}).get("text", "")
                if kind == "reply" and key == "output":
                    # 用户 m13442 第 4 条：预览里也显示插件接管后的轮数 / 单轮工具数。
                    text = self._rewrite_agent_limits(text, sid)
                if by_name.get(key, {}).get("location") == "dynamic":
                    warnings.append(m["label"] + "保持在动态尾段，不进入固定 system")
                    continue
            elif typ == "realtime_marker":
                # 用户 m02159 图1：实时窗口只是位置说明 + 一段假聊天记录，不进 system。
                parts.append({"id": m["id"], "label": m["label"], "name": "realtime", "type": typ,
                              "text": m["content"] or REALTIME_NOTE, "chars": 0, "dynamic": True})
                continue
            elif typ == "system_memory":
                parts.append(self._sysmem_part(kind, m, source_rows))
                continue
            elif typ == "system_time":
                parts.append(self._systime_part(kind, m, source_rows))
                continue
            elif typ == "system_dynamic":
                # 用户 m10297 图1：算法搬进 _sysdyn_part，跟「关掉启用」时给卡片看的那份共用。
                parts.append(self._sysdyn_part(kind, m, source_rows))
                continue

            elif typ == "dynamic":
                # 用户 m02993：动态段是能改的模板，正文拼在本轮 user 消息的最后，不进 system。
                parts.append(self._dynamic_part(m))
                continue
            elif typ in ("plugin_memory", "plugin_memory_ctx"):
                # 用户 m21698：记忆插件追加的两段——固定说明进 system（跟在工具说明后面），
                # 本轮上下文是动态段（拼在本轮消息末尾）。正文都从这一轮的 tools 段现摘。
                part = self._plugin_part(m, sid, context)
                if not part["text"]:
                    continue
                parts.append(part)
                continue
            if typ in ("text", "custom", "decision_prompt", "decision_format") and text:
                # 用户 m23323：自写文本里可以写 {{persona}} / {{identity}} / {{tools}} /
                # {{memory}} / {{time}} 这类框，请求时按这一轮的真实内容替换。
                text = self.fill_tokens(text, kind, sid, context, by_name)
            if text == "":
                # 用户 m02159：人格留空、工具没勾都是正常状态（模块上就写着会跳过），不该刷警告。
                if typ not in BODY_TYPES and typ not in ("persona", "tools"):
                    warnings.append(m["label"] + "暂缺同会话来源，未借用其它会话或生成替身")
                continue
            name = by_name.get(key, {}).get("name", key) or ({"identity": "accounts", "persona": "persona", "tools": "tools"}.get(typ)) or m["id"]
            parts.append({"id": m["id"], "label": m["label"], "name": name, "type": typ, "text": text, "chars": len(text)})
        # 回复引用已含 Prompt.end；用户自写文本也是精确字串，不在第二处另造分隔符。
        # dynamic_marker 只是位置提示，不参与 system 拼接。
        # 用户 m23323 图3：段与段之间统一留一个空行——以前回复侧是直接首尾相接，
        # 自写文本末尾没带换行时，下一段会黏在上一段最后一行后面。
        chunks = [str(p.get("text") or "").rstrip() for p in parts
                  if not p.get("dynamic") and str(p.get("text") or "").strip()]
        system = "\n\n".join(chunks)
        return {"ok": True, "system": system, "parts": parts, "chars": len(system), "warnings": warnings,
                # 用户 m10297 图1：被「启用」关掉的展示段（系统动态段 / 动态段）放在这里，
                # 只给左边卡片显示字数与正文，不参与 system 拼接、也不进右侧预览。
                "inactive": inactive,
                "source": self._source_label(sid, sample)}

    def _rewrite_agent_limits(self, text, sid=""):
        """把框架 output 段里烤死的「N 轮」「N 个工具」换成插件接管后的值。

        用户 m13442 第 4 条：这两项以前只能在框架「代理设置」里改，现在归插件管，
        预览必须跟着变，否则设置页看到的和模型收到的是两套数。
        """
        body = str(text or "")
        if not body:
            return body
        try:
            limits = self.plugin.reply_tool_limits(sid) or {}
        except Exception:
            logger.exception("[multi-decision] 读回复工具参数失败，框架原文照旧")
            return body
        loop = int(limits.get("rounds") or 5)
        calls = int(limits.get("calls_per_turn") or 5)
        body = _RE_LOOP_LIMIT.sub(lambda m: "%s%d%s" % (m.group(1), loop, m.group(2)), body)
        body = _RE_CALL_LIMIT.sub(lambda m: "%s%d%s" % (m.group(1), calls, m.group(2)), body)
        return body

    def _patch_agent_limits(self, prompts, sid=""):
        """把框架 output 段 Prompt 的轮数 kwargs 换成插件接管后的值。

        用户 m13442 第 4 条：框架这一段是 render_template=True 的 Prompt，轮数在
        BuildRequest 阶段就烤进 kwargs 了；这里改 kwargs，框架在 assemble 阶段渲染时
        用的就是插件的值——不用重造 Prompt，也不会丢掉它自己的 end / persist。
        """
        try:
            limits = self.plugin.reply_tool_limits(sid) or {}
        except Exception:
            logger.exception("[multi-decision] 读回复工具参数失败，框架原文照旧")
            return
        loop = int(limits.get("rounds") or 5)
        calls = int(limits.get("calls_per_turn") or 5)
        for p in prompts or []:
            kw = getattr(p, "kwargs", None)
            if not isinstance(kw, dict):
                continue
            if "max_tool_loop" in kw:
                kw["max_tool_loop"] = loop
            if "max_tool_calls_per_turn" in kw:
                kw["max_tool_calls_per_turn"] = calls

    def _source_label(self, sid, sample=False):
        live = self._live.get(normalize_sid(sid))
        if live and live.get("rows"):
            return live.get("source") or "请求时现取"
        return "插件自带文案 + 实时变量（这个会话还没跑过请求）"

    async def assemble_jev(self, sid, opt=None, layout=None, persona=""):
        """用户 m06706：JEV 请求的 instructions 走这一份，跟预览、记录快照同源。"""
        doc = self.layout("jev", sid)[0] if layout is None else layout
        await self.refresh_live(sid)
        return self._render("jev", sid, doc,
                            await self._context(sid, opt, doc, persona=(persona or None)))

    async def assemble_decision(self, sid, opt=None, layout=None):
        doc = self.layout("decision", sid)[0] if layout is None else layout
        # 用户 m10789：决策请求没有框架现成的 system 可用，也在这一刻现造，不读上一轮快照。
        await self.refresh_live(sid)
        return self._render("decision", sid, doc, await self._context(sid, opt, doc))

    def preview_reply(self, sid="", layout=None):
        doc = self.layout("reply", sid)[0] if layout is None else layout
        return dict(self._render("reply", sid, doc, {"tools": self._reply_tools_text(sid)}, sample=True), sid=sid)

    async def prefetch_personas(self, sid="", layout=None):
        """用户 m23323 图2：真实回复这一路是同步装配，人格正文没预热就整段丢弃。
        所以装配之前先 await 一遍，把正文塞进缓存，装配时 persona_text_now 才读得到。"""
        doc = self.layout("reply", sid)[0] if layout is None else layout
        phase = getattr(self.plugin, "decision_phase", None)
        if phase is None:
            return {}
        keys = [str(m.get("key") or "") for m in (doc.get("modules") or [])
                if m.get("type") == "persona" and str(m.get("key") or "")]
        got = {}
        cached = getattr(phase, "_persona_cache", None) or {}
        for k in keys:
            if cached.get(k):
                got[k] = cached[k]
                continue
            body = ""
            try:
                body = await phase.persona_text(k)
            except Exception:
                logger.exception("[multi-decision] 预取人格失败：%s", k)
            if body:
                got[k] = body
        self._personas_pre.setdefault(normalize_sid(sid), {}).update(got)
        if len(self._personas_pre) > 64:
            self._personas_pre.pop(next(iter(self._personas_pre)))
        return got
    @staticmethod
    def _tools_extra_text(text):
        """从一段 tools 正文里摘出记忆插件追加的部分（「使用 memory_add …」之后）。"""
        body = str(text or "")
        for marker in ("使用 memory_add", "Use memory_add"):
            idx = body.find(marker)
            if idx >= 0:
                return body[idx:].strip()
        return ""

    @classmethod
    def _tools_extra(cls, prompts):
        """框架 tools 段里被记忆插件追加的使用说明 + sources 上下文。

        用户 m20353：多决策重建 tools 段时整段替换，把记忆插件追加的说明和上下文丢了。
        这里只摘「使用 memory_add …」之后的部分，拼回插件自己渲染的清单后面。
        """
        for p in prompts or []:
            got = cls._tools_extra_text(getattr(p, "content", ""))
            if got:
                return got
        return ""

    @staticmethod
    def _split_tools_extra(text):
        """把记忆插件追加的那段拆成「固定说明」和「每轮都变的上下文」。

        用户 m20823 第 6 条：说明是固定的，留在 system 的工具段里还能吃缓存；
        后面那串 {"persona_id": …, "sources": […]} 每轮都变，留在 system 里等于每轮都掉命中，
        所以拆出来跟动态段一起拼到本轮消息末尾。
        """
        body = str(text or "").strip()
        if not body:
            return "", ""
        idx = body.rfind('{"persona_id"')
        if idx < 0:
            return body, ""
        return body[:idx].rstrip(), body[idx:].strip()

    def _tight_prompts(self, prompts):
        """用户 m26166：框架原段的正文自带换行，Prompt.end 默认又补一个换行，
        拼起来模块之间就多出空行。这里把每段收成「正文 + 恰好一个空行」，
        真实请求里模块之间就只有一个空行，跟预览一致。

        就地改，不复制：框架的 Prompt 是每轮现造的（get_agent_prompt），改它不影响
        别的请求；复制会换掉对象身份，提示词页「框架原段原样保留」的契约（对象本身、
        kwargs、render_template）就断了。归一化幂等，重试链路反复套也不会叠空行。"""
        for p in prompts:
            text = str(getattr(p, "content", "") or "")
            if not text.strip():
                continue
            try:
                p.content = text.rstrip() + "\n\n"
                p.end = ""
            except Exception:
                continue
        return prompts

    def apply_reply(self, request, sid=""):
        if not isinstance(getattr(request, "system_prompt", None), list):
            return False
        sid = normalize_sid(sid)
        previous = self._applied.get(sid)
        if previous and previous[0] is request and request.system_prompt is previous[2]:
            request.system_prompt = previous[1]
        self._remember_tag_fill(request)
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
                personas[pname] = (self.plugin.decision_phase.persona_text_now(pname)
                                   or (self._personas_pre.get(sid) or {}).get(pname, ""))
                if not personas[pname]:
                    # 用户 m23323 图2：真实请求里人设整段丢了，得能在日志里一眼看见是哪一段。
                    logger.warning("[multi-decision] 回复提示词：人格「%s」正文读不到，这一段没有注入", pname)
        tools_text = self._reply_tools_text(sid)
        # 用户 m20823 第 6 条：记忆插件追加的那段拆两半——固定说明留在 system 吃缓存，
        # 末尾那串每轮都变的 {"persona_id"…} 跟动态段一起拼到本轮消息末尾。
        extra_static, extra_dyn = self._split_tools_extra(self._tools_extra(original.get("tools", [])))
        # 用户 m21698：这两半现在是布局里的两块（记忆插件注入 / 记忆插件 · 本轮上下文），
        # 位置和启停都归提示词页管；只有布局里没有这两块（单段文本模式）时才按老办法接回去。
        mod_types = {str(m.get("type") or "") for m in doc["modules"]}
        owns_static = "plugin_memory" in mod_types
        owns_dyn = "plugin_memory_ctx" in mod_types
        rendered, output = self._render("reply", sid, doc,
                                        {"personas": personas, "tools": tools_text,
                                         "plugin_memory": extra_static,
                                         "plugin_memory_ctx": extra_dyn},
                                        rows=self._rows(request)), []
        for _w in rendered.get("warnings") or []:
            logger.warning("[multi-decision] 回复提示词：%s", _w)
        mods = {m["id"]: m for m in doc["modules"]}
        for p in rendered["parts"]:
            if p.get("sample_only"):
                # 用户 m23028：默认模板只给预览顶位，真发请求时没有真实正文就不注入。
                continue
            if p.get("dynamic"):
                # 用户 m02993：动态段不进 system，下面单独拼到本轮 user 末尾。
                continue
            m = mods.get(p["id"], {})
            key = m.get("key", "") or {"persona": "persona", "tools": "tools", "identity": "accounts"}.get(m.get("type"), "")
            if m.get("type") == "tools" and tools_text:
                # 用户 m02159 图3：勾过工具就按勾的那份重写「工具说明」这一段。
                # 用户 m20353：框架原段里可能被记忆插件追加了使用说明和 sources 上下文，
                # 整段替换会把它们丢掉——把追加部分接在插件清单后面。
                text = str(p["text"] or "")
                if extra_static and not owns_static:
                    text = (text + "\n\n" + extra_static).strip()
                output.append(Prompt(_pad_seg(text), name="tools", source="system", persist=False,
                                     render_template=False, end=""))
            elif m.get("type") == "persona" and m.get("key"):
                # 用户 m02159 图3：人格段自选的文件不在框架原段里，必须按新正文重造一段 Prompt。
                output.append(Prompt(_pad_seg(p["text"]), name="persona", source="system", persist=False,
                                     render_template=False, end=""))
            elif m.get("type") == "identity" and str(p.get("text") or "").strip():
                # 用户 m10789：身份段用插件现拼的那份，和预览同一处来源。框架那行更短的
                # 「社交账号信息（Accounts）」顶替掉它的话，预览和真实请求就对不上了。
                output.append(Prompt(_pad_seg(p["text"]), name="accounts", source="system", persist=False,
                                     render_template=False, end=""))
            elif m.get("type") in ("framework", "persona", "tools"):
                kept = original.get(key, [])
                if m.get("type") == "framework" and key == "output":
                    # 用户 m13442 第 4 条：轮数 / 单轮工具数改由插件接管。
                    self._patch_agent_limits(kept, sid)
                output.extend(self._tight_prompts(kept))  # 用户 m26166：模块之间只留一个空行。
            else:
                output.append(Prompt(_pad_seg(p["text"]), name=p["name"], source="system", persist=False, render_template=False, end=""))
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
        if extra_dyn and not owns_dyn:
            # 用户 m20823 第 6 条：记忆插件那串每轮都变的上下文别留在 system 里，
            # 跟动态段一起拼到本轮消息末尾，system 才能整段吃缓存。
            dyn_text = (dyn_text + "\n\n" + extra_dyn).strip()
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
        # 用户 m23323 图3：动态段紧贴在本轮消息最后一行后面会黏成一行，前后各留一个空行。
        body = "\n" + body + "\n\n"
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
            # 用户 m06428 图1：动态段模板要落在 <system_reminder> 块里、开始标记之前。
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
            raise ValueError("kind 必须是 decision、reply 或 jev")
        context_sid = normalize_sid(sid) if sid else self.plugin._decision_pick_sid()
        doc, inherited = self.layout(kind, sid or context_sid)
        # 2026-11-08（J）：这一页显示的到底是不是存过的旧稿——「默认稿更新」提示只认旧稿。
        # 回落全局默认（自己没存过）不算旧稿，否则没存过布局的会话也会被提示。
        stored = self._saved(kind, sid or context_sid)
        if not sid:
            own = self._data.get("layouts", {}).get("*", {}).get(kind)
            stored = self._saved(kind, "")
            # 用户 m28586 第 1 条：没有会话（看全局）这条分支也得补人格段，
            # 否则页面上「角色扮演」只在指定会话时才出现，保存时又被悄悄写回去。
            # 用户 m36396：决策页「强化格式遵守」是新加的一块，全局作用域也该有——
            # 这两处以前只补人格段，老布局在「看全局」时就会少一块，设置页上根本看不到它。
            doc = (self._prepared(kind, normalize_layout(own, kind)) if own is not None
                   else self._prepared(kind, self.default_layout(kind, context_sid)))
            inherited = False
        if draft is not None:
            doc = self._prepared(kind, normalize_layout(draft, kind))
            stored = False
        await self.refresh_live(context_sid)
        # 用户 m06706：JEV 页的左侧预览要用它自己的人设（留空才跟决策），
        # 顺手把 JevOptions 传下去——里面没有 tools，_context 已经按缺字段处理。
        jev_opt, jev_persona = None, ""
        if kind == "jev":
            try:
                jev_opt = self.plugin.jev_options(context_sid)
            except Exception:
                logger.exception("[multi-decision] 取 JEV 选项失败，预览按默认人设")
                jev_opt = None
            if jev_opt is not None and getattr(jev_opt, "persona", ""):
                try:
                    jev_persona = await self.plugin.decision_phase.persona_text(jev_opt.persona) or ""
                except Exception:
                    logger.exception("[multi-decision] 取 JEV 人设失败，预览不加人设")
        # 用户 m11103：回复链第一次打开这一页就把框架现有的工具全勾上（默认全选）。
        # 用户 m11533：之后新装的插件带的工具也自动勾上，所以每次打开都补一遍（决策链不补）。
        # 必须赶在 _context 拼工具说明之前——否则第一次打开看到的是空清单，勾选框却全勾着。
        if kind == "reply":
            try:
                self.reply_tools_for([c.get("name") for c in self.plugin.decision_phase.tool_catalog()])
            except Exception:
                logger.exception("[multi-decision] 初始化回复工具清单失败（保持框架全量）")
        context = await self._context(context_sid, jev_opt, doc, persona=(jev_persona or None))
        if kind == "reply":
            # 用户 m11103：回复页「工具说明」这一段显示的必须是回复链真正要挂的那几个工具，
            # 以前这里拿到的是决策链那份（默认空），页面上就只剩框架那行标题。
            context["tools"] = self._reply_tools_text(context_sid)
        preview = self._render(kind, context_sid, doc, context, sample=True)
        reply_rows = {}
        for row in self._base_rows(context_sid, sample=True):
            reply_rows[row["name"]] = reply_rows.get(row["name"], "") + row["text"]
        # 用户 m10789：两边的身份段都取插件现拼的那一份，和右侧拼进 system 的必须是同一份。
        identity = self.identity_text(context_sid, sample=True)
        persona_text = (context["persona"] if kind in ("decision", "jev")
                       else reply_rows.get("persona", ""))
        tools_text = (context["tools"] if kind in ("decision", "jev")
                      else reply_rows.get("tools", ""))
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
            from .decision_phase import DECISION_ROLE, DEFAULT_DECISION_FORMAT, DECISION_SKELETONS
            # 用户 m02159 图2：「决策提示词」整块不再出现在下拉里，改用下面三块细化规则。
            # 2026-11-08（J）：最上面那块「角色设定」也是默认模板——下拉里标「默认模板」，
            # 改正文就转自定义，跟五段骨架一个待遇。
            catalog.append({"type": "text", "key": "skel-role", "label": "角色设定", "text": DECISION_ROLE,
                            "source": "判断的人不是 bot 本身，只负责判断", "available": True,
                            "group": "custom", "tag": "默认模板", "suffix": "默认模板",
                            "skeleton": True})
            # 用户 m04343 第 4 条：输出规则跟三块小模块一样是「默认模板」——改正文就变自定义。
            catalog.append({"type": "decision_format", "key": "", "label": TYPE_LABELS["decision_format"],
                            "text": DEFAULT_DECISION_FORMAT, "source": "决策插件自带的默认输出规则",
                            "available": True, "group": "default", "tag": "判断格式",
                            "suffix": "默认模板", "skeleton": True})
            for skey, sname, stext in DECISION_SKELETONS:
                catalog.append({"type": "text", "key": "skel-" + skey, "label": sname, "text": stext,
                                "source": "可拆的小模块骨架", "available": True, "group": "custom",
                                "tag": "默认模板", "suffix": "默认模板", "skeleton": True})
        elif kind == "jev":
            # 2026-11-08（J）：JEV 也分段——角色设定打头，再五段通用规则，跟决策页一个长相。
            from .jev_phase import JEV_ROLE, JEV_SKELETONS
            catalog.append({"type": "text", "key": "skel-jev-role", "label": "角色设定", "text": JEV_ROLE,
                            "source": "判断的人不是 bot 本身，只给概率", "available": True,
                            "group": "custom", "tag": "默认模板", "suffix": "默认模板",
                            "skeleton": True})
            for skey, sname, stext in JEV_SKELETONS:
                catalog.append({"type": "text", "key": "skel-jev-" + skey, "label": sname, "text": stext,
                                "source": "可拆的小模块骨架", "available": True, "group": "custom",
                                "tag": "默认模板", "suffix": "默认模板", "skeleton": True})
        else:
            src = self._ensure_source(context_sid)
            native = {x["name"]: x for x in src.get("native", [])}
            for row in self._base_rows(context_sid, sample=True):
                name, key = row["name"], row["key"]
                # 用户 m02159 图1：记忆 / 时间这类动态段不进下拉；身份 / 人格 / 工具已各有专块。
                if seg_key(name) in DYNAMIC or seg_key(name) in ("accounts", "persona", "tools"):
                    continue
                tail.append({"type": "framework", "key": key, "label": type_label("framework", key),
                             "text": row["text"], "template": native.get(key, {}).get("template", ""),
                             "source": src.get("source", ""), "available": True, "group": "default",
                             "tag": "框架原段", "suffix": "原段"})
        # 用户 m02159 图3：人格段的下拉不是「选模板」而是「选人格」——两边都只列人格文件。
        try:
            for item in await dp.persona_options():
                catalog.append({"type": "persona", "key": str(item.get("name") or ""),
                                "label": TYPE_LABELS["persona"], "text": str(item.get("preview") or ""),
                                "source": "人设页面", "available": True, "group": "default",
                                "tag": str(item.get("name") or ""), "suffix": "人格"})
        except Exception:
            logger.exception("[multi-decision] 列人格文件失败")
        if kind != "jev":
            # 用户 m06706：JEV 不发原生工具，工具段在它那儿没有意义，下拉里也不列。
            catalog.append({"type": "tools", "key": "", "label": TYPE_LABELS["tools"], "text": tools_text,
                            "source": "勾选注入：清单写在这一段下面",
                            "available": True, "group": "default", "tag": "工具", "suffix": "工具"})
        catalog.append({"type": "identity", "key": "", "label": TYPE_LABELS["identity"], "text": identity,
                        "source": ("框架自动填写，只读" if kind in ("decision", "jev")
                                   else "同会话框架身份原段"),
                        "available": True, "group": "default", "tag": "只读", "suffix": "只读"})
        # 用户 m02159 图3：原段一律排在默认模板后面（下拉里永远垫底）。
        catalog.extend(tail)
        catalog.append({"type": "realtime_marker", "key": "", "label": TYPE_LABELS["realtime_marker"],
                        "text": REALTIME_NOTE, "source": "位置说明，不注入", "available": True,
                        "group": "default", "tag": "不注入", "suffix": "位置"})
        catalog.append({"type": "system_memory", "key": "", "label": TYPE_LABELS["system_memory"],
                        "text": "", "source": "默认关着；启用后拼在本轮消息最下面", "available": True,
                        "group": "default", "tag": "位置", "suffix": "位置"})
        catalog.append({"type": "system_time", "key": "", "label": TYPE_LABELS["system_time"],
                        "text": "", "source": "默认关着；启用后拼在本轮消息最下面", "available": True,
                        "group": "default", "tag": "位置", "suffix": "位置"})
        # 用户 m36396：两边都有这一块，只是正文不一样——决策讲 JSON 格式，回复讲 <msg> 气泡。
        from .decision_phase import DECISION_FORMAT_HARD
        catalog.append({"type": "dynamic", "key": "",
                        "label": (TYPE_LABELS["dynamic"] if kind == "jev" else DYNAMIC_TEMPLATE_NAME),
                        "text": (DECISION_FORMAT_HARD if kind == "decision"
                                 else ("" if kind == "jev" else DYNAMIC_TEMPLATE_DEFAULT)),
                        "source": ("动态段：可选，拼在本轮聊天记录最后" if kind == "jev"
                                   else "动态段：拼在本轮聊天记录最后"),
                        "available": True, "group": "default", "tag": "动态段", "suffix": "动态段"})
        choices, picked = [], []
        try:
            # 用户 m11103：决策链与回复链各勾各的（prompt_tools.json 的 names / reply_names）。
            # 回复链第一次打开这一页时，把框架现有的工具全勾上——默认全选，升级后行为不变。
            choices = dp.tool_catalog()
            if kind == "reply":
                picked = self.reply_tools_for([c.get("name") for c in choices]) or []
            else:
                picked = list(getattr(context["opt"], "tools", None) or [])
        except Exception:
            logger.exception("[multi-decision] 列工具失败")
        # 用户 m21698：记忆插件那两段现在是布局里的真模块（plugin_memory / plugin_memory_ctx），
        # 由 _ensure_plugin_segments 补进 doc、由 _render 直接出 part，不再单独回一份 plugin_segments。
        # （旧的两张静态卡 ppPluginSegHtml 已删，避免同一段在页面上出现两次。）
        return {"ok": True, "kind": kind, "sid": sid, "context_sid": context_sid, "layout": doc,
                "default_layout": self.default_layout(kind, context_sid, migrate=False), "catalog": catalog,
                "revision": self.revision(), "inherited": inherited, "preview": preview,
                "native_templates": self._ensure_source(context_sid).get("native", []),
                "official_reference": copy.deepcopy(self._official),
                "tool_choices": choices, "tools_selected": picked,
                "templates": self.templates(),
                "default_notice": self._notice(kind, self._scope(sid), stored, doc)}
