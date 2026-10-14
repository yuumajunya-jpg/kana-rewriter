// Invisible synthetic TSF application for cross-process observer tests.
#define UNICODE
#define _UNICODE
#define NOMINMAX
#include <windows.h>
#include <msctf.h>
#include <textstor.h>
#include <wrl/client.h>
#include <atomic>
#include <algorithm>
#include <iostream>
#include <memory>
#include <string>

using Microsoft::WRL::ComPtr;
class Store final : public ITextStoreACP {
    std::atomic<ULONG> refs{1};
public:
    ComPtr<ITextStoreACPSink> sink;
    std::wstring text=L"さんぽ";
    TS_SELECTION_ACP selection{0,3,{TS_AE_END,FALSE}};
    HWND window=nullptr;
    bool locked=false;
    void sync_window() {
        SendMessageW(window,WM_SETTEXT,0,reinterpret_cast<LPARAM>(text.c_str()));
        SendMessageW(window,EM_SETSEL,text.size(),text.size());
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out) override {
        *out=nullptr; if (iid==IID_IUnknown || iid==IID_ITextStoreACP) {
            *out=static_cast<ITextStoreACP*>(this); AddRef(); return S_OK;
        } return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { auto n=--refs; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE AdviseSink(REFIID iid,IUnknown* object,DWORD) override {
        if (iid!=IID_ITextStoreACPSink) return E_INVALIDARG;
        return object->QueryInterface(IID_PPV_ARGS(&sink));
    }
    HRESULT STDMETHODCALLTYPE UnadviseSink(IUnknown*) override { sink.Reset(); return S_OK; }
    HRESULT STDMETHODCALLTYPE RequestLock(DWORD flags,HRESULT* result) override {
        if (locked) { *result=TS_E_SYNCHRONOUS; return S_OK; }
        locked=true; *result=sink->OnLockGranted(flags); locked=false; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetStatus(TS_STATUS* status) override { *status={0,TS_SS_NOHIDDENTEXT}; return S_OK; }
    HRESULT STDMETHODCALLTYPE QueryInsert(LONG start,LONG end,ULONG,LONG* a,LONG* b) override { *a=start; *b=end; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetSelection(ULONG,ULONG n,TS_SELECTION_ACP* out,ULONG* fetched) override {
        if (!locked) return TS_E_NOLOCK;
        *fetched=n?1:0; if (n) *out=selection; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetSelection(ULONG n,const TS_SELECTION_ACP* value) override {
        if (!locked) return TS_E_NOLOCK;
        if (n) selection=*value; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetText(LONG start,LONG end,WCHAR* out,ULONG size,ULONG* count,
        TS_RUNINFO* runs,ULONG run_size,ULONG* run_count,LONG* next) override {
        if (!locked) return TS_E_NOLOCK;
        if (end==-1) end=static_cast<LONG>(text.size());
        if (start<0 || end<start || end>static_cast<LONG>(text.size())) return TS_E_INVALIDPOS;
        ULONG n=std::min(size,static_cast<ULONG>(end-start));
        if (n && out) std::copy_n(text.data()+start,n,out);
        if (count) *count=n; if (next) *next=start+static_cast<LONG>(n);
        if (run_count) *run_count=run_size?1:0;
        if (run_size) { runs[0].type=TS_RT_PLAIN; runs[0].uCount=n; }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetText(DWORD,LONG start,LONG end,const WCHAR* value,ULONG n,TS_TEXTCHANGE* change) override {
        if (!locked) return TS_E_NOLOCK;
        if (start<0 || end<start || end>static_cast<LONG>(text.size())) return TS_E_INVALIDPOS;
        text.replace(start,end-start,value,n); *change={start,end,start+static_cast<LONG>(n)};
        selection.acpStart=selection.acpEnd=change->acpNewEnd; sync_window(); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetFormattedText(LONG,LONG,IDataObject**) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetEmbedded(LONG,REFGUID,REFIID,IUnknown**) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE QueryInsertEmbedded(const GUID*,const FORMATETC*,BOOL* out) override { *out=FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE InsertEmbedded(DWORD,LONG,LONG,IDataObject*,TS_TEXTCHANGE*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE InsertTextAtSelection(DWORD flags,const WCHAR* value,ULONG n,LONG* start,LONG* end,TS_TEXTCHANGE* change) override {
        if (start) *start=selection.acpStart; if (end) *end=selection.acpStart+static_cast<LONG>(n);
        if (flags&TS_IAS_QUERYONLY) return S_OK;
        TS_TEXTCHANGE local{}; return SetText(0,selection.acpStart,selection.acpEnd,value,n,change?change:&local);
    }
    HRESULT STDMETHODCALLTYPE InsertEmbeddedAtSelection(DWORD,IDataObject*,LONG*,LONG*,TS_TEXTCHANGE*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE RequestSupportedAttrs(DWORD,ULONG,const TS_ATTRID*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE RequestAttrsAtPosition(LONG,ULONG,const TS_ATTRID*,DWORD) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE RequestAttrsTransitioningAtPosition(LONG,ULONG,const TS_ATTRID*,DWORD) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE FindNextAttrTransition(LONG,LONG end,ULONG,const TS_ATTRID*,DWORD,LONG* next,BOOL* found,LONG* offset) override {
        *next=end; *found=FALSE; *offset=0; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE RetrieveRequestedAttrs(ULONG,TS_ATTRVAL*,ULONG* count) override { *count=0; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetEndACP(LONG* end) override { *end=static_cast<LONG>(text.size()); return locked?S_OK:TS_E_NOLOCK; }
    HRESULT STDMETHODCALLTYPE GetActiveView(TsViewCookie* view) override { *view=0; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetACPFromPoint(TsViewCookie,const POINT*,DWORD,LONG*) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetTextExt(TsViewCookie,LONG,LONG,RECT* rect,BOOL* clipped) override { *rect={0,0,100,20}; *clipped=FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetScreenExt(TsViewCookie,RECT* rect) override { *rect={0,0,100,20}; return S_OK; }
    HRESULT STDMETHODCALLTYPE GetWnd(TsViewCookie,HWND* hwnd) override { *hwnd=window; return S_OK; }
};

class Session final : public ITfEditSession, public ITfCompositionSink {
    std::atomic<ULONG> refs{1};
public:
    ComPtr<ITfContext> context;
    ComPtr<ITfComposition>* composition;
    std::string operation;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void** out) override {
        *out=nullptr; if (iid==IID_IUnknown || iid==IID_ITfEditSession) {
            *out=static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
        } else if (iid==IID_ITfCompositionSink) {
            *out=static_cast<ITfCompositionSink*>(this); AddRef(); return S_OK;
        } return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs; }
    ULONG STDMETHODCALLTYPE Release() override { auto n=--refs; if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE OnCompositionTerminated(TfEditCookie,ITfComposition*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie cookie) override {
        if (operation=="begin") {
            TF_SELECTION selection{}; ULONG count=0;
            HRESULT hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&count);
            if (FAILED(hr) || !count) return E_FAIL;
            ComPtr<ITfRange> range; range.Attach(selection.range);
            ComPtr<ITfContextComposition> service;
            hr=context.As(&service);
            if (FAILED(hr)) return hr;
            return service->StartComposition(cookie,range.Get(),static_cast<ITfCompositionSink*>(this),composition->ReleaseAndGetAddressOf());
        }
        if (!*composition) return E_UNEXPECTED;
        ComPtr<ITfRange> range;
        HRESULT hr=(*composition)->GetRange(&range);
        if (FAILED(hr)) return hr;
        if (operation=="cancel") {
            hr=range->SetText(cookie,0,L"",0);
            if (FAILED(hr)) return hr;
        }
        hr=(*composition)->EndComposition(cookie); composition->Reset(); return hr;
    }
};

int main() {
    if (FAILED(CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED))) return 1;
    ComPtr<ITfThreadMgr> manager; ComPtr<ITfDocumentMgr> doc; ComPtr<ITfContext> context; ComPtr<ITfComposition> composition;
    ComPtr<Store> store; store.Attach(new Store());
    store->window=CreateWindowExW(0,L"EDIT",L"",0,0,0,100,20,nullptr,nullptr,GetModuleHandleW(nullptr),nullptr);
    store->sync_window();
    TfClientId client=0; TfEditCookie cookie=0;
    HRESULT hr=CoCreateInstance(CLSID_TF_ThreadMgr,nullptr,CLSCTX_INPROC_SERVER,IID_PPV_ARGS(&manager));
    if (SUCCEEDED(hr)) hr=manager->Activate(&client);
    if (SUCCEEDED(hr)) hr=manager->CreateDocumentMgr(&doc);
    if (SUCCEEDED(hr)) hr=doc->CreateContext(client,0,static_cast<ITextStoreACP*>(store.Get()),&context,&cookie);
    if (SUCCEEDED(hr)) hr=doc->Push(context.Get());
    if (SUCCEEDED(hr)) hr=manager->SetFocus(doc.Get());
    if (FAILED(hr)) { std::cout<<"ERROR "<<std::hex<<hr<<std::endl; return 1; }
    std::cout<<"READY "<<GetCurrentProcessId()<<" "<<GetCurrentThreadId()<<" "<<reinterpret_cast<UINT_PTR>(store->window)<<std::endl;
    bool running=true;
    while (running) {
        MSG message{};
        while (PeekMessageW(&message,nullptr,0,0,PM_REMOVE)) { TranslateMessage(&message); DispatchMessageW(&message); }
        DWORD bytes=0;
        if (PeekNamedPipe(GetStdHandle(STD_INPUT_HANDLE),nullptr,0,nullptr,&bytes,nullptr) && bytes) {
            std::string operation; std::getline(std::cin,operation);
            if (operation=="exit") running=false;
            else {
                ComPtr<Session> session; session.Attach(new Session());
                session->context=context; session->composition=std::addressof(composition); session->operation=operation;
                HRESULT result=E_FAIL;
                hr=context->RequestEditSession(client,session.Get(),TF_ES_SYNC|TF_ES_READWRITE,&result);
                std::cout<<"RESULT "<<std::hex<<hr<<" "<<result<<std::dec<<std::endl;
            }
        }
        Sleep(5);
    }
    manager->SetFocus(nullptr); composition.Reset(); doc->Pop(TF_POPF_ALL);
    context.Reset(); doc.Reset(); manager->Deactivate(); manager.Reset();
    DestroyWindow(store->window); store.Reset(); CoUninitialize(); return 0;
}
