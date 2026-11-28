# -*- coding: utf-8 -*-
"""端口探测、桌面应用窗口与系统托盘域（从 core.py 迁出）。

包含空闲端口探测（pick_port）、Edge/Chrome `--app` 横向居中窗口布局与
启动（_calc_app_window_rect / _open_app_window）以及纯 ctypes 系统托盘线程
（_tray_worker）。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import webbrowser

import ocleaner.core as _core


def pick_port(preferred: int) -> int:
    """找一个真正空闲的端口（裸 bind 探测，不带 SO_REUSEADDR）。"""
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        if port != preferred:
            print(f"端口 {preferred} 已被占用（可能已有 O-Cleaner 在运行），"
                  f"改用 {port}")
        return port
    raise SystemExit(f"{preferred} 起连续 30 个端口都不可用")


def _calc_app_window_rect() -> tuple[int, int, int, int]:
    """按当前桌面工作区计算居中的横向矩形默认窗口 (x, y, w, h)（逻辑像素）。"""
    left, top, work_w, work_h = 0, 0, 1440, 900
    try:
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            ww = rect.right - rect.left
            wh = rect.bottom - rect.top
            if ww >= 640 and wh >= 480:
                scale = 1.0
                try:
                    dpi = ctypes.windll.user32.GetDpiForSystem()
                    if dpi and dpi > 96:
                        scale = dpi / 96.0
                except Exception:
                    pass
                left = int(rect.left / scale)
                top = int(rect.top / scale)
                work_w = int(ww / scale)
                work_h = int(wh / scale)
    except Exception:
        pass
    w = max(min(work_w - 40, 1180), min(int(work_w * 0.80), 1480))
    h = min(work_h - 48, max(660, int(w * 0.58)))
    x = left + max(0, (work_w - w) // 2)
    y = top + max(0, (work_h - h) // 2)
    return x, y, w, h


def _open_app_window(url: str) -> None:
    """Edge/Chrome `--app` 无边框窗口，找不到浏览器时回退系统默认浏览器。"""
    x, y, w, h = _calc_app_window_rect()
    prof_dir = str(_core.RUNTIME_DIR / "app-profile")
    try:
        os.makedirs(prof_dir, exist_ok=True)
    except OSError:
        prof_dir = ""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if os.path.isfile(exe):
            try:
                cmd = [exe, "--new-window", f"--app={url}",
                       f"--window-size={w},{h}", f"--window-position={x},{y}"]
                if prof_dir:
                    cmd.append(f"--user-data-dir={prof_dir}")
                subprocess.Popen(cmd, creationflags=_core.CREATE_NO_WINDOW)
                return
            except OSError:
                continue
    webbrowser.open(url)


def _tray_worker(url: str) -> None:
    """系统托盘常驻（纯 ctypes 零依赖）：左键双击打开界面，右键菜单支持安全清理与退出。"""
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_longlong
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = wintypes.HICON
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user32.CreateWindowExW.restype = wintypes.HWND

        WM_USER_TRAY = 0x0400 + 1
        IDM_OPEN, IDM_CLEAN, IDM_QUIT = 1001, 1002, 1003
        nid_cb = 0

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_ubyte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_longlong)

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_USER_TRAY:
                if lparam == 0x0203:
                    webbrowser.open(url)
                elif lparam == 0x0205:
                    user32.SetForegroundWindow(hwnd)
                    hmenu = user32.CreatePopupMenu()
                    user32.AppendMenuW(hmenu, 0, IDM_OPEN, "打开界面")
                    user32.AppendMenuW(hmenu, 0, IDM_CLEAN, "立即安全清理")
                    user32.AppendMenuW(hmenu, 0x800, 0, None)
                    user32.AppendMenuW(hmenu, 0, IDM_QUIT, "退出")
                    pos = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pos))
                    cmd_id = user32.TrackPopupMenu(
                        hmenu, 0x0182, pos.x, pos.y, 0, hwnd, None)
                    user32.PostMessageW(hwnd, 0, 0, 0)
                    user32.DestroyMenu(hmenu)
                    if cmd_id == IDM_OPEN:
                        webbrowser.open(url)
                    elif cmd_id == IDM_CLEAN:
                        import threading as _t
                        sys_drv = (os.environ.get("SystemDrive") or "C:").rstrip(":\\/")
                        _t.Thread(target=_core.run_auto_clean, args=(sys_drv,),
                                  daemon=True).start()
                        nid = NOTIFYICONDATAW(
                            cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd,
                            uID=nid_cb, uFlags=0x10, szInfo="安全清理已在后台开始"
                            "（只清 safe 预设安全项）",
                            szInfoTitle="O-Cleaner")
                        shell32.Shell_NotifyIconW(1, ctypes.byref(nid))
                    elif cmd_id == IDM_QUIT:
                        user32.PostQuitMessage(0)
            elif msg == 0x0111:
                if wparam & 0xFFFF == IDM_OPEN:
                    webbrowser.open(url)
                elif wparam & 0xFFFF == IDM_CLEAN:
                    import threading as _t
                    _t.Thread(target=_core.run_auto_clean, args=("C",),
                              daemon=True).start()
                    nid = NOTIFYICONDATAW(
                        cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd,
                        uID=nid_cb, uFlags=0x10, szInfo="安全清理已在后台开始"
                        "（只清建议清理项，高风险入回收站）",
                        szInfoTitle="O-Cleaner")
                    shell32.Shell_NotifyIconW(1, ctypes.byref(nid))
                elif wparam & 0xFFFF == IDM_QUIT:
                    user32.PostQuitMessage(0)
            elif msg == 0x0010:
                user32.PostQuitMessage(0)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wndproc = WNDPROC(_wndproc)
        hinst = kernel32.GetModuleHandleW(None)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT),
                        ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]
        wc = WNDCLASSW(style=0, lpfnWndProc=wndproc, hInstance=hinst,
                       lpszClassName="OCleanerTray")
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return
        hwnd = user32.CreateWindowExW(
            0, "OCleanerTray", "O-Cleaner", 0, 0, 0, 0, 0, None, None,
            hinst, None)
        if not hwnd:
            return
        hicon = user32.LoadIconW(None, 32512)
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                              hWnd=hwnd, uID=1, uFlags=0x01 | 0x02 | 0x04,
                              uCallbackMessage=WM_USER_TRAY, hIcon=hicon,
                              szTip="O-Cleaner —— 双击打开 / 右键菜单")
        nid_cb = 1
        if not shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
        os._exit(0)
    except Exception:  # noqa: BLE001
        pass
