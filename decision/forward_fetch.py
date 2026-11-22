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
# 用户 m10466：图片和正文合成一份 forward_cache.json，落盘状态在下面 _FORWARD_CACHE* 里。
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
#: 回收孤儿转发图时防重入：GC 里会读索引，读索引可能触发保存，保存又会调 GC。
_MEDIA_GC_RUNNING = False


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
    """删掉一条转发缓存（图片和正文一起走，用户 m10466）。"""
    key = str(mid or "").strip()
    if not key:
        return
    records = _forward_cache_read()
    if records is None:
        return
    hit = _FORWARD_MID_INDEX.get(key)
    if hit is not None:
        records.pop(hit, None)
        _FORWARD_MID_INDEX.pop(key, None)


def _media_db_trim() -> int:
    """图片索引不再单独裁：条数上限、保留天数都跟着转发缓存一起裁（用户 m10466）。"""
    return 0


def set_media_db_limit_fn(fn) -> None:
    """插件注入「这个会话留多少条」的回调；传 None 恢复老的全局限额。

    2026-11-18 审查 H01/128：真正裁条数的是 _forward_cache_trim，它读的是
    _DISPLAY_PROOF_KEEP_FN。以前这里只写 _MEDIA_KEEP_FN，全插件没有读取点，
    main.py 注入的每会话条数永远不生效，恒按全局 128 裁。现在两个一起写。
    """
    global _MEDIA_KEEP_FN, _DISPLAY_PROOF_KEEP_FN
    _MEDIA_KEEP_FN = fn
    _DISPLAY_PROOF_KEEP_FN = fn

def set_media_db_keep_days_fn(fn) -> None:
    """插件注入「这个会话留多少天」的回调；传 None 不按天清理。

    2026-11-18 审查 H01/128：天数同理，_forward_cache_trim 读的是
    _DISPLAY_PROOF_DAYS_FN，以前写进 _MEDIA_KEEP_DAYS_FN 也没人读。
    """
    global _MEDIA_KEEP_DAYS_FN, _DISPLAY_PROOF_DAYS_FN
    _MEDIA_KEEP_DAYS_FN = fn
    _DISPLAY_PROOF_DAYS_FN = fn


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


def _media_images_size(items, folder) -> tuple:
    """这批引用在磁盘上还剩几张图、一共多少字节（用户 m14353）。

    索引里记的是 plugin_data/<插件>/media_cache/<文件名>，文件可能已经被别处删掉，
    所以按名字去 media_cache 里 stat 一遍，数不到的跳过。
    """
    count = 0
    size = 0
    if folder is None:
        return count, size
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name:
            continue
        target = folder / name
        try:
            if not target.is_file():
                continue
            size += int(target.stat().st_size or 0)
        except OSError:
            continue
        count += 1
    return count, size


def media_db_stats(sid: str = "") -> dict:
    """转发缓存的条数 / 图片数 / 磁盘占用，给缓存记录页用。

    sid 非空时只算这个会话的条目（用户 m22090）；占用按这份子集序列化后的
    字节数算，不把整份混写缓存都记到它头上。
    老数据没有会话归属时先按原图凭证的 outer_mid / 图片路径反查；如果所有已知归属
    都指向同一个会话，就把仍无归属的旧条目算给这个会话，多个会话时不猜。

    用户 m14353：再报 imgs / img_bytes——缓存里那几张图在磁盘上实际还剩多少。
    缓存记录页的「占用」改用这个数，以前报的是索引文件自己的字节数（82 KB），
    1,163 MB 的转发图在账本上完全看不见。
    用户 m10466：图片和原图记录合成一份，这里报的就是合并后的整份转发缓存。
    """
    import json
    records = _forward_cache_read() or {}
    folder = _media_cache_dir()
    target = _norm_sid(str(sid or "").strip())
    legacy = _legacy_media_owners() if target else {}
    by_sid = {}
    owners = {}
    for key, rec in records.items():
        mid = str(rec.get("outer_mid") or "")
        owner = _norm_sid(str(rec.get("raw_sid") or ""))
        if not owner:
            owner = legacy.get(mid) or ""
            if not owner:
                for item in (rec.get("media") or []):
                    if not isinstance(item, dict):
                        continue
                    owner = legacy.get(str(item.get("path") or "").strip()) or ""
                    if owner:
                        break
        owners[key] = owner
        row = by_sid.setdefault(owner, {"count": 0, "refs": 0})
        row["count"] += 1
        row["refs"] += len(rec.get("media") or [])
    size = 0
    path = _forward_cache_path()
    try:
        if path is not None and path.is_file():
            size = int(path.stat().st_size or 0)
    except Exception:
        pass
    if target:
        known = {owner for owner in owners.values() if owner}
        only = next(iter(known)) if len(known) == 1 else ""
        subset = {}
        for key, rec in records.items():
            owner = owners.get(key) or ""
            if owner == target or (not owner and only == target):
                subset[key] = rec
        imgs, img_bytes = _media_images_size(
            [item for rec in subset.values() for item in (rec.get("media") or [])], folder)
        return {"count": len(subset),
                "refs": sum(len(rec.get("media") or []) for rec in subset.values()),
                "bytes": len(json.dumps(subset, ensure_ascii=False).encode("utf-8")),
                "imgs": imgs,
                "img_bytes": img_bytes,
                "sids": by_sid}
    imgs, img_bytes = _media_images_size(
        [item for rec in records.values() for item in (rec.get("media") or [])], folder)
    return {"count": len(records),
            "refs": sum(len(rec.get("media") or []) for rec in records.values()),
            "bytes": size,
            "imgs": imgs, "img_bytes": img_bytes, "sids": by_sid}


def media_db_clear(cutoff=None) -> int:
    """清转发缓存（图片和正文一起），返回删掉的条数（用户 m21482 / m10466）。

    cutoff 为 None 清全部；给了 cutoff 就只删写入时间早于它的条目。
    没有时间戳的条目在按天清理时保留，不拿「不知道」当「太旧」。
    """
    records = _forward_cache_read()
    if not records:
        return 0
    gone = {}
    for key in list(records.keys()):
        if cutoff is not None:
            ts = (records.get(key) or {}).get("saved_at")
            if not isinstance(ts, (int, float)):
                continue
            if float(ts) >= float(cutoff):
                continue
        gone[key] = records.pop(key, None)
    removed = len(gone)
    if not removed:
        return 0
    # 2026-11-18 审查 H05：先确认索引真的落盘，再回收图片。以前不看
    # _forward_cache_write() 的返回值：写盘失败也照样返回 removed 并 gc，可盘上的
    # 记录还引用着这些图，gc 会把它们删成死链，前端只剩裂图。
    if not _forward_cache_write():
        records.update(gone)
        logger.info("[multi-decision] 转发缓存写盘失败，本次清理不生效（%d 条）" % removed)
        return 0
    for key, rec in gone.items():
        mid = str((rec or {}).get("outer_mid") or "") if isinstance(rec, dict) else ""
        if not mid:
            continue
        # 热表按 (会话, 消息号) 存：清掉的那条要连热表一起逐出，否则 media_for_mid
        # 还能把已经删掉的图交出去（审查 H05 的第二条路径）。
        for hot_key in list(_MID_MEDIA.keys()):
            if isinstance(hot_key, tuple) and len(hot_key) == 2 and hot_key[1] == mid:
                _MID_MEDIA.pop(hot_key, None)
                try:
                    _MID_MEDIA_ORDER.remove(hot_key)
                except ValueError:
                    pass
    media_db_gc()
    return removed


def media_db_gc(grace: float = 600.0) -> int:
    """删掉没人引用的转发图文件，返回删掉的张数（用户 m14353）。

    以前裁索引只 pop 条目、不删文件：`_prune_media_cache` 明确跳过 fw_，
    `media_db_clear` 也只清索引，于是每裁一次就留一批孤儿——实测 1,716 张转发图里
    有 1,077 张（约 720 MB）没人引用，从 10-30 一直堆在 media_cache 里。

    谁算「有人引用」：合并后的转发缓存（图片和正文同一条）、进程内最近 64 条缓存。
    两处都没有的 fw_ 文件才是孤儿。grace 是给刚写进来、索引还没落盘的图留的
    宽限（秒），默认 10 分钟。
    宽限（秒），默认 10 分钟。
    """
    global _MEDIA_GC_RUNNING
    if _MEDIA_GC_RUNNING:
        return 0
    folder = _media_cache_dir()
    if folder is None:
        return 0
    _MEDIA_GC_RUNNING = True
    try:
        # 2026-11-18 审查 H01：索引读不出来（_media_db() 返回 None）时绝不能当空表。
        # 以前 `or {}` 把读失败折成空，启动时 _MID_MEDIA 也是空的，于是所有超过
        # grace 的 fw_* 都被当孤儿 unlink——一次读失败就把整库转发图删光。
        index = _media_db()
        if index is None or _FORWARD_CACHE_LOAD_FAILED:
            logger.info("[multi-decision] 转发缓存索引读不出来，本轮不回收转发图")
            return 0
        keep = set()
        # 用户 m10466：图片和正文现在是同一条记录，_media_db() 已经把整份缓存的图都列出来了。
        groups = list(index.values()) + list(_MID_MEDIA.values())
        for items in groups:
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
                if name:
                    keep.add(name)
        try:
            names = [p.name for p in folder.glob("fw_*")]
        except Exception:
            return 0
        now = time.time()
        removed = 0
        for name in names:
            if name in keep:
                continue
            target = folder / name
            try:
                if grace > 0 and now - float(target.stat().st_mtime) < grace:
                    continue
            except OSError:
                continue
            try:
                target.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            logger.info("[multi-decision] 回收没人引用的转发图 %d 张", removed)
        return removed
    finally:
        _MEDIA_GC_RUNNING = False


def _media_db():
    """转发配图索引：从合并后的转发缓存里派生（mid → 图片列表）。

    用户 m10466：以前这是一份独立的 forward_media.json，跟转发原图记录各裁各的，
    两边条数永远对不上。现在图片就存在转发缓存记录的 media 字段里，这里只是视图。
    读不出来（_forward_cache_read() 返回 None）时这里也返回 None：调用方必须
    把它当「不知道」，不能当成空表——media_db_gc 拿空表就会把整库图删光。
    """
    records = _forward_cache_read()
    if records is None:
        return None
    out = {}
    for rec in records.values():
        media = rec.get("media") or []
        if media:
            out[str(rec.get("outer_mid") or "")] = list(media)
    return out




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


def _forward_records_of(records: dict, mid: str) -> list:
    """同一个外层消息号在缓存里的所有记录（可能分属不同会话）。"""
    key = str(mid or "").strip()
    if not records or not key:
        return []
    return [rec for rec in records.values()
            if isinstance(rec, dict) and str(rec.get("outer_mid") or "") == key]


def _forward_record_of(records: dict, raw_sid: str, mid: str):
    """按会话归属挑一条转发记录；归属拿不准就返回 None，绝不跨会话借。

    2026-11-18 审查 H03：_FORWARD_MID_INDEX 是 mid → 单条键，两个会话同号会被
    折成一条，所以取记录不能只看它。
    """
    key = str(mid or "").strip()
    sid = str(raw_sid or "").strip()[:512]
    if not records or not key:
        return None
    if sid:
        direct = records.get(_cache_key(sid, key))
        if isinstance(direct, dict):
            return direct
    hits = _forward_records_of(records, key)
    if sid:
        mine = [rec for rec in hits if str(rec.get("raw_sid") or "") == sid]
        if mine:
            return mine[0]
        # 用户 m09904：插件自己的 raw_sid 映射表只在 collect 时才填，热重载后是空的，
        # 这时调用方传进来的是短 sid（gm:xxx），记录里存的却是完整 sid（bot:gm:xxx）。
        # 按「完整 sid 以 :短 sid 结尾」再认一次；认出不止一条就不猜，宁可少图不串图。
        # 短 sid 是 gm_1145141974 这种（下划线），记录里是 樱井桃华:gm:1145141974，
        # 两种写法都试一遍。
        tails = {":" + sid}
        alt = sid.replace("_", ":", 1)
        if alt != sid:
            tails.add(":" + alt)
        near = [rec for rec in hits
                if any(str(rec.get("raw_sid") or "").endswith(t) for t in tails)]
        if len(near) == 1:
            return near[0]
        if near:
            return None
        legacy = [rec for rec in hits if not str(rec.get("raw_sid") or "")]
        return legacy[0] if len(legacy) == 1 else None
    if len(hits) == 1:
        return hits[0]
    if hits and len({str(rec.get("raw_sid") or "") for rec in hits}) == 1:
        return hits[0]
    return None


def remember_media_by_mid(mid: str, refs: list, raw_sid: str = "") -> None:
    """记下一条转发消息的配图（用户 m10466：直接写进合并后的转发缓存）。"""
    key = str(mid or "").strip()
    if not key or not refs:
        return
    sid = str(raw_sid or "").strip()[:512]
    # 2026-11-18 审查 H03：热表键带上会话归属，两个会话的同号消息各占一格。
    hot = (sid, key)
    _MID_MEDIA[hot] = list(refs)
    try:
        _MID_MEDIA_ORDER.remove(hot)
    except ValueError:
        pass
    _MID_MEDIA_ORDER.append(hot)
    while len(_MID_MEDIA_ORDER) > _MID_MEDIA_MAX:
        old = _MID_MEDIA_ORDER.pop(0)
        if old != hot:
            _MID_MEDIA.pop(old, None)
    records = _forward_cache_read()
    if records is None:
        return
    rec = _forward_record_of(records, sid, key)
    if isinstance(rec, dict) and rec.get("nodes"):
        # 已经有正文的记录：图片由 _display_proof_save 连正文一起写。但图片文件本身
        # 可能被清掉（清图片缓存会把 media_cache 整个删掉），重新取回来的 refs 得能
        # 补回去——数量对得上就按原槽位换一遍，对不上就不动，免得破坏
        # 「扁平 media == 各节点 media 拼接」那条不变量。
        old = list(rec.get("media") or [])
        if len(old) != len(refs):
            return
        rec["media"] = list(refs)
        rec["saved_at"] = int(time.time())
        offset = 0
        for node in rec.get("nodes") or []:
            count = len(node.get("media") or [])
            node["media"] = list(refs[offset:offset + count])
            offset += count
        _forward_cache_write()
        return
    if isinstance(rec, dict):
        rec["media"] = list(refs)
        rec["saved_at"] = int(time.time())
        if sid and rec.get("raw_sid") != sid:
            # 键里带着会话归属，换了归属就得换键，否则下次读会被当成脏数据丢掉。
            # 2026-11-18 审查 H03：按记录对象本身找键，别信 mid 索引（它跨会话折叠）。
            for old_key, old_rec in list(records.items()):
                if old_rec is rec:
                    records.pop(old_key, None)
                    break
            rec["raw_sid"] = sid
            records[_cache_key(sid, key)] = rec
    else:
        records[_cache_key(sid, key)] = {
            "version": 2, "raw_sid": sid, "outer_mid": key, "res_id": "",
            "body": "", "nodes": [], "media": list(refs),
            "saved_at": int(time.time())}
    _forward_cache_write()


def _media_ref_present(ref) -> bool:
    """这条配图在磁盘上还在不在。清图片缓存会把 media_cache 整个目录删掉，
    记录里的路径就成了死链。取不到数据目录时按「在」处理，别把图藏起来。"""
    folder = _media_cache_dir()
    if folder is None:
        return True
    try:
        name = str((ref or {}).get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        return bool(name) and (folder / name).is_file()
    except Exception:
        return True


def media_for_mid(mid: str, raw_sid: str = "") -> list:
    key = str(mid or "").strip()
    sid = str(raw_sid or "").strip()[:512]
    got = _MID_MEDIA.get((sid, key))
    if not got:
        # 2026-11-18 审查 H03：先按会话找自己的记录；没给会话又撞号就不猜。
        rec = _forward_record_of(_forward_cache_read() or {}, sid, key)
        got = (rec or {}).get("media") or []
    # 用户 m10466：死链不交出去——前端只会显示裂图，而且 fetch_forward_items
    # 看到「有图」就不会重新取，图就永远补不回来了。
    return [ref for ref in got if _media_ref_present(ref)]


def media_for_text(text: str, raw_sid: str = "") -> list:
    """一条历史消息正文里的转发配图：按 [message_id: X] 反查。"""
    t = str(text or "")
    if "[Forward" not in t:
        return []
    hit = _LINE_ID_RE.search(t)
    return media_for_mid(hit.group(1), raw_sid) if hit else []


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


# ---------------------------------------------------------------------------
# 转发缓存（用户 m10466）：以前「转发图片」和「转发原图记录」是两份文件、两套上限、
# 两个清理按钮，可它们本来就是同一条转发的两半——图片列表和卡片正文。两边各裁各的，
# 账上就会出现「记录 74 条、图片 105 条」这种对不上的数。现在合成一份
# forward_cache.json：一条记录同时带正文（nodes / body / res_id）和图片（media），
# 上限、保留天数、清理都只有一套。只有图片、没有正文的老条目存成 media-only 记录。
# ---------------------------------------------------------------------------
_FORWARD_CACHE: dict = {}
_FORWARD_CACHE_LOADED = False
_FORWARD_CACHE_LOAD_FAILED = False
_FORWARD_MID_INDEX: dict = {}
_FORWARD_CACHE_BAK = ".bak-m10466"


def _forward_cache_path():
    folder = _media_cache_dir()
    return folder.parent / "forward_cache.json" if folder is not None else None


def _legacy_proof_path():
    folder = _media_cache_dir()
    return folder.parent / "forward_display_proof.json" if folder is not None else None


def _cache_key(raw_sid, mid):
    """合并后的键：会话 + 外层消息 id。跟旧凭证不同，这里允许空会话（老数据没有归属）。"""
    import json
    sid = raw_sid if isinstance(raw_sid, str) else ""
    if len(sid) > 512:
        return None
    key = str(mid or "").strip()
    if not key or len(key) > 512:
        return None
    return json.dumps([sid, key], ensure_ascii=False, separators=(",", ":"))


def _cache_record_ok(rec) -> bool:
    """一条转发缓存记录：要么是完整凭证（有正文），要么是只有图片的老条目。"""
    if not isinstance(rec, dict) or type(rec.get("version")) is not int or rec["version"] != 2:
        return False
    sid, mid = rec.get("raw_sid"), rec.get("outer_mid")
    if not isinstance(sid, str) or len(sid) > 512:
        return False
    if not isinstance(mid, str) or not mid or len(mid) > 512:
        return False
    media = rec.get("media")
    if not isinstance(media, list) or len(media) > _DISPLAY_SLOT_MAX:
        return False
    if not all(_display_ref_ok(ref) for ref in media):
        return False
    nodes = rec.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > _DISPLAY_NODE_MAX:
        return False
    if not nodes:
        # 只有图片的老条目：没有正文、没有 res_id，但必须真的有图。
        return bool(media) and rec.get("body") == "" and rec.get("res_id") == ""
    rid = rec.get("res_id")
    if not isinstance(rid, str) or not rid or len(rid) > 2048:
        return False
    lines, slots = [], 0
    for node in nodes:
        if not isinstance(node, dict):
            return False
        text, node_media = node.get("text"), node.get("media")
        if not isinstance(text, str) or not text or len(text) > _DISPLAY_TEXT_MAX:
            return False
        if not isinstance(node_media, list) or not all(_display_ref_ok(ref) for ref in node_media):
            return False
        if (len(_DISPLAY_FAKE_SLOT_RE.findall(text)) != len(node_media)
                or text.count("[图片]") != len(node_media)):
            return False
        lines.append(text)
        slots += len(node_media)
    body = rec.get("body")
    if not (slots <= _DISPLAY_SLOT_MAX and isinstance(body, str)
            and len(body) <= _DISPLAY_TEXT_MAX and body == block_of(lines)):
        return False
    # 扁平 media 必须正好是各节点图片按顺序拼起来的那份，两边不能各说各话。
    return [ref for node in nodes for ref in node["media"]] == media


def _forward_mid_index_of(records: dict) -> dict:
    """mid → 记录键。同一个 mid 有多条时优先带会话归属的那条。"""
    out = {}
    for key, rec in records.items():
        mid = str(rec.get("outer_mid") or "")
        if not mid:
            continue
        old = out.get(mid)
        if old is None or (not (records.get(old) or {}).get("raw_sid") and rec.get("raw_sid")):
            out[mid] = key
    return out


def _forward_cache_write_to(path, records) -> bool:
    """原子写一份转发缓存；超过 8MB 就先丢最旧的，还超就放弃（保留旧文件）。"""
    import json
    import os
    import uuid
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
        logger.info("[multi-decision] 转发缓存没写成 %s: %s", type(exc).__name__, exc)
        return False
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _forward_cache_migrate(path):
    """第一次跑新版本时把旧的两份文件合成一份；旧文件改名留底，不删。"""
    import json
    records = {}
    now = int(time.time())
    old_proof = _legacy_proof_path()
    if old_proof is not None and old_proof.is_file():
        try:
            blob = json.loads(old_proof.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.info("[multi-decision] 旧转发凭证读不出来，先不迁移 %s: %s",
                        type(exc).__name__, exc)
            return None
        if not isinstance(blob, dict):
            return None
        for proof in blob.values():
            if not isinstance(proof, dict):
                continue
            if not _display_proof_ok(proof, proof.get("raw_sid"), proof.get("outer_mid")):
                continue
            media = [ref for node in (proof.get("nodes") or [])
                     for ref in (node.get("media") or [])]
            rec = {"version": 2, "raw_sid": proof["raw_sid"],
                   "outer_mid": proof["outer_mid"], "res_id": proof["res_id"],
                   "body": proof["body"], "nodes": proof["nodes"], "media": media,
                   "saved_at": int(proof.get("saved_at") or now)}
            key = _cache_key(rec["raw_sid"], rec["outer_mid"])
            if key is not None:
                records[key] = rec
    old_media = _media_db_path()
    if old_media is not None and old_media.is_file():
        try:
            blob = json.loads(old_media.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.info("[multi-decision] 旧转发配图索引读不出来，先不迁移 %s: %s",
                        type(exc).__name__, exc)
            return None
        if not isinstance(blob, dict):
            return None
        sids, seen = _media_sids(), _media_seen()
        covered = {str(rec.get("outer_mid") or "") for rec in records.values()}
        for mid, items in blob.items():
            mid = str(mid)
            if mid in covered or not isinstance(items, list):
                continue
            refs = [ref for ref in items if _display_ref_ok(ref)]
            if not refs:
                continue
            sid = str(sids.get(mid) or "").strip()[:512]
            key = _cache_key(sid, mid)
            if key is None:
                continue
            ts = seen.get(mid)
            records[key] = {"version": 2, "raw_sid": sid, "outer_mid": mid,
                            "res_id": "", "body": "", "nodes": [], "media": refs,
                            "saved_at": int(ts) if isinstance(ts, (int, float)) else now}
    # 用户 m10466：按写入时间排一遍——裁剪是倒着数（留最新的），迁移时凭证在前、
    # 只有图片的老条目在后，不排的话「最新」会按文件顺序算错。
    records = dict(sorted(records.items(), key=lambda kv: int(kv[1].get("saved_at") or 0)))
    if not records:
        return {}
    if not _forward_cache_write_to(path, records):
        return None
    for old in (_legacy_proof_path(), _media_db_path(),
                _media_sids_path(), _media_seen_path()):
        try:
            if old is not None and old.is_file():
                old.replace(old.with_name(old.name + _FORWARD_CACHE_BAK))
        except Exception:
            pass
    logger.info("[multi-decision] 转发缓存已合并：%d 条（旧凭证 + 旧配图索引）", len(records))
    return records


def _forward_cache_read():
    """读合并后的转发缓存；读不出来返回 None（调用方据此拒绝覆盖）。"""
    global _FORWARD_CACHE, _FORWARD_CACHE_LOADED, _FORWARD_CACHE_LOAD_FAILED, _FORWARD_MID_INDEX
    if _FORWARD_CACHE_LOADED:
        return _FORWARD_CACHE
    import json
    path = _forward_cache_path()
    if path is None:
        _FORWARD_CACHE_LOAD_FAILED = True
        return None
    if not path.exists():
        migrated = _forward_cache_migrate(path)
        if migrated is None:
            _FORWARD_CACHE_LOAD_FAILED = True
            return None
        _FORWARD_CACHE = migrated
        _FORWARD_CACHE_LOADED = True
        _FORWARD_CACHE_LOAD_FAILED = False
        _FORWARD_MID_INDEX = _forward_mid_index_of(migrated)
        return _FORWARD_CACHE
    try:
        if path.stat().st_size > _DISPLAY_FILE_MAX:
            _FORWARD_CACHE_LOAD_FAILED = True
            logger.info("[multi-decision] 转发缓存超过 %d 字节，按读不出来处理",
                        _DISPLAY_FILE_MAX)
            return None
        blob = json.loads(path.read_text(encoding="utf-8"))
        # 2026-11-18 审查 H01：读的时候只按硬上限校验。128 是「没注入回调时」的
        # 裁剪目标，不是文件合法性标准——按会话设了更大的上限后文件自然超过 128，
        # 以前这里直接判成脏数据返回 None，接着 media_db_gc 把整库图当孤儿清掉。
        if not isinstance(blob, dict) or len(blob) > _DISPLAY_PROOF_HARD_MAX:
            _FORWARD_CACHE_LOAD_FAILED = True
            return None
        records = {key: value for key, value in blob.items()
                   if isinstance(value, dict)
                   and key == _cache_key(value.get("raw_sid"), value.get("outer_mid"))
                   and _cache_record_ok(value)}
    except Exception as exc:
        _FORWARD_CACHE_LOAD_FAILED = True
        logger.info("[multi-decision] 转发缓存读不出来 %s: %s", type(exc).__name__, exc)
        return None
    _FORWARD_CACHE = records
    _FORWARD_CACHE_LOADED = True
    _FORWARD_CACHE_LOAD_FAILED = False
    _FORWARD_MID_INDEX = _forward_mid_index_of(records)
    return _FORWARD_CACHE


def _forward_cache_trim(records: dict) -> int:
    """按保留天数 / 每会话上限裁合并后的转发缓存，返回删掉的条数。"""
    removed = 0
    days_fn = _DISPLAY_PROOF_DAYS_FN
    if callable(days_fn):
        now = time.time()
        for key in list(records.keys()):
            rec = records.get(key) or {}
            sid = str(rec.get("raw_sid") or "")
            try:
                days = int(days_fn(sid) or 0)
            except Exception:
                days = 0
            if days <= 0:
                continue
            ts = rec.get("saved_at")
            if not isinstance(ts, (int, float)):
                rec["saved_at"] = int(now)
                continue
            if now - float(ts) > days * 86400:
                records.pop(key, None)
                removed += 1
    fn = _DISPLAY_PROOF_KEEP_FN
    if not callable(fn):
        while len(records) > _DISPLAY_PROOF_MAX:
            records.pop(next(iter(records)))
            removed += 1
        return removed
    counts = {}
    # 用户 m10201：倒着数——记录是按写入顺序排的（每次保存先 pop 再插到末尾），
    # 正着数会把最新那几条裁掉、留下最老的，最近转发的卡片反而取不到内容。
    for key in reversed(list(records.keys())):
        rec = records.get(key) or {}
        sid = str(rec.get("raw_sid") or "")
        try:
            limit = int(fn(sid) or 0)
        except Exception:
            limit = 0
        if limit <= 0:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        if counts[sid] > limit:
            records.pop(key, None)
            removed += 1
    while len(records) > _DISPLAY_PROOF_HARD_MAX:
        records.pop(next(iter(records)))
        removed += 1
    return removed


def _forward_cache_write() -> bool:
    """把内存里的合并缓存写回磁盘；裁掉条目后顺手回收没人引用的图。

    2026-11-18 审查 M05：去重和裁剪以前直接在 _FORWARD_CACHE 本体上做
    （_forward_cache_read 返回的就是那份 dict），写盘失败时内存已经少了条目；
    _forward_cache_write_to 也会先 pop 一条再写，磁盘上的记录跟着丢。
    现在整轮都在副本上做，写盘成功了才整体换掉内存里那份。
    """
    global _FORWARD_CACHE, _FORWARD_MID_INDEX
    live = _forward_cache_read()
    if live is None:
        return False
    records = {key: dict(value) for key, value in live.items() if isinstance(value, dict)}
    # 同一个 mid 只留一条（优先带会话归属的那条），否则图片和正文会各存一份。
    # 2026-11-18 审查 H03：只有「同一个会话」或「其中一条没有会话归属（老数据）」
    # 才算同一条消息的重复；两个不同会话的同号消息各留各的。
    seen = {}
    for key in list(records.keys()):
        rec = records.get(key) or {}
        mid = str(rec.get("outer_mid") or "")
        if not mid:
            continue
        sid = str(rec.get("raw_sid") or "")
        kept = seen.setdefault(mid, [])
        twin = None
        for other in kept:
            other_sid = str((records.get(other) or {}).get("raw_sid") or "")
            if not sid or not other_sid or other_sid == sid:
                twin = other
                break
        if twin is None:
            kept.append(key)
            continue
        twin_sid = str((records.get(twin) or {}).get("raw_sid") or "")
        if not twin_sid and sid:
            records.pop(twin, None)
            kept.remove(twin)
            kept.append(key)
        else:
            records.pop(key, None)
    path = _forward_cache_path()
    if path is None:
        return False
    removed = _forward_cache_trim(records)
    if not _forward_cache_write_to(path, records):
        return False
    _FORWARD_CACHE = records
    _FORWARD_MID_INDEX = _forward_mid_index_of(records)
    if removed:
        media_db_gc()
    return True


def _display_proof_path():
    """凭证现在跟转发图片存在同一份文件里（用户 m10466）。"""
    return _forward_cache_path()


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
    """转发缓存的整份记录（图片 + 正文）；读不出来返回 None。"""
    return _forward_cache_read()


def set_display_proof_keep_fn(fn) -> None:
    """插件注入「这个会话留多少条原始凭证」；传 None 恢复全局 128。"""
    global _DISPLAY_PROOF_KEEP_FN
    _DISPLAY_PROOF_KEEP_FN = fn


def set_display_proof_keep_days_fn(fn) -> None:
    """插件注入「这个会话留多少天原始凭证」；传 None 不按天清理。"""
    global _DISPLAY_PROOF_DAYS_FN
    _DISPLAY_PROOF_DAYS_FN = fn


def _display_proof_trim(records: dict) -> None:
    """按保留天数 / 每会话上限裁一遍（用户 m10466：图片和正文一起裁）。"""
    _forward_cache_trim(records)

def _display_proof_write(records: dict) -> bool:
    """原子写回转发缓存；写不下（超过 8MB）就保留旧文件，返回 False。"""
    global _FORWARD_MID_INDEX
    path = _forward_cache_path()
    if path is None:
        return False
    ok = _forward_cache_write_to(path, records)
    if ok:
        _FORWARD_MID_INDEX = _forward_mid_index_of(records)
    return ok

def display_proof_stats(sid: str = "") -> dict:
    """转发缓存的条数 / 磁盘占用（用户 m10466：跟转发图片合成一份，统计也共用）。"""
    return media_db_stats(sid)


def display_proof_clear(cutoff=None) -> int:
    """清转发缓存，返回删掉的条数（用户 m21482 / m10466）。"""
    return media_db_clear(cutoff)


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
    if not isinstance(got, dict) or not got.get("nodes"):
        # 只有图片的记录证明不了节点 / 槽位归属，按「没有凭证」处理。
        return None
    return {"version": 1, "raw_sid": got.get("raw_sid"),
            "outer_mid": got.get("outer_mid"), "res_id": got.get("res_id"),
            "body": got.get("body"), "nodes": copy.deepcopy(got.get("nodes") or [])}

def _display_proof_save(raw_sid, mid, proof) -> None:
    """写 / 删一条转发缓存记录（用户 m10466：图片和正文是同一条）。"""
    key, path = _display_key(raw_sid, mid), _forward_cache_path()
    if key is None or path is None:
        return
    records = _display_proofs_read()
    # Never overwrite an unreadable file. A failed atomic replace leaves all old bytes.
    if records is None or (proof is not None and not _display_proof_ok(proof, raw_sid, mid)):
        return
    outer = str(mid or "").strip()
    # 用户 m10643：以前用 _FORWARD_MID_INDEX 找旧记录，那张表把同号的不同会话折成一条，
    # 后写的会话会把先写的那条删掉。现在只认本会话自己的那条（短 sid 与完整 sid 认成同一条），
    # 别的会话同号记录一律不碰。
    old = _forward_record_of(records, raw_sid, outer)
    old_key = None
    if old is not None:
        for rec_key, rec in records.items():
            if rec is old:
                old_key = rec_key
                break
    if proof is None and old is None:
        return
    if proof is None:
        # A known-invalid old entry must not survive a failed atomic revocation.
        _display_proof_denied(key, reject=True)
    if old_key is not None:
        records.pop(old_key, None)
    if proof is not None:
        # v4.55（用户 m19940）：记下写入时间，按保留天数清理时要用。
        records[key] = {"version": 2, "raw_sid": raw_sid, "outer_mid": outer,
                        "res_id": proof["res_id"], "body": proof["body"],
                        "nodes": proof["nodes"],
                        "media": [ref for node in proof["nodes"] for ref in node["media"]],
                        "saved_at": int(time.time())}
    elif isinstance(old, dict) and old.get("media"):
        # 用户 m10466：只撤正文凭证时图还在，就降级成「只有图片」的记录，别把图一起丢了。
        records[key] = {"version": 2, "raw_sid": raw_sid, "outer_mid": outer,
                        "res_id": "", "body": "", "nodes": [],
                        "media": list(old["media"]),
                        "saved_at": int(old.get("saved_at") or time.time())}
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
        if not media_for_mid(mid, raw_sid):
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
