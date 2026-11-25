"""插件注入段：发现、建卡、取正文、原样放回。

用户 m20722 / m23037 / m09699：别的插件往 system_prompt / user_prompt 里塞的段，
第一次见到就登记（见 state/plugin_segs.py），布局侧据此自动补一张只读卡；装配时
system 那批原样放回同一个 Prompt 对象，user 那批本来就不重拼、只负责可见与可关。

这里只放「跟布局无关」的那部分——判据、发现、建卡、取样本、拼 part、收集原始对象。
真正插进 _render / apply_reply 的分支仍留在 prompt_layout.py：它们要动 parts 列表
和 request.system_prompt，属于布局装配本身。
"""

from core.logging_manager import get_logger

from .plugin_segs import FRAMEWORK_NAMES
from .sids import normalize_sid

logger = get_logger("multi_decision", "orange")


class PluginSegCards:
    """挂在 PromptLayouts 上，借用它的 plugin / _sources / _applied / _text。"""

    def __init__(self, layouts):
        self.layouts = layouts

    # ---------- 判据 ----------

    def registry(self):
        """登记清单（state/plugin_segs.py 的 PluginSegs）；插件没挂上时是 None。"""
        return getattr(self.layouts.plugin, "plugin_segs", None)

    def is_known(self, name):
        """用户 m20722：这个名字是不是已经建过卡的插件注入段。"""
        reg = self.registry()
        if reg is None:
            return False
        try:
            return str(name or "") in reg.names()
        except Exception:
            logger.exception("[multi-decision] 读插件注入段清单失败，按未知段处理")
            return False

    @staticmethod
    def is_framework(name):
        """用户 m20722：框架自己的段名——按布局走，不在布局里就照旧丢。"""
        return str(name or "") in FRAMEWORK_NAMES

    # ---------- 发现 ----------

    def note(self, request):
        """用户 m20722：把这一轮不认识的段名记进清单，布局侧据此补卡。

        用户 m23037：system_prompt 与 user_prompt 都扫——Alife 的记忆块塞在 user_prompt
        里，以前只认 system，所以页面上一直看不见它。位置分开记，卡片才知道去哪儿找正文。
        """
        reg = self.registry()
        if reg is None:
            return
        for attr, loc in (("system_prompt", "system"), ("user_prompt", "user")):
            for p in getattr(request, attr, []) or []:
                name = str(getattr(p, "name", "") or "")
                if not reg.is_plugin_name(name):
                    continue
                try:
                    body = self.layouts._text(p)
                except Exception:
                    body = ""
                # 用户 m09215：正文样本一起记下来，预览换会话时才有东西顶位。
                reg.note(name, len(body), loc, body)

    def note_late(self, sid):
        """用户 m09537：响应时再扫一遍段名。

        排在咱们后面跑的插件，注入的段在钩子那一刻还看不见；等模型回话时
        request 上已经是最终形态，这时候补扫一次，卡片下一轮就能出现。
        """
        got = self.layouts._applied.get(normalize_sid(sid))
        if not got:
            return
        try:
            self.note(got[0])
        except Exception:
            logger.debug("[multi-decision] 响应时补扫插件注入段失败", exc_info=True)

    # ---------- 建卡 ----------

    def ensure_cards(self, kind, doc):
        """用户 m20722：别的插件注入过的段，各补一张只读卡。

        名字来自 plugin_segs.json（第一次见到时登记）。已经有的卡保留——用户挪过的位置
        不动；新卡按登记顺序接在「工具说明」后面，跟记忆插件那张卡同一处。

        用户 m23037：注入在 user_prompt 里的段（Alife 的记忆块）也建卡，但位置不一样——
        它们跟动态段一样拼在本轮消息里，所以排在动态段前面，不跟 system 那批挤在一起。
        """
        # 延迟导入：prompt_layout 在模块级 import 本文件，这里再在模块级反向 import 会成环。
        from .prompt_layout import module, type_label

        # 用户 m09986 第 7 条：决策与 JEV 也建卡，但默认关着——那两路请求是插件自己拼的，
        # 没有别的插件往里注入，卡片里放的是上一次抓到的正文；要不要让判断模型看见由用户决定。
        # 用户 m10826：位置照登记表走，不再一律塞进 system——回复那边怎么分，这两路就怎么分。
        default_on = kind == "reply"
        if not isinstance(doc, dict) or doc.get("mode") != "modules":
            return doc
        mods = doc.get("modules")
        if not isinstance(mods, list):
            return doc
        reg = self.registry()
        if reg is None:
            return doc
        try:
            wanted = []
            for name in reg.names():
                if not name:
                    continue
                info = reg.info(name) or {}
                loc = "user" if str(info.get("location") or "").strip().lower() == "user" else "system"
                wanted.append((name, loc))
        except Exception:
            logger.exception("[multi-decision] 取插件注入段清单失败，布局按原样")
            return doc
        if not wanted:
            return doc
        locs = dict(wanted)
        # 已有卡：注入位置跟着清单走（插件改了实现时卡片要跟着换），其余字段一律不动。
        rest = []
        moved_sys, moved_user = [], []
        for m in mods:
            if isinstance(m, dict) and str(m.get("type") or "") == "plugin_seg":
                want = locs.get(str(m.get("key") or ""))
                if want and str(m.get("location") or "system") != want:
                    # 用户 m10826：位置真的变了就顺手挪到对应那一侧——以前决策/JEV 一律按
                    # system 排，现在按登记表分，老卡不能留在原地（JEV 那三张原本落在动态段后面）。
                    m = dict(m, location=want)
                    (moved_user if want == "user" else moved_sys).append(m)
                    continue
            rest.append(m)
        # 用户 m10826：算「已经有卡」要看原清单——位置变了的卡已经从 rest 里摘出去等着重排，
        # 拿 rest 算会把它当成缺卡，同一段建出第二张。
        have = {str(m.get("key") or "") for m in mods
                if isinstance(m, dict) and str(m.get("type") or "") == "plugin_seg"}
        missing = [(n, loc) for n, loc in wanted if n not in have]
        if not missing and rest == mods:
            return doc
        out = dict(doc)
        used = {str(m.get("id") or "") for m in rest if isinstance(m, dict)}
        added_sys, added_user = moved_sys, moved_user
        for name, loc in missing:
            mid, n = "plugin-seg-" + name, 2
            while mid in used:
                mid = "plugin-seg-%s-%d" % (name, n)
                n += 1
            used.add(mid)
            card = module(mid, "plugin_seg", type_label("plugin_seg", name), name, "", default_on, loc)
            (added_user if loc == "user" else added_sys).append(card)
        if added_sys:
            # 用户 m10826：JEV 布局里没有「工具说明」这一块，以前找不到就落到最末尾
            # （排在动态段后面，看着像注进动态段）。没有 tools 就接在实时窗口/动态段之前。
            at = len(rest)
            for i, m in enumerate(rest):
                if str(m.get("type") or "") == "tools":
                    at = i + 1
                    break
            else:
                for i, m in enumerate(rest):
                    if str(m.get("type") or "") in ("realtime_marker", "dynamic", "system_memory",
                                                    "system_time", "system_dynamic"):
                        at = i
                        break
            rest[at:at] = added_sys
        if added_user:
            at = len(rest)
            for i, m in enumerate(rest):
                if str(m.get("type") or "") == "dynamic":
                    at = i
                    break
            rest[at:at] = added_user
        out["modules"] = rest
        return out

    # ---------- 渲染 ----------

    def sample(self, sid, key):
        """预览用：这一轮没抓到真实正文时，拿上一次抓到的正文顶位。

        用户 m09215：先按会话找（最准），找不到再退回登记表里存的那份样本——
        切到没发过请求的会话、或者刚重载还没抓过，卡片就不会空成 0 字。
        """
        src = self.layouts._sources.get(normalize_sid(sid)) or {}
        for row in src.get("rows") or []:
            if str((row or {}).get("name") or "") == key:
                got = str(row.get("text") or "")
                if got.strip():
                    return got
                break
        reg = self.registry()
        if reg is not None:
            try:
                return str((reg.info(key) or {}).get("sample") or "")
            except Exception:
                logger.debug("[multi-decision] 取插件注入段样本失败：%s", key, exc_info=True)
        return ""

    @staticmethod
    def heading(key):
        """用户 m09986 第 2 条：注入段正文前面补一行小标题，形如「## Alife_rules」。

        名字就取段名、首字母大写——跟框架自己写的「## Tools 说明」一个长相，
        模型一眼能看出这一段是谁给的。
        """
        name = str(key or "").strip()
        if not name:
            return ""
        return "## " + name[:1].upper() + name[1:]

    def part(self, m, sid, context=None, kind=""):
        """用户 m20722：别的插件注入的段——正文原样放回，这里只决定它放在哪。

        有原始 Prompt 对象就带 passthrough，装配时原样 extend 回去（不重造，保留它自己的
        render_template / persist / end）；没有就只给预览看上一轮抓到的正文。

        用户 m23037：注入在 user_prompt 里的段（Alife 的记忆块）不走 passthrough——
        user_prompt 本来就不重拼，这里只负责让它出现在预览里、以及关掉时把它摘掉。
        """
        key = str(m.get("key") or "")
        loc = "user" if str(m.get("location") or "").strip().lower() == "user" else "system"
        ctx = context or {}
        pool = ctx.get("plugin_segs_user") if loc == "user" else ctx.get("plugin_segs")
        live = list((pool or {}).get(key) or [])
        text = ""
        for p in live:
            try:
                got = self.layouts._text(p)
            except Exception:
                logger.debug("[multi-decision] 插件注入段 %s 取正文失败", key, exc_info=True)
                got = ""
            text = (text + "\n\n" + got).strip() if text else got
        sample_only = not bool(live)
        if sample_only:
            text = self.sample(sid, key)
        body = text
        head = self.heading(key)
        part = {"id": m["id"], "label": m["label"], "name": key or "plugin_seg",
                "type": "plugin_seg",
                "text": (head + "\n" + body) if (head and body) else body,
                "chars": len(body), "heading": head,
                "sample_only": sample_only, "location": loc}
        if loc == "user":
            # 拼在本轮消息里，不进 system 预览正文。
            part["dynamic"] = True
            # 用户 m10826：回复那边这段本来就在 user_prompt 里，不能再标 inject（会重复拼一次）；
            # 决策 / JEV 的请求是插件自己拼的，得靠 inject 才会进动态段。
            if kind in ("decision", "jev"):
                part["inject"] = True
        elif live:
            part["passthrough"] = live
        return part

    # ---------- 装配 ----------

    def collect(self, request):
        """按名字收好原始 Prompt 对象，返回 (system 那批, user 那批)。

        system 那批要原样放回（passthrough），user 那批要能按卡片开关摘掉。
        """
        sys_map, user_map = {}, {}
        for p in list(getattr(request, "system_prompt", []) or []):
            nm = str(getattr(p, "name", "") or "")
            if self.is_known(nm):
                sys_map.setdefault(nm, []).append(p)
        for p in list(getattr(request, "user_prompt", []) or []):
            nm = str(getattr(p, "name", "") or "")
            if self.is_known(nm):
                user_map.setdefault(nm, []).append(p)
        return sys_map, user_map

    @staticmethod
    def off_user_keys(doc):
        """卡片关掉的 user 侧注入段——装配完要从本轮消息里摘掉。"""
        return {str(m.get("key") or "") for m in (doc.get("modules") or [])
                if str(m.get("type") or "") == "plugin_seg"
                and str(m.get("location") or "") == "user"
                and not m.get("enabled", True)}
