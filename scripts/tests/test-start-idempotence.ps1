param([string]$OutputPath)
$ErrorActionPreference='Stop'
$runtimeDir='/fake/runtime'
$source=Join-Path (Split-Path -Parent $PSScriptRoot) 'local-runtime.ps1'
$tokens=$null
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Runtime syntax errors' }
$definition=$ast.Find({param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-NativeRuntimeAlreadyRunning'
},$true)
if (-not $definition) { throw 'Missing repeated-start check' }
. ([scriptblock]::Create($definition.Extent.Text))

# Exercise the real decision code without touching WSL, containers or services.
function Get-NativeServices {
    if ($script:case.QueryFails) { throw [TimeoutException]::new('Unknown WSL state') }
    if ($script:case.Empty) { return @() }
    $names=@('api','dispatcher','worker','frontend')
    if ($script:case.Demo) { $names+='database-mcp' }
    foreach ($name in $names) {
        if ($script:case.Missing -eq $name) { continue }
        $state=if ($script:case.Stopped) {'inactive'} elseif ($script:case.Failed -eq $name) {'failed'} else {'active'}
        $row=@{service="enterprise-mind-local-$name.service";ActiveState=$state;
            SubState=$(if ($state -eq 'active') {'running'} else {'dead'});
            MainPID=$(if ($state -eq 'active') {'123'} else {'0'})}
        $row
        if ($script:case.Duplicate -eq $name) { $row }
    }
}
function Test-Path { param([string]$LiteralPath) return [bool]$script:case.Demo }
function Invoke-WebRequest {
    param([string]$Uri,[int]$TimeoutSec)
    $script:healthReads++
    if ($script:case.HealthThrows) { throw 'Timeout' }
    return @{StatusCode=$(if ($script:case.Unhealthy -and $Uri.Contains($script:case.Unhealthy)) {503} else {200})}
}
function Invoke-NativeServices { throw 'A repeated-start check must not mutate services' }
function Invoke-LocalCompose { throw 'A repeated-start check must not mutate containers' }
function Stop-NativeAfterFailure { throw 'A read failure must not clean up running services' }

$cases=@(
    @{Name='all four services and health ready';Ready=$true;Reads=3},
    @{Name='optional MCP also required';Demo=$true;Ready=$true;Reads=3},
    @{Name='stopped project takes existing cold-start path';Stopped=$true;Ready=$false;Reads=0},
    @{Name='no registered services does not claim running';Empty=$true;Ready=$false;Reads=0},
    @{Name='worker failure is not hidden by a working website';Failed='worker';Throws=$true;Reads=0},
    @{Name='missing worker is not ready';Missing='worker';Throws=$true;Reads=0},
    @{Name='missing configured MCP is not ready';Demo=$true;Missing='database-mcp';Throws=$true;Reads=0},
    @{Name='duplicate service record is not ready';Duplicate='api';Throws=$true;Reads=0},
    @{Name='API readiness failure keeps existing processes';Unhealthy='18000';Throws=$true;Reads=1},
    @{Name='frontend readiness failure';Unhealthy='5173';Throws=$true;Reads=2},
    @{Name='Office readiness failure';Unhealthy='38002';Throws=$true;Reads=3},
    @{Name='HTTP timeout does not report success';HealthThrows=$true;Throws=$true;Reads=1},
    @{Name='unknown WSL state is not stopped or healthy';QueryFails=$true;Throws=$true;Reads=0}
)
$results=@(foreach ($case in $cases) {
    $script:case=$case
    $script:healthReads=0
    $caught=$false
    $ready=$null
    try { $ready=Test-NativeRuntimeAlreadyRunning } catch { $caught=$true }
    $passed=$caught -eq [bool]$case.Throws -and $script:healthReads -eq $case.Reads
    if (-not $caught) { $passed=$passed -and $ready -is [bool] -and $ready -eq $case.Ready }
    @{name=$case.Name;passed=$passed}
})
$report=@{passed=@($results | Where-Object {$_.passed}).Count;failed=@($results | Where-Object {-not $_.passed}).Count;cases=$results}
if ($OutputPath) { $report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $OutputPath -Encoding utf8 }
$report | ConvertTo-Json -Depth 4
if ($report.failed) { exit 1 }
