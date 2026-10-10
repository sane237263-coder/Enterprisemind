param([Parameter(Mandatory)][string]$EvidenceDirectory)
$ErrorActionPreference='Stop'
$tokens=$null
$errors=$null
$source=Join-Path (Split-Path -Parent $PSScriptRoot) 'local-runtime.ps1'
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'Runtime script has syntax errors.' }
$function=$ast.Find({param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Invoke-BoundedReadProcess'
},$true)
if (-not $function) { throw 'Bounded read process function is missing.' }
. ([scriptblock]::Create($function.Extent.Text))
$executable=(Get-Process -Id $PID).Path
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$results=[System.Collections.Generic.List[object]]::new()
function Run-Child {
    param([string]$Code,[int]$TimeoutMilliseconds=10000)
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Code))
    Invoke-BoundedReadProcess -Executable $executable -Arguments @('-NoProfile','-EncodedCommand',$encoded) -TimeoutMilliseconds $TimeoutMilliseconds
}

# Real, owned PowerShell children exercise OS pipes and exit handling, not mocks.
$reply=Run-Child '[Console]::Out.Write("ready"); [Console]::Error.Write("diagnostic"); exit 7'
$results.Add(@{name='nonzero exit and separate streams';passed=($reply.ExitCode -eq 7 -and $reply.Stdout -eq 'ready' -and $reply.Stderr -eq 'diagnostic')})
$reply=Run-Child '[Console]::Out.Write("x" * 262144); [Console]::Error.Write("y" * 262144)'
$results.Add(@{name='drain large stdout and stderr without deadlock';passed=($reply.ExitCode -eq 0 -and $reply.Stdout.Length -eq 262144 -and $reply.Stderr.Length -eq 262144)})

$argumentFile=Join-Path $EvidenceDirectory 'arguments with spaces.ps1'
Set-Content -LiteralPath $argumentFile -Encoding utf8 -Value 'param([string]$Value) [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); [Console]::Out.Write($Value)'
$literal='space "quote" $literal; 中文'
$reply=Invoke-BoundedReadProcess -Executable $executable -Arguments @('-NoProfile','-File',[IO.Path]::GetFullPath($argumentFile),'-Value',$literal)
$results.Add(@{name='arguments remain literal across process boundary';passed=($reply.ExitCode -eq 0 -and $reply.Stdout -ceq $literal);expected_codepoints=@($literal.ToCharArray() | ForEach-Object {[int]$_});actual_codepoints=@($reply.Stdout.ToCharArray() | ForEach-Object {[int]$_})})

$pidFile=Join-Path $EvidenceDirectory 'timeout-child.json'
$escapedPath=[IO.Path]::GetFullPath($pidFile).Replace("'","''")
$code="@{pid=`$PID;started=(Get-Process -Id `$PID).StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath '$escapedPath'; Start-Sleep -Seconds 30"
$watch=[Diagnostics.Stopwatch]::StartNew()
$timedOut=$false
try { Run-Child $code -TimeoutMilliseconds 2000 | Out-Null }
catch [System.TimeoutException] { $timedOut=$true }
$elapsed=$watch.Elapsed.TotalSeconds
$record=Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$remaining=Get-Process -Id $record.pid -ErrorAction SilentlyContinue
$ownedAlive=$remaining -and $remaining.StartTime.ToUniversalTime().ToString('o') -eq $record.started
$results.Add(@{name='timeout returns promptly and ends only its owned launcher';passed=($timedOut -and $elapsed -lt 6 -and -not $ownedAlive);seconds=$elapsed;owned_child_alive=[bool]$ownedAlive})
$report=@{real_local_processes=$true;wsl_or_docker_calls=0;passed=@($results | Where-Object {$_.passed}).Count;failed=@($results | Where-Object {-not $_.passed}).Count;cases=@($results.ToArray())}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'process-tests.json') -Encoding utf8
$report | ConvertTo-Json -Depth 5
if ($report.failed) { exit 1 }
exit 0
