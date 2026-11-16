"""渠道绑定：第一次认出来的渠道名记下来，配置改了也不丢（用户 m22269 第 3 条）。

价目表的「渠道」列是每次现读框架配置 `system_config.json` 算出来的
（`tracer._provider_label_of`）。用户在供应商那边删掉一个模型，配置里就再也
查不到这个名字，价目表里的渠道会凭空变空——他说「不应该第一次识别的时候就
绑定好么」，所以要一份落盘的记忆：

* `providers`：provider_id -> 渠道名。框架的模型键常写成 `<pid>:<mid>`，
  只要 pid 还在配置里，换个模型也不会丢渠道；
* `models`：模型名 / 模型键 -> 渠道名。pid 都不在配置里时（比如第三方接口被
  整个删掉）还能按名字兜住。

文件是插件数据目录里的 model_providers.json：

    {
      "providers": {"<provider_id>": {"label": "渠道名", "first_seen": ts, "updated": ts}},
      "models":    {"<模型名或模型键>": {"label": "渠道名", "provider_id": "", "first_seen": ts, "updated": ts}},
      "updated": ts
    }

只记展示名，不碰任何密钥；写盘是原子的，而且只有内容真的变了才写。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from core.plugin import logger

# 框架没拿到模型身份时账本里写的是这个名字（tracer.MODEL_UNKNOWN），
# 把它当成「一个模型」绑上渠道没有意义，还容易串到别的行上。
_SKIP_KEYS = {"未记录模型", "unknown", "未记录"}


def _clean_label(value) -> str:
    return str(value or "").strip()[:60]


def _clean_key(value) -> str:
    return str(value or "").strip()[:120]


class ProviderBindings:
    """模型/供应商 -> 渠道名 的只增不改记忆。名字只影响展示，不参与任何判定。"""

    def __init__(self, data_dir, filename: str = "model_providers.json"):
        self.path = Path(data_dir) / filename
        self._providers: dict[str, dict] = {}
        self._models: dict[str, dict] = {}
        self._load()

    # ---------- 读写 ----------

    def _load(self):
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                return
            self._providers = self._clean(raw.get("providers"))
            self._models = self._clean(raw.get("models"))
        except Exception:
            logger.exception("[multi-decision] 读取渠道绑定失败（忽略，用空表）")
            self._providers, self._models = {}, {}

    @staticmethod
    def _clean(raw) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return out
        for key, item in raw.items():
            name = _clean_key(key)
            if not name:
                continue
            if isinstance(item, dict):
                label = _clean_label(item.get("label"))
                if not label:
                    continue
                row = {"label": label}
                pid = _clean_key(item.get("provider_id"))
                if pid:
                    row["provider_id"] = pid
                for stamp in ("first_seen", "updated"):
                    num = item.get(stamp)
                    if isinstance(num, (int, float)) and not isinstance(num, bool):
                        row[stamp] = float(num)
                out[name] = row
            else:
                label = _clean_label(item)
                if label:
                    out[name] = {"label": label}
        return out

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "providers": self._providers,
                "models": self._models,
                "updated": time.time(),
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            logger.exception("[multi-decision] 保存渠道绑定失败")

    # ---------- 查询 ----------

    def provider(self, provider_id) -> str:
        pid = _clean_key(provider_id)
        row = self._providers.get(pid) if pid else None
        return str(row.get("label") or "") if isinstance(row, dict) else ""

    def model(self, *keys) -> str:
        for key in keys:
            name = _clean_key(key)
            if not name:
                continue
            row = self._models.get(name)
            if isinstance(row, dict) and row.get("label"):
                return str(row["label"])
        return ""

    def lookup(self, keys=(), provider_id: str = "") -> str:
        """先按 provider_id，再按模型名/模型键。都没有就返回空串（不编）。"""
        label = self.provider(provider_id)
        if label:
            return label
        return self.model(*list(keys or ()))

    # ---------- 记忆 ----------

    def remember(self, keys=(), label: str = "", provider_id: str = "") -> bool:
        """记下这次认出来的渠道名。内容没变就不写盘，返回是否真的变了。"""
        label = _clean_label(label)
        if not label:
            return False
        pid = _clean_key(provider_id)
        now = time.time()
        dirty = False
        if pid:
            cur = self._providers.get(pid) or {}
            if cur.get("label") != label:
                self._providers[pid] = {
                    "label": label,
                    "first_seen": float(cur.get("first_seen") or now),
                    "updated": now,
                }
                dirty = True
        for key in keys or ():
            name = _clean_key(key)
            if not name or name in _SKIP_KEYS:
                continue
            cur = self._models.get(name) or {}
            row = {
                "label": label,
                "first_seen": float(cur.get("first_seen") or now),
                "updated": now,
            }
            pid_of = pid or _clean_key(cur.get("provider_id"))
            if pid_of:
                row["provider_id"] = pid_of
            if cur.get("label") == label and cur.get("provider_id", "") == row.get("provider_id", ""):
                continue
            self._models[name] = row
            dirty = True
        if dirty:
            self._save()
        return dirty

    def all(self) -> dict:
        return {"providers": dict(self._providers), "models": dict(self._models)}
