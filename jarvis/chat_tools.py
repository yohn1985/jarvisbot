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
import json
import html
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def default_cwd() -> Path:
    candidates: list[Path] = []
    for env_name in ("JARVIS_WORKSPACE", "JARVIS_CWD"):
        raw = os.environ.get(env_name)
        if raw:
            candidates.append(Path(raw).expanduser())
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text()) if (ROOT / "config.yaml").exists() else {}
        workspace = ((cfg or {}).get("workspace") or {}).get("root")
        if workspace:
            candidates.append(Path(str(workspace)).expanduser())
    except Exception:
        pass
    try:
        from jarvis import local_knowledge
        for root in local_knowledge.remembered_roots()[:6]:
            candidates.append(root)
            if root.name.lower() in {"docs", "documentation"}:
                candidates.append(root.parent)
    except Exception:
        pass
    candidates.append(Path.cwd())
    for p in candidates:
        try:
            if p.exists() and p.is_dir():
                return p.resolve()
        except Exception:
            continue
    return Path.cwd()

MAX_OUTPUT = 12000
READ_LIMIT = 20000
MAX_TOOL_STEPS = 5
MODE_SHADOW = "shadow"
MODE_ASSIST = "assist"
MODE_AUTONOMOUS = "autonomous"

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
_REDIRECT = re.compile(r"(^|[^<])(\d?>|&>|>>)")
_SAFE_DEVNULL_REDIRECT = re.compile(r"^\s*(2>|2>>|&>)\s*/dev/null\b")
_PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|python)", re.IGNORECASE)
_EDIT_INTENT = re.compile(
    r"\b(fix|change|edit|update|write|append|create|add|implement|patch|save|document|remember)\b",
    re.IGNORECASE,
)
_PROTECTED_WRITE_PREFIXES = (
    "/etc/",
    "/var/",
    "/opt/",
    "/root/",
    "/boot/",
    "/dev/",
    "/proc/",
    "/sys/",
    "/home/yohn/.ssh/",
    "/home/yohn/.codex/",
)


def current_mode() -> str:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text()) if (ROOT / "config.yaml").exists() else {}
        mode = str((((cfg or {}).get("identity") or {}).get("mode") or MODE_SHADOW)).strip().lower()
    except Exception:
        mode = MODE_SHADOW
    if mode == "assisted":
        mode = MODE_ASSIST
    if mode not in {MODE_SHADOW, MODE_ASSIST, MODE_AUTONOMOUS}:
        mode = MODE_SHADOW
    return mode


def mode_description() -> str:
    mode = current_mode()
    if mode == MODE_AUTONOMOUS:
        return "autonomous: local-safe actions allowed without owner wording; destructive/system/provider actions still blocked"
    if mode == MODE_ASSIST:
        return "assist: local writes require explicit owner edit intent or slash command"
    return "shadow: observe and report only; writes/actions are blocked"


TOOL_PROTOCOL = """Local tools are available through Jarvis, independent of the selected model provider.
If you need local evidence or need to perform an explicitly requested file change, reply with exactly one JSON object and no prose:
{"tool":"shell","args":{"cmd":"uptime"}}
{"tool":"read","args":{"path":"docs/INDEX.md"}}
{"tool":"search","args":{"pattern":"pipeline","path":"."}}
{"tool":"write","args":{"path":"example.md","content":"text"}}
{"tool":"append","args":{"path":"example.md","content":"text"}}

Rules:
- Use tools for local files, docs, service status, tickets, pipeline state, host facts, repo facts, and other machine-local evidence.
- shell is read-only and blocks destructive or mutating commands.
- write/append obey the configured mode: shadow blocks writes, assist requires explicit owner edit intent, autonomous allows local-safe writes.
- If no tool is needed, reply exactly: NO_TOOL"""


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
    if _DANGEROUS.search(cmd) or _PIPE_TO_SHELL.search(cmd):
        return "blocked because this chat shell tool is read-only; use a deliberate deploy/edit path for changes"
    for m in _REDIRECT.finditer(cmd):
        frag = cmd[m.start(2):]
        if not _SAFE_DEVNULL_REDIRECT.match(frag):
            return "blocked because shell redirection can write files; use /write or a deliberate edit path"
    return None


def owner_allows_write(owner_text: str) -> bool:
    return bool(_EDIT_INTENT.search(owner_text or ""))


def write_allowed(owner_text: str = "", explicit: bool = False) -> tuple[bool, str]:
    mode = current_mode()
    if mode == MODE_AUTONOMOUS:
        return True, ""
    if mode == MODE_ASSIST and (explicit or owner_allows_write(owner_text)):
        return True, ""
    if mode == MODE_ASSIST:
        return False, "assist mode requires explicit owner edit intent for writes"
    return False, "shadow mode is observe-only; writes are blocked"


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
            cwd=str(default_cwd()),
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
    if current_mode() == MODE_SHADOW:
        return {"ok": False, "tool": "codex", "error": "blocked in shadow mode; switch to assist/autonomous for full agent execution"}
    fd, final_path = tempfile.mkstemp(prefix="jarvis-codex-final-", suffix=".txt")
    os.close(fd)
    try:
        p = subprocess.run(
            [
                "codex", "exec",
                "--cd", str(default_cwd()),
                "--sandbox", "danger-full-access",
                "-c", 'approval_policy="never"',
                "--skip-git-repo-check",
                "--output-last-message", final_path,
                "-",
            ],
            input=task,
            cwd=str(default_cwd()),
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
            p = default_cwd() / p
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


def write_file(path: str, content: str, append: bool = False, owner_text: str = "", explicit: bool = False) -> dict:
    raw = _clean_cmd(path)
    allowed, reason = write_allowed(owner_text, explicit=explicit)
    if not allowed:
        return {"ok": False, "tool": "write", "path": raw, "error": reason}
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = default_cwd() / p
        resolved = str(p.resolve())
        if any(resolved == x.rstrip("/") or resolved.startswith(x) for x in _PROTECTED_WRITE_PREFIXES):
            return {"ok": False, "tool": "write", "path": str(p), "error": "protected path; use the repo/deploy path instead"}
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


def _json_candidate(text: str) -> str | None:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return m.group(1)
    if text.startswith("{") and text.endswith("}"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return None


def parse_model_tool_call(text: str) -> dict | None:
    candidate = _json_candidate(text)
    if not candidate:
        xml_calls = extract_xml_tool_calls(text, limit=1)
        if xml_calls:
            return xml_calls[0]
        cmds = extract_shell_commands(text, limit=1)
        if cmds:
            return {"tool": "shell", "args": {"cmd": cmds[0]}}
        return None
    try:
        data = json.loads(candidate)
    except Exception:
        cmds = extract_shell_commands(text, limit=1)
        if cmds:
            return {"tool": "shell", "args": {"cmd": cmds[0]}}
        return None
    tool = (data.get("tool") or "").strip().lower()
    args = data.get("args") or {}
    if tool not in {"shell", "read", "search", "write", "append"} or not isinstance(args, dict):
        cmds = extract_shell_commands(text, limit=1)
        if cmds:
            return {"tool": "shell", "args": {"cmd": cmds[0]}}
        return None
    return {"tool": tool, "args": args}


def _xml_attrs(raw: str) -> dict:
    attrs = {}
    for key, quote, value in re.findall(r"([A-Za-z_][\w:-]*)\s*=\s*(['\"])(.*?)\2", raw or "", flags=re.S):
        attrs[key.lower()] = html.unescape(value)
    return attrs


def extract_xml_tool_calls(text: str, limit: int = 8) -> list[dict]:
    """Recover XML-ish tool calls from models that use Anthropic/Codex-like pseudo tags.

    Examples:
      <read_file path="/tmp/a.txt" />
      <search pattern="foo" path="/repo" />
      <shell cmd="uptime" />
    """
    out: list[dict] = []
    raw_text = text or ""
    for tag, attrs_raw in re.findall(r"<(read_file|read|search|grep|shell|bash)\b([^>]*)/?>", raw_text, flags=re.I):
        tag_l = tag.lower()
        attrs = _xml_attrs(attrs_raw)
        if tag_l in {"read_file", "read"}:
            path = attrs.get("path") or attrs.get("file")
            if path:
                out.append({"tool": "read", "args": {"path": path}})
        elif tag_l in {"search", "grep"}:
            pattern = attrs.get("pattern") or attrs.get("query") or attrs.get("text")
            path = attrs.get("path") or attrs.get("root")
            if pattern:
                out.append({"tool": "search", "args": {"pattern": pattern, "path": path or str(default_cwd())}})
        elif tag_l in {"shell", "bash"}:
            cmd = attrs.get("cmd") or attrs.get("command")
            if cmd and not shell_safety_error(cmd):
                out.append({"tool": "shell", "args": {"cmd": cmd}})
        if len(out) >= limit:
            break
    return out


def extract_shell_commands(text: str, limit: int = 8) -> list[str]:
    """Recover safe shell commands from models that ignore the JSON tool protocol.

    Some providers emit fenced bash blocks as a "plan". Jarvis should execute safe commands
    through its tool layer instead of showing the plan to the owner as if it were an answer.
    """
    out: list[str] = []
    blocks = [body for _lang, body in re.findall(r"```(bash|sh|shell)?\s*([\s\S]*?)```", text or "", flags=re.IGNORECASE)]
    blocks += re.findall(r"<(?:bash|sh|shell)>\s*([\s\S]*?)\s*</(?:bash|sh|shell)>", text or "", flags=re.IGNORECASE)
    for body in blocks:
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("$"):
                line = line[1:].strip()
            if shell_safety_error(line):
                continue
            if line not in out:
                out.append(line)
            if len(out) >= limit:
                return out
    return out


def run_model_tool(call: dict, owner_text: str) -> dict:
    tool = (call or {}).get("tool")
    args = (call or {}).get("args") or {}
    if tool == "shell":
        return run_shell(str(args.get("cmd") or ""))
    if tool == "read":
        return read_file(str(args.get("path") or ""))
    if tool == "search":
        return search(str(args.get("pattern") or ""), str(args.get("path") or default_cwd()))
    if tool in ("write", "append"):
        return write_file(str(args.get("path") or ""), str(args.get("content") or ""),
                          append=(tool == "append"), owner_text=owner_text)
    return {"ok": False, "tool": tool or "unknown", "error": "unknown tool"}


def search(pattern: str, root: str | None = None) -> dict:
    pattern = (pattern or "").strip()
    if not pattern:
        return {"ok": False, "tool": "search", "error": "missing search pattern"}
    base = Path(root or default_cwd()).expanduser()
    if not base.is_absolute():
        base = default_cwd() / base
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
        return write_file(path.strip(), content, append=(name == "append"), explicit=True)
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
run hostname

Mode guardrails:
shadow = observe only
assist = edits only on explicit request
autonomous = local-safe actions allowed; destructive/system/provider actions still blocked"""
