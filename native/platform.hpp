#pragma once
#include "protocol.hpp"
#include <imm.h>
#include <cwctype>
#include <atomic>
#include <memory>
#include <thread>

namespace kana {
struct Identity {
    HWND foreground = nullptr, focus = nullptr;
    DWORD pid = 0;
    bool operator==(const Identity& rhs) const { return foreground == rhs.foreground && focus == rhs.focus && pid == rhs.pid; }
    bool operator!=(const Identity& rhs) const { return !(*this == rhs); }
};
inline Identity identity() {
    Identity value;
    value.foreground = GetForegroundWindow();
    GUITHREADINFO info{};
    info.cbSize = sizeof(info);
    require(value.foreground && GetGUIThreadInfo(GetWindowThreadProcessId(value.foreground, nullptr), &info) && info.hwndFocus,
            "Cannot obtain focused input control");
    value.focus = info.hwndFocus;
    GetWindowThreadProcessId(value.focus, &value.pid);
    return value;
}
inline std::wstring window_class(HWND hwnd) {
    wchar_t value[256]{};
    require(GetClassNameW(hwnd, value, 256) != 0, "Cannot obtain input class");
    std::wstring result(value);
    std::transform(result.begin(), result.end(), result.begin(), [](wchar_t ch) { return wchar_t(towlower(ch)); });
    return result;
}
inline bool standard(HWND hwnd) {
    auto name = window_class(hwnd);
    return name == L"edit" || starts(name, L"richedit");
}
inline bool pressed(int key) { return (GetAsyncKeyState(key) & 0x8000) != 0; }
inline void input_ready(HWND hwnd, bool modifiers = false) {
    if (!modifiers)
        for (auto key : {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN}) require(!pressed(key), "Release modifier keys");
    for (auto key : {VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2}) require(!pressed(key), "Release mouse buttons");
    if (GetWindowThreadProcessId(hwnd, nullptr) == GetCurrentThreadId()) {
        HIMC context = ImmGetContext(hwnd);
        if (context) {
            LONG length = ImmGetCompositionStringW(context, GCS_COMPSTR, nullptr, 0);
            ImmReleaseContext(hwnd, context);
            require(length <= 0, "Commit IME composition first");
        }
    }
}
inline void release_keys(const Identity& target, const std::vector<int>& trigger) {
    Stage timing("key_release");
    auto deadline = Clock::now() + std::chrono::seconds(2);
    while (true) {
        require(identity() == target, "Focus changed while waiting for key release");
        bool held = false;
        for (auto key : {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN}) held |= pressed(key);
        for (auto key : trigger) held |= pressed(key);
        if (!held) return;
        require(Clock::now() < deadline, "Release conversion hotkey");
        Sleep(5);
    }
}
class Activity {
    std::thread thread_;
    HANDLE ready_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    std::atomic<DWORD> thread_id_{0};
    std::atomic<bool> alive_{false};
    inline static std::atomic<uint64_t> count_{0};
    static LRESULT CALLBACK keyboard(int code, WPARAM message, LPARAM data) {
        if (code == HC_ACTION && (message == WM_KEYDOWN || message == WM_SYSKEYDOWN)) ++count_;
        return CallNextHookEx(nullptr, code, message, data);
    }
    static LRESULT CALLBACK mouse(int code, WPARAM message, LPARAM data) {
        if (code == HC_ACTION && (message == WM_LBUTTONDOWN || message == WM_RBUTTONDOWN ||
            message == WM_MBUTTONDOWN || message == WM_XBUTTONDOWN || message == WM_MOUSEWHEEL || message == WM_MOUSEHWHEEL)) ++count_;
        return CallNextHookEx(nullptr, code, message, data);
    }
public:
    void start() {
        if (thread_.joinable()) { tick(); return; }
        require(ready_ != nullptr, "Cannot create input monitor event");
        thread_ = std::thread([this] {
            MSG message{};
            PeekMessageW(&message, nullptr, 0, 0, PM_NOREMOVE);
            thread_id_ = GetCurrentThreadId();
            auto module = GetModuleHandleW(nullptr);
            HHOOK keys = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard, module, 0);
            HHOOK buttons = SetWindowsHookExW(WH_MOUSE_LL, mouse, module, 0);
            alive_ = keys && buttons;
            SetEvent(ready_);
            if (alive_) while (GetMessageW(&message, nullptr, 0, 0) > 0) {}
            alive_ = false;
            if (keys) UnhookWindowsHookEx(keys);
            if (buttons) UnhookWindowsHookEx(buttons);
        });
        require(WaitForSingleObject(ready_, 2000) == WAIT_OBJECT_0, "Input monitor startup timed out");
        tick();
    }
    uint64_t tick() const { require(alive_, "Input monitor stopped; restart application"); return count_; }
    ~Activity() {
        if (thread_.joinable()) {
            if (thread_id_) PostThreadMessageW(thread_id_, WM_QUIT, 0, 0);
            thread_.join();
        }
        if (ready_) CloseHandle(ready_);
    }
};
inline DWORD_PTR message(HWND hwnd, UINT code, WPARAM wparam, LPARAM lparam, UINT timeout = 500) {
    DWORD_PTR result = 0;
    require(SendMessageTimeoutW(hwnd, code, wparam, lparam, SMTO_BLOCK | SMTO_ABORTIFHUNG | SMTO_ERRORONEXIT, timeout, &result) != 0,
            "Control did not respond; edit will not be retried");
    return result;
}
class ImeSession {
    Identity target_;
    HWND hwnd_ = nullptr;
    bool original_ = false, changed_ = false;
    // IMC_GETOPENSTATUS / IMC_SETOPENSTATUS (not declared by every SDK).
    bool is_open() { return message(hwnd_, WM_IME_CONTROL, 5, 0) != 0; }
    void set_open(bool open) {
        message(hwnd_, WM_IME_CONTROL, 6, open);
        auto deadline = Clock::now() + std::chrono::milliseconds(500);
        while (is_open() != open) {
            require(identity() == target_ && Clock::now() < deadline, "IME switch could not be confirmed");
            Sleep(10);
        }
    }
public:
    explicit ImeSession(const Identity& target) : target_(target), hwnd_(ImmGetDefaultIMEWnd(target.focus)) {
        require(hwnd_ != nullptr, "Cannot determine input IME state; text was not sent");
        original_ = is_open();
    }
    void disable() {
        require(identity() == target_, "Focus changed before IME switch");
        if (original_) { changed_ = true; set_open(false); }
        require(!is_open(), "IME is still open; text was not sent");
    }
    void finish() {
        if (!changed_) return;
        changed_ = false;
        require(identity() == target_, "Focus changed; original input IME may remain disabled");
        if (!is_open()) set_open(true);
    }
    ~ImeSession() { try { finish(); } catch (...) {} }
};
inline std::vector<INPUT> unicode_events(const std::wstring& text) {
    require(text.find(L'\0') == std::wstring::npos, "NUL in replacement");
    utf8(text); // reject malformed surrogate pairs before changing selection
    std::vector<INPUT> result;
    result.reserve(text.size() * 2);
    for (auto ch : normalize(text)) {
        require(ch != L'\t', "Tabs are not supported by UIA input");
        INPUT down{};
        down.type = INPUT_KEYBOARD;
        if (ch == L'\n') down.ki.wVk = VK_RETURN;
        else { down.ki.wScan = WORD(ch); down.ki.dwFlags = KEYEVENTF_UNICODE; }
        auto up = down;
        up.ki.dwFlags |= KEYEVENTF_KEYUP;
        result.push_back(down);
        result.push_back(up);
    }
    return result;
}
class Editor {
protected:
    Identity target_;
    const Config& config_;
    Activity& activity_;
public:
    Editor(const Identity& target, const Config& config, Activity& activity) : target_(target), config_(config), activity_(activity) {}
    virtual ~Editor() = default;
    virtual const char* kind() const = 0;
    virtual State read() = 0;
    virtual void replace(const State&, size_t, size_t, const std::wstring&, uint64_t) = 0;
    virtual void restore(const State&, size_t, uint64_t) = 0;
    virtual void finish() {}
    virtual void focus() { require(identity() == target_, "Focused input changed"); }
    void guard(uint64_t tick) { focus(); require(activity_.tick() == tick, "Editing input occurred; cancelled"); }
    virtual State wait(const State& expected, Deadline deadline) {
        Stage timing("readback");
        size_t attempts = 0;
        while (true) {
            auto actual = read();
            if (actual == expected) return actual;
            require(Clock::now() < deadline, "Cannot confirm edit; it will not be retried");
            Sleep(++attempts <= 2 ? 2 : attempts <= 5 ? 5 : 10);
        }
    }
};
class Win32Editor : public Editor {
    bool rich_;
public:
    Win32Editor(const Identity& target, const Config& config, Activity& activity) : Editor(target, config, activity), rich_(starts(window_class(target.focus), L"richedit")) {
        require(standard(target.focus) && IsWindowUnicode(target.focus), "Input is not a Unicode Edit/RichEdit");
        writable();
    }
    const char* kind() const override { return "win32"; }
    void writable() {
        require(IsWindowEnabled(target_.focus) && !(GetWindowLongPtrW(target_.focus, GWL_STYLE) & (ES_READONLY | ES_PASSWORD)),
                "Read-only, disabled or password input");
    }
    DWORD_PTR msg(UINT code, WPARAM wparam = 0, LPARAM lparam = 0) {
        return message(target_.focus, code, wparam, lparam, UINT(config_.timeout_ms));
    }
    State read() override {
        Stage timing("win32_read");
        auto length = msg(WM_GETTEXTLENGTH);
        require(length <= config_.limit * 2, "Document too large");
        std::vector<wchar_t> buffer(length + 1);
        msg(WM_GETTEXT, buffer.size(), reinterpret_cast<LPARAM>(buffer.data()));
        require(msg(WM_GETTEXTLENGTH) == length, "Document changed while reading");
        std::wstring text(buffer.data());
        if (rich_) text = normalize(text, L'\r');
        require(points(text) <= config_.limit, "Document too large");
        DWORD start = 0, end = 0;
        msg(EM_GETSEL, reinterpret_cast<WPARAM>(&start), reinterpret_cast<LPARAM>(&end));
        return {text, point_offset(text, start), point_offset(text, end)};
    }
    void select(const State& state, size_t start, size_t end) {
        msg(EM_SETSEL, unit_offset(state.text, start), LPARAM(unit_offset(state.text, end)));
    }
    void replace(const State& state, size_t start, size_t end, const std::wstring& result, uint64_t tick) override {
        Stage timing("win32_replace");
        guard(tick); writable(); input_ready(target_.focus);
        require(read() == state, "Document or caret changed before replacement");
        select(state, start, end);
        require(read() == State{state.text, start, end}, "Cannot confirm selected range");
        input_ready(target_.focus); guard(tick); writable();
        msg(EM_REPLACESEL, TRUE, reinterpret_cast<LPARAM>(result.c_str()));
    }
    void restore(const State& state, size_t caret, uint64_t tick) override { guard(tick); select(state, caret, caret); }
    State wait(const State& expected, Deadline) override {
        Stage timing("win32_readback");
        focus();
        auto actual = read();
        require(actual == expected, "Cannot confirm edit; it will not be retried");
        return actual;
    }
};
} // namespace kana
