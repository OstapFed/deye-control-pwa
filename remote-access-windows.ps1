[CmdletBinding()]
param([switch]$Disable)
# Private HTTPS access to the DEYE panel from your phone anywhere, via Tailscale.
# Nothing is opened to the public internet: only devices signed in to YOUR Tailscale account can connect.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)

function Find-Tailscale {
 $cmd=Get-Command tailscale.exe -ErrorAction SilentlyContinue
 if ($cmd) {return $cmd.Source}
 foreach ($p in @("$env:ProgramFiles\Tailscale\tailscale.exe","${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {if (Test-Path $p) {return $p}}
 return $null
}

$ts=Find-Tailscale
if ($Disable) {
 if ($ts) {& $ts serve reset; Write-Host 'Remote access disabled.' -ForegroundColor Green}
 return
}

Write-Host '[1/4] Checking the DEYE server...'
try {Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/health' -TimeoutSec 3 | Out-Null}
catch {throw 'The DEYE server is not running. Run connect-windows.ps1 first or open the DEYE Control shortcut.'}

Write-Host '[2/4] Installing Tailscale (free, Windows will ask for permission)...'
if (-not $ts) {
 $winget=Get-Command winget.exe -ErrorAction SilentlyContinue
 if (-not $winget) {Start-Process 'https://tailscale.com/download/windows'; throw 'Install Tailscale from the opened page, then run this script again.'}
 & $winget.Source install --id Tailscale.Tailscale --exact --silent --accept-package-agreements --accept-source-agreements
 Start-Sleep -Seconds 5
 $ts=Find-Tailscale
 if (-not $ts) {throw 'Tailscale was installed. Reopen PowerShell and run this script again.'}
}

Write-Host '[3/4] Signing in to Tailscale (a browser window may open; use Apple, Google or Microsoft)...'
$state=(& $ts status --json | ConvertFrom-Json)
if ($state.BackendState -ne 'Running') {& $ts up; $state=(& $ts status --json | ConvertFrom-Json)}
if ($state.BackendState -ne 'Running') {throw 'Tailscale is not connected yet. Finish the sign-in and run this script again.'}

Write-Host '[4/4] Publishing the panel privately over HTTPS...'
Write-Host 'If Tailscale asks to enable HTTPS certificates, open the printed link, click Enable, then run this script again.' -ForegroundColor Yellow
& $ts serve --bg 8080
if ($LASTEXITCODE -ne 0) {throw 'tailscale serve failed. Enable MagicDNS and HTTPS in the Tailscale admin console (DNS page), then retry.'}
$name=$state.Self.DNSName.TrimEnd('.')
$url='https://'+$name+'/'
$secret=(Get-Content -Raw -Encoding UTF8 (Join-Path $env:LOCALAPPDATA 'DeyeControl\data\server-password.txt')).Trim()

Write-Host ''
Write-Host 'Remote access is ready.' -ForegroundColor Green
Write-Host ('Panel address: '+$url) -ForegroundColor Cyan
Write-Host ('Server password (enter once on the phone): '+$secret) -ForegroundColor Cyan
Write-Host ''
Write-Host 'On the iPhone:'
Write-Host ' 1. Install "Tailscale" from the App Store and sign in with the SAME account.'
Write-Host ' 2. Open the panel address in Safari, go to the "Zvyazok" tab, choose the real server, enter the password.'
Write-Host ' 3. Share -> Add to Home Screen. The DEYE icon now works from anywhere.'
Write-Host 'Disable later with: remote-access-windows.ps1 -Disable'
