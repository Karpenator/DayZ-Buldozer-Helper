"""Своя камера в Buldozer: математика позы, файл камеры, перехват ввода, связка с окном."""
import math

import pytest

from terrain_editor import freecam as fc
from terrain_editor import winbuldozer as wb
from terrain_editor import buldozer_bridge as bb


def close(a, b, eps=1e-6):
    return all(abs(x - y) < eps for x, y in zip(a, b))


# ---------------------------------------------------------------- поза и движение

def test_pose_from_buldozer_camera_direction():
    p = fc.pose_from_camera((100, 50, 200), (0, 0, 1))          # на север
    assert (p.yaw, p.pitch) == (0.0, 0.0) and p.pos == (100, 50, 200)
    p = fc.pose_from_camera((0, 0, 0), (1, 1, 1))               # как VectorToAngles: <45, 35.26>
    assert abs(p.yaw - 45) < 1e-9 and abs(p.pitch - 35.2644) < 1e-3
    assert abs(fc.pose_from_camera((0, 0, 0), (-1, 0, 0)).yaw - 270) < 1e-9   # на запад
    assert fc.pose_from_camera((0, 0, 0), (0, -1, 0)).pitch == -fc.PITCH_LIMIT  # строго вниз


def test_mouse_look_turns_and_clamps():
    p = fc.CamPose(0, 0, 0, 350, 0)
    p = fc.look(p, 100, 0, 0.2)                                  # вправо на 20° через север
    assert abs(p.yaw - 10) < 1e-9
    p = fc.look(p, 0, -50, 0.2)                                  # мышь вверх — взгляд вверх
    assert abs(p.pitch - 10) < 1e-9
    assert fc.look(p, 0, -50, 0.2, invert_y=True).pitch < p.pitch
    assert fc.look(p, 0, -10000, 0.2).pitch == fc.PITCH_LIMIT


def test_wasd_moves_relative_to_yaw():
    east = fc.CamPose(0, 10, 0, 90, -45)                         # смотрит на восток и вниз
    v = fc.move_velocity(east, fc.CamInput(forward=True), 10)
    assert close(v, (10, 0, 0))                                  # горизонтально, не в землю
    v = fc.move_velocity(east, fc.CamInput(forward=True), 10, along_view=True)
    assert close(v, (10 * math.cos(math.radians(45)), -10 * math.sin(math.radians(45)), 0))
    assert close(fc.move_velocity(east, fc.CamInput(right=True), 10), (0, 0, -10))    # юг
    assert close(fc.move_velocity(east, fc.CamInput(left=True), 10), (0, 0, 10))
    assert close(fc.move_velocity(east, fc.CamInput(up=True, fast=True), 10, 4.0), (0, 40, 0))
    diag = fc.move_velocity(fc.CamPose(0, 0, 0), fc.CamInput(forward=True, right=True), 10)
    assert abs(math.hypot(*diag) - 10) < 1e-9                    # по диагонали не быстрее
    assert fc.move_velocity(east, fc.CamInput(forward=True, back=True), 10) == (0.0, 0.0, 0.0)


def test_wheel_changes_speed():
    assert abs(fc.adjust_speed(30, 120) - 37.5) < 1e-9
    assert abs(fc.adjust_speed(30, -240) - 30 / 1.5625) < 1e-9
    assert fc.adjust_speed(1, -12000) == fc.SPEED_MIN and fc.adjust_speed(100, 0) == 100


def test_camera_file_format():
    text = fc.format_camera(7, fc.CamPose(1000.5, 25, -3.25, 90, -10), (1, 0, -2.5))
    assert text == "WE_CAM 1\nseq 7\nP|1000.5|25|-3.25|90|-10|1|0|-2.5\nend\n"
    assert fc.format_camera(8, fc.CamPose(0, 0, 0, 0, 10), pitch_sign=-1).splitlines()[2] == "P|0|0|0|0|-10|0|0|0"
    assert fc.format_camera(9, None).splitlines()[2] == "off"


def test_free_camera_update_and_feedback():
    cam = fc.FreeCamera(speed=10, sens=0.1)
    cam.start((0, 20, 0), (0, 0, 1))
    text = cam.update(0.2, fc.CamInput(forward=True), dx=0, dy=-200)   # вверх 20°, вперёд 2 м
    assert cam.seq == 1 and abs(cam.pose.z - 2) < 1e-9 and abs(cam.pose.pitch - 20) < 1e-9
    assert text.splitlines()[2].startswith("P|0|20|2|0|20|")
    assert cam.update(10, fc.CamInput(forward=True)) and abs(cam.pose.z - 4.5) < 1e-9   # dt обрезан до 0.25
    # скрипт подтвердил: камера там же, взгляд вверх — знак верный
    assert cam.feedback(True, cam.pose.pos, (0, 0.34, 0.94)) is None and cam.confirmed
    assert cam.pitch_sign == 1


def test_pitch_sign_is_fixed_once_if_engine_is_inverted():
    cam = fc.FreeCamera()
    cam.start((0, 20, 0), (0, 0, 1))
    cam.update(0.016, fc.CamInput(), dy=-300)                   # pitch +36°
    note = cam.feedback(True, cam.pose.pos, (0, -0.59, 0.81))    # движок смотрит вниз
    assert note and cam.pitch_sign == -1
    assert cam.update(0.016, fc.CamInput()).splitlines()[2].split("|")[5] == "-36"
    cam.feedback(True, cam.pose.pos, (0, 0.59, 0.81))            # дальше не дёргаем
    assert cam.pitch_sign == -1


def test_mismatch_when_buldozer_keeps_its_own_camera():
    cam = fc.FreeCamera()
    cam.start((0, 20, 0), (0, 0, 1))
    for _ in range(200):                                         # 3.2 с
        cam.update(0.016, fc.CamInput())
        cam.feedback(True, (500, 20, 500), (0, 0, 1))
    assert cam.mismatch and not cam.confirmed


# ---------------------------------------------------------------- перехват ввода (без WinAPI)

def test_input_hook_swallows_movement_keys_but_not_modifiers_or_releases():
    hook = wb.InputHook([0x57, 0x41, 0x20, 0x11, 0x10, 0x02])   # W A Space Ctrl Shift ПКМ
    assert hook.swallow == {0x57, 0x41, 0x20}
    assert not hook.on_key(0x57, True)                          # перехват выключен — проходит
    assert hook.is_down(0x57)                                   # но состояние помним
    hook.capture_keys = True
    assert hook.on_key(0x41, True) and hook.is_down(0x41)       # A не дойдёт до Buldozer
    assert not hook.on_key(0x41, False) and not hook.is_down(0x41)   # отпускание — проходит
    assert not hook.on_key(0x44, True)                          # D не наша клавиша (её нет в списке)
    assert not hook.on_key(0x57, True, injected=True)           # программные нажатия не трогаем


def test_input_hook_mouse_deltas_and_wheel():
    hook = wb.InputHook([])
    assert not hook.on_move(510, 400, (500, 400))               # не перехватываем — курсор свободен
    hook.capture_mouse = True
    assert hook.on_move(510, 395, (500, 400)) and hook.on_move(503, 400, (500, 400))
    assert not hook.on_wheel(-120)                              # колесо — только над Buldozer
    hook.capture_wheel = True
    assert hook.on_wheel(-120) and not hook.on_wheel(120, over_target=False)   # над меню — меню
    assert hook.take() == (13, -5, -120) and hook.take() == (0, 0, 0)
    assert hook.install() is False                              # не Windows (conftest)


def test_input_hook_look_button_only_over_active_buldozer():
    hook = wb.InputHook([], look_vk=wb.VK_LBUTTON)
    assert hook.look_is_button and not hook.look_held()
    assert not hook.on_button(1, True)                          # Buldozer не активен — щелчок его
    assert not hook.look_held() and not hook.on_button(1, False)   # и отпускание тоже его
    hook.capture_keys = True
    assert not hook.on_button(1, True, over_target=False)       # щелчок по нашему меню — меню
    assert not hook.on_button(1, False)
    assert hook.on_button(1, True) and hook.look_held() and hook.capture_mouse   # обзор сразу
    assert not hook.on_button(2, True)                          # ПКМ — не наша, проходит
    assert hook.on_button(1, False) and not hook.look_held() and not hook.capture_mouse
    hook.capture_keys = False                                   # нажали в Buldozer, ушли в меню —
    hook.capture_keys = True
    hook.on_button(1, True)
    hook.capture_keys = False
    assert hook.on_button(1, False)                             # отпускание всё равно наше


def test_input_hook_short_still_press_is_a_click(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(wb.time, "monotonic", lambda: now[0])
    hook = wb.InputHook([], look_vk=wb.VK_LBUTTON)
    hook.capture_keys = True
    hook.on_button(1, True)
    hook.on_move(502, 501, (500, 500))                          # дрогнула рука — всё ещё щелчок
    hook.on_button(1, False)
    assert hook.take_clicks() == 1 and hook.take_clicks() == 0
    hook.on_button(1, True)
    hook.on_move(530, 500, (500, 500))                          # повёл мышь — это поворот камеры
    hook.on_button(1, False)
    assert hook.take_clicks() == 0
    hook.on_button(1, True)
    now[0] += wb.CLICK_MAX_SECONDS + 0.1                        # долго держал — тоже не щелчок
    hook.on_button(1, False)
    assert hook.take_clicks() == 0
    hook.on_button(1, True, over_target=False)                  # щелчок по нашему меню — не наш
    hook.on_button(1, False)
    assert hook.take_clicks() == 0


# ---------------------------------------------------------------- скрипт моста

def test_script_v5_drives_camera_every_frame():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 5
    assert 'GetUpdateQueue(CALL_CATEGORY_SYSTEM).Insert(CamFrame)' in block
    assert 'CreateObject("staticcamera"' in block and "m_Cam.SetActive(true)" in block
    assert f'$profile:{bb.CAM_FILE}' in block and "ReleaseCamera();" in block
    assert f"m_CamClock - m_CamSeenAt > {bb.CAM_TIMEOUT:.1f}" in block     # окно пропало — отдать
    st = bb.parse_state("WE_BRIDGE 1\nseq 1\nFC 1\nFCE\nend\n")
    assert st.free_camera and st.free_camera_failed
    assert not bb.parse_state("WE_BRIDGE 1\nend\n").free_camera


# ---------------------------------------------------------------- окно над Buldozer

@pytest.fixture
def overlay(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from test_buldozer_overlay import make_overlay
    yield from make_overlay(tmp_path, monkeypatch)


class FakeHook(wb.InputHook):
    instances = []

    def install(self):
        self._thread = object()
        FakeHook.instances.append(self)
        return True


@pytest.fixture
def cam_env(overlay, monkeypatch):
    from test_buldozer_overlay import write_state, _fake_keys
    from terrain_editor import buldozer_overlay as bo
    FakeHook.instances.clear()
    overlay._hook_factory = FakeHook
    overlay.set_wanted(True)
    overlay.window.models.setCurrentRow(0)
    write_state(overlay.profile, "WE_BRIDGE 1\nseq 1\ngen -1\nhit 100500 20000 200250\n"
                                 "cam 100000 50000 200000\ndir 0 0 1000\nend\n")
    overlay._tick()
    overlay._bz = 12345
    down = set()
    fg = [True]
    _fake_keys(monkeypatch, down, True)
    monkeypatch.setattr(bo.wb, "foreground_is", lambda h: fg[0])
    warps = []
    monkeypatch.setattr(bo.wb, "client_rect", lambda h: (0, 0, 1600, 900))
    monkeypatch.setattr(bo.wb, "set_cursor_pos", lambda x, y: warps.append((x, y)) or True)
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: (800, 450))
    monkeypatch.setattr(bo.wb, "is_window", lambda h: h == 12345)
    monkeypatch.setattr(bo.wb, "find_buldozer_window", lambda: 12345)
    monkeypatch.setattr(bo.wb, "pointer_in", lambda h: (0.25, 0.75) if h == 12345 else None)
    clock = [1000.0]
    monkeypatch.setattr(bo.time, "monotonic", lambda: clock[0])
    return overlay, down, fg, warps, clock


def _cam_file(overlay):
    return (overlay.profile / bb.CAM_FILE).read_text(encoding="utf-8").splitlines()


def _step(overlay, clock, dt=0.016):
    clock[0] += dt
    overlay._cam_tick()


def test_camera_starts_from_buldozer_view_and_flies(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    assert overlay.camera.active and FakeHook.instances
    hook = FakeHook.instances[0]
    assert {0x57, 0x53, 0x41, 0x44, 0x20} <= hook.swallow and 0x11 not in hook.swallow
    assert hook.look_vk == wb.VK_LBUTTON and hook.target == 12345
    assert hook.capture_keys and hook.capture_wheel
    assert not hook.capture_mouse and not warps                 # курсор свободен, никуда не прыгает
    p = _cam_file(overlay)[2].split("|")
    assert p[:4] == ["P", "100", "50", "200"]
    hook.on_key(0x57, True)                                     # W
    _step(overlay, clock, 0.1)
    assert abs(overlay.camera.pose.z - 203.0) < 1e-6            # 30 м/с × 0.1 с на север
    hook.on_key(0x57, False)
    hook.capture_mouse = True                                   # без ЛКМ сдвиги не крутят камеру
    hook.on_move(900, 450, (800, 450))
    _step(overlay, clock)
    assert overlay.camera.pose.yaw == 0.0 and not hook.capture_mouse
    hook.on_wheel(120)
    _step(overlay, clock)
    assert abs(overlay.camera.speed - 37.5) < 1e-6 and "38 м/с" in overlay.window.status.text()
    assert "ЛКМ (держать) + мышь — обзор" in overlay.window.status.text()


def test_camera_turns_only_while_left_button_held(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    assert hook.on_button(1, True)                              # ЛКМ над Buldozer — обзор
    hook.on_move(900, 450, (800, 450))                          # вправо на 100 px
    _step(overlay, clock)
    assert abs(overlay.camera.pose.yaw - 12.0) < 1e-6 and hook.capture_mouse
    assert "обзор мышью" in overlay.window.status.text()
    assert hook.on_button(1, False)                             # отпустили — мышь свободна
    _step(overlay, clock)
    assert not hook.capture_mouse and not overlay._cam_mouse
    hook.on_move(1000, 450, (800, 450))                         # не перехвачено — не считается
    _step(overlay, clock)
    assert abs(overlay.camera.pose.yaw - 12.0) < 1e-6
    fg[0] = False                                               # активно наше меню
    _step(overlay, clock)
    assert not hook.capture_keys and not hook.capture_wheel     # печатать и листать можно
    assert not hook.on_button(1, True)                          # щёлкать по меню — тоже


def test_mouse_goes_to_model_while_rotate_combo_held(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    hook.on_button(1, True)                                     # обзор…
    _step(overlay, clock)
    down.add(wb.VK_RBUTTON)
    overlay._poll_keys()                                        # …и ПКМ — поворот модели
    assert overlay._drag is not None and overlay._warp is False
    hook.on_move(860, 450, (800, 450))                          # +60 px -> +30° модели
    _step(overlay, clock)
    assert overlay.window.yaw.value() == 30.0 and overlay.camera.pose.yaw == 0.0
    down.clear()
    overlay._poll_keys()
    hook.on_move(860, 450, (800, 450))
    _step(overlay, clock)
    assert overlay.window.yaw.value() == 30.0 and abs(overlay.camera.pose.yaw - 7.2) < 1e-6


def test_camera_off_with_window_and_shutdown_without_script_confirmation(cam_env):
    from test_buldozer_overlay import write_state
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    assert overlay.camera.active
    overlay.set_wanted(False)                                   # F8 — окно скрыто
    _step(overlay, clock)
    assert not overlay.camera.active and overlay._hook is None
    assert _cam_file(overlay)[2] == "off"
    overlay.set_wanted(True)
    _step(overlay, clock)
    assert overlay.camera.active
    clock[0] += 3.5                                             # скрипт старый: «FC 1» нет
    overlay._tick()
    assert not overlay.camera.active and overlay._hook is None
    assert "обновите мост" in overlay.window.status.text()
    _step(overlay, clock)
    assert not overlay.camera.active                            # до следующего F8 не пытаемся
    # скрипт v5 подтверждает — камера держится
    overlay.set_wanted(False)
    overlay.set_wanted(True)
    _step(overlay, clock)
    pose = overlay.camera.pose
    write_state(overlay.profile, "WE_BRIDGE 1\nseq 9\ngen -1\nhit 100500 20000 200250\n"
                                 f"cam {round(pose.x * 1000)} {round(pose.y * 1000)} "
                                 f"{round(pose.z * 1000)}\ndir 0 0 1000\nFC 1\nend\n")
    clock[0] += 3.5
    overlay._tick()
    assert overlay.camera.active and overlay.camera.confirmed


def test_camera_disabled_in_settings(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay.settings["overlay_camera"] = False
    overlay.reload_settings()
    overlay._cam_tick()
    assert not overlay.camera.active and not FakeHook.instances


def test_failed_hook_disables_camera_with_reason(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._hook_factory = wb.InputHook                        # install() -> False (не Windows)
    overlay._cam_tick()
    assert not overlay.camera.active and "перехватить" in overlay.window.status.text()


def _pinhole(eye, fwd, vfov_deg, aspect, y_down=True):
    """Проекция как у движка: доли экрана (0..1) и глубина."""
    yaw = math.atan2(fwd[0], fwd[2])
    pitch = math.asin(max(-1.0, min(1.0, fwd[1])))
    f = fwd
    r = (math.cos(yaw), 0.0, -math.sin(yaw))
    u = (-math.sin(yaw) * math.sin(pitch), math.cos(pitch), -math.cos(yaw) * math.sin(pitch))
    t = math.tan(math.radians(vfov_deg) / 2)

    def project(p):
        d = [a - b for a, b in zip(p, eye)]
        z = sum(a * b for a, b in zip(d, f))
        x = sum(a * b for a, b in zip(d, r)) / z / (t * aspect)
        y = sum(a * b for a, b in zip(d, u)) / z / t
        return (0.5 + x / 2, 0.5 - y / 2 if y_down else 0.5 + y / 2, z)

    def ray_through(sx, sy):
        x = (sx - 0.5) * 2 * t * aspect
        y = ((0.5 - sy) if y_down else (sy - 0.5)) * 2 * t
        v = [fi + ri * x + ui * y for fi, ri, ui in zip(f, r, u)]
        n = math.sqrt(sum(c * c for c in v))
        return tuple(c / n for c in v)
    return project, ray_through


@pytest.mark.parametrize("yaw,pitch,fov,aspect,y_down", [
    (0, 0, 60, 16 / 9, True), (135, -35, 75, 4 / 3, True), (-80, 60, 45, 21 / 9, False),
    (200, -88, 90, 1.0, True)])
def test_pointer_ray_matches_engine_projection(yaw, pitch, fov, aspect, y_down):
    eye = (5000.0, 120.0, 7000.0)
    fwd = fc.forward_vector(yaw, pitch)
    project, ray_through = _pinhole(eye, fwd, fov, aspect, y_down)
    for ptr in ((0.5, 0.5), (0.1, 0.9), (0.97, 0.03), (0.25, 0.6)):
        got = fc.pointer_ray(eye, fwd, ptr, project)
        want = ray_through(*ptr)
        assert got is not None and math.dist(got, want) < 1e-9
        hit = tuple(e + g * 37.0 for e, g in zip(eye, got))      # и обратно: точка — под курсором
        sx, sy, _z = project(hit)
        assert abs(sx - ptr[0]) < 1e-9 and abs(sy - ptr[1]) < 1e-9
    assert fc.pointer_ray(eye, (0.0, 1.0, 0.0), (0.5, 0.5), project) is None   # строго вверх


def test_camera_file_carries_pointer_and_script_uses_it(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    lines = _cam_file(overlay)
    assert lines[2].startswith("P|") and lines[3] == "M|0.25000|0.75000" and lines[4] == "end"
    assert fc.format_camera(1, fc.CamPose(0, 0, 0, 0, 0)).splitlines()[3] == "end"   # без курсора
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 6
    assert 'line.IndexOf("M|") == 0' in block and "GetScreenPosRelative" in block
    assert "if (m_Cam && m_Cam.IsActive() && CursorPoint(ptrX, ptrY))" in block
    assert "rayDir = PointerRay(camPos, camDir, ptrX, ptrY, rayDir);" in block


def test_script_v7_shows_dayz_cursor_and_aims_under_it():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 7
    frame = block[block.index("void CamFrame(float dt)"):block.index("protected void ShowGameCursor()")]
    assert "ShowGameCursor();" in frame                         # каждый кадр своей камеры
    show = block[block.index("protected void ShowGameCursor()"):block.index("protected void HideGameCursor()")]
    assert "ui.IsCursorVisible()" in show and "ui.ShowCursor(true);" in show
    hide = block[block.index("protected void HideGameCursor()"):block.index("protected bool CursorPoint(")]
    assert "!ui.GetMenu()" in hide and "ui.ShowCursor(false);" in hide   # меню Esc не трогаем
    release = block[block.index("protected void ReleaseCamera()"):]
    assert release.index("HideGameCursor();") < release.index("if (!m_Cam)")
    point = block[block.index("protected bool CursorPoint("):block.index("protected void ReleaseCamera()")]
    assert point.index("GetMousePos(mx, my);") < point.index("m_PtrX")  # сначала курсор DayZ
    assert "CursorPoint(ptrX, ptrY)" in block and 'FPrintln(file, "CU 1");' in block
    st = bb.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nFC 1\nCU 1\nend\n")
    assert st.complete and st.free_camera and st.game_cursor
    assert not bb.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nFC 1\nend\n").game_cursor


def test_script_v11_retakes_mouse_focus_and_returns_all_of_it():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 11
    show = block[block.index("protected void ShowGameCursor()"):block.index("protected void HideGameCursor()")]
    # каждый кадр: мышь снова у игры (меню закрылось / ResetGameFocus) — забрать снова и посчитать
    assert "if (input && input.HasGameFocus(INPUT_DEVICE_MOUSE))" in show
    assert show.count("ChangeGameFocus(1, INPUT_DEVICE_MOUSE)") == 1 and "m_FocusAdds++;" in show
    assert "m_FocusRetakes++;" in show
    assert show.index("ChangeGameFocus") < show.index("ui.ShowCursor(true);")
    hide = block[block.index("protected void HideGameCursor()"):block.index("protected bool CursorPoint(")]
    assert "for (int i = 0; i < m_FocusAdds; i++)" in hide
    assert hide.index("HasGameFocus(INPUT_DEVICE_MOUSE)") < hide.index("ChangeGameFocus(-1, INPUT_DEVICE_MOUSE)")
    assert hide.index("ChangeGameFocus(-1") < hide.index("if (!m_CursorShown)")   # отдать всегда
    assert "m_FocusTaken" not in block
    assert 'FPrintln(file, "MF 1");' in block and '"FR " + m_FocusRetakes.ToString()' in block
    st = bb.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nMF 1\nFR 4\nend\n")
    assert st.mouse_focus and st.focus_retakes == 4
    assert bb.CAM_TIMEOUT >= 4.0 and f"> {bb.CAM_TIMEOUT:.1f})" in block


def test_script_v9_invisible_menu_instead_of_esc_menu():
    block = bb.script_block()
    assert bb.SCRIPT_REVISION >= 9
    menu = block[block.index("class WE_CursorMenu : UIScriptedMenu"):block.index("class WE_BuldozerBridge")]
    assert "override Widget Init()" in menu and "CreateWidget(FrameWidgetTypeID" in menu
    assert "WidgetFlags.IGNOREPOINTER" in menu and "return layoutRoot;" in menu
    frame = block[block.index("void CamFrame(float dt)"):block.index("protected void ShowCursorMenu()")]
    assert frame.index("ShowCursorMenu();") < frame.index("ShowGameCursor();")
    show = block[block.index("protected void ShowCursorMenu()"):block.index("protected void HideCursorMenu()")]
    assert "ui.ShowScriptedMenu(m_CursorMenu, NULL);" in show and "top.GetSubMenu()" in show
    assert show.index("if (!top)") < show.index("ShowScriptedMenu")          # только если меню нет
    assert "GetLayoutRoot().Show(false);" in show and "m_HiddenMenu = other;" in show
    hide = block[block.index("protected void HideCursorMenu()"):block.index("protected bool CursorMenuOpen()")]
    assert "m_HiddenMenu.Close();" in hide and "ui.HideScriptedMenu(m_CursorMenu);" in hide
    release = block[block.index("protected void ReleaseCamera()"):]
    assert release.index("HideCursorMenu();") < release.index("HideGameCursor();") < release.index("if (!m_Cam)")
    assert "protected ref WE_CursorMenu m_CursorMenu;" in block          # держим ссылку, как инвентарь
    assert 'FPrintln(file, "UM 1");' in block and 'FPrintln(file, "UH 1");' in block
    st = bb.parse_state("WE_BRIDGE 1\nseq 3\ngen 1\nUM 1\nUH 1\nend\n")
    assert st.cursor_menu and st.menu_hidden


def _age_state(overlay, seconds):
    import os
    import time as real_time
    path = overlay.profile / bb.STATE_FILE
    t = real_time.time() - seconds
    os.utime(path, (t, t))


def test_esc_is_swallowed_while_camera_and_still_deselects(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    assert wb.VK_ESCAPE in hook.swallow                        # меню игры по Esc не откроется
    assert hook.on_key(wb.VK_ESCAPE, True) is True             # Buldozer его не получит
    assert overlay._key_down(wb.VK_ESCAPE)                     # а наше «снять выделение» — видит
    hook.on_key(wb.VK_ESCAPE, False)
    assert not overlay._key_down(wb.VK_ESCAPE)


def test_short_link_gap_keeps_camera_long_gap_stops_it(cam_env):
    overlay, down, fg, warps, clock = cam_env
    overlay._tick()                                            # связь есть «сейчас» (часы теста)
    overlay._cam_tick()
    assert overlay.camera.active
    _age_state(overlay, 3.0)                                   # файл состояния не обновился 3 с
    clock[0] += 3.0
    overlay._tick()
    assert not overlay._connected
    _step(overlay, clock)
    assert overlay.camera.active and overlay._hook is not None  # курсор не отдаём игре
    clock[0] += bb_grace()
    _step(overlay, clock)
    assert not overlay.camera.active and overlay._hook is None  # Buldozer правда пропал


def bb_grace():
    from terrain_editor import buldozer_overlay as bo
    return bo.CONNECT_GRACE


def test_buldozer_window_kept_when_search_misses_once(cam_env, monkeypatch):
    from terrain_editor import buldozer_overlay as bo
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    monkeypatch.setattr(bo.wb, "find_buldozer_window", lambda: None)
    monkeypatch.setattr(bo.wb, "is_minimized", lambda h: False)
    overlay._tick_no = 0
    overlay._tick()
    assert overlay._bz == 12345
    _step(overlay, clock)
    assert overlay.camera.active
    monkeypatch.setattr(bo.wb, "is_window", lambda h: False)  # окно закрыли
    overlay._tick_no = 0
    overlay._tick()
    assert overlay._bz is None


def test_hook_reinstalled_when_windows_drops_it(cam_env, monkeypatch, caplog):
    import logging
    from terrain_editor import buldozer_overlay as bo
    overlay, down, fg, warps, clock = cam_env
    pos = [(800, 450)]
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: pos[0])
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    hook.seen = clock[0]
    for i in range(3):                                         # события идут — всё хорошо
        pos[0] = (810 + i, 450)
        hook.seen = clock[0]
        _step(overlay, clock)
    assert len(FakeHook.instances) == 1 and overlay._hook_restarts == 0
    clock[0] += 1.0
    _step(overlay, clock)                                      # курсор стоит — молчание нормально
    assert overlay._hook_restarts == 0
    with caplog.at_level(logging.WARNING):
        pos[0] = (900, 500)                                    # курсор двигается, а событий нет
        _step(overlay, clock)
    assert overlay._hook_restarts == 1 and "reinstalling" in caplog.text
    assert overlay._hook is hook and hook.installed and hook.seen == clock[0]
    assert overlay.camera.active


def test_focus_retakes_are_logged(cam_env, caplog):
    import logging
    from test_buldozer_overlay import write_state
    overlay, down, fg, warps, clock = cam_env
    overlay._cam_tick()
    pose = overlay.camera.pose
    write_state(overlay.profile, "WE_BRIDGE 1\nseq 9\ngen -1\nhit 100500 20000 200250\n"
                                 f"cam {round(pose.x * 1000)} {round(pose.y * 1000)} "
                                 f"{round(pose.z * 1000)}\ndir 0 0 1000\nFC 1\nMF 1\nFR 2\nend\n")
    from terrain_editor import buldozer_overlay as bo
    with caplog.at_level(logging.INFO, logger=bo.log.name):
        overlay._tick()
    assert "took it again (2 times" in caplog.text


def test_cursor_guard_returns_cursor_centered_by_game(cam_env, monkeypatch, caplog):
    import logging
    from terrain_editor import buldozer_overlay as bo
    overlay, down, fg, warps, clock = cam_env
    cursor = [300, 200]
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    monkeypatch.setattr(bo.wb, "set_cursor_pos", lambda x, y: warps.append((x, y)) or cursor.__setitem__(slice(0, 2), [x, y]) or True)
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    hook.seen = clock[0]
    hook.last_move, hook.move_at = (300, 200), clock[0]       # пользователь подвёл курсор
    _step(overlay, clock, 0.1)
    assert overlay._guard_pos == (300, 200) and not warps
    caplog.set_level(logging.INFO, logger=bo.log.name)
    cursor[:] = [800, 450]                                    # игра: курсор в центр окна (SetCursorPos)
    _step(overlay, clock, 0.016)
    assert warps == [(300, 200)] and cursor == [300, 200] and overlay._guard_fixed == 1
    assert "cursor jump #1" in caplog.text and "(return point)" in caplog.text
    assert len(FakeHook.instances) == 1                       # сторож перехвата не спутал с рукой
    cursor[:] = [1200, 300]                                   # прыжок не в центр — не трогаем
    _step(overlay, clock, 0.016)
    assert len(warps) == 1 and overlay._guard_pos == (1200, 300)
    hook.last_move, hook.move_at = (1100, 600), clock[0]      # мышь двигают — не мешаем
    cursor[:] = [1100, 600]
    _step(overlay, clock, 0.016)
    assert len(warps) == 1 and overlay._guard_pos == (1100, 600)
    for _ in range(bo.GUARD_MAX_FIXES + 3):                   # игра держит курсор — не воюем
        cursor[:] = [800, 450]
        _step(overlay, clock, 0.1)
    assert overlay._guard_fixed == bo.GUARD_MAX_FIXES and cursor == [800, 450]   # пауза
    assert overlay._guard_paused_until > clock[0] and "keeps centering" in caplog.text
    hook.capture_mouse = False
    hook._look_ours = True                                    # обзор (ЛКМ) — сторож молчит
    _step(overlay, clock, 0.016)
    assert overlay._guard_pos is None


def test_cursor_guard_learns_game_return_point_and_rotation_ignores_it(cam_env, monkeypatch):
    from terrain_editor import buldozer_overlay as bo
    overlay, down, fg, warps, clock = cam_env
    cursor = [300, 200]
    monkeypatch.setattr(bo.wb, "cursor_pos", lambda: tuple(cursor))
    monkeypatch.setattr(bo.wb, "set_cursor_pos",
                        lambda x, y: warps.append((x, y)) or cursor.__setitem__(slice(0, 2), [x, y]) or True)
    overlay._cam_tick()
    hook = FakeHook.instances[0]
    hook.last_move, hook.move_at = (300, 200), clock[0]
    _step(overlay, clock, 0.1)
    cursor[:] = [808, 469]                # как в журнале: (969, 590) при центре окна (961, 571)
    _step(overlay, clock, 0.1)
    assert not warps and overlay._guard_pos == (808, 469)     # первый раз — ещё не знаем
    hook.last_move, hook.move_at = (400, 300), clock[0]
    cursor[:] = [400, 300]
    _step(overlay, clock, 0.1)
    cursor[:] = [808, 469]                                    # второй раз туда же — точка возврата
    _step(overlay, clock, 0.1)
    assert warps == [(400, 300)] and overlay.settings["overlay_cursor_targets"] == [[8, 19]]
    # новое окно помнит точку с первого прыжка
    overlay._load_keys(overlay.settings)
    assert overlay._jump_targets == [(8, 19)] and overlay._is_return_point((808, 469))
    assert not overlay._is_return_point((700, 469))
    # поворот модели (ПКМ): прыжок курсора в точку возврата не крутит модель
    overlay._drag, overlay._drag_mode, overlay._warp = (500, 400), "rotate", True
    down.add(bo.wb.VK_RBUTTON)
    cursor[:] = [808, 469]
    yaw = overlay.window.yaw.value()
    overlay._handle_mouse()
    assert overlay.window.yaw.value() == yaw and cursor == [500, 400]
    cursor[:] = [520, 400]                                    # а рука — крутит
    overlay._handle_mouse()
    assert overlay.window.yaw.value() == (yaw + 20 * overlay._yaw_per_px) % 360
