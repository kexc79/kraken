param(
    [ValidateSet('deploy','init','build','check','start','run','logs','status','stop')]
    [string]$Action = 'start',
    [switch]$Local,
    [switch]$UpdateConfig
)
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if ($Local) {
        if ($Action -eq 'deploy' -or $UpdateConfig) { throw 'Deploy/UpdateConfig is for the server. Local configuration is in config/.' }
        New-Item -ItemType Directory -Force -Path 'config','logs' | Out-Null
        foreach ($name in @('.env','.req')) {
            if (-not (Test-Path -LiteralPath "config/$name")) {
                $taskTemplate = if (Test-Path -LiteralPath $name) { $name } else { "$name.example" }
                Copy-Item -LiteralPath $taskTemplate -Destination "config/$name"
            }
        }
        if ($Action -eq 'init') {
            Write-Output 'Configuration prepared in config/. Set real values and add the Google JSON key before start.'
            exit 0
        }
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Local mode requires Docker Desktop with Linux containers. Omit -Local to operate the Ubuntu server.' }
        $env:APP_UID = '1000'
        $env:APP_GID = '1000'
        function Invoke-Compose { & docker compose --env-file .compose.env -p kraken-pharma -f compose.yaml @args }
        if ($Action -in @('start','run')) {
            $taskRunning = Invoke-Compose ps -q --status running pharma
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            if ($taskRunning) { throw 'Already running. Use status, logs or stop.' }
            Invoke-Compose build
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        if ($Action -in @('start','run','check')) {
            Invoke-Compose run --rm --no-deps -T pharma python check_config.py
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        switch ($Action) {
            build { Invoke-Compose build }
            check { exit 0 }
            start { Invoke-Compose up --no-build --no-deps --force-recreate --abort-on-container-exit --exit-code-from pharma pharma }
            run { Invoke-Compose up --no-build --no-deps --force-recreate --abort-on-container-exit --exit-code-from pharma pharma }
            logs { Invoke-Compose logs -f --tail 100 pharma }
            status { Invoke-Compose ps -a }
            stop { Invoke-Compose stop pharma }
        }
        exit $LASTEXITCODE
    }
    & python -c 'import paramiko, dotenv'
    if ($LASTEXITCODE -ne 0) { throw 'Install launcher dependencies: python -m pip install -r requirements-deploy.txt' }
    $taskArguments = @('manage.py', $Action)
    if ($UpdateConfig) { $taskArguments += '--update-config' }
    & python @taskArguments
    exit $LASTEXITCODE
}
finally { Pop-Location }
