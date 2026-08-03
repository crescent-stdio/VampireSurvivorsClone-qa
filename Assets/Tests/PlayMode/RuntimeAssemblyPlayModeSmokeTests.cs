using System;
using System.Linq;
using NUnit.Framework;
using UnityEngine.TestTools;

namespace Vampire.Tests.PlayMode
{
    public class RuntimeAssemblyPlayModeSmokeTests
    {
        [UnityTest]
        public System.Collections.IEnumerator RuntimeAssembly_remains_available_after_a_frame()
        {
            yield return null;

            var runtimeAssembly = AppDomain.CurrentDomain.GetAssemblies()
                .SingleOrDefault(assembly => assembly.GetName().Name == "Vampire.Runtime");

            Assert.That(runtimeAssembly, Is.Not.Null, "The Vampire.Runtime assembly must remain loaded in PlayMode.");
            Assert.That(runtimeAssembly.GetType("Vampire.RuntimeDependencyContract"), Is.Not.Null);
        }
    }
}
