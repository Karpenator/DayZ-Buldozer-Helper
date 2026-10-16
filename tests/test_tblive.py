"""Вставка объектов в открытый Terrain Builder: очередь, подтверждение мостом, окна TB."""
import struct

from terrain_editor import tblive
from terrain_editor.buldozer_bridge import BridgeState, PlacedObject
from terrain_editor.tblive import LiveImporter, TbApi, TbWindow


class FakeApi(TbApi):
    def __init__(self, windows=None):
        self.wins = windows if windows is not None else [
            TbWindow(0x100, 42, "Afx:400000:8", "Terrain Builder - Evelone2666", accepts=True, top=True,
                     area=2_000_000),
            TbWindow(0x200, 42, "AfxFrameOrView", "", accepts=True, area=900_000)]
        self.drops = []
        self.saved = []
        self.dlg = {}
        self.drop_error = ""

    def pids(self):
        return {w.pid for w in self.wins}

    def windows(self):
        return list(self.wins)

    def drop(self, win, paths):
        self.drops.append((win.hwnd, list(paths)))
        return self.drop_error

    def dialogs(self, pids):
        return dict(self.dlg)

    def save(self, win):
        self.saved.append(win.hwnd)
        return ""


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _setup(tmp_path, api=None, **kw):
    clock = Clock()
    api = api or FakeApi()
    live = LiveImporter(api, lambda: tmp_path, clock=clock, **kw)
    return live, api, clock


NAMES = {"evelone_modular\\forklift\\forklift.p3d": "Forklift"}


def _step(live, objs, st=None):
    return live.step(objs, st or BridgeState(), lambda: NAMES, lambda: (500000.0, 442127.0),
                     lambda m: NAMES.get(m.lower()))


def test_drop_waits_for_height_then_sends_file_with_template_and_frame(tmp_path):
    live, api, clock = _setup(tmp_path)
    o = PlacedObject(1, "evelone_modular\\forklift\\forklift.p3d", 10.0, 20.0, 90.0)
    assert not _step(live, [o]) and not api.drops            # высоты от скрипта ещё нет
    o.tb_rel = 0.25
    assert _step(live, [o])
    assert o.tb == "sent" and o.tb_at == clock.t
    hwnd, paths = api.drops[0]
    assert hwnd == 0x200                                     # вид карты (дочернее окно), как мышью
    text = open(paths[0], "rb").read()                        # тот же формат, что у экспорта
    assert text == b'"Forklift";500010;442147;90;0;0;1;0.25\r\n'
    assert paths[0].startswith(str(tmp_path / "WE_edited_layers" / "live"))
    # без высоты от скрипта — через WAIT_REL секунд, по подъёму
    p = PlacedObject(2, "evelone_modular\\forklift\\forklift.p3d", 1.0, 2.0)
    _step(live, [o, p])
    assert not p.tb
    clock.t += tblive.WAIT_REL
    _step(live, [o, p])
    assert p.tb == "sent" and len(api.drops) == 2


def test_bridge_confirms_insert_and_lines_ask_for_it(tmp_path):
    live, api, clock = _setup(tmp_path, save=True)
    o = PlacedObject(3, "evelone_modular\\forklift\\forklift.p3d", 10.0, 20.0, tb_rel=0.0)
    _step(live, [o])
    assert live.verify_lines([o]) == ["V|3|10.000|20.000|evelone_modular\\forklift\\forklift.p3d"]
    clock.t += 1.0
    assert not _step(live, [o], BridgeState(verified={3: 0}))
    assert o.tb == "sent"
    assert _step(live, [o], BridgeState(verified={3: 1}))
    assert o.tb == "ok" and live.verify_lines([o]) == []
    assert not api.saved
    clock.t += tblive.SAVE_DELAY
    _step(live, [o], BridgeState(verified={}))
    assert api.saved == [0x100]                               # «Сохранить» — главному окну TB


def test_not_confirmed_in_time_falls_back_but_late_confirmation_counts(tmp_path):
    live, api, clock = _setup(tmp_path)
    o = PlacedObject(4, "evelone_modular\\forklift\\forklift.p3d", 1.0, 1.0, tb_rel=0.0)
    _step(live, [o])
    clock.t += tblive.VERIFY_TIMEOUT + 0.1
    assert _step(live, [o], BridgeState(verified={4: 0}))
    assert o.tb == "fail" and "не показал" in live.message
    assert live.verify_lines([o])                             # ещё проверяем — вдруг TB был занят
    assert _step(live, [o], BridgeState(verified={4: 1}))
    assert o.tb == "ok"
    assert len(api.drops) == 1                                # повторно не отправляли


def test_no_template_tb_not_running_and_drop_errors(tmp_path):
    live, api, clock = _setup(tmp_path)
    unknown = PlacedObject(5, "dz\\x\\unknown.p3d", 0.0, 0.0, tb_rel=0.0)
    assert _step(live, [unknown])
    assert unknown.tb == "fail" and not api.drops             # TB оборвал бы импорт на нём
    api.wins = []                                             # TB закрыт — только рабочая копия
    o = PlacedObject(6, "evelone_modular\\forklift\\forklift.p3d", 0.0, 0.0, tb_rel=0.0)
    assert not _step(live, [o]) and o.tb == ""
    api.wins = FakeApi().wins
    api.drop_error = "нет доступа"
    clock.t += 3.0
    _step(live, [o])
    assert o.tb == "" and "нет доступа" in live.message
    api.drop_error = ""
    clock.t += 1.0
    _step(live, [o])
    assert o.tb == ""                                         # пауза после ошибки
    clock.t += tblive.RETRY_AFTER
    _step(live, [o])
    assert o.tb == "sent"


def test_windows_without_wm_dropfiles_are_reported(tmp_path):
    api = FakeApi([TbWindow(0x300, 7, "Qt5QWindowIcon", "Terrain Builder", ole=True, top=True, area=10)])
    live, api, clock = _setup(tmp_path, api=api)
    o = PlacedObject(7, "evelone_modular\\forklift\\forklift.p3d", 0.0, 0.0, tb_rel=0.0)
    _step(live, [o])
    assert o.tb == "" and not api.drops
    assert "WM_DROPFILES" in live.message and "мышью" in live.message


def test_tb_dialog_pauses_inserts_and_is_shown(tmp_path, caplog):
    import logging
    live, api, clock = _setup(tmp_path)
    a = PlacedObject(8, "evelone_modular\\forklift\\forklift.p3d", 0.0, 0.0, tb_rel=0.0)
    _step(live, [a])
    api.dlg = {0x900: "TxTImport | Wrong file format or source template not found"}
    clock.t += 1.0
    b = PlacedObject(9, "evelone_modular\\forklift\\forklift.p3d", 5.0, 0.0, tb_rel=0.0)
    with caplog.at_level(logging.WARNING, logger=tblive.log.name):
        _step(live, [a, b])
    assert b.tb == "" and "source template not found" in live.message
    assert any("shows a dialog" in r.getMessage() for r in caplog.records)
    api.dlg = {}
    clock.t += 1.0
    _step(live, [a, b])
    assert b.tb == "sent" and live.message == ""


def test_disabled_does_nothing(tmp_path):
    live, api, clock = _setup(tmp_path, enabled=False)
    o = PlacedObject(10, "evelone_modular\\forklift\\forklift.p3d", 0.0, 0.0, tb_rel=0.0)
    assert not _step(live, [o]) and not api.drops and o.tb == ""
    st = live.status([o])
    assert (st.waiting, st.sent, st.ok) == (1, 0, 0)


def test_dropfiles_blob_layout():
    blob = tblive.dropfiles_blob(["C:\\a.txt", "D:\\b c.txt"])
    assert struct.unpack_from("<IiiII", blob) == (20, 10, 10, 0, 1)
    assert blob[20:].decode("utf-16-le") == "C:\\a.txt\0D:\\b c.txt\0\0"


def test_drop_target_prefers_largest_accepting_child_then_main_window():
    wins = [TbWindow(1, 1, "Child", "", accepts=True, area=50),
            TbWindow(2, 1, "Main", "", accepts=False, top=True, area=100),
            TbWindow(3, 1, "Main2", "", accepts=True, top=True, area=10),
            TbWindow(4, 1, "View", "", accepts=True, area=80)]
    assert tblive.drop_target(wins).hwnd == 4
    assert tblive.drop_target(wins[1:3]).hwnd == 3
    assert tblive.drop_target([wins[1]]) is None
    assert tblive.main_window(wins).hwnd == 2


def test_objects_from_previous_session_are_not_sent(tmp_path):
    live, api, clock = _setup(tmp_path)
    live.skip = {11}
    old = PlacedObject(11, "evelone_modular\\forklift\\forklift.p3d", 0.0, 0.0, tb_rel=0.0)
    new = PlacedObject(12, "evelone_modular\\forklift\\forklift.p3d", 3.0, 0.0, tb_rel=0.0)
    _step(live, [old, new])
    assert old.tb == "" and new.tb == "sent" and len(api.drops) == 1
