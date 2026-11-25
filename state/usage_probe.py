"""模型用量探针：在宿主 LLM / 嵌入 / 排序客户端这一层记下真实 token。

价目页要统计其他插件（尤其 Alife）的用量。宿主自己的 telemetry_llm_usage 只覆盖
工作流里的调用，插件直接拿 client.chat() / client.embed() / client.rerank() 的用量
没人记。这里用 monkey-patch 在客户端方法外面包一层，把真实 token 追加到
plugin_data/<插件>/plugin_usage.jsonl，按月轮转。

三条补丁：
1. LLMModelClient 子类自己定义的 chat / chat_stream：token 在 LLMResponse 或流式
   最后一帧的 usage 里。
2. openai.resources.embeddings.AsyncEmbeddings.create：token 在 response.usage 里
   （宿主五个嵌入客户端都走这个 SDK 方法，打一个类级补丁就全覆盖）。
3. httpx.AsyncClient.send 里 URL 带 rerank 的请求：token 在响应 meta.tokens 里。

设计约束：
- 全局补丁，绝不能影响宿主调用：所有记录逻辑 try/except 包死，异常只记 debug。
- 幂等：类属性 _mdp_probe 标记，重复 install 不套娃；uninstall 还原原函数。
- 归属：从调用栈往上找插件模块名，映射成插件 id；本插件自己的调用跳过（已有账本）。
- 写盘：后台 daemon 线程 + queue，不阻塞事件循环；按月轮转。
"""

from __future__ import annotations

import contextvars
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # 宿主里走 core.logging_manager；单独跑测试时退回标准库
    from core.logging_manager import get_logger

    logger = get_logger("multi_decision", "orange")
except Exception:  # pragma: no cover - 只在脱离宿主时走到
    import logging

    logger = logging.getLogger("multi_decision.usage_probe")


PLUGIN_ID = "kira-ai-plugin-multi-decision"
LEDGER_NAME = "plugin_usage.jsonl"
ARCHIVE_DIR = "archive"
KINDS = ("llm", "embed", "rerank")
_MAX_FRAME_DEPTH = 24
_SCAN_INTERVAL = 30.0
_STOP = object()

_LOCK = threading.RLock()
_INSTALLED = False
_ENABLED = True
_DATA_DIR: Optional[Path] = None
_ORIGINALS: List[Tuple[Any, str, Any]] = []
_WRAPPED: Dict[str, str] = {}
_QUEUE: "queue.Queue[Any]" = queue.Queue()
_WRITER: Optional[threading.Thread] = None
_WRITER_STOP: Optional[threading.Event] = None
_CURRENT_MONTH = ""
_DEPTH = contextvars.ContextVar("mdp_probe_depth", default=0)
_EMBED_SLOT = contextvars.ContextVar("mdp_probe_embed_slot", default=None)
_RERANK_SLOT = contextvars.ContextVar("mdp_probe_rerank_slot", default=None)
_REGISTRY_CACHE: Dict[str, Any] = {"at": 0.0, "map": {}}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _as_int(value) -> int:
    try:
        if value is None or isinstance(value, bool):
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value) -> Optional[int]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _texts_chars(texts) -> int:
    try:
        if texts is None:
            return 0
        if isinstance(texts, str):
            return len(texts)
        if isinstance(texts, (list, tuple, set)):
            return sum(len(str(item)) for item in texts)
        return len(str(texts))
    except Exception:
        return 0


def _embed_chars(args, kwargs) -> int:
    texts = kwargs.get("texts")
    if texts is None and args:
        texts = args[0]
    return _texts_chars(texts)


def _rerank_chars(args, kwargs) -> int:
    query = kwargs.get("query")
    documents = kwargs.get("documents")
    if query is None and args:
        query = args[0]
    if documents is None and len(args) > 1:
        documents = args[1]
    return _texts_chars(query) + _texts_chars(documents)


def _model_of(client) -> Tuple[str, str]:
    """返回 (价目页用的模型键, 展示名)。键跟 tracer.usage_identity 一致。"""
    try:
        model = getattr(client, "model", None)
    except Exception:
        model = None
    if model is None:
        return "", ""
    try:
        mid = str(getattr(model, "model_id", "") or "")
        pid = str(getattr(model, "provider_id", "") or "")
        name = str(getattr(model, "model_name", "") or "")
    except Exception:
        return "", ""
    key = (pid + ":" + mid) if (pid and mid) else (name or mid)
    return key, name


# ---------------------------------------------------------------------------
# 归属：调用栈 → 插件 id
# ---------------------------------------------------------------------------

def _registry_map() -> Dict[str, str]:
    now = time.monotonic()
    if now - float(_REGISTRY_CACHE.get("at") or 0.0) < 5.0:
        return _REGISTRY_CACHE.get("map") or {}
    mapping: Dict[str, str] = {}
    try:
        from core.plugin import registry as _registry

        raw = getattr(_registry, "_module_to_plugin", None)
        if isinstance(raw, dict):
            mapping = {str(k): str(v) for k, v in raw.items()}
    except Exception:
        mapping = {}
    _REGISTRY_CACHE["at"] = now
    _REGISTRY_CACHE["map"] = mapping
    return mapping


def _plugin_of_module(name) -> Optional[str]:
    name = str(name or "")
    if not name:
        return None
    parts = name.split(".")
    if parts[0] == "plugins" and len(parts) >= 2 and parts[1]:
        return parts[1]
    mapping = _registry_map()
    if not mapping:
        return None
    if name in mapping:
        return mapping[name]
    for module, plugin in mapping.items():
        if name.startswith(module + "."):
            return plugin
    return None


def _caller_plugin() -> str:
    """从调用栈上溯找「谁在调模型」。

    用户 m12780 第 7 条：探针自己的模块名是
    plugins.<本插件 id>.state.usage_probe，而 _plugin_of_module() 见到
    plugins. 前缀就直接取第二段，于是探针自己的每一帧都被判成本插件、
    提前 return 本插件 id，所有记录都被当成「自己的调用」丢掉（线上实测
    recent 恒为空、账本文件根本没生成）。所以先跳过探针自己的帧，再从
    第一个真实调用方开始判：属于本插件就返回本插件（调用方据此跳过），属于
    别的插件就返回那个插件，属于宿主核心代码就继续上溯，最后仍找不到才算
    unknown。

    用户 m13933 第 3 条：本插件的 state/reply_media.py 会替换
    provider_mgr.get_model_client，给**所有**客户端包一层 chat，于是别的插件
    调模型时调用链中间必然夹着本插件的一帧（线上实测 Alife 的审计调用：
    usage_probe → provider_manager → reply_media → alife_memory_z.main）。
    所以见到本插件的帧不能立刻判成「自己的调用」，要记下来继续上溯；只有
    整条链上再没有别的插件，才认定这次调用是本插件自己的。
    """
    try:
        frame = sys._getframe(1)
    except Exception:
        return "unknown"
    depth = 0
    own_seen = False
    while frame is not None and depth < _MAX_FRAME_DEPTH:
        try:
            name = frame.f_globals.get("__name__") or ""
        except Exception:
            name = ""
        if name and name != __name__:
            plugin = _plugin_of_module(name)
            if plugin:
                if plugin == PLUGIN_ID:
                    own_seen = True
                else:
                    return plugin
        frame = frame.f_back
        depth += 1
    return PLUGIN_ID if own_seen else "unknown"


def _is_own_class(cls) -> bool:
    try:
        module = str(getattr(cls, "__module__", "") or "")
    except Exception:
        return False
    return _plugin_of_module(module) == PLUGIN_ID


# ---------------------------------------------------------------------------
# 账本：后台线程 + 按月轮转
# ---------------------------------------------------------------------------

def _ledger_path() -> Optional[Path]:
    if _DATA_DIR is None:
        return None
    return Path(_DATA_DIR) / LEDGER_NAME


def _ledger_files() -> List[Path]:
    path = _ledger_path()
    if path is None:
        return []
    files: List[Path] = []
    try:
        if path.exists():
            files.append(path)
        folder = path.parent / ARCHIVE_DIR
        if folder.is_dir():
            files.extend(sorted(folder.glob("plugin_usage-*.jsonl")))
    except Exception:
        logger.debug("[multi-decision] 用量探针列账本失败（已忽略）", exc_info=True)
    return files


def _rewrite(path: Path, rows: List[str]) -> None:
    tmp = path.parent / (path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for text in rows:
                fh.write(text + "\n")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _rotate(now=None) -> None:
    """把主账本里不属于当前月的行挪到 archive/plugin_usage-YYYYMM.jsonl。"""
    path = _ledger_path()
    if path is None or not path.exists():
        return
    now = float(now if now is not None else time.time())
    first = time.mktime(time.localtime(now)[:2] + (1, 0, 0, 0, 0, 0, -1))
    keep: List[str] = []
    buckets: Dict[str, List[str]] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except Exception:
                    continue
                ts = _as_float(row.get("ts") if isinstance(row, dict) else None)
                if ts is None:
                    ts = 0.0
                if ts >= first:
                    keep.append(text)
                else:
                    key = time.strftime("%Y%m", time.localtime(ts)) if ts > 0 else "unknown"
                    buckets.setdefault(key, []).append(text)
    except Exception:
        logger.debug("[multi-decision] 用量探针轮转读取失败（已忽略）", exc_info=True)
        return
    folder = path.parent / ARCHIVE_DIR
    for key, rows in buckets.items():
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / ("plugin_usage-" + key + ".jsonl")).open("a", encoding="utf-8") as fh:
                fh.write("\n".join(rows) + "\n")
        except Exception:
            logger.debug("[multi-decision] 用量探针归档失败（已忽略）", exc_info=True)
    try:
        _rewrite(path, keep)
    except Exception:
        logger.debug("[multi-decision] 用量探针重写主账本失败（已忽略）", exc_info=True)


def _write_row(rec: dict) -> None:
    global _CURRENT_MONTH
    path = _ledger_path()
    if path is None:
        return
    now = time.time()
    month = time.strftime("%Y%m", time.localtime(now))
    if _CURRENT_MONTH and month != _CURRENT_MONTH:
        _rotate(now)
    _CURRENT_MONTH = month
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _writer_loop(stop: threading.Event) -> None:
    try:
        _rotate(time.time())
    except Exception:
        logger.debug("[multi-decision] 用量探针启动轮转失败（已忽略）", exc_info=True)
    last_scan = time.monotonic()
    while not stop.is_set():
        try:
            item = _QUEUE.get(timeout=1.0)
        except queue.Empty:
            item = None
        if item is _STOP:
            try:
                _QUEUE.task_done()
            except Exception:
                pass
            break
        if item is not None:
            try:
                _write_row(item)
            except Exception:
                logger.debug("[multi-decision] 用量探针写盘失败（已忽略）", exc_info=True)
            finally:
                try:
                    _QUEUE.task_done()
                except Exception:
                    pass
        now = time.monotonic()
        if now - last_scan >= _SCAN_INTERVAL:
            last_scan = now
            try:
                rescan()
            except Exception:
                logger.debug("[multi-decision] 用量探针补扫失败（已忽略）", exc_info=True)


def _drop_stale_stops() -> int:
    """丢掉上一个写线程没消费的 _STOP 哨兵（它只对那个线程有意义）。

    插件 reload 时 uninstall() 会往队列塞 _STOP；如果当时写线程已经退出，
    这个哨兵就留在队列里，下一次 install() 起的线程会立刻读到它并退出，
    于是探针静默地再也不写盘。这里在起线程前把它清掉。
    """
    dropped = 0
    try:
        with _QUEUE.mutex:
            if _STOP in _QUEUE.queue:
                kept = [it for it in _QUEUE.queue if it is not _STOP]
                dropped = len(_QUEUE.queue) - len(kept)
                _QUEUE.queue.clear()
                _QUEUE.queue.extend(kept)
                _QUEUE.unfinished_tasks = max(0, _QUEUE.unfinished_tasks - dropped)
                if _QUEUE.unfinished_tasks == 0:
                    _QUEUE.all_tasks_done.notify_all()
    except Exception:
        pass
    return dropped


def _start_writer() -> None:
    global _WRITER, _WRITER_STOP
    with _LOCK:
        if _WRITER is not None and _WRITER.is_alive():
            return
        _drop_stale_stops()
        stop = threading.Event()
        _WRITER_STOP = stop
        thread = threading.Thread(target=_writer_loop, args=(stop,),
                                  name="mdp-usage-probe", daemon=True)
        _WRITER = thread
        thread.start()


def _enqueue(rec: dict) -> None:
    if _DATA_DIR is None:
        return
    if _WRITER is None or not _WRITER.is_alive():
        _start_writer()
    _QUEUE.put_nowait(rec)


def flush(timeout: float = 2.0) -> bool:
    """等后台线程把队列写完；返回是否已清空。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if _QUEUE.unfinished_tasks <= 0:
            return True
        time.sleep(0.01)
    return _QUEUE.unfinished_tasks <= 0


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------

def _safe_record(client, kind, started, resp=None, ok=True, nested=False,
                 usage=None, chars=None, tokens=None, model=None) -> None:
    """所有记录逻辑的唯一入口：任何异常都只记 debug，绝不影响原调用。"""
    try:
        if nested or not _ENABLED:
            return
        plugin = _caller_plugin()
        if plugin == PLUGIN_ID:
            return
        if model is None:
            model_key, model_name = _model_of(client)
        else:
            model_key, model_name = str(model or ""), ""
        rec = {
            "ts": time.time(),
            "plugin": plugin or "unknown",
            "kind": kind,
            "model": model_key,
            "model_name": model_name,
            "in": None,
            "out": None,
            "cached": None,
            "chars": chars,
            "ms": max(0, int((time.perf_counter() - started) * 1000)),
            "ok": bool(ok),
        }
        if kind == "llm":
            if isinstance(usage, dict):
                rec["in"] = _int_or_none(usage.get("input_tokens"))
                rec["out"] = _int_or_none(usage.get("output_tokens"))
                rec["cached"] = _int_or_none(usage.get("cached_tokens"))
            elif resp is not None:
                rec["in"] = _int_or_none(getattr(resp, "input_tokens", None))
                rec["out"] = _int_or_none(getattr(resp, "output_tokens", None))
                rec["cached"] = _int_or_none(getattr(resp, "cached_tokens", None))
        else:
            rec["in"], rec["out"], rec["cached"] = 0, 0, 0
            if tokens is not None:
                try:
                    rec["in"] = int(tokens[0])
                    rec["out"] = int(tokens[1])
                    rec["cached"] = int(tokens[2])
                except Exception:
                    pass
        _enqueue(rec)
    except Exception:
        logger.debug("[multi-decision] 用量探针记录失败（已忽略）", exc_info=True)


def _record_quietly(*args, **kwargs) -> None:
    """包装器专用：连 _safe_record 本身出问题也不许影响原调用。"""
    try:
        _safe_record(*args, **kwargs)
    except Exception:
        logger.debug("[multi-decision] 用量探针记录失败（已忽略）", exc_info=True)


# ---------------------------------------------------------------------------
# 包装器
# ---------------------------------------------------------------------------

def _make_chat_wrapper(orig):
    async def wrapper(self, *args, **kwargs):
        nested = _DEPTH.get() > 0
        token = _DEPTH.set(_DEPTH.get() + 1)
        started = time.perf_counter()
        try:
            resp = await orig(self, *args, **kwargs)
        except BaseException:
            _record_quietly(self, "llm", started, ok=False, nested=nested)
            raise
        finally:
            _DEPTH.reset(token)
        _record_quietly(self, "llm", started, resp=resp, ok=True, nested=nested)
        return resp

    return wrapper


def _make_stream_wrapper(orig):
    async def wrapper(self, *args, **kwargs):
        nested = _DEPTH.get() > 0
        token = _DEPTH.set(_DEPTH.get() + 1)
        started = time.perf_counter()
        usage = None
        ok = True
        try:
            async for chunk in orig(self, *args, **kwargs):
                try:
                    one = getattr(chunk, "usage", None)
                    if isinstance(one, dict) and one:
                        usage = one
                except Exception:
                    pass
                yield chunk
        except GeneratorExit:
            raise
        except BaseException:
            ok = False
            raise
        finally:
            _DEPTH.reset(token)
            _record_quietly(self, "llm", started, ok=ok, nested=nested, usage=usage)

    return wrapper


def _make_embed_wrapper(orig):
    async def wrapper(self, *args, **kwargs):
        started = time.perf_counter()
        chars = _embed_chars(args, kwargs)
        slot = {"tokens": None, "chars": chars}
        token = _EMBED_SLOT.set(slot)
        try:
            result = await orig(self, *args, **kwargs)
        except BaseException:
            _record_quietly(self, "embed", started, ok=False, chars=chars)
            raise
        finally:
            _EMBED_SLOT.reset(token)
        _record_quietly(self, "embed", started, ok=True, chars=chars,
                     tokens=slot.get("tokens"))
        return result

    return wrapper


def _make_rerank_wrapper(orig):
    async def wrapper(self, *args, **kwargs):
        started = time.perf_counter()
        chars = _rerank_chars(args, kwargs)
        slot = {"tokens": None, "chars": chars}
        token = _RERANK_SLOT.set(slot)
        try:
            result = await orig(self, *args, **kwargs)
        except BaseException:
            _record_quietly(self, "rerank", started, ok=False, chars=chars)
            raise
        finally:
            _RERANK_SLOT.reset(token)
        _record_quietly(self, "rerank", started, ok=True, chars=chars,
                     tokens=slot.get("tokens"))
        return result

    return wrapper


# ---------------------------------------------------------------------------
# 类级补丁：openai 嵌入 / httpx 排序
# ---------------------------------------------------------------------------

def _record_embed_sdk(resp, args, kwargs) -> None:
    usage = getattr(resp, "usage", None)
    inp = _as_int(getattr(usage, "prompt_tokens", 0))
    total = _as_int(getattr(usage, "total_tokens", 0))
    inp = inp or total
    model = kwargs.get("model")
    if model is None and args:
        model = args[0]
    texts = kwargs.get("input")
    if texts is None and len(args) > 1:
        texts = args[1]
    chars = _texts_chars(texts)
    slot = _EMBED_SLOT.get()
    if slot is not None:
        slot["tokens"] = (inp, 0, 0)
        if chars:
            slot["chars"] = chars
        return
    _safe_record(None, "embed", time.perf_counter(), ok=True, chars=chars,
                 tokens=(inp, 0, 0), model=str(model or ""))


def _install_openai_patch() -> bool:
    try:
        import openai  # noqa: F401
        from openai.resources.embeddings import AsyncEmbeddings
    except Exception:
        logger.debug("[multi-decision] 用量探针跳过 openai 嵌入补丁（导入失败）")
        return False
    orig = getattr(AsyncEmbeddings, "create", None)
    if orig is None or getattr(orig, "_mdp_probe", False):
        return False

    async def wrapper(self, *args, **kwargs):
        resp = await orig(self, *args, **kwargs)
        try:
            _record_embed_sdk(resp, args, kwargs)
        except Exception:
            logger.debug("[multi-decision] 用量探针记录嵌入失败（已忽略）", exc_info=True)
        return resp

    wrapper._mdp_probe = True
    wrapper._mdp_orig = orig
    try:
        AsyncEmbeddings.create = wrapper
    except Exception:
        return False
    _ORIGINALS.append((AsyncEmbeddings, "create", orig))
    _WRAPPED["openai.resources.embeddings.AsyncEmbeddings.create"] = "embed"
    return True


def _record_chat_sdk(resp, args, kwargs, started=None) -> None:
    """传输层兜底：从 ChatCompletion 的 usage 里取 token（用户本轮第 ③ 条）。

    客户端类那层（_wrap_llm_classes）只包宿主自己的 LLMModelClient 子类，
    插件直接拿 openai 客户端发请求就漏了；这里在 SDK 层再记一条。
    """
    usage = getattr(resp, "usage", None)
    inp = 0
    outp = 0
    cached = 0
    try:
        inp = _as_int(getattr(usage, "prompt_tokens", 0))
    except Exception:
        inp = 0
    try:
        outp = _as_int(getattr(usage, "completion_tokens", 0))
    except Exception:
        outp = 0
    try:
        details = getattr(usage, "prompt_tokens_details", None)
        cached = _as_int(getattr(details, "cached_tokens", 0))
    except Exception:
        cached = 0
    model = kwargs.get("model")
    if model is None and args:
        model = args[0]
    _safe_record(None, "llm", started if started is not None else time.perf_counter(), ok=True,
                 usage={"input_tokens": inp, "output_tokens": outp,
                        "cached_tokens": cached},
                 model=str(model or ""))


def _install_chat_patch() -> bool:
    """传输层兜底：openai 的 Chat Completions（用户本轮第 ③ 条）。

    客户端类那层已经记过的（_DEPTH > 0）直接透传，避免双算；流式响应是异步
    迭代器，包了会破坏调用方，所以 stream=True 也直接透传。
    """
    try:
        import openai  # noqa: F401
        from openai.resources.chat.completions import AsyncCompletions
    except Exception:
        logger.debug("[multi-decision] 用量探针跳过 openai 对话补丁（导入失败）")
        return False
    orig = getattr(AsyncCompletions, "create", None)
    if orig is None or getattr(orig, "_mdp_probe", False):
        return False

    async def wrapper(self, *args, **kwargs):
        if _DEPTH.get() > 0 or kwargs.get("stream"):
            return await orig(self, *args, **kwargs)
        started = time.perf_counter()
        try:
            resp = await orig(self, *args, **kwargs)
        except BaseException:
            try:
                model = kwargs.get("model")
                if model is None and args:
                    model = args[0]
                _record_quietly(None, "llm", started, ok=False, model=str(model or ""))
            except Exception:
                logger.debug("[multi-decision] 用量探针记录对话失败（已忽略）", exc_info=True)
            raise
        try:
            _record_chat_sdk(resp, args, kwargs, started)
        except Exception:
            logger.debug("[multi-decision] 用量探针记录对话失败（已忽略）", exc_info=True)
        return resp

    wrapper._mdp_probe = True
    wrapper._mdp_orig = orig
    try:
        AsyncCompletions.create = wrapper
    except Exception:
        return False
    _ORIGINALS.append((AsyncCompletions, "create", orig))
    _WRAPPED["openai.resources.chat.completions.AsyncCompletions.create"] = "llm"
    return True


def _rerank_tokens(body) -> Optional[Tuple[int, int, int]]:
    try:
        meta = body.get("meta")
        if isinstance(meta, dict):
            tokens = meta.get("tokens")
            if isinstance(tokens, dict):
                return (_as_int(tokens.get("input_tokens")),
                        _as_int(tokens.get("output_tokens")), 0)
        usage = body.get("usage")
        if isinstance(usage, dict):
            for key in ("total_tokens", "input_tokens", "prompt_tokens"):
                value = _as_int(usage.get(key))
                if value:
                    return (value, 0, 0)
    except Exception:
        return None
    return None


def _rerank_request_chars(request) -> int:
    try:
        raw = getattr(request, "content", None)
        if not raw:
            return 0
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        body = json.loads(raw)
        if not isinstance(body, dict):
            return 0
        chars = len(str(body.get("query") or ""))
        docs = body.get("documents") or []
        if isinstance(docs, (list, tuple)):
            chars += sum(len(str(item)) for item in docs)
        return chars
    except Exception:
        return 0


def _record_rerank_http(request, resp) -> None:
    url = str(getattr(request, "url", "") or "")
    if "rerank" not in url.lower():
        return
    body = None
    try:
        body = resp.json()
    except Exception:
        body = None
    tokens = _rerank_tokens(body) if isinstance(body, dict) else None
    chars = _rerank_request_chars(request)
    model = ""
    if isinstance(body, dict):
        model = str(body.get("model") or "")
    slot = _RERANK_SLOT.get()
    if slot is not None:
        slot["tokens"] = tokens or (0, 0, 0)
        if chars:
            slot["chars"] = chars
        return
    _safe_record(None, "rerank", time.perf_counter(), ok=True, chars=chars,
                 tokens=tokens or (0, 0, 0), model=model)


def _install_httpx_patch() -> bool:
    try:
        import httpx
    except Exception:
        logger.debug("[multi-decision] 用量探针跳过 httpx 排序补丁（导入失败）")
        return False
    orig = getattr(httpx.AsyncClient, "send", None)
    if orig is None or getattr(orig, "_mdp_probe", False):
        return False

    async def wrapper(self, request, *args, **kwargs):
        resp = await orig(self, request, *args, **kwargs)
        try:
            _record_rerank_http(request, resp)
        except Exception:
            logger.debug("[multi-decision] 用量探针记录排序失败（已忽略）", exc_info=True)
        return resp

    wrapper._mdp_probe = True
    wrapper._mdp_orig = orig
    try:
        httpx.AsyncClient.send = wrapper
    except Exception:
        return False
    _ORIGINALS.append((httpx.AsyncClient, "send", orig))
    _WRAPPED["httpx.AsyncClient.send"] = "rerank"
    return True


# ---------------------------------------------------------------------------
# 扫描与包装
# ---------------------------------------------------------------------------

def _iter_subclasses(base):
    seen = set()
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            subs = list(current.__subclasses__())
        except Exception:
            subs = []
        for sub in subs:
            if sub in seen:
                continue
            seen.add(sub)
            yield sub
            stack.append(sub)


def _target_key(cls, attr) -> str:
    module = str(getattr(cls, "__module__", "") or "")
    qual = str(getattr(cls, "__qualname__", "") or getattr(cls, "__name__", "") or "")
    return (module + "." + qual + "." + attr).strip(".")


def _wrap_method(cls, attr, kind, factory) -> bool:
    try:
        orig = cls.__dict__.get(attr)
    except Exception:
        return False
    if orig is None or not callable(orig) or getattr(orig, "_mdp_probe", False):
        return False
    try:
        wrapper = factory(orig)
        wrapper._mdp_probe = True
        wrapper._mdp_orig = orig
        wrapper.__name__ = getattr(orig, "__name__", attr)
        wrapper.__qualname__ = getattr(orig, "__qualname__", attr)
        wrapper.__doc__ = getattr(orig, "__doc__", None)
        setattr(cls, attr, wrapper)
    except Exception:
        logger.debug("[multi-decision] 用量探针包装 %s.%s 失败（已忽略）"
                     % (getattr(cls, "__name__", "?"), attr), exc_info=True)
        return False
    _ORIGINALS.append((cls, attr, orig))
    _WRAPPED[_target_key(cls, attr)] = kind
    return True


def _import_base(name):
    for module_name in ("core.provider.provider", "core.provider"):
        try:
            module = __import__(module_name, fromlist=[name])
            base = getattr(module, name, None)
            if base is not None:
                return base
        except Exception:
            continue
    return None


def _wrap_llm_classes() -> int:
    base = _import_base("LLMModelClient")
    if base is None:
        return 0
    count = 0
    for cls in _iter_subclasses(base):
        if _is_own_class(cls):
            continue
        if _wrap_method(cls, "chat", "llm", _make_chat_wrapper):
            count += 1
        if _wrap_method(cls, "chat_stream", "llm", _make_stream_wrapper):
            count += 1
    return count


def _wrap_embed_classes() -> int:
    base = _import_base("EmbeddingModelClient")
    if base is None:
        return 0
    count = 0
    for cls in _iter_subclasses(base):
        if _is_own_class(cls):
            continue
        if _wrap_method(cls, "embed", "embed", _make_embed_wrapper):
            count += 1
    return count


def _wrap_rerank_classes() -> int:
    base = _import_base("RerankModelClient")
    if base is None:
        return 0
    count = 0
    for cls in _iter_subclasses(base):
        if _is_own_class(cls):
            continue
        if _wrap_method(cls, "rerank", "rerank", _make_rerank_wrapper):
            count += 1
    return count


def _wrap_all() -> int:
    count = 0
    count += _wrap_llm_classes()
    count += _wrap_embed_classes()
    count += _wrap_rerank_classes()
    if _install_openai_patch():
        count += 1
    if _install_chat_patch():
        count += 1
    if _install_httpx_patch():
        count += 1
    return count


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def _resolve_data_dir(plugin) -> Optional[Path]:
    for attr in ("_data_dir", "data_dir"):
        try:
            value = getattr(plugin, attr, None)
        except Exception:
            value = None
        if value:
            return Path(value)
    ctx = getattr(plugin, "ctx", None) if plugin is not None else None
    if ctx is not None:
        fn = getattr(ctx, "get_plugin_data_dir", None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                value = None
            if value:
                return Path(value)
    return None


def install(plugin=None, data_dir=None, enabled=True) -> Dict[str, Any]:
    """装探针。幂等：重复调用只补扫新子类，不会套娃。"""
    global _INSTALLED, _DATA_DIR, _ENABLED
    with _LOCK:
        _ENABLED = bool(enabled)
        if data_dir is not None:
            _DATA_DIR = Path(data_dir)
        elif _DATA_DIR is None:
            _DATA_DIR = _resolve_data_dir(plugin)
        if _DATA_DIR is None:
            logger.warning("[multi-decision] 用量探针拿不到数据目录，只包不记")
        _start_writer()
        if _INSTALLED:
            _wrap_all()
            return status()
        _wrap_all()
        _INSTALLED = True
        return status()


def uninstall() -> None:
    """还原所有原方法，并把队列里剩下的记录写完。"""
    global _INSTALLED, _WRITER, _WRITER_STOP
    with _LOCK:
        for cls, attr, orig in reversed(_ORIGINALS):
            try:
                current = getattr(cls, attr, None)
                if current is not None and getattr(current, "_mdp_probe", False):
                    setattr(cls, attr, orig)
            except Exception:
                logger.debug("[multi-decision] 用量探针还原 %s.%s 失败（已忽略）"
                             % (getattr(cls, "__name__", "?"), attr), exc_info=True)
        _ORIGINALS.clear()
        _WRAPPED.clear()
        _INSTALLED = False
        stop = _WRITER_STOP
        writer = _WRITER
        _WRITER = None
        _WRITER_STOP = None
    try:
        flush(2.0)
    except Exception:
        pass
    if stop is not None:
        stop.set()
    alive = writer is not None and writer.is_alive()
    if alive:
        try:
            _QUEUE.put_nowait(_STOP)
        except Exception:
            pass
        try:
            writer.join(timeout=2.0)
        except Exception:
            pass


def rescan() -> Dict[str, Any]:
    """重新扫子类，给后出现的类补包装（宿主允许插件注册自己的 provider）。"""
    with _LOCK:
        if not _INSTALLED:
            return status()
        _wrap_all()
        return status()


def set_enabled(value) -> bool:
    global _ENABLED
    _ENABLED = bool(value)
    return _ENABLED


def is_installed() -> bool:
    return bool(_INSTALLED)


def iter_records(start=None, end=None) -> Iterator[dict]:
    """按 ts 区间 [start, end) 逐行读账本（含归档）。"""
    lo = _as_float(start)
    hi = _as_float(end)
    for path in _ledger_files():
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    ts = _as_float(row.get("ts"))
                    if ts is None:
                        continue
                    if lo is not None and ts < lo:
                        continue
                    if hi is not None and ts >= hi:
                        continue
                    yield row
        except Exception:
            logger.debug("[multi-decision] 用量探针读账本失败（已忽略）", exc_info=True)


def totals(start=None, end=None) -> Dict[str, Dict[str, Dict[str, dict]]]:
    """按本地日 / 插件 / 类别汇总，并带按模型明细。

    返回 {day: {plugin: {kind: {"calls","in","out","cached","chars",
                                "models": {模型名: {"calls","in","out","cached"}}}}}}。
    模型键优先用账本里的 model_name（价目表按模型名填单价，两边要同一个键），
    拿不到模型名才退回 provider:model_id 引用。
    日期键用本地时区（宿主是 Asia/Shanghai），跟 /pricing/trend 的 day 桶对齐。
    上层 kind 桶的 in/out/cached/calls 等于它下面 models 之和。
    """
    out: Dict[str, Dict[str, Dict[str, dict]]] = {}
    for row in iter_records(start, end):
        ts = _as_float(row.get("ts"))
        if ts is None:
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        plugin = str(row.get("plugin") or "unknown")
        kind = str(row.get("kind") or "llm")
        if kind not in KINDS:
            kind = "llm"
        # 用户本轮第 ① 条：价目表按模型名填单价，账本也按模型名聚合，
        # 两边才是同一个键（拿不到模型名才退回 provider:model_id 引用）。
        model = str(row.get("model_name") or "").strip() or str(row.get("model") or "")
        inp = max(0, _as_int(row.get("in")))
        outp = max(0, _as_int(row.get("out")))
        cached = min(inp, max(0, _as_int(row.get("cached"))))
        chars = max(0, _as_int(row.get("chars")))
        bucket = out.setdefault(day, {}).setdefault(plugin, {}).setdefault(
            kind, {"calls": 0, "in": 0, "out": 0, "cached": 0, "chars": 0, "models": {}})
        bucket["calls"] += 1
        bucket["in"] += inp
        bucket["out"] += outp
        bucket["cached"] += cached
        bucket["chars"] += chars
        mb = bucket["models"].setdefault(
            model, {"calls": 0, "in": 0, "out": 0, "cached": 0})
        mb["calls"] += 1
        mb["in"] += inp
        mb["out"] += outp
        mb["cached"] += cached
    return out


def _recent(limit: int = 20) -> List[dict]:
    path = _ledger_path()
    if path is None or not path.exists():
        return []
    tail: "deque[str]" = deque(maxlen=max(0, int(limit)))
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if text:
                    tail.append(text)
    except Exception:
        return []
    rows: List[dict] = []
    for text in tail:
        try:
            row = json.loads(text)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def status() -> Dict[str, Any]:
    with _LOCK:
        wrapped = [{"target": key, "kind": kind} for key, kind in sorted(_WRAPPED.items())]
        path = _ledger_path()
        return {
            "installed": bool(_INSTALLED),
            "enabled": bool(_ENABLED),
            "data_dir": str(_DATA_DIR) if _DATA_DIR is not None else "",
            "ledger": str(path) if path is not None else "",
            "wrapped_count": len(wrapped),
            "wrapped": wrapped,
            "recent": _recent(20),
        }


__all__ = [
    "PLUGIN_ID",
    "install",
    "uninstall",
    "rescan",
    "set_enabled",
    "is_installed",
    "iter_records",
    "totals",
    "status",
    "flush",
]
