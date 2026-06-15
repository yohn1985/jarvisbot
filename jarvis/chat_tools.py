"""Small built-in tools for ordinary Jarvis chat.

These are intentionally primitive: shell, file read, and text search. Product-specific
skills can sit on top later, but chat needs real tools before it can be useful.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path


DEFAULT_CWD = Path("/home/yohn/projects/work")
if not DEFAULT_CWD.exists():
    DEFAULT_CWD = Path.cwd()

MAX_OUTPUT = 12000
READ_LIMIT = 20000

_DANGEROUS = re.compile(
    r"(^|[;&|]\s*|\s)("
    r"sudo|su|rm|rmdir|mv|cp|dd|mkfs|fdisk|parted|mount|umount|"
    r"reboot|shutdown|poweroff|halt|"
    r"chmod|chown|setfacl|truncate|tee|"
    r"kill|pkill|killall|"
    r"systemctl\s+(start|stop|restart|reload|disable|enable|mask|unmask|kill)|"
    r"service\s+\S+\s+(start|stop|restart|reload)|"
    r"docker\s+(rm|rmi|stop|kill|restart)|"
    r"docker\s+compose\s+(down|up|restart|rm)|"
    r"git\s+(reset|checkout|clean|push|commit|merge|rebase)|"
    r"apt|apt-get|dnf|yum|pacman|pip\s+install|npm\s+install"
    r")(\s|$)",
    re.IGNORECASE,
)
_WRITE_MARKERS = re.compile(r"(^|[^<])>(?!>)|>>|\b(curl|wget)\b.*\|\s*(sh|bash|python)", re.IGNORECASE)


def _clean_cmd(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if (text.startswith("`") and text.endswith("`")) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.strip()


def shell_safety_error(cmd: str) -> str | None:
    cmd = _clean_cmd(cmd)
    if not cmd:
        return "empty command"
    if len(cmd) > 500:
        return "command is too long"
    if "\n" in cmd:
        return "multi-line shell commands are not allowed in chat tools"
    if _DANGEROUS.search(cmd) or _WRITE_MARKERS.search(cmd):
        return "blocked because this chat shell tool is read-only; use a deliberate deploy/edit path for changes"
    return None


def run_shell(cmd: str, timeout: int = 30) -> dict:
    cmd = _clean_cmd(cmd)
    err = shell_safety_error(cmd)
    if err:
        return {"ok": False, "tool": "shell", "command": cmd, "error": err}
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            executable="/bin/bash",
            cwd=str(DEFAULT_CWD),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return {
            "ok": p.returncode == 0,
            "tool": "shell",
            "command": cmd,
            "returncode": p.returncode,
            "output": out.strip()[-MAX_OUTPUT:] or "(no output)",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": "shell", "command": cmd, "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "tool": "shell", "command": cmd, "error": str(e)[:300]}


def run_codex(task: str, timeout: int = 300) -> dict:
    """Run the full Codex CLI agent explicitly.

    This is the broad escape hatch: Codex gets its normal shell/edit/search tool loop and project
    instructions from the shared work root. It is only used for explicit /codex requests.
    """
    task = (task or "").strip()
    if not task:
        return {"ok": False, "tool": "codex", "error": "usage: /codex <task>"}
    fd, final_path = tempfile.mkstemp(prefix="jarvis-codex-final-", suffix=".txt")
    os.close(fd)
    try:
        p = subprocess.run(
            [
                "codex", "exec",
                "--cd", str(DEFAULT_CWD),
                "--sandbox", "danger-full-access",
                "-c", 'approval_policy="never"',
                "--skip-git-repo-check",
                "--output-last-message", final_path,
                "-",
            ],
            input=task,
            cwd=str(DEFAULT_CWD),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        try:
            final = Path(final_path).read_text().strip()
            if final:
                out = final
        except Exception:
            pass
        return {
            "ok": p.returncode == 0,
            "tool": "codex",
            "returncode": p.returncode,
            "output": out.strip()[-MAX_OUTPUT:] or "(no output)",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": "codex", "error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"ok": False, "tool": "codex", "error": str(e)[:300]}
    finally:
        try:
            os.unlink(final_path)
        except Exception:
            pass


def read_file(path: str) -> dict:
    raw = _clean_cmd(path)
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = DEFAULT_CWD / p
        if not p.exists() or not p.is_file():
            return {"ok": False, "tool": "read", "path": str(p), "error": "file not found"}
        data = p.read_text(errors="replace")
        truncated = len(data) > READ_LIMIT
        return {
            "ok": True,
            "tool": "read",
            "path": str(p),
            "output": data[:READ_LIMIT] + ("\n\n[truncated]" if truncated else ""),
        }
    except Exception as e:
        return {"ok": False, "tool": "read", "path": raw, "error": str(e)[:300]}


def write_file(path: str, content: str, append: bool = False) -> dict:
    raw = _clean_cmd(path)
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = DEFAULT_CWD / p
        if not p.parent.exists():
            return {"ok": False, "tool": "write", "path": str(p), "error": "parent directory does not exist"}
        if append:
            with p.open("a") as f:
                f.write(content)
        else:
            p.write_text(content)
        return {"ok": True, "tool": "write", "path": str(p), "output": "wrote file"}
    except Exception as e:
        return {"ok": False, "tool": "write", "path": raw, "error": str(e)[:300]}


def search(pattern: str, root: str | None = None) -> dict:
    pattern = (pattern or "").strip()
    if not pattern:
        return {"ok": False, "tool": "search", "error": "missing search pattern"}
    base = Path(root or DEFAULT_CWD).expanduser()
    if not base.is_absolute():
        base = DEFAULT_CWD / base
    try:
        p = subprocess.run(
            ["rg", "-n", "--hidden", "--glob", "!**/.git/**", "--", pattern, str(base)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
        return {
            "ok": p.returncode in (0, 1),
            "tool": "search",
            "command": "rg",
            "output": (out[-MAX_OUTPUT:] if out else "(no matches)"),
            "returncode": p.returncode,
        }
    except Exception as e:
        return {"ok": False, "tool": "search", "error": str(e)[:300]}


def format_result(result: dict) -> str:
    tool = result.get("tool", "tool")
    if tool == "shell":
        if result.get("ok"):
            return f"$ {result.get('command')}\n{result.get('output')}"
        return f"$ {result.get('command')}\n(error: {result.get('error') or result.get('output')})"
    if tool == "read":
        if result.get("ok"):
            return f"{result.get('path')}\n\n{result.get('output')}"
        return f"{result.get('path')}\n(error: {result.get('error')})"
    if tool == "write":
        if result.get("ok"):
            return f"{result.get('output')}: {result.get('path')}"
        return f"{result.get('path')}\n(error: {result.get('error')})"
    if tool == "search":
        if result.get("ok"):
            return result.get("output") or "(no matches)"
        return f"(search failed: {result.get('error')})"
    if tool == "codex":
        if result.get("ok"):
            return result.get("output") or "(no output)"
        return f"(codex failed: {result.get('error') or result.get('output')})"
    return str(result)


def parse_slash(text: str) -> dict | None:
    parts = (text or "").strip()[1:].split(None, 1)
    name = (parts[0] if parts else "").lower()
    rest = parts[1] if len(parts) > 1 else ""
    if name == "shell":
        return run_shell(rest)
    if name == "codex":
        return run_codex(rest)
    if name == "read":
        return read_file(rest)
    if name in ("write", "append"):
        path, sep, content = rest.partition("\n")
        if not sep:
            return {"ok": False, "tool": "write", "error": f"usage: /{name} <path> then newline then content"}
        return write_file(path.strip(), content, append=(name == "append"))
    if name == "search":
        argv = shlex.split(rest) if rest else []
        if not argv:
            return {"ok": False, "tool": "search", "error": "usage: /search <pattern> [path]"}
        return search(argv[0], argv[1] if len(argv) > 1 else None)
    return None


def maybe_direct(text: str) -> dict | None:
    """Handle explicit ordinary-language tool requests before the LLM.

    This is deliberately conservative. Ambiguous questions still go to the model; direct shell
    requests like "give me the uptime" should not come back as a code block.
    """
    raw = (text or "").strip()
    low = raw.lower()
    if not raw:
        return None
    if raw.startswith("!") or raw.startswith("$ "):
        return run_shell(raw[1:].strip() if raw.startswith("!") else raw[2:].strip())
    if low in ("uptime", "server uptime", "machine uptime") or (
        "uptime" in low and any(w in low for w in ("give", "get", "show", "server", "machine", "what", "tell"))
    ):
        return run_shell("uptime")
    m = re.match(r"^(?:please\s+)?(?:run|execute)\s+(?:the\s+)?(?:shell\s+)?(?:command\s+)?(.+)$", raw, re.I)
    if m:
        return run_shell(m.group(1))
    m = re.match(r"^(?:what(?:'s| is)?|show|give me|get me)\s+(?:the\s+)?(?:output of\s+)?`([^`]+)`\??$", raw, re.I)
    if m:
        return run_shell(m.group(1))
    return None


BUILTIN_HELP = """/shell <command>  run a read-only shell command
/codex <task>     run the full Codex CLI agent with shell/edit/search tools
/read <path>       read a local file
/write <path>      write a file; put content on following lines
/append <path>     append to a file; put content on following lines
/search <text> [path]  search local files with ripgrep

Shortcut examples:
!uptime
give me the uptime
run hostname"""
