param(
    [ValidateSet('Menu', 'Start', 'Stop', 'Status', 'Open')]
    [string]$Action = 'Menu'
)
# UTF-8 BOM is intentional: Explorer launches this with Windows PowerShell 5.1.
$ErrorActionPreference = 'Stop'
$switchRoot = Split-Path -Parent $PSScriptRoot
$switchRuntime = Join-Path $PSScriptRoot 'local-runtime.ps1'
$switchUrl = 'http://127.0.0.1:5173'

function Find-ProjectPowerShell {
    $candidates = @(
        (Join-Path $PSHOME 'pwsh.exe')
        (Join-Path $env:ProgramFiles 'PowerShell/7/pwsh.exe')
        @(Get-Command pwsh.exe -CommandType Application -All -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Source)
        (Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe')
    )
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $versionText = [Diagnostics.FileVersionInfo]::GetVersionInfo($candidate).ProductVersion
        if ($versionText -match '^(\d+\.\d+\.\d+)' -and [version]$Matches[1] -ge [version]'7.5.0') {
            return $candidate
        }
    }
    throw '未找到 PowerShell 7.5 或更新版本。请安装后重新打开项目开关。'
}

function Open-ProjectPage {
    try {
        $response = Invoke-WebRequest "$switchUrl/health/ready" -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ne 200) { throw 'not ready' }
    } catch {
        throw '项目网页尚未就绪。请先启动项目，或选择“查看状态”检查。'
    }
    Start-Process -FilePath $switchUrl
}

function Invoke-ProjectSwitch {
    param([ValidateSet('Start', 'Stop', 'Status', 'Open')][string]$SelectedAction)
    if ($SelectedAction -eq 'Open') { Open-ProjectPage; return }
    if (-not (Test-Path -LiteralPath $switchRuntime -PathType Leaf)) {
        throw '缺少 scripts/local-runtime.ps1，请保留项目开关与项目目录的相对位置。'
    }
    $executable = Find-ProjectPowerShell
    # Serialize launcher actions for this checkout without creating a lock file.
    $hash = [Security.Cryptography.SHA256]::Create()
    try {
        $key = [BitConverter]::ToString($hash.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes($switchRoot.ToLowerInvariant()))).Replace('-', '')
    } finally { $hash.Dispose() }
    $mutex = [Threading.Mutex]::new($false, ('Local\EnterpriseMind.ProjectSwitch.' + $key))
    $acquired = $false
    try {
        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
        if (-not $acquired) { throw '另一个项目开关正在执行操作，请等待完成后再试。' }
        & $executable -NoLogo -NoProfile -ExecutionPolicy Bypass -File $switchRuntime -Runtime WSL -Action $SelectedAction
        if ($LASTEXITCODE -ne 0) {
            throw ('操作未完成（退出码 {0}），请查看上方错误，或选择“查看状态”。' -f $LASTEXITCODE)
        }
        switch ($SelectedAction) {
            'Start' { Write-Host "项目已就绪：$switchUrl" -ForegroundColor Green }
            'Stop' { Write-Host '项目已停止，数据保留。共享 PostgreSQL 和其他项目不受影响。' -ForegroundColor Green }
        }
    } finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Show-ProjectSwitch {
    while ($true) {
        Write-Host ''
        Write-Host '========== EnterpriseMind 项目开关 ==========' -ForegroundColor Cyan
        Write-Host '  1  启动项目'
        Write-Host '  2  停止项目（保留数据）'
        Write-Host '  3  查看服务状态'
        Write-Host '  4  打开项目网页'
        Write-Host '  0  退出开关（项目保持当前运行状态）'
        Write-Host "  地址：$switchUrl"
        Write-Host ''
        $choice = Read-Host '请输入编号'
        if ($choice -eq '0' -or ([Console]::IsInputRedirected -and [string]::IsNullOrEmpty($choice))) { return }
        $selected = switch ($choice) {
            '1' { 'Start' }
            '2' { 'Stop' }
            '3' { 'Status' }
            '4' { 'Open' }
        }
        if (-not $selected) { Write-Host '请输入 0、1、2、3 或 4。' -ForegroundColor Yellow; continue }
        try { Invoke-ProjectSwitch -SelectedAction $selected }
        catch { Write-Host $_.Exception.Message -ForegroundColor Red }
    }
}

try {
    if ($Action -eq 'Menu') { Show-ProjectSwitch }
    else { Invoke-ProjectSwitch -SelectedAction $Action }
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
