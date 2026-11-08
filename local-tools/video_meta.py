#!/usr/bin/env python3
"""MP4/MOV 视频元数据写入（纯标准库，非上游内容）。

在 ISOBMFF 容器层把 QuickTime 经典用户数据 atom 写进 moov/udta：
  - ©mak  制造商（如 Apple）
  - ©mod  机型（如 iPhone 14 Pro）
  - ©xyz  GPS 坐标（ISO 6709 文本，如 +22.515500+113.941000/）
  - ©swr  软件（可选）
  - ©day  拍摄时间（可选，ISO 8601）

与上游 strip_isobmff 同一套约定：**只动容器，不碰 mdat 媒体数据**，
视频流不会被重新编码，画质无损。

难点在 moov 变长之后媒体偏移的维护：stco/co64 记录的是 chunk 在整个
文件里的绝对偏移。策略（按优先级）：
  1. 用文件尾部连续的 free/skip box 吸收增量（mdat 完全不动，最安全）；
  2. mdat 在 moov 之前时，moov 变长不影响 mdat，直接替换；
  3. 都不行就重建 stco/co64，把每个 chunk offset 加上 mdat 的位移量。
"""

from __future__ import annotations

import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "service", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from image_meta import _build_isobmff_box, _parse_isobmff_boxes  # noqa: E402

# 这几个 udta 子 atom 会被本模块写入/替换，其余原样保留
TARGET_ATOMS = {b"\xa9mak", b"\xa9mod", b"\xa9xyz", b"\xa9swr", b"\xa9day"}
# 可被挪用吸收增量的顶层占位 box
FILLER_BOXES = {b"free", b"skip"}


def _atom(fourcc: bytes, text: str) -> bytes:
    return _build_isobmff_box(fourcc, text.encode("utf-8"))


def _iso6709(lat: float, lng: float) -> str:
    def fmt(v: float) -> str:
        return ("+" if v >= 0 else "-") + format(abs(v), ".6f")

    return fmt(lat) + fmt(lng) + "/"


def _build_udta(
    make: str | None,
    model: str | None,
    gps: tuple[float, float] | None,
    software: str | None,
    created: str | None,
) -> bytes:
    atoms = b""
    if make:
        atoms += _atom(b"\xa9mak", make)
    if model:
        atoms += _atom(b"\xa9mod", model)
    if gps is not None:
        atoms += _atom(b"\xa9xyz", _iso6709(gps[0], gps[1]))
    if software:
        atoms += _atom(b"\xa9swr", software)
    if created:
        atoms += _atom(b"\xa9day", created)
    return _build_isobmff_box(b"udta", atoms)


def _patch_offsets(payload: bytes, shift: int) -> tuple[bytes, int]:
    """把 moov 内 stco/co64 的所有 chunk offset 加上 shift，返回 (新 payload, 修补条数)。

    只沿 trak -> mdia -> minf -> stbl 的已知容器链下降，不盲扫，
    避免误改 udta/meta 等结构里同名的字节序列。
    """
    if shift == 0:
        return payload, 0
    boxes, scanned = _parse_isobmff_boxes(payload)
    if scanned != len(payload):
        raise ValueError("moov 内部结构不完整，无法安全写入")
    out = []
    patched = 0
    for fourcc, bp, _size, hs in boxes:
        if fourcc == b"stco":
            n = struct.unpack(">I", bp[4:8])[0]
            body = bytearray(bp)
            for i in range(n):
                off = 8 + i * 4
                old = struct.unpack(">I", body[off : off + 4])[0]
                body[off : off + 4] = struct.pack(">I", (old + shift) & 0xFFFFFFFF)
            patched += n
            out.append(_build_isobmff_box(fourcc, bytes(body), hs))
        elif fourcc == b"co64":
            n = struct.unpack(">I", bp[4:8])[0]
            body = bytearray(bp)
            for i in range(n):
                off = 8 + i * 8
                old = struct.unpack(">Q", body[off : off + 8])[0]
                body[off : off + 8] = struct.pack(">Q", old + shift)
            patched += n
            out.append(_build_isobmff_box(fourcc, bytes(body), hs))
        elif fourcc in (b"trak", b"mdia", b"minf", b"stbl"):
            new_body, cnt = _patch_offsets(bp, shift)
            patched += cnt
            out.append(_build_isobmff_box(fourcc, new_body, hs))
        else:
            out.append(_build_isobmff_box(fourcc, bp, hs))
    return b"".join(out), patched


def _tail_filler_capacity(boxes: list[tuple[bytes, bytes, int, int]], from_idx: int) -> int:
    """moov 之后从尾部往前连续 free/skip 的可吸收字节数（每个 box 最少留 8 字节头）。"""
    cap = 0
    for fourcc, _p, size, _hs in reversed(boxes[from_idx:]):
        if fourcc not in FILLER_BOXES:
            break
        cap += size - 8
    return cap


def tag_video(
    data: bytes,
    *,
    make: str | None = None,
    model: str | None = None,
    gps: tuple[float, float] | None = None,
    software: str | None = None,
    created: str | None = None,
) -> tuple[bytes, list[str]]:
    """向 MP4/MOV 写入拍摄设备 / GPS 元数据，返回 (新文件字节, 动作列表)。"""
    if not any((make, model, gps, software, created)):
        raise ValueError("没有指定任何要写入的字段")
    boxes, scanned = _parse_isobmff_boxes(data)
    if scanned != len(data):
        raise ValueError("文件不是可解析的 MP4/MOV 结构（尾部不完整或容器异常），拒绝写入")
    moov_idx = next(
        (i for i, (f, _p, _s, _h) in enumerate(boxes) if f == b"moov"), None
    )
    if moov_idx is None:
        raise ValueError("找不到 moov box（不是标准 MP4/MOV）")

    moov_payload = boxes[moov_idx][1]
    subs, sub_scanned = _parse_isobmff_boxes(moov_payload)
    if sub_scanned != len(moov_payload):
        raise ValueError("moov 内部结构不完整，拒绝写入")

    udta_idx = next((i for i, (f, *_r) in enumerate(subs) if f == b"udta"), None)
    new_udta = _build_udta(make, model, gps, software, created)
    actions: list[str] = []

    if udta_idx is None:
        new_subs = subs + [(b"udta", new_udta[8:], len(new_udta), 8)]
        actions.append("moov/udta 不存在，已新建并写入拍摄信息")
    else:
        old_udta_payload = subs[udta_idx][1]
        old_sub_atoms, _ = _parse_isobmff_boxes(old_udta_payload)
        kept = [b for b in old_sub_atoms if b[0] not in TARGET_ATOMS]
        replaced = len(old_sub_atoms) - len(kept)
        body = b"".join(_build_isobmff_box(f, p, hs) for f, p, _s, hs in kept)
        body += new_udta[8:]
        new_subs = list(subs)
        new_subs[udta_idx] = (
            b"udta",
            body,
            len(body) + 8,
            8,
        )
        actions.append(
            f"替换 moov/udta 中 {replaced} 个旧 atom 并写入拍摄信息"
            if replaced
            else "moov/udta 已存在，追加拍摄信息"
        )

    new_moov_payload = b"".join(_build_isobmff_box(f, p, hs) for f, p, _s, hs in new_subs)

    # 保证 moov 不变小：变小就在 moov 末尾补 free，保持顶层偏移全部不动
    delta = len(new_moov_payload) - len(moov_payload)
    if delta < 0:
        new_moov_payload += _build_isobmff_box(b"free", b"\x00" * (-delta - 8))
        delta = 0
    if delta == 0:
        actions.append("moov 尺寸不变，其余结构原样保留")

    tail_boxes = boxes[moov_idx + 1 :]
    mdat_in_tail = any(f == b"mdat" for f, *_r in tail_boxes)
    cap = _tail_filler_capacity(boxes, moov_idx + 1)
    tail: list[bytes] = [_build_isobmff_box(f, p, hs) for f, p, _s, hs in tail_boxes]

    shift = 0
    if delta > 0:
        if cap >= delta:
            # 用尾部 free 吸收：mdat 不动，无需修 stco
            remaining = delta
            new_tail = []
            for fourcc, p, size, hs in reversed(tail_boxes):
                if remaining > 0 and fourcc in FILLER_BOXES:
                    shrink = min(remaining, size - 8)
                    new_size = size - shrink
                    remaining -= shrink
                    new_tail.append(
                        _build_isobmff_box(fourcc, b"\x00" * (new_size - hs), hs)
                    )
                else:
                    new_tail.append(_build_isobmff_box(fourcc, p, hs))
            tail = list(reversed(new_tail))
            actions.append(f"用文件尾部 free box 吸收 {delta} 字节增量（媒体数据零移动）")
        elif mdat_in_tail:
            shift = delta
            actions.append(
                f"moov 增大 {delta} 字节，已同步修补 stco/co64 chunk 偏移（+{delta}）"
            )
        else:
            actions.append("mdat 在 moov 之前，moov 增大不影响媒体偏移")

    if shift:
        new_moov_payload, patched = _patch_offsets(new_moov_payload, shift)
        if patched == 0:
            raise ValueError("需要修补 chunk 偏移但没找到 stco/co64，为安全起见放弃写入")
        actions.append(f"共修补 {patched} 条 chunk 偏移记录")

    parts = []
    for i, (fourcc, p, _s, hs) in enumerate(boxes):
        if i == moov_idx:
            parts.append(_build_isobmff_box(b"moov", new_moov_payload, hs))
            parts.extend(tail)
            break
        parts.append(_build_isobmff_box(fourcc, p, hs))

    return b"".join(parts), actions
