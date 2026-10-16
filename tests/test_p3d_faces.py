import struct

from terrain_editor.p3d import _adaptive_tail


def _face(nv: int, gap: int, tex: bytes, mat: bytes, next_nv: int | None) -> bytes:
    """One face record: vertex count, vertices (point, normal, u, v), gap, two names, next count."""
    b = struct.pack("<I", nv)
    for k in range(nv):
        b += struct.pack("<IIff", k, k, 0.0, 0.0)
    b += b"\0" * gap + tex + b"\0" + mat + b"\0"
    if next_nv is not None:
        b += struct.pack("<I", next_nv)
    return b


def test_triangle_has_20_byte_gap_and_quad_has_4():
    mat = b"dz\\structures\\x.rvmat"
    raw = _face(3, 20, b"", mat, None) + _face(4, 4, b"", mat, None)
    o = 4 + 16 * 3                       # past the triangle's count and vertices
    tex, got, o = _adaptive_tail(raw, o, 3, last=False)
    assert (tex, got) == ("", mat.decode("latin1"))
    assert struct.unpack_from("<I", raw, o)[0] == 4   # next face starts here
    o = o + 4 + 16 * 4
    tex, got, o = _adaptive_tail(raw, o, 4, last=True)
    assert got == mat.decode("latin1") and o == len(raw)


def test_wrong_gap_is_rejected_by_the_next_face():
    mat = b"m.rvmat"
    raw = _face(4, 4, b"tex.paa", mat, 3)
    tex, got, _ = _adaptive_tail(raw, 4 + 16 * 4, 4, last=False)
    assert tex == "tex.paa" and got == "m.rvmat"
