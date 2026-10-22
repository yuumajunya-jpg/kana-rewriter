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
    bool asynchronous_locks=false;
    DWORD pending_lock=0;
    bool stall_read_once=false;
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
        if (asynchronous_locks) {
            if (flags&TS_LF_SYNC) { *result=TS_E_SYNCHRONOUS; return S_OK; }
            pending_lock=flags; *result=TS_S_ASYNC;
            PostThreadMessageW(GetCurrentThreadId(),WM_APP+42,0,0);
            return S_OK;
        }
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
        if (stall_read_once) { stall_read_once=false; Sleep(400); } // test-only slow validation
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
        if (operation=="noop") return S_OK;
        if (operation=="insertmarkers" || operation=="insertmarkersbeforecaret" ||
            operation=="insertmarkersinside" || operation=="insertwrongmarkers" || operation=="removemarkers" ||
            operation=="insertmarkerslf" || operation=="insertmarkerslfbeforecaret" ||
            operation=="insertmarkerslfinside" || operation=="insertmarkersdoublelf" || operation=="insertmarkerscr" ||
            operation=="insertordinary" || operation=="insertemoji" || operation=="insertlong" ||
            operation=="insertordinarybeforecaret") {
            TF_SELECTION selection{}; ULONG count=0;
            HRESULT hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&count);
            ComPtr<ITfRange> range; range.Attach(selection.range);
            if (FAILED(hr) || count!=1 || !range) return E_FAIL;
            ComPtr<ITfRange> caret;
            hr=range->Clone(&caret);
            if (SUCCEEDED(hr)) hr=caret->SetGravity(cookie,TF_GRAVITY_BACKWARD,TF_GRAVITY_BACKWARD);
            if (operation=="removemarkers") {
                LONG shifted=0;
                if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,-2,&shifted,nullptr);
                wchar_t text[2]{}; ULONG length=0;
                if (SUCCEEDED(hr)) hr=range->GetText(cookie,0,text,2,&length);
                if (FAILED(hr) || shifted!=-2 || length!=2 || text[0]!=L'\x200b' || text[1]!=L'\x200c') return E_FAIL;
                return range->SetText(cookie,0,L"",0);
            }
            if (operation=="insertmarkersinside" || operation=="insertmarkerslfinside") {
                LONG shifted=0;
                if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,-2,&shifted,nullptr);
                if (SUCCEEDED(hr)) hr=range->Collapse(cookie,TF_ANCHOR_START);
                if (SUCCEEDED(hr) && shifted!=-2) hr=E_FAIL;
            }
            const wchar_t* text=operation=="insertwrongmarkers"?L"\x200c\x200b":L"\x200b\x200c";
            if (operation=="insertmarkerslf" || operation=="insertmarkerslfbeforecaret" || operation=="insertmarkerslfinside") text=L"\x200b\x200c\n";
            if (operation=="insertmarkersdoublelf") text=L"\x200b\x200c\n\n";
            if (operation=="insertmarkerscr") text=L"\x200b\x200c\r";
            std::wstring long_text(1100,L'追');
            if (operation=="insertordinary" || operation=="insertordinarybeforecaret") text=L"続きの文章";
            if (operation=="insertemoji") text=L"\U0001f642\u2060\n";
            if (operation=="insertlong") text=long_text.c_str();
            if (SUCCEEDED(hr)) hr=range->SetText(cookie,0,text,static_cast<LONG>(wcslen(text)));
            if (operation=="insertmarkersbeforecaret" || operation=="insertmarkerslfbeforecaret" || operation=="insertordinarybeforecaret") {
                TF_SELECTION selected{caret.Get(),{TF_AE_END,FALSE}};
                if (SUCCEEDED(hr)) hr=context->SetSelection(cookie,1,&selected);
            }
            return hr;
        }
        if (operation=="replaceprefix" || operation=="insertprefix" || operation=="replaceleftcontext" ||
            operation=="replacerightcontext" || operation=="replacesuffix" || operation=="deletesuffix") {
            TF_SELECTION selection{}; ULONG count=0;
            HRESULT hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&count);
            ComPtr<ITfRange> caret; caret.Attach(selection.range);
            if (FAILED(hr) || count!=1 || !caret) return E_FAIL;
            ComPtr<ITfRange> range;
            LONG offset=operation=="replaceleftcontext"?8:operation=="replacerightcontext"?18:
                        (operation=="replacesuffix" || operation=="deletesuffix")?25:0;
            hr=context->GetStart(cookie,&range);
            LONG shifted=0;
            if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,offset,&shifted,nullptr);
            if (FAILED(hr) || shifted!=offset) return E_FAIL;
            hr=range->Collapse(cookie,TF_ANCHOR_START);
            if (operation!="insertprefix" && SUCCEEDED(hr)) hr=range->ShiftEnd(cookie,1,&shifted,nullptr);
            if (SUCCEEDED(hr)) hr=range->SetText(cookie,0,L"別",operation=="deletesuffix"?0:1);
            TF_SELECTION selected{caret.Get(),{TF_AE_END,FALSE}};
            return SUCCEEDED(hr)?context->SetSelection(cookie,1,&selected):hr;
        }
        if (operation=="insertlinebefore" || operation=="insertlettersbefore") {
            ComPtr<ITfRange> range;
            HRESULT hr=context->GetEnd(cookie,&range);
            LONG shifted=0;
            if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,-3,&shifted,nullptr);
            if (SUCCEEDED(hr) && shifted!=-3) hr=E_FAIL;
            if (SUCCEEDED(hr)) hr=range->Collapse(cookie,TF_ANCHOR_START);
            const wchar_t* inserted=operation=="insertlinebefore"?L"\r\n":L"追加";
            if (SUCCEEDED(hr)) hr=range->SetText(cookie,0,inserted,2);
            range.Reset();
            if (SUCCEEDED(hr)) hr=context->GetEnd(cookie,&range);
            TF_SELECTION selected{range.Get(),{TF_AE_END,FALSE}};
            return SUCCEEDED(hr)?context->SetSelection(cookie,1,&selected):hr;
        }
        if (operation=="propertycycle") {
            ComPtr<ITfRange> range,end;
            HRESULT hr=context->GetStart(cookie,&range);
            if (SUCCEEDED(hr)) hr=context->GetEnd(cookie,&end);
            if (SUCCEEDED(hr)) hr=range->ShiftStartToRange(cookie,end.Get(),TF_ANCHOR_END);
            LONG shifted=0;
            if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,-3,&shifted,nullptr);
            ComPtr<ITfContextComposition> service;
            ComPtr<ITfComposition> temporary;
            if (SUCCEEDED(hr)) hr=context.As(&service);
            if (SUCCEEDED(hr)) hr=service->StartComposition(cookie,range.Get(),
                static_cast<ITfCompositionSink*>(this),&temporary);
            return SUCCEEDED(hr)?temporary->EndComposition(cookie):hr;
        }
        if (operation=="reselect" || operation=="movecaret" || operation=="moveleft" ||
            operation=="reverse" || operation=="sametext" || operation=="differenttext" ||
            operation=="refreshtext" || operation=="refreshdocument" || operation=="changecontext") {
            TF_SELECTION selection{}; ULONG count=0;
            HRESULT hr=context->GetSelection(cookie,TF_DEFAULT_SELECTION,1,&selection,&count);
            if (FAILED(hr) || !count) return E_FAIL;
            ComPtr<ITfRange> range; range.Attach(selection.range);
            if (operation=="changecontext") {
                ComPtr<ITfRange> first;
                hr=context->GetStart(cookie,&first);
                LONG shifted=0;
                if (SUCCEEDED(hr)) hr=first->ShiftEnd(cookie,1,&shifted,nullptr);
                return SUCCEEDED(hr)?first->SetText(cookie,0,L"別",1):hr;
            }
            if (operation=="refreshtext" || operation=="refreshdocument") {
                ComPtr<ITfRange> first,last;
                hr=context->GetStart(cookie,&first);
                if (SUCCEEDED(hr)) hr=context->GetEnd(cookie,&last);
                if (SUCCEEDED(hr)) hr=first->ShiftEndToRange(cookie,last.Get(),TF_ANCHOR_END);
                if (operation=="refreshtext" && SUCCEEDED(hr)) {
                    hr=first->Collapse(cookie,TF_ANCHOR_END);
                    LONG shifted=0;
                    if (SUCCEEDED(hr)) hr=first->ShiftStart(cookie,-3,&shifted,nullptr);
                    if (SUCCEEDED(hr) && shifted!=-3) hr=E_FAIL;
                }
                wchar_t text[2048]{}; ULONG count=0;
                if (SUCCEEDED(hr)) hr=first->GetText(cookie,0,text,2048,&count);
                if (SUCCEEDED(hr)) hr=first->SetText(cookie,0,text,count);
                if (SUCCEEDED(hr)) hr=first->Collapse(cookie,TF_ANCHOR_END);
                TF_SELECTION selected{first.Get(),{TF_AE_END,FALSE}};
                return SUCCEEDED(hr)?context->SetSelection(cookie,1,&selected):hr;
            }
            if (operation=="differenttext") return range->SetText(cookie,0,L"変更",2);
            if (operation=="sametext") return range->SetText(cookie,0,L"さんぽ",3);
            if (operation=="movecaret") {
                hr=range->Collapse(cookie,TF_ANCHOR_START);
                if (FAILED(hr)) return hr;
            }
            if (operation=="moveleft") {
                LONG shifted=0;
                hr=range->ShiftStart(cookie,-1,&shifted,nullptr);
                if (SUCCEEDED(hr)) hr=range->Collapse(cookie,TF_ANCHOR_START);
                if (FAILED(hr) || shifted!=-1) return E_FAIL;
            }
            if (operation=="reverse") selection.style.ase=TF_AE_START;
            return context->SetSelection(cookie,1,&selection);
        }
        if (operation=="reset" || operation=="paragraph" || operation=="paragraph2" || operation=="blankline" || operation=="reported" || operation=="reportedfirst" || operation=="contextcase" || operation=="unicodecontext" || operation=="longcontext") {
            if (*composition) return E_UNEXPECTED;
            ComPtr<ITfRange> range,end;
            HRESULT hr=context->GetStart(cookie,&range);
            if (SUCCEEDED(hr)) hr=context->GetEnd(cookie,&end);
            if (SUCCEEDED(hr)) hr=range->ShiftEndToRange(cookie,end.Get(),TF_ANCHOR_END);
            std::wstring text=operation=="paragraph"?L"前の行\rさんぽ":
                operation=="paragraph2"?L"前の行\r散歩さんぽ":
                operation=="blankline"?L"前の行\r\rさんぽ":
                operation=="reported"?std::wstring(9,L'a')+L"へんかんてすと\r\n":
                operation=="reportedfirst"?L"へんかんてすと\n":
                operation=="contextcase"?std::wstring(10,L'a')+L"へんかんてすと"+std::wstring(10,L'z'):
                operation=="unicodecontext"?L"\U0001f642\r\nへんかんてすと\U0001f642\r\n":
                operation=="longcontext"?[](){std::wstring p; for(int i=0;i<500;++i) p+=L"\U0001f642\r\n"; return p+L"へんかんてすと"+p;}():L"さんぽ";
            if (SUCCEEDED(hr)) hr=range->SetText(cookie,0,text.c_str(),static_cast<LONG>(text.size()));
            if (FAILED(hr)) return hr;
            if (operation!="reset") {
                hr=range->Collapse(cookie,TF_ANCHOR_END);
                LONG shifted=0;
                LONG width=operation=="reported"?9:operation=="reportedfirst"?8:
                    operation=="contextcase"?17:operation=="unicodecontext"?11:operation=="longcontext"?2007:3;
                if (SUCCEEDED(hr)) hr=range->ShiftStart(cookie,-width,&shifted,nullptr);
                if (FAILED(hr) || shifted!=-width) return E_FAIL;
                if (operation=="reported" || operation=="reportedfirst" || operation=="contextcase" || operation=="unicodecontext" || operation=="longcontext") {
                    LONG suffix=operation=="reportedfirst"?1:operation=="contextcase"?10:operation=="unicodecontext"?4:operation=="longcontext"?2000:2;
                    hr=range->ShiftEnd(cookie,-suffix,&shifted,nullptr);
                    if (FAILED(hr) || shifted!=-suffix) return E_FAIL;
                }
            }
            TF_SELECTION selected{range.Get(),{TF_AE_END,FALSE}};
            return context->SetSelection(cookie,1,&selected);
        }
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
        if (operation=="update") return range->SetText(cookie,0,L"さんぽ",3);
        if (operation=="cancel") {
            hr=range->SetText(cookie,0,L"",0);
            if (FAILED(hr)) return hr;
        }
        hr=(*composition)->EndComposition(cookie); composition->Reset();
        if (SUCCEEDED(hr) && operation=="commitcaret") {
            hr=range->Collapse(cookie,TF_ANCHOR_END);
            TF_SELECTION selection{range.Get(),{TF_AE_END,FALSE}};
            if (SUCCEEDED(hr)) hr=context->SetSelection(cookie,1,&selection);
        }
        return hr;
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
        while (PeekMessageW(&message,nullptr,0,0,PM_REMOVE)) {
            if (message.message==WM_APP+42 && store->pending_lock) {
                DWORD flags=store->pending_lock; store->pending_lock=0;
                store->locked=true; store->sink->OnLockGranted(flags); store->locked=false;
            } else { TranslateMessage(&message); DispatchMessageW(&message); }
        }
        DWORD bytes=0;
        if (PeekNamedPipe(GetStdHandle(STD_INPUT_HANDLE),nullptr,0,nullptr,&bytes,nullptr) && bytes) {
            std::string operation; std::getline(std::cin,operation);
            if (operation=="exit") running=false;
            else if (operation=="async") { store->asynchronous_locks=true; std::cout<<"RESULT 0 0"<<std::endl; }
            else if (operation=="stallread") { store->stall_read_once=true; std::cout<<"RESULT 0 0"<<std::endl; }
            else if (operation=="stall") {
                std::cout<<"RESULT 0 0"<<std::endl;
                Sleep(200); // test-only blocked target, never an editor delay
            }
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
