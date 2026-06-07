# s11_autonomous_agents.py — 新增内容详解

> 相对 s10 的增量分析：逐一拆解每个新增方法/机制的设计意图、实现逻辑和实际作用。

---

## 一、s10 → s11：一句话概括增量

**s10** 建立了通信基础设施（MessageBus、收件箱、shutdown/plan 协议、spawn/list），但 agent 仍然是被动的——Lead 必须手动告诉每个 teammate 做什么。

**s11** 赋予 agent **自主性**：spawn 之后 teammate 会进入 `WORK → IDLE → WORK` 的自我维系循环，在 IDLE 阶段自动扫描任务板、认领未分配任务、处理收件箱消息，超时无人理就优雅退出。核心洞察：

> *"The agent finds work itself." — 代理自己寻找工作。*

---

## 二、新增模块级常量

| 常量 | 值 | 作用 |
|---|---|---|
| `TEAM_DIR` | `.team/` | 团队配置和收件箱的父目录 |
| `INBOX_DIR` | `.team/inbox/` | 每个 agent 的 JSONL 收件箱目录 |
| `TASKS_DIR` | `.tasks/` | 任务板 JSON 文件目录 |
| `POLL_INTERVAL` | `5` (秒) | IDLE 阶段轮询间隔 |
| `IDLE_TIMEOUT` | `60` (秒) | IDLE 阶段最长等待时间 |
| `VALID_MSG_TYPES` | `{"message", "broadcast", "shutdown_request", "shutdown_response", "plan_approval_response"}` | 白名单校验，防止非法消息类型污染收件箱 |

**新增原因**：s10 中这些目录和超时要么硬编码、要么不存在。s11 将它们提升为模块常量，统一管理轮询节奏和消息合法性。

---

## 三、新增全局追踪器与锁

```python
shutdown_requests = {}   # {request_id: {"target": str, "status": "pending|approved|rejected"}}
plan_requests = {}       # {request_id: {"from": str, "plan": str, "status": "pending|approved|rejected"}}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()
```

### 3.1 `shutdown_requests` / `plan_requests` — 请求追踪器

**设计意图**：s10 中的协议是发后即忘（fire-and-forget），Lead 发出 shutdown_request 后无法查询状态。s11 引入全局 dict 作为请求状态追踪器，使 `shutdown_response` 工具可以查询请求当前状态。

**实现逻辑**：
- 发出请求时：`shutdown_requests[req_id] = {"target": name, "status": "pending"}`
- 收到响应时：`shutdown_requests[req_id]["status"] = "approved" | "rejected"`
- 查询时：`shutdown_requests.get(request_id, {"error": "not found"})`

**关键作用**：实现 **请求-响应闭环追踪**，Lead 可以确认 teammate 是否同意关闭。

### 3.2 `_tracker_lock` — 追踪器互斥锁

保护 `shutdown_requests` 和 `plan_requests` 的读写，因为多个 teammate 线程可能同时修改这些全局 dict。

### 3.3 `_claim_lock` — 任务认领互斥锁 ★ 新增关键

**设计意图**：防止两个 agent 同时认领同一个任务（race condition）。

**实现逻辑**（详见 `claim_task` 方法）：
```
with _claim_lock:           # 进入临界区
    读任务文件
    检查 owner 是否为空      # 双重检查
    检查 status == pending
    检查 blockedBy 为空
    写入 owner + status
```

**为什么重要**：没有这个锁，两个 agent 可能同时读到 `owner=""` 的同一个任务，然后都认为自己认领成功，导致任务被重复执行。

---

## 四、新增方法详解

### 4.1 `scan_unclaimed_tasks()` — 任务板扫描 ★ 核心新增

```python
def scan_unclaimed_tasks() -> list:
```

**设计意图**：agent 自主性的"眼睛"——让 agent 能够发现有哪些活可以干。

**实现逻辑**：
1. 确保 `.tasks/` 目录存在
2. 遍历 `.tasks/task_*.json`（按文件名排序，保证顺序确定性）
3. 筛选条件（三个条件必须同时满足）：
   - `status == "pending"` — 未开始
   - `owner == ""` — 无人认领
   - `blockedBy == []` — 无阻塞依赖
4. 返回符合条件的所有任务列表

**关键作用**：IDLE 阶段的 poll 循环调用此方法，每次轮询都会检查是否有新任务。这意味着 **任务可以在 agent 运行期间动态添加**——Lead 或其他 agent 随时创建新任务，已运行的 agent 会自动发现并认领。

**边界情况**：
- `.tasks/` 目录为空 → 返回空列表，agent 继续等待或超时
- 所有任务都已被认领 → 返回空列表
- 任务有阻塞依赖 → 暂不认领，等依赖解除后下一轮 poll 发现

---

### 4.2 `claim_task(task_id, owner)` — 原子任务认领 ★ 核心新增

```python
def claim_task(task_id: int, owner: str) -> str:
```

**设计意图**：将任务从"无人认领"状态原子性地转移到"某 agent 持有"状态。这是任务分配的去中心化实现——没有中央调度器，agent 自己抢任务。

**实现逻辑（`_claim_lock` 保护下的临界区）**：

```
Step 1: 构造任务文件路径 .tasks/task_{task_id}.json
Step 2: 检查文件是否存在 → 不存在返回错误
Step 3: 读取 JSON，逐项校验：
   ├─ owner 是否为空？        → 已被认领 → 返回 "claimed by {owner}"
   ├─ status 是否为 pending？ → 状态不对 → 返回错误
   └─ blockedBy 是否为空？   → 有阻塞   → 返回错误
Step 4: 写入 owner + status="in_progress"
Step 5: 持久化到文件
Step 6: 返回成功消息
```

**为什么放在锁内**：Step 3（检查）和 Step 4（写入）之间必须是原子的。如果两个线程交替执行：
```
线程A: 检查 owner=""  ✓
线程B: 检查 owner=""  ✓    ← 两个都通过了！
线程A: 写入 owner="A"
线程B: 写入 owner="B"      ← 覆盖了 A！
```
有了 `_claim_lock`，整个 Check-then-Act 是原子的。

**错误处理（5 种返回路径）**：
| 场景 | 返回值 |
|---|---|
| 任务文件不存在 | `"Error: Task {id} not found"` |
| 已被他人认领 | `"Error: Task {id} has already been claimed by {owner}"` |
| 状态不是 pending | `"Error: Task {id} cannot be claimed because its status is '{status}'"` |
| 有阻塞依赖 | `"Error: Task {id} is blocked by other task(s)..."` |
| 认领成功 | `"Claimed task #{id} for {owner}"` |

---

### 4.3 `make_identity_block(name, role, team_name)` — 身份重新注入

```python
def make_identity_block(name: str, role: str, team_name: str) -> dict:
```

**设计意图**：解决 LLM 上下文压缩后的"失忆"问题。

**问题背景**：当 agent 的消息历史越来越长（几十轮工具调用），可能会触发上下文压缩（context compression）。压缩后的短消息列表只保留最近的交互，agent 可能"忘记"自己是谁、什么角色、属于哪个团队。

**实现逻辑**：
```python
return {
    "role": "user",
    "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
}
```

**触发时机**（在 `_loop` 的 IDLE 阶段）：
```python
if len(messages) <= 3:                     # 信号：消息列表很短 → 可能被压缩过
    messages.insert(0, make_identity_block(...))  # 在消息列表头部插入身份
    messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
```

**为什么是 `<= 3`**：
- 正常情况：system prompt + user prompt + assistant response + tool results... → 消息列表很长
- 压缩后：可能只剩 1-3 条核心消息
- `<= 3` 是一个启发式信号（heuristic），表示"消息列表太短了，agent 可能失忆了"

**关键作用**：保证 agent 在任何时候都知道"我是谁、我的角色是什么、我属于哪个团队"。

---

### 4.4 `TeammateManager._loop(name, role, prompt)` — 自主生命周期循环 ★★★ 最核心新增

```python
def _loop(self, name: str, role: str, prompt: str):
```

**设计意图**：这是整个 s11 的心脏。让一个 teammate 从"被动执行一次任务"变成"持续运行的自主 worker"。生命周期：

```
   +-------+
   | spawn |  (线程启动)
   +---+---+
       |
       v
   +-------+  tool_use    +-------+
   | WORK  | <----------- |  LLM  |
   +---+---+              +-------+
       |
       | stop_reason != tool_use 或 模型调用 idle 工具
       v
   +--------+
   | IDLE   | poll every 5s for up to 60s
   +---+----+
       |
       +---> 检查收件箱 → 有新消息 → resume WORK
       |
       +---> 扫描 .tasks/ → 有未认领任务 → 自动认领 → resume WORK
       |
       +---> 超时 (60s) → shutdown (线程退出)
```

**完整实现逻辑（逐段分析）**：

#### 第一层：初始化

```python
team_name = self.config["team_name"]
sys_prompt = f"You are '{name}', role: {role}, team: {team_name}..."
messages = [{"role": "user", "content": prompt}]
tools = self._teammate_tools()

while True:   # ← 外层无限循环，agent 永远不会主动退出（除非 shutdown）
```

- 每个 teammate 有自己专属的 system prompt，包含名字、角色、团队
- 从 spawn 时传入的 prompt 作为第一条 user 消息
- `_teammate_tools()` 返回 base tools + communication + idle + claim_task

#### 第二层：WORK 阶段

```python
for _ in range(50):   # ← 最多 50 轮工具调用，防止无限循环
```

每轮执行：
1. **drain 收件箱**：`BUS.read_inbox(name)` — 读并清空，检查是否有 shutdown_request
2. **调用 LLM**：`client.messages.create(model=MODEL, system=sys_prompt, messages=messages, tools=tools, max_tokens=8000)`
3. **判断停止原因**：
   - `stop_reason != "tool_use"` → 模型说完了，进入 IDLE
   - 模型调用了 `idle` 工具 → 主动请求空闲，进入 IDLE
4. **执行工具**：调用 `self._exec(name, block.name, block.input)`
5. **拼接结果**：将 tool_result 追加回 messages

**退出 WORK 阶段的条件**：
- 模型自然停止（说完话）
- 模型主动调用 idle 工具
- 超过 50 轮工具调用（安全阀）
- 收到 shutdown_request

#### 第三层：IDLE 阶段

```python
self._set_status(name, "idle")
polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)   # 60s / 5s = 12 次
for _ in range(polls):
    time.sleep(POLL_INTERVAL)   # 等 5 秒

    # 来源1: 收件箱
    inbox = BUS.read_inbox(name)
    if inbox:
        for msg in inbox:
            if msg["type"] == "shutdown_request":
                self._set_status(name, "shutdown")
                return
            messages.append(...)  # 拼入消息
        resume = True
        break

    # 来源2: 任务板
    unclaimed = scan_unclaimed_tasks()
    if unclaimed:
        task = unclaimed[0]
        result = claim_task(task["id"], name)
        if result.startswith("Error:"):
            continue   # 认领失败，等下一轮
        # 身份重新注入（如果需要）
        if len(messages) <= 3:
            messages.insert(0, make_identity_block(...))
            messages.insert(1, ...)
        # 将任务拼入消息
        messages.append({"role": "user", "content": task_prompt})
        messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
        resume = True
        break

if not resume:
    self._set_status(name, "shutdown")
    return   # 线程退出
self._set_status(name, "working")   # 回到 WORK
```

**两个唤醒源**：
| 唤醒源 | 触发条件 | 行为 |
|---|---|---|
| 收件箱消息 | 其他 agent 发来了消息 | 读入消息，恢复 WORK |
| 未认领任务 | `scan_unclaimed_tasks()` 非空 | 自动认领，拼接任务提示，恢复 WORK |

**两种退出方式**：
| 退出方式 | 触发条件 | 行为 |
|---|---|---|
| shutdown_request | 收到 Lead 的关闭请求 | 立即退出线程 |
| 超时 | 60s 内无消息、无新任务 | 优雅退出线程 |

**身份重新注入的条件判断**：
```python
if len(messages) <= 3:
```
只在消息列表很短时才注入身份。正常情况下 agent 的消息历史很长，不需要注入。

---

### 4.5 `TeammateManager._exec(sender, tool_name, args)` — 工具执行分发器（增强版）

相比 s10，新增了两个工具的处理：

```python
if tool_name == "idle":
    # 不在 _exec 中处理，而是在 _loop 中检测 idle 工具调用
    # _exec 返回一个占位字符串

if tool_name == "claim_task":
    return claim_task(args["task_id"], sender)
    # 使用 sender 的名称作为 owner，让 agent 认领任务
```

**关键设计**：`idle` 工具虽然定义为工具，但它的"执行"不是在 `_exec` 中完成的——`_exec` 只返回一个状态字符串。真正的 IDLE 切换逻辑在 `_loop` 中通过检测 `block.name == "idle"` 实现。这是一层**工具语义与实际执行的分离**。

---

### 4.6 `TeammateManager._load_config()` / `_save_config()` — 团队持久化

```python
def _load_config(self) -> dict:
    if self.config_path.exists():
        return json.loads(self.config_path.read_text())
    return {"team_name": "default", "members": []}

def _save_config(self):
    self.config_path.write_text(json.dumps(self.config, indent=2))
```

**新增于 s11**：s10 的 TeammateManager 没有持久化，重启即丢失。s11 将团队配置写入 `.team/config.json`，每次状态变更即时落盘。

**存储结构**：
```json
{
  "team_name": "default",
  "members": [
    {"name": "coder", "role": "backend", "status": "working"},
    {"name": "reviewer", "role": "code-review", "status": "idle"}
  ]
}
```

---

### 4.7 `TeammateManager._find_member(name)` / `_set_status(name, status)`

```python
def _find_member(self, name: str) -> dict:
    for m in self.config["members"]:
        if m["name"] == name:
            return m
    return None

def _set_status(self, name: str, status: str):
    member = self._find_member(name)
    if member:
        member["status"] = status
        self._save_config()    # ← 状态变更即时持久化
```

**设计意图**：使 teammate 状态对外可观测。Lead 通过 `list_teammates` 可以看到每个 agent 是 working / idle / shutdown。`_set_status` 在 `_loop` 中被多次调用，标记 agent 当前处于哪个阶段。

---

### 4.8 `TeammateManager.member_names()` — 成员名称列表

```python
def member_names(self) -> list:
    return [m["name"] for m in self.config["members"]]
```

**新增原因**：s10 的 `broadcast` 需要知道所有队友的名字，但之前没有这个方法。s11 中 `BUS.broadcast("lead", content, TEAM.member_names())` 使用它来获取广播目标列表。

---

### 4.9 `MessageBus.send()` — 增强：消息类型校验

```python
def send(self, sender, to, content, msg_type="message", extra=None):
    if msg_type not in VALID_MSG_TYPES:           # ← s11 新增校验
        return f"Error: Invalid type '{msg_type}'"
    ...
```

**新增原因**：防止 agent 或 LLM 幻觉产生的非法消息类型污染收件箱。白名单校验保证只有已知的 5 种协议消息会被写入。

---

## 五、新增工具（Teammate + Lead 共用）

### 5.1 `idle` 工具

```python
{"name": "idle",
 "description": "Signal that you have no more work. Enters idle polling phase.",
 "input_schema": {"type": "object", "properties": {}}}
```

**设计意图**：让模型能够**主动**声明"我干完了"。不需要 Lead 来询问，agent 自己判断当前任务已结束并进入空闲状态。

**在 `_loop` 中的处理**：
```python
if block.name == "idle":
    idle_requested = True
    output = "Entering idle phase. Will poll for new tasks."
```
设置 `idle_requested = True`，跳出 WORK for 循环，进入 IDLE 阶段。

**Lead 版本**：`lambda **kw: "Lead does not idle."` — Lead 不应该空闲，简单拒绝。

---

### 5.2 `claim_task` 工具

```python
{"name": "claim_task",
 "description": "Claim a task from the task board by ID.",
 "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}}
```

**设计意图**：让模型能够**主动**认领指定 ID 的任务。虽然 IDLE 阶段的自动认领已经覆盖了主要场景，但模型在 WORK 阶段也可能想手动认领某个特定任务。

**Teammate 版本**：`claim_task(args["task_id"], sender)` — 使用 teammate 自己的名字
**Lead 版本**：`claim_task(kw["task_id"], "lead")` — 使用 "lead" 作为 owner

---

## 六、新增 CLI 快捷命令

在 `__main__` 交互循环中新增了三个斜杠命令：

```python
if query.strip() == "/team":
    print(TEAM.list_all())    # 查看团队状态
    continue
if query.strip() == "/inbox":
    print(json.dumps(BUS.read_inbox("lead"), indent=2))  # 查看 Lead 收件箱
    continue
if query.strip() == "/tasks":
    # 遍历 .tasks/ 目录，格式化输出任务板
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        t = json.loads(f.read_text())
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[t["status"]]
        owner = f" @{t['owner']}" if t.get("owner") else ""
        print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
    continue
```

**设计意图**：快速查看系统状态，无需退出交互或另开终端：
- `/team` — 查看所有 agent 的状态
- `/inbox` — 查看 Lead 收到了什么消息
- `/tasks` — 查看任务板的当前状态（谁在做什么）

---

## 七、Lead agent loop 增强

### 7.1 收件箱轮询

```python
def agent_loop(messages):
    while True:
        inbox = BUS.read_inbox("lead")     # ← s11 新增
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        ...
```

**新增原因**：s10 的 Lead 不会主动检查收件箱。s11 在每轮 LLM 调用前 drain 收件箱，使得 teammate 发来的 shutdown_response、plan_approval_response 等消息能被 Lead 及时看到。

---

## 八、TeammateManager.spawn() 增强

```python
def spawn(self, name, role, prompt):
    member = self._find_member(name)
    if member:
        if member["status"] not in ("idle", "shutdown"):   # ← s11 新增状态检查
            return f"Error: '{name}' is currently {member['status']}"
        member["status"] = "working"   # ← s11：恢复已有队友
        member["role"] = role
    else:
        member = {"name": name, "role": role, "status": "working"}  # ← s11：新建
        self.config["members"].append(member)
    self._save_config()    # ← s11：即时持久化
    thread = threading.Thread(
        target=self._loop,          # ← s11：从单次执行变为自主循环
        args=(name, role, prompt),
        daemon=True,
    )
    self.threads[name] = thread
    thread.start()
```

**相比 s10 的关键变化**：
| 方面 | s10 | s11 |
|---|---|---|
| 重复 spawn | 未处理 | 如果 status 是 idle/shutdown，可重新激活 |
| 线程目标 | 单次执行函数 | `_loop` : WORK→IDLE→WORK 自主循环 |
| 状态管理 | 无 | working → idle → shutdown 状态机 |
| 持久化 | 无 | 每次 spawn 即时写入 config.json |

---

## 九、架构全景对比：s10 vs s11

```
s10 (通信基础设施):
┌─────────┐  send_message   ┌───────────┐
│  Lead   │ ◄──────────────►│ Teammate  │  (一次性执行，结束即退出)
└─────────┘                 └───────────┘

s11 (自主代理):
┌─────────┐  send/broadcast  ┌─────────────────────────────────┐
│  Lead   │ ◄───────────────►│ Teammate (自主循环)              │
│         │                  │                                 │
│         │                  │  ┌──────┐    ┌──────┐           │
│         │                  │  │ WORK │◄──►│ IDLE │           │
│         │                  │  └──┬───┘    └──┬───┘           │
│         │                  │     │            │              │
│         │                  │     │    ┌───────┴──────────┐   │
│         │                  │     │    │ poll inbox        │   │
│         │                  │     │    │ scan .tasks/      │   │
│         │                  │     │    │ auto-claim        │   │
│         │                  │     │    │ identity re-inject│   │
│         │                  │     │    └──────────────────┘   │
│         │                  └─────────────────────────────────┘
└─────────┘
        │
        ▼
  ┌───────────┐
  │ .tasks/   │  ← 任务板：共享工作来源
  │ task_*.json│
  └───────────┘
```

---

## 十、设计精髓：为什么这样设计？

### 10.1 自主性 ≠ 失控

agent 可以自己找活干，但有安全边界：
- 每轮 WORK 最多 50 次工具调用（防止无限循环）
- IDLE 60s 超时自动退出（防止僵尸进程）
- claim_task 有锁保护（防止竞态）
- 危险命令依然被拦截

### 10.2 去中心化调度

没有中央调度器。任务的分配是通过 **agent 竞争 claim_task** 来实现的：
- 优点：简单、无单点故障、自然负载均衡
- 缺点：无法做优先级调度、无法保证任务被最优 agent 认领

这是"足够好"哲学——对于 agent 团队协作场景，竞争式认领已经足够。

### 10.3 文件即协议

所有协调都通过文件系统完成：
- 任务状态 → `.tasks/task_{id}.json`
- 消息通信 → `.team/inbox/{name}.jsonl`
- 团队状态 → `.team/config.json`

无需消息队列、无需数据库、无需共识算法。这种极简设计使得系统：
- 可以被 `cat`/`grep`/`jq` 直接调试
- 崩溃后状态不丢失
- 零运维依赖

### 10.4 身份重新注入是"胶水"

LLM 的上下文压缩是一个现实问题。`make_identity_block` 用最轻量的方式（一条 user 消息）解决了"我是谁"的问题，避免了 agent 在长对话后行为漂移。

---

## 十一、方法索引速查

| 方法/函数 | 所属 | 新增/增强 | 一句话作用 |
|---|---|---|---|
| `scan_unclaimed_tasks()` | 模块级 | ★ 新增 | 扫描 .tasks/ 返回可认领任务列表 |
| `claim_task(task_id, owner)` | 模块级 | ★ 新增 | 原子性认领任务（有锁保护） |
| `make_identity_block(name, role, team)` | 模块级 | ★ 新增 | 生成身份标识消息块 |
| `TeammateManager._loop()` | 类方法 | ★ 新增 | teammate 自主生命周期循环 |
| `TeammateManager._exec()` | 类方法 | 增强 | 新增 idle/claim_task 分发 |
| `TeammateManager._load_config()` | 类方法 | ★ 新增 | 加载团队配置 |
| `TeammateManager._save_config()` | 类方法 | ★ 新增 | 持久化团队配置 |
| `TeammateManager._find_member()` | 类方法 | ★ 新增 | 按名称查找团队成员 |
| `TeammateManager._set_status()` | 类方法 | ★ 新增 | 更新成员状态并持久化 |
| `TeammateManager.member_names()` | 类方法 | ★ 新增 | 获取所有成员名称列表 |
| `TeammateManager.spawn()` | 类方法 | 增强 | 支持重新激活、状态检查、持久化 |
| `MessageBus.send()` | 类方法 | 增强 | 新增消息类型白名单校验 |
| `agent_loop()` | 模块级 | 增强 | 每轮 drain 收件箱 |
| `idle` 工具 | Teammate | ★ 新增 | 模型主动声明"干完了" |
| `claim_task` 工具 | 共用 | ★ 新增 | 模型主动认领指定任务 |
| `/team` 命令 | CLI | ★ 新增 | 快速查看团队状态 |
| `/inbox` 命令 | CLI | ★ 新增 | 快速查看 Lead 收件箱 |
| `/tasks` 命令 | CLI | ★ 新增 | 快速查看任务板 |
