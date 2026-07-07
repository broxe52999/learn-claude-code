# s12 vs s11 新增内容详细对比文档

> 对比文件：`s12_worktree_task_isolation.py` 相对于 `s11_autonomous_agents.py` 的全部新增内容
>
> 核心一句话：**s11 解决「谁来做」（自主调度），s12 解决「在哪做」（空间隔离）。**

---

## 一、架构总览：根本性范式转换

| 维度 | s11（被替代） | s12（新增） |
|------|-------------|-----------|
| **核心隐喻** | 团队协作：Lead 管理 N 个 Teammate | 双平面架构：Control Plane + Execution Plane |
| **Agent 数量** | 多 Agent（1 Lead + N 个 Teammate 线程） | 单 Agent（一个 `agent_loop`） |
| **协调方式** | MessageBus 消息通信（`.team/inbox/*.jsonl`） | 文件系统状态（`.tasks/` + `.worktrees/`） |
| **隔离手段** | 无——所有 teammate 共享同一文件系统 | git worktree 物理目录隔离 |
| **并行策略** | 多线程 + `_claim_lock` 互斥锁 | 多 worktree，每个任务独立目录 |
| **可观测性** | 无专用机制 | EventBus JSONL 事件溯源 |
| **核心问题** | "Agent 如何自己找活干" | "多个任务如何并行且互不干扰" |

---

## 二、新增类与组件（3 个全新大类）

### 2.1 EventBus — 生命周期事件总线

**s11 状态：** ❌ 完全不存在。

**s12 实现：**

```python
class EventBus:
    def __init__(self, event_log_path: Path): ...
    def emit(self, event, task, worktree, error): ...
    def list_recent(self, limit=20): ...
```

| 维度 | 说明 |
|------|------|
| **存储位置** | `.worktrees/events.jsonl`，JSONL 格式（每行一个 JSON 对象） |
| **写入策略** | 仅追加（append-only），不可变，确保事件不可篡改 |
| **事件结构** | `{ "event": str, "ts": float, "task": {}, "worktree": {}, "error?": str }` |
| **事件类型** | `worktree.create.before`、`worktree.create.after`、`worktree.create.failed`、`worktree.remove.before`、`worktree.remove.after`、`worktree.remove.failed`、`worktree.keep`、`task.completed` |
| **对应工具** | `worktree_events` —— 让 LLM 可以回溯"发生了什么" |

**设计意图：** 为调试、审计和 Agent 自我感知提供统一的事件溯源能力。在 `worktree_create`、`worktree_remove`、`worktree_keep` 等关键操作的前后和失败点都会发射事件，形成完整的审计线索。

---

### 2.2 TaskManager — 完整任务 CRUD 管理器

**s11 状态：** 只有 3 个散落的模块级函数，功能极简：

```python
# s11 的任务代码（散落函数）
_claim_lock = threading.Lock()              # 互斥锁
def scan_unclaimed_tasks() -> list: ...     # 扫描未认领
def claim_task(task_id, owner) -> str: ...  # 认领任务
```

**s12 实现：** 完整类，约 80 行代码：

| 方法 | 实现逻辑 | 与 s11 对比 |
|------|---------|-------------|
| `create(subject, description)` | 自增 ID → 构造完整 task 对象 → 写入 `task_{id}.json` | **新增**：s11 无创建能力，任务需手动编写 JSON |
| `get(task_id)` | 从文件反序列化 JSON → 返回 | **新增**：s11 无按 ID 查询 |
| `exists(task_id)` | 检查文件是否存在 | **新增** |
| `update(task_id, status, owner)` | 校验 status 枚举值（`pending`/`in_progress`/`completed`）→ 更新字段 → 写入 | **增强**：s11 的 `claim_task` 只能设置 `in_progress` |
| `bind_worktree(task_id, worktree, owner)` | 设置 `worktree` 字段，**若为 `pending` 则自动切 `in_progress`** | **新增**：s11 无 worktree 概念 |
| `unbind_worktree(task_id)` | 清空 `worktree` 字段 | **新增** |
| `list_all()` | 遍历 `task_*.json`，带图标 `[ ]/[>]/[x]` + owner + wt 信息 | **增强**：s11 的 `scan_unclaimed_tasks` 只看 pending 且无 owner 且无 block 的任务 |

**s12 任务数据模型（新增字段）：**

```json
{
  "id": 12,
  "subject": "Implement auth refactor",
  "description": "...",
  "status": "pending|in_progress|completed",
  "owner": "coder",
  "worktree": "auth-refactor",        // ← 新增：绑定的工作树名称
  "blockedBy": [],                    // ← 新增：阻塞依赖列表（预留）
  "created_at": 1717500000.0,         // ← 新增：创建时间戳
  "updated_at": 1717500000.0          // ← 新增：最后更新时间戳
}
```

---

### 2.3 WorktreeManager — Git Worktree 生命周期管理器

**s11 状态：** ❌ 完全不存在。s11 没有任何 git worktree 概念。

**s12 实现：** 整个 s12 的核心，约 150 行代码，完整封装 git worktree 的创建/运行/保留/移除全生命周期。

#### 内部辅助方法

| 方法 | 实现 | 作用 |
|------|------|------|
| `_is_git_repo()` | 执行 `git rev-parse --is-inside-work-tree` | **门禁**：非 git 仓库时所有工作树工具优雅报错 |
| `_validate_name(name)` | 正则 `^[A-Za-z0-9._-]{1,40}$` | **安全**：防止名称注入 git 命令（如 `; rm -rf /`） |
| `_load_index()` | 读取 `.worktrees/index.json` | 索引持久化读 |
| `_save_index(data)` | 写入 `.worktrees/index.json` | 索引持久化写 |
| `_find(name)` | 在索引数组中按名称查找 | 内部查找复用 |
| `_run_git(args)` | 封装 `subprocess.run(["git", ...])`，含错误处理 | 统一 git 调用入口 |

#### 核心公开方法

##### `create(name, task_id, base_ref)` — 创建工作树

**执行流程：**

```
1. _validate_name(name)           ← 名称合法性校验
2. _find(name) → 检查重复         ← 防止同名冲突
3. 可选：验证 task_id 存在         ← 任务必须已创建
4. emit("worktree.create.before") ← 事件：创建前
5. git worktree add -b wt/{name} {path} {base_ref}
   ├─ 创建分支 wt/{name}
   └─ 在 .worktrees/{name}/ 创建物理目录
6. 写入索引条目 (name/path/branch/task_id/status/created_at)
7. 若提供 task_id → tasks.bind_worktree(task_id, name)
   └─ 自动将任务从 pending 切到 in_progress
8. emit("worktree.create.after")  ← 事件：创建成功
```

**作用：** 为每个任务创建**物理隔离的执行通道**。两个 worktree 之间的文件修改、依赖安装、构建产物完全互不干扰。

##### `remove(name, force, complete_task)` — 移除工作树

**执行流程：**

```
1. _find(name) → 加载索引条目
2. emit("worktree.remove.before")
3. git worktree remove [--force] {path}
4. 若 complete_task=True 且有关联 task_id：
   ├─ task_update(task_id, status="completed")
   ├─ task_unbind_worktree(task_id)
   └─ emit("task.completed")
5. 索引标记 status="removed" + removed_at 时间戳
6. emit("worktree.remove.after")
```

**作用：** 清理隔离通道。`complete_task` 参数实现「移除通道时自动标记任务完成」的级联操作。

##### `keep(name)` — 保留工作树

**执行流程：**

```
1. _find(name) → 加载索引条目
2. 更新 status="kept" + kept_at 时间戳
3. emit("worktree.keep")
```

**作用：** 不执行物理删除，仅标记。用于关闭阶段确认工作树成果需要保留（如重要修改尚未合并）。

##### `run(name, command)` — 在隔离目录中执行命令

**执行流程：**

```
1. 危险命令拦截（sudo, rm -rf /, shutdown, reboot, > /dev/）
2. _find(name) → 获取工作树物理路径
3. 验证路径存在
4. subprocess.run(command, shell=True, cwd=工作树路径, timeout=300)
```

**与 s11 的 `_run_bash` 关键差异：**

| 维度 | s11 `_run_bash` | s12 `worktree_run` |
|------|----------------|-------------------|
| 执行目录 | 固定 `WORKDIR` | 动态：指定 worktree 的物理路径 |
| 超时 | 120s | 300s（隔离通道内可能执行更长任务） |
| 危险拦截 | 3 项 | 5 项（新增 `> /dev/` 防输出重定向逃逸） |

##### `list_all()` / `status(name)` — 可观测性

| 方法 | 实现 | 输出示例 |
|------|------|---------|
| `list_all()` | 遍历索引 | `[active] auth-refactor -> .../.worktrees/auth-refactor (wt/auth-refactor) task=12` |
| `status(name)` | `git status --short --branch` | `## wt/auth-refactor\n M src/auth.py\n?? test_new.py` |

#### 工作树索引数据模型

```json
// .worktrees/index.json
{
  "worktrees": [
    {
      "name": "auth-refactor",
      "path": "/repo/.worktrees/auth-refactor",
      "branch": "wt/auth-refactor",
      "task_id": 12,
      "status": "active|kept|removed",
      "created_at": 1717500000.0,
      "kept_at": null,
      "removed_at": null
    }
  ]
}
```

#### 双状态机

```
工作树状态机：
  active ──(keep)──▶ kept      ← 保留，不删除物理目录
     │
     └──(remove)──▶ removed    ← git worktree remove + 索引标记

任务状态机（含 worktree 联动）：
  pending ──(bind_worktree)──▶ in_progress
     │                               │
     │                               ├──(keep)──▶ 保持 in_progress
     │                               │
     └──(update)───────────────────▶ completed
                                     ▲
                    (remove +        │
                   complete_task=True)┘
```

---

## 三、新增辅助函数

### 3.1 `detect_repo_root(cwd)` — Git 仓库根检测

```python
def detect_repo_root(cwd: Path) -> Path | None:
    """Return git repo root if cwd is inside a repo, else None."""
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], ...)
    root = Path(r.stdout.strip())
    return root if root.exists() else None
```

| 维度 | 说明 |
|------|------|
| **s11 做法** | `WORKDIR = Path.cwd()` —— 当前目录即工作根 |
| **s12 做法** | `REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR` |
| **作用** | 确保 `.tasks/`、`.worktrees/` 统一存放在仓库根目录下，而非某个子目录中。当用户在仓库子目录运行脚本时，所有 worktree 仍共享同一套任务板。 |

---

## 四、工具集完全重构

### 4.1 工具数量变化

| 类别 | s11 | s12 |
|------|-----|-----|
| 基础工具 | 4 个 | 4 个（保留） |
| 任务工具 | 2 个（`idle`、`claim_task`） | 5 个（`task_create/list/get/update/bind_worktree`） |
| 通信工具 | 5 个（`send_message/read_inbox/broadcast/shutdown_*/plan_*`） | 0 个（全部移除） |
| 管理工具 | 2 个（`spawn_teammate`、`list_teammates`） | 0 个（全部移除） |
| 工作树工具 | 0 个 | 6 个（`worktree_create/list/status/run/keep/remove`） |
| 可观测工具 | 0 个 | 1 个（`worktree_events`） |
| **合计** | **14 个** | **16 个** |

### 4.2 保留的工具（4 个）

| 工具 | 说明 |
|------|------|
| `bash` | 不变，仍在 `WORKDIR` 执行 |
| `read_file` | 不变 |
| `write_file` | 不变 |
| `edit_file` | 不变 |

### 4.3 移除的工具（10 个）及原因

| 移除的工具 | s11 中的作用 | 移除原因 |
|-----------|-------------|---------|
| `spawn_teammate` | Lead 派生队友线程 | s12 是单 Agent 模型，无 teammate 概念 |
| `list_teammates` | 列出所有队友状态 | 同上 |
| `send_message` | 点对点消息 | MessageBus 被移除，通信改为文件状态共享 |
| `read_inbox` | 读取收件箱 | 同上 |
| `broadcast` | 全员广播 | 同上 |
| `shutdown_request` | 请求队友关闭 | 无 teammate，无需优雅关闭协议 |
| `shutdown_response` | 响应关闭请求 | 同上 |
| `plan_approval` | 提交计划审批 | 审批协议被移除 |
| `idle` | 主动进入空闲轮询 | 无自主生命周期循环 |
| `claim_task` | 认领任务（线程安全） | 被 `task_update` + `task_bind_worktree` 替代 |

### 4.4 新增的工具（12 个）

#### 任务工具组（5 个）

| 工具 | 输入参数 | 实现 | 作用 |
|------|---------|------|------|
| `task_create` | `subject` (必填), `description` (可选) | `TASKS.create(subject, description)` | 在共享任务板上创建新任务 |
| `task_list` | 无 | `TASKS.list_all()` | 列出所有任务，带状态图标、owner、worktree 绑定 |
| `task_get` | `task_id` (必填) | `TASKS.get(task_id)` | 查看单个任务完整 JSON |
| `task_update` | `task_id` (必填), `status?`, `owner?` | `TASKS.update(task_id, status, owner)` | 更新任务状态或所有者 |
| `task_bind_worktree` | `task_id` (必填), `worktree` (必填), `owner?` | `TASKS.bind_worktree(task_id, worktree, owner)` | 将任务绑定到工作树，pending 自动切 in_progress |

#### 工作树工具组（6 个）

| 工具 | 输入参数 | 实现 | 作用 |
|------|---------|------|------|
| `worktree_create` | `name` (必填), `task_id?`, `base_ref?` | `WORKTREES.create(name, task_id, base_ref)` | 创建 git worktree 隔离通道 |
| `worktree_list` | 无 | `WORKTREES.list_all()` | 列出索引中所有工作树 |
| `worktree_status` | `name` (必填) | `WORKTREES.status(name)` | 查看工作树 `git status` |
| `worktree_run` | `name` (必填), `command` (必填) | `WORKTREES.run(name, command)` | **在隔离目录中执行命令** |
| `worktree_keep` | `name` (必填) | `WORKTREES.keep(name)` | 标记保留，不物理删除 |
| `worktree_remove` | `name` (必填), `force?`, `complete_task?` | `WORKTREES.remove(name, force, complete_task)` | 移除工作树，可选关联完成任务 |

#### 可观测工具组（1 个）

| 工具 | 输入参数 | 实现 | 作用 |
|------|---------|------|------|
| `worktree_events` | `limit?` (默认 20，最大 200) | `EVENTS.list_recent(limit)` | 查询最近 N 条生命周期事件 |

---

## 五、新增安全机制

| 层级 | 机制 | s11 | s12 |
|------|------|-----|-----|
| **名称层** | 工作树名称正则校验 `[A-Za-z0-9._-]{1,40}` | ❌ 无 | ✅ 新增 |
| **环境层** | Git 仓库门禁 `_is_git_repo()` | ❌ 无 | ✅ 新增（非 git 仓库时优雅报错） |
| **路径层** | `safe_path()` 防止路径逃逸 | ✅ 有 | ✅ 保留 |
| **命令层** | 危险命令拦截 | 3 项：`rm -rf /`、`sudo`、`shutdown`、`reboot` | 5 项：+`> /dev/` |
| **超时层** | 命令超时保护 | 120s（统一） | 120s（bash）/ 300s（worktree_run） |
| **仓库层** | `detect_repo_root()` 仓库根定位 | ❌ 无 | ✅ 新增 |

---

## 六、典型工作流对比

### s11 典型流程

```
用户: "重构 auth 模块"
  ↓
Lead 调用 spawn_teammate("coder", "backend", "重构 auth")
  ↓
Teammate 进入 WORK→IDLE→WORK 循环
  - 扫描 .tasks/ 找到未认领任务 → claim_task()
  - 执行 bash/read/write/edit
  - 完成 → send_message("lead", "done")
  - 无任务 → idle → 60s 超时 → shutdown
```

### s12 典型流程

```
用户: "重构 auth 模块"
  ↓
LLM 自主编排:
  1. task_create("Refactor auth module", "...")   ← 控制平面
  2. worktree_create("auth-fix", task_id=1)        ← 执行平面
     ├─ git worktree add -b wt/auth-fix ...
     └─ task_bind_worktree(1, "auth-fix")
  3. worktree_run("auth-fix", "pytest tests/auth/")  ← 隔离执行
  4. 关闭阶段:
     ├─ worktree_keep("auth-fix")     ← 成果保留
     └─ task_update(1, "completed")   ← 标记完成
```

### 并行工作流（s12 独有能力）

```
# s12 中 LLM 可以自主并行编排：
task_create("Refactor auth")     → task_id=1
task_create("Add logging")      → task_id=2

worktree_create("auth",     task_id=1)   # 通道 A
worktree_create("logging",  task_id=2)   # 通道 B

worktree_run("auth",    "npm test")      # A 和 B 的文件系统完全隔离
worktree_run("logging", "go test ./...") # 互不干扰

# 关闭阶段：
worktree_keep("auth")                           # 保留成果
worktree_remove("logging", complete_task=True)  # 清理 + 完成任务
```

---

## 七、设计哲学转变总结

| 维度 | s11 | s12 |
|------|-----|------|
| **核心洞察** | "The agent finds work itself." | "Isolate by directory, coordinate by task ID." |
| **时间 vs 空间** | 时间维度调度（空闲轮询、超时关闭） | 空间维度隔离（物理目录、并行通道） |
| **耦合方式** | 消息通信（MessageBus） | 文件状态（JSON 文件） |
| **Agent 角色** | 管理者 + 执行者层级 | 单一编排者 |
| **并发模型** | 多线程 + 锁 | 多目录 + 天然隔离 |
| **生命周期** | spawn → WORK → IDLE → shutdown | create → bind → run → keep/remove |
| **可观测性** | print 输出 | EventBus 事件溯源 + worktree_events 工具 |

---

## 八、两者关系：正交可组合

s11 和 s12 解决的问题是**正交**的：

- **s11** = 自主调度引擎（时间维度）：Agent 空闲时自动找活干
- **s12** = 空间隔离引擎（空间维度）：每个任务拥有独立文件系统通道

理论上可以组合：在每个 worktree 中跑一个自主 teammate，形成「空间隔离 + 自主调度」的完整系统。但 s12 选择了更简洁的单 Agent 模型，让 LLM 自己编排整个并行流水线——这是 Agent-Native 设计哲学的关键体现：**把编排权交给模型**。
