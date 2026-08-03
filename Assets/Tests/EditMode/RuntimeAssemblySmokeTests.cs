using System;
using System.Linq;
using NUnit.Framework;

namespace Vampire.Tests.EditMode
{
    public class RuntimeAssemblySmokeTests
    {
        [Test]
        public void RuntimeAssembly_exposes_required_dependency_contract()
        {
            var runtimeAssembly = AppDomain.CurrentDomain.GetAssemblies()
                .SingleOrDefault(assembly => assembly.GetName().Name == "Vampire.Runtime");

            Assert.That(runtimeAssembly, Is.Not.Null, "The Vampire.Runtime assembly must be loaded.");

            var contractType = runtimeAssembly.GetType("Vampire.RuntimeDependencyContract");
            Assert.That(contractType, Is.Not.Null, "The runtime dependency contract must be available.");

            var dependencyProperty = contractType.GetProperty("AllRequiredAssembliesAreAvailable");
            Assert.That(dependencyProperty, Is.Not.Null, "The dependency contract must expose its availability result.");
            Assert.That((bool)dependencyProperty.GetValue(null), Is.True);
        }
    }
}
