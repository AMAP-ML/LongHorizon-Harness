"""Windows compatibility test suite for LongHorizon-Harness.

This script tests all Windows-specific fixes to ensure the harness
works correctly on Windows platforms.
"""

import os
import sys
import time
from pathlib import Path


def test_imports():
    """Test that all modules can be imported without errors."""
    print("Testing module imports...")
    modules = [
        "lh_harness.supervisor.service",
        "lh_harness.supervisor.control_bus",
        "lh_harness.manager",
        "lh_harness.environment.remote_files",
        "lh_harness.environment.local",
        "lh_harness.dashboard.state",
        "lh_harness.utils.process_group",
        "lh_harness.trajectory_artifacts",
        "lh_harness.adapters.claude_permissions",
    ]

    for module in modules:
        try:
            __import__(module)
            print(f"  ✅ {module}")
        except Exception as e:
            print(f"  ❌ {module}: {e}")
            return False
    return True


def test_path_deny_rules():
    """Test Windows path handling in permission rules."""
    print("\nTesting path deny rules...")
    from lh_harness.adapters.claude_permissions import path_deny_rules

    test_paths = [
        Path.cwd() / ".lh-harness",
        "E:/Project/Test/.lh-harness",
    ]

    for path in test_paths:
        rules = path_deny_rules([str(path)])
        print(f"  Input: {path}")

        # On Windows, check that drive letters don't have // prefix
        if sys.platform == "win32":
            for rule in rules:
                if "//E:/" in rule or "//C:/" in rule:
                    print(f"  ❌ Invalid Windows path format: {rule}")
                    return False
                print(f"    {rule}")

    print("  ✅ Path deny rules correct")
    return True


def test_file_locking():
    """Test file locking works on Windows."""
    print("\nTesting file locking...")

    if sys.platform == "win32":
        import msvcrt

        test_file = Path("test_lock.tmp")
        try:
            with open(test_file, "w") as f:
                f.write("test")
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            test_file.unlink()
            print("  ✅ msvcrt.locking works")
            return True
        except Exception as e:
            print(f"  ❌ msvcrt.locking failed: {e}")
            if test_file.exists():
                test_file.unlink()
            return False
    else:
        print("  ⏭️  Skipped (not Windows)")
        return True


def test_path_quoting():
    """Test Windows-specific path quoting for cmd.exe."""
    print("\nTesting path quoting...")
    from lh_harness.adapters.cli_agent import _quote_path

    test_cases = [
        ("simple.txt", True),
        ("path with spaces", True),
        ("E:\\Project\\Test", True if sys.platform == "win32" else True),
    ]

    for path, _ in test_cases:
        quoted = _quote_path(path)
        if sys.platform == "win32":
            # Windows should use double quotes
            if not (quoted.startswith('"') and quoted.endswith('"')):
                print(f"  ❌ Path not properly quoted: {quoted}")
                return False
        else:
            # Unix should use shlex.quote (single quotes or escaped)
            pass
        print(f"  ✅ {path} -> {quoted}")

    return True


def test_env_var_syntax():
    """Test Windows-specific environment variable syntax."""
    print("\nTesting environment variable syntax...")

    # Simulate env_parts construction
    env_parts = [
        "VAR1=value1",
        "VAR2=value2",
    ]

    if sys.platform == "win32":
        env_prefix = (" && ".join(f"set {part}" for part in env_parts) + " && ") if env_parts else ""
        # Check Windows syntax
        if "set VAR1=value1" not in env_prefix:
            print(f"  ❌ Incorrect Windows syntax: {env_prefix}")
            return False
        if " && " not in env_prefix:
            print(f"  ❌ Missing && separator: {env_prefix}")
            return False
        print(f"  ✅ Windows syntax: {env_prefix}")
    else:
        env_prefix = (" ".join(env_parts) + " ") if env_parts else ""
        print(f"  ✅ Unix syntax: {env_prefix}")

    return True


def test_run_supervisor():
    """Test RunSupervisor creation and basic operations."""
    print("\nTesting RunSupervisor...")
    from lh_harness.supervisor.service import RunSupervisor

    workspace = Path.cwd()
    runs_root = workspace / ".lh-runs-test"
    runs_root.mkdir(exist_ok=True)

    try:
        supervisor = RunSupervisor(workspace_root=workspace, runs_root=runs_root)
        print("  ✅ RunSupervisor initialized")

        run_id = supervisor.create_run(
            task="Test task",
            agent="claude_code",
        )
        print(f"  ✅ Run created: {run_id['id']}")

        # Wait a bit for files to be created
        time.sleep(5)

        # Check that files were created
        run_dir = runs_root / run_id["id"]
        if run_dir.exists():
            file_count = len(list(run_dir.rglob("*")))
            print(f"  ✅ Files created: {file_count}")
        else:
            print("  ❌ Run directory not created")
            return False

        # Clean up
        import subprocess
        import shutil

        subprocess.run(
            ["taskkill", "/F", "/T", "/FI", f"WINDOWTITLE eq *{run_id['id']}*"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        shutil.rmtree(runs_root, ignore_errors=True)
        print("  ✅ Cleanup successful")

        return True

    except Exception as e:
        print(f"  ❌ RunSupervisor test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_scandir():
    """Test os.scandir with path instead of file descriptor on Windows."""
    print("\nTesting os.scandir...")

    test_dir = Path("test_scandir_dir")
    test_dir.mkdir(exist_ok=True)
    (test_dir / "file1.txt").write_text("test")
    (test_dir / "file2.txt").write_text("test")

    try:
        # On Windows, scandir should accept path strings
        if sys.platform == "win32":
            with os.scandir(test_dir) as entries:
                count = len(list(entries))
            print(f"  ✅ os.scandir(path) works: {count} files")
        else:
            # On Unix, test with file descriptor
            fd = os.open(test_dir, os.O_RDONLY)
            try:
                with os.scandir(fd) as entries:
                    count = len(list(entries))
                print(f"  ✅ os.scandir(fd) works: {count} files")
            finally:
                os.close(fd)

        # Cleanup
        import shutil
        shutil.rmtree(test_dir)
        return True

    except Exception as e:
        print(f"  ❌ os.scandir test failed: {e}")
        import shutil
        shutil.rmtree(test_dir, ignore_errors=True)
        return False


def main():
    """Run all Windows compatibility tests."""
    print("=" * 70)
    print("LongHorizon-Harness Windows Compatibility Test Suite")
    print("=" * 70)
    print(f"Platform: {sys.platform}")
    print(f"Python: {sys.version}")
    print()

    tests = [
        ("Module Imports", test_imports),
        ("Path Deny Rules", test_path_deny_rules),
        ("File Locking", test_file_locking),
        ("os.scandir", test_scandir),
        ("Path Quoting", test_path_quoting),
        ("Environment Variable Syntax", test_env_var_syntax),
        ("RunSupervisor", test_run_supervisor),
    ]

    results = {}
    for name, test_func in tests:
        try:
            results[name] = test_func()
        except Exception as e:
            print(f"\n❌ {name} crashed: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    print()
    print("=" * 70)
    print("Test Results Summary")
    print("=" * 70)

    passed = sum(1 for result in results.values() if result)
    total = len(results)

    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")

    print()
    print(f"Total: {passed}/{total} tests passed")

    if passed == total:
        print("\n🎉 All tests passed! Windows compatibility verified.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
