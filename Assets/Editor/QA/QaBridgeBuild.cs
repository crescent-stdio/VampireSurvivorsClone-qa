using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;

namespace Vampire.Editor.QA
{
    /// <summary>Builds the production menu/gameplay route with the opt-in file Bridge enabled by command line.</summary>
    public static class QaBridgeBuild
    {
        private const string MainMenuScene = "Assets/Scenes/Game/Main Menu.unity";
        private const string LevelScene = "Assets/Scenes/Game/Level 1.unity";
        private const string DefaultMacPlayer = "QAArtifacts/bridge-player/macos/VampireSurvivorsClone.app";
        private const string DefaultWindowsPlayer = "QAArtifacts/bridge-player/windows/VampireSurvivorsClone.exe";

        public static void BuildMacPlayerForBatchMode()
        {
            BuildPlayer(BuildTarget.StandaloneOSX, DefaultMacPlayer);
        }

        public static void BuildWindowsPlayerForBatchMode()
        {
            BuildPlayer(BuildTarget.StandaloneWindows64, DefaultWindowsPlayer);
        }

        private static void BuildPlayer(BuildTarget target, string defaultPlayer)
        {
            var group = BuildPipeline.GetBuildTargetGroup(target);
            if (!BuildPipeline.IsBuildTargetSupported(group, target))
                throw new InvalidOperationException("Unity is missing standalone build support for " + target + ".");

            if (EditorUserBuildSettings.activeBuildTarget != target &&
                !EditorUserBuildSettings.SwitchActiveBuildTarget(group, target))
                throw new InvalidOperationException("Unable to switch to the Windows standalone build target.");

            QaAssetGenerator.BuildAddressablesForBatchMode();

            var enabledScenes = EditorBuildSettings.scenes
                .Where(scene => scene.enabled)
                .Select(scene => scene.path)
                .ToArray();
            if (!enabledScenes.Contains(MainMenuScene) || !enabledScenes.Contains(LevelScene))
                throw new InvalidOperationException("The Bridge player requires enabled Main Menu and Level 1 scenes.");

            var buildPath = Environment.GetEnvironmentVariable("QA_BRIDGE_BUILD_PATH");
            if (string.IsNullOrWhiteSpace(buildPath))
                buildPath = defaultPlayer;
            buildPath = Path.GetFullPath(buildPath);
            Directory.CreateDirectory(Path.GetDirectoryName(buildPath));

            var report = BuildPipeline.BuildPlayer(enabledScenes, buildPath, target, BuildOptions.None);
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException("Bridge player build failed: " + report.summary.result);
        }
    }
}
