"""私有补丁准入层。

本插件有几处直接改宿主对象（provider_mgr.get_model_client、event_bus.publish、
message_delivery.send_message_chain、用量探针、prompt 的 kwargs），宿主升级换结构时
这些补丁会悄无声息地失效。用户 m24700 第 ⑤ 条：打之前统一检查版本与结构，不满足就
跳过并留一条 warning，装了几个、跳了哪个都能在 /health 和设置页看到。

补丁本身仍由各模块实现；这里只管「装之前问一句、装完记账」。
"""

import threading
from typing import Any, Callable, Dict, Optional

try:  # 宿主里走 core.logging_manager；单独跑测试时退回标准库
    from core.logging_manager import get_logger

    _logger = get_logger("multi_decision", "orange")
except Exception:  # pragma: no cover - 只在脱离宿主时走到
    import logging

    _logger = logging.getLogger("multi_decision.patch_gate")


from . import compat


def version_tuple(text: Any) -> tuple:
    """把 "v3.3.0" / "3.3.0-beta" 变成 (3, 3, 0)；没有数字时返回空元组。"""
    raw = str(text or "").strip().lstrip("vV")
    parts = []
    for chunk in raw.replace("-", ".").replace("_", ".").replace("+", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def require_callable(owner: Any, attr: str, label: str = "宿主") -> str:
    """检查 owner.attr 可调用；可以就返回空串，否则返回一句人话。"""
    if owner is None:
        return f"{label}对象缺失"
    if not hasattr(owner, attr):
        return f"{label}缺 {attr}"
    if not callable(getattr(owner, attr, None)):
        return f"{label}.{attr} 不可调用"
    return ""


class PatchGate:
    """记账 + 准入：每个私有补丁登记一条，装成功标 applied，跳过标 skipped。"""

    def __init__(self, ctx: Any = None, logger: Any = None):
        self._ctx = ctx
        self._logger = logger or _logger
        self._lock = threading.RLock()
        self._entries = []

    # -- 对外 -----------------------------------------------------------
    def install(
        self,
        name: str,
        label: str,
        apply: Callable[[], Any],
        requires: Optional[Callable[[], str]] = None,
        min_host: str = "",
    ) -> bool:
        """检查通过就执行 apply；apply 返回 False 视为结构不匹配，一并跳过。"""
        reason = self._check(min_host, requires)
        if reason:
            self._skip(name, label, reason)
            return False
        try:
            result = apply()
        except Exception as exc:  # 补丁装不上不能连累插件启动
            self._skip(name, label, f"安装抛错：{exc!r}")
            return False
        if result is False:
            self._skip(name, label, "宿主结构不匹配")
            return False
        self._record(name, label, "applied", "")
        return True

    def declare(
        self,
        name: str,
        label: str,
        requires: Optional[Callable[[], str]] = None,
        min_host: str = "",
    ) -> bool:
        """只登记不安装：给每次请求才生效的补丁（如 prompt kwargs）。"""
        reason = self._check(min_host, requires)
        if reason:
            self._skip(name, label, reason)
            return False
        self._record(name, label, "declared", "")
        return True

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            entries = [dict(item) for item in self._entries]
        skipped = [item for item in entries if item["state"] == "skipped"]
        applied = [item for item in entries if item["state"] in ("applied", "declared")]
        return {
            "host": compat.host_version(),
            "total": len(entries),
            "applied": len(applied),
            "skipped": skipped,
            "entries": entries,
        }

    def line(self) -> str:
        """一行摘要，给启动自检日志与设置页顶部用。"""
        data = self.summary()
        text = f"宿主 {data['host'] or '未知'} · 私有补丁 {data['applied']}/{data['total']} 就绪"
        if data["skipped"]:
            text += "（跳过：" + "、".join(item["label"] for item in data["skipped"]) + "）"
        return text

    def reset(self) -> None:
        """测试用：清掉已记的条目。"""
        with self._lock:
            self._entries = []

    # -- 内部 -----------------------------------------------------------
    def _check(self, min_host: str, requires) -> str:
        host = compat.host_version()
        if min_host and host:
            need = version_tuple(min_host)
            have = version_tuple(host)
            if need and have and have < need:
                return f"宿主 {host} 低于要求 {min_host}"
        if requires is not None:
            try:
                reason = requires()
            except Exception as exc:
                return f"结构检查抛错：{exc!r}"
            if reason:
                return str(reason)
        return ""

    def _skip(self, name: str, label: str, reason: str) -> None:
        self._record(name, label, "skipped", reason)
        try:
            self._logger.warning("[multi-decision] 私有补丁「%s」已跳过：%s", label, reason)
        except Exception:
            pass

    def _record(self, name: str, label: str, state: str, reason: str) -> None:
        with self._lock:
            self._entries.append({"name": name, "label": label, "state": state, "reason": reason})
