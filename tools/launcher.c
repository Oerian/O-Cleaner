/* Minimal Win32 GUI launcher for O-Cleaner (zero console window).
 * Compiles with MSVC (`cl /O2 /DUNICODE /D_UNICODE /Fe:O-Cleaner.exe launcher.c user32.lib`)
 * or MinGW (`gcc -mwindows -municode -O2 launcher.c -o O-Cleaner.exe`).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdlib.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, LPWSTR lpCmd, int nShow) {
    (void)hInst; (void)hPrev; (void)nShow;
    wchar_t dir[MAX_PATH], work[MAX_PATH], pyw[MAX_PATH], app[MAX_PATH], found[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    if (dir[0] && dir[1] == L':' && dir[2] == L'\0') {
        _snwprintf(work, MAX_PATH, L"%s\\", dir);
    } else {
        _snwprintf(work, MAX_PATH, L"%s", dir);
    }
    work[MAX_PATH - 1] = L'\0';

    int r1 = _snwprintf(pyw, MAX_PATH, L"%s\\python\\pythonw.exe", dir);
    int r2 = _snwprintf(app, MAX_PATH, L"%s\\app.py", dir);
    if (r1 < 0 || r1 >= MAX_PATH || r2 < 0 || r2 >= MAX_PATH) return 1;
    pyw[MAX_PATH - 1] = L'\0';
    app[MAX_PATH - 1] = L'\0';

    const wchar_t *exe = NULL;
    DWORD attrs = GetFileAttributesW(pyw);
    if (attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        exe = pyw;
    } else if (SearchPathW(NULL, L"pythonw.exe", NULL, MAX_PATH, found, NULL) > 0) {
        found[MAX_PATH - 1] = L'\0';
        exe = found;
    }
    if (!exe) {
        MessageBoxW(NULL, L"Failed to start O-Cleaner (pythonw.exe not found).",
                    L"O-Cleaner", MB_OK | MB_ICONERROR);
        return 1;
    }

    const wchar_t *extra = (lpCmd && *lpCmd) ? lpCmd : L"";
    size_t cap = wcslen(exe) + wcslen(app) + wcslen(extra) + 32;
    wchar_t *cmd = (wchar_t *)malloc(cap * sizeof(wchar_t));
    if (!cmd) return 1;
    int r3 = _snwprintf(cmd, cap, L"\"%s\" \"%s\" --app %s", exe, app, extra);
    if (r3 < 0 || (size_t)r3 >= cap) {
        free(cmd);
        return 1;
    }
    cmd[cap - 1] = L'\0';

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    BOOL ok = CreateProcessW(exe, cmd, NULL, NULL, FALSE,
                             CREATE_NO_WINDOW, NULL, work, &si, &pi);
    free(cmd);
    if (!ok) {
        MessageBoxW(NULL, L"Failed to start O-Cleaner (pythonw.exe failed).",
                    L"O-Cleaner", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
