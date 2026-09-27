## 🐛 Problem

Fixes #81

The Web service fails to start on Windows with:
```
ModuleNotFoundError: No module named 'fcntl'
RuntimeError: secure supervisor locking is unavailable
```

The `_supervisor_locked()` method in `supervisor/service.py` uses Unix-only `fcntl` module and POSIX file APIs (`O_NOFOLLOW`, `dir_fd`), which are not available on Windows.

**Impact**: 
- ❌ `POST /api/runs` returns 500 error on Windows
- ❌ Cannot create tasks via Web UI
- ❌ CLI runs also affected (related to #77, #63, #70)

## ✅ Solution

Added Windows-specific file locking using `msvcrt.locking()`:

**Changes**:
- Detects Windows via `sys.platform == "win32"`
- Uses `msvcrt.LK_LOCK` for exclusive locking (equivalent to `fcntl.LOCK_EX`)
- Falls back to the original Unix `fcntl` path on non-Windows platforms
- Maintains identical locking semantics and error handling

**File Modified**: `src/lh_harness/supervisor/service.py` (+33 lines)

## 🧪 Testing

Tested on **Windows 10/11 with Python 3.11**:

✅ **File locking mechanism works correctly**
```python
# Test script verified lock acquisition and release
with supervisor._supervisor_locked():
    # Lock acquired successfully
    pass
# Lock released successfully
```

✅ **Web service starts successfully**
```bash
$ lh-harness web --workspace-root . --port 0
INFO:     Uvicorn running on http://127.0.0.1:52265
```

✅ **Run creation now works**
- `POST /api/runs` returns 200 OK
- Tasks can be created via Web UI
- No 500 errors in server logs

✅ **No dependency changes required**
- `msvcrt` is part of Python standard library on Windows
- No impact on Unix/Linux/macOS platforms

## 🔗 Related Issues

This PR directly fixes:
- #81 - Web UI: creating a run fails on Windows - supervisor lock hard-depends on fcntl

May also improve (partial fix, needs testing):
- #77 - Windows: `lh run` bootstrap fails (control bus still needs separate fix)
- #70 - Windows 11 全流程不可用
- #63 - Windows: "secure control-bus path opening is unavailable"

## 📚 Additional Context

The README mentions:
> Platform status: Currently tested on macOS. Windows support is included but has not yet been thoroughly tested.

This PR improves Windows compatibility by addressing one of the core blocking issues.

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)
