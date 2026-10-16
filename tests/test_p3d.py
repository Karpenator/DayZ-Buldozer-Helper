import struct

import numpy as np
import pytest

from terrain_editor.p3d import P3DError, read_p3d_points


def make_p3d(path, lods):
    out = bytearray(b"MLOD" + struct.pack("<II", 257, len(lods)))
    for pts in lods:
        out += b"P3DM" + struct.pack("<6I", 28, 256, len(pts), 0, 0, 0)
        for x, y, z in pts:
            out += struct.pack("<3fI", x, y, z, 0)
    path.write_bytes(bytes(out))


def test_reads_points_and_bbox(tmp_path):
    p = tmp_path / "box.p3d"
    box = [(-2.0, 0.0, -2.0), (2.0, 0.0, -2.0), (2.0, 3.0, 2.0), (-2.0, 3.0, 2.0)]
    make_p3d(p, [box, box[:2]])
    lods = read_p3d_points(p)
    assert [len(l.points) for l in lods] == [4, 2]
    lo, hi = lods[0].bbox
    np.testing.assert_allclose(lo, [-2, 0, -2])
    np.testing.assert_allclose(hi, [2, 3, 2])


def test_rejects_non_mlod(tmp_path):
    p = tmp_path / "x.p3d"
    p.write_bytes(b"ODOL" + b"\0" * 64)
    with pytest.raises(P3DError):
        read_p3d_points(p)


def test_real_file_bbox_if_present():
    """Smoke check on the real EV3_2 model (18.5 m wide) when the repo has it."""
    from pathlib import Path
    real = Path(__file__).resolve().parents[2] / "evelone_modular" / "EV3_2.p3d"
    if not real.exists():
        pytest.skip("evelone_modular not in this checkout")
    lo, hi = read_p3d_points(real)[0].bbox
    assert 18.0 < hi[0] - lo[0] < 19.0
