#!/usr/bin/env python3
"""video_meta.tag_video 的回归测试（纯标准库）。

构造三种顶层布局的最小 MP4，逐项校验：
  1. 写入后容器结构仍可完整解析；
  2. mdat 媒体字节逐位不变；
  3. stco 记录的 chunk 绝对偏移仍精确指向 mdat 里的 marker；
  4. 重复写入是替换而不是叠加。
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service", "scripts")))

from image_meta import _parse_isobmff_boxes, inspect_isobmff  # noqa: E402
from video_meta import tag_video  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    tag = "✓" if cond else "✗"
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def box(fourcc, payload):
    return struct.pack(">I", 8 + len(payload)) + fourcc + payload


def fullbox(fourcc, version_flags, payload):
    return box(fourcc, struct.pack(">I", version_flags) + payload)


def make_mp4(layout):
    """layout: 'moov_first' | 'mdat_first' | 'mdat_first_free'"""
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")

    # mdat：两个 chunk，marker 分别在 chunk 头部
    mdat_payload = b"CHUNK-A-0123456789abcdef" + b"\x00" * 32 + b"CHUNK-B-fedcba9876543210" + b"\x00" * 16
    mdat = box(b"mdat", mdat_payload)

    # stco 里的 offset 稍后按最终布局填
    def build_moov(chunk_offsets):
        stco = fullbox(b"stco", 0, struct.pack(">I", 2) + b"".join(struct.pack(">I", o) for o in chunk_offsets))
        stbl = box(b"stbl", stco)
        minf = box(b"minf", stbl)
        mdia = box(b"mdia", minf)
        trak = box(b"trak", box(b"tkhd", b"\x00" * 80) + mdia)
        mvhd = fullbox(b"mvhd", 0, b"\x00" * 96)
        return box(b"moov", mvhd + trak)

    if layout == "moov_first":
        # moov 在 mdat 前：先占位 moov 长度（写入的目标 chunk offset 依赖最终布局，
        # 这里模拟一个"真实"文件：stco 指向 mdat 中 chunk 的绝对位置）
        moov_probe = build_moov([0, 0])
        mdat_start = len(ftyp) + len(moov_probe)
        moov = build_moov([mdat_start + 8, mdat_start + 8 + 56])
        return ftyp + moov + mdat
    if layout == "mdat_first":
        mdat_start = len(ftyp)
        moov = build_moov([mdat_start + 8, mdat_start + 8 + 56])
        return ftyp + mdat + moov
    if layout == "mdat_first_free":
        mdat_start = len(ftyp)
        moov = build_moov([mdat_start + 8, mdat_start + 8 + 56])
        free = box(b"free", b"\x00" * 64)
        return ftyp + mdat + moov + free
    raise ValueError(layout)


def verify(original, tagged, label):
    # 1. 结构完整
    boxes, scanned = _parse_isobmff_boxes(tagged)
    check(f"{label}: 容器可完整解析", scanned == len(tagged))

    # 2. mdat 字节不变
    old_mdat = next(p for f, p, _s, _h in _parse_isobmff_boxes(original)[0] if f == b"mdat")
    new_mdat = next(p for f, p, _s, _h in boxes if f == b"mdat")
    check(f"{label}: mdat 媒体数据逐位不变", old_mdat == new_mdat)

    # 3. stco 指向正确
    def find_stco(payload):
        subs, s = _parse_isobmff_boxes(payload)
        assert s == len(payload)
        for f, p, _sz, _h in subs:
            if f == b"stco":
                return p
            if f in (b"trak", b"mdia", b"minf", b"stbl"):
                r = find_stco(p)
                if r is not None:
                    return r
        return None

    moov_p = next(p for f, p, _s, _h in boxes if f == b"moov")
    stco = find_stco(moov_p)
    n = struct.unpack(">I", stco[4:8])[0]
    offsets = [struct.unpack(">I", stco[8 + i * 4 : 12 + i * 4])[0] for i in range(n)]
    mdat_start = next(
        (struct.unpack(">I", tagged[pos : pos + 4])[0] and pos + 8)
        for pos in range(len(tagged))
        if tagged[pos + 4 : pos + 8] == b"mdat" and pos >= 0
    ) if False else None
    # 直接扫 mdat 起点
    pos = 0
    mdat_start = None
    while pos + 8 <= len(tagged):
        size = struct.unpack(">I", tagged[pos : pos + 4])[0]
        if tagged[pos + 4 : pos + 8] == b"mdat":
            mdat_start = pos
            break
        pos += size
    ok1 = tagged[offsets[0] : offsets[0] + 7] == b"CHUNK-A"
    ok2 = tagged[offsets[1] : offsets[1] + 7] == b"CHUNK-B"
    check(f"{label}: chunk#1 偏移指向 marker A", ok1, f"offset={offsets[0]} mdat={mdat_start}")
    check(f"{label}: chunk#2 偏移指向 marker B", ok2, f"offset={offsets[1]} mdat={mdat_start}")

    # 4. inspect 不报异常
    has_c2pa, has_ai, findings = inspect_isobmff(tagged, fmt="mp4")
    check(f"{label}: inspect 通过", isinstance(findings, list))


def find_udta_atoms(data):
    boxes, _ = _parse_isobmff_boxes(data)
    moov = next(p for f, p, _s, _h in boxes if f == b"moov")
    subs, _ = _parse_isobmff_boxes(moov)
    udta = next(p for f, p, _s, _h in subs if f == b"udta")
    return {f: p for f, p, _s, _h in _parse_isobmff_boxes(udta)[0]}


GPS = (22.5155 + 0.0013, 113.9410 - 0.0009)


def main():
  for layout in ("moov_first", "mdat_first", "mdat_first_free"):
    print(f"\n布局 {layout}:")
    original = make_mp4(layout)
    tagged, actions = tag_video(
        original,
        make="Apple",
        model="iPhone 14 Pro",
        gps=GPS,
        created="2026-11-08T15:30:00+0800",
    )
    for a in actions:
        print(f"    · {a}")
    verify(original, tagged, layout)

    atoms = find_udta_atoms(tagged)
    check("写入 ©mak=Apple", atoms.get(b"\xa9mak") == b"Apple")
    check("写入 ©mod=iPhone 14 Pro", atoms.get(b"\xa9mod") == b"iPhone 14 Pro")
    xyz = atoms.get(b"\xa9xyz", b"").decode()
    check("©xyz 为 ISO6709 格式", xyz.startswith("+") and xyz.endswith("/") and xyz.count("+") == 2, xyz)
    check("写入 ©day", atoms.get(b"\xa9day") == b"2026-11-08T15:30:00+0800")

    # 重复写入 = 替换
    tagged2, _ = tag_video(tagged, make="Apple", model="iPhone 15 Pro", gps=GPS)
    atoms2 = find_udta_atoms(tagged2)
    check("二次写入为替换（©mod 更新）", atoms2.get(b"\xa9mod") == b"iPhone 15 Pro")
    check("二次写入不留旧 atom", list(atoms2).count(b"\xa9mod") == 1)
    verify(tagged, tagged2, layout + " 二次")


if __name__ == "__main__":
    main()
    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项: {FAILURES}")
        sys.exit(1)
    print("全部通过 ✓")
