"""Проект Terrain Builder (*.tv4p): рамка карты, сдвиги центров моделей, перевод координат."""
import math
import struct
from pathlib import Path

import pytest

from terrain_editor.map_edits import MapEdit, save_edits
from terrain_editor.tbproject import (AlbError, AlbReader, TbProject, find_project_file,
                                      load_project, norm_model, rotate_delta)
from terrain_editor.tv4l import Tv4lLayer, sdbm

from test_tv4l import _obj, _table, make_layer

REAL = Path(__file__).resolve().parents[2] / "Evelone2666"
REAL_TV4P = REAL / "TerrainBuilder" / "Evelone2666.tv4p"
need_real = pytest.mark.skipif(not REAL_TV4P.is_file(), reason="нет проекта Evelone2666")
# проект до обновления DayZ Tools 1.30: рамка карты 500000/442127 — с ней сохранены слои и .wrp
# (после 1.30 в проекте рамка 200000/0, а объекты слоёв остались на месте)
FRAME500K = Path(__file__).resolve().parent / "data" / "Evelone2666_frame500k.tv4p"

# объекты из Evelone2666.wrp (так их ставит Buldozer: начало модели) и они же в слоях (центр)
WRP_VS_LAYER = [
    ("evelone_modular\\forklift\\forklift.p3d", 1189.14453125, 1175.3988037109375, 225.405517578125,
     501188.75007003645, 443302.4001482852),
    ("evelone_modular\\fans\\fan_v1.p3d", 1187.1370849609375, 1184.145263671875, 179.90721130371094,
     501187.1451959974, 443311.6807107456),
    ("evelone_modular\\2\\ev2_2.p3d", 1234.010009765625, 1174.4698486328125, 90.0,
     501234.75085032085, 443301.8150515646),
]


def project(deltas=None, frame=(500.0, 300.0, 1524.0, 1324.0)):
    return TbProject(None, "Test", frame, {norm_model(k): v for k, v in (deltas or {}).items()})


def test_rotate_delta_matches_dayz_object_axes():
    # курс 90°: ось X модели смотрит на −Z мира, ось Z модели — на +X
    rx, rz = rotate_delta(1.0, 0.0, 90.0)
    assert abs(rx) < 1e-12 and abs(rz + 1.0) < 1e-12
    rx, rz = rotate_delta(0.0, 1.0, 90.0)
    assert abs(rx - 1.0) < 1e-12 and abs(rz) < 1e-12


def test_to_layer_and_back():
    p = project({"P:\\Evelone\\Box.p3d": (0.0, -0.5)})
    lx, lz = p.to_layer(100.0, 200.0, 90.0, "evelone/box.p3d")
    assert (round(lx, 9), round(lz, 9)) == (600.5, 500.0)     # центр сдвинут на R(90°)·(0, −0.5)
    assert p.from_layer(lx, lz, 90.0, "EVELONE\\BOX.P3D") == pytest.approx((100.0, 200.0))
    assert p.to_layer(1.0, 2.0, 33.0, "dz\\other.p3d") == (501.0, 302.0)   # нет delta — без сдвига


def _alb(fields, classes, body):
    return b"ALB1" + struct.pack("<I", 1) + b"\0" * 8 + _table(2, fields) + _table(3, classes) + body


def _f64s(fid, values):
    return struct.pack("<HBB", fid, 0x15, len(values)) + struct.pack(f"<{len(values)}d", *values)


def _obj_field(fid, cls, inner):
    return struct.pack("<HBIHI", fid, 0x0D, len(inner) + 6, cls, 0) + inner


def _list(fid, items):
    inner = struct.pack("<I", len(items)) + b"".join(items)
    return struct.pack("<HBI", fid, 0x0C, len(inner)) + inner


def tiny_tv4p(frame=(500000.0, 442127.39, 505120.0, 447247.39)):
    """Как у TB: objManager { mapFrame { mframe [SMapFrame{mpos}] }, mobjectsDelta [pair] }."""
    f = [(1, "mname"), (2, "objManager"), (3, "mapFrame"), (4, "mframe"), (5, "item"), (6, "mpos"),
         (7, "mobjectsDelta"), (8, "pair"), (9, "key"), (10, "data"), (11, "x"), (12, "z"), (13, "libs")]
    c = [(1, "CProject"), (2, "CObjManager"), (3, "CMapFrames"), (4, "SMapFrame"),
         (5, "SObjectCenterDelta")]
    frame_obj = _obj_field(5, 4, _f64s(6, frame))
    frames = _obj_field(3, 3, _list(4, [frame_obj]))
    key = struct.pack("<HBH", 9, 0x0B, len(b"Dz\\X\\Forklift.p3d")) + b"Dz\\X\\Forklift.p3d"
    delta = _obj_field(10, 5, struct.pack("<HBd", 11, 0x14, 0.25) + struct.pack("<HBd", 12, 0x14, -0.5))
    pair = struct.pack("<HB", 8, 0x0F) + key + delta
    om = _obj_field(2, 2, frames + _list(7, [pair]))
    libs = _list(13, [struct.pack("<HBH", 5, 0x0B, 11) + b"Vanilla.tml"])
    body = struct.pack("<HI", 1, 0) + struct.pack("<HBH", 1, 0x0B, 4) + b"Demo" + libs + om
    return _alb(f, c, body)


def test_tiny_project_parsed():
    p = TbProject.from_bytes(tiny_tv4p())
    assert p.name == "Demo" and p.frame == (500000.0, 442127.39, 505120.0, 447247.39)
    assert p.deltas == {"dz\\x\\forklift.p3d": (0.25, -0.5)} and p.libs == ["Vanilla.tml"]
    assert p.origin == (500000.0, 442127.39)


def test_not_a_project():
    with pytest.raises(AlbError):
        TbProject.from_bytes(b"NOPE" + b"\0" * 40)
    with pytest.raises(AlbError):          # слой — не проект (нет objManager)
        TbProject.from_bytes(make_layer())


def test_find_project_file(tmp_path):
    root = tmp_path / "Evelone2666"
    tb = root / "TerrainBuilder"
    (tb / "Evelone2666.Layers").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "data" / "junk.tv4p").write_bytes(b"x")       # в data не ищем
    assert find_project_file(root) is None
    (tb / "Evelone2666.tv4p").write_bytes(tiny_tv4p())
    assert find_project_file(root) == tb / "Evelone2666.tv4p"
    assert find_project_file(tb / "Evelone2666.Layers") == tb / "Evelone2666.tv4p"
    assert find_project_file(tb / "Evelone2666.tv4p") == tb / "Evelone2666.tv4p"
    assert find_project_file(tmp_path / "nope") is None and find_project_file("") is None
    assert load_project(root).name == "Demo"
    (tb / "Evelone2666.tv4p").write_bytes(b"ALB1broken")
    assert load_project(root) is None


def names(model):
    return {"evelone\\box.p3d": ["BOX"], "evelone\\pt.p3d": ["PT"]}.get(model.lower(), [])


def test_save_edits_with_project_rotates_around_model_origin(tmp_path):
    p = project({"evelone\\box.p3d": (0.0, -0.5), "evelone\\pt.p3d": (0.3, 0.0)})
    # слой: BOX стоит с курсом 0 — центр = начало + рамка − (0, −0.5); PT — как точка (без delta)
    data = make_layer([(sdbm("BOX"), [(600.0, 500.5, 0.0, 1), (600.0, 510.5, 0.0, 2)])],
                      [(sdbm("PT"), [(1000.0, 400.0, 0.0, 3)])])
    (tmp_path / "Walls.tv4l").write_bytes(data)
    layers = [Tv4lLayer.load(tmp_path / "Walls.tv4l")]
    box = MapEdit("evelone\\box.p3d", 100.0, 210.0, 0.0, x=102.0, z=210.0, yaw=90.0, lift=0.25)
    pt = MapEdit("evelone\\pt.p3d", 500.0, 100.0, 0.0, x=501.0, z=100.0, yaw=0.0)
    rep = save_edits(layers, [box, pt], tmp_path / "out", names, project=p)
    assert rep.moved == 2 and not rep.not_found and rep.no_delta == 1 and rep.lifted == 1
    out = Tv4lLayer.load(rep.written[0])
    b = out.find(2)                                 # тот, что в 210 (не соседний в 200)
    assert (b.x, b.z) == pytest.approx(p.to_layer(102.0, 210.0, 90.0, box.model))
    assert abs(b.yaw - 90.0) < 1e-4 and abs(b.elevation - 1.25) < 1e-6
    assert (out.find(1).x, out.find(1).z) == (600.0, 500.5)
    assert (out.find(3).x, out.find(3).z) == (1001.0, 400.0)   # без delta — просто сдвиг


def test_save_edits_with_project_rejects_far_objects(tmp_path):
    p = project({"evelone\\box.p3d": (0.0, -0.5)})
    (tmp_path / "Walls.tv4l").write_bytes(make_layer([(sdbm("BOX"), [(600.0, 500.5, 0.0, 1)])]))
    layers = [Tv4lLayer.load(tmp_path / "Walls.tv4l")]
    near = MapEdit("evelone\\box.p3d", 100.2, 200.0, 0.0, x=101.0, z=200.0, yaw=0.0)
    rep = save_edits(layers, [near], tmp_path / "out", names, project=p)
    assert rep.moved == 0 and rep.not_found == [near] and not rep.written   # 20 см — не он
    turned = MapEdit("evelone\\box.p3d", 100.0, 200.0, 45.0, x=101.0, z=200.0, yaw=45.0)
    rep = save_edits(layers, [turned], tmp_path / "out", names, project=p)
    assert rep.moved == 0                                    # курс не тот


@need_real
def test_real_project_frame_and_deltas():
    now = load_project(REAL)
    assert now.path == REAL_TV4P and now.name.lower() == "evelone2666"
    assert len(now.deltas) == 70 and len(now.libs) == 2
    p = TbProject.load(FRAME500K)
    assert p.name == "Evelone2666"
    assert p.frame == pytest.approx((500000.0, 442127.3902531095, 505120.0, 447247.3902531095))
    assert len(p.deltas) == 70 and len(p.libs) == 2
    assert p.delta("P:\\evelone_modular\\forklift\\forklift.p3d") == pytest.approx((-3.5e-6, -0.55394), abs=1e-4)


@need_real
def test_real_wrp_positions_match_layers():
    p = TbProject.load(FRAME500K)
    for model, x, z, yaw, lx, lz in WRP_VS_LAYER:
        gx, gz = p.to_layer(x, z, yaw, model)
        assert math.hypot(gx - lx, gz - lz) < 0.001, model
        bx, bz = p.from_layer(lx, lz, yaw, model)
        assert math.hypot(bx - x, bz - z) < 0.001, model


@need_real
def test_real_layers_edit_saved_exactly(tmp_path):
    from terrain_editor.tv4l import load_layers
    layers = load_layers(REAL / "TerrainBuilder" / "Evelone2666.Layers")
    p = TbProject.load(FRAME500K)
    model, x, z, yaw, lx, lz = WRP_VS_LAYER[0]
    e = MapEdit(model, x, z, yaw, x=x + 3.0, z=z - 1.5, yaw=yaw + 30.0, lift=0.4)

    rep = save_edits(layers, [e], tmp_path, None, project=p)    # шаблон Forklift — без регистра
    assert rep.moved == 1 and [w.name for w in rep.written] == ["Decorations.tv4l"]
    out = Tv4lLayer.load(rep.written[0])
    want = p.to_layer(e.x, e.z, e.yaw, model)
    o = min(out.objects, key=lambda o: math.hypot(o.x - want[0], o.z - want[1]))
    assert math.hypot(o.x - want[0], o.z - want[1]) < 1e-4
    assert abs(o.yaw - (yaw + 30.0) % 360.0) < 1e-3 and abs(o.elevation - 1.4) < 1e-5
    assert len(out.objects) == len(next(lay for lay in layers if lay.name == "Decorations").objects)


def test_alb_reader_keeps_unknown_blobs_raw():
    r = AlbReader(make_layer([(sdbm("EV_1"), [(100.0, 900.0, 0.0, 1)])]))
    assert r.fields[34] == "tree"


def test_obj_helper_layout():
    assert len(_obj(1.0, 2.0, 3.0, 4)) == 40


@need_real
def test_frame_moved_in_project_is_reported():
    """После 1.30 рамка карты в проекте — 200000/0, а объекты слоёв — около 500000/442127."""
    from terrain_editor.layer_sync import frame_check
    from terrain_editor.tv4l import load_layers
    layers = load_layers(REAL / "TerrainBuilder" / "Evelone2666.Layers")
    text = frame_check(layers, load_project(REAL))
    assert "(200000, 0)" in text and "внутри рамки 0 из" in text and "Mapframe" in text
    assert frame_check(layers, TbProject.load(FRAME500K)) == ""


def test_project_choice_prefers_last_opened_then_newest(tmp_path):
    import os
    from terrain_editor.tbproject import last_tb_project
    tb = tmp_path / "TerrainBuilder"
    tb.mkdir()
    data = FRAME500K.read_bytes()
    for name in ("Map.tv4p", "Map_2.tv4p"):
        (tb / name).write_bytes(data)
    os.utime(tb / "Map.tv4p", (2_000_000_000, 2_000_000_000))       # «новее»
    os.utime(tb / "Map_2.tv4p", (1_000_000_000, 1_000_000_000))
    assert find_project_file(tmp_path).name == "Map.tv4p"
    (tb / "Dialogs_new.vdat").write_text("\n\ndtExportWRP\nP:\\Map\\world\\Map.wrp\ndtProject\n"
                                         "P:\\Map\\TerrainBuilder\\map_2.tv4p\n", encoding="utf-8")
    assert last_tb_project(tb) == "map_2.tv4p"
    assert find_project_file(tmp_path).name == "Map_2.tv4p"          # открыт в TB последним
    # папка слоёв — того же проекта
    from terrain_editor.tv4l import find_layers_dir
    for name in ("Map.Layers", "Map_2.Layers"):
        (tb / name).mkdir()
        (tb / name / "A.tv4l").write_bytes(make_layer())
    assert find_layers_dir(tmp_path).name == "Map_2.Layers"
