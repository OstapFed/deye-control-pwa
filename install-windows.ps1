[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$LoggerHost,
    [Parameter(Mandatory=$true)][UInt32]$LoggerSerial,
    [ValidateRange(1,247)][int]$UnitId = 1
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Find-DeyePython {
    $candidates = @()
    foreach ($name in @('py.exe','python.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Source }
    }
    foreach ($version in @('313','312','311','310')) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path $candidate)) { continue }
        try {
            $found = & $candidate -c "import sys; print(sys.executable if sys.version_info >= (3,10) else '')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path ($found | Select-Object -Last 1))) {
                return ($found | Select-Object -Last 1)
            }
        } catch { }
    }
    return $null
}

try {
    $parsedAddress = $null
    if (-not [Net.IPAddress]::TryParse($LoggerHost,[ref]$parsedAddress)) {
        throw 'LoggerHost must be the local IPv4 address shown by your router.'
    }
    if ($LoggerSerial -eq 0) { throw 'LoggerSerial must be the logger SN, not the inverter SN.' }
    Write-Host '[1/4] Checking Python...'
    $deyePython = Find-DeyePython
    if (-not $deyePython) {
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $winget) {
            Start-Process 'https://www.python.org/downloads/windows/'
            throw 'Install Python 3.10 or newer from the opened official page, then run this installer again.'
        }
        Write-Host 'Installing Python 3.12 for the current Windows user...'
        & $winget.Source install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -notin @(0,3010)) { throw 'Python installation failed. Install Python from python.org and retry.' }
        $deyePython = Find-DeyePython
        if (-not $deyePython) { throw 'Python was installed. Reopen PowerShell and run this installer again.' }
    }

    $deyeRoot = Join-Path $env:LOCALAPPDATA 'DeyeControl'
    $deyeSource = Join-Path $deyeRoot 'source'
    $deyeTemp = Join-Path ([IO.Path]::GetTempPath()) ('deye-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $deyeRoot,$deyeSource,$deyeTemp | Out-Null
    Write-Host '[2/4] Downloading your Deye Control repository...'
    $archive = Join-Path $deyeTemp 'source.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/OstapFed/deye-control-pwa/archive/refs/heads/main.zip' -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $deyeTemp -Force
    $unpacked = Join-Path $deyeTemp 'deye-control-pwa-main'
    if (-not (Test-Path (Join-Path $unpacked 'gateway\probe_solarman.py'))) { throw 'Downloaded archive is incomplete.' }
    Get-ChildItem -LiteralPath $unpacked -Force | Copy-Item -Destination $deyeSource -Recurse -Force

    Write-Host '[3/4] Preparing an isolated Python environment...'
    $deyeVenv = Join-Path $deyeRoot '.venv'
    if (-not (Test-Path (Join-Path $deyeVenv 'Scripts\python.exe'))) {
        & $deyePython -m venv $deyeVenv
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
    }
    $deyeRuntime = Join-Path $deyeVenv 'Scripts\python.exe'
    & $deyeRuntime -m pip install --disable-pip-version-check -r (Join-Path $deyeSource 'gateway\requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Solarman dependencies. Check internet access.' }

    $deyeData = Join-Path $deyeRoot 'data'
    New-Item -ItemType Directory -Force -Path $deyeData | Out-Null
    $deyeConfig = Join-Path $deyeData 'config.json'
    if (-not (Test-Path $deyeConfig)) {
        # No unverified register addresses or write permissions are enabled.
        $profile = @{
            mode='solarman_v5'; host=$LoggerHost; port=8899; unit=$UnitId;
            logger_serial=$LoggerSerial; registers=@{};
            verified_profile=$false; allow_writes=$false
        }
        $profile | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $deyeConfig -Encoding UTF8
        # Windows PowerShell 5 writes a UTF-8 BOM; Python expects UTF-8 without BOM.
        [IO.File]::WriteAllText($deyeConfig, ($profile | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    }
    Write-Host '[4/4] Testing local Solarman V5 reads. No inverter settings will be changed...'
    $report = Join-Path $deyeRoot 'connection-check.txt'
    & $deyeRuntime (Join-Path $deyeSource 'gateway\probe_solarman.py') --host $LoggerHost --serial $LoggerSerial --unit $UnitId 2>&1 | Tee-Object -FilePath $report
    $probeExit = $LASTEXITCODE
    Write-Host "Installed in: $deyeRoot"
    Write-Host "Read-only diagnostic report: $report"
    if ($probeExit -eq 0) {
        Write-Host 'V5 read succeeded. Next step: run connect-windows.ps1 to start the panel and autostart.' -ForegroundColor Green
    } else {
        Write-Warning 'V5 reading failed. Send the diagnostic error; no settings were changed.'
    }
} catch {
    Write-Host ('Setup stopped: ' + $_.Exception.Message) -ForegroundColor Red
    throw
}
