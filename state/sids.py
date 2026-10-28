"""会话 id 归一化。

框架给的会话 id 形如 `{机器人名}:{gm|dm}:{对方 id}`。机器人改个名字，同一个群
就变成另一条会话——窗口、统计、单群配置全部从头开始（v2.42 之前就吃过这个亏：
磁盘上同时躺着 `桃华_gm_1145141974` 和 `樱井桃华_gm_1145141974` 两套数据）。

这里只保留真正稳定的那两段：类型 + 对方 id。
"""
from __future__ import annotations

TYPES = ("gm", "dm")


def normalize_sid(sid: str) -> str:
    """把 `桃华:gm:1145141974` 归成 `gm:1145141974`。已经归一化的原样返回。

    认不出来（段数不够、类型不是 gm/dm）就原样返回，不做猜测。
    """
    text = str(sid or "").strip()
    parts = text.split(":")
    if len(parts) >= 3 and parts[-2] in TYPES and parts[-1]:
        return parts[-2] + ":" + parts[-1]
    return text
