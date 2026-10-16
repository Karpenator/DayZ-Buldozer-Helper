import struct
from pathlib import Path

import numpy as np
import pytest

from terrain_editor.paa import PaaError, read_paa
from terrain_editor.p3d import P3DMesh
from terrain_editor.thumbnails import BACKGROUND, render_thumbnail


def _make_paa(width: int, height: int, block: bytes) -> bytes:
    """Minimal uncompressed DXT1 .paa with one mipmap and no palette."""
    out = bytearray(struct.pack("<H", 0xFF01))
    out += b"GGAT" + b"CGVA" + struct.pack("<I", 4) + b"\0\0\0\0"
    out += struct.pack("<H", 0)                                   # palette count
    out += struct.pack("<HH", width, height) + bytes([len(block) & 255, (len(block) >> 8) & 255,
                                                       len(block) >> 16])
    out += block
    out += struct.pack("<HH", 0, 0) + b"\0\0\0"                    # end of mipmaps
    return bytes(out)


def test_dxt1_block_white(tmp_path: Path):
    # c0 = white (0xFFFF), c1 = black, all indices 0 -> every texel is white
    block = struct.pack("<HHI", 0xFFFF, 0x0000, 0)
    f = tmp_path / "w.paa"
    f.write_bytes(_make_paa(4, 4, block))
    img = read_paa(f, min_size=4)
    assert img.shape == (4, 4, 4)
    assert np.all(img[..., :3] == 255)
    assert np.all(img[..., 3] == 255)


def test_dxt1_indices_pick_second_colour(tmp_path: Path):
    # index 1 for every texel -> c1 (black)
    block = struct.pack("<HHI", 0xFFFF, 0x0000, 0x55555555)
    f = tmp_path / "b.paa"
    f.write_bytes(_make_paa(4, 4, block))
    img = read_paa(f, min_size=4)
    assert np.all(img[..., :3] == 0)


def test_no_mip_big_enough_raises(tmp_path: Path):
    f = tmp_path / "s.paa"
    f.write_bytes(_make_paa(4, 4, struct.pack("<HHI", 0, 0, 0)))
    with pytest.raises(PaaError):
        read_paa(f, min_size=256)


def test_textured_thumbnail_uses_texture_colour():
    v = np.array([[0, 0, 0], [10, 0, 0], [10, 0, 10], [0, 0, 10]], dtype=np.float32)
    t = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    uv = np.array([[[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 1], [0, 1]]], dtype=np.float32)
    mesh = P3DMesh(v, t, uvs=uv, tex_ids=np.zeros(2, np.int32), textures=["t.paa"])
    red = np.zeros((8, 8, 4), dtype=np.uint8)
    red[..., 0] = 255
    red[..., 3] = 255
    img = render_thumbnail(mesh, 64, textures={0: red})
    filled = np.any(img != np.array(BACKGROUND, dtype=np.uint8), axis=2)
    assert filled.any()
    drawn = img[filled]
    # shading only darkens: red stays dominant and green/blue stay near zero
    assert np.all(drawn[:, 1] < 40) and np.all(drawn[:, 2] < 40)
    assert drawn[:, 0].mean() > 80
