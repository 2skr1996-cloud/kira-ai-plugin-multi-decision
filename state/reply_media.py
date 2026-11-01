"""Request-scoped reply media projection, after native assembly, before provider read.

Prompts/persistent memory are never changed. Install before clients are selected;
stage also covers pre-existing event model groups. No model is instantiated here.
"""
from asyncio import CancelledError
from contextlib import aclosing
from copy import copy, deepcopy
from functools import wraps
import re
import weakref


_MARK = "_mdp_prepared_reply_media"
_OWNER = "_mdp_reply_media_owner"
_PREVIOUS = "_mdp_reply_media_previous"
_OWN = "_mdp_reply_media_previous_own"
_NODE = "_mdp_reply_media_node"
_FAILURE = "_mdp_reply_media_failure"
_MID = re.compile(r"\[message_id:\s*([^\]\r\n]{1,200})\]")
_IMAGES = {"image_url", "kira_image_ref"}
_NOTE_NAMES = {"对当前聊天消息的决策建议", "对以上聊天记录的决策建议"}


class ReplyMediaPreparationError(ValueError):
    """A marked media request cannot safely be mapped to native assembled input."""


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _signature(message):
    # Provider-visible fields only; dict omissions and OpenAIMessage defaults agree.
    return (_get(message, "role"), _get(message, "name"), _get(message, "content"),
            _get(message, "reasoning_content", "") or "",
            _get(message, "tool_calls", []) or [], _get(message, "tool_call_id") or None)


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _mid(content):
    found = _MID.search(_text(content))
    return found.group(1).strip() if found else ""


def _parts(content):
    return content if isinstance(content, list) else []


def _has_images(content):
    return any(isinstance(p, dict) and p.get("type") in _IMAGES for p in _parts(content))


def _add_text(content, text, *, front=False):
    if not text:
        return content
    if isinstance(content, str):
        return text + content if front else content + text
    part = {"type": "text", "text": text}
    return [part, *content] if front else [*content, part]


def _owner(method):
    # functools.wraps on a third-party wrapper may copy our metadata.
    return getattr(method, _OWNER, None) if getattr(method, _NODE, None) is method else None


def _contains(method, owner):
    while (current := _owner(method)) is not None:
        if current is owner:
            return True
        method = getattr(method, _PREVIOUS)
    return False


def _live_previous(method, owned):
    # Remove only our closed wrappers; never unwrap or overwrite another plugin.
    while (owner := _owner(method)) is not None and owner._closed:
        owned = getattr(method, _OWN)
        method = getattr(method, _PREVIOUS)
    return method, owned


def _remember(wrapper, owner, previous, owned):
    setattr(wrapper, _OWNER, owner)
    setattr(wrapper, _NODE, wrapper)
    setattr(wrapper, _PREVIOUS, previous)
    setattr(wrapper, _OWN, owned)
    return wrapper


def _restore(obj, name, wrapper):
    if getattr(obj, name, None) is not wrapper:
        return
    previous, owned = _live_previous(getattr(wrapper, _PREVIOUS), getattr(wrapper, _OWN))
    if owned:
        setattr(obj, name, previous)
    else:
        delattr(obj, name)


class ReplyMediaAdapter:
    def __init__(self, ctx=None, logger=None, on_failure=None):
        self.ctx, self.logger = ctx, logger
        self._on_failure = on_failure
        self._token = object()
        self._manager = None
        self._factory = None
        self._clients = []  # Weak refs only; bound originals live on each client.
        self._closed = False

    def install(self) -> bool:
        if self._closed:
            return False
        manager = getattr(self.ctx, "provider_mgr", None)
        previous = getattr(manager, "get_model_client", None)
        if not callable(previous):
            return False
        if previous is self._factory or _contains(previous, self):
            return True
        owned = "get_model_client" in vars(manager)

        @wraps(previous)
        def factory(*args, **kwargs):
            client = previous(*args, **kwargs)
            if not self._closed:
                self._wrap_client(client)
            return client

        self._factory = _remember(factory, self, previous, owned)
        self._manager = manager
        manager.get_model_client = factory
        return True

    def _wrap_client(self, client):
        # Actual 3.0 clients advertise ModelType.LLM; no guesses from model names.
        kind = getattr(client, "type", None)
        if getattr(kind, "value", kind) != "llm":
            return False
        changed = False
        for name in ("chat", "chat_stream"):
            previous = getattr(client, name, None)
            if not callable(previous) or _contains(previous, self):
                continue
            owned = name in vars(client)
            wrapper = self._call_wrapper(previous, stream=name == "chat_stream")
            setattr(client, name, _remember(wrapper, self, previous, owned))
            changed = True
        if changed:
            self._clients = [ref for ref in self._clients if ref() is not None]
            if not any(ref() is client for ref in self._clients):
                self._clients.append(weakref.ref(client))
        return callable(getattr(client, "chat", None))

    def _call_wrapper(self, previous, *, stream):
        async def arguments(args, kwargs):
            if self._closed:
                return args, kwargs
            request = args[0] if args else kwargs.get("request")
            try:
                wire = self._wire_request(request)
            except ReplyMediaPreparationError as exc:
                await self._notify_failure(request, exc)
                raise
            if wire is request:
                return args, kwargs
            if args:
                return (wire, *args[1:]), kwargs
            return args, {**kwargs, "request": wire}

        if stream:
            @wraps(previous)
            async def wrapped(*args, **kwargs):
                args, kwargs = await arguments(args, kwargs)
                async with aclosing(previous(*args, **kwargs)) as chunks:
                    async for chunk in chunks:
                        yield chunk
        else:
            @wraps(previous)
            async def wrapped(*args, **kwargs):
                args, kwargs = await arguments(args, kwargs)
                return await previous(*args, **kwargs)
        return wrapped

    async def _notify_failure(self, request, exc):
        # Only our marked preparation errors, before invoking any provider/guard.
        mark = getattr(request, _MARK, None)
        callback = self._on_failure
        if (not isinstance(mark, dict) or mark.get("token") is not self._token
                or not callable(callback)):
            return
        state = getattr(request, _FAILURE, None)
        if isinstance(state, dict) and state.get("token") is self._token:
            if state.get("done") or state.get("running"):
                return
        else:
            state = {"token": self._token, "done": False, "running": False}
            setattr(request, _FAILURE, state)
        state["running"] = True
        try:
            await callback(request, exc)
            state["done"] = True
        except (Exception, CancelledError):
            # Failure reporting must never hide/replace the original preparation error.
            try:
                if self.logger is not None:
                    self.logger.exception("[multi-decision] reply preparation failure callback failed")
            except Exception:
                pass
        finally:
            state["running"] = False

    def stage(self, request, prepared_incoming, *, event=None, retained=None) -> int:
        if self._closed:
            raise ReplyMediaPreparationError("reply media adapter is closed")
        rows = list(prepared_incoming or [])
        prompts = [p for p in (getattr(request, "user_prompt", None) or [])
                   if getattr(p, "name", "") == "message"]
        if len(rows) != len(prompts):
            raise ReplyMediaPreparationError("prepared incoming count differs from message prompts")
        kept = None
        if retained is not None:
            if not isinstance(retained, dict):
                raise ReplyMediaPreparationError("retained reply requires an explicit round descriptor")
            ids, note_index = retained.get("ids"), retained.get("note_index")
            history = list(getattr(request, "messages", None) or [])
            if (not isinstance(ids, list) or not ids
                    or any(not isinstance(mid, str) or not mid or mid != mid.strip() for mid in ids)
                    or len(set(ids)) != len(ids) or type(note_index) is not int
                    or not 0 <= note_index < len(history)):
                raise ReplyMediaPreparationError("invalid retained reply ids or note_index")
            note = history[note_index]
            if _get(note, "role") != "assistant" or _get(note, "name") not in _NOTE_NAMES:
                raise ReplyMediaPreparationError("retained reply requires an explicitly named assistant note")
            snapshots = []
            for mid in ids:
                sources = [p for p in prompts if _mid(getattr(p, "content", None)) == mid]
                positions = [i for i, m in enumerate(history)
                             if _get(m, "role") == "user" and _mid(_get(m, "content")) == mid]
                if len(sources) != 1 or len(positions) != 1 or positions[0] >= note_index:
                    raise ReplyMediaPreparationError("retained reply id is not unique before its note")
                position = positions[0]
                message = history[position]
                snapshots.append(deepcopy(_signature(message)))
            snapshots.append(deepcopy(_signature(note)))
            leading = bool(history and _get(history[0], "role") == "system")
            kept = {"ids": tuple(ids), "protected": snapshots,
                    "history": deepcopy(tuple(_signature(m) for m in history[int(leading):])),
                    "leading_system": deepcopy(_signature(history[0])) if leading else None}
        # Ordinary plain str requests stay exactly on the native path, even with old notes.
        if kept is None and not any(isinstance(_get(row, "content"), list) for row in rows):
            return 0
        raw = []
        for prompt, row in zip(prompts, rows):
            original, ready = getattr(prompt, "content", None), _get(row, "content")
            if not isinstance(original, str) or not isinstance(ready, (str, list)):
                raise ReplyMediaPreparationError("reply media requires original string prompts")
            source_id, prepared_id = _mid(original), _mid(ready)
            if _has_images(ready) and (not source_id or source_id != prepared_id):
                raise ReplyMediaPreparationError("prepared image has missing or mismatched message_id")
            if prepared_id and source_id != prepared_id:
                raise ReplyMediaPreparationError("prepared message_id differs from original prompt")
            raw.append(original)
        installed = self.install()
        explicit = False
        for client in getattr(event, "model_group", None) or []:
            explicit = self._wrap_client(client) or explicit
        if not installed and not explicit:
            raise ReplyMediaPreparationError("no provider client factory or event model group for reply media")
        setattr(request, _MARK, {"token": self._token, "raw": tuple(raw),
                                "ready": deepcopy([_get(row, "content") for row in rows]),
                                "retained": kept})
        return len(rows)

    def _wire_request(self, request):
        mark = getattr(request, _MARK, None)
        if not isinstance(mark, dict) or mark.get("token") is not self._token:
            return request
        messages = list(getattr(request, "messages", None) or [])
        kept = mark.get("retained")
        history_end = 0
        if kept is not None:
            # assemble_prompt may insert/replace/remove exactly one leading system.
            # Everything else is the complete, ordered staged base history, not a
            # substring search for whichever retained user/note happens to match.
            leading = bool(messages and _get(messages[0], "role") == "system")
            if not getattr(request, "system_prompt", None):
                current_system = _signature(messages[0]) if leading else None
                if current_system != kept["leading_system"]:
                    raise ReplyMediaPreparationError("retained reply history changed after staging")
            history_start = int(leading)
            history_end = history_start + len(kept["history"])
            if tuple(_signature(m) for m in messages[history_start:history_end]) != kept["history"]:
                raise ReplyMediaPreparationError("retained reply history changed after staging")
            # Tool continuation may append new messages, never a duplicate round.
            if any(_signature(m) in kept["protected"] for m in messages[history_end + 1:]):
                raise ReplyMediaPreparationError("retained reply history changed after staging")
        prompts = list(getattr(request, "user_prompt", None) or [])
        message_prompts = [p for p in prompts if getattr(p, "name", "") == "message"]
        if tuple(getattr(p, "content", None) for p in message_prompts) != mark["raw"]:
            raise ReplyMediaPreparationError("message prompts changed after media staging")
        rendered, projected, prefix, index = [], [], "", 0
        for prompt in prompts:
            to_string = getattr(prompt, "to_string", None)
            if not callable(to_string):
                raise ReplyMediaPreparationError("unsupported prompt in staged reply")
            text = to_string()
            if not isinstance(text, str):
                raise ReplyMediaPreparationError("Prompt.to_string must remain a string")
            rendered.append(text)
            if getattr(prompt, "name", "") != "message":
                if projected:
                    projected[-1] = _add_text(projected[-1], text)
                else:
                    prefix += text
                continue
            original = mark["raw"][index]
            if not text.startswith(original):
                raise ReplyMediaPreparationError("rendered message no longer matches staged prompt")
            if kept is not None and _mid(original) in kept["ids"]:
                # Suppress only this round's repeated body, never Prompt.end or other prompts.
                tail = text[len(original):]
                if projected:
                    projected[-1] = _add_text(projected[-1], tail)
                else:
                    prefix += tail
                index += 1
                continue
            content = deepcopy(mark["ready"][index])
            content = _add_text(content, prefix, front=True)
            prefix = ""
            projected.append(_add_text(content, text[len(original):]))
            index += 1
        if prefix:
            projected.append(prefix)
        expected = "".join(rendered)
        candidates = []
        for i, message in enumerate(messages):
            # The host appends packeduser only after stage. Never guess a historical user.
            if kept is not None and i < history_end:
                continue
            content = _get(message, "content")
            if _get(message, "role") != "user":
                continue
            if content == expected:
                candidates.append(i)
            elif (isinstance(content, list) and content and isinstance(content[0], dict)
                  and content[0].get("type") == "text" and content[0].get("text") == expected):
                if any(not isinstance(p, dict) or p.get("type") not in _IMAGES for p in content[1:]):
                    raise ReplyMediaPreparationError("unexpected native parts in staged reply")
                candidates.append(i)
        if (len(candidates) != 1 or (not projected and kept is None)
                or kept is not None and candidates != [history_end]):
            raise ReplyMediaPreparationError("cannot uniquely locate assembled incoming reply media")
        position = candidates[0]
        replacement = []
        for content in projected:
            item = copy(messages[position])
            if isinstance(item, dict):
                item["content"] = content
            else:
                item.content = content
            replacement.append(item)
        wire = copy(request)
        wire.messages = messages[:position] + replacement + messages[position + 1:]
        # Prevent reprocessing if base chat_stream delegates to wrapped chat.
        setattr(wire, _MARK, None)
        return wire

    def unstage(self, request):
        """Remove only this adapter's own request marker after a failed preparation."""
        mark = getattr(request, _MARK, None)
        if isinstance(mark, dict) and mark.get("token") is self._token:
            delattr(request, _MARK)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._on_failure = None
        if self._manager is not None and self._factory is not None:
            _restore(self._manager, "get_model_client", self._factory)
        for ref in self._clients:
            client = ref()
            if client is None:
                continue
            for name in ("chat", "chat_stream"):
                wrapper = getattr(client, name, None)
                if _owner(wrapper) is self:
                    _restore(client, name, wrapper)
        self._clients.clear()
        self._factory = None
        self._manager = None
