#!/usr/bin/env python3
# Harness: autonomy -- models that find work without being told.
# 机制：自主性 —— 无需被告知就能自己找到工作的模型
"""
s11_autonomous_agents.py - Autonomous Agents / 自主代理

Idle cycle with task board polling, auto-claiming unclaimed tasks, and
identity re-injection after context compression. Builds on s10's protocols.
（空闲循环与任务板轮询、自动认领未分配任务、以及上下文压缩后的身份重新注入。
  基于 s10 的协议构建。）

    Teammate lifecycle:  /  队友生命周期：
    +-------+
    | spawn |  /  派生
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use  /  模型停止调用工具
        v
    +--------+
    | IDLE   | poll every 5s for up to 60s  /  空闲：每5秒轮询，最多60秒
    +---+----+
        |
        +---> check inbox -> message? -> resume WORK   /  检查收件箱 → 有消息 → 恢复工作
        |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK  /  扫描任务板 → 未认领 → 认领 → 恢复工作
        |
        +---> timeout (60s) -> shutdown  /  超时 → 关闭

    Identity re-injection after compression:  /  压缩后身份重新注入：
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

Key insight: "The agent finds work itself."
核心洞察：「代理自己寻找工作。」
"""

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"

# 空闲轮询间隔（秒）和超时（秒）
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."

# 有效消息类型
VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- Request trackers --
# -- 请求追踪器 --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
_claim_lock = threading.Lock()  # 任务认领互斥锁


# -- MessageBus: JSONL inbox per teammate --
# -- MessageBus：每个队友一个 JSONL 收件箱 --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        """发送消息：将 JSON 行追加到接收者的收件箱文件中"""
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        """读取并清空收件箱（drain 语义）"""
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        """向所有队友广播消息"""
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# -- Task board scanning --
# -- 任务板扫描 --、
def scan_unclaimed_tasks() -> list:
    """扫描 .tasks/ 目录，返回所有未被认领且无阻塞依赖的 pending 任务"""
    TASKS_DIR.mkdir(exist_ok=True)
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed


def claim_task(task_id: int, owner: str) -> str:
    """
    认领任务：线程安全地将任务标记为 in_progress 并设置 owner。
    检查：任务存在、未被他人认领、状态为 pending、无阻塞依赖。
    """
    with _claim_lock:
        path = TASKS_DIR / f"task_{task_id}.json"
        if not path.exists():
            return f"Error: Task {task_id} not found"
        task = json.loads(path.read_text())
        if task.get("owner"):
            existing_owner = task.get("owner") or "someone else"
            return f"Error: Task {task_id} has already been claimed by {existing_owner}"
        if task.get("status") != "pending":
            status = task.get("status")
            return f"Error: Task {task_id} cannot be claimed because its status is '{status}'"
        if task.get("blockedBy"):
            return f"Error: Task {task_id} is blocked by other task(s) and cannot be claimed yet"
        task["owner"] = owner
        task["status"] = "in_progress"
        path.write_text(json.dumps(task, indent=2))
    return f"Claimed task #{task_id} for {owner}"


# -- Identity re-injection after compression --
# -- 压缩后身份重新注入 --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    """
    生成身份标识块。当消息列表被压缩后，将此块插回消息开头，
    确保代理始终知道自己的身份。
    """
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }


# -- Autonomous TeammateManager --
# -- 自主 TeammateManager --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        """加载团队配置"""
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        """持久化团队配置"""
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        """按名称查找团队成员"""
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        """更新团队成员状态并持久化"""
        member = self._find_member(name)
        if member:
            member["status"] = status
            self._save_config()

    def spawn(self, name: str, role: str, prompt: str) -> str:
        """派生自主队友：拥有 WORK → IDLE → WORK 生命周期循环"""
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        """
        自主队友的生命周期循环：
        1. WORK 阶段：正常的 agent loop（最多 50 轮工具调用）
        2. IDLE 阶段：轮询收件箱和任务板
           - 有新消息 → 恢复 WORK
           - 有未认领任务 → 自动认领 → 恢复 WORK
           - 超时（60s）→ shutdown
        """
        team_name = self.config["team_name"]
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        messages = [{"role": "user", "content": prompt}]
        tools = self._teammate_tools()

        while True:
            # -- WORK PHASE: standard agent loop --
            # -- 工作阶段：标准 agent loop --
            for _ in range(50):
                inbox = BUS.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg)})
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                if response.stop_reason != "tool_use":
                    break
                results = []
                idle_requested = False
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # 模型主动请求进入空闲状态
                            idle_requested = True
                            output = "Entering idle phase. Will poll for new tasks."
                        else:
                            output = self._exec(name, block.name, block.input)
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                messages.append({"role": "user", "content": results})
                if idle_requested:
                    break

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            # -- 空闲阶段：轮询收件箱消息和未认领任务 --
            self._set_status(name, "idle")
            resume = False
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            for _ in range(polls):
                time.sleep(POLL_INTERVAL)
                # 检查收件箱
                inbox = BUS.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                    resume = True
                    break
                # 扫描未认领任务
                unclaimed = scan_unclaimed_tasks()
                if unclaimed:
                    task = unclaimed[0]
                    result = claim_task(task["id"], name)
                    if result.startswith("Error:"):
                        continue
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    # 身份重新注入：如果消息列表很短（可能被压缩过），插入身份块
                    if len(messages) <= 3:
                        messages.insert(0, make_identity_block(name, role, team_name))
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content": task_prompt})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break

            if not resume:
                # 空闲超时，优雅关闭
                self._set_status(name, "shutdown")
                return
            # 恢复工作
            self._set_status(name, "working")

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        """队友的工具执行分发器：基础工具 + 通信 + 协议 + 自主功能"""
        # these base tools are unchanged from s02
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        """队友可用的工具列表：基础 + 通信 + 协议 + idle + claim_task"""
        # these base tools are unchanged from s02
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
        ]

    def list_all(self) -> str:
        """列出所有团队成员及其状态"""
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        """获取所有成员名称列表"""
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- Base tool implementations (these base tools are unchanged from s02) --
# -- 基础工具实现（与 s02 相同）--
def _safe_path(p: str) -> Path:
    """将相对路径解析为工作目录下的绝对路径，并检查路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _run_bash(command: str) -> str:
    """执行 shell 命令，阻止危险命令，超时 120 秒"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def _run_read(path: str, limit: int = None) -> str:
    """读取文件内容，可选限制行数"""
    try:
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def _run_write(path: str, content: str) -> str:
    """写入文件内容，自动创建父目录"""
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def _run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的精确文本（仅替换第一次出现）"""
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead-specific protocol handlers --
# -- Lead 专用协议处理器 --
def handle_shutdown_request(teammate: str) -> str:
    """Lead 向队友发起关闭请求"""
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    """Lead 审批队友的计划"""
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    """查询关闭请求状态"""
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- Lead tool dispatch (14 tools) --
# -- Lead 工具分发（14 个工具）--
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "Lead does not idle.",
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
}

# these base tools are unchanged from s02
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    """Lead 的 agent loop：轮询收件箱 → 调用 LLM → 执行工具 → 循环"""
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        if query.strip() == "/tasks":
            # 快速查看任务板
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()


# 我发现由于工具是大模型自己决策的，导致代码流程中的一些逻辑都变成了黑盒，就是比如子agent 把完成的任务给父agent是哪一步我就完全不懂了，后来发现原来是调用了send_message

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY: Design Philosophy & Key Logic / 设计思想与关键逻辑总结
# ══════════════════════════════════════════════════════════════════════════════

"""
一、核心设计思想 (Design Philosophy)
──────────────────────────────────────

1. 自主性优先 (Autonomy-First)
   传统 agent 模型是 "人给任务 → agent 执行 → 结束"，而这里实现的是
   "agent 自己找活干"。队友 spawn 之后不是被动等待指令，而是进入
   WORK→IDLE→WORK 的自我维系循环。核心洞察就一句：
   "The agent finds work itself."

2. 任务板驱动 (Task-Board-Driven)
   所有工作通过 .tasks/task_*.json 文件来描述和追踪。任务板是团队共享的
   单一事实来源 (Single Source of Truth)。每个 JSON 包含 id/subject/
   description/status/owner/blockedBy 字段，构成了一个微型的项目管理系统。

3. 消息总线通信 (MessageBus Communication)
   JSONL 文件作为收件箱，每个 agent 一个 .team/inbox/{name}.jsonl 文件。
   这提供了：
   - 持久化：消息不丢，跨进程可读
   - 解耦：发送者和接收者不需要同时在线
   - 可审计：所有通信历史可追溯
   Drain 语义 (读后即清) 保证消息不会被重复处理。

4. 身份连续性 (Identity Continuity)
   LLM 上下文压缩（context compression）后，agent 可能"忘记"自己是谁。
   身份重新注入机制 (make_identity_block) 确保：
   - 在消息列表过短（≤3 条）时自动插入身份块
   - agent 始终知道自己的名字、角色和所属团队

5. Lead/Teammate 分级架构 (Hierarchical Architecture)
   - Lead：管理者，拥有 spawn/shutdown/broadcast/plan_approval 等管理工具
   - Teammate：工作者，拥有 bash/edit/send_message/claim_task/idle 等执行工具
   这种分级使得系统可以"一个人管理一群人"，而非扁平无组织。

6. 优雅状态机 (Graceful State Machine)
   每个 agent 的生命周期是明确的状态机：
   working → idle → working (有任务时恢复)
                    → shutdown (超时 60s 无任务)

7. 协议化协调 (Protocol-Based Coordination)
   不是简单的函数调用，而是通过消息协议来协调：
   - shutdown_request / shutdown_response：优雅关闭
   - plan_approval / plan_approval_response：计划审批
   - broadcast：全员通知
   每种协议都有 request_id 追踪，保证请求-响应可匹配。


二、关键逻辑实现 (Key Logic Implementation)
──────────────────────────────────────────

1. 自主生命周期循环 (_loop)
   ───────────────────────────
   TeammateManager._loop() 是整个系统的心脏，实现了：
   
   外层 while True 循环包裹两个阶段：

   【WORK 阶段】（最多 50 轮工具调用）
   - 每轮先 drain 收件箱，将消息拼入 messages
   - 调用 LLM 获取 response
   - 如果 stop_reason != "tool_use"（模型说完话了），进入 IDLE
   - 如果模型调用了 idle 工具（主动请求空闲），也进入 IDLE
   - 否则执行工具调用，将结果拼回 messages，继续下一轮

   【IDLE 阶段】（每 5s 轮询，最多 60s）
   - 检查收件箱 → 有新消息 → 恢复 WORK
   - 扫描 .tasks/ 目录 → 有未认领任务 → 自动认领 → 恢复 WORK
   - 超时 → 优雅 shutdown

   这个设计让 agent 永远不会"卡住"——要么在工作，要么在找活干。

2. 任务认领的线程安全 (claim_task)
   ──────────────────────────────
   使用 threading.Lock() 保护临界区，确保：
   - 检查任务是否存在
   - 检查任务状态是否为 pending
   - 检查是否已被他人认领（owner 字段）
   - 检查是否有阻塞依赖（blockedBy 字段）
   - 原子性地设置 owner 和 status = "in_progress"

   没有这个锁，两个 agent 可能同时认领同一个任务（race condition）。

3. 消息总线的 Drain 语义 (read_inbox)
   ──────────────────────────────
   BUS.read_inbox(name) 的逻辑：
   1. 打开 {name}.jsonl
   2. 读取所有 JSON 行 → 返回列表
   3. 立即清空文件 (write_text(""))
   
   这保证了每条消息恰好被处理一次 (exactly-once semantics)。
   不像消息队列有 ack 机制，这里用最简单的方式：读即清。

4. 身份重新注入 (make_identity_block)
   ──────────────────────────────
   触发条件：len(messages) <= 3（消息列表被压缩过的信号）
   
   操作：
   1. messages.insert(0, identity_block)  —— 插到最前面
   2. messages.insert(1, "I am {name}. Continuing.") —— 确认身份
   3. 然后追加任务提示
   
   这解决了 LLM 在长对话压缩后"我是谁？我在哪？"的问题。

5. 工具分发与协议处理 (_exec + TOOL_HANDLERS)
   ──────────────────────────────
   - Teammate 的 _exec() 是 if/elif 链分发，每个工具名对应一个处理函数
   - Lead 的 TOOL_HANDLERS 是 dict-lambda 分发，更紧凑
   
   协议工具（shutdown_response, plan_approval）会：
   1. 更新全局跟踪器 (shutdown_requests / plan_requests)
   2. 通过 BUS 发送响应消息给 Lead
   3. 返回状态字符串给 LLM

6. 优雅关闭链路 (Shutdown Flow)
   ──────────────────────────────
   Lead 调用 shutdown_request(teammate) →
     BUS.send("lead", teammate, ..., "shutdown_request") →
       队友在 WORK 或 IDLE 阶段 drain 收件箱 →
         发现 shutdown_request →
           _set_status("shutdown") + return（线程退出）

   整个链路是异步、非强制的，队友会在当前循环轮次结束时优雅退出。

7. 团队持久化 (Team Persistence)
   ──────────────────────────────
   TeammateManager 将团队配置写入 .team/config.json：
   { "team_name": "...", "members": [
       {"name": "...", "role": "...", "status": "..."}
   ]}
   
   每次状态变更（spawn/status change）都会即时持久化。
   这保证了系统崩溃后可以恢复团队状态。


三、与前面步骤的演进关系 (Evolution from Previous Steps)
──────────────────────────────────────────
s02: 基础 agent loop（bash/read/write/edit）
s10: 通信协议（MessageBus, inbox, shutdown, plan_approval）
s11: 自主性（idle poll, auto-claim, identity re-injection, WORK→IDLE→WORK）

s11 是 s10 的自然延伸：s10 提供了通信基础设施，s11 在此基础上
让 agent 从"被动响应"升级为"主动寻找工作"。

四、设计取舍 (Design Trade-offs)
──────────────────────────────
✓ 简单性 > 完备性：文件系统而非数据库，JSONL 而非消息队列
✓ 自主性 > 可控性：agent 自己找活干，而非 Lead 精确调度
✓ 可见性 > 性能：所有状态存在文件中，可随时 cat 查看
✗ 不适合高并发（文件锁而非分布式锁）
✗ 不适合长任务（50 轮工具调用上限，60s 空闲超时）
✗ 没有错误重试和死信队列
"""
