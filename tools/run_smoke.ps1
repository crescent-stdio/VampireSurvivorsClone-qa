param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$GameExe = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'QAArtifacts\bridge-player\windows\VampireSurvivorsClone.exe'),
    [ValidateSet('player', 'qa')]
    [string]$Mode = 'player',
    [ValidateSet('heuristic', 'llm')]
    [string]$Policy = 'heuristic',
    [string]$Output = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'QAArtifacts\bridge-runs\latest'),
    [string]$Model = $env:QA_MODEL,
    [string]$ApiUrl = $env:QA_API_URL,
    [string]$Objective = 'Explore autonomously, maximize useful gameplay coverage, survive when possible, and report only evidence-backed anomalies.',
    [ValidateSet('free', 'east', 'west', 'north', 'south')]
    [string]$MovementConstraint = 'free',
    [int]$MaxRestarts = 1,
    [string[]]$FocusArea = @(),
    [int]$Seed = 1337,
    [float]$MaxSimulationSeconds = 60,
    [float]$TimeScale = 1.0,
    [Alias('ActionSeconds')]
    [float]$PlanHorizonSeconds = 5.0,
    [float]$MinForwardComponent = 0.15,
    [bool]$CollectNearbyChests = $true,
    [float]$ChestRadius = 16.0,
    [float]$ThreatRadius = 8.0,
    [float]$SurvivalWeight = 1.4,
    [float]$InterruptHealthRatio = 0.30,
    [float]$InterruptDangerScore = 0.85,
    [int]$MaxSteps = 80,
    [int]$MaxStalledSteps = 2,
    [switch]$Headless,
    [switch]$PromptForApiKey,
    [switch]$PauseDuringPlanning,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$temporaryApiKey = $false
if ($Policy -eq 'llm') {
    if (-not $Model) {
        throw 'LLM policy requires -Model or QA_MODEL.'
    }
    if (-not $env:QA_API_KEY -and -not $env:OPENAI_API_KEY) {
        if (-not $PromptForApiKey) {
            throw 'LLM policy requires QA_API_KEY/OPENAI_API_KEY or -PromptForApiKey.'
        }
        $secureApiKey = Read-Host 'Enter your LLM API key (kept only for this run)' -AsSecureString
        $env:QA_API_KEY = [System.Net.NetworkCredential]::new('', $secureApiKey).Password
        Remove-Variable secureApiKey
        $temporaryApiKey = $true
    }
}
$arguments = @(
    '-m', 'qa_smoke.run',
    '--game-exe', ([System.IO.Path]::GetFullPath($GameExe)),
    '--project-root', ([System.IO.Path]::GetFullPath($ProjectRoot)),
    '--output', ([System.IO.Path]::GetFullPath($Output)),
    '--mode', $Mode,
    '--policy', $Policy,
    '--objective', $Objective,
    '--movement-constraint', $MovementConstraint,
    '--max-restarts', $MaxRestarts,
    '--seed', $Seed,
    '--max-simulation-seconds', $MaxSimulationSeconds,
    '--time-scale', $TimeScale,
    '--plan-horizon-seconds', $PlanHorizonSeconds,
    '--min-forward-component', $MinForwardComponent,
    '--chest-radius', $ChestRadius,
    '--threat-radius', $ThreatRadius,
    '--survival-weight', $SurvivalWeight,
    '--interrupt-health-ratio', $InterruptHealthRatio,
    '--interrupt-danger-score', $InterruptDangerScore,
    '--max-steps', $MaxSteps,
    '--max-stalled-steps', $MaxStalledSteps
)
if ($CollectNearbyChests) {
    $arguments += '--collect-chests'
}
else {
    $arguments += '--no-collect-chests'
}
if ($Policy -eq 'llm') {
    $arguments += @('--model', $Model)
    if ($ApiUrl) {
        $arguments += @('--api-url', $ApiUrl)
    }
}
foreach ($area in $FocusArea) {
    $arguments += @('--focus-area', $area)
}
if ($Headless) {
    $arguments += '--headless'
}
if ($PauseDuringPlanning) {
    $arguments += '--pause-during-planning'
}
if ($Quiet) {
    $arguments += '--quiet'
}
Push-Location $ProjectRoot
try {
    py -3 @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
    if ($temporaryApiKey) {
        Remove-Item Env:QA_API_KEY -ErrorAction SilentlyContinue
    }
}
exit $exitCode
