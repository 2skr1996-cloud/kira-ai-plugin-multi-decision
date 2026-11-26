"""框架能力探测层。

插件有几处私有补丁（配置接管、媒体包装、提示词 kwargs、内置聊天接管、默认决策槽）
依赖框架内部实现。框架升级后这些依赖可能悄悄消失——补丁照打、日志照常，功能却不再
生效。这里统一探测一次并缓存，调用方按探测结果决定「打补丁」还是「记一条 WARNING
跳过」，探测本身任何情况下都不抛错。

约定：所有 has_* 探测失败一律返回 False；llm_client_base() / model_type() 等取不到返回 None。

探测点只留在这个文件：别的模块不要自己 importlib、inspect.signature 或 getattr(mgr, "...")
去猜宿主结构，一律走这里的函数（用户 m24378 第 ① 条：兼容层单点化）。
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


def accepts_kwarg(fn, name: str) -> bool:
    """fn 是否接受关键字 name（签名探测失败一律当不接受）。

    框架少数方法在不同版本里多出可选关键字（session、kind、extra_event）。
    调用前先问这里，再决定传不传——不要调用失败后重放，发布类调用可能有副作用。
    """
    if not callable(fn):
        return False
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def has_session_llm() -> bool:
    """get_default_llm_client 是否接受 session 关键字（3.2.1 起）。

    3.2.0 只有无参版本，传 session 会 TypeError；有它才能按会话覆盖模型取客户端。
    """
    def probe():
        mod = _module("core.plugin.plugin_context")
        fn = getattr(getattr(mod, "PluginContext", None), "get_default_llm_client", None)
        if not callable(fn):
            return False
        return accepts_kwarg(fn, "session")
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


def plugin_toggle_setter(mgr):
    """PluginManager.set_plugin_enabled 的绑定方法，取不到返回 None。

    它会 _save_plugin_state() 持久化，只在内置聊天接管彻底失效时当兜底用。
    """
    setter = getattr(mgr, "set_plugin_enabled", None)
    return setter if callable(setter) else None


def default_llm_client(ctx, session=None):
    """框架默认 LLM 客户端，返回 (client, note)。

    session 给了且宿主支持按会话取（3.2.1 起），就用会话自己选的模型。取不到
    返回 (None, note)，宿主抛的错一并吞掉，note 供调用方记日志。
    """
    if ctx is None:
        return None, ""
    fn = getattr(ctx, "get_default_llm_client", None)
    if not callable(fn):
        return None, "框架没有 get_default_llm_client"
    try:
        if session is not None and has_session_llm():
            return fn(session=session), ""
        return fn(), ""
    except Exception as exc:
        return None, "取默认 LLM 客户端失败：%s: %s" % (type(exc).__name__, exc)


def default_chat_client(ctx):
    """插件没点名模型时用哪个客户端，返回 (client, note)。

    先问框架默认快速模型（3.2.1 起）；3.3.0 宿主有这个方法，但没配快速模型时会
    返回 None 或抛错，这时继续问默认 LLM。两处都拿不到返回 (None, note)。note 是
    给调用方记日志的一句说明，正常为空串。解只读代理由调用方按需做。
    """
    if ctx is None:
        return None, ""
    note = ""
    getter = getattr(ctx, "get_default_fast_llm_client", None)
    client = None
    if callable(getter):
        try:
            client = getter()
        except Exception as exc:
            note = "取默认快速模型失败，退回默认 LLM：%s: %s" % (type(exc).__name__, exc)
    if client is None:
        client, why = default_llm_client(ctx)
        if why and not note:
            note = why
    return client, note

def default_decision_client(mgr):
    """官方默认决策槽（3.2.1 起）给的客户端。

    宿主没这个方法时返回 None；槽里空着时宿主会抛 TypeError/ValueError 或返回
    None，交给调用方按自己的文案处理。
    """
    fn = getattr(mgr, "get_default_decision", None)
    if not callable(fn):
        return None
    return fn()


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
        return accepts_kwarg(fn, "kind")
    return _cached("sessions_kind", probe)


_CLIENT_BASE_MODULES = ("core.provider", "core.provider.provider")
_CLIENT_BASE_CACHE: dict = {}


def client_base(name: str, client=None):
    """按名字取框架客户端基类，取不到返回 None。

    模块顺序固定：core.provider 导出 → core.provider.provider；都给不出时，如果
    传了 client，再顺着它自己的 MRO 找同名类（框架发来的客户端本来就是基类子类，
    core.provider 的导出变了也还能认）。模块级结果按名字缓存，MRO 那一步不缓存。
    """
    if name in _CLIENT_BASE_CACHE:
        base = _CLIENT_BASE_CACHE[name]
    else:
        base = None
        for module_name in _CLIENT_BASE_MODULES:
            mod = _module(module_name)
            found = getattr(mod, name, None)
            if isinstance(found, type):
                base = found
                break
        _CLIENT_BASE_CACHE[name] = base
    if base is not None:
        return base
    if client is not None:
        for cls in type(client).__mro__:
            if cls.__name__ == name:
                return cls
    return None


def llm_client_base(client=None):
    """LLMModelClient 基类，取不到返回 None。

    框架在 ON_LLM_REQUEST 里按 isinstance(m, LLMModelClient) 过滤 model_group，
    所以思考等级包装必须是真的子类；取不到就原样用客户端。
    """
    return client_base("LLMModelClient", client)


def has_llm_client_base():
    """旧名字（只判有没有），返回基类或 None；新代码用 llm_client_base()。"""
    return llm_client_base()


def model_type():
    """core.provider.ModelType 枚举，取不到返回 None（旧宿主没有 DECISION）。"""
    def probe():
        mod = _module("core.provider")
        mt = getattr(mod, "ModelType", None)
        return mt if isinstance(mt, type) else None
    return _cached("model_type", probe, default=None)


def config_timezone(cfg):
    """框架配置对象上的时区，取不到返回 None（旧宿主没有 get_timezone）。"""
    fn = getattr(cfg, "get_timezone", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None

def host_version() -> str:
    """宿主版本号（如 "3.3.0"），读不到返回空串。

    只用于启动自检与私有补丁准入的最低版本判断。宿主没有统一的版本常量，
    依次看 core.config.default.VERSION、core.config.VERSION、core.VERSION；
    都读不到就返回空串——版本未知时不拦补丁，结构检查还会再看一遍。
    """
    def probe():
        for module_name in ("core.config.default", "core.config", "core"):
            mod = _module(module_name)
            raw = getattr(mod, "VERSION", None)
            if isinstance(raw, str) and raw.strip():
                return raw.strip().lstrip("vV")
        return ""
    return _cached("host_version", probe, default="")


def direct_client(client):
    """解开只读代理客户端，拿回内层真实客户端。

    有的第三方插件（流式加速器）会把 get_model_client 的返回值换成自己的代理，
    chat() 强制走流式；而流式响应的分片里通常不带 usage，决策这类「只要一次完整
    响应」的调用就会把 token 记成 0，记录页的轨迹也跟着空掉。这里只在代理明确
    暴露了内层客户端（_wrapped / _inner 槽位）时才解包，且要求内层真的有 chat；
    其余情况原样返回，绝不猜——猜错会把请求发到一个根本没有 chat 的对象上。
    """
    seen = set()
    cur = client
    for _ in range(4):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        inner = None
        for name in ("_wrapped", "_inner"):
            try:
                value = object.__getattribute__(cur, name)
            except Exception:
                continue
            if value is not None and value is not cur and callable(getattr(value, "chat", None)):
                inner = value
                break
        if inner is None:
            break
        cur = inner
    return cur

def flags() -> dict:
    """七项能力探测结果，键名与 describe() 一致（/health 与设置页直接读）。"""
    return {
        "session_llm": has_session_llm(),
        "default_decision": has_default_decision(),
        "plugin_toggle": has_plugin_toggle(),
        "prompt_kwargs": has_prompt_kwargs(),
        "config_patch": has_config_patch_target(),
        "sessions_kind": has_sessions_kind(),
        "llm_client_base": has_llm_client_base() is not None,
    }


def describe() -> str:
    """一行摘要，给启动自检日志用。"""
    return " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in flags().items())


def reset_cache() -> None:
    """测试用：清掉缓存，让下一次探测重新读框架。"""
    _CACHE.clear()
    _CLIENT_BASE_CACHE.clear()
