# Windows 排障指南

LongHorizon-Harness 在 Windows 上可完整运行（本仓库 CI 含 `windows-latest` 矩阵），
但 Windows 生态有几个已知坑。按症状排查：

## `lh-harness` 命令找不到

`uv tool install` 装到 `%USERPROFILE%\.local\bin`，安装器会提示并自动加入 PATH，
但**当前已打开的终端不会生效**。重开终端，或手动：

```powershell
$env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
```

## 控制台中文乱码

harness 的持久化产物（report.json / events.jsonl / 轨迹）全部是 UTF-8。v0.1.7+
的 CLI 在 Windows 上会自动把 stdout/stderr 切到 UTF-8 并将控制台输出码页切至
65001，正常终端不再乱码。若你仍在自解析输出：

- 读取产物文件时**显式指定 UTF-8**（PowerShell 5.1 的 `Get-Content` 默认按
  ANSI/GBK 解码，是绝大多数"文件乱码"的真相）；
- 或运行前 `$env:PYTHONUTF8 = "1"`。

## Codex CLI 装了但 doctor 判 FAIL

从 Microsoft Store 安装 Codex 桌面应用后，PATH 上会留下一个 **0 字节的
`codex.exe` 别名**——那不是 CLI。`lh-harness doctor` 会实际执行
`codex --version` 验证，因此能识别。修复：卸载 Store 别名或安装 npm 版
`@openai/codex`。

## GUI（computer-use）任务

- 必须在**已登录的桌面会话**中运行 harness，不要以管理员身份运行服务类进程；
- Windows 上无需 macOS 式手动授权，但 UI Automation 要求真实桌面会话；
- 纯 CLI 任务不受影响。

## 访问 GitHub / PyPI 网络超时

`doctor` 与 `check-update` 会访问 api.github.com。若直连不稳：

- 为 git 配置代理：`git config --global http.proxy http://127.0.0.1:<port>`
- 环境变量方式：`$env:HTTPS_PROXY = "http://127.0.0.1:<port>"`
- 注意部分代理对 Go 程序（gh CLI）的 TLS 握手不稳定，git/curl 通常没问题。

## 免费网关模型限流

`opencode/*-free` 系列模型有速率限制。长任务（多轮 × 3 角色）撞限流时，
harness 会按"可恢复超时"自动重试，但表现为 executor 单集耗时变长。
对时限敏感的任务换用付费端点。

## 测试套件

```powershell
pip install -e ".[test]"
pytest tests -q
```

Windows 上无需额外依赖；`pytest-timeout` 建议一并安装以防个别用例挂死。
