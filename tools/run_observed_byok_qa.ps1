param(
    [string]$Model,
    [string]$ApiUrl = 'https://api.openai.com/v1/chat/completions',
    [string]$Objective = 'Maintain net eastward exploration progress across the run. Use full 2D movement and allow lateral detours or brief backtracking to survive, evade enemies, and actively collect reachable treasure chests. Report only evidence-backed gameplay bugs.',
    [ValidateSet('free', 'east', 'west', 'north', 'south')]
    [string]$MovementConstraint = 'east',
    [float]$MaxSimulationSeconds = 120,
    [float]$PlanHorizonSeconds = 6,
    [int]$MaxStalledSteps = 2,
    [string]$Output = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path ('QAArtifacts\bridge-runs\byok-observed-' + (Get-Date -Format 'yyyyMMdd-HHmmss')))
)

$ErrorActionPreference = 'Stop'
if (-not $Model) {
    $Model = Read-Host 'Enter the exact model ID for your API account'
    if (-not $Model) {
        throw 'A model ID is required.'
    }
}
$runner = Join-Path $PSScriptRoot 'run_smoke.ps1'
$runnerArguments = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $runner,
    '-Mode', 'qa',
    '-Policy', 'llm',
    '-Model', $Model,
    '-ApiUrl', $ApiUrl,
    '-PromptForApiKey',
    '-Objective', $Objective,
    '-MovementConstraint', $MovementConstraint,
    '-ChestRadius', '20',
    '-ThreatRadius', '8',
    '-SurvivalWeight', '1.6',
    '-MinForwardComponent', '0.12',
    '-PlanHorizonSeconds', [string]$PlanHorizonSeconds,
    '-MaxSimulationSeconds', [string]$MaxSimulationSeconds,
    '-TimeScale', '1',
    '-MaxSteps', '120',
    '-MaxStalledSteps', [string]$MaxStalledSteps,
    '-Output', $Output
)
& powershell.exe @runnerArguments
$runExitCode = $LASTEXITCODE
if ($runExitCode -ne 0) {
    Write-Host ''
    $reportPath = Join-Path $Output 'report.md'
    if (Test-Path -LiteralPath $reportPath) {
        Write-Host "The QA run failed. Inspect: $reportPath" -ForegroundColor Red
    }
    else {
        Write-Host "The QA run failed before a report was created (exit code $runExitCode)." -ForegroundColor Red
    }
    Read-Host 'Press Enter after reading the error'
    exit $runExitCode
}
Write-Host "QA run completed. Report: $Output\report.md" -ForegroundColor Green
