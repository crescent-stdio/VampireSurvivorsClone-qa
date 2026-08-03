using TMPro;
using Unity.MLAgents;
using UnityEngine.InputSystem;
using UnityEngine.Localization.Settings;
using UnityEngine.ResourceManagement.AsyncOperations;

namespace Vampire
{
    public static class RuntimeDependencyContract
    {
        public static bool AllRequiredAssembliesAreAvailable =>
            typeof(InputAction).Assembly != null &&
            typeof(LocalizationSettings).Assembly != null &&
            typeof(AsyncOperationHandle).Assembly != null &&
            typeof(TMP_Text).Assembly != null &&
            typeof(Agent).Assembly != null;
    }
}
