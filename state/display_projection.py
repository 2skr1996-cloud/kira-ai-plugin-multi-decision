"""Read-only Web ownership projection; never rewrite model media or message chains."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

_MAX_IDS = 900  # Sid/limit plus ids stay below SQLite's legacy 999-variable limit.
_MAX_ROWS = 4096
_MAX_CHAIN_BYTES = 2 * 1024 * 1024
_OWNER_HEAD = re.compile(
    r'^\s*(?:\[(?:(?:\d{4}-\d{2}-\d{2} )?\d{1,2}:\d{2}(?::\d{2})?'
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{4} '
    r'\d{1,2}:\d{2}(?::\d{2})? (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))\]\s*)?'
    r'(?:\[message_id:\s*([^\]\s]+)\]|<msg\b[^>]*\bmessage_id=["\']([^"\']+)["\'])',
    re.I,
)
_REPLY_HEAD = re.compile(r"^\s*\[Reply ID:\s*([^\]\s]+)", re.I)
_LEAF_TYPES = {"text", "at", "emoji", "notice", "poke", "record", "file", "video", "json"}


def content_text(content) -> str:
    """Extract only text parts, never a Python repr of structured content."""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(
            part["text"] for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def _item_text(item, text_of_content=None) -> str:
    extract = text_of_content or content_text
    if not isinstance(item, dict):
        return ""
    for key in ("text", "content", "raw"):
        try:
            text = extract(item.get(key))
        except Exception:
            continue
        if isinstance(text, str) and text:
            return text
    return ""


def message_id(item, *, text_of_content=None) -> str:
    """Return the explicit id or the first outer owner header, never a quoted id."""
    if not isinstance(item, dict):
        return ""
    for key in ("message_id", "platform_message_id"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    extract = text_of_content or content_text
    for key in ("text", "content", "raw"):
        try:
            text = extract(item.get(key))
        except Exception:
            continue
        match = _OWNER_HEAD.match(text) if isinstance(text, str) else None
        if match:
            return str(match.group(1) or match.group(2) or "").strip()
    return ""


def read_native_rows(raw_sid: str, message_ids, *, db_path=None, timeout: float = 0.25) -> dict[str, dict]:
    """Read one bounded exact-(raw sid, mid) batch, without caches or writes.

    Missing/locked/malformed records remain unknown and may be retried next GET.
    db_path injection keeps standalone tests away from the real application DB.
    """
    if not isinstance(raw_sid, str) or not raw_sid:
        return {}
    want = []
    seen = set()
    for index, value in enumerate(message_ids or ()):
        if index >= _MAX_ROWS:
            break
        if not isinstance(value, (str, int)):
            continue
        mid = str(value).strip()
        if mid and mid not in seen:
            want.append(mid)
            seen.add(mid)
        if len(want) >= _MAX_IDS:
            break
    if not want:
        return {}
    conn = None
    out = {}
    try:
        budget = min(1.0, max(0.01, float(timeout)))
        deadline = time.monotonic() + budget
        if db_path is None:
            from core.utils.path_utils import get_data_path

            db_path = Path(get_data_path()) / "data.db"
        uri = Path(db_path).resolve().as_uri()
        # SQLite on Windows rejects URI authorities other than localhost; a UNC
        # path belongs in the filename with an empty authority, not in the host.
        if uri.startswith("file://") and not uri.startswith("file:///"):
            uri = "file:////" + uri[len("file://"):]
        uri += "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=budget)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        placeholders = ",".join("?" for _ in want)
        cursor = conn.execute(
            "SELECT session_id, platform_message_id, chain, sender_name, timestamp "
            "FROM messages WHERE session_id = ? AND platform_message_id IN ("
            + placeholders + ") ORDER BY timestamp DESC, rowid DESC LIMIT ?",
            [raw_sid, *want, _MAX_ROWS],
        )
        for row in cursor:
            if time.monotonic() >= deadline:
                break
            mid = str(row["platform_message_id"] or "")
            if row["session_id"] != raw_sid or mid not in seen or mid in out:
                continue
            encoded = row["chain"]
            if not isinstance(encoded, (str, bytes)) or len(encoded) > _MAX_CHAIN_BYTES:
                continue
            try:
                chain = json.loads(encoded)
            except (ValueError, TypeError, RecursionError):
                continue
            if not isinstance(chain, list):
                continue
            out[mid] = dict(row)
            out[mid]["chain"] = chain
            if len(out) == len(want):
                break
    except Exception:
        # This observation-only projection must never make /window fail.
        pass
    finally:
        if conn is not None:
            conn.close()
    return out


def _unique_refs(refs) -> list:
    out = []
    seen = set()
    for ref in refs if isinstance(refs, (list, tuple)) else ():
        if not isinstance(ref, dict):
            continue
        key = str(ref.get("url") or ref.get("path") or "")
        if not key and not ref.get("missing"):
            continue
        identity = key or ("missing", str(ref.get("mime") or ""))
        if identity not in seen:
            seen.add(identity)
            out.append(dict(ref))
    return out


def _merge_slots(*sources):
    """Union alternate attachment views without dropping repeated occurrences."""
    out, counts = [], {}
    for refs in sources:
        local = {}
        for ref in refs if isinstance(refs, (list, tuple)) else ():
            if not isinstance(ref, dict):
                continue
            key = str(ref.get("url") or ref.get("path") or "")
            if not key and not ref.get("missing"):
                continue
            key = key or ("missing", str(ref.get("mime") or ""))
            local[key] = local.get(key, 0) + 1
            if local[key] > counts.get(key, 0):
                out.append(dict(ref))
        for key, count in local.items():
            counts[key] = max(counts.get(key, 0), count)
    return out


def _content_refs(item):
    """Only explicit original content parts; aggregate media fields are not proof."""
    for key in ("content", "raw"):
        content = item.get(key)
        if not isinstance(content, (list, tuple)):
            continue
        refs = []
        for part in content:
            if not isinstance(part, dict):
                return None
            kind = part.get("type")
            if kind == "text":
                continue
            if kind == "kira_image_ref":
                refs.append({"path": part.get("path"), "mime": part.get("mime_type") or part.get("mime") or ""})
            elif kind in ("image_url", "input_image"):
                value = part.get("image_url")
                refs.append({"url": value.get("url") if isinstance(value, dict) else value, "mime": ""})
            else:
                return None
        return refs
    return None


def raw_forward_proof_items(messages):
    """Retain unprepared snapshot content, never convert aggregate refs into parts."""
    return [dict(msg) if isinstance(msg, dict) else {"content": getattr(msg, "content", None)}
            for msg in messages or []]


def _unassigned_media(item, raw_sid, public_media):
    refs = [dict(ref) for ref in item.get("media") or [] if isinstance(ref, dict)]
    explicit = _content_refs(item) if "[Forward" in content_text(item.get("content") or item.get("raw")) else None
    pending = _merge_slots(item.get("media_refs"), explicit)
    converted = []
    for ref in pending:
        if not isinstance(ref, dict):
            continue
        path = str(ref.get("path") or ref.get("url") or "")
        mime = str(ref.get("mime") or ref.get("mime_type") or "")
        try:
            got = public_media(path, mime, raw_sid) if path else dict(ref)
        except Exception:
            got = {}
        if not isinstance(got, dict) or not (got.get("path") or got.get("url") or got.get("missing")):
            got = {"missing": 1, "path": path, "mime": mime} if path else {}
        converted.append(got)
    return _merge_slots(refs, converted)


def _direct_reply_from_text(text: str):
    # Only the beginning of the outer body can introduce a direct reply.
    split = re.split(r"\]\s*\|\s*", text, maxsplit=1)
    body = split[1] if len(split) == 2 else text
    match = _REPLY_HEAD.match(body)
    return {"id": match.group(1)} if match else None


def _forward_own_body(text: str):
    """Keep the existing Forward card syntax, removing only the outer Reply span."""
    split = re.split(r"\]\s*\|\s*", text, maxsplit=1)
    body = split[1] if len(split) == 2 else text
    xml = re.search(r"<text>([\s\S]*?)</text>", body, re.I)
    if xml:
        body = xml.group(1)
    match = _REPLY_HEAD.match(body)
    if match:
        depth = 0
        end = None
        for index, char in enumerate(body):
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            return None
        body = body[end:].lstrip()
    return body.strip() if "[Forward" in body else None


def _native_display(chain, raw_sid: str, public_media):
    refs = []
    body = []
    nodes = 0
    needs_original_body = False
    direct = None
    for part in chain:
        if isinstance(part, dict) and part.get("type") == "reply":
            value = part.get("message_id")
            if isinstance(value, (str, int)) and str(value).strip():
                direct = {"id": str(value).strip()}
            break

    def walk(parts, depth=0):
        nonlocal nodes, needs_original_body
        if not isinstance(parts, list) or depth > 8:
            return False
        for part in parts:
            nodes += 1
            if nodes > 4096 or not isinstance(part, dict):
                return False
            kind = part.get("type")
            if not isinstance(kind, str):
                return False
            if kind == "reply":
                continue  # Including every image/text/Forward below this Reply.
            if kind == "forward":
                needs_original_body = True
                children = part.get("chains")
                if not isinstance(children, list) or not children:
                    return False  # Native serializers may retain only {type: forward}.
                for nested in children:
                    if not walk(nested, depth + 1):
                        return False
                continue
            if kind not in _LEAF_TYPES and kind not in {"image", "sticker"}:
                return False  # Unsupported containers are not evidence of zero images.
            if any(key in part for key in ("chain", "chains", "children", "message_list")):
                return False
            if kind in {"image", "sticker"} or (kind == "emoji" and part.get("file")):
                path = part.get("file") or part.get("path") or part.get("url")
                if not isinstance(path, str) or not path.strip():
                    return False
                mime = str(part.get("mime") or part.get("mime_type") or "")
                try:
                    ref = public_media(path, mime, raw_sid)
                except Exception:
                    ref = {}
                if not isinstance(ref, dict) or not (ref.get("path") or ref.get("url")):
                    # A known source path remains a missing-image hint, never known empty.
                    ref = {"missing": 1, "path": path, "mime": mime}
                refs.append(ref)
            elif kind == "text":
                value = part.get("text")
                if not isinstance(value, str):
                    return False
                body.append(value)
            elif kind == "at":
                body.append("@" + str(part.get("nickname") or part.get("pid") or "") + " ")
            elif kind == "emoji":
                body.append("[表情]")
            else:
                # Video/file/record/JSON/notice/poke cards have richer existing text.
                # Known zero images is not proof that their own body is empty.
                needs_original_body = True
        return True

    known = walk(chain)
    return known, refs, direct, None if needs_original_body else "".join(body).strip()


def _owner_forward_body(original, mid, raw_sid):
    if (not isinstance(original, dict) or message_id(original) != mid
            or any(original.get(key) and original[key] != raw_sid for key in ("raw_sid", "session_id"))):
        return None
    bodies = []
    # content/raw are the original snapshot. text may have lost all image slots
    # in view_of, and may only stand alone when no original field was retained.
    keys = ("content", "raw") if any(original.get(k) is not None for k in ("content", "raw")) else ("text",)
    for key in keys:
        text = content_text(original.get(key))
        if not text:
            continue
        if len(text) > _MAX_CHAIN_BYTES:
            return None
        head = _OWNER_HEAD.match(text)
        if not head or str(head.group(1) or head.group(2) or "").strip() != mid:
            return None
        outer = re.fullmatch(r"\s*(?:\[user_nickname:[^\]\r\n]*\]\s*)?\|\s*([\s\S]*)", text[head.end():])
        if not outer:
            return None
        bodies.append(outer.group(1).strip())
    return bodies[0] if bodies and all(body == bodies[0] for body in bodies) else None


def _complete_forward(body):
    if (not isinstance(body, str) or len(body) > _MAX_CHAIN_BYTES
            or not re.match(r"^\[Forward\s+\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?\] [^\n]*:", body)
            or len(re.findall(r"\[Forward\b", body, re.I)) != 1
            or re.search(r"\[Reply\b", body, re.I)):
        return False
    depth = 0
    for index, char in enumerate(body):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0 or (depth == 0 and index != len(body) - 1):
                return False
    return depth == 0 and body.endswith("]")


def _public_slots(refs, raw_sid, public_media):
    if not isinstance(refs, (list, tuple)) or len(refs) > _MAX_ROWS:
        return None
    out = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("missing"):
            return None
        path = ref.get("path") or ref.get("url")
        if not isinstance(path, str) or not path:
            return None
        try:
            got = public_media(path, str(ref.get("mime") or ref.get("mime_type") or ""), raw_sid)
        except Exception:
            return None
        if (not isinstance(got, dict) or got.get("missing")
                or not (got.get("path") or got.get("url"))):
            return None
        out.append(dict(got))
    return out


def _forward_cache_display(original, mid, raw_sid, chain, public_media, display_proof_for_mid):
    """Authorize only independent original parts or a real fetch's sid-bound nodes.

    Aggregate media/media_refs and the old global mid cache are never evidence.
    v1 node order (including repeated files) is authoritative, not view_of's media.
    """
    if not raw_sid or chain != [{"type": "forward"}]:
        return None
    body = _owner_forward_body(original, mid, raw_sid)
    if body is None:
        return None
    refs = _content_refs(original)
    if refs:
        # Explicit original image parts are a separate, complete source. They may
        # not be paired with a placeholder or a truncated/Reply-containing body.
        if not _complete_forward(body) or not 0 < body.count("[图片]") == len(refs):
            return None
        ready = _public_slots(refs, raw_sid, public_media)
        return (body, ready) if ready is not None else None
    if not callable(display_proof_for_mid):
        return None
    try:
        proof = display_proof_for_mid(raw_sid, mid)
    except Exception:
        return None
    if (not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1
            or proof.get("raw_sid") != raw_sid or proof.get("outer_mid") != mid
            or not isinstance(proof.get("res_id"), str) or not proof["res_id"]
            or len(proof["res_id"]) > 2048):
        return None
    nodes = proof.get("nodes")
    if not isinstance(nodes, list) or not 0 < len(nodes) <= 128:
        return None
    lines, refs = [], []
    for node in nodes:
        if not isinstance(node, dict):
            return None
        text, media = node.get("text"), node.get("media")
        if (not isinstance(text, str) or not text or len(text) > 65536
                or not isinstance(media, list) or text.count("[图片]") != len(media)
                or len(re.findall(r"\[图片\]|\[image(?: attached)?\]|\[CQ:image[^\]]*\]", text, re.I)) != len(media)):
            return None
        lines.append(text)
        refs.extend(media)
    canonical = "[Forward " + "\n\n".join(lines) + "]"
    if (proof.get("body") != canonical or not _complete_forward(canonical)
            or not 0 < len(refs) <= 256):
        return None
    # After a restart only the native empty shell can stand in for the canonical
    # fetched body. Only the host image marker spelling may differ; other text is exact.
    if body not in ("[Forward]", "[Forward message]") and body.replace("[Image attached]", "[图片]") != canonical:
        return None
    ready = _public_slots(refs, raw_sid, public_media)
    return (canonical, ready) if ready is not None else None


def project_items(items, raw_sid: str, native_rows: dict, *, public_media, text_of_content=None,
                  forward_media_for_mid=None, forward_proof_items=None, forward_display_proof_for_mid=None) -> list:
    """Copy API items and add display only; aggregate media/raw/text stay untouched.

    public_media is the injected (path, mime, raw_sid) callback. Unknown aggregate
    attachments are neutral unassigned refs and never become own/quoted media.
    forward_proof_items, when supplied, is the independent original same-session
    snapshot before any compatibility attachment fill; never pass filled copies.
    """
    # Explicit proof_items must be the caller's original same-session items, before
    # compatibility attachment recovery. Duplicate mids are ambiguous, not proof.
    proofs = {}
    for proof in (items if forward_proof_items is None else forward_proof_items) or []:
        proof_mid = message_id(proof, text_of_content=text_of_content)
        if proof_mid:
            proofs[proof_mid] = None if proof_mid in proofs else proof
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        mid = message_id(item, text_of_content=text_of_content)
        row = native_rows.get(mid) if isinstance(native_rows, dict) else None
        extract = text_of_content or content_text
        texts = []
        for key in ("text", "content", "raw"):
            try:
                text = extract(item.get(key))
            except Exception:
                continue
            if isinstance(text, str) and text:
                texts.append(text)
        direct = next((reply for text in texts
                       if (reply := _direct_reply_from_text(text)) is not None), None)
        display = {
            "own_media_known": False, "own_media": [],
            "unassigned_media": [],
            "direct_reply": direct, "source": "unknown",
        }
        native_candidates = []
        if (isinstance(row, dict) and row.get("session_id") == raw_sid
                and str(row.get("platform_message_id") or "") == mid
                and isinstance(row.get("chain"), list)):
            known, refs, direct, body = _native_display(row["chain"], raw_sid, public_media)
            native_candidates = refs
            if direct is not None:
                display["direct_reply"] = direct
            if known:
                display["direct_reply"] = direct
                if not any(ref.get("missing") for ref in refs):
                    display.update(own_media_known=True, own_media=refs, unassigned_media=[],
                                   source="native_chain")
                # Media readiness may be unknown while native own text is certain.
                if body is None:
                    body = next((kept for text in texts
                                 if (kept := _forward_own_body(text)) is not None), None)
                if body is not None:
                    display["own_body"] = body
            else:
                aligned = _forward_cache_display(
                    proofs.get(mid), mid, raw_sid, row["chain"], public_media,
                    forward_display_proof_for_mid,
                )
                if aligned is not None:
                    body, refs = aligned
                    display.update(own_media_known=True, own_media=refs, own_body=body,
                                   unassigned_media=[], source="forward_cache_aligned")
        if not display["own_media_known"]:
            original = proofs.get(mid)
            display["unassigned_media"] = _merge_slots(
                _unassigned_media(original, raw_sid, public_media) if isinstance(original, dict) else [],
                _unassigned_media(item, raw_sid, public_media), native_candidates,
            )
        projected = dict(item)
        projected["display"] = display
        out.append(projected)
    return out
