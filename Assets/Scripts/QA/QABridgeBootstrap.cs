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
            QABridgeLaunchOptions options = QABridgeLaunchOptions.Parse(args);
            if (!options.IsRequested || UnityEngine.Object.FindObjectOfType<QABridge>() != null)
                return;

            GameObject bridgeObject = new GameObject("QA Bridge");
            UnityEngine.Object.DontDestroyOnLoad(bridgeObject);
            bridgeObject.AddComponent<QABridge>();
        }
    }
}
