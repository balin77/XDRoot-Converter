<#
.SYNOPSIS
  Laedt FFmpeg und bmx nach tools\ffmpeg bzw. tools\bmx (Windows PowerShell 5.1).
.DESCRIPTION
  FFmpeg: aktuelles "essentials_build.zip" aus den GitHub-Releases von GyanD/codexffmpeg.
  bmx:    "bmx-win64-binary-<version>.zip" aus den GitHub-Releases von bbc/bmx.
  Bereits vorhandene Programme werden uebersprungen.
.PARAMETER ToolsDir
  Zielverzeichnis (Standard: tools neben diesem Skript).
#>
param(
    [string]$ToolsDir = (Join-Path $PSScriptRoot 'tools')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # beschleunigt Invoke-WebRequest in PS 5.1 stark
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$headers = @{ 'User-Agent' = 'XDROOT-Converter-setup' }

function Test-Programs([string]$Dir, [string[]]$Names) {
    if (-not (Test-Path -LiteralPath $Dir)) { return $false }
    foreach ($n in $Names) {
        $hit = Get-ChildItem -LiteralPath $Dir -Recurse -Filter $n -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $hit) { return $false }
    }
    return $true
}

function Get-ReleaseAsset([string]$Repo, [string]$Pattern) {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $headers
    $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
    if (-not $asset) { throw "Kein passendes Asset ($Pattern) in $Repo gefunden." }
    return $asset
}

function Install-Zip([string]$Name, [string]$Repo, [string]$Pattern, [string]$Dest, [string[]]$Programs) {
    if (Test-Programs $Dest $Programs) {
        Write-Host "[$Name] bereits vorhanden in $Dest - uebersprungen."
        return
    }
    Write-Host "[$Name] suche aktuelles Release in $Repo ..."
    $asset = Get-ReleaseAsset $Repo $Pattern
    $sizeMb = [math]::Round($asset.size / 1MB, 1)
    Write-Host "[$Name] lade $($asset.name) ($sizeMb MB) ..."
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    $zip = Join-Path ([IO.Path]::GetTempPath()) ("xdroot_setup_" + [guid]::NewGuid().ToString('N') + '.zip')
    try {
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -Headers $headers -UseBasicParsing
        Write-Host "[$Name] entpacke nach $Dest ..."
        Expand-Archive -LiteralPath $zip -DestinationPath $Dest -Force
    } finally {
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue }
    }
}

$ffmpegDir = Join-Path $ToolsDir 'ffmpeg'
$bmxDir = Join-Path $ToolsDir 'bmx'
$ffProgs = @('ffmpeg.exe', 'ffprobe.exe')
$bmxProgs = @('raw2bmx.exe', 'mxf2raw.exe')

try {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    Install-Zip 'FFmpeg' 'GyanD/codexffmpeg' '^ffmpeg-.*-essentials_build\.zip$' $ffmpegDir $ffProgs
    Install-Zip 'bmx' 'bbc/bmx' '^bmx-win64-binary-.*\.zip$' $bmxDir $bmxProgs
} catch {
    Write-Host ""
    Write-Host "FEHLER: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Pruefe die Internetverbindung oder lade die Programme manuell (siehe README)."
    exit 1
}

Write-Host ""
Write-Host "Pruefung:"
$ok = $true
foreach ($pair in @(@($ffmpegDir, $ffProgs), @($bmxDir, $bmxProgs))) {
    foreach ($n in $pair[1]) {
        $hit = Get-ChildItem -LiteralPath $pair[0] -Recurse -Filter $n -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) {
            Write-Host "  OK       $n  ->  $($hit.FullName)"
        } else {
            Write-Host "  FEHLT    $n" -ForegroundColor Red
            $ok = $false
        }
    }
}
if (-not $ok) { exit 1 }
Write-Host ""
Write-Host "Fertig. XDROOT-Converter.bat kann jetzt benutzt werden."
exit 0
