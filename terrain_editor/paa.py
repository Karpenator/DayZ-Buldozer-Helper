"""Reader for Arma/DayZ .paa textures (decodes one mipmap to RGBA uint8).

Layout (little-endian):
  u16 type: 0xFF01 DXT1, 0xFF03 DXT3, 0xFF05 DXT5, 0x4444 ARGB4444, 0x8888 ARGB8888
  tags: 'GGAT' + 4-byte name + u32 length + data (repeated; CGVA, CXAM, SFFO, ...)
  u16 palette count, then count * 3 bytes (RGB; 0 here)
  mipmaps, largest first: u16 width (bit 15 = LZSS compressed), u16 height,
  u24 data size, data. A header with width = height = 0 ends the list.
Mipmaps 128 px and smaller are stored uncompressed. Larger ones use LZSS, which is not
decoded yet (tried variants did not reproduce the data; see the test notes).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class PaaError(ValueError):
    pass


TYPE_DXT1 = 0xFF01
TYPE_DXT3 = 0xFF03
TYPE_DXT5 = 0xFF05
TYPE_ARGB4444 = 0x4444
TYPE_ARGB8888 = 0x8888


@dataclass
class _Mip:
    width: int
    height: int
    compressed: bool
    start: int
    size: int


def _read_header(raw: bytes) -> tuple[int, list[_Mip]]:
    if len(raw) < 4:
        raise PaaError("file too short")
    (typ,) = struct.unpack_from("<H", raw, 0)
    pos = 2
    while raw[pos:pos + 4] == b"GGAT":
        (ln,) = struct.unpack_from("<I", raw, pos + 8)
        pos += 12 + ln
    (pal,) = struct.unpack_from("<H", raw, pos)
    pos += 2 + 3 * pal
    mips: list[_Mip] = []
    while pos + 7 <= len(raw):
        w, h = struct.unpack_from("<HH", raw, pos)
        size = raw[pos + 4] | (raw[pos + 5] << 8) | (raw[pos + 6] << 16)
        pos += 7
        if w == 0 and h == 0:
            break
        compressed = bool(w & 0x8000)
        mips.append(_Mip(w & 0x7FFF, h & 0x7FFF, compressed, pos, size))
        pos += size
    if not mips:
        raise PaaError("no mipmaps")
    return typ, mips


def _rgb565(c: np.ndarray) -> np.ndarray:
    r = (c >> 11) & 31
    g = (c >> 5) & 63
    b = c & 31
    return np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)],
                    axis=-1).astype(np.float64)


def _dxt_colors(blocks: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """blocks: (N, 8) uint8 colour part. Returns colours (N,16,4) float and alpha-key flag."""
    c0 = blocks[:, 0].astype(np.uint16) | (blocks[:, 1].astype(np.uint16) << 8)
    c1 = blocks[:, 2].astype(np.uint16) | (blocks[:, 3].astype(np.uint16) << 8)
    idx = np.zeros((len(blocks), 16), dtype=np.int64)
    word = (blocks[:, 4].astype(np.uint32) | (blocks[:, 5].astype(np.uint32) << 8)
            | (blocks[:, 6].astype(np.uint32) << 16) | (blocks[:, 7].astype(np.uint32) << 24))
    for p in range(16):
        idx[:, p] = (word >> (2 * p)) & 3
    p0 = _rgb565(c0)
    p1 = _rgb565(c1)
    pal = np.zeros((len(blocks), 4, 4), dtype=np.float64)
    pal[:, 0, :3] = p0
    pal[:, 1, :3] = p1
    pal[:, 2, :3] = (2 * p0 + p1) / 3
    pal[:, 3, :3] = (p0 + 2 * p1) / 3
    pal[:, :, 3] = 255
    four = c0 > c1
    # DXT1 colour-key mode (c0 <= c1): index 3 is transparent, the mix is the average
    pal[~four, 2, :3] = (p0[~four] + p1[~four]) / 2
    pal[~four, 3, :] = 0
    colours = np.take_along_axis(pal, idx[:, :, None], axis=1)
    return colours, ~four


def _decode_dxt1(data: bytes, w: int, h: int) -> np.ndarray:
    bw, bh = max(1, w // 4), max(1, h // 4)
    blocks = np.frombuffer(data, dtype=np.uint8, count=bw * bh * 8).reshape(-1, 8)
    cols, _ = _dxt_colors(blocks)
    return _assemble(cols, bw, bh, w, h)


def _decode_dxt5(data: bytes, w: int, h: int) -> np.ndarray:
    bw, bh = max(1, w // 4), max(1, h // 4)
    raw = np.frombuffer(data, dtype=np.uint8, count=bw * bh * 16).reshape(-1, 16)
    cols, _ = _dxt_colors(raw[:, 8:16])
    a0 = raw[:, 0].astype(np.int64)
    a1 = raw[:, 1].astype(np.int64)
    word = np.zeros(len(raw), dtype=np.int64)
    for k in range(6):
        word |= raw[:, 2 + k].astype(np.int64) << (8 * k)
    aidx = np.zeros((len(raw), 16), dtype=np.int64)
    for p in range(16):
        aidx[:, p] = (word >> (3 * p)) & 7
    apal = np.zeros((len(raw), 8), dtype=np.float64)
    apal[:, 0] = a0
    apal[:, 1] = a1
    gt = a0 > a1
    for k in range(1, 7):
        if True:
            apal[gt, k + 1] = ((7 - k) * a0[gt] + k * a1[gt]) / 7
            apal[~gt, k + 1] = ((5 - k + 1) * a0[~gt] + (k - 1) * a1[~gt]) / 5 if k < 6 else 0
    apal[~gt, 6] = 0
    apal[~gt, 7] = 255
    alpha = np.take_along_axis(apal, aidx, axis=1)
    cols[:, :, 3] = alpha
    return _assemble(cols, bw, bh, w, h)


def _decode_dxt3(data: bytes, w: int, h: int) -> np.ndarray:
    bw, bh = max(1, w // 4), max(1, h // 4)
    raw = np.frombuffer(data, dtype=np.uint8, count=bw * bh * 16).reshape(-1, 16)
    cols, _ = _dxt_colors(raw[:, 8:16])
    nib = np.zeros((len(raw), 16), dtype=np.float64)
    for p in range(16):
        byte = raw[:, p // 2]
        nib[:, p] = (byte >> (4 * (p % 2))) & 15
    cols[:, :, 3] = nib * 17
    return _assemble(cols, bw, bh, w, h)


def _decode_argb4444(data: bytes, w: int, h: int) -> np.ndarray:
    px = np.frombuffer(data, dtype="<u2", count=w * h).reshape(h, w).astype(np.uint16)
    a = (px >> 12) & 15
    r = (px >> 8) & 15
    g = (px >> 4) & 15
    b = px & 15
    return np.stack([r * 17, g * 17, b * 17, a * 17], axis=-1).astype(np.uint8)


def _decode_argb8888(data: bytes, w: int, h: int) -> np.ndarray:
    px = np.frombuffer(data, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4)
    # stored as BGRA
    return px[:, :, [2, 1, 0, 3]].copy()


def _assemble(cols: np.ndarray, bw: int, bh: int, w: int, h: int) -> np.ndarray:
    img = cols.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return np.clip(img[:h, :w], 0, 255).astype(np.uint8)


def _mip_bytes(raw: bytes, typ: int, mip: _Mip) -> bytes:
    if mip.compressed:
        raise PaaError("LZSS-compressed mipmap (not decoded yet)")
    return raw[mip.start:mip.start + mip.size]


def read_paa(path: str | Path, min_size: int = 256) -> np.ndarray:
    """Decode the smallest uncompressed mipmap that is at least min_size wide.

    Only uncompressed mipmaps are read: LZSS decoding is not implemented, and DayZ
    textures keep their 128 px and smaller mipmaps uncompressed, which is enough for
    previews. Raises PaaError if no such mipmap exists.
    """
    raw = Path(path).read_bytes()
    typ, mips = _read_header(raw)
    fitting = [m for m in mips if not m.compressed
               and m.width >= min_size and m.height >= min_size]
    if not fitting:
        raise PaaError(f"no uncompressed mipmap >= {min_size}px")
    mip = min(fitting, key=lambda m: m.width * m.height)
    data = _mip_bytes(raw, typ, mip)
    w, h = mip.width, mip.height
    if typ == TYPE_DXT1:
        return _decode_dxt1(data, w, h)
    if typ == TYPE_DXT3:
        return _decode_dxt3(data, w, h)
    if typ == TYPE_DXT5:
        return _decode_dxt5(data, w, h)
    if typ == TYPE_ARGB4444:
        return _decode_argb4444(data, w, h)
    if typ == TYPE_ARGB8888:
        return _decode_argb8888(data, w, h)
    raise PaaError(f"unsupported PAA type {typ:#x}")
