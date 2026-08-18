using System;
using UnityEngine;

namespace Vampire.QA
{
    public static class QABridgeBootstrap
    {
        [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
        private static void CreateBridge()
        {
            string[] args = Environment.GetCommandLineArgs();
            QaFaultOptions faultOptions = QaFaultOptions.Parse(args);
            if (!faultOptions.IsValid)
            {
                Debug.LogError(faultOptions.FailureReason);
                Application.Quit(2);
                return;
            }
            QABridgeLaunchOptions options = QABridgeLaunchOptions.Parse(args);
            if (!options.IsRequested || UnityEngine.Object.FindObjectOfType<QABridge>() != null)
                return;

            GameObject bridgeObject = new GameObject("QA Bridge");
            UnityEngine.Object.DontDestroyOnLoad(bridgeObject);
            bridgeObject.AddComponent<QABridge>();
        }
    }
}
