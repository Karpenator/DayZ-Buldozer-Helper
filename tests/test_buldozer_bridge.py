import os
import random
import time
from pathlib import Path

from terrain_editor import buldozer_bridge as bb
from terrain_editor.objects import parse_line, parse_objects
from terrain_editor.winbuldozer import HotkeyEdge, Hotkey, parse_hotkey


# ---------------------------------------------------------------- скрипт Buldozer

def test_script_block_has_markers_and_file_names():
    block = bb.script_block()
    assert block.startswith(bb.MARK_BEGIN)
    assert block.rstrip().endswith(bb.MARK_END)
    assert f"$profile:{bb.STATE_FILE}" in block
    assert f"$profile:{bb.CMD_FILE}" in block
    assert "static void WE_BridgeStart()" in block
    assert "%" not in block.replace("%(", "")      # шаблон подставлен целиком


def test_script_braces_are_balanced():
    block = bb.script_block()
    assert block.count("{") == block.count("}")
    assert block.count("(") == block.count(")")


def test_merge_creates_new_file_with_main():
    text, action = bb.merge_script(None)
    assert action == "created"
    assert "static void BuldozerMain()" in text
    assert bb.START_CALL in bb._strip_block(text)    # вызов в BuldozerMain, вне нашего блока
    assert bb.MARK_BEGIN in text


def test_merge_into_user_script_adds_call_first_in_main():
    user = ("static void BuldozerMain() {\n"
            "\tGetGame().GetWorld().SetViewDistance(1500);\n}\n")
    text, action = bb.merge_script(user)
    assert action == "merged"
    body = text[text.index("static void BuldozerMain() {"):]
    assert body.index(bb.START_CALL) < body.index("SetViewDistance")
    assert "SetViewDistance(1500)" in text           # своё пользователя сохраняется
    again, action2 = bb.merge_script(text)
    assert action2 == "unchanged" and again == text   # повторная установка ничего не ломает


def test_merge_updates_old_block_only():
    user = "static void BuldozerMain()\n{\n\tFoo();\n}\n"
    text, _ = bb.merge_script(user)
    old = text.replace("WE_BuldozerBridge", "WE_OldBridge", 1)
    new, action = bb.merge_script(old)
    assert action == "updated"
    assert "WE_OldBridge" not in new
    assert new.count(bb.MARK_BEGIN) == 1
    assert "Foo();" in new


def test_merge_with_other_profile_name_and_missing_main():
    text, action = bb.merge_script("// пусто, только комментарий\n", script_name="karp")
    assert action == "merged"
    assert "static void KarpMain()" in text
    assert bb.main_function_name("Buldozer") == "BuldozerMain"


def test_install_script_writes_backup(tmp_path):
    path = tmp_path / "scripts" / "buldozer.c"
    res = bb.install_script(path)
    assert res.action == "created" and res.backup is None and path.is_file()
    path.write_text("static void BuldozerMain() { Bar(); }\n", encoding="utf-8")
    res = bb.install_script(path, now=0)
    assert res.action == "merged"
    assert res.backup is not None and "Bar();" in res.backup.read_text(encoding="utf-8")
    assert bb.START_CALL in path.read_text(encoding="utf-8")
    assert bb.install_script(path).action == "unchanged"


def test_default_script_path():
    assert bb.default_script_path("P:\\") == "P:\\scripts\\buldozer.c"
    assert bb.default_script_path("D:", "Karp") == "D:\\scripts\\karp.c"


# ---------------------------------------------------------------- файлы моста

STATE = """WE_BRIDGE 1
seq 42
gen 7
hit 1119800 19860 860100
cam 1100000 45000 -840000
dir 707 -300 640
Y|1|21345|1200
E|2
GE
end
"""


def test_parse_state():
    st = bb.parse_state(STATE)
    assert st.complete and st.version == 1 and st.seq == 42 and st.generation == 7
    assert st.hit == (1119.8, 19.86, 860.1)
    assert st.camera == (1100.0, 45.0, -840.0)
    assert st.placed == {1: (21.345, 1.2, None)}       # скрипт v3: без высоты для TB
    assert st.failed == {2}
    assert st.ghost_failed


def test_parse_state_incomplete_and_no_hit():
    st = bb.parse_state("WE_BRIDGE 1\nseq 1\ngen -1\ncam 0 0 0\n")
    assert not st.complete and st.hit is None


def test_format_commands_and_object_lines():
    o = bb.PlacedObject(5, "dz/structures/a|b.p3d", 1119.8, 860.1, 45, 1.25, -0.5)
    text = bb.format_commands(18, [o], "dz\\x.p3d", 30, 1, 0)
    lines = text.splitlines()
    assert lines[0] == "WE_CMD 1" and lines[1] == "gen 18"
    assert lines[2] == "G|dz\\x.p3d|30|1|0"
    assert lines[3] == "O|5|dz\\structures\\ab.p3d|1119.8|860.1|45|1.25|-0.5"
    assert lines[-1] == "end"
    assert len(lines[3].split("|")) == 8          # скрипт ждёт ровно 8 полей
    assert bb.format_commands(1, [], "").splitlines()[2] == "G|"


def test_session_add_undo_state_and_export(tmp_path):
    s = bb.PlacementSession(tmp_path / "session.json")
    a = s.add("dz\\structures\\military\\houses\\mil_barracks_round.p3d", 1119.8, 860.1, 19.86, 370, 1.0)
    b = s.add("dz\\plants\\tree\\t_betula2s.p3d", 10, 20, 5.0)
    assert (a.id, b.id) == (1, 2) and a.yaw == 10
    assert s.apply_state(bb.parse_state("Y|1|21345|1200\nE|2\nend\n"))
    assert a.y == 21.345 and a.center == 1.2 and b.failed
    assert not s.apply_state(bb.parse_state("Y|1|21345|1200\nE|2\nend\n"))

    out = tmp_path / "tb.txt"
    assert s.export_tb(out) == 2
    raw = out.read_bytes()
    assert raw.count(b"\r\n") == 2
    objs, errors = parse_objects(raw.decode("utf-8"))
    assert not errors
    assert objs[0].model == "mil_barracks_round"
    assert abs(objs[0].x - 1119.8) < 1e-6 and abs(objs[0].z - 860.1) < 1e-6
    # TB Relative = высота исходного начала над землёй: 21.345 − 1.2 − 19.86 (скрипт v3)
    assert abs(objs[0].y - 0.285) < 1e-6 and objs[1].y == 0.0   # нет ответа — подъём
    absolute, _ = parse_objects(s.tb_text(relative_height=False))
    assert abs(absolute[0].y - (21.345 - 1.2)) < 1e-6
    assert absolute[1].y == 5.0                      # нет ответа скрипта -> высота земли
    full = s.tb_text(template_names=False).splitlines()[0]
    assert parse_line(full).model.endswith("mil_barracks_round.p3d")
    assert "201119.8;" in full                       # X со сдвигом 200000, как у DayZ Editor

    s2 = bb.PlacementSession(tmp_path / "session.json")
    s2.load()
    assert [o.id for o in s2.objects] == [1, 2] and s2.next_id == 3

    s.remove([1])
    assert [o.id for o in s.objects] == [2]
    assert s.undo() and [o.id for o in s.objects] == [1, 2]
    assert s.remove_last().id == 2
    s.clear()
    assert s.objects == [] and s.undo() and len(s.objects) == 1


def test_placement_values():
    rng = random.Random(1)
    assert bb.placement_values(400, False, 1.5, 0, rng) == (40, 1.5)
    for _ in range(50):
        yaw, scale = bb.placement_values(0, True, 1.0, 20, rng)
        assert 0 <= yaw < 360 and 0.8 <= scale <= 1.2


def test_profile_candidates_and_freshest(tmp_path):
    cands = bb.profile_candidates({"overlay_profile_dir": str(tmp_path / "a"),
                                   "bulldozer_profiles": "", "work_drive": "Q:\\"}, home=tmp_path)
    assert cands[0] == tmp_path / "a"
    assert Path("Q:\\Buldozer") in cands
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / bb.STATE_FILE).write_text("x")
    (b / bb.STATE_FILE).write_text("x")
    now = time.time()
    os.utime(a / bb.STATE_FILE, (now - 10, now - 10))
    folder, age = bb.freshest_state([a, b, tmp_path / "none"], now=now)
    assert folder == b and age < 1
    folder, age = bb.freshest_state([a], now=now)
    assert folder == a and age > bb.STATE_MAX_AGE


# ---------------------------------------------------------------- клавиши

def test_parse_hotkey():
    assert parse_hotkey("F8") == Hotkey(0x77)
    assert parse_hotkey("shift+f9") == Hotkey(0x78, shift=True)
    assert parse_hotkey("Ctrl+Alt+P") == Hotkey(ord("P"), ctrl=True, alt=True)
    assert parse_hotkey("Insert").vk == 0x2D
    assert parse_hotkey("Shift+F9").text() == "Shift+F9"
    assert parse_hotkey("") is None and parse_hotkey("Ctrl+Shift") is None
    # один модификатор — сама клавиша (камера: «вниз» = Ctrl, «быстрее» = Shift)
    assert parse_hotkey("Ctrl") == Hotkey(0x11) and parse_hotkey("Ctrl").text() == "Ctrl"
    assert parse_hotkey("Shift").text() == "Shift" and parse_hotkey("Space").text() == "Space"
    assert parse_hotkey("Foo") is None


def test_hotkey_edge_needs_exact_modifiers():
    down = set()
    edge = HotkeyEdge(parse_hotkey("F9"))
    shifted = HotkeyEdge(parse_hotkey("Shift+F9"))
    is_down = down.__contains__
    assert not edge.update(is_down)
    down.add(0x78)
    assert edge.update(is_down) and not shifted.update(is_down)
    assert not edge.update(is_down)                  # держим — второй раз не срабатывает
    down.clear()
    edge.update(is_down)
    shifted.update(is_down)
    down.update({0x78, 0x10})
    assert shifted.update(is_down) and not edge.update(is_down)


def test_hotkey_edge_ignores_extra_ctrl_alt_but_not_shift():
    down = {0x78, 0x11, 0x12}                        # F9 + «залипшие» Ctrl и Alt (AltGr и т.п.)
    edge = HotkeyEdge(parse_hotkey("F9"))
    assert edge.update(down.__contains__) and edge.pressed and edge.mods == (True, False, True)
    ctrl_needed = HotkeyEdge(parse_hotkey("Ctrl+F9"))
    assert not ctrl_needed.update({0x78}.__contains__) and ctrl_needed.pressed
    plain = HotkeyEdge(parse_hotkey("F9"))
    assert not plain.update({0x78, 0x10}.__contains__)   # Shift+F9 — это «отменить», не «поставить»


def test_buldozer_window_title_and_pick():
    from terrain_editor.winbuldozer import is_buldozer_title, is_engine_exe, pick_buldozer
    assert is_buldozer_title("Buldozer") and is_buldozer_title(" buldozer ")
    assert not is_buldozer_title("buldozer.c - Блокнот")
    assert not is_buldozer_title("buldozer.c - scripts - Visual Studio Code")
    assert is_engine_exe(r"E:\Steam\steamapps\common\DayZ\DayZ_x64.exe")
    assert is_engine_exe("C:/x/DayZDiag_x64.exe") and not is_engine_exe(r"C:\Windows\explorer.exe")
    windows = [(1, "Buldozer", r"C:\Windows\explorer.exe"),       # папка P:\Buldozer
               (2, "buldozer.c - Блокнот", r"C:\Windows\notepad.exe"),
               (3, "Buldozer", r"E:\DayZ\DayZ_x64.exe")]
    assert pick_buldozer(windows) == 3
    assert pick_buldozer([(1, "Buldozer", r"C:\Windows\explorer.exe"), (5, "Buldozer", "")]) == 5
    assert pick_buldozer([(1, "Buldozer", r"C:\Windows\explorer.exe")]) == 1
    assert pick_buldozer([(2, "buldozer.c - Блокнот", "")]) is None


def test_script_v2_is_managed_and_replaces_v1_block():
    block = bb.script_block()
    assert block.startswith(f"{bb.MARK_BEGIN} v{bb.SCRIPT_REVISION} >>>") and bb.SCRIPT_REVISION >= 2
    assert "class WE_BuldozerBridge : Managed" in block
    assert "void ~WE_BuldozerBridge()" in block and "s_ActiveToken" in block
    assert "HideGhost" not in block                  # призрак не пересоздаётся каждый такт
    old_v1 = (f"{bb.MARK_BEGIN} v1 >>>\nclass WE_BuldozerBridge\n{{\n}}\n{bb.MARK_END}\n"
              "static void BuldozerMain()\n{\n\tWE_BridgeStart();\n}\n")
    new, action = bb.merge_script(old_v1)
    assert action == "updated" and new.count(bb.MARK_BEGIN) == 1 and new.count(bb.MARK_END) == 1
    assert "Managed" in new and bb._strip_block(new).count(bb.START_CALL) == 1   # вызов не задвоен


def test_tb_relative_not_absolute():
    """Объект с земли (18.63 м) не должен уезжать в TB на Absolute 37.3 — пишем Relative."""
    s = bb.PlacementSession(None)
    on_ground = s.add("Evelone_modular\\1\\EV_5.p3d", 1280.502, 732.437, 18.63)
    raised = s.add("Evelone_modular\\1\\EV_5.p3d", 1290.0, 732.0, 18.63, lift=1.5)
    s.apply_state(bb.parse_state("Y|1|18630|0|0\nY|2|20130|0|1500\nend\n"))
    assert on_ground.tb_object().y == 0.0 and raised.tb_object().y == 1.5
    assert on_ground.tb_object(relative_height=False).y == 18.63
    line = s.tb_text().splitlines()[0]
    assert line == '"EV_5";201280.502;732.437;0;0;0;1;0'


def _buldozer_y(ground, mlod_min, mlod_max, autocenter, scale=1.0, lift=0.0):
    """Что делает скрипт: Binarize переносит начало в центр габаритов (если autocenter),
    Buldozer ставит низ на землю. Возвращает строку Y|… v3 (без готовой высоты)."""
    shift = (mlod_min + mlod_max) / 2 if autocenter else 0.0
    clip_min = mlod_min - shift                      # ClippingInfo — в перенесённых координатах
    origin = ground - clip_min * scale + lift
    return f"Y|1|{round(origin * 1000)}|{round(shift * scale * 1000)}"


def test_tb_relative_accounts_for_autocenter():
    """Модель 6 м, в исходнике низ на −3 м (начало посередине): без autocenter=0 Binarize
    центрирует, Buldozer ставит низ на землю. В TB исходное начало должно быть на 3 м выше
    земли, иначе модель уходит под землю на половину высоты."""
    for autocenter in (True, False):
        s = bb.PlacementSession(None)
        o = s.add("mod\\tower.p3d", 100, 200, 50.0)
        s.apply_state(bb.parse_state(_buldozer_y(50.0, -3.0, 3.0, autocenter) + "\nend\n"))
        assert abs(o.tb_object().y - 3.0) < 1e-6, autocenter
    # низ модели в исходнике на нуле — Relative 0 при любом autocenter
    for autocenter in (True, False):
        s = bb.PlacementSession(None)
        o = s.add("dz\\house.p3d", 100, 200, 50.0, lift=0.5)
        s.apply_state(bb.parse_state(_buldozer_y(50.0, 0.0, 6.0, autocenter, lift=0.5) + "\nend\n"))
        assert abs(o.tb_object().y - 0.5) < 1e-6, autocenter
    # масштаб 2: исходный низ на −1 м -> начало на 2 м над землёй
    s = bb.PlacementSession(None)
    o = s.add("mod\\rock.p3d", 0, 0, 10.0, scale=2.0)
    s.apply_state(bb.parse_state(_buldozer_y(10.0, -1.0, 2.0, True, scale=2.0) + "\nend\n"))
    assert abs(o.tb_object().y - 2.0) < 1e-6


def test_tb_relative_prefers_script_value():
    """Скрипт v4 присылает точное значение (с поворотом и землёй под исходным началом)."""
    st = bb.parse_state("Y|7|53000|3000|2950\nY|8|1000|0|\nend\n")
    assert st.placed == {7: (53.0, 3.0, 2.95), 8: (1.0, 0.0, None)}
    s = bb.PlacementSession(None)
    s.next_id = 7
    o = s.add("mod\\tower.p3d", 100, 200, 50.0)
    assert s.apply_state(st) and o.tb_rel == 2.95
    assert o.tb_object().y == 2.95                         # а не 53 − 3 − 50 = 0
    assert abs(o.tb_object(relative_height=False).y - 50.0) < 1e-6
    assert not s.apply_state(st)


def test_script_v4_reports_tb_height():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 4
    assert "GetBoundingCenter().Multiply3(m)" in block and "tbRelative" in block
    assert 'Mm(c[1]) + "|" + Mm(tbRelative)' in block


def test_ghost_pin_in_commands_and_script_v3():
    text = bb.format_commands(3, [], "dz\\x.p3d", 15, 1, 0.25, (100.5, 20.0, 200.25))
    assert text.splitlines()[2] == "G|dz\\x.p3d|15|1|0.25|100.5|20|200.25"
    assert bb.format_commands(3, [], "", ghost_pin=(1, 2, 3)).splitlines()[2] == "G|"
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 3 and "m_GhostPinned" in block and "MoveGhost(m_GhostPin)" in block
    assert "p.Count() >= 8" in block


def test_script_v12_selection_frame_on_canvas_and_counters():
    from terrain_editor import buldozer_bridge as bb2
    block = bb2.script_block()
    assert bb2.SCRIPT_REVISION >= 12
    assert block.count("{") == block.count("}")
    assert "CanvasWidgetTypeID" not in block                   # нет в списке типов — сломал бы скрипт
    assert 'CreateWidgets("gui/layouts/debug/day_z_debugcanvas.layout")' in block
    assert 'CanvasWidget.Cast(m_CanvasRoot.FindAnyWidget("CanvasWidget"))' in block
    frame = block[block.index("void CamFrame(float dt)"):block.index("protected void ShowCursorMenu()")]
    assert frame.index("DropCanvas();") < frame.index("DrawFrames();") < frame.index("ReadCamera();")
    draw = block[block.index("protected void DrawBox("):block.index("void Start()")]
    assert "GetScreenPosRelative(w)" in draw and "m_Canvas.DrawLine(" in draw and "s[2] > 0" in draw
    assert " ? " not in draw                                   # в Enforce нет тернарного оператора
    assert "m_MenuOpens++;" in block and "m_CursorShows++;" in block
    st = bb2.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\ncam 0 0 0\ndir 0 0 1000\nMO 2\nCS 5\nCV 1\nend\n")
    assert (st.menu_opens, st.cursor_shows, st.canvas) == (2, 5, True)
    st = bb2.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\ncam 0 0 0\ndir 0 0 1000\nend\n")
    assert (st.menu_opens, st.cursor_shows, st.canvas) == (0, 0, None)


def test_script_v13_keeps_camera_when_cam_file_is_busy():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 13
    read = block[block.index("protected void ReadCamera()"):]
    read = read[:read.index("protected Object")] if "protected Object" in read else read
    busy = read[read.index("if (file == 0)"):read.index("string line;")]
    assert "m_CamWanted = false" not in busy and "m_CamReadFails++;" in busy
    st = bb.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\ncam 0 0 0\ndir 0 0 1000\nCF 7\nend\n")
    assert st.cam_read_fails == 7



def test_script_v14_verifies_tb_insert_and_state_reports_it():
    from terrain_editor.buldozer_bridge import (SCRIPT_REVISION, PlacedObject, format_commands,
                                                parse_state, script_block)
    assert SCRIPT_REVISION >= 14
    text = script_block()
    assert 'parts.Get(0) == "V"' in text and "RunVerify()" in text
    assert "GetObjectsAtPosition(at, 2.0, near, null)" in text
    assert '"VF|" + vid.ToString()' in text
    st = parse_state("WE_BRIDGE 1\nseq 3\nVF|7|1\nVF|8|0\nend\n")
    assert st.verified == {7: 1, 8: 0}
    # объект, уже вставленный в TB, скрипту не посылается — его показывает TB
    a = PlacedObject(1, "a\\b.p3d", 1.0, 2.0)
    b = PlacedObject(2, "a\\c.p3d", 3.0, 4.0, tb="ok")
    cmd = format_commands(5, [a, b], extra=["V|1|1.000|2.000|a\\b.p3d"])
    assert "O|1|" in cmd and "O|2|" not in cmd and "V|1|1.000|2.000|a\\b.p3d" in cmd


# enum KeyCode из scripts/1_core/proto/ensystem.c (DayZ-Script-Diff): неизвестное имя в скрипте —
# ошибка компиляции всего buldozer.c
DAYZ_KEYCODES = set("""
    KC_ESCAPE KC_1 KC_2 KC_3 KC_4 KC_5 KC_6 KC_7
    KC_8 KC_9 KC_0 KC_MINUS KC_EQUALS KC_BACK KC_TAB KC_Q
    KC_W KC_E KC_R KC_T KC_Y KC_U KC_I KC_O
    KC_P KC_LBRACKET KC_RBRACKET KC_RETURN KC_LCONTROL KC_A KC_S KC_D
    KC_F KC_G KC_H KC_J KC_K KC_L KC_SEMICOLON KC_APOSTROPHE
    KC_GRAVE KC_LSHIFT KC_BACKSLASH KC_Z KC_X KC_C KC_V KC_B
    KC_N KC_M KC_COMMA KC_PERIOD KC_SLASH KC_RSHIFT KC_MULTIPLY KC_LMENU
    KC_SPACE KC_CAPITAL KC_F1 KC_F2 KC_F3 KC_F4 KC_F5 KC_F6
    KC_F7 KC_F8 KC_F9 KC_F10 KC_NUMLOCK KC_SCROLL KC_NUMPAD7 KC_NUMPAD8
    KC_NUMPAD9 KC_SUBTRACT KC_NUMPAD4 KC_NUMPAD5 KC_NUMPAD6 KC_ADD KC_NUMPAD1 KC_NUMPAD2
    KC_NUMPAD3 KC_NUMPAD0 KC_DECIMAL KC_OEM_102 KC_F11 KC_F12 KC_NUMPADEQUALS KC_PREVTRACK
    KC_CIRCUMFLEX KC_AT KC_COLON KC_UNDERLINE KC_STOP KC_AX KC_UNLABELED KC_NEXTTRACK
    KC_NUMPADENTER KC_RCONTROL KC_MUTE KC_CALCULATOR KC_PLAYPAUSE KC_MEDIASTOP KC_VOLUMEDOWN KC_VOLUMEUP
    KC_WEBHOME KC_NUMPADCOMMA KC_DIVIDE KC_SYSRQ KC_RMENU KC_PAUSE KC_HOME KC_UP
    KC_PRIOR KC_LEFT KC_RIGHT KC_END KC_DOWN KC_NEXT KC_INSERT KC_DELETE
    KC_LWIN KC_RWIN KC_APPS KC_POWER KC_SLEEP KC_WAKE KC_MEDIASELECT
""".split())


def test_script_v15_reports_requested_keys_by_keycode_name():
    import re
    from terrain_editor.buldozer_bridge import SCRIPT_REVISION, parse_state, script_block
    from terrain_editor import winbuldozer as wb
    assert SCRIPT_REVISION >= 15
    block = script_block()
    used = set(re.findall(r"KeyCode\.(KC_[A-Z0-9_]+)", block))
    assert used and used <= DAYZ_KEYCODES, used - DAYZ_KEYCODES
    assert {n for names in wb.VK_TO_KEYCODE.values() for n in names} == used
    assert 'parts.Get(0) == "KW"' in block and '"KD|"' in block and "KeyState(kdCode)" in block
    assert "%(" not in block
    st = parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nKD|KC_F8,KC_LSHIFT\nend\n")
    assert st.keys_down == {"KC_F8", "KC_LSHIFT"}
    assert parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nKD|\nend\n").keys_down == set()
    assert parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nend\n").keys_down is None
    assert wb.keycodes_for([0x77, 0x10, 0x01, 0x78]) == ["KC_F8", "KC_LSHIFT", "KC_RSHIFT", "KC_F9"]
