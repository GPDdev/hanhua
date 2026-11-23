param([Parameter(Mandatory=$true)][string]$Jobs)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null=[Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$streamType=[Windows.Media.SpeechSynthesis.SpeechSynthesisStream,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$asTask=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation' + [char]96 + '1' } | Select-Object -First 1
$synth=New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
try {
    $rows=Get-Content -Raw -Encoding utf8 -LiteralPath $Jobs | ConvertFrom-Json
    foreach($row in $rows){
        $voices=@([Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -like ($row.lang+'*') } | Sort-Object DisplayName)
        if(-not $voices.Count){
            Add-Type -AssemblyName System.Speech
            $sapi=New-Object System.Speech.Synthesis.SpeechSynthesizer
            try {
                $available=@($sapi.GetInstalledVoices() | Where-Object {$_.Enabled -and $_.VoiceInfo.Culture.Name -like ($row.lang+'*')})
                if(-not $available.Count){throw "No installed voice for $($row.lang)"}
                $sapi.SelectVoice($available[([int]$row.voice % $available.Count)].VoiceInfo.Name)
                $sapi.SetOutputToWaveFile($row.path)
                $sapi.Speak([string]$row.text)
            } finally { $sapi.Dispose() }
            continue
        }
        $synth.Voice=$voices[([int]$row.voice % $voices.Count)]
        $op=$synth.SynthesizeTextToStreamAsync([string]$row.text)
        $task=$asTask.MakeGenericMethod($streamType).Invoke($null,@($op))
        $stream=$task.GetAwaiter().GetResult()
        $reader=[System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)
        $file=[IO.File]::Open($row.path,[IO.FileMode]::Create)
        try { $reader.CopyTo($file) } finally { $file.Dispose(); $reader.Dispose(); $stream.Dispose() }
    }
} finally { $synth.Dispose() }
