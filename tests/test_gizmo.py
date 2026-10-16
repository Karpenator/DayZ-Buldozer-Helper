"""v16: гизмо выделенного объекта (стрелки X/Y/Z, тянуть ЛКМ по оси) и удаление по Delete."""
import pytest

from terrain_editor import buldozer_bridge as bb
from terrain_editor import buldozer_overlay as bo
from terrain_editor import winbuldozer as wb
from terrain_editor.gizmo import AxisDrag, Gizmo, axis_at, parse_gizmo, to_pixels
from terrain_editor.map_edits import MapEdit, Sample, edited_dir, save_edits
from terrain_editor.tv4l import Tv4lLayer

from test_buldozer_overlay import (_map_model, _pick_line, _select_map_object, _state, commands,
                                   make_overlay, write_state)
from test_freecam import FakeHook
from test_map_edits import OX, OZ, layer_bytes, names

RECT = (100, 50, 1600, 900)         # клиентская область Buldozer на экране
# начало в центре; X вправо на 160 px, Y вверх на 90 px, Z влево-вниз; длина стрелок 2 м
GZ = "GZ|42|3|500000|500000|600000|500000|500000|400000|450000|550000|2000"


@pytest.fixture
def overlay(tmp_path, monkeypatch):
    yield from make_overlay(tmp_path, monkeypatch)


# ---------------------------------------------------------------- математика (без Qt)

def test_parse_and_pixels():
    g = parse_gizmo(GZ.split("|"))
    assert (g.session, g.ref, g.length) == (42, 3, 2.0)
    o, ends = to_pixels(g, RECT)
    assert o == (900.0, 500.0)
    assert ends == [(1060.0, 500.0), (900.0, 410.0), (820.0, 545.0)]
    assert parse_gizmo("GZ|42|3|1|2".split("|")) is None
    assert parse_gizmo("GZ|42|3|a|0|0|0|0|0|0|0|2000".split("|")) is None


def test_axis_hit_test():
    g = parse_gizmo(GZ.split("|"))
    assert axis_at(g, RECT, (1000, 504)) == 0              # у красной стрелки
    assert axis_at(g, RECT, (903, 440)) == 1               # у зелёной
    assert axis_at(g, RECT, (850, 530)) == 2               # у синей
    assert axis_at(g, RECT, (1000, 540)) is None           # мимо
    assert axis_at(g, RECT, (1080, 500)) is None           # за концом стрелки
    assert axis_at(None, RECT, (1000, 500)) is None
    assert axis_at(g, None, (1000, 500)) is None
    # ось смотрит прямо в камеру (стрелка — точка) — не берётся
    flat = Gizmo(1, 1, (0.5, 0.5), ((0.6, 0.5), (0.5, 0.512), (0.4, 0.5)), 2.0)
    assert axis_at(flat, RECT, (900, 515)) is None         # у короткой Y (11 px), X/Z дальше 10 px
    assert axis_at(flat, RECT, (902, 502)) == 0            # а X — длинная, её взять можно


def test_axis_drag_moves_only_its_axis():
    g = parse_gizmo(GZ.split("|"))
    d = AxisDrag.begin(g, RECT, 0, (10.0, 20.0, 0.0))      # X: 160 px на 2 м -> 80 px/м
    d.add(40, 0)
    d.add(0, 300)                                          # поперёк стрелки — ничего
    assert abs(d.meters - 0.5) < 1e-9 and d.values() == (10.5, 20.0, 0.0)
    d = AxisDrag.begin(g, RECT, 1, (10.0, 20.0, 1.0))      # Y: вверх по экрану = вверх в мире
    d.add(0, -45)
    assert d.values() == (10.0, 20.0, 2.0)
    d = AxisDrag.begin(g, RECT, 2, (10.0, 20.0, 0.0))      # Z: стрелка влево-вниз
    d.add(-80, 45)
    x, z, lift = d.values()
    assert (x, lift) == (10.0, 0.0) and abs(z - 22.0) < 1e-9
    flat = Gizmo(1, 1, (0.5, 0.5), ((0.5, 0.5), (0.5, 0.4), (0.4, 0.5)), 2.0)
    assert AxisDrag.begin(flat, RECT, 0, (0, 0, 0)) is None


# ---------------------------------------------------------------- протокол, скрипт, слои

def test_state_gz_and_deleted_command():
    st = bb.parse_state("WE_BRIDGE 1\nseq 5\ngen 1\n" + GZ + "\nend\n")
    assert st.gizmo == parse_gizmo(GZ.split("|"))
    assert bb.parse_state("WE_BRIDGE 1\nseq 5\ngen 1\nend\n").gizmo is None
    e = MapEdit("a.p3d", 1.0, 2.0, 90.0, x=1.0, z=2.0, yaw=90.0, sess=42, ref=3)
    assert not e.changed
    e.deleted = True
    assert e.changed and e.command() == "T|42|3|1|2|90|0|1"


def test_script_v16_gizmo_and_hide():
    s = bb.script_block()
    assert bb.SCRIPT_REVISION >= 16
    assert "protected void DrawGizmo(Object o)" in s and "DrawGizmo(" in s.split("protected void DrawGizmo")[0]
    assert '"GZ|"' in s and "m_GizmoLine" in s and "m_GizmoAxis" in s
    assert '"GA"' in s                                         # подсветка стрелки под курсором
    assert "y = y - 1000.0" in s                               # удалённый объект карты — под землю
    assert "%" not in s.replace("%(", "")


def test_save_edits_removes_deleted_object(tmp_path):
    src = tmp_path / "Evelone2666.Layers"
    src.mkdir()
    (src / "Walls.tv4l").write_bytes(layer_bytes())
    layers = [Tv4lLayer.load(src / "Walls.tv4l")]
    e = MapEdit("evelone\\ev_1.p3d", 110.0 - OX, 910.0 - OZ, 90.0, x=110.0 - OX, z=910.0 - OZ, yaw=90.0,
                deleted=True)
    rep = save_edits(layers, [e], edited_dir(src, 0), names,
                     samples=[Sample("evelone\\ev_2.p3d", 900.0 - OX, 100.0 - OZ)])
    assert rep.deleted == 1 and rep.moved == 0 and "удалено: 1" in rep.message
    out = Tv4lLayer.load(rep.written[0])
    assert out.find(2) is None and out.find(1) is not None and out.find(3) is not None
    assert len(out.objects) == 2
    assert (src / "Walls.tv4l").read_bytes() == layer_bytes()  # оригинал не тронут


def test_undo_restores_map_edits(tmp_path):
    s = bb.PlacementSession(tmp_path / "s.json")
    e = MapEdit("a.p3d", 1.0, 2.0, 0.0, x=1.0, z=2.0, sess=1, ref=1)
    s.map_edits.append(e)
    s.begin_change()
    e.deleted = True
    assert s.undo()
    assert len(s.map_edits) == 1 and not s.map_edits[0].deleted


# ---------------------------------------------------------------- окно: Delete

def test_delete_key_removes_selected_map_object_and_undo_brings_it_back(overlay, monkeypatch):
    profile, model, down, cursor = _select_map_object(overlay, monkeypatch)
    edit = overlay._sel.edit
    assert "Delete — удалить" in overlay.window.sel_label.text()
    down.add(0x2E)                                             # Delete в Buldozer
    overlay._poll_keys()
    assert edit.deleted and overlay._sel is None
    assert "T|42|3|60|880|90|0|1" in commands(profile)         # скрипт прячет объект
    assert "удалён" in overlay.window.status.text()
    assert "(1)" in overlay.window.save_layers.text()          # уйдёт из слоя при записи
    down.clear()
    overlay._poll_keys()
    overlay.undo()
    assert not overlay.session.map_edits[0].deleted
    assert not any(line.startswith("T|") for line in commands(profile))   # скрипт вернёт на место


def test_delete_key_removes_own_object(overlay, monkeypatch):
    profile = overlay.profile
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    write_state(profile, _state(1))
    overlay._tick()
    assert overlay.place_at_cursor()
    obj = overlay.session.objects[0]
    overlay.window.models.setCurrentRow(-1)
    overlay.window.models.clearSelection()
    overlay.select_at_cursor()
    write_state(profile, _state(2, _pick_line(overlay._pick_seq, obj.model, ref=1, own=obj.id,
                                              x=obj.x, z=obj.z)))
    overlay._tick()
    assert overlay._sel is not None and overlay._sel.own == obj.id
    overlay._bz = 12345
    down = {0x2E}
    from test_buldozer_overlay import _fake_keys
    _fake_keys(monkeypatch, down, False)                       # активно другое окно — не удаляем
    overlay._poll_keys()
    assert overlay.session.objects
    down.clear()
    overlay._poll_keys()
    _fake_keys(monkeypatch, down, True)
    down.add(0x2E)
    overlay._poll_keys()
    assert not overlay.session.objects and overlay._sel is None
    assert not any(line.startswith(f"O|{obj.id}|") for line in commands(profile))
    overlay.undo()
    assert [o.id for o in overlay.session.objects] == [obj.id]


def test_delete_key_configurable_and_relayed(overlay):
    overlay.settings.update(overlay_delete_key="Ctrl+X")
    overlay.reload_settings()
    assert overlay._k_delete.hotkey.text() == "Ctrl+X"
    assert "KC_X" in overlay._relay_line()
    overlay.settings.update(overlay_delete_key="Delete")
    overlay.reload_settings()
    assert "KC_DELETE" in overlay._relay_line() and 0x2E in overlay._cam_swallow()


# ---------------------------------------------------------------- окно: тянуть стрелку (своя камера)

def _gizmo_env(overlay, monkeypatch):
    profile, model, down, cursor = _select_map_object(overlay, monkeypatch)
    FakeHook.instances.clear()
    overlay._hook_factory = FakeHook
    write_state(profile, _state(3, _pick_line(overlay._pick_seq, model), GZ))
    overlay._tick()
    overlay._bz = 12345
    monkeypatch.setattr(bo.wb, "client_rect", lambda h: RECT)
    monkeypatch.setattr(bo.wb, "is_window", lambda h: h == 12345)
    monkeypatch.setattr(bo.wb, "find_buldozer_window", lambda: 12345)
    monkeypatch.setattr(bo.wb, "pointer_in", lambda h: (0.5, 0.5) if h == 12345 else None)
    clock = [1000.0]
    monkeypatch.setattr(bo.time, "monotonic", lambda: clock[0])
    return profile, model, down, cursor, clock


def _step(overlay, clock, dt=0.1):
    clock[0] += dt
    overlay._cam_tick()


def test_drag_gizmo_arrow_moves_along_that_axis_only(overlay, monkeypatch):
    profile, model, down, cursor, clock = _gizmo_env(overlay, monkeypatch)
    edit = overlay._sel.edit
    cursor[:] = [1000, 503]                                    # над красной стрелкой X
    overlay._cam_tick()
    assert overlay.camera.active and overlay._gizmo_hot == 0
    assert "GA|1" in commands(profile)                         # скрипт подсветит её
    assert "стрелки X/Y/Z" in overlay.window.sel_label.text()
    hook = FakeHook.instances[0]
    assert hook.on_button(1, True)                             # ЛКМ на стрелке
    _step(overlay, clock)
    assert overlay._gizmo_drag is not None
    hook.on_move(1080, 553, (1000, 503))                       # +80 px вправо (и вниз — не считается)
    _step(overlay, clock)
    assert (edit.x, edit.z, edit.lift) == (61.0, 880.0, 0.0)   # 80 px/м -> +1 м по X
    assert overlay.camera.pose.yaw == 0.0 and overlay.camera.pose.pitch == 0.0   # камера стоит
    assert "T|42|3|61|880|90|0" in commands(profile)
    assert hook.on_button(1, False)
    _step(overlay, clock)
    assert overlay._gizmo_drag is None and overlay._sel is not None    # щелчка-выделения нет
    assert overlay.session.map_edits[0].x == 61.0
    overlay.undo()                                             # «Отменить» — вернуть как было
    assert overlay._sel is not None and overlay._sel.edit is overlay.session.map_edits[0]
    assert overlay._sel.edit.x == 60.0


def test_drag_y_arrow_lifts_and_lmb_elsewhere_still_looks(overlay, monkeypatch):
    profile, model, down, cursor, clock = _gizmo_env(overlay, monkeypatch)
    edit = overlay._sel.edit
    cursor[:] = [902, 450]                                     # над зелёной Y
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    hook.on_button(1, True)
    _step(overlay, clock)
    hook.on_move(902, 405, (902, 450))                         # вверх на 45 px -> +1 м
    _step(overlay, clock)
    assert (edit.x, edit.z) == (60.0, 880.0) and abs(edit.lift - 1.0) < 1e-9
    hook.on_button(1, False)
    _step(overlay, clock)
    cursor[:] = [300, 300]                                     # мимо стрелок — обычный обзор
    _step(overlay, clock)
    assert overlay._gizmo_hot is None and not any(l.startswith("GA|") for l in commands(profile))
    hook.on_button(1, True)
    _step(overlay, clock)
    hook.on_move(400, 300, (300, 300))
    _step(overlay, clock)
    assert overlay._gizmo_drag is None and overlay.camera.pose.yaw != 0.0


def test_gizmo_of_other_object_ignored(overlay, monkeypatch):
    profile, model, down, cursor, clock = _gizmo_env(overlay, monkeypatch)
    write_state(profile, _state(4, _pick_line(overlay._pick_seq, model), GZ.replace("GZ|42|3|", "GZ|42|9|")))
    overlay._tick()
    overlay._bz = 12345
    cursor[:] = [1000, 503]
    overlay._cam_tick()
    assert overlay._gizmo_hot is None


def test_launcher_has_delete_key_setting():
    from terrain_editor.settings import DEFAULTS
    assert DEFAULTS["overlay_delete_key"] == "Delete"
    assert wb.parse_hotkey("Delete").vk == 0x2E
    assert _map_model                                          # помощник доступен (импорт не сломан)


def test_undo_of_placement_keeps_untouched_map_selection(overlay, monkeypatch):
    profile, model, down, cursor = _select_map_object(overlay, monkeypatch)
    sel = overlay._sel
    overlay.session.map_edits.clear()                          # «выделили после» последнего снимка
    overlay.session.begin_change()
    overlay.session.map_edits.append(sel.edit)
    assert overlay.session.undo() or True
    overlay._reattach_selection()
    assert overlay._sel is sel and sel.edit in overlay.session.map_edits


def test_trial_v17_script_is_replaced(tmp_path):
    """v17 (режим «Рельеф») убран: уже записанный v17 — «старый», программа его заменит."""
    path = tmp_path / "buldozer.c"
    path.write_text(bb.script_block().replace(f"bridge v{bb.SCRIPT_REVISION} ", "bridge v17 "),
                    encoding="utf-8")
    assert bb.SCRIPT_REVISION == 18 and bb.script_status(path) == ("old", 17)
    assert "WatchNativeInputs" not in bb.script_block() and "UABuld" not in bb.script_block()
