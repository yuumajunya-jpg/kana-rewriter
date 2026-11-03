// Read-only, per-thread TSF observer. No IME registration.
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

// llvm-mingw omits ITfTextLayoutSink; declare its Windows SDK ABI locally.
#ifdef KANA_LIVE_PREVIEW
const GUID preview_layout_iid={0x2af2d06a,0xdd5b,0x4927,{0xa0,0xb4,0x54,0xf1,0x9c,0x91,0xfa,0xde}};
struct PreviewLayoutSink : IUnknown {
    virtual HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext*,int,ITfContextView*)=0;
};
#endif

using Microsoft::WRL::ComPtr;
constexpr unsigned capacity = 64, text_limit = 1024;
struct Packet {
    DWORD kind, pid, tid, hr, key, active, modifiers;
    ULONGLONG tick, hwnd, generation, composition_tick;
    wchar_t text[text_limit];
#ifdef KANA_LIVE_PREVIEW
    RECT caret_rect;
    DWORD position_valid;
    wchar_t before[text_limit],after[text_limit];
#endif
};
constexpr DWORD ModifierCtrl=1, ModifierAlt=2, ModifierShift=4, ModifierWin=8;
struct Shared {
    volatile LONG write, read, dropped, stop, attached;
    ULONGLONG control_hwnd;
    Packet packets[capacity];
};
enum Kind { Attached=1, Context=2, Editing=3, Candidate=4, EmptyEnd=5,
            Error=6, Detached=7, Key=8, Unsupported=9 };
const GUID composing_guid = {0xe12ac060,0xaf15,0x11d2,{0xaf,0xc5,0x00,0x10,0x5a,0x27,0x99,0xb5}};
HINSTANCE module_handle;
UINT control_message;
HANDLE host_mapping;
Shared* host_shared;
HHOOK host_hook;
DWORD host_tid;
HANDLE host_thread;
bool host_poisoned=false;

#ifdef KANA_LIVE_PREVIEW
std::wstring mapping_name(DWORD pid) { return L"Local\\kana-tsf-preview-" + std::to_wstring(pid); }
UINT message_id() { return RegisterWindowMessageW(L"kana-tsf-preview-control-v1"); }
#else
std::wstring mapping_name(DWORD pid) { return L"Local\\kana-tsf-probe-" + std::to_wstring(pid); }
UINT message_id() { return RegisterWindowMessageW(L"kana-tsf-probe-control-v1"); }
#endif

class Observer;
thread_local Observer* observer;
LRESULT CALLBACK control_window(HWND, UINT, WPARAM, LPARAM);

class Observer final : public ITfTextEditSink, public ITfThreadMgrEventSink, public ITfKeyTraceEventSink
#ifdef KANA_LIVE_PREVIEW
    , public PreviewLayoutSink
#endif
{
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
#ifdef KANA_LIVE_PREVIEW
    RECT preview_rect{};
    bool preview_position_valid=false;
    DWORD layout_cookie=TF_INVALID_COOKIE;
    TfClientId preview_client=0;
    bool preview_client_active=false, layout_pending=false;
    std::wstring preview_before,preview_after;
    HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext*,int,ITfContextView*) override;
#endif
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
#ifdef KANA_LIVE_PREVIEW
        else if (iid==preview_layout_iid) *result=static_cast<PreviewLayoutSink*>(this);
#endif
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
#ifdef KANA_LIVE_PREVIEW
        packet.caret_rect=preview_rect;
        packet.position_valid=preview_position_valid?1:0;
        std::copy(preview_before.begin(),preview_before.end(),packet.before);
        std::copy(preview_after.begin(),preview_after.end(),packet.after);
#endif
        if (text.size()>=text_limit) { packet.kind=Unsupported; packet.hr=ERROR_BUFFER_OVERFLOW; }
        else std::copy(text.begin(),text.end(),packet.text);
        shared->packets[static_cast<unsigned>(w)%capacity]=packet;
        MemoryBarrier(); InterlockedExchange(&shared->write,w+1);
    }
    bool stopping() const { return failed || !shared || shared->stop || !owner || WaitForSingleObject(owner,0)!=WAIT_TIMEOUT; }
    HRESULT clear_context() {
        previous.clear(); canceled=false; last_key=0; last_modifiers=0; key_tick=0; ++generation;
        HRESULT hr=S_OK;
#ifdef KANA_LIVE_PREVIEW
        if (context_source && layout_cookie!=TF_INVALID_COOKIE)
            hr=context_source->UnadviseSink(layout_cookie);
        layout_cookie=TF_INVALID_COOKIE;
#endif
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
#ifdef KANA_LIVE_PREVIEW
        if (SUCCEEDED(hr) && context_source)
            hr=context_source->AdviseSink(preview_layout_iid,static_cast<PreviewLayoutSink*>(this),&layout_cookie);
#endif
        emit(Context,hr);
    }
    void refresh_focus() { ComPtr<ITfDocumentMgr> doc; if (!stopping() && manager && SUCCEEDED(manager->GetFocus(&doc))) focus(doc.Get()); }
    HRESULT init(DWORD pid) {
        owner=OpenProcess(SYNCHRONIZE,FALSE,pid);
        mapping=OpenFileMappingW(FILE_MAP_ALL_ACCESS,FALSE,mapping_name(pid).c_str());
        if (!owner || !mapping) return HRESULT_FROM_WIN32(GetLastError());
        shared=static_cast<Shared*>(MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared)));
        if (!shared) return HRESULT_FROM_WIN32(GetLastError());
        using GetManager=HRESULT(WINAPI*)(ITfThreadMgr**);
        HMODULE tsf=GetModuleHandleW(L"msctf.dll");
        auto get=tsf ? reinterpret_cast<GetManager>(GetProcAddress(tsf,"TF_GetThreadMgr")) : nullptr;
        HRESULT hr=get ? get(&manager) : E_NOINTERFACE;
        if (FAILED(hr) || !manager) { emit(Error,FAILED(hr)?hr:E_NOINTERFACE); return E_NOINTERFACE; }
#ifdef KANA_LIVE_PREVIEW
        hr=manager->Activate(&preview_client);
        if (FAILED(hr)) return hr;
        preview_client_active=true;
#endif
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
#ifdef KANA_LIVE_PREVIEW
        if (manager && preview_client_active) {
            clean=SUCCEEDED(manager->Deactivate()) && clean;
            preview_client_active=false;
        }
#endif
        manager.Reset();
        emit(Detached,clean?S_OK:E_FAIL);
        if (shared) { shared->control_hwnd=0; if (clean) InterlockedExchange(&shared->attached,0); }
        if (window) { KillTimer(window,1); DestroyWindow(window); window=nullptr; }
    }
    ~Observer() {
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
        last_key=static_cast<DWORD>(key);
        last_modifiers=modifier_state();
        key_tick=GetTickCount64();
        if (key==VK_ESCAPE && !previous.empty()) canceled=true;
        emit(Key); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyTraceUp(WPARAM,LPARAM) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnEndEdit(ITfContext* edited,TfEditCookie cookie,ITfEditRecord*) override {
        if (stopping() || edited!=context.Get()) return S_OK;
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
#ifdef KANA_LIVE_PREVIEW
        preview_rect={}; preview_position_valid=false;
        preview_before.clear(); preview_after.clear();
        std::wstring live;
        if (!current.empty()) {
            std::sort(current.begin(),current.end(),[&](const Range& a,const Range& b) {
                LONG order=0;
                return SUCCEEDED(a.range->CompareStart(cookie,b.range.Get(),TF_ANCHOR_START,&order)) && order<0;
            });
            ComPtr<ITfRange> whole;
            hr=current.front().range->Clone(&whole);
            if (SUCCEEDED(hr)) hr=whole->ShiftEndToRange(cookie,current.back().range.Get(),TF_ANCHOR_END);
            // GetText may return fewer characters than requested. Read a clone
            // to exhaustion rather than treating one chunk as the entire input.
            auto read_all=[&](ITfRange* source,std::wstring& text) -> HRESULT {
                ComPtr<ITfRange> cursor; HRESULT result=source->Clone(&cursor);
                if (FAILED(result)) return result;
                for (;;) {
                    wchar_t buffer[256]{}; ULONG count=0;
                    result=cursor->GetText(cookie,TF_TF_MOVESTART,buffer,256,&count);
                    if (FAILED(result)) return result;
                    if (!count) return S_OK;
                    if (text.size()+count>=text_limit) return HRESULT_FROM_WIN32(ERROR_BUFFER_OVERFLOW);
                    text.append(buffer,count);
                }
            };
            if (SUCCEEDED(hr)) hr=read_all(whole.Get(),live);
            if (FAILED(hr)) { emit(Error,hr); return; }
            ComPtr<ITfRange> surrounding;
            LONG shifted=0;
            hr=whole->Clone(&surrounding);
            if (SUCCEEDED(hr)) hr=surrounding->Collapse(cookie,TF_ANCHOR_START);
            if (SUCCEEDED(hr)) hr=surrounding->ShiftStart(cookie,-static_cast<LONG>(text_limit-1),&shifted,nullptr);
            if (SUCCEEDED(hr)) hr=read_all(surrounding.Get(),preview_before);
            if (FAILED(hr)) { emit(Error,hr); return; }
            hr=whole->Clone(&surrounding);
            if (SUCCEEDED(hr)) hr=surrounding->Collapse(cookie,TF_ANCHOR_END);
            if (SUCCEEDED(hr)) hr=surrounding->ShiftEnd(cookie,static_cast<LONG>(text_limit-1),&shifted,nullptr);
            if (SUCCEEDED(hr)) hr=read_all(surrounding.Get(),preview_after);
            if (FAILED(hr)) { emit(Error,hr); return; }
            ComPtr<ITfContextView> context_view;
            ComPtr<ITfRange> caret;
            BOOL clipped=TRUE;
            hr=edited->GetActiveView(&context_view);
            if (SUCCEEDED(hr)) hr=whole->Clone(&caret);
            if (SUCCEEDED(hr)) hr=caret->Collapse(cookie,TF_ANCHOR_END);
            if (SUCCEEDED(hr)) hr=context_view->GetTextExt(cookie,caret.Get(),&preview_rect,&clipped);
            if (FAILED(hr) || preview_rect.bottom<=preview_rect.top) {
                // Some text stores reject empty ranges. Query the last glyph.
                LONG shifted=0;
                if (caret && context_view && SUCCEEDED(caret->ShiftStart(cookie,-1,&shifted,nullptr)) && shifted==-1)
                    hr=context_view->GetTextExt(cookie,caret.Get(),&preview_rect,&clipped);
            }
            preview_position_valid=SUCCEEDED(hr) && !clipped &&
                preview_rect.bottom>preview_rect.top && preview_rect.right>=preview_rect.left;
        }
        emit(Editing,S_OK,live,static_cast<DWORD>(current.size()),
             current.empty()?0:current.front().started);
        // A retired empty range is not the end of the live preview when the
        // IME replaced its composition (e.g. Backspace followed by typing).
        // The active snapshot above is authoritative for the whole context.
        if (!current.empty()) {
            previous=std::move(current);
            return;
        }
#else
        emit(Editing,S_OK,L"",static_cast<DWORD>(current.size()));
#endif
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
            emit(Candidate,S_OK,std::wstring(buffer,count),0,old.started);
        }
        previous=std::move(current);
    }
};

#ifdef KANA_LIVE_PREVIEW
class PreviewLayoutRead final : public ITfEditSession {
    std::atomic<ULONG> refs{1};
    Observer* owner;
    ComPtr<ITfContext> context;
public:
    explicit PreviewLayoutRead(Observer* value):owner(value),context(value->context) { owner->AddRef(); }
    ~PreviewLayoutRead() { owner->layout_pending=false; owner->Release(); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** result) override {
        if (!result) return E_POINTER;
        *result=nullptr;
        if (iid!=IID_IUnknown && iid!=IID_ITfEditSession) return E_NOINTERFACE;
        *result=static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { auto count=--refs; if (!count) delete this; return count; }
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie cookie) override {
        if (!owner->stopping() && owner->context.Get()==context.Get() && !owner->previous.empty()) {
            Observer::Busy guard(owner->busy);
            try { owner->read_edit(context.Get(),cookie); } catch (...) { owner->emit(Error,E_FAIL); }
        }
        return S_OK;
    }
};
HRESULT Observer::OnLayoutChange(ITfContext* changed,int code,ITfContextView*) {
    if (stopping() || changed!=context.Get() || code!=1 || previous.empty() || layout_pending)
        return S_OK;
    layout_pending=true;
    ComPtr<ITfEditSession> session; session.Attach(new PreviewLayoutRead(this));
    HRESULT result=E_FAIL;
    context->RequestEditSession(preview_client,session.Get(),TF_ES_ASYNC|TF_ES_READ,&result);
    return S_OK;
}
#endif

LRESULT CALLBACK control_window(HWND hwnd,UINT message,WPARAM wparam,LPARAM lparam) {
    if (message==WM_NCCREATE) {
        auto* data=reinterpret_cast<CREATESTRUCTW*>(lparam);
        SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(data->lpCreateParams));
    }
    auto* state=reinterpret_cast<Observer*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
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
extern "C" __declspec(dllexport) DWORD WINAPI ProbeProtocolVersion() {
#ifdef KANA_LIVE_PREVIEW
    return 5;
#else
    return 3;
#endif
}
extern "C" __declspec(dllexport) BOOL WINAPI StartProbe(DWORD tid) {
    if (!tid || host_poisoned) { SetLastError(ERROR_INVALID_PARAMETER); return FALSE; }
    if (host_mapping) { SetLastError(ERROR_ALREADY_EXISTS); return FALSE; }
    host_thread=OpenThread(SYNCHRONIZE,FALSE,tid);
    if (!host_thread) return FALSE;
    host_mapping=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(Shared),mapping_name(GetCurrentProcessId()).c_str());
    if (!host_mapping) { CloseHandle(host_thread); host_thread=nullptr; return FALSE; }
    host_shared=static_cast<Shared*>(MapViewOfFile(host_mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared)));
    if (!host_shared) { CloseHandle(host_mapping); host_mapping=nullptr; CloseHandle(host_thread); host_thread=nullptr; return FALSE; }
    ZeroMemory(host_shared,sizeof(Shared)); host_tid=tid;
    host_hook=SetWindowsHookExW(WH_GETMESSAGE,ProbeHook,module_handle,tid);
    if (!host_hook || !PostThreadMessageW(tid,message_id(),GetCurrentProcessId(),0)) {
        DWORD error=GetLastError(); if (host_hook) UnhookWindowsHookEx(host_hook);
        UnmapViewOfFile(host_shared); CloseHandle(host_mapping); host_shared=nullptr; host_mapping=nullptr;
        CloseHandle(host_thread); host_thread=nullptr;
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
    return clean;
}
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID) {
    if (reason==DLL_PROCESS_ATTACH) module_handle=module;
    return TRUE;
}
