param(
    [string]$RunId = '20260921-phase10-baseline-lunamax-01',
    [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root 'backend\.venv\Scripts\python.exe'
$evidence = Join-Path $Root ".local\phase10\$RunId\runtime"
$databaseConfig = Join-Path $evidence 'database.yaml'
$stream = "enterprise-mind:phase10:$RunId"

$env:PYTHONPATH = Join-Path $Root 'backend\src'
$env:EM_ENV = 'test'
$env:EM_POSTGRES_HOST = '127.0.0.1'
$env:EM_POSTGRES_PORT = '25432'
$env:EM_POSTGRES_DATABASE = 'em_stage1_test'
$env:EM_POSTGRES_USER = 'em_test_owner'
$env:EM_POSTGRES_PASSWORD_FILE = Join-Path $Root '.local\test-secrets\postgres_password'
$env:EM_TOKEN_SIGNING_KEY_FILE = Join-Path $Root '.local\test-secrets\token_key'
$env:EM_APPLICATION_CONFIG = Join-Path $Root 'config\application.yaml'
$env:EM_DATABASE_CONFIG = $databaseConfig
$env:EM_MINIO_ENDPOINT = '127.0.0.1:29000'
$env:EM_MINIO_BUCKET = 'em-p10-20260921-lm01-phase10'
$env:EM_MINIO_ACCESS_KEY_FILE = Join-Path $evidence 'minio_p10_app_access'
$env:EM_MINIO_SECRET_KEY_FILE = Join-Path $evidence 'minio_p10_app_secret'
$env:EM_MINIO_SECURE = 'false'
$env:EM_REDIS_HOST = '127.0.0.1'
$env:EM_REDIS_PORT = '26379'
$env:EM_REDIS_USERNAME = 'enterprise_mind_app'
$env:EM_REDIS_PASSWORD_FILE = Join-Path $Root '.local\test-secrets\redis_password'
$env:EM_REDIS_STREAM = $stream

# DatabaseQueryContextStore and the Phase-10 export plan owner are deliberately
# process-local.  Run this acceptance API with one Uvicorn worker so the query
# and export requests share that verified process-local context.  Keep the
# source config untouched and record the derived, non-secret runtime config in
# the evidence directory.
$sourceApplicationConfig = Join-Path $Root 'config\application.yaml'
$singleWorkerConfig = Join-Path $evidence 'application.single-worker.yaml'
$applicationText = Get-Content -LiteralPath $sourceApplicationConfig -Raw
$applicationText = [regex]::Replace($applicationText, '(?m)^  workers:\s*\d+\s*$', '  workers: 1')
if ($applicationText -notmatch '(?m)^  workers:\s*1\s*$') {
    throw 'could not derive the single-worker Phase-10 API configuration'
}
Set-Content -LiteralPath $singleWorkerConfig -Value $applicationText -Encoding utf8
$env:EM_APPLICATION_CONFIG = $singleWorkerConfig

function Start-Phase10Process {
    param(
        [string]$Name,
        [string[]]$Arguments
    )
    $stdout = Join-Path $evidence "$Name.stdout.log"
    $stderr = Join-Path $evidence "$Name.stderr.log"
    $process = Start-Process -FilePath $python -ArgumentList $Arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    [pscustomobject]@{
        name = $Name
        launcher_pid = $process.Id
        command = @($python) + $Arguments
        stdout = $stdout
        stderr = $stderr
    }
}

$env:EM_ENV = 'development'
$api = Start-Phase10Process -Name 'api' -Arguments @('-m', 'enterprise_mind.cli', 'serve', '--host', '127.0.0.1', '--port', '28080')
$env:EM_ENV = 'test'
$dispatcher = Start-Phase10Process -Name 'dispatcher' -Arguments @('-m', 'enterprise_mind.cli', 'dispatch')
$worker = Start-Phase10Process -Name 'worker' -Arguments @('-m', 'enterprise_mind.cli', 'worker')
$records = @($api, $dispatcher, $worker)
$records | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $evidence 'process-launch.json') -Encoding utf8
$records | ConvertTo-Json -Depth 4
