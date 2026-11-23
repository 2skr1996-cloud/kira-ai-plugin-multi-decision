"""框架能力探测层。

插件有几处私有补丁（配置接管、媒体包装、提示词 kwargs、内置聊天接管、默认决策槽）
依赖框架内部实现。框架升级后这些依赖可能悄悄消失——补丁照打、日志照常，功能却不再
生效。这里统一探测一次并缓存，调用方按探测结果决定「打补丁」还是「记一条 WARNING
跳过」，探测本身任何情况下都不抛错。

约定：所有 has_* 探测失败一律返回 False；has_llm_client_base() 取不到返回 None。
"""
from __future__ import annotations

import importlib
import inspect

_CACHE: dict = {}


def _module(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _cached(key: str, probe, default=False):
    if key in _CACHE:
        return _CACHE[key]
    try:
        value = probe()
    except Exception:
        value = default
    if value is None:
        value = default
    _CACHE[key] = value
    return value


def has_session_llm() -> bool:
    """get_default_llm_client 是否接受 session 关键字（3.2.1 起）。

    3.2.0 只有无参版本，传 session 会 TypeError；有它才能按会话覆盖模型取客户端。
    """
    def probe():
        mod = _module("core.plugin.plugin_context")
        fn = getattr(getattr(mod, "PluginContext", None), "get_default_llm_client", None)
        if not callable(fn):
            return False
        return "session" in inspect.signature(fn).parameters
    return _cached("session_llm", probe)


def has_default_decision() -> bool:
    """ProviderManager 是否有 get_default_decision（3.2.1 起）。

    有它才能让 JEV 主模型留空时跟随 KiraAI 设置里的「默认决策模型」。
    """
    def probe():
        mod = _module("core.provider.provider_manager")
        cls = getattr(mod, "ProviderManager", None)
        return callable(getattr(cls, "get_default_decision", None))
    return _cached("default_decision", probe)


def has_plugin_toggle() -> bool:
    """PluginManager 是否有 set_plugin_enabled（3.2.1 起）。

    注意：它会 _save_plugin_state() 持久化，只在内置聊天接管彻底失效时当兜底用。
    """
    def probe():
        mod = _module("core.plugin.manager")
        cls = getattr(mod, "PluginManager", None)
        return callable(getattr(cls, "set_plugin_enabled", None))
    return _cached("plugin_toggle", probe)


def has_prompt_kwargs() -> bool:
    """Prompt 是否带 kwargs 字典（提示词轮数补丁依赖它改渲染参数）。"""
    def probe():
        mod = _module("core.prompt_manager")
        cls = getattr(mod, "Prompt", None)
        if cls is None:
            return False
        p = cls("probe", name="probe", source="system", persist=False,
                render_template=False, end="")
        return isinstance(getattr(p, "kwargs", None), dict)
    return _cached("prompt_kwargs", probe)


def has_config_patch_target() -> bool:
    """KiraConfig.get_config 是否可打类方法补丁（代理设置接管依赖它）。"""
    def probe():
        mod = _module("core.config.config_loader")
        cls = getattr(mod, "KiraConfig", None)
        return callable(getattr(cls, "get_config", None))
    return _cached("config_patch", probe)


def has_sessions_kind() -> bool:
    """SessionManager.get_sessions 是否接受 kind 关键字（3.2.0 起）。

    有它就能只取 IM 会话，省掉插件自己遍历 subagent 会话那一步。
    """
    def probe():
        mod = _module("core.chat.session_manager")
        fn = getattr(getattr(mod, "SessionManager", None), "get_sessions", None)
        if not callable(fn):
            return False
        return "kind" in inspect.signature(fn).parameters
    return _cached("sessions_kind", probe)


def has_llm_client_base():
    """LLMModelClient 基类，取不到返回 None。

    框架在 ON_LLM_REQUEST 里按 isinstance(m, LLMModelClient) 过滤 model_group，
    所以思考等级包装必须是真的子类。三级兜底：core.provider 导出 → 子模块 →
    调用方自己顺着客户端 MRO 找（这里只负责前两级）。
    """
    def probe():
        for name in ("core.provider", "core.provider.provider"):
            mod = _module(name)
            base = getattr(mod, "LLMModelClient", None)
            if isinstance(base, type):
                return base
        return None
    return _cached("llm_client_base", probe, default=None)


def describe() -> str:
    """一行摘要，给启动自检日志用。"""
    flags = (
        ("session_llm", has_session_llm()),
        ("default_decision", has_default_decision()),
        ("plugin_toggle", has_plugin_toggle()),
        ("prompt_kwargs", has_prompt_kwargs()),
        ("config_patch", has_config_patch_target()),
        ("sessions_kind", has_sessions_kind()),
        ("llm_client_base", has_llm_client_base() is not None),
    )
    return " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in flags)


def reset_cache() -> None:
    """测试用：清掉缓存，让下一次探测重新读框架。"""
    _CACHE.clear()
