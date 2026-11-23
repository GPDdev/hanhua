// Stream the selected process tree's PCM audio to stdout (16 kHz, mono, signed 16 bit).
#define NOMINMAX
#include <windows.h>
#include <audioclientactivationparams.h>
#include <audioclient.h>
#include <mmdeviceapi.h>
#include <wrl.h>
#include <cstdio>
#include <vector>
#include <fcntl.h>
#include <io.h>
using namespace Microsoft::WRL;

class Activated final : public RuntimeClass<RuntimeClassFlags<ClassicCom>, IActivateAudioInterfaceCompletionHandler, FtmBase> {
public:
    HANDLE ready = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    HRESULT status = E_FAIL;
    ComPtr<IAudioClient> client;
    ~Activated() { CloseHandle(ready); }
    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation* operation) override {
        ComPtr<IUnknown> object;
        HRESULT result = E_FAIL;
        status = operation->GetActivateResult(&result, &object);
        if (SUCCEEDED(status)) status = result;
        if (SUCCEEDED(status)) status = object.As(&client);
        SetEvent(ready);
        return S_OK;
    }
};
static HRESULT stream(DWORD pid) {
    auto activated = Make<Activated>();
    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT variant{};
    variant.vt=VT_BLOB;
    variant.blob.cbSize=sizeof(params);
    variant.blob.pBlobData=reinterpret_cast<BYTE*>(&params);
    ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
    HRESULT hr=ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,__uuidof(IAudioClient),&variant,activated.Get(),&operation);
    if (FAILED(hr)) return hr;
    if (WaitForSingleObject(activated->ready,15000)!=WAIT_OBJECT_0) return HRESULT_FROM_WIN32(ERROR_TIMEOUT);
    if (FAILED(activated->status)) return activated->status;
    auto audio=activated->client;
    WAVEFORMATEX format{WAVE_FORMAT_PCM,1,16000,32000,2,16,0};
    hr=audio->Initialize(AUDCLNT_SHAREMODE_SHARED,AUDCLNT_STREAMFLAGS_LOOPBACK|AUDCLNT_STREAMFLAGS_EVENTCALLBACK|AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,0,0,&format,nullptr);
    if (FAILED(hr)) return hr;
    HANDLE signal=CreateEventW(nullptr,FALSE,FALSE,nullptr);
    ComPtr<IAudioCaptureClient> recorder;
    hr=audio->SetEventHandle(signal);
    if (SUCCEEDED(hr)) hr=audio->GetService(IID_PPV_ARGS(&recorder));
    if (SUCCEEDED(hr)) hr=audio->Start();
    std::vector<BYTE> silence;
    while (SUCCEEDED(hr)) {
        if (WaitForSingleObject(signal,1000)!=WAIT_OBJECT_0) continue;
        UINT32 count=0;
        hr=recorder->GetNextPacketSize(&count);
        while (SUCCEEDED(hr) && count) {
            BYTE* data=nullptr; UINT32 frames=0; DWORD flags=0;
            hr=recorder->GetBuffer(&data,&frames,&flags,nullptr,nullptr);
            if (FAILED(hr)) break;
            const size_t bytes=size_t(frames)*format.nBlockAlign;
            if (flags&AUDCLNT_BUFFERFLAGS_SILENT) { silence.assign(bytes,0); data=silence.data(); }
            const bool wrote=std::fwrite(data,1,bytes,stdout)==bytes && std::fflush(stdout)==0;
            hr=recorder->ReleaseBuffer(frames);
            if (!wrote) { hr=HRESULT_FROM_WIN32(ERROR_BROKEN_PIPE); break; }
            if (SUCCEEDED(hr)) hr=recorder->GetNextPacketSize(&count);
        }
    }
    audio->Stop(); CloseHandle(signal);
    return hr;
}
int wmain(int argc,wchar_t** argv) {
    if (argc!=2) return 2;
    DWORD pid=wcstoul(argv[1],nullptr,10);
    if (!pid) return 2;
    _setmode(_fileno(stdout),_O_BINARY);
    CoInitializeEx(nullptr,COINIT_MULTITHREADED);
    HRESULT hr=stream(pid);
    if (FAILED(hr)) std::fprintf(stderr,"Process loopback HRESULT 0x%08lx\n",static_cast<unsigned long>(hr));
    CoUninitialize();
    return FAILED(hr)?1:0;
}
