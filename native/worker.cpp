#include "uia.hpp"
#include <fcntl.h>
#include <io.h>
#include <condition_variable>
#include <mutex>

using namespace kana;

namespace {
class Worker {
    Config config_;
    bool initialized_ = false;
    Activity activity_;
    std::unique_ptr<Automation> automation_;
    std::unique_ptr<Editor> editor_;
    State saved_;
    Identity target_;
    std::vector<int> keys_;
    uint64_t serial_ = 0, token_ = 0;
public:
    void warmup() {
        Stage timing("warmup");
        activity_.start();
        if (config_.backend != "win32" && !automation_) {
            try { automation_ = std::make_unique<Automation>(); }
            catch (...) { if (config_.backend == "uia") throw; }
        }
    }
    Writer dispatch(uint64_t command, Reader& reader) {
        Writer result;
        if (command == 1) {
            require(!initialized_, "Worker already initialized");
            config_.backend = reader.text(); config_.ime = reader.text();
            config_.timeout_ms = reader.number(); config_.limit = reader.number();
            config_.initial_delay_ms = reader.number(); config_.timing = reader.number() != 0;
            config_.wait = reader.text();
            reader.done();
            require(config_.backend == "auto" || config_.backend == "uia" || config_.backend == "win32", "Unsupported native editor backend");
            require(config_.ime == "auto" || config_.ime == "strict" || config_.ime == "off", "Invalid IME check mode");
            require(config_.wait == "poll" || config_.wait == "event", "Invalid UIA wait mode");
            require(config_.timeout_ms >= 1000 && config_.timeout_ms <= 30000 && config_.limit >= 1000 && config_.limit <= 1000000 && config_.initial_delay_ms <= 100,
                    "Invalid worker limits");
            measure = config_.timing;
            initialized_ = true;
            return result;
        }
        require(initialized_, "Worker is not initialized");
        if (command == 2) { reader.done(); warmup(); }
        else if (command == 3) {
            Stage timing("capture");
            // A failed new capture also invalidates the previous edit token.
            editor_.reset(); token_ = 0;
            auto count = reader.number();
            require(count <= 16, "Too many trigger keys");
            keys_.clear();
            for (uint64_t i = 0; i < count; ++i) {
                auto key = reader.number(); require(key >= 1 && key <= 255, "Invalid trigger key"); keys_.push_back(int(key));
            }
            reader.done();
            warmup();
            target_ = identity();
            input_ready(target_.focus, true);
            auto tick = activity_.tick();
            editor_ = focused_editor(target_, config_, activity_, automation_.get());
            saved_ = editor_->read();
            if (activity_.tick() != tick) require(editor_->read() == saved_, "Input changed during capture");
            editor_->focus();
            token_ = ++serial_;
            result.number(token_);
            result.number(reinterpret_cast<uintptr_t>(target_.foreground));
            result.number(reinterpret_cast<uintptr_t>(target_.focus));
            result.number(target_.pid);
            result.number(tick);
            result.text(saved_.text); result.number(saved_.start); result.number(saved_.end);
        } else if (command == 4) {
            Stage timing("apply");
            auto token = reader.number(), start = reader.number(), end = reader.number(), caret = reader.number();
            auto text = wide(reader.text());
            reader.done();
            require(editor_ && token && token == token_, "Invalid or consumed edit token");
            auto editor = std::move(editor_);
            token_ = 0; // consumed before validation or any mutation
            auto total = points(saved_.text);
            require(start <= end && end <= total && !text.empty() && text.find(L'\0') == std::wstring::npos, "Invalid replacement");
            auto a = unit_offset(saved_.text, start), b = unit_offset(saved_.text, end);
            auto expected = saved_.text.substr(0, a) + text + saved_.text.substr(b);
            require(points(expected) <= config_.limit && caret <= points(expected), "Replacement exceeds document limit");
            auto inserted = start + points(text);
            try {
                release_keys(target_, keys_);
                editor->focus();
                auto tick = activity_.tick();
                editor->replace(saved_, start, end, text, tick);
                auto updated = editor->wait({expected, inserted, inserted},
                    Clock::now() + std::chrono::milliseconds(std::min<uint64_t>(config_.timeout_ms, 2000)));
                auto after_tick = activity_.tick();
                if (caret != inserted) {
                    Stage timing_restore("caret_restore");
                    editor->guard(after_tick);
                    editor->restore(updated, caret, after_tick);
                    auto deadline = Clock::now() + std::chrono::milliseconds(std::min<uint64_t>(config_.timeout_ms, 2000));
                    // Keep the existing 10 ms caret-confirmation backoff.
                    while (true) {
                        editor->guard(after_tick);
                        auto state = editor->read();
                        if (state == State{expected, caret, caret}) break;
                        editor->guard(after_tick);
                        require(Clock::now() < deadline, "Text replaced but caret restoration could not be confirmed");
                        Sleep(10);
                    }
                }
                editor->finish();
            } catch (const std::exception& error) {
                std::string original = error.what();
                try { editor->finish(); }
                catch (const std::exception& restoration) { original += std::string("; IME restoration: ") + restoration.what(); }
                throw std::runtime_error(original);
            }
        } else if (command == 5) {
            reader.done(); warmup();
            auto editor = focused_editor(identity(), config_, activity_, automation_.get());
            auto state = editor->read();
            result.text(std::string(editor->kind())); result.number(points(state.text)); result.number(state.start); result.number(state.end);
        } else if (command == 6) { reader.done(); editor_.reset(); token_ = 0; }
        else throw std::runtime_error("Unknown worker command");
        return result;
    }
};

// This fixture is invisible, owned by the self-test, and never receives
// SendInput or changes foreground focus. It runs its own message pump.
class Fixture {
    std::thread thread_;
    std::mutex mutex_;
    std::condition_variable ready_;
    HWND hwnd_ = nullptr;
    DWORD thread_id_ = 0;
    bool started_ = false;
public:
    explicit Fixture(bool rich) {
        thread_ = std::thread([this, rich] {
            HMODULE module = rich ? LoadLibraryW(L"Msftedit.dll") : nullptr;
            MSG msg{};
            PeekMessageW(&msg, nullptr, 0, 0, PM_NOREMOVE);
            HWND hwnd = CreateWindowExW(0, rich ? L"RICHEDIT50W" : L"EDIT", L"", ES_MULTILINE | ES_AUTOVSCROLL,
                                        0, 0, 640, 480, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
            {
                std::lock_guard<std::mutex> lock(mutex_);
                hwnd_ = hwnd; thread_id_ = GetCurrentThreadId(); started_ = true;
            }
            ready_.notify_one();
            if (hwnd) {
                while (GetMessageW(&msg, nullptr, 0, 0) > 0) { TranslateMessage(&msg); DispatchMessageW(&msg); }
                DestroyWindow(hwnd);
            }
            if (module) FreeLibrary(module);
        });
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [this] { return started_; });
    }
    HWND hwnd() const { return hwnd_; }
    ~Fixture() { if (thread_id_) PostThreadMessageW(thread_id_, WM_QUIT, 0, 0); thread_.join(); }
};
class FixtureUia : public UiaEditor {
public:
    using UiaEditor::UiaEditor;
    void focus() override {
        require(IsWindow(target_.focus) && !IsWindowVisible(target_.focus), "Self-test fixture is not hidden");
    }
};
class FixtureWin32 : public Win32Editor {
public:
    using Win32Editor::Win32Editor;
    void focus() override {
        require(IsWindow(target_.focus) && !IsWindowVisible(target_.focus), "Self-test fixture is not hidden");
    }
};
// Read-only benchmark entry point. It accepts only hidden Edit/RichEdit
// fixtures. It cannot select ranges, mutate text, disable IME or send keys.
void benchmark_read(HWND hwnd, const std::string& backend, int count) {
    require(hwnd && IsWindow(hwnd) && !IsWindowVisible(hwnd) && standard(hwnd), "Benchmark requires a hidden Edit/RichEdit fixture");
    require(count >= 1 && count <= 1000, "Invalid benchmark sample count");
    Identity target{nullptr, hwnd, 0};
    GetWindowThreadProcessId(hwnd, &target.pid);
    Config config;
    Activity activity; // read-only tests do not need hooks
    std::unique_ptr<Automation> automation;
    std::unique_ptr<Editor> editor;
    if (backend == "win32") editor = std::make_unique<FixtureWin32>(target, config, activity);
    else {
        require(backend == "uia", "Invalid benchmark backend");
        automation = std::make_unique<Automation>();
        Com<IUIAutomationElement> element;
        hr(automation->client->ElementFromHandle(hwnd, element.put()), "Cannot obtain benchmark fixture");
        auto safety = automation->safety(element.get());
        require(!safety[0] && safety[1], "Benchmark fixture is password or disabled");
        auto text_pattern = pattern<IUIAutomationTextPattern>(element.get(), UIA_TextPatternId);
        require(bool(text_pattern), "Benchmark fixture lacks TextPattern");
        editor = std::make_unique<FixtureUia>(target, config, activity, *automation, element, element, text_pattern, std::vector<bool>{false, true, true});
    }
    auto expected = editor->read();
    for (int i = 0; i < 2; ++i) require(editor->read() == expected, "Benchmark fixture changed");
    for (int i = 0; i < count; ++i) {
        auto start = Clock::now();
        auto state = editor->read();
        auto us = std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() - start).count();
        require(state == expected, "Benchmark fixture changed");
        std::cout << us << '\n';
    }
}
void self_test() {
    auto text = wide(u8"前😀。\r\n\r\nさんぽ。後ろ");
    require(utf8(text) == u8"前😀。\r\n\r\nさんぽ。後ろ", "UTF roundtrip");
    require(unit_offset(text, 2) == 3 && point_offset(text, 3) == 2, "Surrogate offset");
    bool rejected = false;
    try { point_offset(text, 2); } catch (...) { rejected = true; }
    require(rejected, "Split surrogate accepted");
    auto events = unicode_events(wide(u8"😀\n"));
    require(events.size() == 6 && events[4].ki.wVk == VK_RETURN && (events[3].ki.dwFlags & KEYEVENTF_KEYUP), "Unicode events");
    rejected = false;
    try { unicode_events(L"\t"); } catch (...) { rejected = true; }
    require(rejected, "Tab input accepted");
    Writer encoded; encoded.number(0x123456789abcdef0ULL); encoded.text(utf8(text));
    Reader decoded{encoded.data};
    require(decoded.number() == 0x123456789abcdef0ULL && decoded.text() == utf8(text), "Protocol roundtrip");
    decoded.done();
    Config config;
    Activity activity;
    activity.start();
    Automation automation;
    for (bool rich : {false, true}) {
        Fixture fixture(rich);
        require(fixture.hwnd() != nullptr, "Cannot create hidden fixture");
        Identity target{nullptr, fixture.hwnd(), GetCurrentProcessId()};
        FixtureWin32 editor(target, config, activity);
        editor.msg(WM_SETTEXT, 0, reinterpret_cast<LPARAM>(text.c_str()));
        auto state = editor.read();
        auto start_unit = state.text.find(wide(u8"さんぽ"));
        auto start = point_offset(state.text, start_unit);
        editor.select(state, start, start + 3);
        auto selected = editor.read();
        require(selected.start == start && selected.end == start + 3, "Multiline selection mapping");
        auto replacement = wide(u8"散歩");
        // Exercise the production replacement guards on our hidden control.
        editor.select(state, state.start, state.end);
        require(editor.read() == state, "Cannot reset fixture selection");
        editor.replace(state, start, start + 3, replacement, activity.tick());
        auto actual = editor.read();
        require(actual.text == state.text.substr(0, start_unit) + replacement + state.text.substr(start_unit + 3), "Cross-thread replacement");
        require(editor.msg(EM_UNDO) && editor.read().text == state.text, "Undo preservation");
        auto stale = state;
        stale.text += L"x";
        rejected = false;
        try { editor.replace(stale, start, start + 3, replacement, activity.tick()); } catch (...) { rejected = true; }
        require(rejected && editor.read().text == state.text, "Stale state was edited");
        editor.select(state, points(state.text), points(state.text));
        Com<IUIAutomationElement> element;
        hr(automation.client->ElementFromHandle(fixture.hwnd(), element.put()), "Cannot obtain fixture UIA element");
        auto safety = automation.safety(element.get());
        require(!safety[0] && safety[1] && !safety[2], "UIA fresh safety cache");
        if (rich) {
            auto text_pattern = pattern<IUIAutomationTextPattern>(element.get(), UIA_TextPatternId);
            require(bool(text_pattern), "RichEdit TextPattern unavailable");
            // Focus bypass belongs only to this private, hidden fixture class.
            FixtureUia uia(target, config, activity, automation, element, element, text_pattern, {false, true, true});
            auto uiastate = uia.read();
            require(uiastate.text == normalize(state.text), "UIA newline mapping");
            for (size_t caret : {size_t(0), size_t(2), points(uiastate.text)}) {
                uia.restore(uiastate, caret, activity.tick());
                uiastate = uia.read();
                require(uiastate.start == caret && uiastate.end == caret, "UIA caret range verification");
            }
            Config event_config = config;
            event_config.wait = "event";
            FixtureUia notified(target, event_config, activity, automation, element, element, text_pattern, {false, true, true});
            auto changed = normalize(text) + L"new";
            std::thread update([hwnd = fixture.hwnd(), changed] {
                Sleep(30);
                message(hwnd, WM_SETTEXT, 0, reinterpret_cast<LPARAM>(changed.c_str()));
            });
            try {
                auto confirmed = notified.wait({changed, 0, 0}, Clock::now() + std::chrono::seconds(2));
                require(confirmed.text == changed, "Notification readback failed");
            } catch (...) { update.join(); throw; }
            update.join();
        }
        SetWindowLongPtrW(fixture.hwnd(), GWL_STYLE, GetWindowLongPtrW(fixture.hwnd(), GWL_STYLE) | ES_READONLY);
        rejected = false;
        try { editor.writable(); } catch (...) { rejected = true; }
        require(rejected, "Read-only input accepted");
    }
    std::cout << "Native self-test passed (UTF-16, IPC, Edit/RichEdit, undo, UIA, read-only; no foreground input)\n";
}
} // namespace

int main(int argc, char** argv) {
    HRESULT apartment = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(apartment)) return 2;
    int status = 0;
    try {
        if (argc == 2 && std::string(argv[1]) == "--self-test") self_test();
        else if (argc == 5 && std::string(argv[1]) == "--benchmark-read")
            benchmark_read(reinterpret_cast<HWND>(uintptr_t(std::stoull(argv[2]))), argv[3], std::stoi(argv[4]));
        else {
            require(argc == 1, "Unsupported worker arguments");
            _setmode(_fileno(stdin), _O_BINARY); _setmode(_fileno(stdout), _O_BINARY);
            Worker worker;
            std::string frame;
            while (read_frame(frame)) {
                timings.clear();
                Reader reader{frame};
                auto command = reader.number();
                Writer payload;
                std::string error;
                try { payload = worker.dispatch(command, reader); }
                catch (const std::exception& exc) { error = exc.what(); }
                Writer response;
                response.number(error.empty() ? 1 : 0); response.text(error);
                response.number(timings.size());
                for (const auto& timing : timings) { response.text(timing.name); response.number(timing.us); }
                response.data += payload.data;
                write_frame(response);
                if (command == 6) break;
            }
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; status = 1;
    }
    CoUninitialize();
    return status;
}
