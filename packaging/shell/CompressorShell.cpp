// Compressor's entries in Windows 11's main right-click menu (not just "Show more options").
//
// Windows 11 only puts IExplorerCommand handlers from a package with identity in its main menu, so this DLL is
// registered by a small sparse package (packaging/shell/AppxManifest.xml) that points at the installed app.
// It has no menu of its own: it shows exactly what the classic menu has, read from the same registry keys
// (compressor/gui/integration.py writes them): "Compress with Compressor" with the current options and each
// preset, and "Convert with Compressor" for pictures, videos and music. Turning the menu off in Settings removes
// those keys, which hides these too. Choosing an item starts Compressor once with all the selected paths.
//
// Plain COM, no WRL/ATL, so it builds with MSVC (CI) or MinGW.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <shlwapi.h>
#include <shobjidl.h>
#include <shlobj.h>
#include <new>
#include <string>
#include <vector>
#include <algorithm>

// {c0dc2a7f-2eb1-46b8-8cd3-549d97989547}: "Compress with Compressor"
static const CLSID CLSID_Compress = {0xc0dc2a7f, 0x2eb1, 0x46b8, {0x8c, 0xd3, 0x54, 0x9d, 0x97, 0x98, 0x95, 0x47}};
// {ab141279-6699-40e3-9afc-ae9f9f4373dc}: "Convert with Compressor"
static const CLSID CLSID_Convert = {0xab141279, 0x6699, 0x40e3, {0x9a, 0xfc, 0xae, 0x9f, 0x9f, 0x43, 0x73, 0xdc}};

static LONG g_objects = 0;
static LONG g_locks = 0;

// ------------------------------------------------------------------------------------------- registry helpers

static std::wstring ReadString(HKEY root, const std::wstring& key, const wchar_t* value) {
    wchar_t buf[2048];
    DWORD size = sizeof(buf);
    if (RegGetValueW(root, key.c_str(), value, RRF_RT_REG_SZ, nullptr, buf, &size) != ERROR_SUCCESS) return L"";
    return buf;
}

static DWORD ReadDword(HKEY root, const std::wstring& key, const wchar_t* value) {
    DWORD v = 0, size = sizeof(v);
    if (RegGetValueW(root, key.c_str(), value, RRF_RT_REG_DWORD, nullptr, &v, &size) != ERROR_SUCCESS) return 0;
    return v;
}

static bool KeyExists(const std::wstring& key) {
    HKEY h;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, key.c_str(), 0, KEY_READ, &h) != ERROR_SUCCESS) return false;
    RegCloseKey(h);
    return true;
}

static std::vector<std::wstring> SubKeys(const std::wstring& key) {
    std::vector<std::wstring> out;
    HKEY h;
    if (RegOpenKeyExW(HKEY_CURRENT_USER, key.c_str(), 0, KEY_READ, &h) != ERROR_SUCCESS) return out;
    wchar_t name[256];
    for (DWORD i = 0;; ++i) {
        DWORD len = 256;
        if (RegEnumKeyExW(h, i, name, &len, nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS) break;
        out.push_back(name);
    }
    RegCloseKey(h);
    std::sort(out.begin(), out.end());           // 00, 01, ...: the classic menu's order
    return out;
}

// The kind of file Windows says it is ("image", "video", "audio"), from its extension.
static std::wstring PerceivedType(const std::wstring& path) {
    const wchar_t* ext = PathFindExtensionW(path.c_str());
    if (!ext || !*ext) return L"";
    std::wstring t = ReadString(HKEY_CLASSES_ROOT, ext, L"PerceivedType");
    std::transform(t.begin(), t.end(), t.begin(), ::towlower);
    return t;
}

static std::vector<std::wstring> SelectedPaths(IShellItemArray* items) {
    std::vector<std::wstring> paths;
    DWORD count = 0;
    if (!items || FAILED(items->GetCount(&count))) return paths;
    for (DWORD i = 0; i < count; ++i) {
        IShellItem* item = nullptr;
        if (FAILED(items->GetItemAt(i, &item))) continue;
        PWSTR p = nullptr;
        if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &p)) && p) {
            paths.push_back(p);
            CoTaskMemFree(p);
        }
        item->Release();
    }
    return paths;
}

// The classic menu's key for this command and selection ("" = nothing to show).
static std::wstring MenuKey(bool convert, IShellItemArray* items) {
    if (!convert) {
        std::wstring key = L"Software\\Classes\\*\\shell\\Compressor";
        return KeyExists(key) ? key : L"";
    }
    std::vector<std::wstring> paths = SelectedPaths(items);
    if (paths.empty()) return L"";
    std::wstring kind = PerceivedType(paths[0]);
    if (kind != L"image" && kind != L"video" && kind != L"audio") return L"";
    for (const auto& p : paths)
        if (PerceivedType(p) != kind) return L"";          // a mix of kinds: no single list of formats fits
    std::wstring key = L"Software\\Classes\\SystemFileAssociations\\" + kind + L"\\shell\\CompressorConvert";
    return KeyExists(key) ? key : L"";
}

static std::wstring Quote(const std::wstring& s) { return L"\"" + s + L"\""; }

// Run an item's classic command once for the whole selection: its "%1" becomes every selected path.
static HRESULT RunCommand(const std::wstring& command, const std::vector<std::wstring>& paths) {
    std::wstring base = command;
    size_t at = base.rfind(L"\"%1\"");
    if (at != std::wstring::npos) base.erase(at);
    // Windows allows 32767 characters on a command line: start more processes for very large selections (the
    // running Compressor collects them, as it does for the classic menu).
    std::wstring line = base;
    size_t added = 0;
    auto launch = [](std::wstring cmd) {
        STARTUPINFOW si{};
        si.cb = sizeof(si);
        PROCESS_INFORMATION pi{};
        if (CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, FALSE, 0, nullptr, nullptr, &si, &pi)) {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            return true;
        }
        return false;
    };
    for (const auto& p : paths) {
        std::wstring part = L" " + Quote(p);
        if (added && line.size() + part.size() > 30000) {
            launch(line);
            line = base;
            added = 0;
        }
        line += part;
        ++added;
    }
    if (added && !launch(line)) return HRESULT_FROM_WIN32(GetLastError());
    return S_OK;
}

// ------------------------------------------------------------------------------------------- commands

class Command : public IExplorerCommand {
public:
    // A root ("Compress with Compressor") when key is empty and convert says which; else one item of a submenu.
    Command(bool convert, std::wstring itemKey = L"", bool separator = false)
        : m_ref(1), m_convert(convert), m_itemKey(std::move(itemKey)), m_separator(separator) {
        InterlockedIncrement(&g_objects);
    }
    virtual ~Command() { InterlockedDecrement(&g_objects); }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IExplorerCommand)) {
            *ppv = static_cast<IExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    IFACEMETHODIMP_(ULONG) Release() override {
        ULONG r = InterlockedDecrement(&m_ref);
        if (!r) delete this;
        return r;
    }

    IFACEMETHODIMP GetTitle(IShellItemArray* items, LPWSTR* name) override {
        std::wstring title;
        if (m_itemKey.empty())
            title = m_convert ? L"Convert with Compressor" : L"Compress with Compressor";
        else
            title = ReadString(HKEY_CURRENT_USER, m_itemKey, L"MUIVerb");
        return SHStrDupW(title.c_str(), name);
    }
    IFACEMETHODIMP GetIcon(IShellItemArray* items, LPWSTR* icon) override {
        if (!m_itemKey.empty()) { *icon = nullptr; return E_NOTIMPL; }
        std::wstring key = m_convert ? MenuKey(true, items) : MenuKey(false, items);
        std::wstring path = key.empty() ? L"" : ReadString(HKEY_CURRENT_USER, key, L"Icon");
        if (path.empty()) { *icon = nullptr; return E_NOTIMPL; }
        return SHStrDupW(path.c_str(), icon);
    }
    IFACEMETHODIMP GetToolTip(IShellItemArray*, LPWSTR* tip) override { *tip = nullptr; return E_NOTIMPL; }
    IFACEMETHODIMP GetCanonicalName(GUID* guid) override {
        *guid = m_convert ? CLSID_Convert : CLSID_Compress;
        return S_OK;
    }
    IFACEMETHODIMP GetState(IShellItemArray* items, BOOL, EXPCMDSTATE* state) override {
        *state = ECS_ENABLED;
        if (m_itemKey.empty() && MenuKey(m_convert, items).empty()) *state = ECS_HIDDEN;
        return S_OK;
    }
    IFACEMETHODIMP Invoke(IShellItemArray* items, IBindCtx*) override {
        if (m_itemKey.empty() || m_separator) return S_OK;
        std::wstring command = ReadString(HKEY_CURRENT_USER, m_itemKey + L"\\command", nullptr);
        if (command.empty()) return E_FAIL;
        return RunCommand(command, SelectedPaths(items));
    }
    IFACEMETHODIMP GetFlags(EXPCMDFLAGS* flags) override {
        *flags = m_separator ? ECF_ISSEPARATOR : (m_itemKey.empty() ? ECF_HASSUBCOMMANDS : ECF_DEFAULT);
        return S_OK;
    }
    IFACEMETHODIMP EnumSubCommands(IEnumExplorerCommand** out) override;

private:
    LONG m_ref;
    bool m_convert;
    std::wstring m_itemKey;
    bool m_separator;
};

class SubCommands : public IEnumExplorerCommand {
public:
    explicit SubCommands(std::vector<Command*> items) : m_ref(1), m_items(std::move(items)), m_pos(0) {
        InterlockedIncrement(&g_objects);
    }
    virtual ~SubCommands() {
        for (auto* c : m_items) c->Release();
        InterlockedDecrement(&g_objects);
    }
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == __uuidof(IEnumExplorerCommand)) {
            *ppv = static_cast<IEnumExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    IFACEMETHODIMP_(ULONG) Release() override {
        ULONG r = InterlockedDecrement(&m_ref);
        if (!r) delete this;
        return r;
    }
    IFACEMETHODIMP Next(ULONG count, IExplorerCommand** out, ULONG* fetched) override {
        ULONG n = 0;
        for (; n < count && m_pos < m_items.size(); ++n, ++m_pos) {
            m_items[m_pos]->AddRef();
            out[n] = m_items[m_pos];
        }
        if (fetched) *fetched = n;
        return n == count ? S_OK : S_FALSE;
    }
    IFACEMETHODIMP Skip(ULONG count) override {
        m_pos = std::min<size_t>(m_items.size(), m_pos + count);
        return S_OK;
    }
    IFACEMETHODIMP Reset() override { m_pos = 0; return S_OK; }
    IFACEMETHODIMP Clone(IEnumExplorerCommand** out) override { *out = nullptr; return E_NOTIMPL; }

private:
    LONG m_ref;
    std::vector<Command*> m_items;
    size_t m_pos;
};

// The submenu: one item per classic entry, with the classic menu's separators ("CommandFlags" 0x40: a line
// after this item). The selection isn't known here, so Convert lists the kind GetState picked last.
static std::wstring g_lastConvertKey;

IFACEMETHODIMP Command::EnumSubCommands(IEnumExplorerCommand** out) {
    *out = nullptr;
    if (!m_itemKey.empty()) return E_NOTIMPL;
    std::wstring key = m_convert ? g_lastConvertKey : MenuKey(false, nullptr);
    if (key.empty()) return E_FAIL;
    std::vector<Command*> items;
    for (const auto& sub : SubKeys(key + L"\\shell")) {
        std::wstring itemKey = key + L"\\shell\\" + sub;
        items.push_back(new (std::nothrow) Command(m_convert, itemKey));
        if (ReadDword(HKEY_CURRENT_USER, itemKey, L"CommandFlags") & 0x40)
            items.push_back(new (std::nothrow) Command(m_convert, itemKey, true));
    }
    items.erase(std::remove(items.begin(), items.end(), nullptr), items.end());
    auto* e = new (std::nothrow) SubCommands(std::move(items));
    if (!e) return E_OUTOFMEMORY;
    *out = e;
    return S_OK;
}

// Convert's submenu depends on the selection, which EnumSubCommands doesn't get: remember it from GetState.
class ConvertRoot : public Command {
public:
    ConvertRoot() : Command(true) {}
    IFACEMETHODIMP GetState(IShellItemArray* items, BOOL ok, EXPCMDSTATE* state) override {
        g_lastConvertKey = MenuKey(true, items);
        *state = g_lastConvertKey.empty() ? ECS_HIDDEN : ECS_ENABLED;
        return S_OK;
    }
};

// ------------------------------------------------------------------------------------------- class factory

class Factory : public IClassFactory {
public:
    explicit Factory(bool convert) : m_ref(1), m_convert(convert) {}
    virtual ~Factory() = default;
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) {
            *ppv = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override { return InterlockedIncrement(&m_ref); }
    IFACEMETHODIMP_(ULONG) Release() override {
        ULONG r = InterlockedDecrement(&m_ref);
        if (!r) delete this;
        return r;
    }
    IFACEMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** ppv) override {
        *ppv = nullptr;
        if (outer) return CLASS_E_NOAGGREGATION;
        Command* c = m_convert ? static_cast<Command*>(new (std::nothrow) ConvertRoot())
                               : new (std::nothrow) Command(false);
        if (!c) return E_OUTOFMEMORY;
        HRESULT hr = c->QueryInterface(riid, ppv);
        c->Release();
        return hr;
    }
    IFACEMETHODIMP LockServer(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_locks); else InterlockedDecrement(&g_locks);
        return S_OK;
    }

private:
    LONG m_ref;
    bool m_convert;
};

extern "C" HRESULT __stdcall DllGetClassObject(REFCLSID clsid, REFIID riid, void** ppv) {
    *ppv = nullptr;
    bool convert;
    if (IsEqualCLSID(clsid, CLSID_Compress)) convert = false;
    else if (IsEqualCLSID(clsid, CLSID_Convert)) convert = true;
    else return CLASS_E_CLASSNOTAVAILABLE;
    auto* f = new (std::nothrow) Factory(convert);
    if (!f) return E_OUTOFMEMORY;
    HRESULT hr = f->QueryInterface(riid, ppv);
    f->Release();
    return hr;
}

extern "C" HRESULT __stdcall DllCanUnloadNow() {
    return (g_objects == 0 && g_locks == 0) ? S_OK : S_FALSE;
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
