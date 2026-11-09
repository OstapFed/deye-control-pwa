[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$LoggerHost,
 [Parameter(Mandatory=$true)][UInt32]$LoggerSerial,
 [string]$ExpectedInverterSerial,
 [string]$SourceRef='main',
 [switch]$NoAutostart
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
if ($SourceRef -ne 'main' -and $SourceRef -notmatch '^[0-9a-f]{40}$') {throw 'SourceRef must be main or a full commit SHA.'}
Write-Host ('DEYE update source: '+$SourceRef) -ForegroundColor Cyan
$deyeRoot=Join-Path $env:LOCALAPPDATA 'DeyeControl'
$deyeSource=Join-Path $deyeRoot 'source'
$deyeData=Join-Path $deyeRoot 'data'
$deyePython=Join-Path $deyeRoot '.venv\Scripts\python.exe'
$deyePythonW=Join-Path $deyeRoot '.venv\Scripts\pythonw.exe'
$taskName='DEYE Control'
if (-not (Test-Path $deyePython)) {
 # First install on this computer: Python, virtual environment and Solarman driver.
 Write-Host '[0/6] First install on this computer: preparing Python...' -ForegroundColor Cyan
 $installer=Invoke-WebRequest -UseBasicParsing ('https://raw.githubusercontent.com/OstapFed/deye-control-pwa/'+$SourceRef+'/install-windows.ps1')
 & ([scriptblock]::Create($installer.Content)) -LoggerHost $LoggerHost -LoggerSerial $LoggerSerial
 if (-not (Test-Path $deyePython)) {throw 'Python setup did not finish. Run this command again.'}
}

Write-Host '[1/6] Stopping the previous DEYE server...'
$task=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue}
# Stop only our gateway processes; leave unrelated Python applications alone.
$pattern='('+[regex]::Escape((Join-Path $deyeSource 'gateway\app.py'))+'|'+[regex]::Escape((Join-Path $deyeSource 'gateway\run_service.py'))+')(?:"|\s|$)'
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | Where-Object {$_.CommandLine -and $_.CommandLine -match $pattern} | ForEach-Object {
 Write-Host ('Stopping previous DEYE gateway PID '+$_.ProcessId)
 Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}
# An older manual install (for example %USERPROFILE%\DeyeControl) may still hold port 8080.
foreach ($c in @(Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue)) {
 $owner=Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
 if ($owner -and $owner.ProcessName -match '^pythonw?$') {Write-Host ('Stopping older DEYE server on port 8080 (PID '+$owner.Id+')'); Stop-Process -Id $owner.Id -Force -ErrorAction SilentlyContinue}
}
Start-Sleep -Seconds 2
$legacy=Join-Path $env:USERPROFILE 'DeyeControl'
if ((Test-Path (Join-Path $legacy 'data\config.json')) -and -not (Test-Path (Join-Path $deyeData 'config.json'))) {
 Write-Host 'Moving settings and history from the older install...'
 New-Item -ItemType Directory -Force -Path $deyeData | Out-Null
 foreach ($f in @('config.json','history.sqlite')) {if (Test-Path (Join-Path $legacy ('data\'+$f))) {Copy-Item (Join-Path $legacy ('data\'+$f)) (Join-Path $deyeData $f) -Force}}
}
$legacyStart=Join-Path $legacy 'Start-DEYE.ps1'
if (Test-Path $legacyStart) {Rename-Item $legacyStart 'Start-DEYE.ps1.old' -Force -ErrorAction SilentlyContinue; Write-Host 'Old launcher renamed to Start-DEYE.ps1.old (the new server starts automatically).'}

Write-Host '[2/6] Downloading the latest version...'
$env:PYTHONIOENCODING='utf-8'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$deyeTemp=Join-Path ([IO.Path]::GetTempPath()) ('deye-connect-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $deyeTemp | Out-Null
$deyeArchiveUrl=if ($SourceRef -eq 'main') {'https://github.com/OstapFed/deye-control-pwa/archive/refs/heads/main.zip'} else {'https://github.com/OstapFed/deye-control-pwa/archive/'+$SourceRef+'.zip'}
Invoke-WebRequest -UseBasicParsing $deyeArchiveUrl -OutFile (Join-Path $deyeTemp 'source.zip')
Expand-Archive (Join-Path $deyeTemp 'source.zip') -DestinationPath $deyeTemp
Get-ChildItem (Join-Path $deyeTemp ('deye-control-pwa-'+$SourceRef)) -Force | Copy-Item -Destination $deyeSource -Recurse -Force
Remove-Item $deyeTemp -Recurse -Force -ErrorAction SilentlyContinue
& $deyePython -m pip install --disable-pip-version-check -q -r (Join-Path $deyeSource 'gateway\requirements.txt')
if ($LASTEXITCODE -ne 0) {throw 'Could not install Python dependencies. Check internet access.'}

Write-Host '[3/6] Reading the inverter (no settings are changed)...'
$env:DEYE_DATA=$deyeData
$arguments=@((Join-Path $deyeSource 'gateway\configure_deye.py'),'--host',$LoggerHost,'--serial',"$LoggerSerial",'--apply')
if ($ExpectedInverterSerial) {$arguments+=@('--expected-inverter-serial',$ExpectedInverterSerial)}
$deyeReport=Join-Path $deyeRoot 'live-connection-check.txt'
& $deyePython @arguments 2>&1 | Tee-Object -FilePath $deyeReport
if ($LASTEXITCODE -ne 0) {throw ('Read-only profile check failed. Detailed report: '+$deyeReport)}

Write-Host '[4/6] Starting the DEYE server in the background...'
$service=Join-Path $deyeSource 'gateway\run_service.py'
Remove-Item Env:DEYE_PASSWORD -ErrorAction SilentlyContinue
if ($NoAutostart) {
 Start-Process -FilePath $deyePythonW -ArgumentList @(('"'+$service+'"'),('"'+$deyeData+'"')) -WindowStyle Hidden
} else {
 # Runs at every Windows logon for this user, restarts after failures, keeps running on battery.
 $action=New-ScheduledTaskAction -Execute $deyePythonW -Argument ('"'+$service+'" "'+$deyeData+'"') -WorkingDirectory (Join-Path $deyeSource 'gateway')
 $trigger=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
 $settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
 $principal=New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
 Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'DEYE Control gateway: inverter polling, Telegram alerts, local panel' -Force | Out-Null
 Start-ScheduledTask -TaskName $taskName
}
$ready=$false
for ($i=0;$i -lt 40;$i++) {
 try {$r=Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/health' -TimeoutSec 2; if ($r.ok) {$ready=$true; break}} catch {}
 Start-Sleep -Milliseconds 500
}
if (-not $ready) {throw ('The server did not start. See '+(Join-Path $deyeData 'server.log'))}
Write-Host ('Server version: '+$r.version) -ForegroundColor Green

Write-Host '[5/6] Creating the DEYE desktop shortcut...'
$opener=@'
$deyeData=Join-Path $env:LOCALAPPDATA 'DeyeControl\data'
$secret=(Get-Content -Raw -Encoding UTF8 (Join-Path $deyeData 'server-password.txt')).Trim()
try {Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/health' -TimeoutSec 3 | Out-Null}
catch {Start-ScheduledTask -TaskName 'DEYE Control' -ErrorAction SilentlyContinue; Start-Sleep -Seconds 4}
Start-Process ('http://127.0.0.1:8080/#deye-local='+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($secret)))
'@
$openerPath=Join-Path $deyeRoot 'Open-DEYE.ps1'
[IO.File]::WriteAllText($openerPath,$opener,(New-Object Text.UTF8Encoding($false)))
$icon=Join-Path $deyeRoot 'deye.ico'
try {
 Add-Type -AssemblyName System.Drawing
 $bitmap=[Drawing.Bitmap]::FromFile((Join-Path $deyeSource 'deye-final-192.png'))
 $handle=$bitmap.GetHicon(); $ico=[Drawing.Icon]::FromHandle($handle)
 $stream=[IO.File]::Create($icon); $ico.Save($stream); $stream.Close(); $bitmap.Dispose()
} catch {$icon=$null}
$shell=New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))) {
 $link=$shell.CreateShortcut((Join-Path $folder 'DEYE Control.lnk'))
 $link.TargetPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
 $link.Arguments='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+$openerPath+'"'
 $link.WindowStyle=7
 if ($icon) {$link.IconLocation=$icon}
 $link.Description='DEYE Control panel'
 $link.Save()
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $openerPath

Write-Host '[6/6] Checking for new inverter measurements for 30 seconds...'
$secret=(Get-Content -Raw -Encoding UTF8 (Join-Path $deyeData 'server-password.txt')).Trim()
$deyeAuth='Basic '+[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('admin:'+$secret))
$deyeHealth=@()
for ($check=0;$check -lt 6;$check++) {
 Start-Sleep -Seconds 5
 try {
  $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8080/api/status' -Headers @{Authorization=$deyeAuth} -TimeoutSec 5
  $deyeHealth+=@{checked_at=[DateTimeOffset]::UtcNow.ToString('o');version=$health.version;online=$health.online;error=$health.error;sample=$health.sample}
  if ($health.online -and $health.sample) {Write-Host ('Sample: SOC '+$health.sample.soc+'%, load '+$health.sample.load_w+' W, read in '+$health.sample.collection_seconds+' s')}
  elseif ($health.error) {Write-Host ('Waiting for logger: '+$health.error) -ForegroundColor Yellow}
 } catch {$deyeHealth+=@{checked_at=[DateTimeOffset]::UtcNow.ToString('o');online=$false;error=$_.Exception.Message}}
}
$deyeReport=Join-Path $deyeRoot 'stability-check.json'
[IO.File]::WriteAllText($deyeReport,($deyeHealth | ConvertTo-Json -Depth 10),(New-Object Text.UTF8Encoding($false)))
$freshTimes=@($deyeHealth | Where-Object {$_.online -and $_.sample -and -not $_.sample.demo} | ForEach-Object {$_.sample.t} | Select-Object -Unique)
if ($freshTimes.Count -ge 2) {Write-Host ('LIVE CHECK PASSED: '+$freshTimes.Count+' distinct measurements received.') -ForegroundColor Green}
else {Write-Host 'LIVE CHECK NOT PASSED: fewer than two new measurements. See stability-check.json and data\server.log.' -ForegroundColor Yellow}
Write-Host ''
Write-Host 'Done. The server now runs in the background and starts automatically when you sign in to Windows.' -ForegroundColor Green
Write-Host 'Open the panel any time with the "DEYE Control" shortcut on the desktop. This window can be closed.'
Write-Host 'Phone access from anywhere: run remote-access-windows.ps1 (Tailscale, private HTTPS).'
