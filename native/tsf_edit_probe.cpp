// Experimental TSF commit-range editor. Separate DLL/protocol from read-only monitor.
#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <msctf.h>
#include <wrl/client.h>
#include <atomic>
#include <vector>
#include <string>
#include <algorithm>
#include <cstdio>

using Microsoft::WRL::ComPtr;
constexpr unsigned capacity = 64, text_limit = 1024;
constexpr unsigned document_limit = 200000;
struct Packet {
    DWORD kind, pid, tid, hr, key, active, modifiers;
    ULONGLONG tick, hwnd, generation, composition_tick;
    wchar_t text[text_limit];
    ULONGLONG commit_id, revision;
};
constexpr DWORD ModifierCtrl=1, ModifierAlt=2, ModifierShift=4, ModifierWin=8;
struct TextDiff { DWORD before,after,prefix,suffix,source_equal; };
struct Shared {
    volatile LONG write, read, dropped, stop, attached;
    ULONGLONG control_hwnd;
    Packet packets[capacity];
    volatile LONG edit_state; // 0 idle, 1 queued, 2 validating, 3 done, 4 canceled, 5 writing
    ULONGLONG request_id, commit_id, revision, deadline;
    DWORD result, mutated;
    DWORD diagnostic;
    TextDiff text_diff;
    wchar_t inserted_text[17];
    DWORD inserted_count;
    DWORD original_units;
    wchar_t expected[text_limit], replacement[text_limit];
};
enum Kind { Attached=1, Context=2, Editing=3, Candidate=4, EmptyEnd=5,
            Error=6, Detached=7, Key=8, Unsupported=9 };
// Low 16 bits: failed validation. High 16 bits: last range invalidation.
enum Diagnostic : DWORD { NoReason=0, Stopped=1, MissingRange=2, ContextChanged=3,
    CommitChanged=4, RevisionChanged=5, FocusChanged=6, Expired=7,
    ForegroundChanged=8, ActiveComposition=9, TextMismatch=10,
    KeyInvalidation=101, ContextInvalidation=102, EditInvalidation=103,
    SelectionInvalidation=104, StyleInvalidation=105, InspectionInvalidation=106,
    CompletedInvalidation=107, SnapshotInvalidation=108, TextInvalidation=109,
    CompositionInvalidation=110, NewCompositionInvalidation=111, RangeTextInvalidation=112,
    DocumentSnapshotInvalidation=113 };
const GUID composing_guid = {0xe12ac060,0xaf15,0x11d2,{0xaf,0xc5,0x00,0x10,0x5a,0x27,0x99,0xb5}};
HINSTANCE module_handle;
UINT control_message;
HANDLE host_mapping;
Shared* host_shared;
HHOOK host_hook;
DWORD host_tid;
HANDLE host_thread;
bool host_poisoned=false;
HANDLE host_edit_done;
volatile LONG host_edit_busy=0;

std::wstring mapping_name(DWORD pid) { return L"Local\\kana-tsf-edit-" + std::to_wstring(pid); }
std::wstring event_name(DWORD pid) { return mapping_name(pid)+L"-done"; }
UINT message_id() { return RegisterWindowMessageW(L"kana-tsf-edit-control-v1"); }

class Observer;
thread_local Observer* observer;
LRESULT CALLBACK control_window(HWND, UINT, WPARAM, LPARAM);

class Observer final : public ITfTextEditSink, public ITfThreadMgrEventSink, public ITfKeyTraceEventSink {
    std::atomic<ULONG> refs{1};
public:
    struct Busy {
        unsigned& count;
        explicit Busy(unsigned& value):count(value) { ++count; }
        ~Busy() { --count; }
    };
    HANDLE mapping=nullptr, owner=nullptr;
    Shared* shared=nullptr;
    HWND window=nullptr;
    ComPtr<ITfThreadMgr> manager;
    ComPtr<ITfSource> manager_source, context_source;
    ComPtr<ITfContext> context;
    DWORD manager_cookie=TF_INVALID_COOKIE, text_cookie=TF_INVALID_COOKIE, key_cookie=TF_INVALID_COOKIE;
    ULONGLONG generation=0;
    unsigned busy=0;
    DWORD last_key=0, last_modifiers=0;
    ULONGLONG key_tick=0;
    bool canceled=false;
    bool failed=false;
    TfClientId client=0;
    bool client_active=false;
    HANDLE edit_done=nullptr;
    ComPtr<ITfRange> committed;
    ComPtr<ITfRange> committed_selection;
    std::wstring committed_text;
    std::wstring committed_document;
    bool document_snapshot_valid=false;
    TextDiff last_text_diff{};
    std::wstring last_inserted_text;
    DWORD last_original_units=0;
    TF_SELECTIONSTYLE committed_style{};
    HWND committed_focus=nullptr;
    ULONGLONG commit_id=0, edit_revision=0;
    bool applying=false;
    DWORD last_invalidation=NoReason;
    void invalidate_edit(DWORD reason=EditInvalidation) {
        committed.Reset(); committed_selection.Reset(); committed_text.clear();
        committed_document.clear(); document_snapshot_valid=false;
        ++edit_revision; last_invalidation=reason;
    }
    bool read_document(TfEditCookie cookie,std::wstring& text) {
        ComPtr<ITfRange> range,end;
        HRESULT hr=context->GetStart(cookie,&range);
        if (SUCCEEDED(hr)) hr=context->GetEnd(cookie,&end);
        if (SUCCEEDED(hr)) hr=range->ShiftEndToRange(cookie,end.Get(),TF_ANCHOR_END);
        if (FAILED(hr)) return false;
        text.resize(document_limit+1);
        ULONG count=0;
        hr=range->GetText(cookie,TF_TF_MOVESTART,text.data(),document_limit+1,&count);
        BOOL empty=FALSE;
        if (FAILED(hr) || count>document_limit || FAILED(range->IsEmpty(cookie,&empty)) || !empty) {
            text.clear(); return false;
        }
        text.resize(count); return true;
    }
    bool unchanged_commit(TfEditCookie cookie,ITfEditRecord* record) {
        if (!committed) return false;
        last_invalidation=InspectionInvalidation;
        if (!committed || !committed_selection || !record) return false;
        // OnEndEdit also reports a repeated SetSelection or property update.
        // A notification alone does not prove a content change. Verify exact
        // document equality for text notifications, then verify the original
        // range and selection. Never recreate a range or replay a request.
        const GUID* properties[]={&composing_guid};
        ComPtr<IEnumTfRanges> updates;
        HRESULT hr=record->GetTextAndPropertyUpdates(TF_GTP_INCL_TEXT,nullptr,0,&updates);
        if (FAILED(hr) || !updates) return false;
        ComPtr<ITfRange> changed;
        HRESULT next=updates->Next(1,&changed,nullptr);
        if (next!=S_OK && next!=S_FALSE) return false;
        bool text_updated=next==S_OK;
        if (text_updated) {
            if (!document_snapshot_valid) { last_invalidation=DocumentSnapshotInvalidation; return false; }
            std::wstring document;
            if (!read_document(cookie,document)) return false;
            if (document!=committed_document) {
                size_t prefix=0,suffix=0;
                while (prefix<document.size() && prefix<committed_document.size() &&
                       document[prefix]==committed_document[prefix]) ++prefix;
                while (suffix<document.size()-prefix && suffix<committed_document.size()-prefix &&
                       document[document.size()-1-suffix]==committed_document[committed_document.size()-1-suffix]) ++suffix;
                wchar_t source[text_limit]{}; ULONG length=0;
                HRESULT read_hr=committed->GetText(cookie,0,source,text_limit,&length);
                last_text_diff={static_cast<DWORD>(committed_document.size()),static_cast<DWORD>(document.size()),
                    static_cast<DWORD>(prefix),static_cast<DWORD>(suffix),
                    FAILED(read_hr)?2u:std::wstring(source,length)==committed_text?1u:0u};
                last_inserted_text=document.substr(prefix,std::min<size_t>(16,document.size()-prefix-suffix));
                last_original_units=static_cast<DWORD>(committed_text.size());
                last_invalidation=TextInvalidation; return false;
            }
        }
        updates.Reset(); changed.Reset();
        hr=record->GetTextAndPropertyUpdates(0,properties,1,&updates);
        if (FAILED(hr) || !updates) return false;
        next=updates->Next(1,&changed,nullptr);
        if (next!=S_OK && next!=S_FALSE) return false;
        bool composition_updated=next==S_OK;
        if (composition_updated || text_updated) {
            ComPtr<ITfContextComposition> service;
            ComPtr<IEnumITfCompositionView> compositions;
            ComPtr<ITfCompositionView> active;
            hr=context.As(&service);
            if (SUCCEEDED(hr)) hr=service->EnumCompositions(&compositions);
            if (FAILED(hr)) return false;
            hr=compositions->Next(1,&active,nullptr);
            if (hr!=S_FALSE) {
                if (SUCCEEDED(hr)) last_invalidation=NewCompositionInvalidation;
                return false;
            }
            wchar_t text[text_limit]{}; ULONG length=0;
            if (FAILED(committed->GetText(cookie,0,text,text_limit,&length))) return false;
            if (std::wstring(text,length)!=committed_text) {
                last_invalidation=RangeTextInvalidation; return false;
            }
            ComPtr<ITfProperty> property;
            VARIANT value{}; VariantInit(&value);
            hr=context->GetProperty(composing_guid,&property);
            if (SUCCEEDED(hr)) hr=property->GetValue(cookie,committed.Get(),&value);
            bool ended=SUCCEEDED(hr) && (value.vt==VT_EMPTY ||
                (value.vt==VT_I4 && value.lVal==0) || (value.vt==VT_BOOL && value.boolVal==VARIANT_FALSE));
            VariantClear(&value);
            if (!ended) { last_invalidation=CompositionInvalidation; return false; }
        }
        BOOL selection_changed=TRUE;
        if (FAILED(record->GetSelectionStatus(&selection_changed))) return false;
        if (!selection_changed && !composition_updated && !text_updated) return true;
        TF_SELECTION selection{}; ULONG count=0;
        hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&count);
        ComPtr<ITfRange> selected; selected.Attach(selection.range);
        if (FAILED(hr) || count!=1 || !selected) return false;
        if (selection.style.ase!=committed_style.ase ||
            selection.style.fInterimChar!=committed_style.fInterimChar) {
            last_invalidation=StyleInvalidation; return false;
        }
        LONG start=1,end=1;
        if (FAILED(selected->CompareStart(cookie,committed_selection.Get(),TF_ANCHOR_START,&start)) ||
            FAILED(selected->CompareEnd(cookie,committed_selection.Get(),TF_ANCHOR_END,&end))) return false;
        if (start || end) { last_invalidation=SelectionInvalidation; return false; }
        return true;
    }
    HRESULT request_edit();
    struct Range {
        ComPtr<IUnknown> identity;
        ComPtr<ITfRange> range;
        ULONGLONG started=0;
    };
    std::vector<Range> previous;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void** result) override {
        if (!result) return E_POINTER;
        *result=nullptr;
        if (iid==IID_IUnknown || iid==IID_ITfTextEditSink) *result=static_cast<ITfTextEditSink*>(this);
        else if (iid==IID_ITfThreadMgrEventSink) *result=static_cast<ITfThreadMgrEventSink*>(this);
        else if (iid==IID_ITfKeyTraceEventSink) *result=static_cast<ITfKeyTraceEventSink*>(this);
        else return E_NOINTERFACE;
        AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { ULONG n=--refs; if (!n) delete this; return n; }
    void emit(DWORD kind, HRESULT hr=S_OK, const std::wstring& text=L"", DWORD active=0,
              ULONGLONG composition_tick=0) {
        if (!shared) return;
        LONG w=InterlockedCompareExchange(&shared->write,0,0);
        LONG r=InterlockedCompareExchange(&shared->read,0,0);
        if (static_cast<unsigned>(w-r)>=capacity) { InterlockedIncrement(&shared->dropped); return; }
        Packet packet{};
        packet.kind=kind; packet.hr=static_cast<DWORD>(hr); packet.pid=GetCurrentProcessId();
        packet.tid=GetCurrentThreadId(); packet.tick=GetTickCount64();
        packet.hwnd=reinterpret_cast<ULONGLONG>(GetFocus()); packet.generation=generation;
        bool fresh_key=GetTickCount64()-key_tick<=1500;
        packet.key=(kind==Candidate && !fresh_key)?0:last_key;
        packet.modifiers=(kind==Key || (kind==Candidate && fresh_key))?last_modifiers:0;
        packet.active=active;
        packet.composition_tick=composition_tick;
        packet.commit_id=commit_id; packet.revision=edit_revision;
        if (text.size()>=text_limit) { packet.kind=Unsupported; packet.hr=ERROR_BUFFER_OVERFLOW; }
        else std::copy(text.begin(),text.end(),packet.text);
        shared->packets[static_cast<unsigned>(w)%capacity]=packet;
        MemoryBarrier(); InterlockedExchange(&shared->write,w+1);
    }
    bool stopping() const { return failed || !shared || shared->stop || !owner || WaitForSingleObject(owner,0)!=WAIT_TIMEOUT; }
    HRESULT clear_context() {
        invalidate_edit(ContextInvalidation);
        previous.clear(); canceled=false; last_key=0; last_modifiers=0; key_tick=0; ++generation;
        HRESULT hr=S_OK;
        if (context_source && text_cookie!=TF_INVALID_COOKIE) hr=context_source->UnadviseSink(text_cookie);
        text_cookie=TF_INVALID_COOKIE; context_source.Reset(); context.Reset();
        if (FAILED(hr)) { failed=true; emit(Error,hr); }
        return hr;
    }
    void focus(ITfDocumentMgr* doc) {
        if (stopping() || FAILED(clear_context())) return;
        if (!doc) { emit(Context,S_FALSE); return; }
        HRESULT hr=doc->GetTop(&context);
        if (SUCCEEDED(hr) && context) hr=context.As(&context_source);
        if (SUCCEEDED(hr) && context_source)
            hr=context_source->AdviseSink(IID_ITfTextEditSink,static_cast<ITfTextEditSink*>(this),&text_cookie);
        emit(Context,hr);
    }
    void refresh_focus() { ComPtr<ITfDocumentMgr> doc; if (!stopping() && manager && SUCCEEDED(manager->GetFocus(&doc))) focus(doc.Get()); }
    HRESULT init(DWORD pid) {
        owner=OpenProcess(SYNCHRONIZE,FALSE,pid);
        mapping=OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,mapping_name(pid).c_str());
        if (!owner || !mapping) return HRESULT_FROM_WIN32(GetLastError());
        shared=static_cast<Shared*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared)));
        if (!shared) return HRESULT_FROM_WIN32(GetLastError());
        edit_done=OpenEventW(EVENT_MODIFY_STATE,FALSE,event_name(pid).c_str());
        if (!edit_done) return HRESULT_FROM_WIN32(GetLastError());
        using GetManager=HRESULT(WINAPI*)(ITfThreadMgr**);
        HMODULE tsf=GetModuleHandleW(L"msctf.dll");
        auto get=tsf ? reinterpret_cast<GetManager>(GetProcAddress(tsf,"TF_GetThreadMgr")) : nullptr;
        HRESULT hr=get ? get(&manager) : E_NOINTERFACE;
        if (FAILED(hr) || !manager) { emit(Error,FAILED(hr)?hr:E_NOINTERFACE); return E_NOINTERFACE; }
        // Balanced Activate/Deactivate obtains an editing client id; no TIP/IME registration.
        hr=manager->Activate(&client);
        if (FAILED(hr)) return hr;
        client_active=true;
        hr=manager.As(&manager_source);
        if (SUCCEEDED(hr)) hr=manager_source->AdviseSink(IID_ITfThreadMgrEventSink,
            static_cast<ITfThreadMgrEventSink*>(this),&manager_cookie);
        if (FAILED(hr)) { emit(Error,hr); return hr; }
        hr=manager_source->AdviseSink(IID_ITfKeyTraceEventSink,static_cast<ITfKeyTraceEventSink*>(this),&key_cookie);
        if (FAILED(hr)) emit(Unsupported,hr);
        WNDCLASSW wc{}; wc.lpfnWndProc=control_window; wc.hInstance=module_handle; wc.lpszClassName=L"kana-tsf-probe-hidden";
        RegisterClassW(&wc);
        window=CreateWindowExW(0,wc.lpszClassName,L"",0,0,0,0,0,HWND_MESSAGE,nullptr,module_handle,this);
        if (!window) return HRESULT_FROM_WIN32(GetLastError());
        shared->control_hwnd=reinterpret_cast<ULONGLONG>(window);
        if (!SetTimer(window,1,250,nullptr)) { emit(Error,E_FAIL); return E_FAIL; }
        InterlockedExchange(&shared->attached,1); emit(Attached); refresh_focus(); return S_OK;
    }
    void detach() {
        bool clean=!failed;
        clean=SUCCEEDED(clear_context()) && clean;
        if (manager_source && manager_cookie!=TF_INVALID_COOKIE)
            clean=SUCCEEDED(manager_source->UnadviseSink(manager_cookie)) && clean;
        if (manager_source && key_cookie!=TF_INVALID_COOKIE)
            clean=SUCCEEDED(manager_source->UnadviseSink(key_cookie)) && clean;
        manager_cookie=key_cookie=TF_INVALID_COOKIE; manager_source.Reset();
        if (manager && client_active) {
            clean=SUCCEEDED(manager->Deactivate()) && clean; client_active=false;
        }
        manager.Reset();
        emit(Detached,clean?S_OK:E_FAIL);
        if (shared) { shared->control_hwnd=0; if (clean) InterlockedExchange(&shared->attached,0); }
        if (window) { KillTimer(window,1); DestroyWindow(window); window=nullptr; }
    }
    ~Observer() {
        if (edit_done) CloseHandle(edit_done);
        if (shared) UnmapViewOfFile(shared);
        if (mapping) CloseHandle(mapping);
        if (owner) CloseHandle(owner);
    }
    HRESULT STDMETHODCALLTYPE OnInitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT change_focus(ITfDocumentMgr* doc,bool refresh) {
        Busy guard(busy);
        try { if (refresh) refresh_focus(); else focus(doc); }
        catch (...) { failed=true; emit(Error,E_FAIL); }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnUninitDocumentMgr(ITfDocumentMgr*) override { return change_focus(nullptr,true); }
    HRESULT STDMETHODCALLTYPE OnSetFocus(ITfDocumentMgr* doc,ITfDocumentMgr*) override { return change_focus(doc,false); }
    HRESULT STDMETHODCALLTYPE OnPushContext(ITfContext*) override { return change_focus(nullptr,true); }
    HRESULT STDMETHODCALLTYPE OnPopContext(ITfContext*) override { return change_focus(nullptr,true); }
    DWORD modifier_state() const {
        DWORD value=0;
        if (GetKeyState(VK_CONTROL)&0x8000) value|=ModifierCtrl;
        if (GetKeyState(VK_MENU)&0x8000) value|=ModifierAlt;
        if (GetKeyState(VK_SHIFT)&0x8000) value|=ModifierShift;
        if ((GetKeyState(VK_LWIN)|GetKeyState(VK_RWIN))&0x8000) value|=ModifierWin;
        return value;
    }
    HRESULT STDMETHODCALLTYPE OnKeyTraceDown(WPARAM key,LPARAM) override {
        if (stopping()) return S_OK;
        // Any key, including a new composition, invalidates the retained commit.
        invalidate_edit(KeyInvalidation);
        last_key=static_cast<DWORD>(key);
        last_modifiers=modifier_state();
        key_tick=GetTickCount64();
        if (key==VK_ESCAPE && !previous.empty()) canceled=true;
        emit(Key); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyTraceUp(WPARAM,LPARAM) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnEndEdit(ITfContext* edited,TfEditCookie cookie,ITfEditRecord* record) override {
        if (stopping() || edited!=context.Get()) return S_OK;
        if (applying) return S_OK;
        DWORD previous_invalidation=last_invalidation;
        if (!unchanged_commit(cookie,record)) invalidate_edit(last_invalidation);
        else last_invalidation=previous_invalidation;
        ++busy;
        try { read_edit(edited,cookie); } catch (...) { previous.clear(); emit(Error,E_FAIL); }
        --busy; return S_OK;
    }
    void read_edit(ITfContext* edited,TfEditCookie cookie) {
        ComPtr<ITfContextComposition> composing;
        ComPtr<IEnumITfCompositionView> enumeration;
        HRESULT hr=edited->QueryInterface(IID_PPV_ARGS(&composing));
        if (SUCCEEDED(hr)) hr=composing->EnumCompositions(&enumeration);
        if (FAILED(hr)) { emit(Error,hr); previous.clear(); return; }
        std::vector<Range> current;
        ComPtr<ITfCompositionView> view;
        while ((hr=enumeration->Next(1,&view,nullptr))==S_OK) {
            Range entry; ComPtr<ITfRange> range;
            if (FAILED(view.As(&entry.identity)) || FAILED(view->GetRange(&range)) || FAILED(range->Clone(&entry.range))) {
                emit(Error,E_FAIL); previous.clear(); return;
            }
            auto old=std::find_if(previous.begin(),previous.end(),[&](const Range& item) {
                return item.identity.Get()==entry.identity.Get();
            });
            entry.started=old==previous.end()?GetTickCount64():old->started;
            current.push_back(std::move(entry)); view.Reset();
        }
        if (hr!=S_FALSE) { emit(Error,hr); previous.clear(); return; }
        if (previous.empty() && !current.empty()) { canceled=false; last_key=0; last_modifiers=0; key_tick=0; }
        emit(Editing,S_OK,L"",static_cast<DWORD>(current.size()));
        for (auto& old:previous) {
            bool exists=std::any_of(current.begin(),current.end(),[&](const Range& now){return now.identity.Get()==old.identity.Get();});
            if (exists) continue;
            wchar_t buffer[text_limit]{}; ULONG count=0;
            hr=old.range->GetText(cookie,0,buffer,text_limit,&count);
            if (FAILED(hr)) { emit(Error,hr); continue; }
            if (count>=text_limit) { emit(Unsupported,ERROR_BUFFER_OVERFLOW); continue; }
            if (canceled || !count) { emit(EmptyEnd); continue; }
            if (!current.empty()) { emit(Unsupported,S_FALSE); continue; } // partial/overlapping compositions: observe only
            ComPtr<ITfProperty> property;
            VARIANT value{}; VariantInit(&value);
            hr=edited->GetProperty(composing_guid,&property);
            if (SUCCEEDED(hr)) hr=property->GetValue(cookie,old.range.Get(),&value);
            bool committed=SUCCEEDED(hr) && (value.vt==VT_EMPTY ||
                (value.vt==VT_I4 && value.lVal==0) || (value.vt==VT_BOOL && value.boolVal==VARIANT_FALSE));
            VariantClear(&value);
            if (!committed) { emit(Unsupported,FAILED(hr)?hr:S_FALSE); continue; }
            // A removed composition can also be cancellation. The Python consumer
            // requires an observed Enter and a matching live editor snapshot.
            hr=old.range->Clone(&this->committed);
            if (FAILED(hr)) { emit(Error,hr); continue; }
            TF_SELECTION selection{}; ULONG selected_count=0;
            hr=edited->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&selected_count);
            ComPtr<ITfRange> selected; selected.Attach(selection.range);
            if (SUCCEEDED(hr) && selected_count==1 && selected)
                hr=selected->Clone(&committed_selection);
            else if (SUCCEEDED(hr)) hr=E_FAIL;
            if (FAILED(hr)) { invalidate_edit(SnapshotInvalidation); emit(Error,hr); continue; }
            committed_style=selection.style;
            committed_text.assign(buffer,count);
            document_snapshot_valid=read_document(cookie,committed_document);
            last_text_diff={};
            last_inserted_text.clear();
            last_original_units=0;
            committed_focus=GetFocus();
            ++commit_id;
            emit(Candidate,S_OK,std::wstring(buffer,count),0,old.started);
        }
        previous=std::move(current);
    }
};

class ReplaceSession final : public ITfEditSession {
    std::atomic<ULONG> refs{1};
    Observer* owner;
    ComPtr<ITfContext> context;
    ComPtr<ITfRange> range;
    ULONGLONG request, commit, revision;
    HWND focus;
    std::wstring expected, replacement;
public:
    explicit ReplaceSession(Observer* value):owner(value),context(value->context),range(value->committed),
        request(value->shared->request_id),commit(value->shared->commit_id),revision(value->shared->revision),
        focus(value->committed_focus),expected(value->shared->expected),replacement(value->shared->replacement) { owner->AddRef(); }
    ~ReplaceSession() { owner->Release(); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out) override {
        if (!out) return E_POINTER;
        *out=nullptr;
        if (iid!=IID_IUnknown && iid!=IID_ITfEditSession) return E_NOINTERFACE;
        *out=static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { auto count=--refs; if (!count) delete this; return count; }
    HRESULT replace(TfEditCookie cookie) {
        auto reject=[&](DWORD reason,HRESULT hr=HRESULT_FROM_WIN32(ERROR_REVISION_MISMATCH)) {
            owner->shared->text_diff=owner->last_invalidation==TextInvalidation?owner->last_text_diff:TextDiff{};
            if (owner->last_invalidation==TextInvalidation) {
                std::fill_n(owner->shared->inserted_text,17,L'\0');
                std::copy(owner->last_inserted_text.begin(),owner->last_inserted_text.end(),owner->shared->inserted_text);
                owner->shared->inserted_count=static_cast<DWORD>(owner->last_inserted_text.size());
                owner->shared->original_units=owner->last_original_units;
            }
            owner->shared->diagnostic=reason|(owner->last_invalidation<<16); return hr;
        };
        if (owner->stopping()) return reject(Stopped);
        if (!range) return reject(MissingRange);
        if (context.Get()!=owner->context.Get()) return reject(ContextChanged);
        if (commit!=owner->commit_id) return reject(CommitChanged);
        if (revision!=owner->edit_revision) return reject(RevisionChanged);
        if (GetFocus()!=focus) return reject(FocusChanged);
        if (GetTickCount64()>owner->shared->deadline) return reject(Expired);
        if (focus && GetAncestor(focus,GA_ROOT)!=GetForegroundWindow())
            return reject(ForegroundChanged);
        ComPtr<ITfContextComposition> service;
        ComPtr<IEnumITfCompositionView> compositions;
        ComPtr<ITfCompositionView> active;
        HRESULT hr=context.As(&service);
        if (SUCCEEDED(hr)) hr=service->EnumCompositions(&compositions);
        if (FAILED(hr)) return hr;
        hr=compositions->Next(1,&active,nullptr);
        if (hr!=S_FALSE) return reject(ActiveComposition,FAILED(hr)?hr:E_PENDING);
        wchar_t buffer[text_limit]{}; ULONG count=0;
        hr=range->GetText(cookie,0,buffer,text_limit,&count);
        if (FAILED(hr)) return hr;
        if (std::wstring(buffer,count)!=expected) return reject(TextMismatch);
        if (GetTickCount64()>owner->shared->deadline ||
            InterlockedCompareExchange(&owner->shared->edit_state,5,2)!=2) return E_ABORT;
        // The range is an object retained from this exact composition, not a
        // range guessed from UIA offsets. Never select or send keyboard input.
        // A failed provider call does not prove it had no side effect.
        // From this point report "written or unknown", never "not written".
        owner->shared->mutated=1;
        hr=range->SetText(cookie,0,replacement.data(),static_cast<LONG>(replacement.size()));
        if (FAILED(hr)) return hr;
        count=0;
        hr=range->GetText(cookie,0,buffer,text_limit,&count);
        if (FAILED(hr)) return hr;
        if (std::wstring(buffer,count)!=replacement) return HRESULT_FROM_WIN32(ERROR_INVALID_DATA);
        ComPtr<ITfRange> caret;
        hr=range->Clone(&caret);
        if (SUCCEEDED(hr)) hr=caret->Collapse(cookie,TF_ANCHOR_END);
        if (FAILED(hr)) return hr;
        TF_SELECTION selection{caret.Get(),{TF_AE_END,FALSE}};
        hr=context->SetSelection(cookie,1,&selection);
        if (FAILED(hr)) return hr;
        TF_SELECTION observed{}; ULONG fetched=0;
        hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&observed,&fetched);
        if (FAILED(hr)) return hr;
        ComPtr<ITfRange> selected; selected.Attach(observed.range);
        if (fetched!=1 || !selected) return E_FAIL;
        BOOL at_start=FALSE, at_end=FALSE;
        hr=caret->IsEqualStart(cookie,selected.Get(),TF_ANCHOR_START,&at_start);
        if (SUCCEEDED(hr)) hr=caret->IsEqualEnd(cookie,selected.Get(),TF_ANCHOR_END,&at_end);
        if (FAILED(hr)) return hr;
        return at_start && at_end?S_OK:E_FAIL;
    }
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie cookie) override {
        auto* shared=owner->shared;
        if (shared->request_id!=request || InterlockedCompareExchange(&shared->edit_state,2,1)!=1) return E_ABORT;
        HRESULT hr=E_FAIL;
        owner->applying=true;
        try { hr=replace(cookie); } catch (...) { hr=E_FAIL; }
        owner->applying=false;
        owner->invalidate_edit(CompletedInvalidation); // no replay, including a failed/partially applied write
        shared->result=static_cast<DWORD>(hr);
        MemoryBarrier(); InterlockedExchange(&shared->edit_state,3);
        SetEvent(owner->edit_done);
        return hr;
    }
};

HRESULT Observer::request_edit() {
    if (InterlockedCompareExchange(&shared->edit_state,1,1)!=1) return E_ABORT;
    HRESULT hr=E_UNEXPECTED, session_hr=E_FAIL;
    if (!busy && !stopping() && context) {
        ComPtr<ReplaceSession> session; session.Attach(new ReplaceSession(this));
        hr=context->RequestEditSession(client,session.Get(),TF_ES_ASYNCDONTCARE|TF_ES_READWRITE,&session_hr);
        if (SUCCEEDED(hr) && SUCCEEDED(session_hr)) return hr;
        if (SUCCEEDED(hr)) hr=session_hr;
    }
    shared->result=static_cast<DWORD>(hr);
    MemoryBarrier(); InterlockedExchange(&shared->edit_state,3); SetEvent(edit_done);
    return hr;
}

LRESULT CALLBACK control_window(HWND hwnd,UINT message,WPARAM wparam,LPARAM lparam) {
    if (message==WM_NCCREATE) {
        auto* data=reinterpret_cast<CREATESTRUCTW*>(lparam);
        SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(data->lpCreateParams));
    }
    auto* state=reinterpret_cast<Observer*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    if (message==WM_APP+2 && state) {
        try { state->request_edit(); }
        catch (...) {
            state->shared->result=E_FAIL;
            MemoryBarrier(); InterlockedExchange(&state->shared->edit_state,3); SetEvent(state->edit_done);
        }
        return 0;
    }
    if ((message==WM_TIMER || message==WM_APP+1) && state && !state->busy && state->stopping()) {
        observer=nullptr;
        { Observer::Busy guard(state->busy); state->detach(); }
        state->Release(); return 0;
    }
    return DefWindowProcW(hwnd,message,wparam,lparam);
}

LRESULT CALLBACK ProbeHook(int code,WPARAM wparam,LPARAM lparam) {
    if (code==HC_ACTION && wparam==PM_REMOVE) {
        auto* msg=reinterpret_cast<MSG*>(lparam);
        if (msg->message==message_id() && msg->wParam && !observer) {
            // Keep the module mapped until target process exit. COM callbacks
            // must never point into a DLL unloaded by hook removal.
            HMODULE retained;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_PIN|GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                reinterpret_cast<LPCWSTR>(&ProbeHook),&retained);
            Observer* state=nullptr;
            try {
                state=new Observer();
                HRESULT hr;
                { Observer::Busy guard(state->busy); hr=state->init(static_cast<DWORD>(msg->wParam)); }
                if (SUCCEEDED(hr)) observer=state;
                else { { Observer::Busy guard(state->busy); state->detach(); } state->Release(); }
            } catch (...) {
                if (state) { { Observer::Busy guard(state->busy); state->detach(); } state->Release(); }
            }
        }
    }
    return CallNextHookEx(nullptr,code,wparam,lparam);
}

// Check the packet layout before Python passes a buffer to PollProbe.
extern "C" __declspec(dllexport) DWORD WINAPI ProbeProtocolVersion() { return 101; }
extern "C" __declspec(dllexport) BOOL WINAPI StartProbe(DWORD tid) {
    if (!tid || host_poisoned) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    if (host_mapping) { SetLastError(ERROR_ALREADY_EXISTS); return FALSE; }
    host_edit_done=CreateEventW(nullptr,FALSE,FALSE,event_name(GetCurrentProcessId()).c_str());
    if (!host_edit_done) return FALSE;
    host_thread=OpenThread(SYNCHRONIZE,FALSE,tid);
    if (!host_thread) { CloseHandle(host_edit_done); host_edit_done=nullptr; return FALSE; }
    host_mapping=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(Shared),mapping_name(GetCurrentProcessId()).c_str());
    if (!host_mapping) { CloseHandle(host_thread); host_thread=nullptr; CloseHandle(host_edit_done); host_edit_done=nullptr; return FALSE; }
    host_shared=static_cast<Shared*>(MapViewOfFile(host_mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared)));
    if (!host_shared) { CloseHandle(host_mapping); host_mapping=nullptr; CloseHandle(host_thread); host_thread=nullptr; CloseHandle(host_edit_done); host_edit_done=nullptr; return FALSE; }
    ZeroMemory(host_shared,sizeof(Shared)); host_tid=tid;
    host_hook=SetWindowsHookExW(WH_GETMESSAGE,ProbeHook,module_handle,tid);
    if (!host_hook || !PostThreadMessageW(tid,message_id(),GetCurrentProcessId(),0)) {
        DWORD error=GetLastError(); if (host_hook) UnhookWindowsHookEx(host_hook);
        UnmapViewOfFile(host_shared); CloseHandle(host_mapping); host_shared=nullptr; host_mapping=nullptr;
        CloseHandle(host_thread); host_thread=nullptr;
        CloseHandle(host_edit_done); host_edit_done=nullptr;
        SetLastError(error); return FALSE;
    }
    return TRUE;
}
extern "C" __declspec(dllexport) BOOL WINAPI PollProbe(Packet* packet) {
    if (!host_shared || !packet) return FALSE;
    LONG r=InterlockedCompareExchange(&host_shared->read,0,0);
    LONG w=InterlockedCompareExchange(&host_shared->write,0,0);
    if (r==w) return FALSE;
    *packet=host_shared->packets[static_cast<unsigned>(r)%capacity];
    MemoryBarrier(); InterlockedExchange(&host_shared->read,r+1); return TRUE;
}
extern "C" __declspec(dllexport) DWORD WINAPI DroppedProbe() {
    return host_shared?static_cast<DWORD>(InterlockedCompareExchange(&host_shared->dropped,0,0)):0;
}
struct EditResult { DWORD hr, mutated, state; };
extern "C" __declspec(dllexport) DWORD WINAPI LastEditDiagnostic() {
    return host_shared?host_shared->diagnostic:0;
}
extern "C" __declspec(dllexport) BOOL WINAPI LastEditTextDiff(TextDiff* value) {
    if (!host_shared || !value || !host_shared->text_diff.before) return FALSE;
    *value=host_shared->text_diff; return TRUE;
}
extern "C" __declspec(dllexport) DWORD WINAPI LastEditInsertedText(wchar_t* value,DWORD capacity) {
    if (!host_shared || !value || capacity<17) return 0;
    std::copy_n(host_shared->inserted_text,17,value);
    return host_shared->inserted_count;
}
extern "C" __declspec(dllexport) DWORD WINAPI LastEditOriginalUnits() {
    return host_shared?host_shared->original_units:0;
}
extern "C" __declspec(dllexport) BOOL WINAPI ReplaceCommit(ULONGLONG commit,ULONGLONG revision,
        const wchar_t* expected,const wchar_t* replacement,DWORD timeout,EditResult* result) {
    if (!host_shared || host_poisoned || !expected || !replacement || !result || !timeout ||
        wcslen(expected)>=text_limit || wcslen(replacement)>=text_limit || !*expected) {
        SetLastError(ERROR_INVALID_PARAMETER); return FALSE;
    }
    if (InterlockedCompareExchange(&host_edit_busy,1,0)) { SetLastError(ERROR_BUSY); return FALSE; }
    struct ReleaseBusy { ~ReleaseBusy(){InterlockedExchange(&host_edit_busy,0);} } release;
    ResetEvent(host_edit_done);
    ++host_shared->request_id;
    host_shared->commit_id=commit; host_shared->revision=revision;
    host_shared->deadline=GetTickCount64()+timeout;
    wcscpy_s(host_shared->expected,text_limit,expected); wcscpy_s(host_shared->replacement,text_limit,replacement);
    host_shared->result=E_PENDING; host_shared->mutated=0; host_shared->diagnostic=0;
    host_shared->text_diff={};
    std::fill_n(host_shared->inserted_text,17,L'\0');
    host_shared->inserted_count=0;
    host_shared->original_units=0;
    MemoryBarrier(); InterlockedExchange(&host_shared->edit_state,1);
    if (!host_shared->control_hwnd || !PostMessageW(reinterpret_cast<HWND>(host_shared->control_hwnd),WM_APP+2,0,0)) {
        InterlockedExchange(&host_shared->edit_state,4); SetLastError(ERROR_NOT_READY); return FALSE;
    }
    HANDLE events[]{host_edit_done,host_thread};
    DWORD wait=WaitForMultipleObjects(2,events,FALSE,timeout);
    if (wait==WAIT_OBJECT_0) {
        MemoryBarrier(); *result={host_shared->result,host_shared->mutated,
            static_cast<DWORD>(InterlockedCompareExchange(&host_shared->edit_state,0,0))}; return TRUE;
    }
    LONG previous=InterlockedCompareExchange(&host_shared->edit_state,4,1);
    if (previous==2) previous=InterlockedCompareExchange(&host_shared->edit_state,4,2);
    // Never reuse this mailbox after timeout: a late COM callback may still exist.
    host_poisoned=true;
    *result={static_cast<DWORD>(HRESULT_FROM_WIN32(ERROR_TIMEOUT)),previous==5?1u:host_shared->mutated,static_cast<DWORD>(previous)};
    return TRUE;
}
extern "C" __declspec(dllexport) BOOL WINAPI StopProbe() {
    if (!host_shared) return TRUE;
    InterlockedExchange(&host_shared->stop,1);
    if (host_shared->control_hwnd) PostMessageW(reinterpret_cast<HWND>(host_shared->control_hwnd),WM_APP+1,0,0);
    ULONGLONG deadline=GetTickCount64()+1500;
    while (host_shared->attached && WaitForSingleObject(host_thread,0)==WAIT_TIMEOUT && GetTickCount64()<deadline) Sleep(10);
    BOOL clean=host_shared->attached==0 || WaitForSingleObject(host_thread,0)==WAIT_OBJECT_0;
    if (!clean) host_poisoned=true;
    if (host_hook) UnhookWindowsHookEx(host_hook);
    host_hook=nullptr;
    UnmapViewOfFile(host_shared); CloseHandle(host_mapping); host_shared=nullptr; host_mapping=nullptr;
    CloseHandle(host_thread); host_thread=nullptr;
    CloseHandle(host_edit_done); host_edit_done=nullptr;
    return clean;
}
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID) {
    if (reason==DLL_PROCESS_ATTACH) module_handle=module;
    return TRUE;
}
