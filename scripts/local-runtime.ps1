param(
    [Parameter(Mandatory)][ValidateSet('Init','Start','Stop','Status','Backup')][string]$Action,
    [string]$AdminUsername = 'localadmin',
    [ValidateSet('WSL','Windows')][string]$Runtime = 'WSL',
    [switch]$EnableDemoDatabase
)
$ErrorActionPreference = 'Stop'
$runtimeRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $runtimeRoot '.local/runtime'
$runtimePython = Join-Path $runtimeRoot 'backend/.venv/Scripts/python.exe'
$runtimeCompose = Join-Path $runtimeRoot 'deploy/local/compose.yaml'
$runtimeRecords = Join-Path $runtimeDir 'processes.json'
$runtimeWslRoot = '/mnt/' + $runtimeRoot.Substring(0,1).ToLower() + $runtimeRoot.Substring(2).Replace('\','/')
$runtimePorts = @(18000,5173,5433,36379,39000,38001,38002)

# Fail before initialization creates credentials or starts containers. These helpers
# manage the preconfigured local runtime; README.md describes setup requirements.
if ($Action -in @('Init','Start')) {
    if ($Runtime -eq 'Windows' -and -not (Test-Path -LiteralPath (Join-Path $runtimeRoot '.local/model-env.ps1') -PathType Leaf)) {
        throw 'Missing .local/model-env.ps1. Copy config/model-env.example.ps1 there without overwriting existing files, then review it. Windows Init also requires configured Embedding for build-vector-index. See README.md for setup requirements and deploy/local/README.md for the managed runtime.'
    }
    if ($Runtime -eq 'WSL') {
        foreach ($configName in @('providers.local.yaml','rag.local.yaml')) {
            if (-not (Test-Path -LiteralPath (Join-Path $runtimeRoot "config/$configName") -PathType Leaf)) {
                throw 'WSL runtime requires reviewed config/providers.local.yaml and config/rag.local.yaml; Init requires configured Embedding. It does not load .local/model-env.ps1. See README.md for setup requirements and deploy/local/README.md for the managed runtime.'
            }
        }
    }
}

function Invoke-LocalDocker {
    param([string[]]$DockerArgs)
    & wsl.exe -d Ubuntu -- docker @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Local Docker operation failed.' }
}
function Invoke-LocalCompose {
    param([string[]]$ComposeArgs)
    Invoke-LocalDocker -DockerArgs (@('compose','--project-name','enterprise-mind-local','-f',"$runtimeWslRoot/deploy/local/compose.yaml") + $ComposeArgs)
}
function Set-LocalEnvironment {
    $runtimePrivate = Join-Path $runtimeDir 'secrets'
    $env:PYTHONPATH = Join-Path $runtimeRoot 'backend/src'
    $env:EM_ENV = 'development'
    $env:EM_POSTGRES_HOST = '127.0.0.1'
    $env:EM_POSTGRES_PORT = '5433'
    $env:EM_POSTGRES_DATABASE = 'enterprise_mind'
    $env:EM_POSTGRES_USER = 'enterprise_mind_app'
    $env:EM_POSTGRES_PASSWORD_FILE = Join-Path $runtimePrivate 'postgres_app_password'
    $env:EM_TOKEN_SIGNING_KEY_FILE = Join-Path $runtimePrivate 'token_signing_key'
    $env:EM_APPLICATION_CONFIG = Join-Path $runtimeDir 'application.yaml'
    $env:EM_AGENT_CONFIG = Join-Path $runtimeRoot 'config/agent.yaml'
    $env:EM_DATABASE_CONFIG = Join-Path $runtimeDir 'database.yaml'
    $env:EM_CHECKPOINT_POSTGRES_USER = 'enterprise_mind_checkpoint'
    $env:EM_CHECKPOINT_POSTGRES_PASSWORD_FILE = Join-Path $runtimePrivate 'postgres_checkpoint_password'
    $env:EM_MINIO_ENDPOINT = '127.0.0.1:39000'
    $env:EM_MINIO_BUCKET = 'enterprise-mind-local'
    $env:EM_MINIO_SECURE = 'false'
    $env:EM_MINIO_ACCESS_KEY_FILE = Join-Path $runtimePrivate 'minio_app_access'
    $env:EM_MINIO_SECRET_KEY_FILE = Join-Path $runtimePrivate 'minio_app_secret'
    $env:EM_REDIS_HOST = '127.0.0.1'
    $env:EM_REDIS_PORT = '36379'
    $env:EM_REDIS_USERNAME = 'enterprise_mind_app'
    $env:EM_REDIS_PASSWORD_FILE = Join-Path $runtimePrivate 'redis_app_password'
    $env:EM_REDIS_STREAM = 'enterprise-mind:local:processing'
    $env:EM_OFFICE_CONVERTER_TOKEN_FILE = Join-Path $runtimePrivate 'converter_token'
    Remove-Item Env:EM_BOOTSTRAP_ADMIN_USERNAME,Env:EM_BOOTSTRAP_ADMIN_PASSWORD_FILE -ErrorAction SilentlyContinue
    . (Join-Path $runtimeRoot '.local/model-env.ps1')
}
function Get-LocalProcesses {
    if (Test-Path -LiteralPath $runtimeRecords) {
        return @(Get-Content -LiteralPath $runtimeRecords -Raw | ConvertFrom-Json -DateKind String)
    }
    return @()
}
function Protect-LocalSecrets {
    $privatePath=Join-Path $runtimeDir 'secrets'
    $privateAcl=Get-Acl -LiteralPath $privatePath
    $localUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $rules=@($privateAcl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]))
    $expectedSids=@($localUser.Value,'S-1-5-18')
    if ($privateAcl.AreAccessRulesProtected -and $rules.Count -eq 2 -and
        @($rules | Where-Object {$_.IdentityReference.Value -notin $expectedSids -or
            $_.FileSystemRights -ne [System.Security.AccessControl.FileSystemRights]::FullControl -or
            $_.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow}).Count -eq 0) {
        return
    }
    # Write only the DACL. Reapplying owner/audit metadata requires unnecessary OS privileges.
    $privateAcl=[System.Security.AccessControl.DirectorySecurity]::new()
    $privateAcl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @($localUser,[System.Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
        $privateAcl.SetAccessRule($rule)
    }
    [System.IO.FileSystemAclExtensions]::SetAccessControl([System.IO.DirectoryInfo]::new($privatePath),$privateAcl)
}
function Test-LocalProcess {
    param($Record)
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.pid)" -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    $started = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    return $started -and $started.StartTime.ToUniversalTime().ToString('o') -eq $Record.started_utc -and $proc.CommandLine.Contains($runtimeRoot)
}
function Stop-LocalProcesses {
    foreach ($record in (Get-LocalProcesses)) {
        if (Test-LocalProcess $record) {
            # PID, creation time and absolute command path must match before killing this tree.
            & taskkill.exe /PID $record.pid /T /F | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "Could not stop owned $($record.name) tree." }
        }
    }
    if (Test-Path -LiteralPath $runtimeRecords) { Set-Content -LiteralPath $runtimeRecords -Value '[]' -Encoding UTF8 }
}
function Start-LocalProcess {
    param([string]$Name,[string]$Executable,[string[]]$Arguments,[string]$WorkingDirectory)
    $argsQuoted = @($Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } })
    $process = Start-Process -FilePath $Executable -ArgumentList $argsQuoted -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir "$Name.stdout.log") -RedirectStandardError (Join-Path $runtimeDir "$Name.stderr.log")
    return [pscustomobject]@{name=$Name;pid=$process.Id;started_utc=$process.StartTime.ToUniversalTime().ToString('o')}
}
function Invoke-LocalCLI {
    param([string[]]$CLIArgs)
    & $runtimePython -m enterprise_mind.cli @CLIArgs
    if ($LASTEXITCODE -ne 0) { throw ('Local initialization failed: ' + $CLIArgs[0]) }
}
function Wait-LocalDependencies {
    for ($attempt=0;$attempt -lt 45;$attempt++) {
        $ready = $true
        foreach ($port in @(5433,36379,39000,38002)) {
            $client = [System.Net.Sockets.TcpClient]::new()
            try { $client.Connect('127.0.0.1',$port) } catch { $ready=$false } finally { $client.Dispose() }
        }
        if ($ready) { return }
        Start-Sleep -Seconds 2
    }
    throw 'Local dependencies did not become ready.'
}

function Invoke-BoundedReadProcess {
    param([string]$Executable,[string[]]$Arguments,[int]$TimeoutMilliseconds=30000)
    $process=[System.Diagnostics.Process]::new()
    $process.StartInfo.FileName=$Executable
    $process.StartInfo.UseShellExecute=$false
    $process.StartInfo.CreateNoWindow=$true
    $process.StartInfo.RedirectStandardOutput=$true
    $process.StartInfo.RedirectStandardError=$true
    $process.StartInfo.StandardOutputEncoding=[System.Text.UTF8Encoding]::new($false)
    $process.StartInfo.StandardErrorEncoding=[System.Text.UTF8Encoding]::new($false)
    foreach ($argument in $Arguments) { $process.StartInfo.ArgumentList.Add($argument) }
    $clock=[System.Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $process.Start()) { throw 'Read-only process could not start.' }
        # Drain both streams concurrently so full pipes cannot block process exit.
        $stdout=$process.StandardOutput.ReadToEndAsync()
        $stderr=$process.StandardError.ReadToEndAsync()
        $remaining=[Math]::Max(0,$TimeoutMilliseconds-[int]$clock.ElapsedMilliseconds)
        $exited=$process.WaitForExit($remaining)
        $remaining=[Math]::Max(0,$TimeoutMilliseconds-[int]$clock.ElapsedMilliseconds)
        if (-not $exited -or -not [System.Threading.Tasks.Task]::WaitAll(
            [System.Threading.Tasks.Task[]]@($stdout,$stderr),$remaining)) {
            # Only end the query launcher created here; never kill a WSL/service tree.
            if (-not $process.HasExited) {
                $process.Kill()
                [void]$process.WaitForExit(2000)
            }
            throw [System.TimeoutException]::new('Read-only WSL status query timed out; service state is unknown.')
        }
        return [pscustomobject]@{ExitCode=$process.ExitCode;Stdout=$stdout.Result;Stderr=$stderr.Result}
    } finally {
        $process.Dispose()
    }
}
function Invoke-NativeServices {
    param([string]$NativeAction)
    if ($NativeAction -ne 'status') {
        $result=& wsl.exe -d Ubuntu -u root -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_services.py" $NativeAction
        if ($LASTEXITCODE -ne 0) { throw "Ubuntu EM service operation failed: $NativeAction" }
        return $result
    }
    for ($queryAttempt=1;$queryAttempt -le 3;$queryAttempt++) {
        $query=Invoke-BoundedReadProcess -Executable 'wsl.exe' -Arguments @(
            '-d','Ubuntu','-u','root','--','/usr/bin/python3',"$runtimeWslRoot/scripts/local_wsl_services.py",'status')
        if ($query.ExitCode -eq 0) { return $query.Stdout }
        $failureText=($query.Stdout + "`n" + $query.Stderr) -replace "`0",''
        # Retry only a read-only query whose WSL entry failed. Never repeat a mutation.
        if ($failureText -notmatch '0x8007274c' -or $queryAttempt -eq 3) {
            throw "Ubuntu EM service operation failed: $NativeAction"
        }
        Start-Sleep -Seconds $queryAttempt
    }
}
function Get-NativeServices {
    $status=Invoke-NativeServices -NativeAction 'status' | ConvertFrom-Json
    if ($null -eq $status -or $status.native_services -isnot [array]) {
        throw 'Ubuntu EM service status is unavailable or invalid.'
    }
    $services=@($status.native_services)
    if (@($services | Where-Object {
        [string]::IsNullOrWhiteSpace($_.service) -or
        [string]::IsNullOrWhiteSpace($_.ActiveState) -or
        [string]::IsNullOrWhiteSpace($_.SubState)
    }).Count) { throw 'Ubuntu EM service status is incomplete.' }
    return $services
}
function Invoke-NativeCLI {
    param([string[]]$CLIArgs)
    & wsl.exe -d Ubuntu -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_cli.py" @CLIArgs
    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu EM CLI operation failed.' }
}
function Assert-NoNativeServices {
    if (@(Get-NativeServices | Where-Object {$_.ActiveState -in @('active','activating','deactivating')}).Count) {
        throw 'Stop native EM services first.'
    }
}
function Test-NativeRuntimeAlreadyRunning {
    # This branch only observes existing services. Never restart or clean up on a failed read.
    $services=@(Get-NativeServices)
    $live=@($services | Where-Object {
        $_.ActiveState -notin @('inactive','failed') -or [long]$_.MainPID -gt 0
    })
    if (-not $live.Count) { return $false }
    $expected=@('api','dispatcher','worker','frontend')
    if (Test-Path -LiteralPath (Join-Path $runtimeDir 'wsl/demo-source.json')) {
        $expected+='database-mcp'
    }
    $problems=@(foreach ($name in $expected) {
        $matches=@($services | Where-Object {$_.service -ceq "enterprise-mind-local-$name.service"})
        if ($matches.Count -ne 1) { "${name}：缺少或重复的服务状态" }
        elseif ($matches[0].ActiveState -ne 'active' -or $matches[0].SubState -ne 'running' -or
            [long]$matches[0].MainPID -le 0) {
            "${name}：$($matches[0].ActiveState)/$($matches[0].SubState)"
        }
    })
    if ($services.Count -ne $expected.Count -or $problems.Count) {
        $detail=if ($problems.Count) {$problems -join '；'} else {'服务清单与本项目配置不一致'}
        throw "项目部分服务未就绪（$detail）。请先查看状态；本次未执行启停。"
    }
    foreach ($check in @(
        @{Name='API';Uri='http://127.0.0.1:18000/health/ready'},
        @{Name='网页';Uri='http://127.0.0.1:5173/health/ready'},
        @{Name='Office 转换';Uri='http://127.0.0.1:38002/health/ready'}
    )) {
        try {
            $response=Invoke-WebRequest $check.Uri -TimeoutSec 3
            if ($response.StatusCode -ne 200) { throw 'Not ready' }
        } catch {
            throw "项目进程已运行，但$($check.Name)服务健康检查未通过。请先查看状态；本次未执行启停。"
        }
    }
    return $true
}
function Stop-NativeAfterFailure {
    # Attempt both cleanup operations without replacing the original startup error.
    try { Invoke-NativeServices -NativeAction 'stop' | Out-Null }
    catch { Write-Warning 'Could not stop Ubuntu EM units; check runtime status before retrying.' }
    try { Invoke-LocalCompose -ComposeArgs @('stop') }
    catch { Write-Warning 'Could not stop EM dependency containers; check runtime status before retrying.' }
}
function Start-NativeRuntime {
    try {
        Invoke-LocalCompose -ComposeArgs @('up','-d','--wait','--wait-timeout','180')
        Wait-LocalDependencies
        Invoke-NativeServices -NativeAction 'start'
        $online=$false
        for ($attempt=0;$attempt -lt 45;$attempt++) {
            try {
                $api=Invoke-WebRequest 'http://127.0.0.1:18000/health/ready' -TimeoutSec 3
                $web=Invoke-WebRequest 'http://127.0.0.1:5173/health/ready' -TimeoutSec 3
                if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) {$online=$true;break}
            } catch { }
            Start-Sleep -Seconds 2
        }
        if (-not $online) { throw 'Ubuntu API/frontend readiness failed; inspect .local/runtime/wsl logs.' }
    } catch {
        Stop-NativeAfterFailure
        throw
    }
    try {
        $services=@(Get-NativeServices)
        if (-not $services.Count) { throw 'Ubuntu EM service status is empty after startup.' }
    } catch {
        # A failed observation does not prove that the services failed.
        Write-Warning 'Service status could not be verified after startup; existing processes were left untouched. Use Status before retrying Start.'
        throw
    }
    if (@($services | Where-Object {$_.ActiveState -ne 'active' -or $_.SubState -ne 'running'}).Count) {
        Stop-NativeAfterFailure
        throw 'An Ubuntu EM service is not running.'
    }
    Write-Output 'Ubuntu EnterpriseMind ready: http://127.0.0.1:5173'
}

# Keep Windows mode as a fallback; default development processes now run in Ubuntu.
if ($Runtime -eq 'WSL') {
    Push-Location $runtimeRoot
    try {
        if ($Action -eq 'Init') {
            Assert-NoNativeServices
            if (@(Get-LocalProcesses | Where-Object {Test-LocalProcess $_}).Count) { throw 'Stop Windows EM services first.' }
            & wsl.exe -d Ubuntu -- bash "$runtimeWslRoot/scripts/local_wsl_setup.sh"
            if ($LASTEXITCODE -ne 0) { throw 'Ubuntu locked tool/dependency setup failed.' }
            $nativePaths=Get-Content -LiteralPath (Join-Path $runtimeDir 'wsl/paths.json') -Raw | ConvertFrom-Json
            & wsl.exe -d Ubuntu -- $nativePaths.python "$runtimeWslRoot/scripts/local_runtime_prepare.py" --admin-username $AdminUsername
            if ($LASTEXITCODE -ne 0) { throw 'Ubuntu runtime preparation failed.' }
            Protect-LocalSecrets
            try {
                Invoke-LocalCompose -ComposeArgs @('up','-d','--wait','--wait-timeout','180')
                Wait-LocalDependencies
                & wsl.exe -d Ubuntu -u root -- runuser -u postgres -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_provision.py"
                if ($LASTEXITCODE -ne 0) { throw 'Ubuntu PostgreSQL provisioning failed.' }
                Invoke-NativeCLI -CLIArgs @('--role','migrator','--bootstrap','migrate','--backend-root',"$runtimeWslRoot/backend")
                Invoke-NativeCLI -CLIArgs @('--role','migrator','checkpoint-init')
                Invoke-NativeCLI -CLIArgs @('--role','migrator','build-vector-index')
                Invoke-NativeCLI -CLIArgs @('storage-init','--root-access-key-file',"$runtimeWslRoot/.local/runtime/secrets/minio_root_access",'--root-secret-key-file',"$runtimeWslRoot/.local/runtime/secrets/minio_root_secret")
                if ($EnableDemoDatabase) {
                    & wsl.exe -d Ubuntu -u root -- runuser -u postgres -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_demo_readonly.py"
                    if ($LASTEXITCODE -ne 0) { throw 'demo_db readonly provisioning failed.' }
                    & wsl.exe -d Ubuntu -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_enable_demo.py"
                    if ($LASTEXITCODE -ne 0) { throw 'demo_db configuration failed.' }
                    & wsl.exe -d Ubuntu -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_prepare.py"
                    if ($LASTEXITCODE -ne 0) { throw 'Ubuntu database path configuration failed.' }
                }
                Invoke-NativeServices -NativeAction 'install'
            } catch {
                Stop-NativeAfterFailure
                throw
            }
            Write-Output 'Ubuntu EM initialization complete; existing data and credentials preserved.'
            return
        } elseif ($Action -eq 'Start') {
            if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'wsl/paths.json'))) { throw 'Run Init first for Ubuntu.' }
            if (@(Get-LocalProcesses | Where-Object {Test-LocalProcess $_}).Count) { throw 'Stop Windows EM services first.' }
            if (Test-NativeRuntimeAlreadyRunning) {
                Write-Output '项目已在运行，无需重复启动：http://127.0.0.1:5173'
                return
            }
            $nativePorts=@(18000,5173)
            if (Test-Path -LiteralPath (Join-Path $runtimeDir 'wsl/demo-source.json')) { $nativePorts+=38001 }
            foreach ($port in $nativePorts) {
                $probe=[System.Net.Sockets.TcpClient]::new()
                try { $probe.Connect('127.0.0.1',$port);$occupied=$true } catch {$occupied=$false} finally {$probe.Dispose()}
                if ($occupied) { throw "Port $port already occupied." }
            }
            & wsl.exe -d Ubuntu -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_prepare.py"
            if ($LASTEXITCODE -ne 0) { throw 'Ubuntu path configuration failed.' }
            Start-NativeRuntime
            return
        } elseif ($Action -eq 'Stop') {
            Invoke-NativeServices -NativeAction 'stop'
            Stop-LocalProcesses
            Invoke-LocalCompose -ComposeArgs @('stop')
            Write-Output 'EM stopped; Ubuntu PostgreSQL and all data retained.'
            return
        } elseif ($Action -eq 'Status') {
            Invoke-LocalCompose -ComposeArgs @('ps','--all')
            Invoke-NativeServices -NativeAction 'status'
            return
        } elseif ($Action -eq 'Backup') {
            Assert-NoNativeServices
            # The existing offline backup logic below also checks Windows processes.
        }
    } finally { Pop-Location }
} elseif ($Action -in @('Init','Start','Backup')) {
    Assert-NoNativeServices
}

Push-Location $runtimeRoot
try {
    if ($Action -eq 'Init') {
        if (@(Get-LocalProcesses | Where-Object {Test-LocalProcess $_}).Count) { throw 'Stop local services before initialization.' }
        & $runtimePython (Join-Path $PSScriptRoot 'local_runtime_prepare.py') --admin-username $AdminUsername
        if ($LASTEXITCODE -ne 0) { throw 'Runtime preparation failed.' }
        Protect-LocalSecrets
        Set-LocalEnvironment
        Invoke-LocalCompose -ComposeArgs @('up','-d','--wait','--wait-timeout','180')
        Wait-LocalDependencies
        $runtimePrivate = Join-Path $runtimeDir 'secrets'
        # Native Ubuntu PG uses OS peer authentication for the one-time provisioner.
        & wsl.exe -d Ubuntu -u root -- runuser -u postgres -- /usr/bin/python3 "$runtimeWslRoot/scripts/local_wsl_provision.py"
        if ($LASTEXITCODE -ne 0) { throw 'Ubuntu native PostgreSQL provisioning failed.' }
        $env:EM_POSTGRES_USER='enterprise_mind_migrator'
        $env:EM_POSTGRES_PASSWORD_FILE=Join-Path $runtimePrivate 'postgres_migrator_password'
        $runtimeIdentity=Get-Content -LiteralPath (Join-Path $runtimeDir 'runtime.json') -Raw | ConvertFrom-Json
        $env:EM_BOOTSTRAP_ADMIN_USERNAME=$runtimeIdentity.admin_username
        $env:EM_BOOTSTRAP_ADMIN_PASSWORD_FILE=Join-Path $runtimePrivate 'bootstrap_admin_password'
        Invoke-LocalCLI -CLIArgs @('migrate','--backend-root',(Join-Path $runtimeRoot 'backend'))
        Remove-Item Env:EM_BOOTSTRAP_ADMIN_USERNAME,Env:EM_BOOTSTRAP_ADMIN_PASSWORD_FILE
        Invoke-LocalCLI -CLIArgs @('checkpoint-init')
        Invoke-LocalCLI -CLIArgs @('build-vector-index')
        Invoke-LocalCLI -CLIArgs @('storage-init','--root-access-key-file',(Join-Path $runtimePrivate 'minio_root_access'),'--root-secret-key-file',(Join-Path $runtimePrivate 'minio_root_secret'))
        Set-LocalEnvironment
        Write-Output 'Local initialization complete; credentials remain in .local/runtime/secrets.'
    } elseif ($Action -eq 'Start') {
        if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'runtime.json'))) { throw 'Run Init first.' }
        $owned=@(Get-LocalProcesses | Where-Object {Test-LocalProcess $_})
        if ($owned.Count -gt 0) { throw 'Owned services already running; use Status or Stop before Start.' }
        foreach ($port in @(18000,5173)) {
            if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port $port already occupied." }
        }
        Set-LocalEnvironment
        Invoke-LocalCompose -ComposeArgs @('up','-d','--wait','--wait-timeout','180')
        Wait-LocalDependencies
        $records=@()
        try {
            foreach ($service in @('api','dispatcher','worker')) {
                $arguments = if ($service -eq 'api') {@('-m','enterprise_mind.cli','serve','--host','127.0.0.1','--port','18000')} elseif ($service -eq 'dispatcher') {@('-m','enterprise_mind.cli','dispatch')} else {@('-m','enterprise_mind.cli','worker')}
                $records += Start-LocalProcess -Name $service -Executable $runtimePython -Arguments $arguments -WorkingDirectory $runtimeRoot
                $records | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $runtimeRecords -Encoding UTF8
            }
            $node = (Get-Command node.exe).Source
            $vite = Join-Path $runtimeRoot 'frontend/node_modules/vite/bin/vite.js'
            $records += Start-LocalProcess -Name 'frontend' -Executable $node -Arguments @($vite,'--host','127.0.0.1','--port','5173') -WorkingDirectory (Join-Path $runtimeRoot 'frontend')
            $records | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $runtimeRecords -Encoding UTF8
            $online=$false
            for ($attempt=0;$attempt -lt 45;$attempt++) {
                try {
                    $api = Invoke-WebRequest 'http://127.0.0.1:18000/health/ready' -TimeoutSec 3
                    $web = Invoke-WebRequest 'http://127.0.0.1:5173/health/ready' -TimeoutSec 3
                    if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) { $online=$true;break }
                } catch { }
                Start-Sleep -Seconds 2
            }
            if (-not $online) { throw 'API/frontend readiness failed; inspect private runtime logs.' }
            Write-Output 'EnterpriseMind ready: http://127.0.0.1:5173'
        } catch {
            Stop-LocalProcesses
            Invoke-LocalCompose -ComposeArgs @('stop')
            throw
        }
    } elseif ($Action -eq 'Stop') {
        Stop-LocalProcesses
        Invoke-LocalCompose -ComposeArgs @('stop')
        Write-Output 'Local services stopped; named data volumes retained.'
    } elseif ($Action -eq 'Status') {
        Invoke-LocalCompose -ComposeArgs @('ps','--all')
        $processStatus=@(foreach ($record in (Get-LocalProcesses)) { [pscustomobject]@{service=$record.name;pid=$record.pid;alive=[bool](Test-LocalProcess $record)} })
        $portStatus=@(
        foreach ($port in $runtimePorts) {
            $probe=[System.Net.Sockets.TcpClient]::new()
            try { $probe.Connect('127.0.0.1',$port);$reachable=$true } catch { $reachable=$false } finally {$probe.Dispose()}
            [pscustomobject]@{port=$port;reachable=$reachable}
        }
        )
        [pscustomobject]@{processes=$processStatus;ports=$portStatus} | ConvertTo-Json -Depth 4
    } elseif ($Action -eq 'Backup') {
        $owned=@(Get-LocalProcesses | Where-Object {Test-LocalProcess $_})
        if ($owned.Count -gt 0) { throw 'Run Stop first; Backup requires a consistent offline boundary.' }
        $running=Invoke-LocalDocker -DockerArgs @('ps','--filter','label=com.docker.compose.project=enterprise-mind-local','--format','{{.ID}}')
        if ($running) { throw 'Run Stop first; data containers must be stopped.' }
        $backupTag=Get-Date -Format 'yyyyMMdd-HHmmss'
        $backupDir=Join-Path $runtimeDir "backups/$backupTag"
        New-Item -ItemType Directory -Path $backupDir | Out-Null
        & wsl.exe -d Ubuntu -u root -- runuser -u postgres -- /usr/bin/pg_dump -p 5433 -d enterprise_mind --format=custom --no-owner --no-acl --file "$runtimeWslRoot/.local/runtime/backups/$backupTag/enterprise_mind.dump"
        if ($LASTEXITCODE -ne 0) { throw 'Ubuntu pg_dump failed.' }
        foreach ($volume in @('minio_data','redis_data')) {
            $volumeName="enterprise-mind-local_$volume"
            Invoke-LocalDocker -DockerArgs @('volume','inspect',$volumeName,'--format','{{.Name}}') | Out-Null
            Invoke-LocalDocker -DockerArgs @('run','--rm','--network','none','--read-only','-v',"${volumeName}:/source:ro",'-v',"${runtimeWslRoot}/.local/runtime/backups/${backupTag}:/backup",'python:3.13-slim-bookworm','tar','-C','/source','-cf',"/backup/$volume.tar",'.')
        }
        $backupFiles=@(Get-ChildItem -LiteralPath $backupDir -File | ForEach-Object {[pscustomobject]@{name=$_.Name;bytes=$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower()}})
        [pscustomobject]@{project='enterprise-mind-local';created=([DateTime]::UtcNow.ToString('o'));type='native-pg-logical-and-offline-object-snapshot';postgres='Ubuntu 18/main:5433';secretsIncluded=$false;files=$backupFiles} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $backupDir 'manifest.json') -Encoding UTF8
        Write-Output "Backup created: $backupDir. Preserve runtime/secrets separately for recovery."
    }
} finally { Pop-Location }
