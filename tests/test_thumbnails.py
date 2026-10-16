import numpy as np

from terrain_editor.p3d import P3DMesh
from terrain_editor.thumbnails import BACKGROUND, render_thumbnail


def _quad_mesh() -> P3DMesh:
    v = np.array([[0, 0, 0], [10, 0, 0], [10, 0, 10], [0, 0, 10]], dtype=np.float32)
    t = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.uint32)
    return P3DMesh(v, t)


def test_thumbnail_shape_and_content():
    img = render_thumbnail(_quad_mesh(), 64)
    assert img.shape == (64, 64, 3) and img.dtype == np.uint8
    filled = np.any(img != np.array(BACKGROUND, dtype=np.uint8), axis=2)
    # the plate covers a sizeable part of the square (it is drawn solid, see the 128px check)
    assert filled.mean() > 0.15


def test_empty_mesh_is_background():
    m = P3DMesh(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32))
    img = render_thumbnail(m, 32)
    assert np.all(img == np.array(BACKGROUND, dtype=np.uint8))


def test_degenerate_extent_is_background():
    v = np.zeros((3, 3), np.float32)
    m = P3DMesh(v, np.array([[0, 1, 2]], np.uint32))
    img = render_thumbnail(m, 32)
    assert np.all(img == np.array(BACKGROUND, dtype=np.uint8))
