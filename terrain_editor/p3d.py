"""Minimal reader for Arma/DayZ P3D models (MLOD, uncompressed).

Decoded from evelone_modular/*.p3d (verified on 8 files):
  'MLOD' u32 version, u32 lodCount, then per LOD:
  'P3DM' u32 version, u32 ?(256), u32 numPoints, u32 numNormals, u32 numFaces, u32 flags
  points  : numPoints * (f32 x, f32 y, f32 z, u32 flags)
  normals : numNormals * (f32 x, f32 y, f32 z)
  faces   : triangle/quad records and tagged sections (including named properties)
Mesh vertices are translated to the model origin used by Binarize and WRP.
The points-only reader returns the original file coordinates.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MLOD = b"MLOD"
P3DM = b"P3DM"


class P3DError(ValueError):
    pass


@dataclass
class P3DLod:
    index: int
    points: np.ndarray  # (N, 3) float32

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.points.min(axis=0), self.points.max(axis=0)


def read_p3d_points(path: str | Path) -> list[P3DLod]:
    """Return the vertex array of every LOD in an MLOD .p3d file."""
    raw = Path(path).read_bytes()
    if raw[:4] != MLOD:
        raise P3DError("not an MLOD .p3d file")
    _ver, lod_count = struct.unpack_from("<II", raw, 4)
    starts = []
    pos = 0
    while True:
        pos = raw.find(P3DM, pos)
        if pos < 0:
            break
        starts.append(pos)
        pos += 4
    if len(starts) < lod_count:
        raise P3DError("fewer P3DM headers than declared LODs")
    lods: list[P3DLod] = []
    for i, s in enumerate(starts[:lod_count]):
        _v, _res, npt, _nnr, _nfc, _fl = struct.unpack_from("<6I", raw, s + 4)
        end = s + 28 + npt * 16
        if end > len(raw):
            raise P3DError(f"LOD {i}: points run past end of file")
        arr = np.frombuffer(raw, dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                                  ("f", "<u4")]),
                            count=npt, offset=s + 28)
        pts = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float32)
        lods.append(P3DLod(i, pts))
    return lods


# ---- mesh (faces) ----------------------------------------------------------
# Face layout: u32 nVerts, nVerts*(u32 point, u32 normal, f32 u, f32 v),
# then a gap, then cstr texture and cstr material. The gap is 20 bytes in visual LODs
# (flags + 16 bytes) and 4 bytes in shadow LODs. Each LOD must end exactly at its
# tagged sections ('#EndOfFile#'), which is used to pick the gap per LOD.

@dataclass
class P3DMesh:
    vertices: np.ndarray   # (N, 3) float32
    triangles: np.ndarray  # (M, 3) uint32; n-gons are fan-triangulated
    resolution: float = 0.0  # LOD resolution: 0, 1, 2... = visual, 1e13+ = geometry/shadow/memory
    uvs: np.ndarray | None = None      # (M, 3, 2) float32 texture coords per triangle corner
    tex_ids: np.ndarray | None = None  # (M,) index into textures
    textures: list[str] = field(default_factory=list)  # texture names as stored in the model
    properties: dict[str, str] = field(default_factory=dict)
    source_center: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)


def _cstr(raw: bytes, o: int) -> tuple[str, int]:
    e = raw.index(b"\0", o)
    return raw[o:e].decode("latin1"), e + 1


def _adaptive_tail(raw: bytes, o: int, nv: int, last: bool) -> tuple[str, str, int]:
    """Texture and material names after a face's vertices, when the gap is not the same for
    every face. Seen in a DayZ model: triangles have a 20-byte gap, quads a 4-byte gap.
    The gap order follows the vertex count; a candidate is accepted only if the next face
    starts with a valid vertex count (3..64). The last face has no next face."""
    gaps = (20, 4, 0, 8, 12, 16, 24) if nv == 3 else (4, 20, 0, 8, 12, 16, 24)
    for gap in gaps:
        try:
            p = o + gap
            tex, p = _cstr(raw, p)
            mat, p = _cstr(raw, p)
        except (ValueError, IndexError):
            continue
        if last or (p + 4 <= len(raw) and 3 <= struct.unpack_from("<I", raw, p)[0] <= 64):
            return tex, mat, p
    raise P3DError("face tail not found")


def _parse_lod_mesh(raw: bytes, s: int, gap: int, adaptive: bool = False) -> P3DMesh:
    lod_end = _next_marker(raw, s)
    _v, _x, npt, nnr, nfc, _fl = struct.unpack_from("<6I", raw, s + 4)
    o = s + 28
    arr = np.frombuffer(raw, dtype=np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
                                              ("f", "<u4")]), count=npt, offset=o)
    pts = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float32)
    o += npt * 16 + nnr * 12
    tris: list[tuple[int, int, int]] = []
    uvs: list[list[tuple[float, float]]] = []
    tex_ids: list[int] = []
    textures: list[str] = []
    tex_index: dict[str, int] = {}
    for k in range(nfc):
        nv, = struct.unpack_from("<I", raw, o)
        o += 4
        if not 3 <= nv <= 64:
            raise P3DError("bad face vertex count")
        # face vertex: u32 point index, u32 normal index, f32 u, f32 v
        idx = [struct.unpack_from("<I", raw, o + 16 * k)[0] for k in range(nv)]
        uv = [struct.unpack_from("<ff", raw, o + 16 * k + 8) for k in range(nv)]
        if adaptive:
            tex, _mat, o = _adaptive_tail(raw, o + 16 * nv, nv, k == nfc - 1)
        else:
            o += 16 * nv + gap
            tex, o = _cstr(raw, o)
            _mat, o = _cstr(raw, o)
        tid = tex_index.setdefault(tex, len(textures))
        if tid == len(textures):
            textures.append(tex)
        for k in range(1, nv - 1):
            tris.append((idx[0], idx[k], idx[k + 1]))
            uvs.append([uv[0], uv[k], uv[k + 1]])
            tex_ids.append(tid)
    properties: dict[str, str] = {}
    while True:  # tagged sections
        if raw[o:o + 4] == b"TAGG":
            o += 4
        active = raw[o] != 0
        o += 1
        name, o = _cstr(raw, o)
        ln, = struct.unpack_from("<I", raw, o)
        o += 4
        if o + ln > lod_end - 4:
            raise P3DError("tag runs past end of LOD")
        if active and name == "#Property#" and ln == 128:
            key = raw[o:o + 64].split(b"\0", 1)[0].decode("latin1").strip().lower()
            value = raw[o + 64:o + 128].split(b"\0", 1)[0].decode("latin1").strip()
            properties[key] = value
        o += ln
        if name == "#EndOfFile#":
            break
    resolution, = struct.unpack_from("<f", raw, o)
    o += 4
    if o != lod_end:
        raise P3DError("LOD does not end at the next header")
    tri = np.array(tris, dtype=np.uint32).reshape(-1, 3)
    if tri.size and int(tri.max()) >= npt:
        raise P3DError("face index out of range")
    return P3DMesh(pts, tri, float(resolution),
                   uvs=np.array(uvs, dtype=np.float32).reshape(-1, 3, 2),
                   tex_ids=np.array(tex_ids, dtype=np.int32),
                   textures=textures, properties=properties)


def _next_marker(raw: bytes, s: int) -> int:
    n = raw.find(P3DM, s + 4)
    return len(raw) if n < 0 else n


def _lod_starts(raw: bytes) -> list[int]:
    starts: list[int] = []
    pos = 0
    while (pos := raw.find(P3DM, pos)) >= 0:
        starts.append(pos)
        pos += 4
    return starts


VISUAL_RES_LIMIT = 1e10  # resolutions below this are visual LODs; higher ones are geometry/shadow


def lod_resolutions(path: str | Path) -> list[float]:
    """Resolution of every LOD (float stored at the end of each LOD)."""
    raw = Path(path).read_bytes()
    if raw[:4] != MLOD:
        raise P3DError("not an MLOD .p3d file")
    starts = _lod_starts(raw)
    out = []
    for k, s in enumerate(starts):
        nxt = starts[k + 1] if k + 1 < len(starts) else len(raw)
        out.append(float(struct.unpack_from("<f", raw, nxt - 4)[0]))
    return out


def best_visual_lod(resolutions: list[float]) -> int:
    """Index of the highest-quality visual LOD (smallest resolution below the limit)."""
    cands = [(r, i) for i, r in enumerate(resolutions) if r < VISUAL_RES_LIMIT]
    if not cands:
        return 0
    return min(cands)[1]


def _read_lod_mesh(raw: bytes, s: int) -> P3DMesh:
    last: Exception | None = None
    for gap in (20, 4, 0, 8, 12, 16, 24):
        try:
            return _parse_lod_mesh(raw, s, gap)
        except (P3DError, ValueError, struct.error, IndexError) as exc:
            last = exc
    try:
        return _parse_lod_mesh(raw, s, 0, adaptive=True)
    except (P3DError, ValueError, struct.error, IndexError) as exc:
        last = exc
    raise P3DError(f"cannot decode LOD at {s} ({last})")


def _model_center(raw: bytes, starts: list[int], resolutions: list[float],
                  selected: int, mesh: P3DMesh) -> np.ndarray:
    """Binarize centers all LODs together unless Geometry has autocenter=0.

    A property on a visual LOD does not disable this. Verified with DayZ
    Tools Binarize (ODOL 55), including a Memory LOD outside the visual bounds.
    """
    for i, resolution in enumerate(resolutions):
        if np.isclose(resolution, 1e13, rtol=1e-6, atol=0):
            geometry = mesh if i == selected else _read_lod_mesh(raw, starts[i])
            if geometry.properties.get("autocenter") == "0":
                return np.zeros(3, dtype=np.float64)
            break

    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for i, s in enumerate(starts):
        npt, = struct.unpack_from("<I", raw, s + 12)
        end = starts[i + 1] if i + 1 < len(starts) else len(raw)
        if s + 28 + npt * 16 > end:
            raise P3DError(f"LOD {i}: points run past end of LOD")
        if not npt:
            continue
        # Every point record contains three floats and four flag bytes.
        points = np.frombuffer(raw, dtype="<f4", count=npt * 4, offset=s + 28).reshape(-1, 4)[:, :3]
        if not np.isfinite(points).all():
            raise P3DError(f"LOD {i}: non-finite vertex coordinates")
        lo = np.minimum(lo, points.min(axis=0))
        hi = np.maximum(hi, points.max(axis=0))
    if not np.isfinite(lo).all():
        raise P3DError("model contains no vertices")
    return (lo + hi) * 0.5


def read_p3d_mesh(path: str | Path, lod: int | None = None) -> P3DMesh:
    """Read one LOD in WRP-local coordinates; keep the file itself unchanged.

    lod=None picks the best visual LOD. Vertices use the same model origin
    as Binarize, so an instance position can be written to WRP directly.
    source_center records the translation removed from the MLOD vertices.
    """
    raw = Path(path).read_bytes()
    if raw[:4] != MLOD:
        raise P3DError("not an MLOD .p3d file")
    _ver, lod_count = struct.unpack_from("<II", raw, 4)
    starts = _lod_starts(raw)[:lod_count]
    if not starts or len(starts) != lod_count:
        raise P3DError("missing P3DM headers")
    res = [float(struct.unpack_from("<f", raw, (starts[k + 1] if k + 1 < len(starts)
                                                else len(raw)) - 4)[0])
           for k in range(len(starts))]
    if lod is None:
        lod = best_visual_lod(res)
    if lod < 0 or lod >= len(starts):
        raise P3DError(f"no LOD {lod}")
    mesh = _read_lod_mesh(raw, starts[lod])
    center = _model_center(raw, starts, res, lod, mesh)
    mesh.source_center = tuple(float(v) for v in center)
    mesh.vertices = (mesh.vertices - center).astype(np.float32)
    return mesh
