"""Decision-only prefix evidence; never reads or encodes image files."""
from __future__ import annotations

import copy
import hashlib
import json
import re


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def build_fingerprints(messages, dynamic="") -> dict:
    """Hash the already-built base chat, removing only the final injected suffix.

    Part/role order, detail and the complete data URL participate in the digest.
    Inputs are not mutated and only hexadecimal digests leave this helper.
    """
    rows = copy.deepcopy(list(messages or []))
    if dynamic:
        for row in reversed(rows):
            if not isinstance(row, dict) or row.get("role") != "user":
                continue
            content = row.get("content")
            if isinstance(content, list):
                if (content and isinstance(content[-1], dict)
                        and content[-1].get("type") == "text"
                        and content[-1].get("text") == dynamic):
                    row["content"] = content[:-1]
            elif isinstance(content, str) and content.endswith("\n\n" + dynamic):
                row["content"] = content[:-len("\n\n" + dynamic)]
            break
    return {"version": 1, "scope": "base_chat_pre_dynamic",
            "message_hashes": [_digest(row) for row in rows]}


def valid_fingerprints(record, count):
    """Reject malformed or partial captures, including otherwise valid short lists."""
    value = record.get("chat_comparison")
    if not isinstance(value, dict):
        return None
    hashes = value.get("message_hashes")
    if (type(value.get("version")) is not int or value["version"] != 1
            or value.get("scope") != "base_chat_pre_dynamic"
            or not isinstance(hashes, list) or len(hashes) != count
            or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h)
                   for h in hashes)):
        return None
    return list(hashes)


_USER = re.compile(
    r"^\s*\[(?P<time>[^\]]{1,24})\]\s*"
    r"(?:\[message_id:[^\]]*\]\s*)?"
    r"\[user_nickname:\s*(?P<nick>[^,\]\n]+)(?:,[^\]]*)?\]\s*\|?\s*"
    r"(?P<body>[\s\S]*)$")
_BOT = re.compile(r"<msg\b[^>]*>([\s\S]*?)</msg>", re.S)
_BOT_TEXT = re.compile(r"<text>([\s\S]*?)</text>", re.S)
_MID = re.compile(r'\[message_id:\s*(-?\d+)\]|<msg\b[^>]*\bmessage_id="?(-?\d+)"?', re.I)
_IMAGE_TYPES = ("kira_image_ref", "image_url", "image", "input_image")


def _text_of(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        out = []
        for part in value:
            if isinstance(part, dict):
                if part.get("type") == "text" or (not part.get("type") and "text" in part):
                    out.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                out.append(part)
        return "\n".join(x for x in out if x)
    return str(value)


def _clean_bot(text):
    raw = str(text or "").strip()
    if not raw:
        return []
    blocks = _BOT.findall(raw)
    if not blocks:
        return [raw]
    rows = []
    for block in blocks:
        m = _BOT_TEXT.search(block)
        body = (m.group(1) if m else block).strip()
        if body:
            t = re.search(r"<time>([^<]*)</time>", block)
            rows.append(("[%s] " % t.group(1).strip() if t else "") + body)
    return rows or [raw]


def _clean_user(text):
    raw = str(text or "").strip()
    m = _USER.match(raw)
    if not m:
        return raw
    nick, body = m.group("nick").strip(), m.group("body").strip()
    you = body.startswith("[@你]")
    if you:
        body = body[len("[@你]"):].lstrip()
    if body.startswith("|"):
        body = body[1:].lstrip()
    if not body:
        return raw if not you else "[%s] %s（叫你）" % (m.group("time").strip(), nick)
    return "[%s] %s%s：%s" % (m.group("time").strip(), nick, "（叫你）" if you else "", body)


def legacy_record_estimate(record, series):
    """Replay only saved facts, with explicit uncertainty; no filesystem/image IO.

    decision_images is the *used* list. Incoming media is not a sent-image list.
    Raw-row boundaries are retained and transcript must verify byte-for-byte.
    No later record can supply a missing GIF caption or any other evidence.
    """
    reasons = {"legacy_record_estimate"}

    def unknown(reason):
        return {"hashes": [], "comparable": False, "reasons": sorted(reasons | {reason})}

    if not isinstance(series, list) or any(not isinstance(r, dict) for r in series):
        return unknown("rows_invalid")
    images = record.get("decision_images", [])
    if not isinstance(images, list) or any(not isinstance(i, dict) for i in images):
        return unknown("used_images_invalid")
    if "decision_image_count" in record and record["decision_image_count"] != len(images):
        return unknown("used_image_count_mismatch")
    by_mid = {}
    for order, image in enumerate(images):
        mid = str(image.get("mid") or "")
        if not mid or not image.get("path") or not (image.get("mime") or image.get("mime_type")):
            return unknown("used_image_unassociated")
        by_mid.setdefault(mid, []).append((order, image))
    has_media = bool(images) or any(
        isinstance(r.get("content"), list) or r.get("media") for r in series)
    if has_media and "decision_images" not in record:
        return unknown("used_image_evidence_missing")
    transcript_rows, projected = [], []
    for row in series:
        role = str(row.get("role") or "").strip().lower()
        content = row.get("content")
        raw = _text_of(content).strip()
        if role not in ("user", "assistant") or not raw:
            return unknown("payload_row_count_mismatch")
        clean = (lambda t: "\n".join(_clean_bot(t))) if role == "assistant" else _clean_user
        body = clean(raw)
        if role == "assistant":
            transcript_rows.extend("【我】" + line for line in _clean_bot(raw))
        else:
            transcript_rows.append(body)
        match = _MID.search(raw)
        mid = str(match.group(1) or match.group(2)) if match else ""
        got = by_mid.pop(mid, []) if mid else []
        inline = isinstance(content, (list, tuple)) and any(
            isinstance(p, dict) and p.get("type") in _IMAGE_TYPES for p in content)

        def image_part(order, image, detail=None):
            # Missing evidence remains a literal null, never a wildcard.
            evidence = {k: copy.deepcopy(v) for k, v in image.items()
                        if k not in ("mid", "ts", "who", "detail", "mime_type", "mime")}
            evidence["mime"] = image.get("mime") or image.get("mime_type")
            evidence["bytes"] = image.get("bytes")
            evidence["b64"] = image.get("b64")
            evidence["order"] = order
            if not any(image.get(k) for k in ("sha256", "bytes_digest", "digest")):
                reasons.add("bytes_digest_missing")
            if detail is None:
                detail = image.get("detail")
            if not detail:
                detail = "high"
                reasons.add("detail_inferred")
            return {"type": "image_evidence", "image": evidence, "detail": detail}

        if got and inline:
            parts, selected, head_cleaned = [], set(), False
            for part in content:
                if isinstance(part, str):
                    if part:
                        parts.append({"type": "text", "text": part})
                    continue
                if not isinstance(part, dict):
                    return unknown("inline_part_invalid")
                typ = part.get("type")
                if typ == "text" or (not typ and "text" in part):
                    text = str(part.get("text") or "")
                    if not head_cleaned and text.strip():
                        text, head_cleaned = clean(text), True
                    if text:
                        parts.append({"type": "text", "text": text})
                elif typ in _IMAGE_TYPES:
                    value = part.get("image_url")
                    path = str(part.get("path") or part.get("url") or
                               (value.get("url") if isinstance(value, dict) else value) or "")
                    found = next(((order, im) for order, im in got
                                  if im["path"] == path and order not in selected), None)
                    if found is None:
                        return unknown("inline_used_image_mismatch")
                    order, image = found
                    detail = part.get("detail") or (value.get("detail") if isinstance(value, dict) else None)
                    parts.append(image_part(order, image, detail))
                    selected.add(order)
                else:
                    return unknown("inline_part_unsupported")
            if len(selected) != len(got):
                return unknown("inline_used_count_mismatch")
            projected.append({"role": role, "content": parts})
        elif got:
            projected.append({"role": role, "content":
                              [{"type": "text", "text": body or "[图片]"}] +
                              [image_part(order, image) for order, image in got]})
        else:
            if inline and "decision_images" not in record:
                return unknown("used_image_evidence_missing")
            projected.append({"role": role, "content": body})
    if by_mid:
        return unknown("used_image_unassociated")
    saved = record.get("decision_transcript")
    if saved is not None:
        if not isinstance(saved, str) or "\n".join(transcript_rows) != saved:
            return unknown("transcript_mismatch")
    elif has_media:
        return unknown("transcript_missing")
    if len(projected) != len(series):
        return unknown("payload_row_count_mismatch")
    return {"hashes": [_digest(r) for r in projected], "comparable": True,
            "reasons": sorted(reasons)}


def comparison_pair(previous, current, previous_series, current_series):
    """Choose one common representation for both ends; never zip hash dialects."""
    a = valid_fingerprints(previous, len(previous_series))
    b = valid_fingerprints(current, len(current_series))
    if a is not None and b is not None:
        return a, b, "base_chat_pre_dynamic", []
    a = legacy_record_estimate(previous, previous_series)
    b = legacy_record_estimate(current, current_series)
    reasons = sorted(set(a["reasons"] + b["reasons"]))
    if not a["comparable"] or not b["comparable"]:
        return [], [], "unknown", reasons
    return a["hashes"], b["hashes"], "legacy_record_estimate", reasons
