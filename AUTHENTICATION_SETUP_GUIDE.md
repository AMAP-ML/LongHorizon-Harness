# LongHorizon-Harness Windows 兼容性修复 - 认证配置指南

## 当前状态

✅ **所有 Windows 兼容性问题已修复（14个）**  
✅ **Claude Code CLI 可以正常启动**  
⚠️ **需要配置 API Key 才能运行任务**

## 检测到的配置

```bash
ANTHROPIC_BASE_URL=https://ai-api.ur-xiaoyang.com  # ✅ 已设置
ANTHROPIC_API_KEY=                                   # ❌ 未设置
```

## 配置步骤

### 1. 设置 API Key（必需）

您需要设置 `ANTHROPIC_API_KEY` 环境变量：

```bash
# Windows CMD
set ANTHROPIC_API_KEY=your-api-key-here

# Windows PowerShell
$env:ANTHROPIC_API_KEY="your-api-key-here"

# 永久设置（系统环境变量）
# 1. 右键"此电脑" -> 属性
# 2. 高级系统设置 -> 环境变量
# 3. 新建用户变量或系统变量
#    名称: ANTHROPIC_API_KEY
#    值: your-api-key-here
```

### 2. 验证配置

```bash
# 测试 Claude Code
claude --print "hello"

# 如果配置正确，应该返回响应而不是 "Not logged in"
```

### 3. 运行 LongHorizon-Harness 任务

```bash
python -m lh_harness.cli run --task "请读取 README.md 文件并告诉我项目的名称" --agent claude_code --max-rounds 3
```

## 关于 ccswitch

您提到使用 `ccswitch` 来管理 Claude Code 的 API。如果 `ccswitch` 是一个自定义工具：

1. **确认它是否已运行**
   ```bash
   # 检查进程
   tasklist | findstr ccswitch
   ```

2. **确认它如何设置环境变量**
   - 是否自动设置 `ANTHROPIC_API_KEY`？
   - 是否需要手动激活？

3. **如果 ccswitch 提供了 API endpoint**
   - `ANTHROPIC_BASE_URL` 已正确设置为 `https://ai-api.ur-xiaoyang.com`
   - 只需要添加对应的 API Key

## 故障排除

### 问题 1: "Not logged in · Please run /login"

**原因**: `ANTHROPIC_API_KEY` 未设置

**解决**:
```bash
set ANTHROPIC_API_KEY=your-api-key-here
```

### 问题 2: API Key 设置后仍然失败

**检查**:
```bash
# 确认环境变量已设置
echo %ANTHROPIC_API_KEY%

# 应该显示您的 API Key，而不是空白
```

### 问题 3: 自定义 API endpoint 不工作

**确认**:
1. `ANTHROPIC_BASE_URL` 正确指向您的 API 服务器
2. API Key 与该服务器匹配
3. 服务器支持 Claude Code 使用的 API 版本

## 测试命令

配置完成后，运行以下命令测试：

```bash
# 1. 测试 Claude Code
claude --print "test"

# 2. 测试 LongHorizon-Harness 兼容性
python tests/test_windows_compatibility.py

# 3. 运行简单任务
python -m lh_harness.cli run --task "hello world" --agent claude_code --max-rounds 1

# 4. 运行完整任务
python -m lh_harness.cli run --task "请读取 README.md 文件并告诉我项目的名称" --agent claude_code --max-rounds 3
```

## 下一步

1. ✅ 所有 Windows 兼容性问题已修复
2. ⏳ 等待您设置 `ANTHROPIC_API_KEY`
3. ⏳ 继续测试循环，确保任务可以完整执行

设置完成后，告诉我继续测试！

---

**修复完成日期**: 2026-09-28  
**测试环境**: Windows 11, Python 3.12  
**状态**: ✅ 兼容性修复完成，等待认证配置
