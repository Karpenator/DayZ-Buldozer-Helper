"""Model preview thumbnails rendered with numpy (no GPU, no Qt).

The model is turned to a fixed three-quarter view, its triangles are sampled with a
barycentric grid of points, and the nearest point wins each pixel (z-buffer by sorting).
Each point gets its colour from the texture (UV lookup, nearest texel) when the texture
is available, otherwise a neutral base colour; then it is shaded from the triangle normal.
Good enough to recognise a model in a list; it is not a reference renderer.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Callable

import numpy as np

from .p3d import P3DMesh, read_p3d_mesh

MAX_TRIANGLES = 20000      # larger meshes are sub-sampled (thumbnails do not need every face)
MAX_SUBDIV = 48
POINT_BUDGET = 1_500_000   # total sampled points; keeps big models fast
BACKGROUND = (58, 62, 68)       # под тёмное окно над Buldozer
BASE_COLOR = np.array([196.0, 186.0, 168.0])
LIGHT = np.array([0.35, 0.75, 0.55]) / np.linalg.norm([0.35, 0.75, 0.55])
ALPHA_CUT = 128            # texels with alpha below this are not drawn


def _rotation(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    y, p = np.radians(yaw_deg), np.radians(pitch_deg)
    ry = np.array([[np.cos(y), 0, np.sin(y)], [0, 1, 0], [-np.sin(y), 0, np.cos(y)]])
    rx = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]])
    return rx @ ry


def _barycentric_grid(n: int) -> np.ndarray:
    """(K, 3) barycentric weights of a regular grid with n subdivisions."""
    rows = []
    for i in range(n + 1):
        for j in range(n + 1 - i):
            k = n - i - j
            rows.append((i / n, j / n, k / n))
    return np.array(rows, dtype=np.float64)


def _mask(size: int, flat_idx: np.ndarray) -> np.ndarray:
    m = np.zeros(size * size, dtype=bool)
    m[flat_idx] = True
    return m.reshape(size, size)


def _fill_holes(img: np.ndarray, filled: np.ndarray, passes: int = 2) -> None:
    """Fill background pixels that have at least two filled neighbours (sampling gaps)."""
    for _ in range(passes):
        f = filled.astype(np.float64)
        pad = np.pad(f, 1)
        col = np.pad(img.astype(np.float64), ((1, 1), (1, 1), (0, 0)))
        cnt = np.zeros_like(f)
        acc = np.zeros(img.shape, dtype=np.float64)
        h, w = f.shape
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                wgt = pad[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
                cnt += wgt
                acc += col[1 + dy:1 + dy + h, 1 + dx:1 + dx + w] * wgt[..., None]
        new = (~filled) & (cnt >= 2)
        if not new.any():
            break
        img[new] = (acc[new] / cnt[new][:, None]).astype(np.uint8)
        filled = filled | new


def render_thumbnail(mesh: P3DMesh, size: int = 256, yaw: float = 35.0,
                     pitch: float = -25.0,
                     textures: dict[int, np.ndarray] | None = None) -> np.ndarray:
    """Return an (size, size, 3) uint8 RGB image of the mesh.

    textures maps a mesh texture index (mesh.tex_ids) to an RGBA uint8 array. Triangles
    whose texture is missing use the base colour.
    """
    img = np.empty((size, size, 3), dtype=np.uint8)
    img[:] = BACKGROUND
    if mesh.triangles.size == 0 or mesh.vertices.size == 0:
        return img

    tris = mesh.triangles.astype(np.int64)
    uvs = mesh.uvs
    tex_ids = mesh.tex_ids
    if len(tris) > MAX_TRIANGLES:
        pick = np.linspace(0, len(tris) - 1, MAX_TRIANGLES).astype(np.int64)
        tris = tris[pick]
        if uvs is not None:
            uvs = uvs[pick]
        if tex_ids is not None:
            tex_ids = tex_ids[pick]
    textures = textures or {}

    verts = mesh.vertices.astype(np.float64)
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    centre = (lo + hi) / 2
    v = (verts - centre) @ _rotation(yaw, pitch).T   # camera looks down -z; larger z is nearer

    extent = float(max(v[:, 0].max() - v[:, 0].min(), v[:, 1].max() - v[:, 1].min()))
    if extent <= 0:
        return img
    scale = size * 0.86 / extent
    px = v[:, 0] * scale + size / 2
    py = -v[:, 1] * scale + size / 2

    a_, b_, c_ = v[tris[:, 0]], v[tris[:, 1]], v[tris[:, 2]]
    normal = np.cross(b_ - a_, c_ - a_)
    length = np.linalg.norm(normal, axis=1)
    length[length == 0] = 1.0
    normal /= length[:, None]
    tri_shade = 0.30 + 0.70 * np.abs(normal @ LIGHT)          # two-sided

    # per-triangle subdivision: about one sample per pixel of its longest screen edge
    longest = np.max(np.stack([
        np.hypot(px[tris[:, a]] - px[tris[:, b]], py[tris[:, a]] - py[tris[:, b]])
        for a, b in ((0, 1), (1, 2), (2, 0))]), axis=0)
    n_tri = np.clip(np.ceil(longest / 0.8), 1, MAX_SUBDIV).astype(np.int64)
    while np.sum((n_tri + 1) * (n_tri + 2) // 2) > POINT_BUDGET and n_tri.max() > 1:
        n_tri = np.maximum(1, (n_tri * 0.8).astype(np.int64))

    parts: dict[str, list[np.ndarray]] = {k: [] for k in ("pts", "shade", "x", "y", "uv", "tid")}
    for n in np.unique(n_tri):
        sel = np.nonzero(n_tri == n)[0]
        weights = _barycentric_grid(int(n))                    # (K, 3)
        K = len(weights)
        corners = np.stack([a_[sel], b_[sel], c_[sel]], axis=1)  # (T, 3, xyz)
        parts["pts"].append(np.einsum("kc,tcx->tkx", weights, corners).reshape(-1, 3))
        parts["shade"].append(np.repeat(tri_shade[sel], K))
        tp = tris[sel]
        parts["x"].append(np.einsum("kc,tc->tk", weights, px[tp]).reshape(-1))
        parts["y"].append(np.einsum("kc,tc->tk", weights, py[tp]).reshape(-1))
        if uvs is not None:
            parts["uv"].append(np.einsum("kc,tcx->tkx", weights, uvs[sel].astype(np.float64))
                               .reshape(-1, 2))
        else:
            parts["uv"].append(np.zeros((len(sel) * K, 2)))
        parts["tid"].append(np.repeat(tex_ids[sel] if tex_ids is not None
                                      else np.full(len(sel), -1), K))
    pts = np.concatenate(parts["pts"])
    shade = np.concatenate(parts["shade"])
    sx = np.concatenate(parts["x"])
    sy = np.concatenate(parts["y"])
    uv = np.concatenate(parts["uv"])
    tid = np.concatenate(parts["tid"])

    colors = np.tile(BASE_COLOR, (len(pts), 1))
    keep = np.ones(len(pts), dtype=bool)
    for t in np.unique(tid):
        tex = textures.get(int(t))
        if tex is None:
            continue
        sel = tid == t
        th, tw = tex.shape[:2]
        tx = np.floor((uv[sel, 0] % 1.0) * tw).astype(np.int64).clip(0, tw - 1)
        ty = np.floor((uv[sel, 1] % 1.0) * th).astype(np.int64).clip(0, th - 1)
        texel = tex[ty, tx]
        colors[sel] = texel[:, :3].astype(np.float64)
        keep[np.nonzero(sel)[0][texel[:, 3] < ALPHA_CUT]] = False

    colors = np.clip(colors * shade[:, None], 0, 255)
    pts, colors, sx, sy = pts[keep], colors[keep], sx[keep], sy[keep]
    if len(pts) == 0:
        return img

    ix = np.clip(np.floor(sx), 0, size - 1).astype(np.int64)
    iy = np.clip(np.floor(sy), 0, size - 1).astype(np.int64)
    flat = iy * size + ix

    order = np.argsort(-pts[:, 2], kind="stable")              # nearest first
    flat_sorted = flat[order]
    uniq, first = np.unique(flat_sorted, return_index=True)
    buf = img.reshape(-1, 3)
    buf[uniq] = colors[order][first].astype(np.uint8)
    _fill_holes(img, _mask(size, uniq))
    return img


def load_mesh_textures(mesh: P3DMesh, resolve_texture: Callable[[str], Path | None] | None,
                       cache: dict | None = None, texture_px: int = 128) -> dict[int, np.ndarray]:
    """Decode the .paa textures a mesh uses. Returns {texture index: RGBA array}.

    Names that are not .paa files (procedural textures), files that cannot be found, and
    textures that cannot be decoded are left out; those faces use the base colour.
    cache: optional dict path -> RGBA array or None, shared between models.
    """
    from .paa import read_paa

    out: dict[int, np.ndarray] = {}
    if resolve_texture is None:
        return out
    cache = {} if cache is None else cache
    for tid, name in enumerate(mesh.textures):
        if not name.lower().endswith(".paa"):
            continue
        src = resolve_texture(name)
        if src is None:
            continue
        if src not in cache:
            try:
                cache[src] = read_paa(src, texture_px)
            except Exception:  # unsupported or broken texture
                cache[src] = None
        if cache[src] is not None:
            out[tid] = cache[src]
    return out


def png_bytes(rgb: np.ndarray) -> bytes:
    """RGB uint8 image -> PNG file bytes (no Qt: thumbnails are written from a worker thread)."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def thumbnail_for_file(path: str | Path, size: int = 256,
                       resolve_texture: Callable[[str], Path | None] | None = None,
                       texture_px: int = 128) -> np.ndarray:
    """Render the best visual LOD of a .p3d file.

    resolve_texture maps a texture name from the model (e.g. 'a\\\\b\\\\x.paa') to a file on
    disk, or None. Textures that cannot be found or decoded fall back to the base colour.
    Raises P3DError on unreadable models.
    """
    mesh = read_p3d_mesh(path)
    textures = load_mesh_textures(mesh, resolve_texture, texture_px=texture_px)
    return render_thumbnail(mesh, size, textures=textures)
