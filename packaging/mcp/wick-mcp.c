/* wick-mcp.exe: the program in Wick's Claude Desktop extension (.mcpb). It finds the installed
 * Wick and runs "Wick.exe mcp <arguments>" with the same standard input and output, so the client
 * talks to the app's MCP server directly. Nothing else: no network, no files, no settings of its own.
 *
 * Where Wick is: WICK_EXE (the extension's optional "Wick.exe location" setting, for the
 * portable zip), else the App Paths entry the installer writes (per user, then for everyone).
 * The server ends when this launcher is closed (a job object), so nothing is left running.
 *
 * Build (MSVC):  cl /nologo /O2 /MT /W4 wick-mcp.c /link advapi32.lib /OUT:wick-mcp.exe
 */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

#define APP_PATHS L"Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\Wick.exe"

static int exists(const wchar_t *path)
{
    DWORD a = GetFileAttributesW(path);
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}

static int from_registry(HKEY root, wchar_t *out, DWORD chars)
{
    DWORD bytes = chars * sizeof(wchar_t);
    return RegGetValueW(root, APP_PATHS, NULL, RRF_RT_REG_SZ, NULL, out, &bytes) == ERROR_SUCCESS && exists(out);
}

/* The rest of this program's own command line, exactly as the client quoted it. */
static const wchar_t *arguments(void)
{
    const wchar_t *p = GetCommandLineW();
    if (*p == L'"') {
        p++;
        while (*p && *p != L'"') p++;
        if (*p) p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t') p++;
    }
    while (*p == L' ' || *p == L'\t') p++;
    return p;
}

static void fail(const char *message)
{
    fprintf(stderr, "Wick MCP: %s\n", message);
}

int wmain(void)
{
    static wchar_t exe[4096];
    DWORD n = GetEnvironmentVariableW(L"WICK_EXE", exe, ARRAYSIZE(exe));
    if (!(n > 0 && n < ARRAYSIZE(exe) && exists(exe))
        && !from_registry(HKEY_CURRENT_USER, exe, ARRAYSIZE(exe))
        && !from_registry(HKEY_LOCAL_MACHINE, exe, ARRAYSIZE(exe))) {
        fail("Wick isn't installed (or was moved). Install it from https://github.com/Lanntern/Wick, "
             "or set \"Wick.exe location\" in the extension's settings.");
        return 2;
    }

    const wchar_t *rest = arguments();
    size_t len = wcslen(exe) + wcslen(rest) + 16;
    wchar_t *cmd = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, len * sizeof(wchar_t));
    if (!cmd) {
        fail("out of memory");
        return 1;
    }
    swprintf(cmd, len, L"\"%ls\" mcp %ls", exe, rest);

    /* The client's pipes go to the app unchanged. */
    STARTUPINFOW si;
    ZeroMemory(&si, sizeof si);
    si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    HANDLE std[3] = { si.hStdInput, si.hStdOutput, si.hStdError };
    for (int i = 0; i < 3; i++) {
        if (std[i] && std[i] != INVALID_HANDLE_VALUE)
            SetHandleInformation(std[i], HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
    }

    /* Closing this launcher (the client stopping the server) closes the app's server too. */
    HANDLE job = CreateJobObjectW(NULL, NULL);
    if (job) {
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info;
        ZeroMemory(&info, sizeof info);
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job, JobObjectExtendedLimitInformation, &info, sizeof info);
    }

    PROCESS_INFORMATION pi;
    ZeroMemory(&pi, sizeof pi);
    if (!CreateProcessW(exe, cmd, NULL, NULL, TRUE, CREATE_SUSPENDED | CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) {
        fail("couldn't start Wick.");
        return 1;
    }
    if (job)
        AssignProcessToJobObject(job, pi.hProcess);
    ResumeThread(pi.hThread);
    CloseHandle(pi.hThread);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    return (int)code;
}
