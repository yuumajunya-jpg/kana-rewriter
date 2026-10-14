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

using Microsoft::WRL::ComPtr;
constexpr unsigned capacity = 64, text_limit = 1024;
struct Packet {
    DWORD kind, pid, tid, hr, key, active;
    ULONGLONG tick, hwnd, generation, composition_tick;
    wchar_t text[text_limit];
};
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

std::wstring mapping_name(DWORD pid) { return L"Local\\kana-tsf-probe-" + std::to_wstring(pid); }
UINT message_id() { return RegisterWindowMessageW(L"kana-tsf-probe-control-v1"); }

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
    DWORD last_key=0;
    ULONGLONG key_tick=0;
    bool canceled=false;
    bool failed=false;
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
        packet.key=(kind==Candidate && GetTickCount64()-key_tick>1500)?0:last_key;
        packet.active=active;
        packet.composition_tick=composition_tick;
        if (text.size()>=text_limit) { packet.kind=Unsupported; packet.hr=ERROR_BUFFER_OVERFLOW; }
        else std::copy(text.begin(),text.end(),packet.text);
        shared->packets[static_cast<unsigned>(w)%capacity]=packet;
        MemoryBarrier(); InterlockedExchange(&shared->write,w+1);
    }
    bool stopping() const { return failed || !shared || shared->stop || !owner || WaitForSingleObject(owner,0)!=WAIT_TIMEOUT; }
    HRESULT clear_context() {
        previous.clear(); canceled=false; last_key=0; key_tick=0; ++generation;
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
        using GetManager=HRESULT(WINAPI*)(ITfThreadMgr**);
        HMODULE tsf=GetModuleHandleW(L"msctf.dll");
        auto get=tsf ? reinterpret_cast<GetManager>(GetProcAddress(tsf,"TF_GetThreadMgr")) : nullptr;
        HRESULT hr=get ? get(&manager) : E_NOINTERFACE;
        if (FAILED(hr) || !manager) { emit(Error,FAILED(hr)?hr:E_NOINTERFACE); return E_NOINTERFACE; }
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
        manager_cookie=key_cookie=TF_INVALID_COOKIE; manager_source.Reset(); manager.Reset();
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
    HRESULT STDMETHODCALLTYPE OnKeyTraceDown(WPARAM key,LPARAM) override {
        if (stopping()) return S_OK;
        last_key=static_cast<DWORD>(key);
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
        if (previous.empty() && !current.empty()) { canceled=false; last_key=0; key_tick=0; }
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
            emit(Candidate,S_OK,std::wstring(buffer,count),0,old.started);
        }
        previous=std::move(current);
    }
};

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
extern "C" __declspec(dllexport) DWORD WINAPI ProbeProtocolVersion() { return 2; }
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
