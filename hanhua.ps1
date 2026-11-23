$ErrorActionPreference = 'Stop'
$base = if ($env:HANHUA_HOME) { $env:HANHUA_HOME } else { Join-Path $env:LOCALAPPDATA 'hanhua' }
$runtime = Join-Path $base 'runtime'
$python = Join-Path $runtime 'Scripts\python.exe'
$requirements = Join-Path $PSScriptRoot 'scripts\requirements.txt'
$stamp = Join-Path $runtime 'hanhua-requirements.sha256'
$hash = (Get-FileHash -LiteralPath $requirements).Hash
$uv = Get-Command uv.exe -ErrorAction SilentlyContinue
if (-not (Test-Path -LiteralPath $python)) {
    if ($uv) { & $uv.Source venv --python 3.11 $runtime }
    else { & py -3.11 -m venv $runtime }
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
}
if (-not (Test-Path -LiteralPath $stamp) -or (Get-Content -Raw -LiteralPath $stamp).Trim() -ne $hash) {
    if ($uv) { & $uv.Source pip install --python $python -r $requirements }
    else { & $python -m pip install -r $requirements }
    if ($LASTEXITCODE) { exit $LASTEXITCODE }
    [IO.File]::WriteAllText($stamp, $hash)
}
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $python (Join-Path $PSScriptRoot 'scripts\hanhua.py') @args
exit $LASTEXITCODE
