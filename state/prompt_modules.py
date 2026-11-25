"""提示词模块注册口（用户 m02800：提示词模块-地基方案）。

黑话、样式学习、模拟日程这类功能不该再改装配代码——它们注册一个模块，
往动态段里塞自己算好的文字就行。装配侧只认「模块」这一种布局块。

render 必须是同步的：_render / apply_reply / preview_reply 都是同步函数，
不能在这里 await 数据库。内置模块靠后台 tick 算好放内存，外部插件在
initialize 里注册、闭包持有自己的实例。
"""

from __future__ import annotations

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")


class PromptModule:
    """一个提示词模块。子类只实现 render()，其余是元信息。"""

    #: 唯一键，小写英文，如 "jargon"。布局块靠它认人，注册后不能改。
    name = ""
    #: 中文名，设置页卡片标题用，如 "黑话"。
    label = ""
    #: 0 = 不限；> 0 时注册表按字符截断，模块自己不用管长度。
    max_chars = 0
    #: 老布局里没有这块时自动补一块（地基阶段不注册模块，页面看不到变化）。
    auto_block = True

    def render(self, ctx):
        """返回要注入的正文。ctx 只有 sid / kind / sample / request 四个字段。"""
        return ""


class PromptModuleRegistry:
    """插件级的模块表。同一线程读写，不加锁。"""

    def __init__(self):
        self._modules = {}

    # ---- 注册 ----
    def register(self, module):
        name = str(getattr(module, "name", "") or "").strip()
        if not name:
            raise ValueError("提示词模块必须有 name")
        if name in self._modules:
            raise ValueError("提示词模块 %s 已经注册过了" % name)
        self._modules[name] = module
        logger.info("[multi-decision] 提示词模块已注册：%s（%s）", name, self.label(name) or name)
        return module

    def unregister(self, name):
        return self._modules.pop(str(name or "").strip(), None) is not None

    def clear(self):
        self._modules.clear()

    # ---- 查询 ----
    def get(self, name):
        return self._modules.get(str(name or "").strip())

    def names(self):
        return list(self._modules)

    def label(self, name):
        module = self.get(name)
        return str(getattr(module, "label", "") or "") if module is not None else ""

    def blocks(self):
        """要自动补进布局的模块名，按注册顺序。"""
        return [n for n, m in self._modules.items() if bool(getattr(m, "auto_block", True))]

    # ---- 渲染 ----
    def render(self, name, ctx):
        module = self.get(name)
        if module is None:
            return ""
        try:
            text = module.render(dict(ctx or {}))
        except Exception:
            logger.warning("[multi-decision] 提示词模块 %s 渲染失败，这一段跳过", name, exc_info=True)
            return ""
        text = str(text or "").strip()
        if not text:
            return ""
        try:
            limit = int(getattr(module, "max_chars", 0) or 0)
        except Exception:
            limit = 0
        if limit > 0 and len(text) > limit:
            logger.debug("[multi-decision] 提示词模块 %s 超长（%d > %d），已截断", name, len(text), limit)
            text = text[:limit].rstrip()
        return text
