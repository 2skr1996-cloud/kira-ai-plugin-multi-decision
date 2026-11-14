# -*- coding: utf-8 -*-
"""空壳转发的补救：框架只给了「这里有一条合并转发」，没把里面的记录取回来。

根因在框架侧（core/adapter/src/qq/im.py 处理 forward 段的那几行）：它把**外层消息
自己的 message_id** 当成转发的 res_id 传给 get_forward_msg，NapCat 自然取不到东西，
于是构造出 Forward(chains=[])，格式化时退化成 "[Forward message]"。真正的 res_id
在 OneBot 段的 `data.id` 里（形如 jWdbJWOdruke9T6bZWztTd+8Q0SsvozCPjdipGsF2AuUcQCeCFCV95xqi869ITZ8）。

框架目录本机不可写（D: 是只读网络盘），只能在插件侧补一次：
  send_action("get_msg", {"message_id": 外层 id})  → data.message[].data.id
  send_action("get_forward_msg", {"id": res_id})  → data.messages[]
把每条记录拼成一行 `[时间] 昵称: 正文`，塞回元素的 chains，
engine._forward_text 就会照框架的格式吐出完整的 "[Forward …]"，前端照常出卡。
"""

from __future__ import annotations

import asyncio
import os
import re
import time

from core.logging_manager import get_logger

logger = get_logger("multi_decision", "orange")


def _norm_sid(sid: str) -> str:
    """只留 gm/dm + 对方 id，兼容带机器人名前缀的旧 raw_sid。"""
    text = str(sid or "").strip()
    parts = text.split(":")
    if len(parts) >= 3 and parts[-2] in ("gm", "dm") and parts[-1]:
        return parts[-2] + ":" + parts[-1]
    return text

# 同一条转发常常被读两次（进待发一次、结算时再一次），按 res_id 记一份就行。
_CACHE: dict = {}
_CACHE_ORDER: list = []
_CACHE_MAX = 64
# 框架历史里只留得下 "[Forward]" 这个空壳，正文由插件按 message_id 补回来；
# 下一轮 collect 把框架历史并进窗口时要用它换掉那枚空壳（key 是外层 message_id）。
_MID_LINES: dict = {}
_MID_ORDER: list = []
_MID_MAX = 128

_CQ_FWD_RE = re.compile(r"\[CQ:forward,[^\]]*?id=([^,\]]+)")
_LINE_ID_RE = re.compile(r"\[message_id:\s*(-?\d+)\]")


def _seg_text(seg) -> str:
    """把一条 OneBot 消息段压成一行文字。"""
    if not isinstance(seg, dict):
        return ""
    kind = str(seg.get("type") or "")
    data = seg.get("data")
    if not isinstance(data, dict):
        data = {}
    if kind == "text":
        return str(data.get("text") or "")
    if kind == "image":
        return "[图片]"
    if kind == "face":
        # 用户 m17145：小黄脸只有 id、没有图，写成框架自己的 [Emoji N]，
        # 页面就能按 id 取到真图（动图），不再只画一个 🙂 占位。
        fid = str(data.get("id") or data.get("face_id") or "").strip()
        return "[Emoji %s]" % fid if fid.isdigit() else "[表情]"
    if kind in ("mface", "marketface"):
        return "[表情]"
    if kind == "at":
        nm = str(data.get("name") or data.get("qq") or "").strip()
        return "@" + nm if nm else "@"
    if kind == "record":
        return "[语音]"
    if kind == "video":
        return "[视频]"
    if kind == "file":
        nm = str(data.get("file") or data.get("name") or "").strip()
        return "[文件 %s]" % nm if nm else "[文件]"
    if kind == "forward":
        return "[合并转发]"
    if kind == "json":
        return "[卡片]"
    if kind == "xml":
        return "[卡片]"
    if kind == "reply":
        return ""
    return ""


def _when_of(value) -> str:
    try:
        stamp = float(value)
    except Exception:
        return str(value or "").strip()
    if stamp <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
    except Exception:
        return ""


def _line_of(msg) -> str:
    """一条转发记录 → `[时间] 昵称: 正文`。时间/昵称要自己拼：engine 的
    _fw_time/_fw_name 走 getattr，对 dict 取不到值。"""
    if not isinstance(msg, dict):
        return ""
    segs = msg.get("message")
    if isinstance(segs, list):
        body = "".join(_seg_text(s) for s in segs)
    elif isinstance(segs, str):
        body = segs
    else:
        body = str(msg.get("raw_message") or "")
        body = _CQ_FWD_RE.sub("[合并转发]", body)
        body = re.sub(r"\[CQ:[^\]]+\]", "", body)
    body = body.replace("\r", " ").strip()
    sender = msg.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    nick = str(sender.get("card") or sender.get("nickname") or "").strip()
    if not nick:
        nick = str(msg.get("user_id") or "").strip()
    when = _when_of(msg.get("time"))
    head = ""
    if when:
        head += "[%s]" % when
    if nick:
        head += (" " if head else "") + nick + ":"
    return (head + (" " if (head and body) else "") + body).strip()


#: 转发里面的图（v4.47，用户 m24522 第 3 项）：框架只把转发压成文字，图一律变成
#: 「[图片]」四个字，卡片上就只剩占位。这里在补内容的时候顺手把图也存进插件缓存，
#: 前端按占位顺序认领（web 的 forwardHtml 早就会分图，只差一份 media）。
_PLUGIN_ID = "kira-ai-plugin-multi-decision"
_IMG_MAX = 8 * 1024 * 1024
_MID_MEDIA: dict = {}
_MID_MEDIA_ORDER: list = []
_MID_MEDIA_MAX = 64
# 进程重启就丢，这一份落盘（用户 m25038 第 4 项：老消息/老快照里的转发图还要能认回来）
_MEDIA_DB: dict = {}
_MEDIA_DB_LOADED = False
# 读文件失败时不再把内存清空、也不算「已加载」：下次再读一次，别让一次坏文件
# 把后面保存的映射盖掉。真要在坏文件之上写时，先把它原样另存一份。
_MEDIA_DB_LOAD_FAILED = False
_MEDIA_DB_BACKED_UP = False
_MEDIA_DB_MAX = 300
# v4.54（用户 m18227）：转发配图索引以前只记 mid -> refs，没有会话归属，没法按
# 「每个群 / 私聊各自的上限」裁。这里再落一份 mid -> raw_sid 的旁挂表：
# 老索引没有这条记录，统一归到空 sid（仍受一份上限约束），不猜它属于哪个群。
_MEDIA_SIDS: dict = {}
_MEDIA_SIDS_LOADED = False
_MEDIA_HARD_MAX = 5000
# 由插件注入：返回某个会话该留多少条（0 / 非法值 = 不限）。没注入时保持老行为。
_MEDIA_KEEP_FN = None
# v4.55（用户 m19940）：转发配图索引也按保留天数清理。老索引没有时间戳，首次读到
# 时按当前时间补一份，给已有记录留一个完整周期，不立刻删老数据。
_MEDIA_KEEP_DAYS_FN = None
_MEDIA_SEEN = {}
_MEDIA_SEEN_LOADED = False


def _media_cache_dir():
    try:
        from core.utils.path_utils import get_data_path
    except Exception:
        return None
    from pathlib import Path
    try:
        return Path(str(get_data_path())) / "plugin_data" / _PLUGIN_ID / "media_cache"
    except Exception:
        return None


def _media_db_path():
    folder = _media_cache_dir()
    if folder is None:
        return None
    try:
        return folder.parent / "forward_media.json"
    except Exception:
        return None


def _media_sids_path():
    folder = _media_cache_dir()
    if folder is None:
        return None
    try:
        return folder.parent / "forward_media_sids.json"
    except Exception:
        return None


def _media_seen_path():
    folder = _media_cache_dir()
    if folder is None:
        return None
    try:
        return folder.parent / "forward_media_seen.json"
    except Exception:
        return None


def _media_sids() -> dict:
    global _MEDIA_SIDS, _MEDIA_SIDS_LOADED
    if _MEDIA_SIDS_LOADED:
        return _MEDIA_SIDS
    path = _media_sids_path()
    if path is None or not path.is_file():
        _MEDIA_SIDS_LOADED = True
        return _MEDIA_SIDS
    try:
        import json
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 旁挂表坏了就当没有：统计和裁剪都按空 sid 处理，索引本身仍可读。
        _MEDIA_SIDS_LOADED = True
        return _MEDIA_SIDS
    if isinstance(blob, dict):
        _MEDIA_SIDS = {str(k): str(v) for k, v in blob.items()
                       if isinstance(v, str) and len(v) <= 512}
    _MEDIA_SIDS_LOADED = True
    return _MEDIA_SIDS


def _media_sids_save() -> None:
    path = _media_sids_path()
    if path is None:
        return
    import json
    import os
    import uuid
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
        blob = json.dumps(_media_sids(), ensure_ascii=False).encode("utf-8")
        with open(tmp, "xb") as fp:
            fp.write(blob)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        tmp = None
    except Exception as exc:
        logger.info("[multi-decision] 转发配图会话索引没写成 %s: %s",
                    type(exc).__name__, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _media_seen() -> dict:
    """转发配图索引每条的写入时间（epoch 秒），给按天清理用。"""
    global _MEDIA_SEEN, _MEDIA_SEEN_LOADED
    if _MEDIA_SEEN_LOADED:
        return _MEDIA_SEEN
    path = _media_seen_path()
    if path is None or not path.is_file():
        _MEDIA_SEEN_LOADED = True
        return _MEDIA_SEEN
    try:
        import json
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _MEDIA_SEEN_LOADED = True
        return _MEDIA_SEEN
    if isinstance(blob, dict):
        for key, value in blob.items():
            try:
                _MEDIA_SEEN[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    _MEDIA_SEEN_LOADED = True
    return _MEDIA_SEEN


def _media_seen_save() -> None:
    path = _media_seen_path()
    if path is None:
        return
    import json
    import os
    import uuid
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
        blob = json.dumps(_media_seen(), ensure_ascii=False).encode("utf-8")
        with open(tmp, "xb") as fp:
            fp.write(blob)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        tmp = None
    except Exception as exc:
        logger.info("[multi-decision] 转发配图时间索引没写成 %s: %s",
                    type(exc).__name__, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _media_db_pop(mid, sids=None) -> None:
    _MEDIA_DB.pop(mid, None)
    (sids if sids is not None else _media_sids()).pop(mid, None)
    _media_seen().pop(mid, None)


def _media_db_trim() -> None:
    """按会话裁转发配图索引。

    没注入 limit_fn 时保持老行为（全局 300 条）；注入后按每个 sid 各自的上限裁，
    只留最新插入的那几条，旧条目连同 sid 一起丢。
    v4.55（用户 m19940）：再按每个会话的保留天数先清一遍；老条目没有时间戳时
    按当前时间补一份，给它们一个完整周期，不立刻删。
    """
    sids = _media_sids()
    seen = _media_seen()
    days_fn = _MEDIA_KEEP_DAYS_FN
    if callable(days_fn):
        now = time.time()
        for mid in list(_MEDIA_DB.keys()):
            sid = str(sids.get(mid) or "")
            try:
                days = int(days_fn(sid) or 0)
            except Exception:
                days = 0
            if days <= 0:
                continue
            ts = seen.get(mid)
            if not isinstance(ts, (int, float)):
                seen[mid] = now
                continue
            if now - float(ts) > days * 86400:
                _media_db_pop(mid, sids)
    fn = _MEDIA_KEEP_FN
    if not callable(fn):
        while len(_MEDIA_DB) > _MEDIA_DB_MAX:
            _media_db_pop(next(iter(_MEDIA_DB)), sids)
        return
    counts = {}
    for mid in list(_MEDIA_DB.keys()):
        sid = str(sids.get(mid) or "")
        try:
            limit = int(fn(sid) or 0)
        except Exception:
            limit = 0
        if limit <= 0:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        if counts[sid] > limit:
            _media_db_pop(mid, sids)
    # 兜底：配置写坏 / 群特别多时别让进程内存无限长；正常不会碰到。
    while len(_MEDIA_DB) > _MEDIA_HARD_MAX:
        _media_db_pop(next(iter(_MEDIA_DB)), sids)


def set_media_db_limit_fn(fn) -> None:
    """插件注入「这个会话留多少条」的回调；传 None 恢复老的全局限额。"""
    global _MEDIA_KEEP_FN
    _MEDIA_KEEP_FN = fn


def set_media_db_keep_days_fn(fn) -> None:
    """插件注入「这个会话留多少天」的回调；传 None 不按天清理。"""
    global _MEDIA_KEEP_DAYS_FN
    _MEDIA_KEEP_DAYS_FN = fn


def _legacy_media_owners() -> dict:
    """旧转发配图没有 sid 旁挂表时，从原图凭证里反查归属。

    早期版本的 forward_media.json 只记 message_id，没有 forward_media_sids.json。
    原图凭证里同时有 outer_mid 和每条转发里的图片路径，可以补出大部分旧条目的
    会话；仍补不出的保持空归属，统计时不硬塞给某个群。
    """
    owners = {}
    try:
        proofs = _display_proofs_read()
    except Exception:
        return owners
    for proof in proofs.values():
        if not isinstance(proof, dict):
            continue
        owner = _norm_sid(str(proof.get("raw_sid") or ""))
        if not owner:
            continue
        mid = str(proof.get("outer_mid") or "").strip()
        if mid:
            owners.setdefault(mid, owner)
        for node in (proof.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            for media in (node.get("media") or []):
                if not isinstance(media, dict):
                    continue
                path = str(media.get("path") or "").strip()
                if path:
                    owners.setdefault(path, owner)
    return owners


def media_db_stats(sid: str = "") -> dict:
    """转发配图索引的条数 / 图片数 / 磁盘占用，给缓存记录页用。

    sid 非空时只算这个会话的条目（用户 m22090）；占用按这份子集序列化后的
    字节数算，不把整份混写索引都记到它头上。
    旧数据没有 sid 时先按原图凭证的 outer_mid / 图片路径反查；如果所有已知归属
    都指向同一个会话，就把仍无归属的旧条目算给这个会话，多个会话时不猜。
    """
    import json
    db = _media_db()
    sids = _media_sids()
    target = _norm_sid(str(sid or "").strip())
    legacy = _legacy_media_owners() if target else {}
    refs = 0
    by_sid = {}
    owners = {}
    for mid, items in db.items():
        n = len(items or [])
        refs += n
        owner = _norm_sid(str(sids.get(mid) or ""))
        if not owner:
            owner = legacy.get(str(mid)) or ""
            if not owner:
                for item in (items or []):
                    if not isinstance(item, dict):
                        continue
                    path = str(item.get("path") or "").strip()
                    owner = legacy.get(path) or ""
                    if owner:
                        break
        owners[str(mid)] = owner
        row = by_sid.setdefault(owner, {"count": 0, "refs": 0})
        row["count"] += 1
        row["refs"] += n
    size = 0
    for path in (_media_db_path(), _media_sids_path(), _media_seen_path()):
        try:
            if path is not None and path.is_file():
                size += int(path.stat().st_size or 0)
        except Exception:
            pass
    if target:
        known = {owner for owner in owners.values() if owner}
        only = next(iter(known)) if len(known) == 1 else ""
        subset = {}
        for mid, items in db.items():
            owner = owners.get(str(mid)) or ""
            if owner == target or (not owner and only == target):
                subset[mid] = items
        return {"count": len(subset),
                "refs": sum(len(items or []) for items in subset.values()),
                "bytes": len(json.dumps(subset, ensure_ascii=False).encode("utf-8")),
                "sids": by_sid}
    return {"count": len(db), "refs": refs, "bytes": size, "sids": by_sid}


def media_db_clear(cutoff=None) -> int:
    """清转发配图索引，返回删掉的消息条数（用户 m21482）。

    cutoff 为 None 清全部；给了 cutoff 就只删写入时间早于它的条目。
    没有时间戳的条目在按天清理时保留，不拿「不知道」当「太旧」。
    """
    db = _media_db()
    seen = _media_seen()
    sids = _media_sids()
    removed = 0
    for mid in list(db.keys()):
        if cutoff is not None:
            ts = seen.get(mid)
            if not isinstance(ts, (int, float)):
                continue
            if float(ts) >= float(cutoff):
                continue
        _media_db_pop(mid, sids)
        removed += 1
    if removed:
        _media_db_save()
    return removed


def _media_db() -> dict:
    global _MEDIA_DB, _MEDIA_DB_LOADED, _MEDIA_DB_LOAD_FAILED
    if _MEDIA_DB_LOADED:
        return _MEDIA_DB
    path = _media_db_path()
    if path is None or not path.is_file():
        _MEDIA_DB_LOADED = True
        _MEDIA_DB_LOAD_FAILED = False
        return _MEDIA_DB
    try:
        import json
        blob = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # 坏文件：保留内存里已有的映射，也不标「已加载」，下次调用再试一次。
        _MEDIA_DB_LOAD_FAILED = True
        logger.info("[multi-decision] 转发配图索引读不出来 %s: %s",
                    type(exc).__name__, exc)
        return _MEDIA_DB
    if not isinstance(blob, dict):
        # 解析成对象才算「读到了」；list/null/数字是坏内容，同样按失败处理。
        _MEDIA_DB_LOAD_FAILED = True
        logger.info("[multi-decision] 转发配图索引不是对象，按读取失败处理")
        return _MEDIA_DB
    _MEDIA_DB = {str(k): [x for x in v if isinstance(x, dict)]
                 for k, v in blob.items() if isinstance(v, list)}
    # 旁挂表里可能残留已经不在索引里的 mid，读的时候顺手清掉，统计才对得上。
    sids = _media_sids()
    for mid in list(sids.keys()):
        if mid not in _MEDIA_DB:
            sids.pop(mid, None)
    _MEDIA_DB_LOADED = True
    _MEDIA_DB_LOAD_FAILED = False
    _media_db_trim()
    # 首次读到老索引时补的时间戳 / 按天数删掉的旧条目都要落盘，
    # 否则下次重启又会把同一批老条目读回来。
    _media_db_save()
    return _MEDIA_DB


def _media_db_backup_broken(path) -> bool:
    """坏文件第一次被覆盖前，原样另存一份；存不下就不写，别把旧映射盖掉。"""
    global _MEDIA_DB_BACKED_UP
    if _MEDIA_DB_BACKED_UP:
        return True
    import os
    import uuid
    bak = path.with_name(path.name + ".bad-" + uuid.uuid4().hex[:8])
    try:
        with open(bak, "xb") as fp:
            fp.write(path.read_bytes())
    except FileExistsError:
        _MEDIA_DB_BACKED_UP = True
        return True
    except Exception as exc:
        logger.info("[multi-decision] 坏索引另存失败，先不覆盖 %s: %s",
                    type(exc).__name__, exc)
        return False
    _MEDIA_DB_BACKED_UP = True
    return True


def _media_db_save() -> None:
    global _MEDIA_DB_LOAD_FAILED, _MEDIA_DB_BACKED_UP
    path = _media_db_path()
    if path is None:
        return
    import json
    import os
    import uuid
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _media_db_trim()
        if _MEDIA_DB_LOAD_FAILED and path.is_file():
            if not _media_db_backup_broken(path):
                return
        # 同目录唯一临时文件 + 替换：写一半断掉不会留下半份索引。
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(_MEDIA_DB, ensure_ascii=False))
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        tmp = None
        _MEDIA_DB_LOAD_FAILED = False
        _media_seen_save()
    except Exception as exc:
        logger.info("[multi-decision] 转发配图索引没写成 %s: %s", type(exc).__name__, exc)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _sniff_image_ext(blob: bytes) -> str:
    """按文件头认图片格式，认不出返回 ""（M09）。

    以前按 Content-Type 猜后缀：对方随便回个 image/jpeg 就能把网页存成 .jpg，
    真实 PNG 配错头又会存成 .jpg，前端按 mime 画图就画不出来。文件头是图片
    自己带的，不认服务器怎么说。
    """
    if not blob:
        return ""
    for magic, ext in ((b"\x89PNG\r\n\x1a\n", ".png"),
                       (b"\xff\xd8\xff", ".jpg"),
                       (b"GIF87a", ".gif"),
                       (b"GIF89a", ".gif")):
        if blob.startswith(magic):
            return ext
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    return ""


def _mime_of(ext: str) -> str:
    return {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/jpeg")


def _save_local_image(u: str, folder) -> dict:
    """转发图只有本地路径（file:// 或裸路径）时，直接复制进缓存。

    以前这里直接返回 {}，图就静默丢了：35 个 [图片] 只存下 9 张、日志里一条
    失败都没有，就是这条分支吃掉的。读不到才记一条日志。
    """
    import hashlib
    from pathlib import Path
    from urllib.parse import unquote

    raw = u[7:] if u.startswith("file://") else u
    try:
        raw = unquote(raw)
    except Exception:
        pass
    if not raw:
        logger.info("[multi-decision] 转发配图没有可用地址，跳过")
        return {}
    src = Path(raw)
    if not src.is_absolute():
        try:
            from core.utils.path_utils import get_data_path
            src = Path(str(get_data_path())) / raw
        except Exception:
            pass
    try:
        if not src.is_file():
            logger.info("[multi-decision] 转发配图本地路径读不到，跳过 %s", raw[:80])
            return {}
        size = src.stat().st_size
        if size <= 0 or size > _IMG_MAX:
            return {}
        blob = src.read_bytes()
    except Exception as exc:
        logger.info("[multi-decision] 转发配图本地路径读取失败 %s：%s", raw[:80], exc)
        return {}
    ext = _sniff_image_ext(blob)
    if not ext:
        logger.info("[multi-decision] 转发配图不是图片（按文件头判断）%s", raw[:80])
        return {}
    key = "fw_" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (key + ext)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
    except Exception as exc:
        logger.info("[multi-decision] 转发配图写入失败 %s：%s", raw[:80], exc)
        return {}
    return {"path": "plugin_data/%s/media_cache/%s%s" % (_PLUGIN_ID, key, ext),
            "mime": _mime_of(ext)}


def _save_image(url: str) -> dict:
    """把转发里的一张图存进插件缓存，返回 {path, mime}；取不到就返回 {}。"""
    u = str(url or "").strip()
    folder = _media_cache_dir()
    if folder is None:
        return {}
    if not (u.startswith("http://") or u.startswith("https://")):
        return _save_local_image(u, folder)
    import hashlib
    import urllib.request as _ur

    key = "fw_" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:24]
    for ext in (".jpg", ".png", ".gif", ".webp"):
        hit = folder / (key + ext)
        if hit.is_file():
            return {"path": "plugin_data/%s/media_cache/%s%s" % (_PLUGIN_ID, key, ext),
                    "mime": _mime_of(ext)}
    try:
        req = _ur.Request(u, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": "https://q.qq.com/",
        })
        with _ur.urlopen(req, timeout=6) as resp:
            blob = resp.read(_IMG_MAX + 1)
            ctype = str(resp.headers.get("Content-Type") or "").lower()
        if not blob or len(blob) > _IMG_MAX:
            return {}
        ext = _sniff_image_ext(blob)
        if not ext:
            logger.info("[multi-decision] 转发配图不是图片（按文件头判断）%s", u[:80])
            return {}
        folder.mkdir(parents=True, exist_ok=True)
        # D12（2026-11-13 交接文档）：直接写目标文件，进程中断会留下截断图，下次
        # is_file() 命中就当有效图复用。跟 _save_local_image 一样先写 tmp 再原子替换。
        target = folder / (key + ext)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
        return {"path": "plugin_data/%s/media_cache/%s%s" % (_PLUGIN_ID, key, ext),
                "mime": _mime_of(ext)}
    except Exception as exc:
        logger.info("[multi-decision] 转发配图没取到 %s：%s: %s", u[:80], type(exc).__name__, exc)
        return {}


def media_of(messages) -> list:
    """按转发条目的先后把图存下来，顺序与文本里的 [图片] 占位一一对应。"""
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        segs = msg.get("message")
        if not isinstance(segs, list):
            continue
        for seg in segs:
            if not isinstance(seg, dict) or str(seg.get("type") or "") != "image":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                data = {}
            got = _save_image(str(data.get("url") or data.get("file") or ""))
            if got:
                out.append(got)
    return out


def remember_media_by_mid(mid: str, refs: list, raw_sid: str = "") -> None:
    key = str(mid or "").strip()
    if not key or not refs:
        return
    _MID_MEDIA[key] = list(refs)
    _MID_MEDIA_ORDER.append(key)
    while len(_MID_MEDIA_ORDER) > _MID_MEDIA_MAX:
        old = _MID_MEDIA_ORDER.pop(0)
        if old != key:
            _MID_MEDIA.pop(old, None)
    db = _media_db()
    db[key] = list(refs)
    # v4.55（用户 m19940）：记下写入时间，按保留天数清理时要用。
    _media_seen()[key] = time.time()
    # v4.54（用户 m18227）：记下这条索引属于哪个群 / 私聊，缓存页才能按会话设上限。
    sid = str(raw_sid or "").strip()
    if sid:
        _media_sids()[key] = sid[:512]
    else:
        # 显式没有会话归属时别留上一次的旧归属，统计才不会串群。
        _media_sids().pop(key, None)
    _media_db_save()
    _media_sids_save()


def media_for_mid(mid: str) -> list:
    key = str(mid or "").strip()
    got = _MID_MEDIA.get(key)
    if got:
        return list(got)
    return list(_media_db().get(key) or [])


def media_for_text(text: str) -> list:
    """一条历史消息正文里的转发配图：按 [message_id: X] 反查。"""
    t = str(text or "")
    if "[Forward" not in t:
        return []
    hit = _LINE_ID_RE.search(t)
    return media_for_mid(hit.group(1)) if hit else []


def _remember(res_id: str, lines: list) -> None:
    if not res_id or not lines:
        return
    _CACHE[res_id] = list(lines)
    _CACHE_ORDER.append(res_id)
    while len(_CACHE_ORDER) > _CACHE_MAX:
        old = _CACHE_ORDER.pop(0)
        if old != res_id:
            _CACHE.pop(old, None)


def remember_by_mid(mid: str, lines: list) -> None:
    """按外层 message_id 记一份：框架历史里那行空壳要靠它换掉。"""
    key = str(mid or "").strip()
    if not key or not lines:
        return
    _MID_LINES[key] = list(lines)
    _MID_ORDER.append(key)
    while len(_MID_ORDER) > _MID_MAX:
        old = _MID_ORDER.pop(0)
        if old != key:
            _MID_LINES.pop(old, None)


def lines_for_mid(mid: str) -> list:
    return list(_MID_LINES.get(str(mid or "").strip()) or [])


def block_of(lines) -> str:
    """和框架 message_formatter 一致：条目之间空一行。"""
    return "[Forward " + "\n\n".join(str(x) for x in lines) + "]"


def patch_text(text: str) -> str:
    """把一段历史文本里的空壳转发换成真内容。

    只认「同一行既有 [message_id: X] 又有 [Forward]/[Forward message]」的行——
    框架的 user 内容是「[时间] [message_id: X] [user_nickname: …] | 正文」，
    转发正文就是那个空壳。没补到过内容的消息原样留着，不编。
    """
    if not text or "[Forward" not in text:
        return text
    out = []
    changed = False
    for line in text.split("\n"):
        if "[Forward" in line:
            hit = _LINE_ID_RE.search(line)
            lines = lines_for_mid(hit.group(1)) if hit else []
            if lines:
                block = block_of(lines)
                if "[Forward message]" in line:
                    line = line.replace("[Forward message]", block)
                elif "[Forward]" in line:
                    line = line.replace("[Forward]", block)
                changed = True
        out.append(line)
    return "\n".join(out) if changed else text


async def _call(bot, action: str, params: dict, timeout: float):
    try:
        res = await asyncio.wait_for(bot.send_action(action, params, timeout=timeout), timeout=timeout)
    except Exception as exc:
        logger.info("[multi-decision] 转发补取 %s 失败：%s: %s" % (action, type(exc).__name__, exc))
        return None
    if not isinstance(res, dict):
        return None
    if str(res.get("status") or "") == "failed":
        return None
    try:
        if int(res.get("retcode") or 0) != 0:
            return None
    except Exception:
        pass
    return res.get("data")


def _res_id_of(data) -> str:
    """从 get_msg 的返回里抠出转发 res_id：优先进段里的 data.id。"""
    if not isinstance(data, dict):
        return ""
    segs = data.get("message")
    if isinstance(segs, list):
        for seg in segs:
            if not isinstance(seg, dict) or str(seg.get("type") or "") != "forward":
                continue
            box = seg.get("data")
            if not isinstance(box, dict):
                box = {}
            rid = str(box.get("id") or box.get("res_id") or "").strip()
            if rid:
                return rid
    hit = _CQ_FWD_RE.search(str(data.get("raw_message") or ""))
    return hit.group(1) if hit else ""


def items_of(messages) -> list:
    """data.messages[] → [{"message_str": "[时间] 昵称: 正文"}, …]（engine 认 dict）。"""
    out = []
    for msg in messages or []:
        line = _line_of(msg)
        if line:
            out.append({"message_str": line})
    return out


async def _forward_raw(bot, res_id: str, timeout: float):
    """取转发的原始条目（图就在这里面）。NapCat 认 {`message_id`: res_id}，
    标准 OneBot 写的是 {"id": …}，两个都试。"""
    fwd = await _call(bot, "get_forward_msg", {"message_id": res_id}, timeout)
    if not isinstance(fwd, dict):
        fwd = await _call(bot, "get_forward_msg", {"id": res_id}, timeout)
    if not isinstance(fwd, dict):
        return None
    return fwd.get("messages") or fwd.get("message") or []


# Display-only evidence is deliberately separate from the legacy flat media cache.
# Readers never fetch, mutate either cache, or write. Only this fetch's raw nodes may
# establish slot ownership; old text/media entries are not provenance.
_DISPLAY_PROOF_MAX = 128
# v4.54（用户 m18227）：按会话设上限后，总条数可以超过老的全局 128；这里只留一个
# 防止配置写坏的安全阀。没注入回调时仍旧按全局 128 裁（测试也按这个口径）。
_DISPLAY_PROOF_HARD_MAX = 10000
_DISPLAY_PROOF_KEEP_FN = None
# v4.55（用户 m19940）：转发原图记录也按保留天数清理；老记录没有 saved_at
# 时按首次读到的时间算，给它们一个完整周期。
_DISPLAY_PROOF_DAYS_FN = None
_DISPLAY_NODE_MAX = 128
_DISPLAY_SLOT_MAX = 256
_DISPLAY_TEXT_MAX = 65536
_DISPLAY_FILE_MAX = 8 * 1024 * 1024
_DISPLAY_FAKE_SLOT_RE = re.compile(r"\[图片\]|\[image(?: attached)?\]|\[CQ:image[^\]]*\]", re.I)


def _display_proof_path():
    folder = _media_cache_dir()
    return folder.parent / "forward_display_proof.json" if folder is not None else None


def _display_key(raw_sid, mid):
    import json
    # Preserve the framework's raw sid, including its prefix; never invent one.
    if not isinstance(raw_sid, str) or not raw_sid.strip() or len(raw_sid) > 512:
        return None
    key = str(mid or "").strip()
    if not key or len(key) > 512:
        return None
    return json.dumps([raw_sid, key], ensure_ascii=False, separators=(",", ":"))


def _display_ref_ok(ref) -> bool:
    if not isinstance(ref, dict):
        return False
    path = ref.get("path")
    if not isinstance(path, str) or not path or len(path) > 2048:
        return False
    parts = path.replace("\\", "/").split("/")
    return (not path.startswith(("/", "\\")) and ":" not in path
            and not any(p in ("", ".", "..") for p in parts)
            and ref.get("mime") in ("image/png", "image/jpeg", "image/gif", "image/webp"))


def _display_proof_ok(proof, raw_sid, mid) -> bool:
    if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1:
        return False
    if proof.get("raw_sid") != raw_sid or proof.get("outer_mid") != str(mid or "").strip():
        return False
    rid, nodes = proof.get("res_id"), proof.get("nodes")
    if not isinstance(rid, str) or not rid or len(rid) > 2048:
        return False
    if not isinstance(nodes, list) or not 0 < len(nodes) <= _DISPLAY_NODE_MAX:
        return False
    lines, slots = [], 0
    for node in nodes:
        if not isinstance(node, dict):
            return False
        text, media = node.get("text"), node.get("media")
        if not isinstance(text, str) or not text or len(text) > _DISPLAY_TEXT_MAX:
            return False
        if not isinstance(media, list) or not all(_display_ref_ok(ref) for ref in media):
            return False
        if len(_DISPLAY_FAKE_SLOT_RE.findall(text)) != len(media) or text.count("[图片]") != len(media):
            return False
        lines.append(text)
        slots += len(media)
    body = proof.get("body")
    return (slots <= _DISPLAY_SLOT_MAX and isinstance(body, str)
            and len(body) <= _DISPLAY_TEXT_MAX and body == block_of(lines))


def _display_proofs_read():
    import json
    path = _display_proof_path()
    if path is None:
        return None
    try:
        if not path.exists():
            return {}
        if path.stat().st_size > _DISPLAY_FILE_MAX:
            return None
        blob = json.loads(path.read_text(encoding="utf-8"))
        cap = (_DISPLAY_PROOF_HARD_MAX if callable(_DISPLAY_PROOF_KEEP_FN)
               else _DISPLAY_PROOF_MAX)
        if not isinstance(blob, dict) or len(blob) > cap:
            return None
        return {key: value for key, value in blob.items()
                if isinstance(value, dict)
                and key == _display_key(value.get("raw_sid"), value.get("outer_mid"))
                and _display_proof_ok(value, value.get("raw_sid"), value.get("outer_mid"))}
    except Exception:
        return None


def set_display_proof_keep_fn(fn) -> None:
    """插件注入「这个会话留多少条原始凭证」；传 None 恢复全局 128。"""
    global _DISPLAY_PROOF_KEEP_FN
    _DISPLAY_PROOF_KEEP_FN = fn


def set_display_proof_keep_days_fn(fn) -> None:
    """插件注入「这个会话留多少天原始凭证」；传 None 不按天清理。"""
    global _DISPLAY_PROOF_DAYS_FN
    _DISPLAY_PROOF_DAYS_FN = fn


def _display_proof_trim(records: dict) -> None:
    days_fn = _DISPLAY_PROOF_DAYS_FN
    if callable(days_fn):
        now = time.time()
        for key in list(records.keys()):
            proof = records.get(key) or {}
            sid = str(proof.get("raw_sid") or "")
            try:
                days = int(days_fn(sid) or 0)
            except Exception:
                days = 0
            if days <= 0:
                continue
            ts = proof.get("saved_at")
            if not isinstance(ts, (int, float)):
                proof["saved_at"] = int(now)
                continue
            if now - float(ts) > days * 86400:
                records.pop(key, None)
    fn = _DISPLAY_PROOF_KEEP_FN
    if not callable(fn):
        while len(records) > _DISPLAY_PROOF_MAX:
            records.pop(next(iter(records)))
        return
    counts = {}
    for key in list(records.keys()):
        proof = records.get(key) or {}
        sid = str(proof.get("raw_sid") or "")
        try:
            limit = int(fn(sid) or 0)
        except Exception:
            limit = 0
        if limit <= 0:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        if counts[sid] > limit:
            records.pop(key, None)
    while len(records) > _DISPLAY_PROOF_HARD_MAX:
        records.pop(next(iter(records)))


def _display_proof_write(records: dict) -> bool:
    """原子写回凭证文件；写不下（超过 8MB）就保留旧文件，返回 False。"""
    import json
    import os
    import uuid
    path = _display_proof_path()
    if path is None:
        return False
    tmp = None
    try:
        blob = json.dumps(records, ensure_ascii=False).encode("utf-8")
        while len(blob) > _DISPLAY_FILE_MAX and len(records) > 1:
            records.pop(next(iter(records)))
            blob = json.dumps(records, ensure_ascii=False).encode("utf-8")
        if len(blob) > _DISPLAY_FILE_MAX:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        with open(tmp, "xb") as fp:
            fp.write(blob)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception as exc:
        logger.info("[multi-decision] 转发显示证据没写成 %s: %s",
                    type(exc).__name__, exc)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def display_proof_stats(sid: str = "") -> dict:
    """转发原图凭证的条数 / 磁盘占用，给缓存记录页用。

    sid 非空时只算这个会话的凭证（用户 m22090）；占用按这份子集序列化后的
    字节数算，不把整份混写凭证都记到它头上。
    """
    import json
    records = _display_proofs_read()
    if records is None:
        records = {}
    else:
        # 缓存页也算一次维护：读的时候顺手按天数 / 每会话上限清一遍，
        # 否则一个很久没新转发的群，旧凭证永远不会被写到触发清理。
        before = json.dumps(records, ensure_ascii=False, sort_keys=True)
        _display_proof_trim(records)
        if json.dumps(records, ensure_ascii=False, sort_keys=True) != before:
            _display_proof_write(records)
    by_sid = {}
    for proof in records.values():
        owner = _norm_sid(str(proof.get("raw_sid") or "")) if isinstance(proof, dict) else ""
        by_sid[owner] = by_sid.get(owner, 0) + 1
    target = _norm_sid(str(sid or "").strip())
    if target:
        subset = {key: proof for key, proof in records.items()
                  if isinstance(proof, dict)
                  and _norm_sid(str(proof.get("raw_sid") or "")) == target}
        return {"count": len(subset),
                "bytes": len(json.dumps(subset, ensure_ascii=False).encode("utf-8")),
                "sids": by_sid}
    size = 0
    try:
        path = _display_proof_path()
        if path is not None and path.is_file():
            size = int(path.stat().st_size or 0)
    except Exception:
        pass
    return {"count": len(records), "bytes": size, "sids": by_sid}


def display_proof_clear(cutoff=None) -> int:
    """清转发原图凭证，返回删掉的条数（用户 m21482）。

    cutoff 为 None 清全部；给了 cutoff 就只删 saved_at 早于它的条目。
    没有 saved_at 的条目在按天清理时保留。
    """
    records = _display_proofs_read()
    if not records:
        return 0
    removed = 0
    for key in list(records.keys()):
        if cutoff is not None:
            ts = (records.get(key) or {}).get("saved_at")
            if not isinstance(ts, (int, float)):
                continue
            if float(ts) >= float(cutoff):
                continue
        records.pop(key, None)
        removed += 1
    if removed:
        _display_proof_write(records)
    return removed


def _display_proof_denied(key, *, reject=False, clear=False) -> bool:
    """Keep bounded, path-scoped revocations across module reload/re-exec."""
    import os
    import sys
    import types
    path = _display_proof_path()
    if path is None:
        return True
    try:
        scope = os.path.normcase(str(path.resolve()))
    except Exception:
        return True
    name = "_kira_forward_display_proof_denials_v1"
    registry = sys.modules.get(name)
    if registry is None:
        registry = types.ModuleType(name)
        registry.paths, registry.count = {}, 0
        sys.modules[name] = registry
    bucket = registry.paths.get(scope)
    if bucket is None:
        # Never evict known revocations under pressure and accidentally revive them.
        if len(registry.paths) >= _DISPLAY_PROOF_MAX:
            return True
        if not reject:
            return False
        bucket = {"keys": set(), "closed": False}
        registry.paths[scope] = bucket
    if reject and key not in bucket["keys"]:
        if registry.count >= _DISPLAY_PROOF_MAX:
            bucket["closed"] = True
        else:
            bucket["keys"].add(key)
            registry.count += 1
    if clear and key in bucket["keys"]:
        bucket["keys"].remove(key)
        registry.count -= 1
    # A saturated path stays closed; a single successful write cannot reopen others.
    return bucket["closed"] or key in bucket["keys"]


def display_proof_for_mid(raw_sid, mid) -> dict | None:
    """Read independent v1 node/slot provenance; no I/O except bounded metadata read."""
    import copy
    key = _display_key(raw_sid, mid)
    if key is None or _display_proof_denied(key):
        return None
    got = (_display_proofs_read() or {}).get(key)
    return copy.deepcopy(got) if got is not None else None


def _display_proof_save(raw_sid, mid, proof) -> None:
    import json
    import os
    import uuid
    key, path = _display_key(raw_sid, mid), _display_proof_path()
    if key is None or path is None:
        return
    records = _display_proofs_read()
    # Never overwrite an unreadable file. A failed atomic replace leaves all old bytes.
    if records is None or (proof is not None and not _display_proof_ok(proof, raw_sid, mid)):
        return
    if proof is None and key not in records:
        return
    if proof is None:
        # A known-invalid old entry must not survive a failed atomic revocation.
        _display_proof_denied(key, reject=True)
    records.pop(key, None)
    if proof is not None:
        # v4.55（用户 m19940）：记下写入时间，按保留天数清理时要用。
        proof = dict(proof)
        proof["saved_at"] = int(time.time())
        records[key] = proof
    _display_proof_trim(records)
    if not _display_proof_write(records):
        return
    if proof is not None:
        _display_proof_denied(key, clear=True)


def _display_fetch_identity_ok(data, raw_sid, mid) -> bool:
    if _display_key(raw_sid, mid) is None or not isinstance(data, dict):
        return False
    if "message_id" in data and str(data["message_id"]) != mid:
        return False
    # Only compare explicit OneBot identities; absence is not a made-up identity.
    match = re.fullmatch(r"[^:]+:(gm|dm):([^:]+)", raw_sid)
    if not match:
        return False
    kind, peer = match.groups()
    if kind == "gm":
        if "group_id" in data and str(data["group_id"]) != peer:
            return False
        if "message_type" in data and str(data["message_type"]) != "group":
            return False
    else:
        if "user_id" in data and str(data["user_id"]) != peer:
            return False
        if "message_type" in data and str(data["message_type"]) != "private":
            return False
        sender = data.get("sender")
        if isinstance(sender, dict) and "user_id" in sender and str(sender["user_id"]) != peer:
            return False
    return True


def _display_proof_from_raw(raw_sid, mid, res_id, raw_nodes, refs):
    if _display_key(raw_sid, mid) is None or not isinstance(raw_nodes, list):
        return None
    if not 0 < len(raw_nodes) <= _DISPLAY_NODE_MAX or not isinstance(refs, list):
        return None
    nodes, counts = [], []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            return None
        segs = raw.get("message")
        if not isinstance(segs, list) or len(segs) > _DISPLAY_SLOT_MAX:
            return None
        count = 0
        for seg in segs:
            if not isinstance(seg, dict) or not isinstance(seg.get("data"), dict):
                return None
            kind, data = seg.get("type"), seg["data"]
            if kind == "image":
                url = data.get("url") or data.get("file")
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    return None
                count += 1
            elif kind not in ("text", "at", "face", "mface", "marketface", "video"):
                # Known video renders [视频] with zero image slots; never traverse its data/cover.
                # Reply/nested/unknown media cannot prove direct image ownership.
                return None
            elif _DISPLAY_FAKE_SLOT_RE.search(_seg_text(seg)):
                return None
        line = _line_of(raw)
        nodes.append({"text": line, "media": []})
        counts.append(count)
    # media_of omits failed downloads: equality proves that every direct slot succeeded.
    # Keep occurrences, not unique paths (A/B/A must remain A/B/A).
    if sum(counts) != len(refs) or sum(counts) > _DISPLAY_SLOT_MAX:
        return None
    offset = 0
    for node, count in zip(nodes, counts):
        node["media"] = refs[offset:offset + count]
        offset += count
    lines = [item["message_str"] for item in items_of(raw_nodes)]
    proof = {"version": 1, "raw_sid": raw_sid, "outer_mid": mid, "res_id": res_id,
             "body": block_of(lines), "nodes": nodes}
    return proof if _display_proof_ok(proof, raw_sid, mid) else None


async def fetch_forward_items(bot, message_id: str, timeout: float = 5.0, *,
                              raw_sid=None, refresh_display_proof=False) -> list:
    """按外层消息 id 反查转发内容；显式刷新只补显示证据，不改旧文字/媒体索引。"""
    mid = str(message_id or "").strip()
    if not mid or bot is None:
        return []
    if refresh_display_proof is True:
        # Display-only repair: legacy text/flat media are neither provenance nor write targets.
        if not _display_fetch_identity_ok({}, raw_sid, mid):
            return []
        existing = display_proof_for_mid(raw_sid, mid)
        if existing is not None:
            return list(_CACHE.get(existing["res_id"], []))
        data = await _call(bot, "get_msg", {"message_id": mid}, timeout)
        if not isinstance(data, dict) and mid.lstrip("-").isdigit():
            data = await _call(bot, "get_msg", {"message_id": int(mid)}, timeout)
        res_id = _res_id_of(data)
        cached = list(_CACHE.get(res_id, []))
        if not res_id or not _display_fetch_identity_ok(data, raw_sid, mid):
            return cached
        raw_nodes = await _forward_raw(bot, res_id, timeout)
        if raw_nodes is None:
            return cached
        lines = cached if res_id in _CACHE else items_of(raw_nodes)
        try:
            refs = await asyncio.to_thread(media_of, raw_nodes)
        except Exception:
            return lines
        proof = _display_proof_from_raw(raw_sid, mid, res_id, raw_nodes, refs)
        if proof is not None:
            _display_proof_save(raw_sid, mid, proof)
        return lines
    data = await _call(bot, "get_msg", {"message_id": mid}, timeout)
    if not isinstance(data, dict) and mid.lstrip("-").isdigit():
        # 少数实现只认 int
        data = await _call(bot, "get_msg", {"message_id": int(mid)}, timeout)
    res_id = _res_id_of(data)
    if not res_id:
        return []
    identity_ok = _display_fetch_identity_ok(data, raw_sid, mid)
    existing = display_proof_for_mid(raw_sid, mid)
    if existing is not None and (not identity_ok or existing["res_id"] != res_id):
        _display_proof_save(raw_sid, mid, None)
    if res_id in _CACHE:
        # 文字命中了缓存，图却可能还没存过（早先只有取文字的那版代码）——补存一次。
        if not media_for_mid(mid):
            raw_again = await _forward_raw(bot, res_id, timeout)
            try:
                refs_again = await asyncio.to_thread(media_of, raw_again or [])
            except Exception:
                refs_again = []
            if refs_again:
                remember_media_by_mid(mid, refs_again, raw_sid)
            if _display_key(raw_sid, mid) is not None:
                proof = (_display_proof_from_raw(raw_sid, mid, res_id, raw_again, refs_again)
                         if identity_ok else None)
                _display_proof_save(raw_sid, mid, proof)
        return list(_CACHE[res_id])
    raw_msgs = await _forward_raw(bot, res_id, timeout)
    if raw_msgs is None:
        return []
    lines = items_of(raw_msgs)
    _remember(res_id, lines)
    try:
        refs = await asyncio.to_thread(media_of, raw_msgs)
    except Exception:
        refs = []
    if refs:
        remember_media_by_mid(mid, refs, raw_sid)
    if _display_key(raw_sid, mid) is not None:
        proof = (_display_proof_from_raw(raw_sid, mid, res_id, raw_msgs, refs)
                 if identity_ok else None)
        _display_proof_save(raw_sid, mid, proof)
    return lines
