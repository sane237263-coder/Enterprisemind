param(
    [string]$SourcePath = (Join-Path (Split-Path -Parent $PSScriptRoot) 'local-runtime.ps1'),
    [string]$OutputPath
)
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($SourcePath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Runtime script has syntax errors.' }
# Load only the actual functions under test. Never execute the runtime entry point.
foreach ($name in @('Invoke-NativeServices', 'Get-NativeServices', 'Stop-NativeAfterFailure', 'Start-NativeRuntime')) {
    $definition = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    if ($definition) {
        . ([scriptblock]::Create($definition.Extent.Text))
    } elseif ($name -eq 'Start-NativeRuntime') {
        # Exercise the original inline startup block as the red regression control.
        $startup = @($ast.FindAll({ param($node)
            $node -is [System.Management.Automation.Language.TryStatementAst] -and
            $node.Body.Extent.Text.Contains('$online=$false')
        }, $true) | Sort-Object {$_.Extent.Text.Length} | Select-Object -First 1)
        if ($startup.Count -ne 1) { throw 'Cannot locate the original startup block.' }
        . ([scriptblock]::Create('function Start-NativeRuntime {' + $startup[0].Extent.Text + '}'))
    } else {
        throw "Missing runtime function: $name"
    }
}

# All external operations are in-process fakes: no WSL, Docker, HTTP or sleep.
$runtimeWslRoot = '/fake/project'
function Invoke-BoundedReadProcess {
    param([string]$Executable,[string[]]$Arguments)
    if ($Executable -ne 'wsl.exe' -or $Arguments[-1] -ne 'status') {
        throw 'Only read-only status may use the bounded process runner.'
    }
    if ($script:case.Mode -eq 'timeout') {
        $script:statusCalls++
        throw [System.TimeoutException]::new('Simulated status timeout.')
    }
    $output=wsl.exe @Arguments
    if ($script:case.Mode -eq 'stderr-transient' -and $script:statusCalls -eq 1) {
        return @{ExitCode=1;Stdout='';Stderr='Wsl/Service/0x8007274c'}
    }
    return @{ExitCode=$global:LASTEXITCODE;Stdout=($output -join "`n");Stderr=''}
}
function wsl.exe {
    $operation = $args[-1]
    [void]$script:operations.Add($operation)
    $global:LASTEXITCODE = 0
    if ($operation -eq 'start' -and $script:case.StartFails) {
        $global:LASTEXITCODE = 1
        return 'Wsl/Service/0x8007274c'
    }
    if ($operation -eq 'stop' -and $script:case.StopFails) {
        $global:LASTEXITCODE = 1
        return 'Wsl/Service/0x8007274c'
    }
    if ($operation -ne 'status') { return '{}' }
    $script:statusCalls++
    switch ($script:case.Mode) {
        'transient' {
            if ($script:statusCalls -eq 1) {
                $global:LASTEXITCODE = 1
                return 'Wsl/Service/0x8007274c'
            }
        }
        'persistent' {
            $global:LASTEXITCODE = 1
            return 'Wsl/Service/0x8007274c'
        }
        'nontransient' {
            $global:LASTEXITCODE = 1
            return 'Unmanaged service unit'
        }
        'invalid' { return '{"unexpected":true}' }
        'empty' { return '{"native_services":[]}' }
    }
    $workerState = if ($script:case.Mode -eq 'failed-worker') { 'failed' } else { 'active' }
    $workerSubstate = if ($workerState -eq 'failed') { 'failed' } else { 'running' }
    return @{native_services=@(
        @{service='enterprise-mind-local-api.service';ActiveState='active';SubState='running'},
        @{service='enterprise-mind-local-worker.service';ActiveState=$workerState;SubState=$workerSubstate}
    )} | ConvertTo-Json -Depth 4 -Compress
}
function Invoke-LocalCompose {
    param([string[]]$ComposeArgs)
    [void]$script:composeOperations.Add(($ComposeArgs -join ' '))
    if ($ComposeArgs[0] -eq 'up') {
        if ('--wait' -notin $ComposeArgs -or '--wait-timeout' -notin $ComposeArgs) {
            throw 'Dependency startup must wait for Office health with a deadline.'
        }
        if ($script:case.DependenciesFail) { throw 'Office dependency did not become healthy.' }
    }
}
function Wait-LocalDependencies {}
function Invoke-WebRequest {
    param([string]$Uri, [int]$TimeoutSec)
    return @{StatusCode=$(if ($script:case.ReadinessFails) {503} else {200})}
}
function Start-Sleep {
    param([int]$Seconds)
    [void]$script:delays.Add($Seconds)
}

$cases = @(
    @{Name='unhealthy Office prevents native service startup';Mode='healthy';DependenciesFail=$true;Throws=$true;StatusCalls=0;Stops=1},
    @{Name='healthy';Mode='healthy';Throws=$false;StatusCalls=1;Stops=0},
    @{Name='transient status read retries without restarting services';Mode='transient';Throws=$false;StatusCalls=2;Stops=0},
    @{Name='stderr transient failure also retries read-only status';Mode='stderr-transient';Throws=$false;StatusCalls=2;Stops=0},
    @{Name='status timeout fails without retries or cleanup';Mode='timeout';Throws=$true;StatusCalls=1;Stops=0},
    @{Name='persistent status error leaves started services running';Mode='persistent';Throws=$true;StatusCalls=3;Stops=0},
    @{Name='nontransient status error is not retried or cleaned up';Mode='nontransient';Throws=$true;StatusCalls=1;Stops=0},
    @{Name='invalid status is unknown';Mode='invalid';Throws=$true;StatusCalls=1;Stops=0},
    @{Name='empty status cannot claim readiness';Mode='empty';Throws=$true;StatusCalls=1;Stops=0},
    @{Name='confirmed failed worker still triggers cleanup';Mode='failed-worker';Throws=$true;StatusCalls=1;Stops=1},
    @{Name='failed start still triggers cleanup';Mode='healthy';StartFails=$true;Throws=$true;StatusCalls=0;Stops=1},
    @{Name='failed cleanup does not retry mutations and still stops dependencies';Mode='healthy';StartFails=$true;StopFails=$true;Throws=$true;StatusCalls=0;Stops=1},
    @{Name='failed API readiness still triggers cleanup';Mode='healthy';ReadinessFails=$true;Throws=$true;StatusCalls=0;Stops=1}
)
$results = @(foreach ($case in $cases) {
    $script:case = $case
    $script:operations = [System.Collections.Generic.List[string]]::new()
    $script:composeOperations = [System.Collections.Generic.List[string]]::new()
    $script:delays = [System.Collections.Generic.List[int]]::new()
    $script:statusCalls = 0
    $caught = $false
    $messages = [System.Collections.Generic.List[string]]::new()
    try { Start-NativeRuntime 3>$null | ForEach-Object { [void]$messages.Add([string]$_) } }
    catch { $caught = $true }
    $starts = @($script:operations | Where-Object {$_ -eq 'start'}).Count
    $stops = @($script:operations | Where-Object {$_ -eq 'stop'}).Count
    $composeStops = @($script:composeOperations | Where-Object {$_ -eq 'stop'}).Count
    $ready = @($messages | Where-Object {$_ -like '*EnterpriseMind ready:*'}).Count -gt 0
    $passed = ($caught -eq $case.Throws -and $script:statusCalls -eq $case.StatusCalls -and
        $starts -eq $(if ($case.DependenciesFail) {0} else {1}) -and $stops -eq $case.Stops -and $composeStops -eq $case.Stops -and
        $ready -eq (-not $case.Throws))
    if ($case.Mode -eq 'persistent') { $passed = $passed -and (($script:delays -join ',') -eq '1,2') }
    [ordered]@{name=$case.Name;passed=$passed;threw=$caught;status_calls=$script:statusCalls;
        starts=$starts;stops=$stops;compose_stops=$composeStops;reported_ready=$ready}
})
$report = [ordered]@{isolated_fakes_only=$true;external_commands_executed=0;
    passed=@($results | Where-Object {$_.passed}).Count;failed=@($results | Where-Object {-not $_.passed}).Count;cases=$results}
if ($OutputPath) { $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $OutputPath -Encoding utf8 }
$report | ConvertTo-Json -Depth 6
if ($report.failed) { exit 1 }
exit 0
