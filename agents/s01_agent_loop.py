#!/usr/bin/env python3
# Harness: the loop -- the model's first connection to the real world.
"""
s01_agent_loop.py - The Agent Loop

The entire secret of an AI coding agent in one pattern:

    while stop_reason == "tool_use":
        response = LLM(messages, tools)
        execute tools
        append results

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.
"""

import os
import subprocess
import sys
import threading
import time

try:
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
    readline.parse_and_bind('set enable-meta-keybindings on')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."

TOOLS = [{
    "name": "bash",
    "description": "Run a shell command.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}]


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


class ModelInterrupted(Exception):
    """内部信号：用户取消了当前这次模型响应。"""
    pass


class InterruptController:
    """
    模型流式输出期间的中断控制器。

    主线程正在读取 Anthropic 的 SSE 流，不能同时用 input()
    等待键盘输入。为了让 Esc 在模型输出期间也能生效，这里
    启动一个后台线程监听键盘，并通过 cancelled 通知主线程停止。

    Ctrl+C 不在这个线程里处理。Python 会在主线程抛出
    KeyboardInterrupt，stream_llm() 会把它转换成 ModelInterrupted。
    """

    def __init__(self):
        # 用户按下 Esc，或者上层把 Ctrl+C 转成取消时，会设置这个事件。
        self.cancelled = threading.Event()
        # 当前模型流结束后设置它，用来通知键盘监听线程退出。
        self.stopped = threading.Event()
        # 当前活跃的 Anthropic stream。Esc 会关闭它，以打断阻塞中的网络读取。
        self._stream = None
        self._lock = threading.Lock()
        self._thread = None

    def __enter__(self):
        # 只有交互式终端才启动单键监听。管道、CI、重定向输入场景
        # 不应该让后台线程去读 stdin。
        if sys.stdin.isatty():
            self._thread = threading.Thread(target=self._watch_esc, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc, exc_tb):
        # 模型响应结束或被中断后，通知监听线程退出，并解除 stream 引用。
        self.stopped.set()
        self.set_stream(None)
        if self._thread:
            self._thread.join(timeout=0.2)

    def set_stream(self, stream):
        # 监听线程和 stream_llm() 主线程都会访问 _stream，所以加锁。
        with self._lock:
            self._stream = stream

    def cancel(self):
        # 只设置 Event 不够：如果主线程正在等网络返回下一个 token，
        # 它可能暂时看不到 cancelled。主动关闭 stream 可以更快解除阻塞。
        self.cancelled.set()
        with self._lock:
            stream = self._stream
        if stream:
            try:
                stream.close()
            except Exception:
                pass

    def _watch_esc(self):
        # Windows 和 POSIX 终端读取单键输入的 API 不一样。
        if os.name == "nt":
            self._watch_esc_windows()
        else:
            self._watch_esc_posix()

#   - 只在模型流式输出期间启动，不在普通 s01 >> 输入时运行。
#   - msvcrt.kbhit() 是非阻塞检查：看当前有没有键盘输入。                                                                                                                    ───────────────────────────────────────────
#   - 没按键时会 sleep(0.05)，也就是 50ms 检查一次，约每秒 20 次。
#   - 模型输出结束后，__exit__() 会设置 stopped，这个循环就退出。
#   - 按下 Esc 后设置 cancelled 并关闭 stream，然后循环退出。
    def _watch_esc_windows(self):
        try:
            import msvcrt
        except ImportError:
            return
        while not self.stopped.is_set() and not self.cancelled.is_set():
            if msvcrt.kbhit():
                # "\x1b" 就是 Esc 键。
                if msvcrt.getwch() == "\x1b":
                    self.cancel()
                    return
            time.sleep(0.05)

    def _watch_esc_posix(self):
        try:
            import select
            import termios
            import tty
        except ImportError:
            return

        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            return

        try:
            # cbreak 模式可以一按 Esc 就读到，不需要再按 Enter。
            # 它仍然允许 Ctrl+C 在主线程抛出 KeyboardInterrupt。
            tty.setcbreak(fd)
            while not self.stopped.is_set() and not self.cancelled.is_set():
                readable, _, _ = select.select([sys.stdin], [], [], 0.05)
                if readable and sys.stdin.read(1) == "\x1b":
                    self.cancel()
                    return
        finally:
            # 必须恢复终端设置，否则程序退出后 shell 可能还停留在单键模式。
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def stream_llm(messages: list):
    # 模型文本会边生成边 print。这个标记用于在一次响应结束后只补一个换行。
    printed_text = False
    interrupt = None
    try:
        # 键盘监听只在模型响应期间存在；普通命令行输入仍由下面的 input() 处理。
        with InterruptController() as interrupt:
            with client.messages.stream(
                model=MODEL, system=SYSTEM, messages=messages,
                tools=TOOLS, max_tokens=8000,
            ) as stream:
                # 把当前 stream 暴露给 Esc 监听线程，这样 Esc 可以主动关闭它。
                interrupt.set_stream(stream)
                for event in stream:
                    if interrupt.cancelled.is_set():
                        raise ModelInterrupted
                    # 遍历完整事件流，而不是只遍历 stream.text_stream。
                    # 这样模型正在生成 tool_use、几乎没有可见文本时，也能检查取消。
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        print(event.delta.text, end="", flush=True)
                        printed_text = True
                if interrupt.cancelled.is_set():
                    raise ModelInterrupted
                # 只有完整读完 stream 后，才会得到可安全写入 history 的完整消息。
                response = stream.get_final_message()
    except KeyboardInterrupt as e:
        # 模型输出期间，Ctrl+C 表示“取消当前响应”，不是退出整个 CLI。
        # 等待 prompt 输入时的 Ctrl+C 仍然由 input() 外层逻辑处理为退出。
        raise ModelInterrupted from e
    except Exception as e:
        # Esc 监听线程关闭 stream 后，主线程可能收到 HTTP/stream 异常。
        # 如果确实已经请求取消，就统一转换成 ModelInterrupted。
        if interrupt and interrupt.cancelled.is_set():
            raise ModelInterrupted from e
        raise
    finally:
        if printed_text:
            print()
    return response


# -- The core pattern: a while loop that calls tools until the model stops --
def agent_loop(messages: list):
    while True:
        try:
            response = stream_llm(messages)
        except ModelInterrupted:
            # 不保存半截模型输出。用一个普通文本标记占位，既保持 history 结构合法，
            # 又让后续轮次知道上一轮是用户主动取消的。
            print("[Interrupted by user]")
            messages.append({"role": "assistant", "content": "[Interrupted by user]"})
            return "interrupted"
        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})
        # If the model didn't call a tool, we're done
        if response.stop_reason != "tool_use":
            return "completed"
        # Execute each tool call, collect results
        results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\033[33m$ {block.input['command']}\033[0m")
                output = run_bash(block.input["command"])
                print(output[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": output})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms01 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        print()


#KeyboardInterrupt 监听ctrl +c
#_watch_esc_windows 监听esc