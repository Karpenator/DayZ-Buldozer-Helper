"""Мост «окно поверх Buldozer» ↔ скрипт внутри Buldozer (без Qt).

Как это устроено (тот же приём, что у MaskOver, github.com/Mahauu/MaskOver):

* В ``P:\\scripts\\buldozer.c`` ставится наш блок на Enforce Script. Buldozer, запущенный из
  Terrain Builder с ``-name=Buldozer``, выполняет функцию ``BuldozerMain()`` из этого файла по
  клавише **F10** (штатная возможность Buldozer for Terrain Builder). После одного F10 блок
  20 раз в секунду:

  - находит точку на земле под курсором мыши и пишет её (и камеру) в файл состояния
    ``$profile:we_bridge_state.txt`` (``$profile:`` — папка из ``-profiles=``, обычно
    ``P:\\Buldozer``);
  - читает файл команд ``$profile:we_bridge_cmd.txt``, который пишет наше окно, и приводит
    сцену Buldozer к списку из этого файла: создаёт новые модели
    (``CreateStaticObjectUsingP3D``), удаляет убранные, двигает «призрак» выбранной модели
    за курсором.

* Объекты, созданные скриптом, Buldozer показывает сразу, но Terrain Builder о них не знает
  (это не его объекты). Поэтому окно хранит список расставленного в сеансе и выгружает его
  в текстовый файл импорта Terrain Builder (формат ``objects.py``). Прямое добавление в проект
  TB (через канал TB ↔ Buldozer) — следующий этап, см. ``OVERLAY_BULDOZER.md``.

Форматы файлов — строки, разделитель полей ``|`` (в путях моделей его не бывает):

Состояние (пишет скрипт)::

    WE_BRIDGE 1
    seq 123                      счётчик записей
    gen 17                       последнее применённое поколение команд
    hit 1119800 19860 860100     точка под курсором, миллиметры (целые — без потери точности)
    cam 1100000 45000 840000     позиция камеры, мм
    dir 707 -300 640             направление камеры ×1000
    Y|5|21345|1200|0             объект 5 создан, мм: высота начала координат, перенос начала
                                 по высоте (BoundingCenter с поворотом и масштабом) и высота
                                 для Terrain Builder (Relative; с v4)
    E|6                          объект 6 создать не удалось (модель не найдена)
    GE                           не удалось создать «призрак»
    FC 1                         включена своя камера (v5); FCE — камеру создать не удалось
    CU 1                         v7: скрипт показал курсор DayZ (пока своя камера)
    MF 1                         v8: скрипт забрал мышь у игры (ChangeGameFocus) — курсор свободен
    FR 3                         v11: сколько раз мышь пришлось забирать снова (её вернули игре:
                                 закрылось меню, ResetGameFocus…) — для журнала
    UM 1                         v9: открыто наше невидимое меню; UH 1 — спрятано меню игры (Esc)
    MO 2                         v12: сколько раз скрипт открывал невидимое меню (его закрывали)
    CS 5                         v12: сколько раз скрипт снова показывал курсор DayZ (его прятали)
    CV 1                         v12: рамка выделения рисуется (CanvasWidget создан); CV 0 — нет
    CF 7                         v13: сколько раз файл камеры не открылся (окно подменяло его) —
                                 камера при этом больше не отдаётся Buldozer
    end

Команды (пишет окно)::

    WE_CMD 1
    gen 18
    G|dz\\structures\\...\\x.p3d|45|1|0     призрак: модель|поворот|масштаб|подъём (пусто = нет)
    O|5|dz\\structures\\...\\x.p3d|1119.8|860.1|45|1|0   объект: id|модель|x|z|поворот|масштаб|подъём
    end

Камера (пишет окно, пока открыто окно F8, ~60 раз в секунду; см. ``freecam.py``)::

    WE_CAM 1
    seq 4512
    P|x|y|z|yaw|pitch|vx|vy|vz   поза (м, градусы) и скорость (м/с) — между записями скрипт
                                 двигает камеру по скорости; вместо P — ``off`` (вернуть камеру)
    M|0.61250|0.40111            v6: курсор над Buldozer (доли окна, сверху вниз) — нет строки:
                                 курсор над другим окном, остаётся прежнее положение
    end

Скрипт применяет файл команд целиком и только если в нём есть ``end`` (файл не дописан —
попробует на следующем такте) и ``gen`` отличается от уже применённого.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Iterable

from .dayzpaths import resolve_work_drive
from .gizmo import Gizmo, parse_gizmo
from .map_edits import MapEdit, Sample
from .objects import TBObject, format_objects

BRIDGE_VERSION = 1              # формат файлов cmd/state
SCRIPT_REVISION = 18            # версия блока в buldozer.c (v2: Managed, без «зомби» после F10;
                                # v3: «призрак» можно закрепить в точке — G|…|x|y|z;
                                # v4: Y|… сообщает готовую высоту для Terrain Builder;
                                # v5: своя камера (WASD + мышь) по файлу CAM_FILE;
                                # v6: точка «под курсором» при своей камере — по строке M|u|v;
                                # v7: при своей камере показан курсор DayZ, точка — под ним;
                                # v8: мышь у интерфейса (ChangeGameFocus), курсор не возвращается;
                                # v9: невидимое меню вместо Esc — курсор свободен, меню игры
                                #     поверх своей камеры прячется;
                                # v10: объекты карты — выбор, перенос, поворот;
                                # v11: мышь забирается у игры снова, если её вернули (курсор
                                #     больше не прыгает в центр «через время»), таймаут камеры 4 с;
                                # v12: рамка выделения на экране (CanvasWidget, каждый кадр),
                                #     счётчики MO/CS — когда игра закрывает меню/прячет курсор;
                                # v13: файл камеры не открылся (окно как раз его подменяло) —
                                #     камера остаётся: раньше она на кадр отдавалась Buldozer,
                                #     меню закрывалось и курсор прыгал в центр «через время»);
                                # v14: V|id|x|z|модель — есть ли в точке такой объект не наш
                                #     (вставку в Terrain Builder видно в Buldozer) -> VF|id|n;
                                # v15: KW|KC_F8,KC_F9,… — скрипт сообщает нажатые из них (KeyState)
                                #     строкой KD|… : клавиши работают и когда Buldozer запущен
                                #     от администратора, а программа нет (Windows ей их не даёт);
                                # v16: гизмо выделенного — стрелки X/Y/Z на экране, их место ->
                                #     GZ|…, подсвеченная ось <- GA|n; T|…|1 — объект карты удалён
                                #     (спрятан под землю, пока строка есть)
                                # v17 (режим «Рельеф») убран; v18 = v16 — номер выше, чтобы
                                #     заменить уже записанный v17
STATE_FILE = "we_bridge_state.txt"
CMD_FILE = "we_bridge_cmd.txt"
CAM_FILE = "we_bridge_cam.txt"   # поза своей камеры (пишет окно ~60 раз в секунду, см. freecam.py)
CAM_TIMEOUT = 4.0               # с: окно не обновляет файл камеры дольше — скрипт отдаёт камеру Buldozer
STATE_MAX_AGE = 2.0            # с: файл состояния старше — скрипт не работает (нет F10/Buldozer закрыт)
SESSION_FILE = Path.home() / ".dayz_terrain_editor" / "buldozer_overlay_session.json"
DEFAULT_SCRIPT_NAME = "Buldozer"     # -name=Buldozer -> P:\scripts\buldozer.c, функция BuldozerMain

MARK_BEGIN = "// >>> DayZ World Editor bridge"
MARK_END = "// <<< DayZ World Editor bridge <<<"
START_CALL = "WE_BridgeStart();"


# ----------------------------------------------------------------- скрипт для Buldozer

SCRIPT_BLOCK = r"""// >>> DayZ World Editor bridge v%(revision)d >>>
// Мост для окна «поверх Buldozer» (DayZ World Editor). Блок ставит и обновляет редактор:
// правки внутри блока будут перезаписаны. Нажмите F10 в Buldozer один раз за сеанс.
// Managed: если F10 нажали ещё раз (модуль скрипта перезагружается), старый экземпляр
// удаляется вместе со своими объектами, а его такт снимается из очереди — без «зомби».

// Пустое невидимое меню: пока оно открыто, движок отдаёт мышь интерфейсу и показывает курсор
// DayZ (как в меню по Esc, но без самого меню). Открыто, пока работает своя камера.
class WE_CursorMenu : UIScriptedMenu
{
	override Widget Init()
	{
		if (!layoutRoot)
			layoutRoot = GetGame().GetWorkspace().CreateWidget(FrameWidgetTypeID, 0, 0, 1, 1, WidgetFlags.VISIBLE | WidgetFlags.IGNOREPOINTER, 0x00000000, 0);
		return layoutRoot;
	}
}

class WE_BuldozerBridge : Managed
{
	static int s_ActiveToken;
	protected int m_Token;
	protected ref map<int, Object> m_Objects;
	protected ref map<int, string> m_Lines;
	protected ref map<int, string> m_Results;
	protected int m_Generation = -1;
	protected int m_Seq;
	protected Object m_Ghost;
	protected string m_GhostLine;
	protected string m_GhostModel;
	protected float m_GhostYaw;
	protected float m_GhostScale = 1.0;
	protected float m_GhostLift;
	protected float m_GhostBottom;
	protected bool m_GhostFailed;
	protected bool m_GhostPinned;
	protected vector m_GhostPin;
	// своя камера: поза из файла камеры, между записями — по скорости (плавно на любом FPS)
	protected Camera m_Cam;
	protected bool m_CamWanted;
	protected bool m_CamFailed;
	protected int m_CamSeq = -1;
	protected float m_CamClock;
	protected float m_CamSeenAt;
	protected vector m_CamPos;
	protected vector m_CamVel;
	protected float m_CamYaw;
	protected float m_CamPitch;
	protected bool m_PtrSet;
	protected bool m_CursorShown;
	protected int m_FocusAdds;         // сколько раз сделали ChangeGameFocus(+1) (вернём столько же)
	protected int m_FocusRetakes;      // мышь вернули игре, пришлось забрать снова (FR)
	protected ref WE_CursorMenu m_CursorMenu;
	protected UIScriptedMenu m_HiddenMenu;
	protected float m_MenuTriedAt = -10.0;
	protected float m_PtrX;
	protected float m_PtrY;
	// v10: объекты карты — выбор лучом, перенос, поворот (реестр «выбранных» по номерам)
	protected int m_Sess;
	protected int m_PickWanted = -1;
	protected int m_PickSeq = -1;
	protected string m_PickResult = "";
	protected int m_RefCounter;
	protected ref map<int, Object> m_Picked;
	protected ref map<int, vector> m_PickPos;
	protected ref map<int, vector> m_PickOri;
	protected ref map<int, string> m_TLines;
	protected int m_DragRef = -1;
	protected bool m_DragStarted;
	protected vector m_DragOffset;
	protected float m_DragRel;
	protected string m_DragResult = "";
	protected int m_SelRef = -1;
	protected Shape m_SelShape;
	protected Object m_SelShapeObj;
	protected string m_Hover = "";
	// v12: рамка выделения — линии на экране (отладочные фигуры Shape в Buldozer не видно)
	protected Object m_HoverObj;
	protected Widget m_CanvasRoot;
	protected CanvasWidget m_Canvas;
	protected bool m_CanvasTried;
	protected int m_MenuOpens;
	protected int m_CursorShows;
	protected int m_CamReadFails;      // v13: файл камеры не открылся (окно подменяло его)
	// v14: проверка вставки в TB — объект этой модели (не наш) в точке нашего объекта
	protected ref map<int, string> m_Verify;
	protected ref map<int, string> m_VerifyOut;
	protected int m_VerifyTick;
	// v15: клавиши, которые просит окно (KW|…), и их коды; нажатые — в KD|…
	protected ref map<string, int> m_KeyCodes;
	protected ref array<string> m_KeyWatch;
	// v16: гизмо выделенного объекта — где стрелки на экране (GZ|…) и какая подсвечена (GA|n)
	protected string m_GizmoLine = "";
	protected int m_GizmoAxis;

	void ~WE_BuldozerBridge()
	{
		if (GetGame())
		{
			GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).RemoveByName(this, "Tick");
			GetGame().GetUpdateQueue(CALL_CATEGORY_SYSTEM).Remove(CamFrame);
			ReleaseCamera();
			RestoreAllPicked();
			DeleteAll();
		}
		DropCanvas();
	}

	protected void DropCanvas()
	{
		if (m_CanvasRoot)
			m_CanvasRoot.Unlink();
		m_CanvasRoot = null;
		m_Canvas = null;
	}

	// Рамка вокруг выделенного объекта (и тонкая — вокруг объекта под курсором): 8 углов
	// габаритов (ClippingInfo) с поворотом и масштабом объекта -> экран, 12 рёбер линиями.
	protected void DrawFrames()
	{
		m_GizmoLine = "";
		if (!m_Canvas)
		{
			if (m_CanvasTried)
				return;
			m_CanvasTried = true;
			m_CanvasRoot = GetGame().GetWorkspace().CreateWidgets("gui/layouts/debug/day_z_debugcanvas.layout");
			if (!m_CanvasRoot)
				return;
			m_CanvasRoot.SetSort(10000);
			m_CanvasRoot.SetFlags(WidgetFlags.IGNOREPOINTER);
			m_Canvas = CanvasWidget.Cast(m_CanvasRoot.FindAnyWidget("CanvasWidget"));
			if (!m_Canvas)
				return;
			m_Canvas.SetFlags(WidgetFlags.IGNOREPOINTER);
		}
		m_Canvas.Clear();
		Object sel = null;
		if (m_SelRef >= 0)
		{
			if (m_Picked)
				sel = m_Picked.Get(m_SelRef);
		}
		if (sel)
		{
			DrawBox(sel, 0xFF33CCFF, 2);
			if (m_Cam)
			{
				if (m_Cam.IsActive())
					DrawGizmo(sel);     // тянуть стрелки можно только со своей камерой
			}
		}
		if (!m_HoverObj)
			return;
		if (m_Ghost)
			return;
		if (!m_Cam)
			return;                 // под курсором — только пока открыто окно (своя камера)
		if (!m_Cam.IsActive())
			return;
		if (m_HoverObj != sel)
			DrawBox(m_HoverObj, 0x90FFFFFF, 1);
	}

	// Гизмо: три стрелки из центра габаритов — X (восток) красная, Y (вверх) зелёная, Z (север)
	// синяя; длина ~0.15 расстояния до камеры (на экране примерно одна). Где они на экране ->
	// m_GizmoLine «GZ|сеанс|номер|ox|oy|xx|xy|yx|yy|zx|zy|длина» (доли экрана ×1e6, длина в мм).
	protected void DrawGizmo(Object o)
	{
		vector box[2];
		o.ClippingInfo(box);
		vector m[4];
		o.GetTransform(m);
		vector c = (box[0] + box[1]) * 0.5;
		vector org = m[3] + m[0] * c[0] + m[1] * c[1] + m[2] * c[2];
		float len = vector.Distance(GetGame().GetCurrentCameraPosition(), org) * 0.15;
		len = Math.Clamp(len, 0.3, 300.0);
		int sw;
		int sh;
		GetScreenSize(sw, sh);
		float cx;
		float cy;
		m_Canvas.GetScreenPos(cx, cy);
		float fw = sw;
		float fh = sh;
		vector so = GetGame().GetScreenPosRelative(org);
		if (so[2] <= 0)
			return;
		float x0 = so[0] * fw - cx;
		float y0 = so[1] * fh - cy;
		string gz = "GZ|" + m_Sess.ToString() + "|" + m_SelRef.ToString() + "|" + Rel6(so[0]) + "|" + Rel6(so[1]);
		for (int ax = 0; ax < 3; ax++)
		{
			vector dir = "1 0 0";
			int color = 0xFFFF3B30;
			if (ax == 1)
			{
				dir = "0 1 0";
				color = 0xFF34C759;
			}
			if (ax == 2)
			{
				dir = "0 0 1";
				color = 0xFF2F80FF;
			}
			if (m_GizmoAxis == ax + 1)
				color = 0xFFFFD60A;
			vector se = GetGame().GetScreenPosRelative(org + dir * len);
			if (se[2] <= 0)
				return;
			float x1 = se[0] * fw - cx;
			float y1 = se[1] * fh - cy;
			m_Canvas.DrawLine(x0, y0, x1, y1, 3, color);
			float ddx = x1 - x0;
			float ddy = y1 - y0;
			float dl = Math.Sqrt(ddx * ddx + ddy * ddy);
			if (dl > 1)
			{
				float ux = ddx / dl;
				float uy = ddy / dl;
				m_Canvas.DrawLine(x1, y1, x1 - ux * 12 - uy * 6, y1 - uy * 12 + ux * 6, 3, color);
				m_Canvas.DrawLine(x1, y1, x1 - ux * 12 + uy * 6, y1 - uy * 12 - ux * 6, 3, color);
			}
			gz = gz + "|" + Rel6(se[0]) + "|" + Rel6(se[1]);
		}
		m_GizmoLine = gz + "|" + Mm(len);
	}

	protected string Rel6(float value)
	{
		int r = Math.Round(value * 1000000.0);
		return r.ToString();
	}

	protected void DrawBox(Object o, int color, float width)
	{
		vector box[2];
		o.ClippingInfo(box);
		vector m[4];
		o.GetTransform(m);
		int sw;
		int sh;
		GetScreenSize(sw, sh);
		float cx;
		float cy;
		m_Canvas.GetScreenPos(cx, cy);
		float fw = sw;
		float fh = sh;
		vector lo = box[0];
		vector hi = box[1];
		float px[8];
		float py[8];
		bool vis[8];
		for (int i = 0; i < 8; i++)
		{
			float lx = lo[0];
			float ly = lo[1];
			float lz = lo[2];
			if ((i & 1) != 0)
				lx = hi[0];
			if ((i & 2) != 0)
				ly = hi[1];
			if ((i & 4) != 0)
				lz = hi[2];
			vector w = m[3] + m[0] * lx + m[1] * ly + m[2] * lz;
			vector s = GetGame().GetScreenPosRelative(w);
			vis[i] = s[2] > 0;
			px[i] = s[0] * fw - cx;
			py[i] = s[1] * fh - cy;
		}
		for (int a = 0; a < 8; a++)
		{
			for (int bit = 1; bit < 8; bit = bit * 2)
			{
				if ((a & bit) != 0)
					continue;
				int b = a | bit;
				if (vis[a] && vis[b])
					m_Canvas.DrawLine(px[a], py[a], px[b], py[b], width, color);
			}
		}
	}

	protected void EnsureMaps()
	{
		if (!m_Objects)
			m_Objects = new map<int, Object>;
		if (!m_Lines)
			m_Lines = new map<int, string>;
		if (!m_Results)
			m_Results = new map<int, string>;
		if (!m_Picked)
			m_Picked = new map<int, Object>;
		if (!m_PickPos)
			m_PickPos = new map<int, vector>;
		if (!m_PickOri)
			m_PickOri = new map<int, vector>;
		if (!m_TLines)
			m_TLines = new map<int, string>;
		if (!m_Verify)
			m_Verify = new map<int, string>;
		if (!m_VerifyOut)
			m_VerifyOut = new map<int, string>;
		if (!m_KeyWatch)
			m_KeyWatch = new array<string>;
		if (!m_KeyCodes)
		{
			m_KeyCodes = new map<string, int>;
%(keytable)s
		}
		if (m_Sess == 0)
		{
			int hh;
			int mi;
			int ss;
			GetHourMinuteSecond(hh, mi, ss);
			m_Sess = ((hh * 60 + mi) * 60 + ss) * 1000 + Math.RandomInt(1, 999);
		}
	}

	void Start()
	{
		EnsureMaps();
		s_ActiveToken++;
		m_Token = s_ActiveToken;
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).RemoveByName(this, "Tick");
		GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(Tick, 50, true);
		GetGame().GetUpdateQueue(CALL_CATEGORY_SYSTEM).Remove(CamFrame);
		GetGame().GetUpdateQueue(CALL_CATEGORY_SYSTEM).Insert(CamFrame);
	}

	// Каждый кадр: своя камера, пока окно редактора пишет её позу (F8 открыто).
	void CamFrame(float dt)
	{
		if (m_Token != s_ActiveToken)
		{
			GetGame().GetUpdateQueue(CALL_CATEGORY_SYSTEM).Remove(CamFrame);
			ReleaseCamera();
			DropCanvas();
			return;
		}
		DrawFrames();
		m_CamClock += dt;
		ReadCamera();
		if (!m_CamWanted || m_CamClock - m_CamSeenAt > %(cam_timeout)s)
		{
			ReleaseCamera();
			return;
		}
		if (!m_Cam)
		{
			if (m_CamFailed)
				return;
			m_Cam = Camera.Cast(GetGame().CreateObject("staticcamera", m_CamPos, true));
			if (!m_Cam)
			{
				m_CamFailed = true;
				return;
			}
		}
		float ahead = m_CamClock - m_CamSeenAt;
		if (ahead > 0.1)
			ahead = 0.1;
		m_Cam.SetPosition(m_CamPos + m_CamVel * ahead);
		m_Cam.SetOrientation(Vector(m_CamYaw, m_CamPitch, 0));
		if (!m_Cam.IsActive())
			m_Cam.SetActive(true);
		ShowCursorMenu();
		ShowGameCursor();
	}

	// Наше невидимое меню — открыто; меню игры (Esc), открытое поверх, — спрятать.
	protected void ShowCursorMenu()
	{
		UIManager ui = GetGame().GetUIManager();
		if (!ui)
			return;
		UIMenuPanel top = ui.GetMenu();
		int depth = 0;
		while (top && top.GetSubMenu() && depth < 8)
		{
			top = top.GetSubMenu();
			depth++;
		}
		if (!top)
		{
			if (m_CamClock - m_MenuTriedAt < 0.3)
				return;
			m_MenuTriedAt = m_CamClock;
			if (!m_CursorMenu)
				m_CursorMenu = new WE_CursorMenu();
			ui.ShowScriptedMenu(m_CursorMenu, NULL);
			m_MenuOpens++;
			return;
		}
		if (top == m_CursorMenu)
			return;
		UIScriptedMenu other = UIScriptedMenu.Cast(top);
		if (other && other != m_HiddenMenu && other.GetLayoutRoot())
		{
			other.GetLayoutRoot().Show(false);
			m_HiddenMenu = other;
		}
	}

	protected void HideCursorMenu()
	{
		UIManager ui = GetGame().GetUIManager();
		if (m_HiddenMenu)
		{
			m_HiddenMenu.Close();
			m_HiddenMenu = null;
		}
		if (m_CursorMenu && ui && m_CursorMenu.IsVisible())
			ui.HideScriptedMenu(m_CursorMenu);
		m_MenuTriedAt = -10.0;
	}

	protected bool CursorMenuOpen()
	{
		return m_CursorMenu && m_CursorMenu.IsVisible();
	}

	// Курсор Buldozer (красный шарик) живёт в мире у камеры Buldozer — при своей камере его не
	// видно, поэтому показываем курсор DayZ, как это делают меню (UIScriptedMenu.LockControls):
	// мышь — интерфейсу (иначе игра держит курсор на месте), курсор — видимый. Каждый кадр:
	// меню могло его спрятать. ChangeGameFocus — счётчик: «мышь у игры» = счётчик 0
	// (HasGameFocus). Его уменьшает любое закрывшееся меню (UnlockControls), а ResetGameFocus
	// обнуляет — тогда игра снова держит курсор в центре. Поэтому проверяем каждый кадр и
	// забираем снова; сколько раз прибавили — столько и вернём в HideGameCursor.
	protected void ShowGameCursor()
	{
		Input input = GetGame().GetInput();
		if (input && input.HasGameFocus(INPUT_DEVICE_MOUSE))
		{
			input.ChangeGameFocus(1, INPUT_DEVICE_MOUSE);
			if (m_FocusAdds > 0)
				m_FocusRetakes++;
			m_FocusAdds++;
		}
		UIManager ui = GetGame().GetUIManager();
		if (!ui || ui.IsCursorVisible())
			return;
		ui.ShowCursor(true);
		m_CursorShown = true;
		m_CursorShows++;
	}

	protected void HideGameCursor()
	{
		if (m_FocusAdds > 0)
		{
			Input input = GetGame().GetInput();
			if (input)
			{
				for (int i = 0; i < m_FocusAdds; i++)
				{
					if (input.HasGameFocus(INPUT_DEVICE_MOUSE))
						break;      // уже у игры — ниже нуля не уводить (иначе мышь не вернётся)
					input.ChangeGameFocus(-1, INPUT_DEVICE_MOUSE);
				}
			}
			m_FocusAdds = 0;
		}
		if (!m_CursorShown)
			return;
		m_CursorShown = false;
		UIManager ui = GetGame().GetUIManager();
		if (ui && !ui.GetMenu())
			ui.ShowCursor(false);
	}

	// Где курсор (доли экрана): курсор DayZ, иначе — что прислало окно (M|u|v).
	protected bool CursorPoint(out float x, out float y)
	{
		UIManager ui = GetGame().GetUIManager();
		if (ui && ui.IsCursorVisible())
		{
			int mx;
			int my;
			int sw;
			int sh;
			GetMousePos(mx, my);
			GetScreenSize(sw, sh);
			if (sw > 0 && sh > 0)
			{
				float fx = mx;
				float fy = my;
				float fw = sw;
				float fh = sh;
				x = (fx + 0.5) / fw;
				y = (fy + 0.5) / fh;
				return true;
			}
		}
		if (!m_PtrSet)
			return false;
		x = m_PtrX;
		y = m_PtrY;
		return true;
	}

	protected void ReadCamera()
	{
		FileHandle file = OpenFile("$profile:%(cam)s", FileMode.READ);
		if (file == 0)
		{
			// окно как раз подменяет файл (60 раз в секунду) — это не «камера не нужна»: раньше
			// здесь камера на кадр отдавалась Buldozer, меню закрывалось, и игра ставила курсор
			// в центр. Камеру отдаёт только «off» или тишина дольше таймаута.
			m_CamReadFails++;
			return;
		}
		string line;
		if (FGets(file, line) <= 0 || line.IndexOf("WE_CAM") != 0)
		{
			CloseFile(file);
			return;
		}
		if (FGets(file, line) <= 0 || line.IndexOf("seq ") != 0)
		{
			CloseFile(file);
			return;
		}
		int seq = line.Substring(4, line.Length() - 4).ToInt();
		if (seq == m_CamSeq)
		{
			CloseFile(file);
			return;
		}
		string body = "";
		string ptr = "";
		bool complete = false;
		while (FGets(file, line) > 0)
		{
			line.TrimInPlace();
			if (line == "end")
			{
				complete = true;
				break;
			}
			if (line.IndexOf("M|") == 0)
				ptr = line;
			else if (line != "")
				body = line;
		}
		CloseFile(file);
		if (!complete)
			return;
		m_CamSeq = seq;
		m_CamSeenAt = m_CamClock;
		if (ptr != "")
		{
			ref array<string> m = new array<string>;
			ptr.Split("|", m);
			if (m.Count() >= 3)
			{
				m_PtrX = m.Get(1).ToFloat();
				m_PtrY = m.Get(2).ToFloat();
				m_PtrSet = true;
			}
		}
		ref array<string> c = new array<string>;
		body.Split("|", c);
		if (c.Count() >= 9 && c.Get(0) == "P")
		{
			m_CamPos = Vector(c.Get(1).ToFloat(), c.Get(2).ToFloat(), c.Get(3).ToFloat());
			m_CamYaw = c.Get(4).ToFloat();
			m_CamPitch = c.Get(5).ToFloat();
			m_CamVel = Vector(c.Get(6).ToFloat(), c.Get(7).ToFloat(), c.Get(8).ToFloat());
			m_CamWanted = true;
		}
		else
		{
			m_CamWanted = false;
			m_CamFailed = false;
			m_PtrSet = false;
		}
	}

	// Луч из камеры через курсор (доли окна m_PtrX/m_PtrY). У своей камеры движок положение
	// курсора не обновляет, поэтому GetPointerDirection() не годится. Проекцию берём у самого
	// движка (GetScreenPosRelative): на плоскости поперёк взгляда экранные координаты линейны.
	protected vector PointerRay(vector eye, vector fwd, float ptrX, float ptrY, vector fallback)
	{
		vector right = Vector(fwd[2], 0, -fwd[0]);
		float len = right.Length();
		if (len < 0.001)
			return fallback;
		right = right * (1.0 / len);
		vector up = Vector(fwd[1] * right[2] - fwd[2] * right[1], fwd[2] * right[0] - fwd[0] * right[2], fwd[0] * right[1] - fwd[1] * right[0]);
		vector center = eye + fwd * 10.0;
		vector s0 = GetGame().GetScreenPosRelative(center);
		vector sx = GetGame().GetScreenPosRelative(center + right);
		vector sy = GetGame().GetScreenPosRelative(center + up);
		if (s0[2] <= 0)
			return fallback;
		float ax = sx[0] - s0[0];
		float ay = sx[1] - s0[1];
		float bx = sy[0] - s0[0];
		float by = sy[1] - s0[1];
		float det = ax * by - bx * ay;
		if (Math.AbsFloat(det) < 0.0000001)
			return fallback;
		float px = ptrX - s0[0];
		float py = ptrY - s0[1];
		float a = (px * by - bx * py) / det;
		float b = (ax * py - ay * px) / det;
		vector ray = fwd * 10.0 + right * a + up * b;
		return ray.Normalized();
	}

	protected void ReleaseCamera()
	{
		HideCursorMenu();
		HideGameCursor();
		if (!m_Cam)
			return;
		m_Cam.SetActive(false);
		GetGame().ObjectDelete(m_Cam);
		m_Cam = null;
	}

	void Tick()
	{
		if (m_Token != s_ActiveToken)
		{
			GetGame().GetCallQueue(CALL_CATEGORY_SYSTEM).RemoveByName(this, "Tick");
			GetGame().GetUpdateQueue(CALL_CATEGORY_SYSTEM).Remove(CamFrame);
			ReleaseCamera();
			return;
		}
		EnsureMaps();
		ReadCommands();
		vector camPos = GetGame().GetCurrentCameraPosition();
		vector camDir = GetGame().GetCurrentCameraDirection();
		vector rayDir = GetGame().GetPointerDirection();
		float ptrX;
		float ptrY;
		if (m_Cam && m_Cam.IsActive() && CursorPoint(ptrX, ptrY))
			rayDir = PointerRay(camPos, camDir, ptrX, ptrY, rayDir);
		vector rayEnd = camPos + rayDir * 30000.0;
		vector hit;
		vector hitNormal;
		int hitComponent;
		bool ok = DayZPhysics.RaycastRV(camPos, rayEnd, hit, hitNormal, hitComponent, NULL, NULL, NULL, false, true);
		if (m_GhostPinned)
			MoveGhost(m_GhostPin);
		else if (ok)
			MoveGhost(hit);
		Object under = PickObject(camPos, rayEnd);
		m_HoverObj = under;
		m_Hover = "";
		if (under)
			m_Hover = ModelOf(under);
		if (m_PickWanted != m_PickSeq)
		{
			m_PickSeq = m_PickWanted;
			m_PickResult = RegisterPick(under);
		}
		if (ok)
			DragTo(hit);
		UpdateSelShape();
		m_VerifyTick++;
		if (m_VerifyTick >= 6)
		{
			m_VerifyTick = 0;
			RunVerify();
		}
		WriteState(ok, hit, camPos, camDir);
	}

	// Имя файла модели без пути, строчными: так сравниваем модель объекта TB и нашего.
	protected string ShortModel(string model)
	{
		string name = model;
		name.Replace("/", "\\");
		int cut = name.LastIndexOf("\\");
		if (cut >= 0)
			name = name.Substring(cut + 1, name.Length() - cut - 1);
		name.ToLower();
		return name;
	}

	// V|id|x|z|модель: сколько объектов этой модели (не наших и не «призрака») стоит ближе
	// 0.3 м от нашего объекта id (или от x/z, если нашего уже нет) -> VF|id|n.
	protected void RunVerify()
	{
		m_VerifyOut.Clear();
		foreach (int vid, string vline : m_Verify)
		{
			ref array<string> vp = new array<string>;
			vline.Split("|", vp);
			if (vp.Count() < 5)
				continue;
			vector at = Vector(vp.Get(2).ToFloat(), 0, vp.Get(3).ToFloat());
			Object mineObj = m_Objects.Get(vid);
			if (mineObj)
				at = mineObj.GetPosition();
			at[1] = GetGame().SurfaceY(at[0], at[2]);
			string want = ShortModel(vp.Get(4));
			ref array<Object> near = new array<Object>;
			GetGame().GetObjectsAtPosition(at, 2.0, near, null);
			int count = 0;
			for (int ni = 0; ni < near.Count(); ni++)
			{
				Object cand = near.Get(ni);
				if (!cand || cand == m_Ghost || cand == m_Cam || cand == mineObj)
					continue;
				if (ShortModel(ModelOf(cand)) != want)
					continue;
				vector cp = cand.GetPosition();
				float ddx = cp[0] - at[0];
				float ddz = cp[2] - at[2];
				if (ddx * ddx + ddz * ddz < 0.09)
					count++;
			}
			m_VerifyOut.Set(vid, "VF|" + vid.ToString() + "|" + count.ToString());
		}
	}

	// Объект под курсором (не земля): ближайшее пересечение луча. Сначала View Geometry, затем Geometry.
	protected Object PickObject(vector from, vector to)
	{
		vector contact;
		vector normal;
		int component;
		ref set<Object> found = new set<Object>;
		int pass = 0;
		while (pass < 2)
		{
			int how = ObjIntersectView;
			if (pass == 1)
				how = ObjIntersectGeom;
			found.Clear();
			DayZPhysics.RaycastRV(from, to, contact, normal, component, found, NULL, m_Ghost, true, false, how);
			for (int i = 0; i < found.Count(); i++)
			{
				Object o = found.Get(i);
				if (o && o != m_Ghost && o != m_Cam)
					return o;
			}
			pass++;
		}
		return null;
	}

	protected string ModelOf(Object o)
	{
		string name = o.GetShapeName();
		if (name == "")
			name = o.GetType();
		name.Replace("|", "");
		return name;
	}

	// Высота начала объекта над землёй под ним (сохраняется при переносе).
	protected float RelHeight(vector pos)
	{
		return pos[1] - GetGame().SurfaceY(pos[0], pos[2]);
	}

	// Выбор (K|n): номер в реестре, наш ли это объект (id расстановки) и как он стоит.
	protected string RegisterPick(Object o)
	{
		string head = "P|" + m_PickSeq.ToString() + "|" + m_Sess.ToString() + "|";
		if (!o)
			return head + "-1";
		int pr = -1;
		foreach (int knownRef, Object known : m_Picked)
		{
			if (known == o)
			{
				pr = knownRef;
				break;
			}
		}
		if (pr < 0)
		{
			m_RefCounter++;
			pr = m_RefCounter;
			m_Picked.Set(pr, o);
			m_PickPos.Set(pr, o.GetPosition());
			m_PickOri.Set(pr, o.GetOrientation());
		}
		int own = -1;
		foreach (int placedId, Object mine : m_Objects)
		{
			if (mine == o)
			{
				own = placedId;
				break;
			}
		}
		vector origPos = m_PickPos.Get(pr);
		vector origOri = m_PickOri.Get(pr);
		vector pos = o.GetPosition();
		vector ori = o.GetOrientation();
		string line = head + pr.ToString() + "|" + own.ToString() + "|" + ModelOf(o);
		line = line + "|" + Mm(pos[0]) + "|" + Mm(pos[1]) + "|" + Mm(pos[2]);
		line = line + "|" + Mm(ori[0]) + "|" + Mm(ori[1]) + "|" + Mm(ori[2]) + "|" + Mm(o.GetScale());
		line = line + "|" + Mm(origPos[0]) + "|" + Mm(origPos[2]) + "|" + Mm(origOri[0]) + "|" + Mm(RelHeight(origPos));
		return line;
	}

	// T|сеанс|номер|x|z|курс|подъём: объект карты — туда (высота над землёй та же + подъём).
	protected void ApplyTransforms(map<int, string> wanted)
	{
		ref array<int> restore = new array<int>;
		foreach (int oldRef, string oldLine : m_TLines)
		{
			if (!wanted.Contains(oldRef))
				restore.Insert(oldRef);
		}
		foreach (int backRef : restore)
		{
			m_TLines.Remove(backRef);
			if (backRef != m_DragRef)
				RestorePicked(backRef);
		}
		foreach (int pr, string line : wanted)
		{
			if (m_TLines.Contains(pr) && m_TLines.Get(pr) == line)
				continue;
			m_TLines.Set(pr, line);
			if (pr != m_DragRef)
				PlacePicked(pr, line);
		}
	}

	protected void PlacePicked(int pr, string line)
	{
		Object o = m_Picked.Get(pr);
		if (!o)
			return;
		ref array<string> p = new array<string>;
		line.Split("|", p);
		if (p.Count() < 7)
			return;
		float x = p.Get(3).ToFloat();
		float z = p.Get(4).ToFloat();
		vector ori = m_PickOri.Get(pr);
		float y = GetGame().SurfaceY(x, z) + RelHeight(m_PickPos.Get(pr)) + p.Get(6).ToFloat();
		if (p.Count() >= 8)
		{
			if (p.Get(7) == "1")
				y = y - 1000.0;        // удалён (Delete): под землёй его не видно и не выбрать
		}
		o.SetPosition(Vector(x, y, z));
		o.SetOrientation(Vector(p.Get(5).ToFloat(), ori[1], ori[2]));
	}

	protected void RestorePicked(int pr)
	{
		Object o = m_Picked.Get(pr);
		if (!o)
			return;
		o.SetPosition(m_PickPos.Get(pr));
		o.SetOrientation(m_PickOri.Get(pr));
	}

	protected void RestoreAllPicked()
	{
		if (!m_TLines)
			return;
		foreach (int pr, string line : m_TLines)
			RestorePicked(pr);
		m_TLines.Clear();
		if (m_SelShape)
		{
			m_SelShape.Destroy();
			m_SelShape = null;
		}
	}

	// D|сеанс|номер: объект едет за курсором (с той же высотой над землёй), пока строка есть.
	protected void SetDrag(int pr)
	{
		if (pr == m_DragRef)
			return;
		int was = m_DragRef;
		m_DragRef = pr;
		m_DragStarted = false;
		m_DragResult = "";
		if (was >= 0 && m_TLines.Contains(was))
			PlacePicked(was, m_TLines.Get(was));
	}

	protected void DragTo(vector hit)
	{
		if (m_DragRef < 0)
			return;
		Object o = m_Picked.Get(m_DragRef);
		if (!o)
			return;
		vector pos = o.GetPosition();
		if (!m_DragStarted)
		{
			m_DragStarted = true;
			m_DragOffset = Vector(pos[0] - hit[0], 0, pos[2] - hit[2]);
			m_DragRel = RelHeight(pos);
		}
		float x = hit[0] + m_DragOffset[0];
		float z = hit[2] + m_DragOffset[2];
		o.SetPosition(Vector(x, GetGame().SurfaceY(x, z) + m_DragRel, z));
		m_DragResult = "DP|" + m_Sess.ToString() + "|" + m_DragRef.ToString() + "|" + Mm(x) + "|" + Mm(z);
	}

	// S|сеанс|номер: рамка вокруг выделенного (отладочная фигура; где её нет — просто без рамки).
	protected void SetSelection(int pr)
	{
		if (pr == m_SelRef)
			return;
		m_SelRef = pr;
		if (m_SelShape)
		{
			m_SelShape.Destroy();
			m_SelShape = null;
		}
		m_SelShapeObj = null;
	}

	protected void UpdateSelShape()
	{
		if (m_SelRef < 0)
			return;
		Object o = m_Picked.Get(m_SelRef);
		if (!o)
			return;
		if (!m_SelShape || m_SelShapeObj != o)
		{
			if (m_SelShape)
				m_SelShape.Destroy();
			vector box[2];
			o.ClippingInfo(box);
			m_SelShape = Shape.Create(ShapeType.BBOX, 0xFF33CCFF, ShapeFlags.WIREFRAME | ShapeFlags.NOZBUFFER, box[0], box[1]);
			m_SelShapeObj = o;
		}
		if (!m_SelShape)
			return;
		vector m[4];
		o.GetTransform(m);
		m_SelShape.SetMatrix(m);
	}

	protected string Mm(float value)
	{
		int mm = Math.Round(value * 1000.0);
		return mm.ToString();
	}

	protected string Vec(vector v)
	{
		return Mm(v[0]) + " " + Mm(v[1]) + " " + Mm(v[2]);
	}

	protected void WriteState(bool ok, vector hit, vector camPos, vector camDir)
	{
		FileHandle file = OpenFile("$profile:%(state)s", FileMode.WRITE);
		if (file == 0)
			return;
		m_Seq++;
		FPrintln(file, "WE_BRIDGE %(version)d");
		FPrintln(file, "seq " + m_Seq.ToString());
		FPrintln(file, "gen " + m_Generation.ToString());
		if (ok)
			FPrintln(file, "hit " + Vec(hit));
		FPrintln(file, "cam " + Vec(camPos));
		FPrintln(file, "dir " + Vec(camDir));
		foreach (int id, string result : m_Results)
			FPrintln(file, result);
		if (m_GhostFailed)
			FPrintln(file, "GE");
		if (m_Cam && m_Cam.IsActive())
			FPrintln(file, "FC 1");
		if (m_CamFailed)
			FPrintln(file, "FCE");
		if (m_CursorShown)
			FPrintln(file, "CU 1");
		if (m_FocusAdds > 0)
			FPrintln(file, "MF 1");
		if (m_FocusRetakes > 0)
			FPrintln(file, "FR " + m_FocusRetakes.ToString());
		if (CursorMenuOpen())
			FPrintln(file, "UM 1");
		if (m_HiddenMenu)
			FPrintln(file, "UH 1");
		if (m_MenuOpens > 0)
			FPrintln(file, "MO " + m_MenuOpens.ToString());
		if (m_CursorShows > 0)
			FPrintln(file, "CS " + m_CursorShows.ToString());
		if (m_CamReadFails > 0)
			FPrintln(file, "CF " + m_CamReadFails.ToString());
		if (m_Canvas)
			FPrintln(file, "CV 1");
		else if (m_CanvasTried)
			FPrintln(file, "CV 0");
		FPrintln(file, "bs " + m_Sess.ToString());
		if (m_Hover != "")
			FPrintln(file, "H|" + m_Hover);
		if (m_PickResult != "")
			FPrintln(file, m_PickResult);
		if (m_DragResult != "")
			FPrintln(file, m_DragResult);
		foreach (int vfId, string vfLine : m_VerifyOut)
			FPrintln(file, vfLine);
		if (m_GizmoLine != "")
			FPrintln(file, m_GizmoLine);
		if (m_KeyWatch)
		{
			if (m_KeyWatch.Count() > 0)
			{
				string kdLine = "KD|";
				bool kdFirst = true;
				int kdCode;
				foreach (string kdName : m_KeyWatch)
				{
					if (m_KeyCodes.Find(kdName, kdCode))
					{
						if (KeyState(kdCode) != 0)
						{
							if (!kdFirst)
								kdLine = kdLine + ",";
							kdLine = kdLine + kdName;
							kdFirst = false;
						}
					}
				}
				FPrintln(file, kdLine);
			}
		}
		FPrintln(file, "end");
		CloseFile(file);
	}

	protected void ReadCommands()
	{
		FileHandle file = OpenFile("$profile:%(cmd)s", FileMode.READ);
		if (file == 0)
			return;
		string line;
		if (FGets(file, line) <= 0 || line.IndexOf("WE_CMD") != 0)
		{
			CloseFile(file);
			return;
		}
		if (FGets(file, line) <= 0 || line.IndexOf("gen ") != 0)
		{
			CloseFile(file);
			return;
		}
		int generation = line.Substring(4, line.Length() - 4).ToInt();
		if (generation == m_Generation)
		{
			CloseFile(file);
			return;
		}
		ref map<int, string> wanted = new map<int, string>;
		ref map<int, string> moves = new map<int, string>;
		string ghostLine = "";
		int pickWanted = m_PickWanted;
		int dragRef = -1;
		int selRef = -1;
		ref map<int, string> verify = new map<int, string>;
		ref array<string> kwList = new array<string>;
		string kwText;
		int gaAxis = 0;
		string sess = m_Sess.ToString();
		bool complete = false;
		while (FGets(file, line) > 0)
		{
			line.TrimInPlace();
			if (line == "end")
			{
				complete = true;
				break;
			}
			ref array<string> parts = new array<string>;
			line.Split("|", parts);
			if (parts.Count() >= 8 && parts.Get(0) == "O")
				wanted.Set(parts.Get(1).ToInt(), line);
			else if (parts.Count() >= 1 && parts.Get(0) == "G")
				ghostLine = line;
			else if (parts.Count() >= 2 && parts.Get(0) == "K")
				pickWanted = parts.Get(1).ToInt();
			else if (parts.Count() >= 7 && parts.Get(0) == "T" && parts.Get(1) == sess)
				moves.Set(parts.Get(2).ToInt(), line);
			else if (parts.Count() >= 3 && parts.Get(0) == "D" && parts.Get(1) == sess)
				dragRef = parts.Get(2).ToInt();
			else if (parts.Count() >= 3 && parts.Get(0) == "S" && parts.Get(1) == sess)
				selRef = parts.Get(2).ToInt();
			else if (parts.Count() >= 5 && parts.Get(0) == "V")
				verify.Set(parts.Get(1).ToInt(), line);
			else if (parts.Count() >= 2 && parts.Get(0) == "GA")
				gaAxis = parts.Get(1).ToInt();
			else if (parts.Count() >= 2 && parts.Get(0) == "KW")
			{
				kwText = parts.Get(1);
				kwList.Clear();
				kwText.Split(",", kwList);
			}
		}
		CloseFile(file);
		if (!complete)
			return;
		m_Generation = generation;
		m_Verify = verify;
		m_KeyWatch = kwList;
		m_GizmoAxis = gaAxis;

		m_PickWanted = pickWanted;
		ref array<int> drop = new array<int>;
		ref array<int> shift = new array<int>;
		foreach (int oldId, string oldLine : m_Lines)
		{
			string newLine;
			if (!wanted.Find(oldId, newLine))
				drop.Insert(oldId);
			else if (newLine != oldLine)
			{
				if (m_Objects.Get(oldId) && SameShape(oldLine, newLine))
					shift.Insert(oldId);
				else
					drop.Insert(oldId);
			}
		}
		foreach (int shiftId : shift)
		{
			m_Lines.Set(shiftId, wanted.Get(shiftId));
			PositionPlaced(shiftId, m_Objects.Get(shiftId), wanted.Get(shiftId));
		}
		foreach (int dropId : drop)
		{
			Object oldObject = m_Objects.Get(dropId);
			if (oldObject)
				GetGame().ObjectDelete(oldObject);
			m_Objects.Remove(dropId);
			m_Lines.Remove(dropId);
			m_Results.Remove(dropId);
		}
		foreach (int newId, string objectLine : wanted)
		{
			if (!m_Lines.Contains(newId))
				CreatePlaced(newId, objectLine);
		}
		ApplyGhost(ghostLine);
		SetDrag(dragRef);
		ApplyTransforms(moves);
		SetSelection(selRef);
	}

	// Та же модель и масштаб — объект можно передвинуть, а не создавать заново.
	protected bool SameShape(string a, string b)
	{
		ref array<string> pa = new array<string>;
		ref array<string> pb = new array<string>;
		a.Split("|", pa);
		b.Split("|", pb);
		if (pa.Count() < 8 || pb.Count() < 8)
			return false;
		return pa.Get(2) == pb.Get(2) && pa.Get(6) == pb.Get(6);
	}

	protected void CreatePlaced(int id, string line)
	{
		ref array<string> p = new array<string>;
		line.Split("|", p);
		m_Lines.Set(id, line);
		if (p.Count() < 8)
		{
			m_Results.Set(id, "E|" + id.ToString());
			return;
		}
		string model = p.Get(2);
		float x = p.Get(3).ToFloat();
		float z = p.Get(4).ToFloat();
		float yaw = p.Get(5).ToFloat();
		float scale = p.Get(6).ToFloat();
		if (scale <= 0)
			scale = 1.0;
		float ground = GetGame().SurfaceY(x, z);
		Object obj = GetGame().CreateStaticObjectUsingP3D(model, Vector(x, ground, z), Vector(yaw, 0, 0), scale, false);
		if (!obj)
		{
			m_Results.Set(id, "E|" + id.ToString());
			return;
		}
		m_Objects.Set(id, obj);
		PositionPlaced(id, obj, line);
	}

	// Поставить наш объект по строке O (при создании и при переносе/повороте из окна).
	protected void PositionPlaced(int id, Object obj, string line)
	{
		ref array<string> p = new array<string>;
		line.Split("|", p);
		if (!obj || p.Count() < 8)
			return;
		float x = p.Get(3).ToFloat();
		float z = p.Get(4).ToFloat();
		float yaw = p.Get(5).ToFloat();
		float scale = p.Get(6).ToFloat();
		float lift = p.Get(7).ToFloat();
		if (scale <= 0)
			scale = 1.0;
		float ground = GetGame().SurfaceY(x, z);
		vector box[2];
		obj.ClippingInfo(box);
		float originY = ground - box[0][1] * scale + lift;
		obj.SetPosition(Vector(x, originY, z));
		obj.SetOrientation(Vector(yaw, 0, 0));
		// Высота для Terrain Builder. TB (как DayZ Editor и ArmaRealMap) считает высоту от
		// исходного начала модели (MLOD), а Binarize переносит начало в центр габаритов, если в
		// Geometry нет autocenter=0: перенос = GetBoundingCenter() (с поворотом и масштабом).
		// Relative = высота исходного начала над землёй под ним.
		vector m[4];
		obj.GetTransform(m);
		float len = m[0].Length();
		if (len < 0.0001)
			len = 1.0;
		vector c = obj.GetBoundingCenter().Multiply3(m) * (scale / len);
		float tbGround = GetGame().SurfaceY(x - c[0], z - c[2]);
		float tbRelative = originY - c[1] - tbGround;
		m_Results.Set(id, "Y|" + id.ToString() + "|" + Mm(originY) + "|" + Mm(c[1]) + "|" + Mm(tbRelative));
	}

	protected void ApplyGhost(string line)
	{
		if (line == m_GhostLine)
			return;
		m_GhostLine = line;
		m_GhostFailed = false;
		string model = "";
		float yaw = 0;
		float scale = 1.0;
		float lift = 0;
		bool pinned = false;
		vector pin;
		ref array<string> p = new array<string>;
		line.Split("|", p);
		if (p.Count() >= 5)
		{
			model = p.Get(1);
			yaw = p.Get(2).ToFloat();
			scale = p.Get(3).ToFloat();
			lift = p.Get(4).ToFloat();
		}
		if (p.Count() >= 8)
		{
			// пока в окне крутят/поднимают модель мышью — она стоит на месте, а не бегает за курсором
			pinned = true;
			pin = Vector(p.Get(5).ToFloat(), p.Get(6).ToFloat(), p.Get(7).ToFloat());
		}
		if (scale <= 0)
			scale = 1.0;
		if (model != m_GhostModel || scale != m_GhostScale)
			DeleteGhost();
		m_GhostModel = model;
		m_GhostYaw = yaw;
		m_GhostScale = scale;
		m_GhostLift = lift;
		m_GhostPinned = pinned;
		m_GhostPin = pin;
	}

	protected void MoveGhost(vector hit)
	{
		if (m_GhostModel == "")
			return;
		if (!m_Ghost)
		{
			if (m_GhostFailed)
				return;
			m_Ghost = GetGame().CreateStaticObjectUsingP3D(m_GhostModel, hit, Vector(m_GhostYaw, 0, 0), m_GhostScale, true);
			if (!m_Ghost)
			{
				m_GhostFailed = true;
				return;
			}
			vector box[2];
			m_Ghost.ClippingInfo(box);
			m_GhostBottom = box[0][1] * m_GhostScale;
		}
		m_Ghost.SetPosition(Vector(hit[0], hit[1] - m_GhostBottom + m_GhostLift, hit[2]));
		m_Ghost.SetOrientation(Vector(m_GhostYaw, 0, 0));
	}

	protected void DeleteGhost()
	{
		if (m_Ghost)
		{
			GetGame().ObjectDelete(m_Ghost);
			m_Ghost = null;
		}
	}

	protected void DeleteAll()
	{
		if (m_Objects)
		{
			foreach (int id, Object placed : m_Objects)
			{
				if (placed)
					GetGame().ObjectDelete(placed);
			}
			m_Objects.Clear();
		}
		if (m_Lines)
			m_Lines.Clear();
		if (m_Results)
			m_Results.Clear();
		DeleteGhost();
	}
}

static ref WE_BuldozerBridge g_WE_BuldozerBridge;

static void WE_BridgeStart()
{
	if (!g_WE_BuldozerBridge)
		g_WE_BuldozerBridge = new WE_BuldozerBridge();
	g_WE_BuldozerBridge.Start();
}
// <<< DayZ World Editor bridge <<<
"""


def script_block() -> str:
    """Наш блок для buldozer.c (между маркерами MARK_BEGIN … MARK_END)."""
    return SCRIPT_BLOCK % {"version": BRIDGE_VERSION, "revision": SCRIPT_REVISION,
                           "state": STATE_FILE, "cmd": CMD_FILE, "cam": CAM_FILE,
                           "cam_timeout": f"{CAM_TIMEOUT:.1f}", "keytable": _key_table()}


def _key_table() -> str:
    """Строки скрипта: имя KeyCode -> код (все клавиши, которые окно может попросить)."""
    from .winbuldozer import VK_TO_KEYCODE
    names = sorted({n for names in VK_TO_KEYCODE.values() for n in names})
    return "\n".join(f'\t\t\tm_KeyCodes.Set("{n}", KeyCode.{n});' for n in names)


def main_function_name(script_name: str = DEFAULT_SCRIPT_NAME) -> str:
    """-name=Buldozer -> BuldozerMain (имя файла и функции зависят от имени профиля)."""
    name = script_name.strip() or DEFAULT_SCRIPT_NAME
    return name[:1].upper() + name[1:] + "Main"


def default_script_path(work_drive: str, script_name: str = DEFAULT_SCRIPT_NAME) -> str:
    """P:\\scripts\\buldozer.c — сюда Buldozer for Terrain Builder смотрит за скриптом F10."""
    name = (script_name.strip() or DEFAULT_SCRIPT_NAME).lower()
    drive = (work_drive or "P:\\").rstrip("\\/")
    return str(PureWindowsPath(drive + "\\", "scripts", f"{name}.c"))


@dataclass
class InstallResult:
    path: Path
    action: str                 # "created" | "updated" | "merged" | "unchanged"
    backup: Path | None = None

    def describe(self) -> str:
        text = {
            "created": "Создан файл скрипта",
            "updated": "Блок моста обновлён",
            "merged": "Блок моста добавлен в ваш скрипт",
            "unchanged": "Скрипт уже свежий",
        }[self.action]
        out = f"{text}: {self.path}"
        if self.backup is not None:
            out += f"\nРезервная копия прежнего файла: {self.backup}"
        return out


_MAIN_RE_TEMPLATE = r"(static\s+)?void\s+{name}\s*\(\s*\)\s*\{{"


def merge_script(text: str | None, script_name: str = DEFAULT_SCRIPT_NAME) -> tuple[str, str]:
    """Вернуть (новый текст, действие) — без записи на диск (для тестов и install_script).

    * файла нет -> наш блок + ``BuldozerMain() { WE_BridgeStart(); }``;
    * блок уже есть -> заменяем его на свежий (содержимое пользователя вне блока не трогаем);
    * блока нет -> вставляем его в начало файла и вызов ``WE_BridgeStart();`` первой строкой
      в функцию ``BuldozerMain()``; если функции нет — дописываем её.
    """
    block = script_block()
    func = main_function_name(script_name)
    if text is None or not text.strip():
        body = (f"{block}\nstatic void {func}()\n{{\n"
                f"\t// press F10 in Buldozer (connected to Terrain Builder) to run this function\n"
                f"\t{START_CALL}\n}}\n")
        return body, "created"

    begin = text.find(MARK_BEGIN)
    end = text.find(MARK_END, begin + 1) if begin >= 0 else -1
    if begin >= 0 and end >= 0:
        end_line = text.find("\n", end)
        end_line = len(text) if end_line < 0 else end_line + 1
        new = text[:begin] + block + text[end_line:]
        action = "unchanged" if new == text else "updated"
    else:
        new = block + "\n" + text
        action = "merged"

    if START_CALL not in _strip_block(new):
        match = re.search(_MAIN_RE_TEMPLATE.format(name=re.escape(func)), new)
        if match:
            pos = match.end()
            new = new[:pos] + f"\n\t{START_CALL}   // DayZ World Editor: окно поверх Buldozer" + new[pos:]
        else:
            new = new.rstrip("\n") + f"\n\nstatic void {func}()\n{{\n\t{START_CALL}\n}}\n"
        if action == "unchanged":
            action = "updated"
    return new, action


_REV_RE = re.compile(re.escape(MARK_BEGIN) + r"\s+v(\d+)")


def script_status(path: str | Path) -> tuple[str, int]:
    """Что сейчас в buldozer.c: ('missing'|'no_block'|'old'|'ok'|'newer', версия блока или 0)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "missing", 0
    m = _REV_RE.search(text)
    if not m or MARK_END not in text:
        return "no_block", 0
    rev = int(m.group(1))
    if rev < SCRIPT_REVISION:
        return "old", rev
    if rev > SCRIPT_REVISION:
        return "newer", rev
    return ("ok" if START_CALL in _strip_block(text) else "old"), rev


def script_path_in(scripts_dir: str | Path, script_name: str = DEFAULT_SCRIPT_NAME) -> Path:
    """<папка скриптов>\\buldozer.c (имя файла = -name= у Buldozer в нижнем регистре)."""
    name = (script_name.strip() or DEFAULT_SCRIPT_NAME).lower()
    return Path(scripts_dir) / f"{name}.c"


def _strip_block(text: str) -> str:
    """Текст без нашего блока (чтобы искать вызов WE_BridgeStart() только у пользователя)."""
    begin = text.find(MARK_BEGIN)
    end = text.find(MARK_END, begin + 1) if begin >= 0 else -1
    if begin < 0 or end < 0:
        return text
    return text[:begin] + text[end + len(MARK_END):]


def install_script(path: str | Path, script_name: str = DEFAULT_SCRIPT_NAME,
                   now: float | None = None) -> InstallResult:
    """Поставить/обновить блок моста в buldozer.c. Чужой файл перед правкой копируется рядом."""
    path = Path(path)
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    new, action = merge_script(old, script_name)
    if action == "unchanged":
        return InstallResult(path, action)
    backup = None
    if old is not None and old.strip():
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        backup = path.with_name(f"{path.name}.we-backup-{stamp}")
        backup.write_text(old, encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return InstallResult(path, action, backup)


# ----------------------------------------------------------------- состояние / команды

@dataclass
class BridgeState:
    version: int = 0
    seq: int = -1
    generation: int = -1
    hit: tuple[float, float, float] | None = None
    camera: tuple[float, float, float] | None = None
    direction: tuple[float, float, float] | None = None
    # id -> (y начала, перенос начала по высоте, высота для TB или None — скрипт до v4)
    placed: dict[int, tuple[float, float, float | None]] = field(default_factory=dict)
    failed: set[int] = field(default_factory=set)
    ghost_failed: bool = False
    free_camera: bool = False      # FC 1 — включена наша камера (скрипт v5)
    free_camera_failed: bool = False   # FCE — камеру создать не удалось
    game_cursor: bool = False      # CU 1 — скрипт показал курсор DayZ (v7)
    mouse_focus: bool = False      # MF 1 — скрипт забрал мышь у игры (v8)
    menu_opens: int = 0            # MO N — сколько раз скрипт открывал невидимое меню (v12)
    cursor_shows: int = 0          # CS N — сколько раз скрипт снова показывал курсор DayZ (v12)
    cam_read_fails: int = 0        # CF N — файл камеры не открылся (v13; раньше это роняло камеру)
    canvas: bool | None = None     # CV — рамка выделения рисуется (v12); None — скрипт старый
    focus_retakes: int = 0         # FR N — мышь возвращали игре, скрипт забирал снова (v11)
    cursor_menu: bool = False      # UM 1 — открыто невидимое меню (v9)
    menu_hidden: bool = False      # UH 1 — меню игры (Esc) спрятано (v9)
    session: int = 0               # bs N — сеанс скрипта (v10; номера объектов карты — в его рамках)
    hover: str = ""                # H|модель — объект под курсором (v10)
    pick: "PickResult | None" = None   # P|… — ответ на выбор K|n (v10)
    drag: tuple[int, int, float, float] | None = None   # DP|сеанс|номер|x|z — объект едет за курсором
    gizmo: "Gizmo | None" = None                        # GZ|… — стрелки осей у выделенного (v16)
    verified: dict[int, int] = field(default_factory=dict)  # VF|id|n — объектов TB в точке нашего (v14)
    keys_down: set[str] | None = None   # KD|KC_F8,… — нажатые клавиши из запрошенных (v15); None — нет строки
    complete: bool = False


@dataclass
class PickResult:
    """Ответ скрипта на выбор объекта под курсором (строка P)."""
    seq: int
    session: int
    ref: int = -1                  # номер в реестре скрипта; −1 — под курсором ничего нет
    own: int = -1                  # id нашего объекта (расстановка из окна), −1 — объект карты
    model: str = ""
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    ori: tuple[float, float, float] = (0.0, 0.0, 0.0)   # курс, тангаж, крен (°)
    scale: float = 1.0
    x0: float = 0.0                # где объект стоял до наших правок (первый выбор в этом сеансе)
    z0: float = 0.0
    yaw0: float = 0.0
    rel0: float = 0.0              # высота начала над землёй до правок

    @property
    def found(self) -> bool:
        return self.ref >= 0


def parse_pick(parts: list[str]) -> PickResult | None:
    try:
        res = PickResult(int(parts[1]), int(parts[2]))
        if len(parts) < 4 or int(parts[3]) < 0:
            return res
        mm = [int(v) / 1000.0 for v in parts[6:17]]
        if len(mm) < 11:
            return None
        res.ref, res.own, res.model = int(parts[3]), int(parts[4]), parts[5]
        res.pos, res.ori, res.scale = tuple(mm[0:3]), tuple(mm[3:6]), mm[6]
        res.x0, res.z0, res.yaw0, res.rel0 = mm[7], mm[8], mm[9], mm[10]
    except (ValueError, IndexError):
        return None
    return res


def _mm3(parts: list[str]) -> tuple[float, float, float] | None:
    try:
        x, y, z = (int(p) / 1000.0 for p in parts[:3])
    except (ValueError, TypeError):
        return None
    return (x, y, z)


def parse_state(text: str) -> BridgeState:
    """Разобрать файл состояния. Недописанный файл (без ``end``) -> complete=False."""
    st = BridgeState()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "end":
            st.complete = True
            break
        if line.startswith("WE_BRIDGE"):
            try:
                st.version = int(line.split()[1])
            except (IndexError, ValueError):
                pass
            continue
        if "|" in line:
            parts = line.split("|")
            try:
                if parts[0] == "Y" and len(parts) >= 4:
                    tb = int(parts[4]) / 1000.0 if len(parts) >= 5 and parts[4].strip() else None
                    st.placed[int(parts[1])] = (int(parts[2]) / 1000.0, int(parts[3]) / 1000.0, tb)
                elif parts[0] == "E" and len(parts) >= 2:
                    st.failed.add(int(parts[1]))
                elif parts[0] == "H":
                    st.hover = parts[1] if len(parts) > 1 else ""
                elif parts[0] == "P" and len(parts) >= 3:
                    st.pick = parse_pick(parts)
                elif parts[0] == "VF" and len(parts) >= 3:
                    st.verified[int(parts[1])] = int(parts[2])
                elif parts[0] == "KD":
                    st.keys_down = {n for n in (parts[1] if len(parts) > 1 else "").split(",") if n}
                elif parts[0] == "GZ":
                    st.gizmo = parse_gizmo(parts)
                elif parts[0] == "DP" and len(parts) >= 5:
                    st.drag = (int(parts[1]), int(parts[2]), int(parts[3]) / 1000.0,
                               int(parts[4]) / 1000.0)
            except ValueError:
                pass
            continue
        if line == "GE":
            st.ghost_failed = True
            continue
        if line == "FCE":
            st.free_camera_failed = True
            continue
        key, _, rest = line.partition(" ")
        parts = rest.split()
        if key == "seq" and parts:
            st.seq = _int(parts[0], -1)
        elif key == "gen" and parts:
            st.generation = _int(parts[0], -1)
        elif key == "hit" and len(parts) >= 3:
            st.hit = _mm3(parts)
        elif key == "cam" and len(parts) >= 3:
            st.camera = _mm3(parts)
        elif key == "dir" and len(parts) >= 3:
            st.direction = _mm3(parts)
        elif key == "FC" and parts:
            st.free_camera = parts[0] == "1"
        elif key == "CU" and parts:
            st.game_cursor = parts[0] == "1"
        elif key == "MF" and parts:
            st.mouse_focus = parts[0] == "1"
        elif key == "FR" and parts:
            try:
                st.focus_retakes = int(parts[0])
            except ValueError:
                pass
        elif key == "UM" and parts:
            st.cursor_menu = parts[0] == "1"
        elif key == "UH" and parts:
            st.menu_hidden = parts[0] == "1"
        elif key == "MO" and parts:
            st.menu_opens = _int(parts[0], 0)
        elif key == "CS" and parts:
            st.cursor_shows = _int(parts[0], 0)
        elif key == "CV" and parts:
            st.canvas = parts[0] == "1"
        elif key == "CF" and parts:
            st.cam_read_fails = _int(parts[0], 0)
        elif key == "bs" and parts:
            st.session = _int(parts[0], 0)
    return st


def _int(text: str, default: int) -> int:
    try:
        return int(text)
    except ValueError:
        return default


def _num(v: float) -> str:
    text = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _clean_model(model: str) -> str:
    return model.replace("|", "").replace("\r", "").replace("\n", "").replace("/", "\\").strip()


@dataclass
class PlacedObject:
    id: int
    model: str                 # путь модели как у игры: dz\structures\...\x.p3d
    x: float
    z: float
    yaw: float = 0.0
    scale: float = 1.0
    lift: float = 0.0          # подъём над землёй, м (0 = низ модели на земле)
    ground: float = 0.0        # высота земли в точке установки (из Buldozer)
    y: float | None = None     # высота начала координат модели, сообщённая скриптом
    center: float = 0.0        # перенос начала модели по высоте (BoundingCenter·масштаб, для TB)
    tb_rel: float | None = None  # высота для TB (Relative), посчитанная скриптом в Buldozer (v4)
    failed: bool = False       # Buldozer не смог создать модель
    # вставка в Terrain Builder (tblive): "" — нет, "sent" — файл отдан TB, ждём объект TB в
    # Buldozer; "ok" — TB его показал (наша копия убрана); "fail" — TB не принял
    tb: str = ""
    tb_at: float = 0.0         # когда отдали TB (time.time())

    def command(self) -> str:
        return "|".join(["O", str(self.id), _clean_model(self.model), _num(self.x), _num(self.z),
                         _num(self.yaw), _num(self.scale), _num(self.lift)])

    def tb_object(self, template_names: bool = True, relative_height: bool = True,
                  names: dict[str, str] | None = None) -> TBObject:
        """Строка для импорта в Terrain Builder (формат DayZ Editor).

        TB ищет объект по имени шаблона в своих библиотеках — это имя файла без ``.p3d``.
        Последнее поле TB при импорте записывает в **Relative** — высоту над землёй
        **исходного начала координат модели** (MLOD). Binarize переносит начало в центр
        габаритов, если в Geometry нет ``autocenter=0`` (перенос = ``GetBoundingCenter``), а
        Buldozer ставит модель низом на землю — поэтому Relative не равен подъёму из окна:
        у модели, чьё начало в исходнике посередине, без поправки низ уходит под землю.

        Порядок: готовое значение от скрипта (v4, точное: с поворотом и землёй под исходным
        началом) → высота начала минус перенос минус земля (скрипт v3) → подъём.
        ``relative_height=False`` — старый вариант DayZ Editor: та же высота, но абсолютная.
        """
        name = _clean_model(self.model)
        if template_names:
            known = (names or {}).get(name.strip("\\").lower())
            if known:                       # имя шаблона из TemplateLibs проекта
                name = known
            else:
                name = PureWindowsPath(name).name
                if name.lower().endswith(".p3d"):
                    name = name[:-4]
        if relative_height:
            if self.tb_rel is not None:
                y = self.tb_rel
            elif self.y is not None:
                y = self.y - self.center - self.ground
            else:
                y = self.lift
        else:
            y = (self.y - self.center) if self.y is not None else self.ground + self.lift
        return TBObject(name, self.x, self.z, self.yaw, 0.0, 0.0, self.scale, y)


def format_commands(generation: int, objects: Iterable[PlacedObject], ghost: str = "",
                    ghost_yaw: float = 0.0, ghost_scale: float = 1.0, ghost_lift: float = 0.0,
                    ghost_pin: tuple[float, float, float] | None = None,
                    extra: Iterable[str] = ()) -> str:
    """Файл команд. ``ghost_pin`` (x, y земли, z) — «призрак» стоит в этой точке, а не у курсора.

    ``extra`` — строки для объектов карты (v10): ``K|n`` выбрать под курсором,
    ``T|сеанс|номер|x|z|курс|подъём`` поставить, ``D|сеанс|номер`` везти за курсором,
    ``S|сеанс|номер`` выделить (рамка); ``V|id|x|z|модель`` — есть ли в точке объект TB (v14).
    Объекты, уже вставленные в Terrain Builder (``tb == "ok"``), не посылаются: их показывает TB.
    """
    lines = [f"WE_CMD {BRIDGE_VERSION}", f"gen {int(generation)}"]
    objects = [o for o in objects if getattr(o, "tb", "") != "ok"]   # их показывает сам TB
    model = _clean_model(ghost)
    if model:
        fields = ["G", model, _num(ghost_yaw), _num(ghost_scale), _num(ghost_lift)]
        if ghost_pin is not None:
            fields += [_num(v) for v in ghost_pin]
        lines.append("|".join(fields))
    else:
        lines.append("G|")
    lines.extend(o.command() for o in objects)
    lines.extend(extra)
    lines.append("end")
    return "\n".join(lines) + "\n"


class PlacementSession:
    """Объекты, расставленные из окна поверх Buldozer за сеанс (с отменой и сохранением)."""

    UNDO_LIMIT = 100

    def __init__(self, path: str | Path | None = SESSION_FILE) -> None:
        self.path = Path(path) if path else None
        self.objects: list[PlacedObject] = []
        self.next_id = 1
        self._undo: list[tuple[list[PlacedObject], list[MapEdit]]] = []
        self.map_edits: list[MapEdit] = []     # объекты карты, передвинутые в Buldozer
        self.samples: list[Sample] = []        # выбранные объекты карты (для сдвига координат слоя)

    # ---- правки
    def _snapshot(self) -> None:
        """Для «Отменить»: свои объекты и правки объектов карты (перенос, удаление) как сейчас."""
        self._undo.append(([PlacedObject(**asdict(o)) for o in self.objects],
                           [MapEdit(**asdict(e)) for e in self.map_edits]))
        if len(self._undo) > self.UNDO_LIMIT:
            self._undo.pop(0)

    def add(self, model: str, x: float, z: float, ground: float = 0.0, yaw: float = 0.0,
            scale: float = 1.0, lift: float = 0.0) -> PlacedObject:
        self._snapshot()
        obj = PlacedObject(self.next_id, _clean_model(model), float(x), float(z),
                           float(yaw) % 360.0, float(scale), float(lift), float(ground))
        self.next_id += 1
        self.objects.append(obj)
        self.save()
        return obj

    def remove(self, ids: Iterable[int]) -> int:
        drop = set(ids)
        if not drop.intersection(o.id for o in self.objects):
            return 0
        self._snapshot()
        before = len(self.objects)
        self.objects = [o for o in self.objects if o.id not in drop]
        self.save()
        return before - len(self.objects)

    def remove_last(self) -> PlacedObject | None:
        if not self.objects:
            return None
        last = self.objects[-1]
        self.remove([last.id])
        return last

    def clear(self) -> None:
        if self.objects:
            self._snapshot()
            self.objects = []
            self.save()

    def begin_change(self) -> None:
        """Перед правкой объекта (своего или карты) — запомнить для «Отменить»."""
        self._snapshot()

    def get(self, object_id: int) -> PlacedObject | None:
        for o in self.objects:
            if o.id == object_id:
                return o
        return None

    def edit_map_object(self, pick, session: int) -> MapEdit:
        """Правка для выбранного объекта карты: прежняя (тот же объект) или новая."""
        for e in self.map_edits:
            if (e.sess == session and e.ref == pick.ref) or e.same_object(pick.model, pick.x0, pick.z0):
                e.sess, e.ref = session, pick.ref
                return e
        e = MapEdit(pick.model, pick.x0, pick.z0, pick.yaw0 % 360.0, pick.rel0,
                    x=pick.x0, z=pick.z0, yaw=pick.yaw0 % 360.0, sess=session, ref=pick.ref)
        self.map_edits.append(e)
        return e

    def add_sample(self, model: str, x: float, z: float) -> None:
        if any(abs(s.x - x) < 0.01 and abs(s.z - z) < 0.01 for s in self.samples):
            return
        self.samples.append(Sample(model, x, z))
        del self.samples[:-40]
        self.save()

    def changed_map_edits(self) -> list[MapEdit]:
        return [e for e in self.map_edits if e.changed]

    def drop_map_edit(self, edit: MapEdit) -> None:
        self.map_edits = [e for e in self.map_edits if e is not edit]
        self.save()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.objects, self.map_edits = self._undo.pop()
        self.save()
        return True

    def apply_state(self, state: BridgeState) -> bool:
        """Взять из состояния высоты созданных объектов. True — что-то изменилось."""
        changed = False
        for o in self.objects:
            got = state.placed.get(o.id)
            if got is not None and (o.y != got[0] or o.center != got[1] or o.tb_rel != got[2]
                                    or o.failed):
                o.y, o.center, o.tb_rel, o.failed = got[0], got[1], got[2], False
                changed = True
            elif o.id in state.failed and not o.failed:
                o.failed = True
                changed = True
        if changed:
            self.save()
        return changed

    # ---- экспорт / сохранение
    def tb_text(self, template_names: bool = True, relative_height: bool = True,
                names: dict[str, str] | None = None,
                offset: tuple[float, float] | None = None) -> str:
        """``offset`` — начало рамки карты TB (из ``*.tv4p``); None — как DayZ Editor (+200000 по X)."""
        return format_objects((o.tb_object(template_names, relative_height, names)
                               for o in self.objects), offset)

    def export_tb(self, path: str | Path, template_names: bool = True,
                  relative_height: bool = True, names: dict[str, str] | None = None,
                  offset: tuple[float, float] | None = None) -> int:
        Path(path).write_text(self.tb_text(template_names, relative_height, names, offset),
                              encoding="utf-8", newline="")
        return len(self.objects)

    def save(self) -> None:
        if not self.path:
            return
        data = {"version": 1, "next_id": self.next_id, "objects": [asdict(o) for o in self.objects],
                "map_edits": [asdict(e) for e in self.map_edits],
                "samples": [asdict(s) for s in self.samples]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            objs = [PlacedObject(**{k: v for k, v in o.items() if k in PlacedObject.__dataclass_fields__})
                    for o in data.get("objects", [])]
            edits = [MapEdit(**{k: v for k, v in e.items() if k in MapEdit.__dataclass_fields__})
                     for e in data.get("map_edits", [])]
            samples = [Sample(**{k: v for k, v in e.items() if k in Sample.__dataclass_fields__})
                       for e in data.get("samples", [])]
        except (OSError, ValueError, TypeError):
            return
        self.map_edits = edits
        self.samples = samples
        self.objects = objs
        self.next_id = max([int(data.get("next_id", 1))] + [o.id + 1 for o in objs])


def placement_values(base_yaw: float, random_yaw: bool, scale: float, scale_jitter: float,
                     rng: random.Random | None = None) -> tuple[float, float]:
    """Поворот и масштаб для очередного объекта (случайный поворот / разброс масштаба в %)."""
    rng = rng or random
    yaw = rng.uniform(0.0, 360.0) if random_yaw else base_yaw
    k = 1.0
    if scale_jitter > 0:
        j = min(scale_jitter, 90.0) / 100.0
        k = rng.uniform(1.0 - j, 1.0 + j)
    return yaw % 360.0, max(0.01, scale * k)


def new_generation() -> int:
    """Стартовый номер поколения: разный у разных запусков окна (скрипт сравнивает на !=)."""
    return (int(time.time()) % 100_000_000) * 10


# ----------------------------------------------------------------- где искать $profile:

def profile_candidates(settings: dict, home: Path | None = None) -> list[Path]:
    """Папки, в которые Buldozer может писать ``$profile:`` (по порядку предпочтения)."""
    home = home or Path.home()
    out: list[str] = []
    if settings.get("overlay_profile_dir"):
        out.append(str(settings["overlay_profile_dir"]))
    drive = resolve_work_drive(settings.get("work_drive"), settings.get("scripts_dir"),
                               settings.get("project_dir")).rstrip("\\/")
    out.append(drive + "\\Buldozer")
    docs = home / "Documents"
    out += [str(docs / "DayZ Other Profiles" / "Buldozer"), str(docs / "DayZ")]
    seen: set[str] = set()
    result: list[Path] = []
    for p in out:
        key = p.lower().rstrip("\\/")
        if key not in seen:
            seen.add(key)
            result.append(Path(p))
    return result


def freshest_state(candidates: Iterable[Path], now: float | None = None,
                   max_age: float = STATE_MAX_AGE) -> tuple[Path | None, float | None]:
    """(папка, возраст файла состояния, с) — самая свежая из кандидатов; (None, None), если нигде."""
    now = time.time() if now is None else now
    best: tuple[Path | None, float | None] = (None, None)
    for folder in candidates:
        try:
            age = now - (folder / STATE_FILE).stat().st_mtime
        except OSError:
            continue
        if best[1] is None or age < best[1]:
            best = (folder, age)
    if best[1] is not None and best[1] > max_age:
        return best      # нашли, но старый: вызывающий покажет «нет связи»
    return best
