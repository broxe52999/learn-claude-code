# s10_team_protocols.py 对比 s09_agent_teams.py 新增/变更方法文档

> 对比基准：`s09_agent_teams.py`（9 个 Lead 工具，Teammate 6 个工具）
> 对比目标：`s10_team_protocols.py`（12 个 Lead 工具，Teammate 8 个工具）
> s10 在保留 s09 全部功能基础上，新增了**协议层**——关闭协议 + 计划审批协议。

### ⚠️ 前置说明：`req_id` 与 `request_id` 的关系

在源代码和本文档中，你会看到两种写法：

| 写法 | 本质 | 出现场景 |
|------|------|----------|
| `req_id` | Python **局部变量名** | 函数体内 `req_id = str(uuid.uuid4())[:8]`，作为 `shutdown_requests[req_id]` 的字典 key |
| `request_id` | **公开接口名**——工具参数名、JSON 键名、函数形参名 | 工具 schema `"request_id": {"type": "string"}`，消息体 `{"request_id": req_id}`，函数签名 `def handle(request_id)` |

**两者承载的是同一个 UUID 值，只是在不同上下文中换了名字：**

```python
req_id = str(uuid.uuid4())[:8]          # 生成时叫 req_id (Python 变量)
shutdown_requests[req_id] = {...}       # 字典 key 用 req_id
BUS.send(..., {"request_id": req_id})   # JSON 消息中键名叫 request_id

# 对方收到后：
req_id = args["request_id"]             # 从 JSON 键 request_id 取出 → 存入 Python 变量 req_id
```

下文在描述具体逻辑时会严格遵循源码中的用词：代码块中用 `req_id`，接口/参数描述中用 `request_id`。**两者始终指向同一个 8 字符 UUID。**

---

## 一、新增的全局数据结构

### 1. `shutdown_requests: dict`
| 维度 | 说明 |
|------|------|
| **含义** | 关闭请求追踪字典。以 `request_id` 为 key，记录每个关闭请求的目标队友和当前状态。 |
| **实现逻辑** | 全局字典，与 `_tracker_lock` 配合保证线程安全。条目结构：`{target: str, status: "pending"|"approved"|"rejected"}` |
| **作用** | 作为关闭协议 FSM 的状态存储，使 Lead 和 Teammate 两侧可以通过同一个 `request_id` 关联请求与响应，实现跨线程的状态同步。 |

### 2. `plan_requests: dict`
| 维度 | 说明 |
|------|------|
| **含义** | 计划审批请求追踪字典。以 `request_id` 为 key，记录每个审批请求的来源队友、计划内容、当前状态。 |
| **实现逻辑** | 全局字典，与 `_tracker_lock` 配合保证线程安全。条目结构：`{from: str, plan: str, status: "pending"|"approved"|"rejected"}` |
| **作用** | 作为计划审批协议 FSM 的状态存储，使得 Teammate 提交计划后可以异步等待 Lead 的审批结果。 |

### 3. `_tracker_lock: threading.Lock`
| 维度 | 说明 |
|------|------|
| **含义** | 追踪器互斥锁，保护 `shutdown_requests` 和 `plan_requests` 的并发读写。 |
| **实现逻辑** | 标准 `threading.Lock()`，在 `TeammateManager._exec()`、`handle_shutdown_request()`、`handle_plan_review()`、`_check_shutdown_status()` 中通过 `with _tracker_lock:` 上下文管理器加锁。 |
| **作用** | 因为 Teammate 各自在独立线程中运行，可能同时读写这两个字典；此锁保证原子性，防止竞态条件。 |

---

## 二、新增的独立函数（Lead 侧协议处理器）

### 4. `handle_shutdown_request(teammate: str) -> str`

| 维度 | 说明 |
|------|------|
| **含义** | Lead 主动向指定队友发起关闭请求。这是关闭协议的 **发起端**。 |
| **实现逻辑** | ① 生成 8 字符 `request_id`（`uuid4()[:8]`） → ② 在 `_tracker_lock` 保护下写入 `shutdown_requests[req_id] = {target: teammate, status: "pending"}` → ③ 通过 `BUS.send()` 以 `shutdown_request` 类型向目标队友 inbox 发送消息（携带 `request_id`）→ ④ 返回 `"Shutdown request {req_id} sent to '{teammate}' (status: pending)"` |
| **作用** | 让 LLM-Lead 有能力优雅地终止一个 Teammate 的工作（而非暴力杀线程），启动 pending → approved/rejected 的状态流转。 |

**调用链：**
```
Lead LLM 调用 shutdown_request(teammate="alice")
  → TOOL_HANDLERS["shutdown_request"]
    → handle_shutdown_request("alice")
      → 写入 shutdown_requests[req_id] = {target:"alice", status:"pending"}
      → BUS.send("lead", "alice", ..., "shutdown_request", {request_id: req_id})
      → alice 的 inbox 收到 shutdown_request 消息
```

### 5. `handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str`

| 维度 | 说明 |
|------|------|
| **含义** | Lead 对队友提交的计划进行审批——批准或拒绝。这是计划审批协议的 **响应端**。 |
| **实现逻辑** | ① 在 `_tracker_lock` 保护下从 `plan_requests` 中取出 `request_id` 对应的记录 → ② 若不存在则返回 `"Error: Unknown plan request_id"` → ③ 将 `req["status"]` 更新为 `"approved"` 或 `"rejected"` → ④ 通过 `BUS.send()` 以 `plan_approval_response` 类型向原始提交者（`req["from"]`）的 inbox 发送审批结果（携带 `request_id`, `approve`, `feedback`）→ ⑤ 返回 `"Plan approved/rejected for 'teammate_name'"` |
| **作用** | 让 LLM-Lead 拥有对 Teammate 工作计划的审批权，防止 Teammate 擅自执行高风险或错误方向的操作。 |

**调用链：**
```
Lead LLM 收到 inbox 中的 plan_approval_response (来自某个 Teammate)
  → LLM 决定 approve=True/False
  → 调用 plan_approval(request_id="abc12345", approve=True, feedback="looks good")
    → TOOL_HANDLERS["plan_approval"]
      → handle_plan_review("abc12345", True, "looks good")
        → plan_requests["abc12345"]["status"] = "approved"
        → BUS.send("lead", "alice", "looks good", "plan_approval_response", {...})
        → alice 的 inbox 收到审批结果
```

### 6. `_check_shutdown_status(request_id: str) -> str`

| 维度 | 说明 |
|------|------|
| **含义** | Lead 查询某个关闭请求的当前状态。 |
| **实现逻辑** | 在 `_tracker_lock` 保护下以 `request_id` 查询 `shutdown_requests` 字典 → 若找到则返回 JSON 序列化的状态对象 → 若未找到则返回 `{"error": "not found"}` |
| **作用** | 映射到 Lead 的 `shutdown_response` 工具，让 LLM 可以轮询关闭请求是否已被队友批准/拒绝。注意：当前实现是**被动查询**（Lead 主动查），而非异步回调。 |

---

## 三、TeammateManager 中新增的方法

### 7. `_exec()` 中新增的 `shutdown_response` 处理分支

| 维度 | 说明 |
|------|------|
| **位置** | `TeammateManager._exec()` 方法内部，`if tool_name == "shutdown_response":` 分支（共约 12 行） |
| **含义** | Teammate 对 Lead 发来的关闭请求做出响应（批准或拒绝）。 |
| **实现逻辑** | ① 从 `args` 中提取 `request_id` 和 `approve` → ② 在 `_tracker_lock` 保护下，若 `req_id` 存在于 `shutdown_requests` 中，则将 `status` 更新为 `"approved"` 或 `"rejected"` → ③ 通过 `BUS.send()` 以 `shutdown_response` 类型向 lead inbox 发送响应消息（携带 `request_id` 和 `approve`）→ ④ 返回 `"Shutdown approved"` 或 `"Shutdown rejected"` |
| **作用** | 这是 Teammate 端的关闭协议响应。当 `approve=True` 时，`_teammate_loop` 会检测到并设置 `should_exit=True`，使线程在下一轮循环时退出。 |

### 8. `_exec()` 中新增的 `plan_approval` 处理分支

| 维度 | 说明 |
|------|------|
| **位置** | `TeammateManager._exec()` 方法内部，`if tool_name == "plan_approval":` 分支（共约 12 行） |
| **含义** | Teammate 向 Lead 提交工作计划以请求审批。 |
| **实现逻辑** | ① 从 `args` 中提取 `plan` 文本 → ② 生成 8 字符 `request_id`（`uuid4()[:8]`）→ ③ 在 `_tracker_lock` 保护下写入 `plan_requests[req_id] = {from: sender, plan: plan_text, status: "pending"}` → ④ 通过 `BUS.send()` 以 `plan_approval_response` 类型向 lead inbox 发送审批请求（携带 `request_id` 和 `plan`）→ ⑤ 返回 `"Plan submitted (request_id={req_id}). Waiting for lead approval."` |
| **作用** | 这是 Teammate 端的计划审批协议发起。模型在执行重要操作前可以先提交计划让 Lead 审查，实现「人/上级在回路中」的安全机制。 |

---

## 四、新增的 LLM 工具定义

### 9. Teammate 工具：`shutdown_response`

```python
{"name": "shutdown_response",
 "description": "Respond to a shutdown request. Approve to shut down, reject to keep working.",
 "input_schema": {
     "type": "object",
     "properties": {
         "request_id": {"type": "string"},
         "approve": {"type": "boolean"},
         "reason": {"type": "string"}
     },
     "required": ["request_id", "approve"]
 }}
```

| 维度 | 说明 |
|------|------|
| **含义** | 暴露给 Teammate LLM 的工具，使其能够响应 Lead 的关闭请求。 |
| **参数** | `request_id`：要响应的关闭请求 ID；`approve`：是否同意关闭；`reason`：可选的理由说明 |
| **作用** | Teammate 的 LLM 在 inbox 中看到 `shutdown_request` 后，调用此工具来决定自己的命运——优雅退出或拒绝关闭继续工作。 |

### 10. Teammate 工具：`plan_approval`

```python
{"name": "plan_approval",
 "description": "Submit a plan for lead approval. Provide plan text.",
 "input_schema": {
     "type": "object",
     "properties": {"plan": {"type": "string"}},
     "required": ["plan"]
 }}
```

| 维度 | 说明 |
|------|------|
| **含义** | 暴露给 Teammate LLM 的工具，使其能够向 Lead 提交工作计划并等待审批。 |
| **参数** | `plan`：计划文本描述 |
| **作用** | Teammate 在执行重要/危险操作前先提交计划，通过异步 inbox 通信等待 Lead 的审批结果。这模拟了真实团队中「先出方案、再执行」的工作流程。 |

### 11. Lead 工具：`shutdown_request`

```python
{"name": "shutdown_request",
 "description": "Request a teammate to shut down gracefully. Returns a request_id for tracking.",
 "input_schema": {
     "type": "object",
     "properties": {"teammate": {"type": "string"}},
     "required": ["teammate"]
 }}
```

| 维度 | 说明 |
|------|------|
| **含义** | 暴露给 Lead LLM 的工具，使其可以向指定队友发起关闭请求。 |
| **参数** | `teammate`：目标队友名称 |
| **作用** | Lead 通过此工具优雅地终止 Teammate，获得 `request_id` 用于后续追踪状态。 |

### 12. Lead 工具：`shutdown_response`

```python
{"name": "shutdown_response",
 "description": "Check the status of a shutdown request by request_id.",
 "input_schema": {
     "type": "object",
     "properties": {"request_id": {"type": "string"}},
     "required": ["request_id"]
 }}
```

| 维度 | 说明 |
|------|------|
| **含义** | 暴露给 Lead LLM 的工具，查询某个关闭请求的当前状态。 |
| **参数** | `request_id`：由 `shutdown_request` 返回的追踪 ID |
| **作用** | Lead 可以用此工具轮询 Teammate 是否已经批准关闭。注意：**Lead 侧叫 `shutdown_response` 但实际做的是「查询」而非「响应」**，命名上 Teammate 的 `shutdown_response` 是真正的批准/拒绝，Lead 的 `shutdown_response` 只是状态查询。 |

### 13. Lead 工具：`plan_approval`

```python
{"name": "plan_approval",
 "description": "Approve or reject a teammate's plan. Provide request_id + approve + optional feedback.",
 "input_schema": {
     "type": "object",
     "properties": {
         "request_id": {"type": "string"},
         "approve": {"type": "boolean"},
         "feedback": {"type": "string"}
     },
     "required": ["request_id", "approve"]
 }}
```

| 维度 | 说明 |
|------|------|
| **含义** | 暴露给 Lead LLM 的工具，审批队友提交的计划。 |
| **参数** | `request_id`：计划审批请求的追踪 ID；`approve`：是否批准；`feedback`：可选的审批意见 |
| **作用** | Lead 在 inbox 中看到 `plan_approval_response` 后，审查计划并通过此工具批准或拒绝，审批结果会写回 Teammate 的 inbox。 |

---

## 五、被修改的方法（s09 中已存在，s10 中行为变更）

### 14. `TeammateManager._teammate_loop()` — 行为变更

| 维度 | s09 原有行为 | s10 变更后 |
|------|-------------|-----------|
| **sys_prompt** | `"Complete your task."` | `"Submit plans via plan_approval before major work. Respond to shutdown_request with shutdown_response."` |
| **退出机制** | 仅 `stop_reason != "tool_use"` 或异常时退出 | 增加 `should_exit` 标志：当 Teammate 调用 `shutdown_response(approve=True)` 时设置，下一轮循环在 poll inbox 后检查并 break |
| **最终 status** | 始终设为 `"idle"` | 根据 `should_exit` 标志：`True` → `"shutdown"`，`False` → `"idle"` |
| **工具执行反馈** | 仅将 tool_result 加入 messages | 额外检查 `if block.name == "shutdown_response" and block.input.get("approve")`，满足时设置 `should_exit = True` |

**变更意义**：Teammate 现在有三种生命周期终态——`idle`（任务自然完成）、`shutdown`（被 Lead 关闭）、异常退出。`shutdown` 状态会持久化到 `config.json`，Lead 可以通过 `list_teammates` 看到。

### 15. `TeammateManager._exec()` — 新增分发分支

在 s09 原有的 6 个 `if tool_name ==` 分支基础上，新增了 `shutdown_response` 和 `plan_approval` 两个分支（详见上文第 7、8 条）。

### 16. `TeammateManager._teammate_tools()` — 新增工具定义

在 s09 原有的 6 个工具定义基础上，新增了 `shutdown_response` 和 `plan_approval`（详见上文第 9、10 条）。

### 17. `TOOL_HANDLERS` — 从 9 个增至 12 个

| 新增 handler | 绑定函数 |
|-------------|---------|
| `"shutdown_request"` | `handle_shutdown_request` |
| `"shutdown_response"` | `_check_shutdown_status` |
| `"plan_approval"` | `handle_plan_review` |

### 18. `TOOLS` — 从 9 个增至 12 个

新增了 `shutdown_request`、`shutdown_response`、`plan_approval` 三个工具定义（详见上文第 11-13 条）。

### 19. `SYSTEM` prompt — 文本变更

| s09 | s10 |
|-----|-----|
| `"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."` | `"You are a team lead at {WORKDIR}. Manage teammates with shutdown and plan approval protocols."` |

### 20. `import` — 新增 `uuid` 模块

s10 新增 `import uuid`，用于为每个协议请求生成唯一追踪 ID。

---

## 六、完整方法/工具对照表

```
s09 (基线)                          s10 (新增/变更)
─────────────────────────────────────────────────────────────
Lead 工具 (9):                      Lead 工具 (12):
  bash                                bash                    (不变)
  read_file                           read_file               (不变)
  write_file                          write_file              (不变)
  edit_file                           edit_file               (不变)
  spawn_teammate                      spawn_teammate          (不变)
  list_teammates                      list_teammates          (不变)
  send_message                        send_message            (不变)
  read_inbox                          read_inbox              (不变)
  broadcast                           broadcast               (不变)
  ─                                   shutdown_request        (NEW)
  ─                                   shutdown_response       (NEW)
  ─                                   plan_approval           (NEW)

Teammate 工具 (6):                   Teammate 工具 (8):
  bash                                bash                    (不变)
  read_file                           read_file               (不变)
  write_file                          write_file              (不变)
  edit_file                           edit_file               (不变)
  send_message                        send_message            (不变)
  read_inbox                          read_inbox              (不变)
  ─                                   shutdown_response       (NEW)
  ─                                   plan_approval           (NEW)

独立函数:                            独立函数:
  _safe_path                          _safe_path              (不变)
  _run_bash                           _run_bash               (不变)
  _run_read                           _run_read               (不变)
  _run_write                          _run_write              (不变)
  _run_edit                           _run_edit               (不变)
  agent_loop                          agent_loop              (不变)
  ─                                   handle_shutdown_request (NEW)
  ─                                   handle_plan_review      (NEW)
  ─                                   _check_shutdown_status  (NEW)

全局数据:                            全局数据:
  (无)                                shutdown_requests       (NEW)
  (无)                                plan_requests           (NEW)
  (无)                                _tracker_lock           (NEW)

修改的方法:
  (无)                                _teammate_loop          (CHANGED)
  (无)                                _exec                   (CHANGED)
  (无)                                _teammate_tools         (CHANGED)
  (无)                                TOOL_HANDLERS           (CHANGED)
  (无)                                TOOLS                   (CHANGED)
  (无)                                SYSTEM                  (CHANGED)
  (无)                                import uuid             (NEW)
```

---

## 七、两个协议的完整时序图

### 关闭协议 (Shutdown Protocol)

> 图中 `request_id` 在源码中对应：Python 变量 `req_id`（函数内）和 JSON 键 `request_id`（消息体），二者是同一个值。

```
时间 →

Lead (主线程)                  Tracker                   Teammate (独立线程)
    │                             │                           │
    │ shutdown_request("alice")   │                           │
    │────────────────────────────>│                           │
    │  request_id="a1b2c3d4"      │                           │
    │  写入: shutdown_requests    │                           │
    │    ["a1b2c3d4"] = {         │                           │
    │      target: "alice",       │                           │
    │      status: "pending"}     │                           │
    │                             │                           │
    │ BUS → alice inbox           │                           │
    │ 类型: shutdown_request      │                           │
    │ 携带: {request_id:          │                           │
    │        "a1b2c3d4"}          │                           │
    │────────────────────────────────────────────────────────>│
    │                             │      alice inbox 收到消息   │
    │                             │      LLM 决定 approve?     │
    │                             │      shutdown_response(    │
    │                             │        request_id=         │
    │                             │          "a1b2c3d4",       │
    │                             │        approve=True)       │
    │                             │<───────────────────────────│
    │                             │  shutdown_requests         │
    │                             │    ["a1b2c3d4"]            │
    │                             │    .status = "approved"    │
    │                             │                           │
    │ alice → lead inbox          │                           │
    │ 类型: shutdown_response     │                           │
    │ 携带: {request_id:          │                           │
    │        "a1b2c3d4",          │                           │
    │        approve:true}        │                           │
    │<────────────────────────────────────────────────────────│
    │                             │     should_exit = True     │
    │                             │     break loop             │
    │                             │     status → "shutdown"    │
    │                             │     线程退出                │
    │                             │                           │
    │ shutdown_response           │                           │
    │   ("a1b2c3d4")              │                           │
    │────────────────────────────>│                           │
    │  返回: {"target":"alice",    │                           │
    │          "status":"approved"}│                           │
```

### 计划审批协议 (Plan Approval Protocol)

> 同上：图中的 `request_id` = 源码中的 Python 变量 `req_id` = JSON 键 `request_id`，三者同一个值。

```
时间 →

Teammate (独立线程)            Tracker                   Lead (主线程)
    │                             │                           │
    │ plan_approval(              │                           │
    │   plan="重构 database.py")  │                           │
    │────────────────────────────>│                           │
    │  request_id="x7y8z9w0"      │                           │
    │  写入: plan_requests        │                           │
    │    ["x7y8z9w0"] = {         │                           │
    │      from: "alice",         │                           │
    │      plan: "重构 database", │                           │
    │      status: "pending"}     │                           │
    │                             │                           │
    │ alice → lead inbox          │                           │
    │ 类型: plan_approval_response│                           │
    │ 携带: {request_id:          │                           │
    │        "x7y8z9w0",          │                           │
    │        plan:"重构 database"}│                           │
    │────────────────────────────────────────────────────────>│
    │                             │   lead inbox 收到          │
    │                             │   LLM 审查计划             │
    │                             │   plan_approval(           │
    │                             │     request_id=            │
    │                             │       "x7y8z9w0",          │
    │                             │     approve=True,          │
    │                             │     feedback="OK but       │
    │                             │      先写测试")            │
    │                             │<───────────────────────────│
    │                             │  plan_requests             │
    │                             │    ["x7y8z9w0"]            │
    │                             │    .status = "approved"    │
    │                             │                           │
    │ lead → alice inbox          │                           │
    │ 类型: plan_approval_response│                           │
    │ 携带: {request_id:          │                           │
    │        "x7y8z9w0",          │                           │
    │        approve:true,        │                           │
    │        feedback:"先写测试"} │                           │
    │<────────────────────────────────────────────────────────│
    │ LLM 读到审批结果             │                           │
    │ 继续按审批意见执行           │                           │
```

---

## 八、核心设计洞察

1. **相同的 `request_id` 关联模式**应用于两个不同领域——关闭和计划审批。这是 s10 最核心的设计抽象：`{request_id, status: pending→approved|rejected}` 的 FSM 模式是通用的，可以复用到任何需要「请求-响应」语义的 agent 间交互。

2. **异步非阻塞**：两个协议都是异步的。发起方发出请求后立即返回（不阻塞等待），通过 inbox 轮询 + tracker 查询来获取最终状态。这避免了跨线程阻塞导致的死锁风险。

3. **协议与通信解耦**：协议层（`handle_shutdown_request`, `handle_plan_review`, `_exec` 中的两个新分支）依赖 MessageBus 的 `send` 能力，但不修改 MessageBus 的任何代码。s09 的通信基础设施完全可以直接承载 s10 的新协议。

4. **Lead 侧 `shutdown_response` 命名歧义**：Lead 的 `shutdown_response` 工具实际做的是「查询」而非「响应」，而 Teammate 的同名工具才是真正的「批准/拒绝」。这是为了保持与 Anthropic API tool-use schema 的对称性而做出的命名妥协。
