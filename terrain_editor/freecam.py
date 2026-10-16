"""Своя камера в Buldozer: WASD + мышь (логика без Qt и WinAPI, тестируется).

Окно редактора считает позу камеры и ~60 раз в секунду пишет её в файл ``CAM_FILE`` в
``$profile:``; скрипт моста (v5) каждый кадр ставит туда свою камеру (``staticcamera``) и между
записями двигает её по присланной скорости — движение плавное на любом FPS.

Координаты DayZ: X — восток, Z — север, Y — вверх. Поворот (yaw) — градусы от севера по часовой
стрелке (как ``vector.VectorToAngles``), наклон (pitch) — вверх положительный.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

CAM_VERSION = 1
PITCH_LIMIT = 89.0
SPEED_MIN, SPEED_MAX = 0.5, 2000.0
WHEEL_STEP = 1.25            # колесо мыши: ×1.25 к скорости за щелчок
WHEEL_DELTA = 120            # один щелчок колеса в единицах Windows


@dataclass(frozen=True)
class CamPose:
    x: float
    y: float
    z: float
    yaw: float = 0.0
    pitch: float = 0.0

    @property
    def pos(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class CamInput:
    forward: bool = False
    back: bool = False
    left: bool = False
    right: bool = False
    up: bool = False
    down: bool = False
    fast: bool = False

    def any_move(self) -> bool:
        return self.forward or self.back or self.left or self.right or self.up or self.down


def pose_from_camera(pos, direction) -> CamPose:
    """Поза из позиции и направления камеры Buldozer (строки ``cam``/``dir`` состояния)."""
    dx, dy, dz = direction
    length = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    dx, dy, dz = dx / length, dy / length, dz / length
    yaw = math.degrees(math.atan2(dx, dz)) % 360.0 if (abs(dx) + abs(dz)) > 1e-9 else 0.0
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, dy))))
    pitch = max(-PITCH_LIMIT, min(PITCH_LIMIT, pitch))
    return CamPose(float(pos[0]), float(pos[1]), float(pos[2]), yaw, pitch)


def forward_vector(yaw: float, pitch: float = 0.0) -> tuple[float, float, float]:
    y, p = math.radians(yaw), math.radians(pitch)
    return (math.sin(y) * math.cos(p), math.sin(p), math.cos(y) * math.cos(p))


def right_vector(yaw: float) -> tuple[float, float, float]:
    y = math.radians(yaw)
    return (math.cos(y), 0.0, -math.sin(y))


def look(pose: CamPose, dx: float, dy: float, sens: float, invert_y: bool = False) -> CamPose:
    """Мышь: вправо — поворот вправо, вверх (dy < 0) — взгляд вверх."""
    if not dx and not dy:
        return pose
    yaw = (pose.yaw + dx * sens) % 360.0
    pitch = pose.pitch + (dy if invert_y else -dy) * sens
    pitch = max(-PITCH_LIMIT, min(PITCH_LIMIT, pitch))
    return replace(pose, yaw=yaw, pitch=pitch)


def move_velocity(pose: CamPose, inp: CamInput, speed: float, fast_mult: float = 4.0,
                  along_view: bool = False) -> tuple[float, float, float]:
    """Скорость (м/с) по нажатым клавишам. ``along_view`` — W/S по взгляду (иначе горизонтально)."""
    f = forward_vector(pose.yaw, pose.pitch if along_view else 0.0)
    r = right_vector(pose.yaw)
    a = int(inp.forward) - int(inp.back)
    b = int(inp.right) - int(inp.left)
    c = int(inp.up) - int(inp.down)
    vx = f[0] * a + r[0] * b
    vy = f[1] * a + c
    vz = f[2] * a + r[2] * b
    length = math.sqrt(vx * vx + vy * vy + vz * vz)
    if length < 1e-9:
        return (0.0, 0.0, 0.0)
    k = speed * (fast_mult if inp.fast else 1.0) / length
    return (vx * k, vy * k, vz * k)


def advance(pose: CamPose, velocity, dt: float) -> CamPose:
    return replace(pose, x=pose.x + velocity[0] * dt, y=pose.y + velocity[1] * dt,
                   z=pose.z + velocity[2] * dt)


def adjust_speed(speed: float, wheel: int) -> float:
    """Колесо мыши (в единицах Windows, 120 = щелчок): вверх — быстрее, вниз — медленнее."""
    if not wheel:
        return speed
    return max(SPEED_MIN, min(SPEED_MAX, speed * WHEEL_STEP ** (wheel / WHEEL_DELTA)))


def _f(v: float) -> str:
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def format_camera(seq: int, pose: CamPose | None, velocity=(0.0, 0.0, 0.0),
                  pitch_sign: float = 1.0, pointer: tuple[float, float] | None = None) -> str:
    """Файл камеры для скрипта. ``pose=None`` — вернуть камеру Buldozer (``off``).

    ``pointer`` — где курсор над Buldozer (доли окна 0..1, сверху вниз): по нему скрипт v6
    ищет точку «под курсором» (у своей камеры движок положение курсора не обновляет)."""
    lines = [f"WE_CAM {CAM_VERSION}", f"seq {int(seq)}"]
    if pose is None:
        lines.append("off")
    else:
        vx, vy, vz = velocity
        lines.append("|".join(["P", _f(pose.x), _f(pose.y), _f(pose.z), _f(pose.yaw),
                               _f(pose.pitch * pitch_sign), _f(vx), _f(vy), _f(vz)]))
        if pointer is not None:
            lines.append(f"M|{pointer[0]:.5f}|{pointer[1]:.5f}")
    lines.append("end")
    return "\n".join(lines) + "\n"


def pointer_ray(eye, fwd, pointer, project):
    """Луч через курсор — то же, что ``PointerRay`` в скрипте моста (v6), для проверки.

    ``project(point) -> (sx, sy, depth)`` — проекция движка (``GetScreenPosRelative``): доли
    экрана. Калибруем её по двум точкам на плоскости в 10 м поперёк взгляда (там экранные
    координаты линейны) и решаем 2×2 — так не важны ни FOV, ни соотношение сторон, ни
    направление осей экрана. None — проекция вырождена (скрипт тогда берёт луч движка).
    """
    fx, fy, fz = fwd
    rx, rz = fz, -fx
    length = math.hypot(rx, rz)
    if length < 0.001:
        return None
    right = (rx / length, 0.0, rz / length)
    up = (fy * right[2] - fz * right[1], fz * right[0] - fx * right[2], fx * right[1] - fy * right[0])
    center = tuple(e + f * 10.0 for e, f in zip(eye, fwd))
    s0 = project(center)
    sx = project(tuple(c + r for c, r in zip(center, right)))
    sy = project(tuple(c + u for c, u in zip(center, up)))
    if s0[2] <= 0:
        return None
    ax, ay = sx[0] - s0[0], sx[1] - s0[1]
    bx, by = sy[0] - s0[0], sy[1] - s0[1]
    det = ax * by - bx * ay
    if abs(det) < 1e-7:
        return None
    px, py = pointer[0] - s0[0], pointer[1] - s0[1]
    a = (px * by - bx * py) / det
    b = (ax * py - ay * px) / det
    ray = tuple(f * 10.0 + r * a + u * b for f, r, u in zip(fwd, right, up))
    n = math.sqrt(sum(c * c for c in ray))
    return tuple(c / n for c in ray)


class FreeCamera:
    """Состояние своей камеры: поза, скорость, номер записи и проверка по ответу скрипта.

    * ``start(cam, dir)`` — начать с того места, куда смотрела камера Buldozer;
    * ``update(dt, input, dx, dy, wheel)`` — такт (~60 Гц): мышь, клавиши, колесо -> текст файла;
    * ``feedback(state)`` — сравнить с тем, что сообщает скрипт: включилась ли камера, не
      перевёрнут ли наклон (знак pitch у ``SetOrientation`` проверяем по ``dir``).
    """

    MISMATCH_AFTER = 2.0     # с: столько камера Buldozer «не там» — предупредить

    def __init__(self, speed: float = 30.0, sens: float = 0.12, fast_mult: float = 4.0,
                 invert_y: bool = False, along_view: bool = False) -> None:
        self.speed = speed
        self.sens = sens
        self.fast_mult = fast_mult
        self.invert_y = invert_y
        self.along_view = along_view
        self.pose: CamPose | None = None
        self.velocity = (0.0, 0.0, 0.0)
        self.seq = 0
        self.pitch_sign = 1.0
        self._sign_checked = False
        self.clock = 0.0
        self._mismatch_since: float | None = None
        self.mismatch = False       # скрипт говорит «камера включена», но камера Buldozer не там
        self.confirmed = False      # скрипт подтвердил: камера включена и стоит где надо

    @property
    def active(self) -> bool:
        return self.pose is not None

    def configure(self, speed=None, sens=None, fast_mult=None, invert_y=None, along_view=None) -> None:
        if speed is not None:
            self.speed = max(SPEED_MIN, min(SPEED_MAX, float(speed)))
        if sens is not None:
            self.sens = float(sens)
        if fast_mult is not None:
            self.fast_mult = float(fast_mult)
        if invert_y is not None:
            self.invert_y = bool(invert_y)
        if along_view is not None:
            self.along_view = bool(along_view)

    def start(self, cam_pos, cam_dir) -> None:
        self.pose = pose_from_camera(cam_pos, cam_dir)
        self.velocity = (0.0, 0.0, 0.0)
        self._mismatch_since = None
        self.mismatch = False
        self.confirmed = False

    def stop(self) -> str:
        """Выключить; вернуть текст файла ``off``."""
        self.pose = None
        self.velocity = (0.0, 0.0, 0.0)
        self.seq += 1
        return format_camera(self.seq, None)

    def update(self, dt: float, inp: CamInput, dx: float = 0.0, dy: float = 0.0,
               wheel: int = 0, pointer: tuple[float, float] | None = None) -> str:
        if self.pose is None:
            raise RuntimeError("camera is not started")
        dt = max(0.0, min(dt, 0.25))         # подвисло окно — не прыгать на сотни метров
        self.clock += dt
        self.speed = adjust_speed(self.speed, wheel)
        self.pose = look(self.pose, dx, dy, self.sens, self.invert_y)
        self.velocity = move_velocity(self.pose, inp, self.speed, self.fast_mult, self.along_view)
        self.pose = advance(self.pose, self.velocity, dt)
        self.seq += 1
        return format_camera(self.seq, self.pose, self.velocity, self.pitch_sign, pointer)

    def feedback(self, free_camera: bool, cam_pos, cam_dir) -> str | None:
        """Ответ скрипта (FC 1, cam, dir). Возвращает сообщение для журнала, если что-то поменяли."""
        if self.pose is None or not free_camera or cam_pos is None:
            self._mismatch_since = None
            return None
        dist = math.dist(cam_pos, self.pose.pos)
        speed = math.sqrt(sum(v * v for v in self.velocity))
        if dist > 3.0 + speed * 0.3:
            if self._mismatch_since is None:
                self._mismatch_since = self.clock
            elif self.clock - self._mismatch_since >= self.MISMATCH_AFTER and not self.mismatch:
                self.mismatch = True
                return f"camera mismatch: buldozer camera at {cam_pos}, ours at {self.pose.pos}"
            return None
        self._mismatch_since = None
        self.mismatch = False
        self.confirmed = True
        if self._sign_checked or cam_dir is None or abs(self.pose.pitch) < 20.0:
            return None
        want = math.sin(math.radians(self.pose.pitch))
        got = cam_dir[1]
        if abs(got) < 0.2:
            return None
        self._sign_checked = True
        if got * want < 0:
            self.pitch_sign = -self.pitch_sign
            return f"camera pitch sign flipped (sent {self.pose.pitch:.1f}, engine dir y {got:.2f})"
        return None
