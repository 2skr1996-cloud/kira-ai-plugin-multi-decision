"""把框架打包的 user 消息拆成单条。

框架会把「同一批进来的 N 条群消息」拼成一条 role=user，正文里一行一条：

    [23:52] [message_id: -1067678968] [user_nickname: OGAS Dandelion, user_id: 1021316269] | 所以说不要上来就骂人
    [23:52] [message_id: 2117163376] [user_nickname: Mcp_management service, user_id: 443715037] | 但这他妈的

窗口里如果保持这个打包形态，一条要算成十几条：条数对不上真实消息数，
页面上一条 user 展开一大坨，裁切边界也只能按整包砍。所以进窗口前先拆开，
一条群消息一条 message。代价只是每条多一个 role 包裹：正文里本来就有的
行首前缀不重复、不重写。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, List

from .buffer import _get, clone_message

#: 一条消息的行首。正文里自带换行时，后续行不匹配这个模式，自然归到上一条。
_HEAD_RE = re.compile(r"^\[(\d{1,2}:\d{2})\]\s*\[message_id:\s*([^\]]{1,200})\]")


def _text_of(content: Any):
    """取出文本部分，其余 part（图片等）原样返回。"""
    if isinstance(content, list):
        texts = []
        others = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
            else:
                others.append(item)
        return "\n".join(texts), others
    if isinstance(content, str):
        return content, []
    return "", list(content or [])


def split_text(text: str) -> List[str]:
    """按「行首是 [HH:MM] [message_id: ...]」把一段文本切成多条。"""
    if not text:
        return []
    chunks: List[str] = []
    cur: List[str] = []
    for line in text.split("\n"):
        if _HEAD_RE.match(line) and cur:
            chunks.append("\n".join(cur))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def split_user_content(content: Any) -> List[Any]:
    """拆一条 user 的 content，返回拆好的 content 列表；不用拆时返回空列表。"""
    text, others = _text_of(content)
    chunks = split_text(text)
    if len(chunks) <= 1:
        return []
    out: List[Any] = list(chunks)
    if others:
        # 图片这类附件没有文本占位，不知道属于哪一条，统一挂在最后一条上。
        out[-1] = [{"type": "text", "text": out[-1]}] + others
    return out


#: assistant 的一条回复里可能连着好几段 <msg ...>……</msg>（群里连发被框架合成一条）。
_MSG_SEG_RE = re.compile(r"<msg\b[^>]*>[\s\S]*?</msg>", re.I)


def split_assistant_content(content: Any) -> List[Any]:
    """拆一条 assistant 的 content，返回拆好的 content 列表；不用拆时返回空列表。

    和 user 一个道理：一条里塞了多段 <msg> 的，窗口里就该算成多条消息。
    不拆的话条数少算、页面上摊成一大坨、裁切也只能整块砍（用户 m26991 图3）。
    """
    if not isinstance(content, str):
        return []
    segs = _MSG_SEG_RE.findall(content)
    if len(segs) < 2:
        return []
    pre = content[: content.find(segs[0])].strip()
    out: List[Any] = []
    for i, seg in enumerate(segs):
        # 段前面还有正文的（比如「这就去」），跟着第一段走，别丢
        out.append((pre + "\n" + seg) if (i == 0 and pre) else seg)
    return out


def expand_packed_messages(messages) -> List[Any]:
    """把消息列表里打包的 user、多段 assistant 拆成多条，其余消息原样返回。"""
    out: List[Any] = []
    for msg in messages or []:
        role = _get(msg, "role") or ""
        if role == "user":
            parts = split_user_content(_get(msg, "content"))
        elif role == "assistant" and not _get(msg, "tool_calls"):
            # 带工具调用的那条不拆：tool_calls 会跟着每一段复制，配对就乱了
            parts = split_assistant_content(_get(msg, "content"))
        else:
            parts = []
        if not parts:
            out.append(msg)
            continue
        for one in parts:
            item = clone_message(msg)
            if isinstance(item, dict):
                item["content"] = one
            else:
                try:
                    item.content = one
                except Exception:
                    item = {"role": "user", "content": one}
            out.append(item)
    return out

#: 唤醒标记。放在 meta 段（[message_id: …] [user_nickname: …]）后面、正文前面。
MARK = " [@你]"

#: 一条消息的行首 meta。群名/群号段可能已经被洗掉，所以做成可选的。
#: 昵称里可能自带中括号（QQ 头衔编码坏掉后落成「[%ĀĀ␇Ö] [@你]我在未来，等你」，用户 m04917 图1），
#: 所以先按「, user_id:」这个结尾锚定整段昵称；认不到才退回不允许中括号的旧写法。
#: 时间前缀两种写法都要认：短格式「[17:46]」和框架偶尔给的长格式
#: 「[Oct 29 2026 17:46 Thu]」。以前只认纯数字，长格式那几个会话的标记一直
#: 打不上（用户 m12833：@你 时有时无）。
_META_RE = re.compile(
    r"^(?P<meta>\[[^\]\n]{1,32}\]\s*\[message_id:[^\]]{1,200}\]\s*"
    r"(?:\[group_name:[^\]]*\]\s*)?(?:\[group_id:[^\]]*\]\s*)?"
    r"(?:\[user_nickname:[\s\S]*?,\s*user_id:[^\]]*\]"
    r"|\[user_nickname:[^\]]*\]))"
)


def mark_mention(chunk: str, names) -> str:
    """一条消息里提到机器人时，在 meta 后面插一个 [@你]。

    只在 meta 段之后插，正文一个字不动——用户 m23841 要的就是这个位置。
    names 是昵称 + 外号，宽松匹配（正文里出现就算），因为 QQ 侧「@某人」
    落下来就是一段普通文本，没有结构化标记可依。
    """
    if not chunk or not names:
        return chunk
    m = _META_RE.match(chunk)
    if not m:
        return chunk
    body = chunk[m.end("meta"):]
    # 只在 meta 之后判「是不是已经打过标记」：昵称里也可能出现 [@你] 这几个字，
    # 拿整条 chunk 判会把它当成已打过，结果真正叫到机器人的消息反而没有徽章。
    if MARK.strip() in body:
        return chunk
    lowered = body.lower()
    for name in names:
        name = str(name or "").strip()
        if name and name.lower() in lowered:
            return m.group("meta") + MARK + body
    return chunk


def mark_block(text: str, names) -> str:
    """一条 content 里可能挤着好几条群消息（一行一条），逐行判、逐行标。

    以前整段直接交给 mark_mention，正则的 ^ 只认第一行，同一条 user 里
    第二条起永远拿不到标记——窗口里就表现为「@你 时有时无」（用户 m12833）。
    """
    if not isinstance(text, str) or not text or not names:
        return text
    if "\n" not in text:
        return mark_mention(text, names)
    lines = text.split("\n")
    changed = False
    for i, line in enumerate(lines):
        new = mark_mention(line, names)
        if new != line:
            lines[i] = new
            changed = True
    return "\n".join(lines) if changed else text


def _set_content(msg, content) -> None:
    if isinstance(msg, dict):
        msg["content"] = content
        return
    try:
        msg.content = content
    except Exception:
        pass


def mark_messages(messages, names) -> int:
    """对一串消息里 role=user 的正文打唤醒标记，返回改动条数。"""
    if not messages or not names:
        return 0
    changed = 0
    for msg in messages:
        if (_get(msg, "role") or "") != "user":
            continue
        content = _get(msg, "content")
        if isinstance(content, str):
            new = mark_block(content, names)
            if new != content:
                _set_content(msg, new)
                changed += 1
            continue
        if isinstance(content, (list, tuple)):
            parts = list(content)
            hit = False
            for i, item in enumerate(parts):
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = str(item.get("text") or "")
                new = mark_block(text, names)
                if new != text:
                    parts[i] = dict(item, text=new)
                    hit = True
            if hit:
                _set_content(msg, parts)
                changed += 1
    return changed


def mark_copies(messages, names):
    """给一批消息打唤醒标记，返回新列表：改过的复制一份，不动调用方手里的对象。

    框架给过来的消息对象是它自己的记忆条目，就地改会把标记写回框架那边，
    别的插件跟着看到一串 [@你]。所以这里只在副本上改。
    """
    out = []
    for msg in messages or []:
        if (_get(msg, "role") or "") != "user":
            out.append(msg)
            continue
        content = _get(msg, "content")
        if isinstance(content, str):
            new = mark_block(content, names)
        elif isinstance(content, (list, tuple)):
            parts = list(content)
            hit = False
            for i, item in enumerate(parts):
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = str(item.get("text") or "")
                one = mark_block(text, names)
                if one != text:
                    parts[i] = dict(item, text=one)
                    hit = True
            new = parts if hit else content
        else:
            out.append(msg)
            continue
        if new == content:
            out.append(msg)
            continue
        copy = clone_message(msg)
        try:
            setattr(copy, "content", new)
        except Exception:
            if isinstance(copy, dict):
                copy["content"] = new
            else:
                out.append(msg)
                continue
        out.append(copy)
    return out


#: 图片 part 的类型（与 manager.IMAGE_TYPES 保持一致，避免循环导入）
_IMG_TYPES = ("image_url", "image", "kira_image_ref", "input_image")

#: 一条消息的行首 message_id
_ID_RE = re.compile(r"\[message_id:\s*([^\]]{1,200})\]")

#: 框架落盘图片的目录前缀：session_media/<sha256(sid)>/<sha256(message_id)>/<文件>
_MEDIA_PREFIX = "session_media/"


def _plain_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(
            str(p.get("text") or "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return ""


def with_owned_images(content, refs, raw_sid: str, force_list: bool = False):
    """Attach only direct-message refs with exact session and owner hashes.

    force_list=True：无论有没有 refs 都交回数组形态（v4.41，用户 m21746）。
    历史行也走这条，同一条消息的形态从头到尾不变——不然图一过保鲜期，
    这条消息就从数组变回字符串，上游 token 序列跟着变，前缀缓存整段作废。
    """
    # v4.25（用户 m22166）：带图消息的形态必须稳定。以前只有真拿到 refs 才把
    # content 换成数组，同一条历史消息就会随「这轮图在不在」在字符串与数组之间
    # 来回变——上游的 token 序列跟着变，字符前缀整段断掉，命中预估掉到 0.1%。
    # 现在只要文本里有 [Image attached] 占位就固定成数组形态（有 refs 就带上）。
    text = _plain_text(content)
    want_list = (force_list or isinstance(content, (list, tuple))
                 or _IMAGE_PLACEHOLDER in text)
    parts = list(content) if isinstance(content, (list, tuple)) else [{"type": "text", "text": text}]
    heads = [_HEAD_RE.match(line) for line in text.split("\n")]
    mids = {m.group(2).strip() for m in heads if m}
    if len(mids) != 1 or not raw_sid:
        return parts if want_list else content
    owner = hashlib.sha256(next(iter(mids)).encode("utf-8")).hexdigest()
    session = hashlib.sha256(raw_sid.encode("utf-8")).hexdigest()
    have = {p.get("path") for p in parts if isinstance(p, dict) and p.get("type") in _IMG_TYPES}
    changed = False
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or "")
        if "\\" in path or path in have or not path.startswith(_MEDIA_PREFIX):
            continue
        segs = path.split("/")
        if any(s in ("", ".", "..") for s in segs):
            continue
        # v4.38（用户 m20312）：框架现在按内容哈希归档图片，形态是
        # session_media/archive/<sha256>（三段、跟 message_id 无关）；老的四段
        # session_media/<会话哈希>/<消息哈希>/<文件> 也还在用。以前只认四段，
        # 新到的图在组装请求时被整段丢掉，模型那边就只剩一个 [图片] 空壳。
        owner_ok = len(segs) == 4 and segs[1] == session and segs[2] == owner
        archive_ok = len(segs) == 3 and segs[1] == "archive"
        if not (owner_ok or archive_ok):
            continue
        mime = str(ref.get("mime_type") or ref.get("mime") or "").strip()
        if mime and not mime.startswith("image/"):
            continue
        if not mime:
            mime = _looks_image(path)
        if not mime:
            continue
        parts.append({"type": "kira_image_ref", "path": path, "mime_type": mime,
                      "detail": str(ref.get("detail") or "high")})
        have.add(path)
        changed = True
    return parts if (changed or want_list) else content


def _looks_image(rel: str) -> str:
    """归档文件没有后缀、也没有 mime，读前 16 字节认类型；实在读不了就当 jpeg。"""
    head = b""
    try:
        from pathlib import Path

        from core.utils.path_utils import get_data_path

        target = Path(get_data_path()) / str(rel).replace("\\", "/")
        with open(target, "rb") as fh:
            head = fh.read(16)
    except Exception:
        return "image/jpeg"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return "image/jpeg"


def _images_of(content) -> list:
    if not isinstance(content, (list, tuple)):
        return []
    return [p for p in content if isinstance(p, dict) and p.get("type") in _IMG_TYPES]


def _ref_owner(part) -> str:
    """从图片 path 里取出它所属 message_id 的 sha256；不是框架标准路径就返回空。"""
    path = part.get("path") if isinstance(part, dict) else ""
    if not isinstance(path, str) or not path.startswith(_MEDIA_PREFIX):
        return ""
    segs = path.split("/")
    if len(segs) < 4:
        return ""
    return segs[2]


def _with_refs(content, refs) -> list:
    rest = [p for p in content if not (isinstance(p, dict) and p.get("type") in _IMG_TYPES)]
    return list(rest) + list(refs)


_IMAGE_PLACEHOLDER = "[Image attached]"


def _trim_quoted_images(messages):
    """丢掉引用 / 合并转发里带进来的图片（v2.55，用户 m26768）。

    框架的 MessageMediaService.iter_images 会递归进 Reply.chain 和 Forward.chains
    （core/workflow/src/im/native_content.py），于是「@JM叶 够扫」这种回复里带合并
    转发的消息，会被塞进转发里的全部图片。这些图文本里没有对应的
    [Image attached] 占位，模型对不上号，还每轮请求重复发一遍。

    只统计框架自己存盘的 kira_image_ref（path 里带 message_id 哈希），
    外部直链 image_url 不参与，避免误伤多模态消息。
    """
    if not messages:
        return messages
    out = []
    touched = False
    for m in messages:
        content = _get(m, "content")
        managed = [p for p in _images_of(content) if _ref_owner(p)]
        if len(managed) <= 1:
            out.append(m)
            continue
        text = _plain_text(content)
        n_ph = text.count(_IMAGE_PLACEHOLDER) + text.count("[Sticker attached]")
        if len(managed) <= n_ph:
            out.append(m)
            continue
        body, kept = [], 0
        for part in content:
            if isinstance(part, dict) and _ref_owner(part):
                if kept < n_ph:
                    body.append(part)
                    kept += 1
            else:
                body.append(part)
        if not any(isinstance(x, dict) and x.get("type") == "text" for x in body):
            body = [{"type": "text", "text": "[图片]"}] + body
        item = clone_message(m)
        if isinstance(item, dict):
            item["content"] = body
        else:
            try:
                item.content = body
            except Exception:
                item = {"role": _get(m, "role") or "user", "content": body}
        out.append(item)
        touched = True
    return out if touched else messages


def regroup_images(messages):
    """把挂错消息的图片挪回它真正的主人（v2.54，用户 m26465）。

    框架 assemble 阶段会把本批所有消息的图片统统塞进最后一条 user
    （core/workflow/src/im/stages/assemble.py:57-64），于是「够扫」那条的
    12 张图跑到了「如何评价虚拟主播真白花音」上，页面上就是凭空多出
    一屏 [图片]。图片落盘路径的第二段是 sha256(message_id)，所以能反查
    出每张图真正属于谁：对不上就摘下来，挂回原主，原主不在窗口里就丢掉。

    保护：消息里没有任何 [message_id: …]（比如 assistant 的自述）时不动它的图；
    ref 不是 session_media/… 标准路径时也不动。
    """
    if not messages:
        return messages
    if not any(_images_of(_get(m, "content")) for m in messages):
        return messages
    msgs = list(messages)

    hashes = []
    for m in msgs:
        text = _plain_text(_get(m, "content"))
        hashes.append(
            {
                hashlib.sha256(mid.strip().encode("utf-8")).hexdigest()
                for mid in _ID_RE.findall(text)
                if mid.strip()
            }
        )

    contents = [_get(m, "content") for m in msgs]
    new_contents = list(contents)
    changed = [False] * len(msgs)
    orphans = []  # (来源下标, part, 归属哈希)

    for i, content in enumerate(contents):
        refs = _images_of(content)
        if not refs or not hashes[i]:
            continue
        keep, lost = [], []
        for part in refs:
            owner = _ref_owner(part)
            if not owner or owner in hashes[i]:
                keep.append(part)
            else:
                lost.append((part, owner))
        if not lost:
            continue
        for part, owner in lost:
            orphans.append((i, part, owner))
        new_contents[i] = _with_refs(content, keep)
        changed[i] = True

    if not orphans:
        return _trim_quoted_images(msgs)

    owner_index = {}
    for i, hs in enumerate(hashes):
        for h in hs:
            owner_index.setdefault(h, i)

    claimed = {}
    for src, part, owner in orphans:
        idx = owner_index.get(owner)
        if idx is None or idx == src:
            continue
        claimed.setdefault(idx, []).append(part)

    for idx, parts in claimed.items():
        base = new_contents[idx]
        if isinstance(base, (list, tuple)):
            new_contents[idx] = _with_refs(base, _images_of(base) + parts)
        else:
            text = _plain_text(base)
            new_contents[idx] = [{"type": "text", "text": text}] + list(parts)
        changed[idx] = True

    out = []
    for i, m in enumerate(msgs):
        if not changed[i]:
            out.append(m)
            continue
        item = clone_message(m)
        if isinstance(item, dict):
            item["content"] = new_contents[i]
        else:
            try:
                item.content = new_contents[i]
            except Exception:
                item = {"role": _get(m, "role") or "user", "content": new_contents[i]}
        out.append(item)
    return out
