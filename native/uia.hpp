#pragma once
#include "platform.hpp"
#include <uiautomation.h>
#include <utility>

namespace kana {
// Arrow keys have provider-dependent character/grapheme semantics. Restrict
// batching to a short unchanged suffix of ordinary, non-combining punctuation.
inline size_t punctuation_steps(const State& state, size_t end, size_t inserted, size_t caret) {
    if (caret <= inserted || caret - inserted > 16) return 0;
    auto count = caret - inserted;
    if (end > points(state.text) || count > points(state.text) - end) return 0;
    auto suffix = state.text.substr(unit_offset(state.text, end), count);
    const std::wstring allowed = L"。、，．！？!?.,:;";
    for (auto ch : suffix) if (allowed.find(ch) == std::wstring::npos) return 0;
    return suffix.size() == count ? count : 0;
}
inline void append_right_keys(std::vector<INPUT>& events, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        INPUT down{}; down.type = INPUT_KEYBOARD; down.ki.wVk = VK_RIGHT;
        auto up = down; up.ki.dwFlags = KEYEVENTF_KEYUP;
        events.push_back(down); events.push_back(up);
    }
}
inline bool simple_caret_text(const std::wstring& text) {
    // Be conservative about graphemes, variation selectors and bidi. In
    // particular, punctuation followed by a combining mark is not one step.
    return std::all_of(text.begin(), text.end(), [](wchar_t ch) {
        return (ch >= 0x20 && ch <= 0x7e) || ch == L'\n' || ch == L'\r' || ch == L'\t' ||
               (ch >= 0x3041 && ch <= 0x3096) || (ch >= 0x30a1 && ch <= 0x30fc) ||
               (ch >= 0x3400 && ch <= 0x9fff) || ch == 0x3000 || ch == 0x3001 || ch == 0x3002 ||
               ch == 0xff01 || ch == 0xff1f || ch == 0xff0c || ch == 0xff0e;
    });
}
template<class T> class Com {
    T* value_ = nullptr;
public:
    Com() = default;
    explicit Com(T* value) : value_(value) {}
    Com(const Com& rhs) : value_(rhs.value_) { if (value_) value_->AddRef(); }
    Com(Com&& rhs) noexcept : value_(std::exchange(rhs.value_, nullptr)) {}
    Com& operator=(Com rhs) noexcept { std::swap(value_, rhs.value_); return *this; }
    ~Com() { if (value_) value_->Release(); }
    T* get() const { return value_; }
    T* operator->() const { return value_; }
    explicit operator bool() const { return value_ != nullptr; }
    T** put() { if (value_) value_->Release(); value_ = nullptr; return &value_; }
};
template<class T> Com<T> pattern(IUIAutomationElement* element, PATTERNID id) {
    Com<T> result;
    HRESULT status = element->GetCurrentPatternAs(id, __uuidof(T), reinterpret_cast<void**>(result.put()));
    if (FAILED(status)) return {};
    return result;
}
struct Variant {
    VARIANT value;
    Variant() { VariantInit(&value); }
    ~Variant() { VariantClear(&value); }
};
inline std::wstring bstr(BSTR value) {
    std::wstring result(value ? value : L"", value ? SysStringLen(value) : 0);
    SysFreeString(value);
    return result;
}
struct Pending : std::runtime_error { using std::runtime_error::runtime_error; };
struct EventSignal {
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    ~EventSignal() { if (event) CloseHandle(event); }
};
class TextChanged final : public IUIAutomationEventHandler {
    std::atomic<ULONG> refs_{1};
    std::shared_ptr<EventSignal> signal_;
public:
    explicit TextChanged(std::shared_ptr<EventSignal> signal) : signal_(std::move(signal)) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** object) override {
        if (!object) return E_POINTER;
        *object = nullptr;
        if (iid == __uuidof(IUnknown) || iid == __uuidof(IUIAutomationEventHandler)) {
            *object = static_cast<IUIAutomationEventHandler*>(this); AddRef(); return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { auto count = --refs_; if (!count) delete this; return count; }
    HRESULT STDMETHODCALLTYPE HandleAutomationEvent(IUIAutomationElement*, EVENTID) override {
        SetEvent(signal_->event); return S_OK;
    }
};
class Automation {
public:
    Com<IUIAutomation> client;
    Com<IUIAutomationCacheRequest> safety_cache, metadata_cache;
    Automation() {
        hr(CoCreateInstance(CLSID_CUIAutomation, nullptr, CLSCTX_INPROC_SERVER, __uuidof(IUIAutomation),
                            reinterpret_cast<void**>(client.put())), "Cannot initialize UI Automation");
        safety_cache = cache({UIA_IsPasswordPropertyId, UIA_IsEnabledPropertyId, UIA_HasKeyboardFocusPropertyId});
        metadata_cache = cache({UIA_FrameworkIdPropertyId, UIA_ClassNamePropertyId});
    }
    Com<IUIAutomationCacheRequest> cache(std::initializer_list<PROPERTYID> ids) {
        Com<IUIAutomationCacheRequest> result;
        hr(client->CreateCacheRequest(result.put()), "Cannot create UIA cache request");
        hr(result->put_TreeScope(TreeScope_Element), "Cannot set UIA cache scope");
        for (auto id : ids) hr(result->AddProperty(id), "Cannot define UIA cached property");
        return result;
    }
    std::vector<bool> safety(IUIAutomationElement* element) {
        Com<IUIAutomationElement> cached;
        bool batched = SUCCEEDED(element->BuildUpdatedCache(safety_cache.get(), cached.put())) && bool(cached);
        auto target = batched ? cached.get() : element;
        BOOL password = FALSE, enabled = FALSE, focus = FALSE;
        if (batched) {
            hr(target->get_CachedIsPassword(&password), "Cannot read password state");
            hr(target->get_CachedIsEnabled(&enabled), "Cannot read enabled state");
            hr(target->get_CachedHasKeyboardFocus(&focus), "Cannot read focus state");
        } else {
            hr(target->get_CurrentIsPassword(&password), "Cannot read password state");
            hr(target->get_CurrentIsEnabled(&enabled), "Cannot read enabled state");
            hr(target->get_CurrentHasKeyboardFocus(&focus), "Cannot read focus state");
        }
        return {bool(password), bool(enabled), bool(focus)};
    }
    std::pair<std::wstring, std::wstring> metadata(IUIAutomationElement* element) {
        Com<IUIAutomationElement> cached;
        bool batched = SUCCEEDED(element->BuildUpdatedCache(metadata_cache.get(), cached.put())) && bool(cached);
        auto target = batched ? cached.get() : element;
        BSTR framework = nullptr, name = nullptr;
        HRESULT status = batched ? target->get_CachedFrameworkId(&framework) : target->get_CurrentFrameworkId(&framework);
        auto framework_text = bstr(framework);
        hr(status, "Cannot read UIA framework");
        status = batched ? target->get_CachedClassName(&name) : target->get_CurrentClassName(&name);
        auto class_text = bstr(name);
        hr(status, "Cannot read UIA class");
        std::transform(framework_text.begin(), framework_text.end(), framework_text.begin(), [](wchar_t ch) { return wchar_t(towlower(ch)); });
        return {framework_text, class_text};
    }
};
class UiaEditor : public Editor {
    Automation& automation_;
    Com<IUIAutomationElement> focused_, element_;
    Com<IUIAutomationTextPattern> pattern_;
    Com<IUIAutomationTextPattern2> caret_pattern_;
    bool caret_checked_ = false, chromium_ = false;
    size_t queued_right_ = 0;
    std::wstring focused_class_;
    Com<IUIAutomationTextRange> anchor_;
    State anchor_state_;
    std::unique_ptr<ImeSession> ime_;
    std::shared_ptr<EventSignal> signal_;
    Com<IUIAutomationEventHandler> handler_;
    bool subscribed_ = false;

    Com<IUIAutomationTextRange> document() {
        Com<IUIAutomationTextRange> range;
        hr(pattern_->get_DocumentRange(range.put()), "Cannot obtain document range");
        require(bool(range), "No document range");
        return range;
    }
    static Com<IUIAutomationTextRange> clone(IUIAutomationTextRange* range) {
        Com<IUIAutomationTextRange> result;
        hr(range->Clone(result.put()), "Cannot clone text range");
        return result;
    }
    static void endpoint(IUIAutomationTextRange* range, TextPatternRangeEndpoint end,
                         IUIAutomationTextRange* other, TextPatternRangeEndpoint other_end) {
        hr(range->MoveEndpointByRange(end, other, other_end), "Cannot position text range");
    }
    static bool collapsed(IUIAutomationTextRange* range) {
        int comparison = 0;
        hr(range->CompareEndpoints(TextPatternRangeEndpoint_Start, range, TextPatternRangeEndpoint_End, &comparison),
           "Cannot inspect range boundaries");
        return comparison == 0;
    }
    static int move(IUIAutomationTextRange* range, TextPatternRangeEndpoint end, int count) {
        int moved = 0;
        hr(range->MoveEndpointByUnit(end, TextUnit_Character, count, &moved), "Cannot move text range");
        return moved;
    }
    Com<IUIAutomationTextRange> selection(bool* is_collapsed = nullptr) {
        Com<IUIAutomationTextRangeArray> ranges;
        hr(pattern_->GetSelection(ranges.put()), "Cannot obtain selection");
        int count = 0;
        if (ranges) hr(ranges->get_Length(&count), "Cannot obtain selection count");
        require(count <= 1, "Multiple selections are not supported");
        Com<IUIAutomationTextRange> selected;
        if (count == 1) hr(ranges->GetElement(0, selected.put()), "Cannot obtain selected range");
        bool empty = !selected || collapsed(selected.get());
        if (is_collapsed) *is_collapsed = empty;
        if (selected && !empty) return selected;
        if (!caret_checked_) {
            caret_pattern_ = pattern<IUIAutomationTextPattern2>(element_.get(), UIA_TextPattern2Id);
            caret_checked_ = true;
        }
        if (caret_pattern_) {
            BOOL active = FALSE;
            Com<IUIAutomationTextRange> caret;
            if (SUCCEEDED(caret_pattern_->GetCaretRange(&active, caret.put())) && active && caret) {
                require(collapsed(caret.get()), "Caret range is not collapsed");
                return caret;
            }
        }
        require(bool(selected), "Cannot obtain caret or selection");
        return selected;
    }
    State read_once(Com<IUIAutomationTextRange> doc = {}, std::wstring text = {}) {
        // A wait probe supplies the document just read after a fresh focus
        // check. Reuse that observation, but always reread at the end.
        if (!doc) { focus(); doc = document(); text = get_text(doc.get()); }
        if (focused_class_ == L"native-edit-context" && !text.empty()) {
            BSTR name = nullptr;
            hr(focused_->get_CurrentName(&name), "Cannot read editor accessibility label");
            require(text != normalize(bstr(name)), "VS Code/Monaco is exposing a label; enable editor.accessibilitySupport");
        }
        bool empty = false;
        auto selected = selection(&empty);
        auto prefix = clone(doc.get());
        endpoint(prefix.get(), TextPatternRangeEndpoint_End, selected.get(), TextPatternRangeEndpoint_Start);
        auto left = get_text(prefix.get());
        std::wstring through = left, contents;
        if (!empty) {
            endpoint(prefix.get(), TextPatternRangeEndpoint_End, selected.get(), TextPatternRangeEndpoint_End);
            through = get_text(prefix.get());
            contents = get_text(selected.get());
        }
        if (!starts(text, left) || !starts(text, through) || through.size() < left.size() ||
            text.substr(left.size(), through.size() - left.size()) != contents)
            throw Pending("Document and selection are updating");
        if (get_text(document().get()) != text) throw Pending("Document changed while reading");
        focus();
        State state{text, points(left), points(through)};
        anchor_ = selected;
        anchor_state_ = state;
        return state;
    }
    bool matches(IUIAutomationTextRange* doc, IUIAutomationTextRange* range, const State& state, size_t start, size_t end) {
        auto a = unit_offset(state.text, start), b = unit_offset(state.text, end);
        if (get_text(range) != state.text.substr(a, b - a)) return false;
        auto prefix = clone(doc);
        endpoint(prefix.get(), TextPatternRangeEndpoint_End, range, TextPatternRangeEndpoint_Start);
        if (get_text(prefix.get()) != state.text.substr(0, a)) return false;
        endpoint(prefix.get(), TextPatternRangeEndpoint_End, range, TextPatternRangeEndpoint_End);
        if (get_text(prefix.get()) != state.text.substr(0, b)) return false;
        require(get_text(document().get()) == state.text, "Document changed during range construction");
        return true;
    }
    Com<IUIAutomationTextRange> point(IUIAutomationTextRange* doc, const State& state, size_t offset) {
        auto prefix = clone(doc);
        endpoint(prefix.get(), TextPatternRangeEndpoint_End, doc, TextPatternRangeEndpoint_Start);
        if (offset) move(prefix.get(), TextPatternRangeEndpoint_End, int(offset));
        auto expected = state.text.substr(0, unit_offset(state.text, offset));
        for (int attempt = 0; attempt < 8; ++attempt) {
            auto actual = get_text(prefix.get());
            if (actual == expected) {
                endpoint(prefix.get(), TextPatternRangeEndpoint_Start, prefix.get(), TextPatternRangeEndpoint_End);
                return prefix;
            }
            auto delta = int(offset) - int(points(actual));
            if (!delta || !move(prefix.get(), TextPatternRangeEndpoint_End, delta)) break;
        }
        throw std::runtime_error("Cannot map UIA character units to document position");
    }
    Com<IUIAutomationTextRange> range_for(const State& state, size_t start, size_t end) {
        Stage timing("uia_range");
        auto doc = document();
        require(get_text(doc.get()) == state.text, "Document changed during range construction");
        auto nearby = clone(anchor_ && anchor_state_ == state ? anchor_.get() : selection().get());
        if (start != state.start || end != state.end) {
            endpoint(nearby.get(), TextPatternRangeEndpoint_Start, nearby.get(), TextPatternRangeEndpoint_End);
            if (end != state.end) {
                move(nearby.get(), TextPatternRangeEndpoint_End, int(end) - int(state.end));
                endpoint(nearby.get(), TextPatternRangeEndpoint_Start, nearby.get(), TextPatternRangeEndpoint_End);
            }
            if (start != end) move(nearby.get(), TextPatternRangeEndpoint_Start, int(start) - int(end));
        }
        if (matches(doc.get(), nearby.get(), state, start, end)) return nearby;
        auto begin = point(doc.get(), state, start), finish = point(doc.get(), state, end);
        endpoint(begin.get(), TextPatternRangeEndpoint_End, finish.get(), TextPatternRangeEndpoint_End);
        if (matches(doc.get(), begin.get(), state, start, end)) return begin;
        std::vector<Com<IUIAutomationTextRange>> anchors{finish};
        if (end == state.end) {
            auto live = clone(selection().get());
            endpoint(live.get(), TextPatternRangeEndpoint_Start, live.get(), TextPatternRangeEndpoint_End);
            anchors.push_back(live);
        }
        for (auto& anchor : anchors) {
            auto candidate = clone(anchor.get());
            move(candidate.get(), TextPatternRangeEndpoint_Start, int(start) - int(end));
            for (int attempt = 0; attempt < 8; ++attempt) {
                if (matches(doc.get(), candidate.get(), state, start, end)) return candidate;
                auto prefix = clone(doc.get());
                endpoint(prefix.get(), TextPatternRangeEndpoint_End, candidate.get(), TextPatternRangeEndpoint_Start);
                auto left = get_text(prefix.get());
                if (!starts(state.text, left)) break;
                auto delta = int(start) - int(points(left));
                if (!delta || !move(candidate.get(), TextPatternRangeEndpoint_Start, delta)) break;
            }
        }
        throw std::runtime_error("Cannot verify replacement range; text was not sent");
    }
    void pause(DWORD milliseconds) {
        poll_pause(milliseconds, subscribed_ ? signal_->event : nullptr);
    }
public:
    UiaEditor(const Identity& target, const Config& config, Activity& activity, Automation& automation,
              Com<IUIAutomationElement> focused, Com<IUIAutomationElement> element, Com<IUIAutomationTextPattern> text_pattern,
              const std::vector<bool>& initial_safety)
        : Editor(target, config, activity), automation_(automation), focused_(std::move(focused)),
          element_(std::move(element)), pattern_(std::move(text_pattern)) {
        auto metadata = automation_.metadata(focused_.get());
        auto provider = element_.get() == focused_.get() ? metadata : automation_.metadata(element_.get());
        focused_class_ = metadata.second;
        chromium_ = metadata.first == L"chrome" || metadata.first == L"chromium" || provider.first == L"chrome" ||
                    provider.first == L"chromium" || starts(window_class(target.focus), L"chrome_");
        writable(&initial_safety);
        if (config.wait == "event") {
            signal_ = std::make_shared<EventSignal>();
            require(signal_->event != nullptr, "Cannot create UIA notification event");
            handler_ = Com<IUIAutomationEventHandler>(new TextChanged(signal_));
            subscribed_ = SUCCEEDED(automation_.client->AddAutomationEventHandler(
                UIA_Text_TextChangedEventId, element_.get(), TreeScope_Element, nullptr, handler_.get()));
        }
    }
    ~UiaEditor() override {
        if (subscribed_) automation_.client->RemoveAutomationEventHandler(UIA_Text_TextChangedEventId, element_.get(), handler_.get());
        try { finish(); } catch (...) {}
    }
    const char* kind() const override { return "uia"; }
    void focus() override {
        Editor::focus();
        Com<IUIAutomationElement> current;
        hr(automation_.client->GetFocusedElement(current.put()), "Cannot obtain focused element");
        BOOL same = FALSE;
        require(bool(current), "No focused element");
        hr(automation_.client->CompareElements(current.get(), focused_.get(), &same), "Cannot verify focused element");
        require(same, "UIA input focus changed");
    }
    void writable(const std::vector<bool>* initial = nullptr) {
        auto safety = initial ? *initial : automation_.safety(focused_.get());
        require(!safety[0] && safety[1] && safety[2], "Password, disabled or unfocused input");
        auto value = pattern<IUIAutomationValuePattern>(element_.get(), UIA_ValuePatternId);
        if (value) {
            BOOL readonly = FALSE;
            hr(value->get_CurrentIsReadOnly(&readonly), "Cannot determine read-only state");
            require(!readonly, "Read-only input");
        }
        Variant readonly;
        hr(document()->GetAttributeValue(UIA_IsReadOnlyAttributeId, &readonly.value), "Cannot determine text read-only state");
        if (readonly.value.vt == VT_BOOL) require(!readonly.value.boolVal, "Read-only input");
        if (readonly.value.vt == VT_I4) require(!readonly.value.lVal, "Read-only input");
        if (config_.ime == "off" || (config_.ime == "auto" && chromium_)) return;
        auto composition = pattern<IUIAutomationTextEditPattern>(element_.get(), UIA_TextEditPatternId);
        if (composition) {
            Com<IUIAutomationTextRange> active;
            hr(composition->GetActiveComposition(active.put()), "Cannot inspect IME composition");
            require(!active || collapsed(active.get()) || get_text(active.get()).empty(), "Commit IME composition first");
        }
    }
    std::wstring get_text(IUIAutomationTextRange* range) {
        BSTR value = nullptr;
        auto status = range->GetText(int(config_.limit * 2 + 1), &value);
        bool absent = value == nullptr;
        auto text = normalize(bstr(value));
        hr(status, "Cannot obtain UIA text");
        // GetText already returns the range's content. Check endpoints only
        // for a null BSTR: this distinguishes a legitimate empty range from
        // a provider update without an extra COM round trip on every read.
        if (absent) {
            if (collapsed(range)) return {};
            throw Pending("Nonempty range returned no text");
        }
        require(points(text) <= config_.limit, "Document too large");
        return text;
    }
    State read() override {
        Stage timing("uia_read");
        auto deadline = Clock::now() + std::chrono::milliseconds(500);
        while (true) {
            try { return read_once(); }
            catch (const Pending&) {
                focus();
                require(Clock::now() < deadline, "UIA document and range did not stabilize");
                pause(10);
            }
        }
    }
    size_t replace_positioned(const State& state, size_t start, size_t end,
                              const std::wstring& result, uint64_t tick, size_t caret) override {
        auto inserted = start + points(result);
        CONTROLTYPEID type = 0;
        // Limit the shortcut to Chromium Edit controls. Documents/Monaco,
        // bidirectional text and arbitrary character navigation retain Select.
        auto steps = punctuation_steps(state, end, inserted, caret);
        bool simple_direction = simple_caret_text(state.text) && simple_caret_text(result);
        if (steps && chromium_ && simple_direction &&
            SUCCEEDED(element_->get_CurrentControlType(&type)) && type == UIA_EditControlTypeId) {
            Variant direction;
            if (SUCCEEDED(document()->GetAttributeValue(UIA_TextFlowDirectionsAttributeId, &direction.value)) &&
                direction.value.vt == VT_I4 && direction.value.lVal == 0) queued_right_ = steps;
        }
        auto positioned = inserted + queued_right_;
        try { replace(state, start, end, result, tick); }
        catch (...) { queued_right_ = 0; throw; }
        queued_right_ = 0;
        return positioned;
    }
    void replace(const State& state, size_t start, size_t end, const std::wstring& result, uint64_t tick) override {
        Stage timing("uia_replace");
        auto events = unicode_events(result);
        if (queued_right_) {
            Stage navigation("uia_caret_batch");
            append_right_keys(events, queued_right_);
        }
        {
            Stage preparation("uia_prepare");
            guard(tick); writable(); input_ready(target_.focus);
            require(read() == state, "Document or selection changed before replacement");
        }
        auto selected = range_for(state, start, end);
        guard(tick);
        State wanted{state.text, start, end};
        {
            Stage selection_timing("uia_select");
            hr(selected->Select(), "Cannot select replacement range");
            auto deadline = Clock::now() + std::chrono::milliseconds(std::min<uint64_t>(config_.timeout_ms, 2000));
            while (true) {
                guard(tick);
                auto actual = read();
                if (actual == wanted) break;
                require(actual.text == state.text && Clock::now() < deadline, "Selection did not stabilize; text was not sent");
                pause(10);
            }
        }
        guard(tick);
        require(get_text(document().get()) == state.text, "Document changed before input");
        input_ready(target_.focus); guard(tick);
        {
            Stage ime_timing("ime_disable");
            ime_ = std::make_unique<ImeSession>(target_);
            ime_->disable();
            require(read() == wanted && activity_.tick() == tick, "State changed during IME switch; text was not sent");
        }
        // One batch; never delete first, retry partial input, or fall back.
        Stage send_timing("send_input");
        require(SendInput(UINT(events.size()), events.data(), sizeof(INPUT)) == events.size(),
                "Input was only partially accepted; it will not be retried");
    }
    State wait(const State& expected, Deadline deadline) override {
        Stage timing("uia_readback");
        if (config_.initial_delay_ms) { focus(); pause(DWORD(config_.initial_delay_ms)); }
        size_t probes = 0;
        while (true) {
            focus();
            try {
                auto doc = document();
                auto text = get_text(doc.get());
                if (text == expected.text) {
                    auto actual = read_once(std::move(doc), std::move(text));
                    if (actual == expected) return actual;
                }
            } catch (const Pending&) {}
            auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - Clock::now()).count();
            require(remaining > 0, "Cannot confirm input; edit will not be retried");
            auto delay = ++probes <= 2 ? 2 : probes <= 5 ? 5 : 10;
            pause(DWORD(std::min<int64_t>(delay, remaining)));
        }
    }
    void restore(const State& state, size_t caret, uint64_t tick) override {
        guard(tick);
        auto range = range_for(state, caret, caret);
        guard(tick);
        hr(range->Select(), "Cannot restore caret");
    }
    void finish() override {
        if (ime_) {
            Stage timing("ime_restore");
            auto session = std::move(ime_);
            session->finish();
        }
    }
};
inline std::unique_ptr<Editor> focused_editor(const Identity& target, const Config& config, Activity& activity, Automation* automation) {
    if ((config.backend == "auto" || config.backend == "win32") && standard(target.focus))
        return std::make_unique<Win32Editor>(target, config, activity);
    require(config.backend != "win32", "Input is not a standard Edit/RichEdit");
    require(automation != nullptr, "UI Automation unavailable");
    Com<IUIAutomationElement> focused;
    hr(automation->client->GetFocusedElement(focused.put()), "Cannot obtain focused UIA input");
    require(bool(focused), "No focused UIA input");
    auto safety = automation->safety(focused.get());
    require(!safety[0] && safety[1] && safety[2], "Password, disabled or unfocused input");
    auto element = focused;
    Com<IUIAutomationTreeWalker> walker;
    for (int depth = 0; depth < 8 && element; ++depth) {
        auto text = pattern<IUIAutomationTextPattern>(element.get(), UIA_TextPatternId);
        if (text) return std::make_unique<UiaEditor>(target, config, activity, *automation, focused, element, text, safety);
        if (!walker) hr(automation->client->get_ControlViewWalker(walker.put()), "Cannot obtain UIA tree walker");
        Com<IUIAutomationElement> parent;
        hr(walker->GetParentElement(element.get(), parent.put()), "Cannot obtain input ancestor");
        element = parent;
    }
    throw std::runtime_error("Input does not expose UIA TextPattern");
}
} // namespace kana
