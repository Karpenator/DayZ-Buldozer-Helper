"""Слои Terrain Builder (*.tv4l): чтение, запись копий, перенос объектов по дереву."""
import struct
from pathlib import Path

import pytest

from terrain_editor.tv4l import (KIND_EMPTY, KIND_LEAVES, KIND_NODES, Leaf, Node, Tv4lError,
                                 Tv4lLayer, child_bbox, child_bit, find_layers_dir, load_layers,
                                 sdbm)

REAL_LAYERS = (Path(__file__).resolve().parents[2] / "Evelone2666" / "TerrainBuilder"
               / "Evelone2666.Layers")
INF = struct.pack("<f", float("-inf"))


def _table(section, entries):
    out = struct.pack("<HBI", section, 0x0F, len(entries))
    for i, name in entries:
        out += struct.pack("<HH", i, len(name)) + name.encode()
    return out


def _obj(x, z, yaw, oid, scale=1.0):
    return struct.pack("<2d5fI", x, z, yaw, 0.0, 0.0, 1.0, scale, oid)


def make_layer(objects_q0=(), objects_q3=()):
    """Маленький слой как у TB: корень 0..1024 (глубина 0) → узлы глубины 1 → листья (глубина 2).

    objects_q0 — объекты в листе (x<256, z>=768), objects_q3 — в листе (x>=768, z<256);
    элементы: (шаблон, [(x, z, курс, id), ...]).
    """
    head = b"ALB1" + struct.pack("<I", 1) + b"\0" * 8
    names = _table(2, [(2, "tags"), (10, "mname"), (34, "tree"), (27, "malpha")])
    classes = _table(3, [(1, "SRawData"), (5, "CLayer")])
    data = struct.pack("<HB", 5, 0x08) + b"\x01\x02\x03\x04"           # поле data (что-то до имени)
    data += struct.pack("<HBH", 10, 0x0B, 4) + b"Test"                  # mname = "Test"

    def leaf(bbox, groups):
        out = struct.pack("<4d", *bbox) + b"\xde\xad\xbe\xef" + b"\x12\x34\x56\x78"
        out += struct.pack("<I", len(groups))
        for tpl, objs in groups:
            out += struct.pack("<II", len(objs), tpl)
            for x, z, yaw, oid in objs:
                out += _obj(x, z, yaw, oid)
        return out

    root = (0.0, 0.0, 1024.0, 1024.0)
    tree = bytes([KIND_NODES]) + struct.pack("<4dI", *root, 0) + b"\0\0\0\0" + bytes([0b1001 | 0b0100])
    for bit in (0, 2, 3):
        nb = child_bbox(root, bit)
        if bit == 2:                                    # пустой узел
            tree += bytes([KIND_EMPTY]) + struct.pack("<4dI", *nb, 1) + b"\0\0\0\0" + bytes([0])
            continue
        groups = objects_q0 if bit == 0 else objects_q3
        leaf_bit = 0 if bit == 0 else 3
        tree += bytes([KIND_LEAVES]) + struct.pack("<4dI", *nb, 1) + INF + bytes([1 << leaf_bit])
        tree += leaf(child_bbox(nb, leaf_bit), groups)
    data += struct.pack("<HBII", 34, 0x0C, len(tree) + 4, len(tree)) + tree
    data += struct.pack("<HBf", 27, 0x0A, 1.0)                          # malpha после дерева
    return head + names + classes + data


SAMPLE = make_layer([(sdbm("EV_1"), [(100.0, 900.0, 90.0, 1), (110.0, 910.0, 0.0, 2)])],
                    [(sdbm("EV_2"), [(900.0, 100.0, 180.0, 3)])])


def test_sdbm_matches_terrain_builder_hashes():
    assert sdbm("EV_1") == 0xB50E9063 and sdbm("EV_2") == 0xB50E9064
    assert sdbm("EV3_2") == 0x097BE9B5 and sdbm("Evelone_modular") == 0x118073B9


def test_quadrant_bits():
    box = (0.0, 0.0, 100.0, 100.0)
    assert child_bit(box, 10, 90) == 0 and child_bit(box, 90, 90) == 1
    assert child_bit(box, 10, 10) == 2 and child_bit(box, 90, 10) == 3
    assert child_bbox(box, 1) == (50.0, 50.0, 100.0, 100.0)
    assert child_bbox(box, 2) == (0.0, 0.0, 50.0, 50.0)


def test_read_objects_and_round_trip_is_byte_exact():
    layer = Tv4lLayer(SAMPLE)
    objs = sorted(layer.objects, key=lambda o: o.id)
    assert [(o.id, o.x, o.z, round(o.yaw)) for o in objs] == [
        (1, 100.0, 900.0, 90), (2, 110.0, 910.0, 0), (3, 900.0, 100.0, 180)]
    assert objs[0].template == sdbm("EV_1") and objs[2].template == sdbm("EV_2")
    assert layer.leaf_depth == 2
    assert layer.to_bytes() == SAMPLE


def test_move_inside_leaf_and_into_new_branch():
    layer = Tv4lLayer(SAMPLE)
    o = layer.find(1)
    layer.move(o, 120.0, 950.0, 45.0)                    # тот же лист
    again = Tv4lLayer(layer.to_bytes())
    assert (again.find(1).x, again.find(1).z, round(again.find(1).yaw)) == (120.0, 950.0, 45)
    layer.move(layer.find(2), 300.0, 300.0)              # в пустой узел (бит 2) — ветка создаётся
    data = layer.to_bytes()
    again = Tv4lLayer(data)
    moved = again.find(2)
    assert (moved.x, moved.z) == (300.0, 300.0) and len(again.objects) == 3
    node = again.root.children[2]
    assert isinstance(node, Node) and node.kind == KIND_LEAVES
    assert isinstance(node.children[child_bit(node.bbox, 300.0, 300.0)], Leaf)
    tree_at = data.index(struct.pack("<HB", 34, 0x0C))
    size, inner = struct.unpack_from("<II", data, tree_at + 3)
    assert size == inner + 4 and data[tree_at + 7 + size:] == struct.pack("<HBf", 27, 0x0A, 1.0)
    layer.move(layer.find(3), 905.0, 120.0)
    assert Tv4lLayer(layer.to_bytes()).find(3).x == 905.0


def test_move_outside_layer_is_refused():
    layer = Tv4lLayer(SAMPLE)
    with pytest.raises(Tv4lError):
        layer.move(layer.find(1), 5000.0, 10.0)


def test_save_writes_copy_but_never_the_source(tmp_path):
    src = tmp_path / "proj.Layers" / "Walls.tv4l"
    src.parent.mkdir()
    src.write_bytes(SAMPLE)
    layer = Tv4lLayer.load(src)
    with pytest.raises(Tv4lError):
        layer.save(src)
    assert src.read_bytes() == SAMPLE
    layer.move(layer.find(3), 950.0, 50.0)
    out = layer.save(tmp_path / "out" / "Walls.tv4l")
    assert Tv4lLayer.load(out).find(3).x == 950.0 and src.read_bytes() == SAMPLE
    assert find_layers_dir(tmp_path) == src.parent
    assert [l.name for l in load_layers(src.parent)] == ["Walls"]


def test_not_a_layer():
    with pytest.raises(Tv4lError):
        Tv4lLayer(b"NOPE" + b"\0" * 64)


@pytest.mark.skipif(not REAL_LAYERS.is_dir(), reason="нет примеров слоёв Evelone2666.Layers")
def test_real_layers_round_trip_and_templates():
    counts = {}
    for path in sorted(REAL_LAYERS.glob("*.tv4l")):
        data = path.read_bytes()
        layer = Tv4lLayer(data, path)
        assert layer.to_bytes() == data, path.name
        counts[path.stem] = len(layer.objects)
        from terrain_editor.tbproject import AlbReader
        assert AlbReader(data).root(stop="tree").get("objectCount") == len(layer.objects), path.name
        if path.stem == "Walls":
            tpls = {o.template for o in layer.objects}
            assert {sdbm("EV_1"), sdbm("EV_2")} <= tpls
        # имена шаблонов — из заголовка слоя, у каждого объекта; кроме объектов, которые TB
        # импортировал из текста с именем не в том регистре («fusebox» у шаблона FuseBox):
        # хэш TB считает по тексту, а в заголовок слоя такое имя не пишет
        unknown = {o.template for o in layer.objects} - set(layer.template_names)
        assert unknown <= {sdbm("fusebox")}, path.name
        assert {o.elevation for o in layer.objects} and all(abs(o.scale - 1.0) < 1e-3 for o in layer.objects[:50])
    assert set(counts) >= {"Decorations", "Floors", "Roofs", "Walls"}


@pytest.mark.skipif(not REAL_LAYERS.is_dir(), reason="нет примеров слоёв Evelone2666.Layers")
def test_real_layer_move_to_far_leaf_and_back():
    path = REAL_LAYERS / "Floors.tv4l"
    layer = Tv4lLayer.load(path)
    o = layer.objects[3]
    x, z, yaw = o.x, o.z, o.yaw
    layer.move(o, x + 400.0, z - 250.0, yaw + 90.0)
    again = Tv4lLayer(layer.to_bytes())
    moved = again.find(o.id)
    assert abs(moved.x - (x + 400.0)) < 1e-9 and abs(moved.yaw - (yaw + 90.0) % 360.0) < 1e-4
    assert len(again.objects) == 208


def _broken_sample(kind=0x04):
    """SAMPLE, где у узла с листом (первый потомок корня) испорчен вид — как в слоях, которые
    программа не понимает."""
    nb = child_bbox((0.0, 0.0, 1024.0, 1024.0), 0)
    at = SAMPLE.find(bytes([KIND_LEAVES]) + struct.pack("<4d", *nb))
    assert at > 0
    bad = bytearray(SAMPLE)
    bad[at] = kind
    return bytes(bad), at


def test_parse_error_carries_offset_and_last_nodes():
    bad, at = _broken_sample()
    with pytest.raises(Tv4lError) as err:
        Tv4lLayer(bad)
    assert "0x04" in str(err.value)
    assert err.value.pos == at + 42                     # узел прочитан целиком, сбой на потомке
    assert err.value.trail[-1] == (at, "узел", 0x04, 1, 0b0001)
    assert [t[1] for t in err.value.trail] == ["узел", "узел"]


def test_diagnose_reports_tree_and_bytes_around_failure():
    from terrain_editor.tv4l import diagnose
    bad, at = _broken_sample()
    text = "\n".join(diagnose(bad))
    assert "tree(0x22)" in text
    assert "tree тип 0x0c" in text
    assert "неизвестный вид узла 0x04" in text
    assert f"{at} узел 0x04 1 0001" in text
    assert f"{at - at % 16:08x}:" in text                # байты вокруг места сбоя
    assert diagnose(SAMPLE)[-1] == "разобран: 3 объектов"
    assert diagnose(b"nope")[0].startswith("размер 4")


@pytest.mark.skipif(not REAL_LAYERS.is_dir(), reason="нет примеров слоёв Evelone2666.Layers")
def test_diagnose_real_layer_header():
    from terrain_editor.tv4l import diagnose
    text = "\n".join(diagnose((REAL_LAYERS / "Walls.tv4l").read_bytes()))
    assert "mname:0x0b@" in text and "=Walls" in text
    assert "mlayerVersion:0x05@" in text and "=4" in text
    assert text.endswith("объектов")


def test_layer_that_does_not_rebuild_byte_exact_is_refused(monkeypatch):
    """Если разобранное дерево собирается в другие байты (незнакомый вариант формата),
    слой не принимается: его нельзя будет записать без порчи."""
    orig = Tv4lLayer._write_node

    def lossy(self, n, out):
        orig(self, n, out)
        if n.depth == 0:
            out += b"\0"
    monkeypatch.setattr(Tv4lLayer, "_write_node", lossy)
    with pytest.raises(Tv4lError, match="не собирается обратно"):
        Tv4lLayer(SAMPLE)


def as_layer_v5(layer):
    """Тот же слой в формате DayZ Tools 1.30 (mlayerVersion 5: у объекта 4 байта после id)."""
    import terrain_editor.tv4l as tv
    data = bytearray(layer._data)
    ids = {v: k for k, v in tv._name_table(bytes(data), 16)[0].items()}
    at = data.find(struct.pack("<HB", ids["mlayerVersion"], 0x05))
    assert struct.unpack_from("<i", data, at + 3)[0] == 4
    struct.pack_into("<i", data, at + 3, 5)
    layer._data = bytes(data)
    layer._tail = tv.OBJECT_TAIL_V5
    for o in layer.objects:
        o.tail = bytes([o.id & 0xFF, 0, 0, 0])     # не нули — проверить, что байты сохраняются
    layer._header_dirty = True
    return Tv4lLayer(layer.to_bytes(), layer.path)


@pytest.mark.skipif(not REAL_LAYERS.is_dir(), reason="нет слоёв карты")
def test_layer_version_5_of_dayz_tools_130_objects_have_4_more_bytes(tmp_path):
    orig = Tv4lLayer.load(REAL_LAYERS / "Roofs.tv4l")
    assert orig.version == 4
    v5 = as_layer_v5(Tv4lLayer.load(REAL_LAYERS / "Roofs.tv4l"))
    assert v5.version == 5
    assert len(v5._data) == len(orig._data) + 4 * len(orig.objects)
    assert [(o.id, o.x, o.z, o.floats) for o in v5.objects] == \
        [(o.id, o.x, o.z, o.floats) for o in orig.objects]
    assert all(o.tail == bytes([o.id & 0xFF, 0, 0, 0]) for o in v5.objects)
    assert v5.to_bytes() == v5._data                      # пересобирается байт в байт
    name = next(iter(v5.template_names.values()))
    first = v5.objects[0]
    new = v5.add_object(name, v5.libraries[0], first.x + 1.0, first.z + 1.0, 30.0, elevation=0.5)
    assert new.tail == b"\0\0\0\0"
    again = Tv4lLayer(v5.to_bytes())
    assert again.version == 5 and len(again.objects) == len(orig.objects) + 1
    got = again.find(new.id)
    assert got is not None and abs(got.yaw - 30.0) < 1e-4 and abs(got.elevation - 0.5) < 1e-6
    assert got.tail == b"\0\0\0\0"


def test_real_v5_object_bytes_from_user_log_decode():
    """Объект из слоя Roofs пользователя (TB 1.30): x/z, масштаб 1, высота ~1, id 15, 4 нуля."""
    raw = bytes.fromhex("f1a162d305971e41 5acb3507aa0f1b41 00000000 00000000 00000000 0000803f "
                        "feff7f3f 0f000000 00000000".replace(" ", ""))
    assert len(raw) == 40 + 4
    x, z, yaw, pitch, roll, scale, rel, oid = struct.unpack_from("<2d5fI", raw)
    assert 501185 < x < 501186 and 443370 < z < 443371
    assert (yaw, pitch, roll, scale, oid) == (0.0, 0.0, 0.0, 1.0, 15) and abs(rel - 1.0) < 1e-6
