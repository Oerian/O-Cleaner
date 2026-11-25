/* Minimal Win32 GUI launcher for O-Cleaner (zero console window).
 * Compiles with MSVC (`cl /O2 /Fe:O-Cleaner.exe launcher.c user32.lib`)
 * or MinGW (`gcc -mwindows -O2 launcher.c -o O-Cleaner.exe`).
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

int WINAPI wWinMain(HINSTANCE hInst, HINSTANCE hPrev, LPWSTR lpCmd, int nShow) {
    (void)hInst; (void)hPrev; (void)nShow;
    wchar_t dir[MAX_PATH], pyw[MAX_PATH], app[MAX_PATH], cmd[2048];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 1;
    wchar_t *slash = wcsrchr(dir, L'\\');
    if (slash) *slash = L'\0';

    _snwprintf(pyw, MAX_PATH, L"%s\\python\\pythonw.exe", dir);
    _snwprintf(app, MAX_PATH, L"%s\\app.py", dir);
    const wchar_t *exe = (GetFileAttributesW(pyw) != INVALID_FILE_ATTRIBUTES)
                         ? pyw : L"pythonw.exe";
    _snwprintf(cmd, 2048, L"\"%s\" \"%s\" --app %s",
               exe, app, (lpCmd && *lpCmd) ? lpCmd : L"");

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE,
                        CREATE_NO_WINDOW, NULL, dir, &si, &pi)) {
        MessageBoxW(NULL, L"Failed to start O-Cleaner (pythonw.exe not found).",
                    L"O-Cleaner", MB_OK | MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
