[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$LoggerHost,[Parameter(Mandatory=$true)][UInt32]$LoggerSerial,[string]$ExpectedInverterSerial,[string]$SourceRef='main')
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
if ($SourceRef -ne 'main' -and $SourceRef -notmatch '^[0-9a-f]{40}$') {throw 'SourceRef must be main or a full commit SHA.'}
Write-Host ('DEYE update source: '+$SourceRef) -ForegroundColor Cyan
$deyeRoot=Join-Path $env:LOCALAPPDATA 'DeyeControl'
$deyeSource=Join-Path $deyeRoot 'source'
$deyePython=Join-Path $deyeRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $deyePython)) {throw 'Run install-windows.ps1 first.'}
# Stop only our previously launched gateway; leave unrelated Python applications alone.
$deyeAppPath=Join-Path $deyeSource 'gateway\app.py'
$deyeAppPattern=[regex]::Escape($deyeAppPath)+'(?:"|\s|$)'
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {
 $_.CommandLine -and $_.CommandLine -match $deyeAppPattern
} | ForEach-Object {
 Write-Host ('Stopping previous DEYE gateway PID '+$_.ProcessId)
 Stop-Process -Id $_.ProcessId -ErrorAction Stop
}
Start-Sleep -Seconds 2
$env:PYTHONIOENCODING='utf-8'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$deyeTemp=Join-Path ([IO.Path]::GetTempPath()) ('deye-connect-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $deyeTemp | Out-Null
$deyeArchiveUrl=if ($SourceRef -eq 'main') {'https://github.com/OstapFed/deye-control-pwa/archive/refs/heads/main.zip'} else {'https://github.com/OstapFed/deye-control-pwa/archive/'+$SourceRef+'.zip'}
Invoke-WebRequest -UseBasicParsing $deyeArchiveUrl -OutFile (Join-Path $deyeTemp 'source.zip')
Expand-Archive (Join-Path $deyeTemp 'source.zip') -DestinationPath $deyeTemp
Get-ChildItem (Join-Path $deyeTemp ('deye-control-pwa-'+$SourceRef)) -Force | Copy-Item -Destination $deyeSource -Recurse -Force
$env:DEYE_DATA=Join-Path $deyeRoot 'data'
$arguments=@((Join-Path $deyeSource 'gateway\configure_deye.py'),'--host',$LoggerHost,'--serial',"$LoggerSerial",'--apply')
if ($ExpectedInverterSerial) {$arguments+=@('--expected-inverter-serial',$ExpectedInverterSerial)}
$deyeReport=Join-Path $deyeRoot 'live-connection-check.txt'
& $deyePython @arguments 2>&1 | Tee-Object -FilePath $deyeReport
if ($LASTEXITCODE -ne 0) {throw ('Read-only profile check failed. Detailed report: '+$deyeReport)}
# Fresh local-only password, not printed or saved. It is passed to the local page in a fragment.
$env:DEYE_PASSWORD=[Guid]::NewGuid().ToString('N')+[Guid]::NewGuid().ToString('N')
$env:DEYE_BIND='127.0.0.1'
$encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($env:DEYE_PASSWORD))
$server=Start-Process -FilePath $deyePython -ArgumentList @(('"'+(Join-Path $deyeSource 'gateway\app.py')+'"')) -PassThru
try {
 $ready=$false
 for ($i=0;$i -lt 20;$i++) {
  if ($server.HasExited) {throw 'Server exited. Check its console window.'}
  try { $response=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8080/manifest.webmanifest' -TimeoutSec 1; $ready=($response.StatusCode -eq 200) } catch {}
  if ($ready) {break}
  Start-Sleep -Milliseconds 500
 }
 if (-not $ready) {throw 'The local server did not start. Check port 8080.'}
 Start-Process ('http://127.0.0.1:8080/#deye-local='+$encoded)
 Write-Host 'Local DEYE panel opened. Keep the Python server window running.' -ForegroundColor Green
 Write-Host 'Compare readings with the inverter display; all inverter writes remain disabled.'
 Write-Host 'Phone access over the internet still requires HTTPS/VPN setup.'
} finally {Remove-Item Env:DEYE_PASSWORD -ErrorAction SilentlyContinue}
