"""Рабочая копия слоёв TB: новые объекты в слое (заголовок, id, шаблоны), пересборка, применение."""
import math
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from terrain_editor import layer_sync as ls
from terrain_editor.buldozer_bridge import PlacedObject
from terrain_editor.map_edits import MapEdit
from terrain_editor.tbproject import AlbReader, TbProject, load_project
from terrain_editor.templates import Template
from terrain_editor.tv4l import Tv4lError, Tv4lLayer, load_layers, sdbm

from test_tv4l import REAL_LAYERS, make_layer

REAL_PROJECT = REAL_LAYERS.parent
needs_real = pytest.mark.skipif(not REAL_LAYERS.is_dir(), reason="нет проекта Evelone2666")
FRAME500K = Path(__file__).resolve().parent / "data" / "Evelone2666_frame500k.tv4p"
FORKLIFT = "evelone_modular\\forklift\\forklift.p3d"
FORKLIFT_LAYER = (501188.75007003645, 443302.4001482852)       # Decorations, как поставил TB


def _real_copy(tmp_path: Path, names=("Floors",)) -> Path:
    tb = tmp_path / "TerrainBuilder"
    layers = tb / "Demo.Layers"
    layers.mkdir(parents=True)
    for n in names:
        shutil.copy(REAL_LAYERS / f"{n}.tv4l", layers / f"{n}.tv4l")
    # проект с той рамкой карты, с которой сохранены эти слои (в новом проекте рамку сдвинули)
    shutil.copy(FRAME500K, tb / "Demo.tv4p")
    return layers


@needs_real
def test_header_rebuild_is_byte_identical_and_add_updates_counters():
    for layer in load_layers(REAL_LAYERS):
        assert layer.can_add
        layer._header_dirty = True
        assert layer.to_bytes() == layer._data, layer.name           # те же байты, что у TB
        n, counter = len(layer.objects), layer.id_counter
        obj = layer.add_object("Land_New_Thing", "NewLib", 501000.0, 443000.0, 370.0, 1.5, 0.25)
        assert obj.id == counter and layer.id_counter == counter + 1
        again = Tv4lLayer(layer.to_bytes())
        assert len(again.objects) == n + 1 and again.id_counter == counter + 1
        assert AlbReader(again._data).root(stop="tree").get("objectCount") == n + 1
        got = again.find(obj.id)
        assert (got.x, got.z) == (501000.0, 443000.0) and abs(got.yaw - 10.0) < 1e-4
        assert abs(got.scale - 1.5) < 1e-6 and abs(got.elevation - 0.25) < 1e-6
        assert again.template_names[got.template] == "Land_New_Thing"
        assert "NewLib" in again.libraries
        names = [nm for nm, _h in again._lists[1]]
        assert names == sorted(names)                                # TB держит шаблоны по алфавиту
        assert dict(again._lists[1])["Land_New_Thing"] == sdbm("NewLib")
        again.remove(again.find(obj.id))
        assert len(Tv4lLayer(again.to_bytes()).objects) == n


def test_add_refused_without_parsed_header():
    layer = Tv4lLayer(make_layer([(sdbm("Rock"), [(100.0, 900.0, 0.0, 1)])]))
    assert not layer.can_add
    with pytest.raises(Tv4lError):
        layer.add_object("Rock", "Rocks", 100.0, 900.0)


@needs_real
def test_new_object_lands_exactly_where_tb_would_put_it(tmp_path):
    layers = _real_copy(tmp_path, ("Decorations",))
    project = load_project(layers)
    # Forklift в .wrp: начало (Buldozer) 1189.14453 / 1175.39880, курс 225.4055 → в слое TB:
    # на 10 м восточнее (на самом месте Forklift уже стоит — такой объект не добавляется дважды)
    obj = PlacedObject(1, FORKLIFT, 1199.14453125, 1175.3988037109375, 225.405517578125, 1.0, tb_rel=0.5)
    tpl = Template("Forklift", FORKLIFT, "Evelone_modular", (-1.0, 0.0, -2.0), (1.4, 2.0, 2.2))
    for pr in (project, replace(project, deltas={})):                # delta из проекта / по габаритам
        if not pr.deltas:
            from terrain_editor.templates import TemplateCatalog
            cat = TemplateCatalog()
            cat.load(REAL_PROJECT.parent)
            tpl = cat.by_model(FORKLIFT)
        out = tmp_path / f"out{len(pr.deltas)}"
        rep = ls.sync(layers, [obj], [], "decorations", lambda m: tpl, pr, out_dir=out)
        assert rep.added == 1 and rep.target == "decorations" and not rep.skipped
        layer = Tv4lLayer.load(out / "Decorations.tv4l")
        new = layer.find(Tv4lLayer.load(layers / "Decorations.tv4l").id_counter)   # mobjectIDcounter
        assert math.hypot(new.x - FORKLIFT_LAYER[0] - 10.0, new.z - FORKLIFT_LAYER[1]) < 1e-5
        assert abs(new.elevation - 0.5) < 1e-6 and layer.template_names[new.template] == "Forklift"
    assert (layers / "Decorations.tv4l").read_bytes() == (REAL_LAYERS / "Decorations.tv4l").read_bytes()


def test_elevation_fallback_uses_template_bottom():
    tpl = Template("T", "a\\t.p3d", "L", (-1.0, -0.4, -1.0), (1.0, 2.0, 1.0))
    obj = PlacedObject(1, "a\\t.p3d", 0.0, 0.0, 0.0, 2.0, lift=0.3)
    assert ls.placed_elevation(obj, tpl) == pytest.approx(0.3 + 0.8)
    assert ls.template_delta(tpl) == (0.0, 0.0)
    assert ls.model_delta("a\\t.p3d", None, Template("T", "", "L", (0, 0, 0), (2, 1, 4))) == (1.0, 2.0)


@needs_real
def test_sync_rebuilds_from_originals_and_removes_stale_copies(tmp_path):
    layers = _real_copy(tmp_path, ("Floors", "Walls"))
    project = load_project(layers)
    tpl = Template("Thing", "x\\thing.p3d", "Evelone_modular", (-1, 0, -1), (1, 1, 1))
    objs = [PlacedObject(1, "x\\thing.p3d", 100.0, 200.0, 0.0, 1.0),
            PlacedObject(2, "x\\thing.p3d", 110.0, 200.0, 90.0, 1.0),
            PlacedObject(3, "x\\missing.p3d", 120.0, 200.0, 0.0, 1.0)]
    find = {"x\\thing.p3d": tpl}.get
    rep = ls.sync(layers, objs, [], "Floors", find, project)
    work = ls.work_dir(layers)
    assert work == tmp_path / "TerrainBuilder" / "WE_edited_layers" / "current"
    assert rep.added == 2 and [p.name for p in rep.written] == ["Floors.tv4l"]
    assert rep.skipped == [("x\\missing.p3d", "модели нет в библиотеках шаблонов TB")]
    first = (work / "Floors.tv4l").read_bytes()
    ls.sync(layers, objs, [], "Floors", find, project)                # повтор — не удваивает
    assert (work / "Floors.tv4l").read_bytes() == first
    assert len(Tv4lLayer(first).objects) == 208 + 2
    rep = ls.sync(layers, objs[:1], [], "Walls", find, project)       # сменили слой
    assert [p.name for p in rep.written] == ["Walls.tv4l"] and [p.name for p in rep.removed] == ["Floors.tv4l"]
    rep = ls.sync(layers, [], [], "Walls", find, project)             # всё отменили — копии нет
    assert not rep.written and not list(work.glob("*.tv4l")) and rep.message == "изменений нет"


def test_sync_reports_missing_layer_and_project(tmp_path):
    layers = tmp_path / "P.Layers"
    layers.mkdir()
    (layers / "Walls.tv4l").write_bytes(make_layer([(sdbm("Rock"), [(100.0, 900.0, 0.0, 1)])]))
    obj = PlacedObject(1, "a.p3d", 1.0, 2.0)
    rep = ls.sync(layers, [obj], [], "Roads", lambda m: None, None, out_dir=tmp_path / "o")
    assert "нет слоя «Roads»" in rep.error and rep.skipped
    rep = ls.sync(layers, [obj], [], "Walls", lambda m: None, None, out_dir=tmp_path / "o")
    assert "tv4p" in rep.error
    project = TbProject(Path("x.tv4p"), "x", (0.0, 0.0, 10.0, 10.0), {}, [])
    rep = ls.sync(layers, [obj], [], "Walls", lambda m: Template("A", "a.p3d", "L"), project,
                  out_dir=tmp_path / "o")
    assert rep.added == 0 and "нельзя добавить" in rep.skipped[0][1]   # заголовок не разобран
    # правки объектов карты (без проекта — сдвиг по образцу) работают и в этом слое
    edit = MapEdit("rock.p3d", 60.0, 880.0, 0.0, x=62.0, z=881.0, yaw=0.0)
    rep = ls.sync(layers, [], [edit], "Walls", lambda m: None, None, lambda m: ["Rock"],
                  offset=(40.0, 20.0), out_dir=tmp_path / "o")
    assert rep.moved == 1 and [p.name for p in rep.written] == ["Walls.tv4l"]


def test_apply_to_project_backs_up_and_replaces(tmp_path):
    layers = tmp_path / "TB" / "P.Layers"
    layers.mkdir(parents=True)
    old = make_layer([(sdbm("Rock"), [(100.0, 900.0, 0.0, 1)])])
    new = make_layer([(sdbm("Rock"), [(101.0, 900.0, 0.0, 1)])])
    (layers / "Walls.tv4l").write_bytes(old)
    (layers / "Roofs.tv4l").write_bytes(old)
    work = ls.work_dir(layers)
    work.mkdir(parents=True)
    (work / "Walls.tv4l").write_bytes(new)
    done, backup = ls.apply_to_project(work, layers, now=0)
    assert done == [layers / "Walls.tv4l"] and (layers / "Walls.tv4l").read_bytes() == new
    assert (layers / "Roofs.tv4l").read_bytes() == old                  # не тронут
    assert (backup / "Walls.tv4l").read_bytes() == old and backup.parent.name == ls.BACKUP_ROOT
    (work / "Walls.tv4l").write_bytes(b"garbage")
    with pytest.raises(Exception):
        ls.apply_to_project(work, layers)                              # битый файл в проект не идёт
    assert (layers / "Walls.tv4l").read_bytes() == new
    assert ls.layer_names(layers) == ["Roofs", "Walls"] and ls.layer_names(None) == []


def test_busy_layer_file_is_read_from_copy(tmp_path, monkeypatch):
    layers = tmp_path / "TB" / "P.Layers"
    layers.mkdir(parents=True)
    data = make_layer([(sdbm("Rock"), [(100.0, 900.0, 0.0, 1)])])
    (layers / "Walls.tv4l").write_bytes(data)
    (layers / "Roofs.tv4l").write_bytes(data)
    got, errors, cached = ls.read_layers(layers)              # читается — копии сделаны
    assert [x.name for x in got] == ["Roofs", "Walls"] and not errors and not cached
    assert (ls.originals_dir(layers) / "Walls.tv4l").read_bytes() == data
    real = Path.read_bytes

    def busy(self):
        if self.parent == layers:
            raise PermissionError(13, "Отказано в доступе")    # TB держит файл
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", busy)
    got, errors, cached = ls.read_layers(layers)
    assert [x.name for x in got] == ["Roofs", "Walls"] and cached == ["Roofs.tv4l", "Walls.tv4l"]
    assert got[1].path == layers / "Walls.tv4l" and not errors
    monkeypatch.setattr(Path, "read_bytes", real)
    (layers / "Walls.tv4l").write_bytes(data + b"")           # файл поменялся (TB сохранил)
    import os
    os.utime(layers / "Walls.tv4l", ns=(1, 1))
    monkeypatch.setattr(Path, "read_bytes", busy)
    got, errors, cached = ls.read_layers(layers)
    assert [x.name for x in got] == ["Roofs"] and "Walls.tv4l: файл не открывается" in errors[0]
    # сборка: без нужного слоя ничего не пишет и копию не трогает, причина — в отчёте
    rep = ls.sync(layers, [PlacedObject(1, "a.p3d", 1.0, 2.0)], [], "Walls", lambda m: None, None)
    assert "слой не прочитан: Walls.tv4l" in rep.error and not rep.written
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError(13, "x"))
                        if self.parent == layers else real(self))
    (ls.originals_dir(layers) / "meta.json").unlink()
    rep = ls.sync(layers, [], [], "Walls", lambda m: None, None)
    assert rep.error.startswith("слои не прочитаны:") and len(rep.load_errors) == 2


def test_unparsed_layer_is_reported_and_diagnosed_once_per_version(tmp_path, caplog):
    import logging
    from test_tv4l import _broken_sample
    layers = tmp_path / "P.Layers"
    layers.mkdir()
    (layers / "Good.tv4l").write_bytes(make_layer([(sdbm("Rock"), [(100.0, 900.0, 0.0, 1)])]))
    bad, _at = _broken_sample()
    (layers / "test.tv4l").write_bytes(bad)
    with caplog.at_level(logging.WARNING, logger=ls.log.name):
        got, errors, _cached = ls.read_layers(layers)
        ls.read_layers(layers)                          # та же версия файла — второй раз не пишем
    assert [lay.name for lay in got] == ["Good"]
    assert errors == ["test.tv4l: не разобран (неизвестный вид узла 0x04)"]
    notes = [r.getMessage() for r in caplog.records if "not parsed" in r.getMessage()]
    assert len(notes) == 1
    assert "последние узлы" in notes[0] and "байты вокруг смещения" in notes[0]



@needs_real
def test_objects_already_saved_by_tb_are_not_added_twice(tmp_path):
    """Объект вставили в открытый TB и проект сохранили — в рабочую копию он второй раз не идёт."""
    layers = _real_copy(tmp_path, ("Decorations",))
    project = load_project(layers)
    obj = PlacedObject(1, FORKLIFT, 1189.14453125, 1175.3988037109375, 225.405517578125, 1.0, tb_rel=0.0)
    tpl = Template("Forklift", FORKLIFT, "Evelone_modular", (-1.0, 0.0, -2.0), (1.4, 2.0, 2.2))
    rep = ls.sync(layers, [obj], [], "decorations", lambda m: tpl, project, out_dir=tmp_path / "o1")
    # в слое уже стоит Forklift ровно там (это объект из .wrp) — «уже в проекте»
    assert rep.added == 0 and rep.already == 1 and "уже в проекте: 1" in rep.message
    moved = replace(obj, x=obj.x + 3.0)
    rep = ls.sync(layers, [moved], [], "decorations", lambda m: tpl, project, out_dir=tmp_path / "o2")
    assert rep.added == 1 and rep.already == 0
    turned = replace(obj, yaw=obj.yaw + 5.0)
    rep = ls.sync(layers, [turned], [], "decorations", lambda m: tpl, project, out_dir=tmp_path / "o3")
    assert rep.added == 1
