param([string]$Destination)
$ErrorActionPreference = 'Stop'
if (-not $Destination) {
    $codexDir = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
    $Destination = Join-Path $codexDir 'skills\hanhua'
}
$dest = [IO.Path]::GetFullPath($Destination)
if ($dest -eq [IO.Path]::GetFullPath($PSScriptRoot)) { throw 'Choose a separate install directory.' }
if ((Test-Path -LiteralPath $dest) -and -not (Test-Path -LiteralPath (Join-Path $dest '.hanhua-install'))) { throw "Existing unmanaged directory: $dest" }
$parent = Split-Path -Parent $dest
$stage = Join-Path $parent ('hanhua-install-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $stage -Force | Out-Null
foreach ($name in 'SKILL.md','hanhua.ps1','LICENSE') {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $stage
}
foreach ($name in 'agents','scripts','references') {
    $files=Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot $name) -Recurse -File
    foreach($file in $files) {
        if ($file.Extension -in '.pyc','.pyo' -or $file.FullName -match '[\\/]__pycache__[\\/]') {continue}
        $relative=$file.FullName.Substring($PSScriptRoot.Length+1)
        $target=Join-Path $stage $relative
        New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($target)) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $target
    }
}
[IO.File]::WriteAllText((Join-Path $stage '.hanhua-install'), 'hanhua')
$previous = $null
if (Test-Path -LiteralPath $dest) {
    $previous = Join-Path $parent ('hanhua-previous-' + [guid]::NewGuid())
    Move-Item -LiteralPath $dest -Destination $previous
}
try { Move-Item -LiteralPath $stage -Destination $dest }
catch { if ($previous) { Move-Item -LiteralPath $previous -Destination $dest }; throw }
if ($previous -and ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($previous)) -eq $parent) -and (Test-Path -LiteralPath (Join-Path $previous '.hanhua-install'))) { Remove-Item -LiteralPath $previous -Force -Recurse }
Write-Output $dest
