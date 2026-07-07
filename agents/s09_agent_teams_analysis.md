# s09_agent_teams.py — MessageBus 与 TeammateManager 详解

> 文件路径: `s09_agent_teams.py`  
> 核心主题: 基于文件邮箱 (JSONL inbox) 的多代理团队协作框架

---

## 目录

1. [整体架构概览](#1-整体架构概览)
2. [MessageBus 详解](#2-messagebus-详解)
   - [2.1 `__init__`](#21-init)
   - [2.2 `send`](#22-send)
   - [2.3 `read_inbox`](#23-read_inbox)
   - [2.4 `broadcast`](#24-broadcast)
3. [TeammateManager 详解](#3-teammatemanager-详解)
   - [3.1 `__init__`](#31-init)
   - [3.2 `_load_config`](#32-_load_config)
   - [3.3 `_save_config`](#33-_save_config)
   - [3.4 `_find_member`](#34-_find_member)
   - [3.5 `spawn`](#35-spawn)
   - [3.6 `_teammate_loop`](#36-_teammate_loop)
   - [3.7 `_exec`](#37-_exec)
   - [3.8 `_teammate_tools`](#38-_teammate_tools)
   - [3.9 `list_all`](#39-list_all)
   - [3.10 `member_names`](#310-member_names)
4. [关键设计思想总结](#4-关键设计思想总结)

---

## 1. 整体架构概览

```
.team/
├── config.json          # 团队配置: 成员名、角色、状态
└── inbox/
    ├── alice.jsonl      # Alice 的消息收件箱
    ├── bob.jsonl        # Bob 的消息收件箱
    └── lead.jsonl       # Lead 的消息收件箱
```

- **MessageBus** (`BUS`): 负责消息的发送、读取、广播。每个成员对应一个 `.jsonl` 文件，消息以追加 (append) 方式写入，读取时一次性清空 (drain)。
- **TeammateManager** (`TEAM`): 负责团队成员的生命周期管理——创建、复用、状态跟踪，以及启动独立线程运行 teammate 的 agent loop。
- **Lead 的 agent_loop**: 主线程中的 agent loop，负责接收用户输入、分解任务、拉起队友、收发消息。
- **Teammate 的 agent_loop**: 子线程中的 agent loop，负责读 inbox、调 LLM、执行工具、完成后回到 idle。

---

## 2. MessageBus 详解

`MessageBus` 是基于文件的、append-only / drain-on-read 语义的消息总线。每个成员对应一个独立的 JSONL 文件作为收件箱。

### 2.1 `__init__`

```python
def __init__(self, inbox_dir: Path):
    self.dir = inbox_dir
    self.dir.mkdir(parents=True, exist_ok=True)
```

| 项目 | 说明 |
|------|------|
| **参数** | `inbox_dir`: 收件箱目录路径（例如 `.team/inbox/`） |
| **逻辑** | 使用 `mkdir(parents=True, exist_ok=True)` 确保目录及其父目录存在，不会因目录缺失而报错 |
| **作用** | 初始化消息总线的存储位置，为后续所有收发操作提供文件系统基础 |
| **副作用** | 可能创建目录结构 |

**设计意图**: 惰性初始化——MessageBus 实例化时自动准备好存储基础设施，调用方无需关心目录是否存在。

---

### 2.2 `send`

```python
def send(self, sender: str, to: str, content: str,
         msg_type: str = "message", extra: dict = None) -> str:
```

| 项目 | 说明 |
|------|------|
| **参数** | `sender`: 发送者名称 (如 `"lead"`, `"alice"`) |
|  | `to`: 接收者名称，对应文件名 `{to}.jsonl` |
|  | `content`: 消息正文（纯文本） |
|  | `msg_type`: 消息类型，默认 `"message"`。必须属于 `VALID_MSG_TYPES` |
|  | `extra`: 可选的额外字段字典，会被合并到消息 JSON 中 |
| **返回值** | 状态字符串，如 `"Sent message to alice"` 或错误信息 |
| **逻辑** | 1. **类型校验**: 检查 `msg_type` 是否在 `VALID_MSG_TYPES` (`message`, `broadcast`, `shutdown_request`, `shutdown_response`, `plan_approval_response`) 中，不合法则返回错误 |
|  | 2. **构造消息**: 组装一个包含 `type`, `from`, `content`, `timestamp` 的字典 |
|  | 3. **合并额外字段**: 如果提供了 `extra` 字典，通过 `msg.update(extra)` 合并 |
|  | 4. **追加写入**: 打开 `{to}.jsonl` 文件（追加模式），写入一行 JSON |
| **作用** | 向指定队友发送一条消息。消息被持久化到文件，即使程序重启也不会丢失 |
| **副作用** | 向文件系统追加一行 JSON |

**关键设计**:

- **仅追加 (append-only)**: 使用 `open(path, "a")` 模式，不会覆盖已有消息，保证并发写入时不会破坏已有数据（尽管本实现未加锁，但对于教学场景足够）。
- **无锁设计**: 依赖文件系统的追加操作原子性（POSIX 保证小于 `PIPE_BUF` 的写入是原子的），假设每条 JSON 消息足够小。
- **类型白名单**: 严格校验消息类型，防止模型产生不可识别的消息类型污染通信通道。

---

### 2.3 `read_inbox`

```python
def read_inbox(self, name: str) -> list:
```

| 项目 | 说明 |
|------|------|
| **参数** | `name`: 要读取收件箱的成员名称 |
| **返回值** | 消息列表，每条消息是一个字典。若文件不存在则返回空列表 `[]` |
| **逻辑** | 1. **定位文件**: 构建路径 `{name}.jsonl` |
|  | 2. **不存在则返回空**: 若文件不存在，直接返回 `[]` |
|  | 3. **逐行解析**: 读取全部文本，按换行分割，对每一行调用 `json.loads` 反序列化 |
|  | 4. **清空文件 (drain)**: 调用 `inbox_path.write_text("")` 将文件内容置为空字符串 |
| **作用** | 一次性读取并清空某个成员的所有待处理消息。这是一种"消费即销毁"的语义 |
| **副作用** | 清空收件箱文件 |

**关键设计——Drain 语义**:

这是整个 MessageBus 最核心的设计选择：

```
read_inbox = "拿出所有消息，然后清空信箱"
```

对比其他方案：

| 方案 | 优点 | 缺点 |
|------|------|------|
| **Drain (本方案)** | 简单、无状态、无需追踪已读/未读 | 若读取后崩溃则消息丢失 |
| **标记已读** | 消息不丢失 | 需要额外元数据、复杂 |
| **消费后删除** | 可审计 | 需要逐条删除、并发复杂 |

对本教学场景而言，drain 语义在 **简单性** 和 **可靠性** 之间取得了恰当的平衡。Agent 在读取收件箱后会立即将消息注入对话历史并调用 LLM，消息已经进入模型上下文，即使此后崩溃，恢复后也只需重新执行任务即可。

---

### 2.4 `broadcast`

```python
def broadcast(self, sender: str, content: str, teammates: list) -> str:
```

| 项目 | 说明 |
|------|------|
| **参数** | `sender`: 发送者名称 |
|  | `content`: 广播内容 |
|  | `teammates`: 所有队友名称的列表 |
| **返回值** | 如 `"Broadcast to 3 teammates"` |
| **逻辑** | 遍历 `teammates` 列表，对每个不等于 `sender` 的成员调用 `self.send()`，消息类型固定为 `"broadcast"` |
| **作用** | 向所有队友（排除发送者自身）发送同一条消息。本质是 `send` 的批量封装 |
| **副作用** | 向多个收件箱文件各追加一行 JSON |

**设计意图**: 将广播抽象为一个方法而非让调用方自行循环，保证：
- 广播消息的类型统一为 `"broadcast"`，方便接收方区分
- 自动排除发送者，避免自己收到自己的广播
- 返回计数，方便调用方确认发送范围

---

## 3. TeammateManager 详解

`TeammateManager` 是团队管理的核心，负责任务分配、线程生命周期、状态持久化。

### 3.1 `__init__`

```python
def __init__(self, team_dir: Path):
    self.dir = team_dir
    self.dir.mkdir(exist_ok=True)
    self.config_path = self.dir / "config.json"
    self.config = self._load_config()
    self.threads = {}
```

| 项目 | 说明 |
|------|------|
| **参数** | `team_dir`: 团队数据目录（如 `.team/`） |
| **逻辑** | 1. 创建团队目录（如不存在） |
|  | 2. 设定配置文件路径为 `config.json` |
|  | 3. 调用 `_load_config()` 加载（或初始化）配置 |
|  | 4. 初始化空的线程字典 `threads` |
| **作用** | 初始化团队管理器，从持久化存储中恢复团队状态 |
| **副作用** | 可能创建目录、读取配置文件 |

**设计要点**: `self.threads` 是内存中的运行时状态，`self.config` 是持久化状态。两者共同描述团队：

| 状态层 | 存储位置 | 内容 |
|--------|---------|------|
| `self.config` | `.team/config.json` | 团队成员名称、角色、状态（持久化） |
| `self.threads` | 内存 | 活跃线程引用（运行时） |

---

### 3.2 `_load_config`

```python
def _load_config(self) -> dict:
    if self.config_path.exists():
        return json.loads(self.config_path.read_text())
    return {"team_name": "default", "members": []}
```

| 项目 | 说明 |
|------|------|
| **返回值** | 团队配置字典 |
| **逻辑** | 若 `config.json` 存在则解析返回；否则返回默认空团队配置 |
| **作用** | 提供"首次使用无需手动创建配置文件"的体验，也支持重启后恢复团队状态 |
| **副作用** | 无（只读） |

**默认配置结构**:

```json
{
  "team_name": "default",
  "members": []
}
```

---

### 3.3 `_save_config`

```python
def _save_config(self):
    self.config_path.write_text(json.dumps(self.config, indent=2))
```

| 项目 | 说明 |
|------|------|
| **逻辑** | 将当前 `self.config` 以 JSON 格式（带缩进）写回 `config.json` |
| **作用** | 将团队状态（成员列表、角色、状态）持久化到磁盘 |
| **副作用** | 覆盖 `config.json` |

**调用时机**: 每次 `spawn` 创建/复用成员、`_teammate_loop` 结束后状态切回 idle 时都会调用。确保配置文件始终反映最新状态。

---

### 3.4 `_find_member`

```python
def _find_member(self, name: str) -> dict:
    for m in self.config["members"]:
        if m["name"] == name:
            return m
    return None
```

| 项目 | 说明 |
|------|------|
| **参数** | `name`: 成员名称 |
| **返回值** | 成员字典（引用，可修改）或 `None` |
| **逻辑** | 线性遍历 `members` 列表，按名称匹配 |
| **作用** | 查找成员，返回值是可修改的引用，调用方可直接修改返回字典来更新状态 |
| **复杂度** | O(n)，对于小团队（<100 人）足够 |

**设计要点**: 返回的是 `self.config["members"]` 列表中元素的直接引用而非副本，因此调用方修改返回值会直接影响配置中的成员数据。这是一种"引用语义"，方便 `spawn` 和 `_teammate_loop` 直接修改成员状态。

---

### 3.5 `spawn`

```python
def spawn(self, name: str, role: str, prompt: str) -> str:
```

| 项目 | 说明 |
|------|------|
| **参数** | `name`: 队友名称 |
|  | `role`: 角色描述（如 `"coder"`, `"reviewer"`） |
|  | `prompt`: 初始任务提示词 |
| **返回值** | 状态字符串，如 `"Spawned 'alice' (role: coder)"` 或错误信息 |
| **逻辑** | 流程如下图 |

```
spawn("alice", "coder", "fix the bug")
│
├─ 成员已存在?
│   ├─ 是 → 状态是 "idle" 或 "shutdown"?
│   │   ├─ 是 → 复用: 状态设为 "working", 更新角色
│   │   └─ 否 → 返回错误 (成员正在工作中)
│   └─ 否 → 创建新成员: 状态 "working", 加入 members 列表
│
├─ _save_config() 持久化
│
├─ 创建 daemon 线程 → _teammate_loop(name, role, prompt)
├─ 存入 self.threads[name]
├─ thread.start()
│
└─ 返回 "Spawned 'alice' (role: coder)"
```

| 作用 | 派生（或复用）一个命名队友，在独立线程中启动其 agent loop |
|------|------|
| **副作用** | 修改配置文件、启动新线程 |

**关键设计决策**:

1. **可复用成员**: 如果 alice 之前完成了任务处于 idle 状态，再次 spawn 同名成员时不会创建新线程，而是复用已有线程，只更新角色和状态。
2. **防重入**: 如果成员状态为 `"working"`（正在执行任务），spawn 会返回错误，防止对同一个成员重复分配任务。
3. **Daemon 线程**: 使用 `daemon=True`，当主程序退出时，队友线程会自动终止，无需手动清理。
4. **无返回值等待**: spawn 是异步的——它启动线程后立即返回，不等待任务完成。任务结果通过消息机制异步返回。

---

### 3.6 `_teammate_loop`

```python
def _teammate_loop(self, name: str, role: str, prompt: str):
```

这是队友的核心 agent loop，在独立线程中运行。其生命周期如下图：

```
┌──────────────────────────────────────────────────────┐
│                  _teammate_loop                       │
│                                                      │
│  1. 构建 system prompt (基于 name, role, WORKDIR)     │
│  2. 初始化 messages = [user: prompt]                  │
│  3. 获取工具列表                                        │
│                                                      │
│  ┌─ for _ in range(50): ──────────────────────────┐  │
│  │                                                 │  │
│  │  a. 读取收件箱 → 注入到 messages                  │  │
│  │  b. 调用 LLM (带 system prompt + tools)          │  │
│  │  c. stop_reason ≠ "tool_use"? → break           │  │
│  │  d. 遍历 tool_use blocks:                        │  │
│  │     - 调用 _exec 执行工具                         │  │
│  │     - 收集结果                                    │  │
│  │  e. 将结果追加到 messages                        │  │
│  │                                                 │  │
│  └─────────────────────────────────────────────────┘  │
│                                                      │
│  4. 状态切回 "idle"（除非已是 "shutdown"）             │
│  5. _save_config()                                   │
└──────────────────────────────────────────────────────┘
```

| 项目 | 说明 |
|------|------|
| **参数** | `name`: 队友名称; `role`: 角色; `prompt`: 初始任务 |
| **逻辑** | 最多 50 轮循环，每轮: 读 inbox → 调 LLM → 执行工具。当 LLM 返回非 `tool_use` 的 stop_reason（即模型认为任务完成）时退出 |
| **作用** | 作为队友的"大脑"，持续运行直到任务完成或达到轮次上限 |
| **副作用** | 收发消息、执行工具、修改配置 |

**关键设计要点**:

1. **System Prompt 构建**:
   ```python
   sys_prompt = (
       f"You are '{name}', role: {role}, at {WORKDIR}. "
       f"Use send_message to communicate. Complete your task."
   )
   ```
   队友知道自己的名字、角色、工作目录，以及"用 send_message 通信"的指令。

2. **收件箱注入**: 每轮 LLM 调用前，先 drain 自己的收件箱，将消息以 JSON 格式注入对话历史：
   ```python
   inbox = BUS.read_inbox(name)
   for msg in inbox:
       messages.append({"role": "user", "content": json.dumps(msg)})
   ```
   这样队友能"看到"别人发给它的消息。

3. **安全限制**: `for _ in range(50)` 防止无限循环。如果模型陷入工具调用循环，最多执行 50 轮后自动停止。

4. **异常处理**: 如果 LLM 调用抛出异常，直接 `break` 退出循环，状态仍会切换到 idle（沉默失败，但在教学场景中可接受）。

5. **状态恢复**: 循环结束后（无论正常完成还是异常退出），都会将状态从 `"working"` 切回 `"idle"`，使成员可以被再次 spawn 复用。唯一的例外是状态已被设为 `"shutdown"`。

---

### 3.7 `_exec`

```python
def _exec(self, sender: str, tool_name: str, args: dict) -> str:
```

| 项目 | 说明 |
|------|------|
| **参数** | `sender`: 发送者名称（用于 send_message 工具的 from 字段） |
|  | `tool_name`: 工具名称 |
|  | `args`: 工具参数字典 |
| **返回值** | 工具执行结果字符串 |
| **逻辑** | 分发器 (dispatcher)：根据 `tool_name` 匹配对应的处理函数 |

分发映射：

| tool_name | 处理函数 | 说明 |
|-----------|---------|------|
| `"bash"` | `_run_bash(command)` | 执行 shell 命令 |
| `"read_file"` | `_run_read(path)` | 读取文件 |
| `"write_file"` | `_run_write(path, content)` | 写入文件 |
| `"edit_file"` | `_run_edit(path, old, new)` | 精确文本替换 |
| `"send_message"` | `BUS.send(sender, to, content, msg_type)` | 发送消息给队友 |
| `"read_inbox"` | `BUS.read_inbox(sender)` | 读取自己的收件箱 |
| _(其他)_ | `"Unknown tool: {name}"` | 未知工具 |

| 作用 | 将 LLM 的工具调用请求转化为实际的本地操作或消息通信 |
|------|------|
| **副作用** | 取决于具体工具（文件读写、消息发送等） |

**设计要点**: `_exec` 是 teammate 和 lead 共用基础工具的关键。teammate 的 `_exec` 与 lead 的 `TOOL_HANDLERS` 功能相同，但路径不同——teammate 没有 `spawn_teammate`、`list_teammates`、`broadcast` 工具（队友不能派生新队友，只能通过 send_message 通信）。

---

### 3.8 `_teammate_tools`

```python
def _teammate_tools(self) -> list:
```

| 项目 | 说明 |
|------|------|
| **返回值** | 工具定义列表，符合 Anthropic API 的 tools 格式 |
| **逻辑** | 硬编码返回 6 个工具的定义 |

**Teammate 的 6 个工具**:

| 工具 | 类别 | 说明 |
|------|------|------|
| `bash` | 基础 | 执行 shell 命令 |
| `read_file` | 基础 | 读取文件 |
| `write_file` | 基础 | 写入文件 |
| `edit_file` | 基础 | 编辑文件 |
| `send_message` | 通信 | 向其他成员发送消息 |
| `read_inbox` | 通信 | 读取自己的收件箱 |

**对比 Lead 的 9 个工具**:

Lead 额外拥有:
- `spawn_teammate` — 派生队友
- `list_teammates` — 列出所有队友
- `broadcast` — 广播消息

**设计意图**: 队友不能派生新队友（避免无限递归），不能广播（减少噪音），只能点对点通信。这反映了层级式团队结构——Lead 是协调者，teammate 是执行者。

---

### 3.9 `list_all`

```python
def list_all(self) -> str:
```

| 项目 | 说明 |
|------|------|
| **返回值** | 格式化的团队状态字符串 |
| **逻辑** | 遍历 `self.config["members"]`，格式化输出 |
| **作用** | 提供人类可读的团队状态概览 |

**输出示例**:

```
Team: default
  alice (coder): idle
  bob (reviewer): working
  charlie (tester): idle
```

| 副作用 | 无（只读） |

---

### 3.10 `member_names`

```python
def member_names(self) -> list:
```

| 项目 | 说明 |
|------|------|
| **返回值** | 所有成员名称的字符串列表，如 `["alice", "bob", "charlie"]` |
| **逻辑** | 列表推导式从 `self.config["members"]` 提取 `name` 字段 |
| **作用** | 为 `broadcast` 提供接收者列表 |

**调用关系**:

```python
TOOL_HANDLERS = {
    ...
    "broadcast": lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
}
```

`member_names()` 是 `broadcast` 的数据源——广播需要知道所有队友的名字才能遍历发送。

---

## 4. 关键设计思想总结

### 4.1 从 Subagent 到 Teammate 的演进

| 维度 | Subagent (s04) | Teammate (s09) |
|------|---------------|----------------|
| **生命周期** | spawn → execute → return → destroy | spawn → work → idle → work → ... → shutdown |
| **命名** | 匿名 | 持久化命名 |
| **状态** | 无状态 | working / idle / shutdown |
| **复用** | 不可复用 | 可反复分配任务 |
| **通信** | 单向返回摘要 | 双向消息通信 |

### 4.2 文件邮箱 vs 内存队列

选择文件（JSONL）而非内存队列作为通信媒介的原因：

- **可观察性**: 可以随时 `cat .team/inbox/alice.jsonl` 查看消息
- **可持久化**: 程序重启后消息不丢失
- **可调试**: 手工向 inbox 写入消息来测试
- **简单**: 无需引入消息队列中间件，文件系统即可

代价是：
- 性能较低（但对 LLM 调用延迟来说可以忽略）
- 无并发锁保护（教学场景中可接受）

### 4.3 Drain 语义的哲学

`read_inbox` 读取后立即清空——"消费即销毁"。这是一种 **at-most-once** 语义：

```
消息被读取 → 注入 LLM 上下文 → 文件被清空
```

如果在此过程中崩溃，消息会丢失。但对于 agent 场景，这通常是可接受的：
- LLM 已经"看到"了消息并做出了响应
- 崩溃后重启，Lead 会重新分配任务，消息会被重新生成

### 4.4 层级式团队结构

```
                    Lead (主线程)
                   /    |    \
                  /     |     \
            alice     bob     charlie
          (thread)  (thread)  (thread)
```

- **Lead** 拥有全部 9 个工具，可以 spawn、broadcast、list
- **Teammate** 只有 6 个工具，不能 spawn 新队员、不能 broadcast
- 这防止了无限递归派生和消息风暴

### 4.5 状态机

```
       spawn()
  ┌──────────────────────────┐
  │                          │
  v                          │
idle ──spawn()──▶ working ──任务完成──▶ idle
  ▲                          │
  │                          │
  └──────────────────────────┘
         spawn() 复用

shutdown (由外部触发，本文件未实现)
```

---

*文档生成时间: 2025-01*
*分析对象: s09_agent_teams.py*
