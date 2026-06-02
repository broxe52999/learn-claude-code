#!/usr/bin/env python3
# Harness: compression -- clean memory for infinite sessions.
# 机制：对话压缩 —— 清理记忆以实现无限会话
"""
s06_context_compact.py - Compact / 上下文压缩

Three-layer compression pipeline so the agent can work forever:
（三层压缩管线，使代理能无限工作：）

    Every turn:  / 每轮对话：
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace non-read_file tool_result content older than last 3
      with "[Previous: used {tool_name}]"
    （第 1 层：微压缩，静默执行，每轮触发）
     将超过最近 3 条的非 read_file 工具结果替换为
     "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]  / token 数是否超过 50000？
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]   / 第 2 层：自动压缩
                  Save full transcript to .transcripts/
                  保存完整对话记录到 .transcripts/
                  Ask LLM to summarize conversation.
                  请求 LLM 总结对话。
                  Replace all messages with [summary].
                  将所有消息替换为摘要。
                        |
                        v
                [Layer 3: compact tool]   / 第 3 层：手动压缩工具
                  Model calls compact -> immediate summarization.
                  模型调用 compact 工具 → 立即触发摘要。
                  Same as auto, triggered manually.
                  与自动压缩相同，但由手动触发。

Key insight: "The agent can forget strategically and keep working forever."
核心洞察：「代理可以策略性地遗忘，从而持续无限工作。」
"""

import json
import os
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# 当 token 估算值超过此阈值时触发自动压缩
THRESHOLD = 50000
# 对话记录存档目录
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 微压缩保留的最近工具结果数量
KEEP_RECENT = 3
# 结果不压缩的工具类型（read_file 的上下文需要保留作为参考）
PRESERVE_RESULT_TOOLS = {"read_file"}


def estimate_tokens(messages: list) -> int:
    """Rough token count: ~4 chars per token.
    粗略估算 token 数量：约每 4 个字符等于 1 个 token。"""
    return len(str(messages)) // 4


# -- Layer 1: micro_compact - replace old tool results with placeholders --
# -- 第 1 层：微压缩 —— 将旧的工具结果替换为占位符 --
def micro_compact(messages: list) -> list:
    """静默压缩：将旧的工具调用结果替换为简短占位符，但保留 read_file 结果。"""
    # Collect (msg_index, part_index, tool_result_dict) for all tool_result entries
    # 收集所有 tool_result 条目的位置信息
    tool_results = []
    for msg_idx, msg in enumerate(messages):
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            for part_idx, part in enumerate(msg["content"]):
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    tool_results.append((msg_idx, part_idx, part))
    if len(tool_results) <= KEEP_RECENT:
        return messages
    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    # 通过匹配之前 assistant 消息中的 tool_use_id 来找到每个结果的工具名称
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name_map[block.id] = block.name
    # Clear old results (keep last KEEP_RECENT). Preserve read_file outputs because
    # they are reference material; compacting them forces the agent to re-read files.
    # 清除旧结果（保留最近 KEEP_RECENT 条）。保留 read_file 输出，因为它们是
    # 参考资料；压缩它们会迫使代理重新读取文件。
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_id = result.get("tool_use_id", "")
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"
    return messages


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
# -- 第 2 层：自动压缩 —— 保存对话记录、生成摘要、替换全部消息 --
def auto_compact(messages: list) -> list:
    """自动压缩：将完整对话存档到磁盘，用 LLM 生成摘要，然后将消息列表替换为摘要。"""
    # Save full transcript to disk / 将完整对话记录保存到磁盘
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str) + "\n")
    print(f"[transcript saved: {transcript_path}]")
    # Ask LLM to summarize / 请求 LLM 生成摘要
    conversation_text = json.dumps(messages, default=str)[-80000:]
    focus_instruction = ""
    if focus:
        focus_instruction = f" Pay special attention to preserving details about: {focus}."
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details."
            f"{focus_instruction}\n\n" + conversation_text}],
        max_tokens=2000,
    )
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    if not summary:
        summary = "No summary generated."
    # Replace all messages with compressed summary / 用压缩后的摘要替换所有消息
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    ]


# -- Tool implementations / 工具实现 --
def safe_path(p: str) -> Path:
    """将相对路径解析为工作目录下的绝对路径，并检查路径逃逸"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行 shell 命令，阻止危险命令，超时 120 秒"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    """读取文件内容，可选限制行数"""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    """写入文件内容，自动创建父目录"""
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    """替换文件中的精确文本（仅替换第一次出现）"""
    try:
        fp = safe_path(path)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # compact 工具仅在 agent_loop 中被特殊处理（触发手动压缩）
    "compact":    lambda **kw: "Manual compression requested.",
}

TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "compact", "description": "Trigger manual conversation compression.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
]


def agent_loop(messages: list):
    """
    代理主循环，内置三层压缩：
    1. 第 1 层：每次 LLM 调用前执行微压缩（micro_compact）
    2. 第 2 层：当 token 估算值超过阈值时执行自动压缩（auto_compact）
    3. 第 3 层：当模型调用 compact 工具时执行手动压缩
    """
    while True:
        # Layer 1: micro_compact before each LLM call
        # 第 1 层：每次 LLM 调用前执行微压缩
        micro_compact(messages)
        # Layer 2: auto_compact if token estimate exceeds threshold
        # 第 2 层：当 token 估算值超过阈值时执行自动压缩
        if estimate_tokens(messages) > THRESHOLD:
            print("[auto_compact triggered]")
            messages[:] = auto_compact(messages)
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        manual_compact = False
        compact_focus = ""
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "compact":
                    # 第 3 层：模型主动请求手动压缩
                    manual_compact = True
                    compact_focus = block.input.get("focus", "")
                    output = "Compressing..."
                else:
                    handler = TOOL_HANDLERS.get(block.name)
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    except Exception as e:
                        output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})
        # Layer 3: manual compact triggered by the compact tool
        # 第 3 层：compact 工具触发手动压缩
        if manual_compact:
            print("[manual compact]")
            messages[:] = auto_compact(messages, focus=compact_focus)
            return


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()