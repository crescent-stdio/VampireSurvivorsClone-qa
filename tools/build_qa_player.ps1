param(
    [Parameter(Mandatory = $true)]
    [string]$UnityExe,
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$BuildPath = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'QAArtifacts\bridge-player\windows\VampireSurvivorsClone.exe'),
    [string]$LogPath = (Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'QAArtifacts\logs\bridge-player-build-windows.log')
)

$ErrorActionPreference = 'Stop'
$resolvedUnity = (Resolve-Path -LiteralPath $UnityExe).Path
$resolvedProject = (Resolve-Path -LiteralPath $ProjectRoot).Path
$buildDirectory = Split-Path -Parent $BuildPath
New-Item -ItemType Directory -Path $buildDirectory -Force | Out-Null
$env:QA_BRIDGE_BUILD_PATH = [System.IO.Path]::GetFullPath($BuildPath)

$resolvedLogPath = [System.IO.Path]::GetFullPath($LogPath)
$logDirectory = Split-Path -Parent $resolvedLogPath
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$unityArguments = @(
    '-batchmode',
    '-nographics',
    '-quit',
    '-projectPath', "`"$resolvedProject`"",
    '-executeMethod', 'Vampire.Editor.QA.QaBridgeBuild.BuildWindowsPlayerForBatchMode',
    '-logFile', "`"$resolvedLogPath`""
)
$unityProcess = Start-Process -FilePath $resolvedUnity -ArgumentList $unityArguments -Wait -PassThru -WindowStyle Hidden
if ($unityProcess.ExitCode -ne 0) {
    throw "Unity build failed with exit code $($unityProcess.ExitCode). See $LogPath"
}
if (-not (Test-Path -LiteralPath $BuildPath -PathType Leaf)) {
    throw "Unity returned success but did not create the Bridge player: $BuildPath"
}
Write-Output ([System.IO.Path]::GetFullPath($BuildPath))
