# Task 1 TDD Evidence

## Isolated RED

An isolated temporary worktree was created from the final Task 1 commit. It retained the test assemblies and `Vampire.Runtime.asmdef`. Only `RuntimeDependencyContract.cs` and its Unity meta file were removed.

```text
Unity -batchmode -nographics \
  -projectPath <temporary-worktree> \
  -runTests -testPlatform EditMode \
  -testResults <temporary-red-results.xml>
```

The repeated run returned exit code `2`. The EditMode result contained one failed test:

```text
Vampire.Tests.EditMode.RuntimeAssemblySmokeTests.
RuntimeAssembly_exposes_required_dependency_contract

The runtime dependency contract must be available.
Expected: not null
But was:  null
```

## Current GREEN

The identical EditMode smoke test was rerun against the current worktree:

```text
Unity -batchmode -nographics \
  -projectPath <current-worktree> \
  -runTests -testPlatform EditMode \
  -testResults <current-green-results.xml>
```

It returned exit code `0`, with one passed test and zero failed tests. No test or production API was weakened to obtain this result.
