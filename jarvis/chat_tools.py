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
import difflib
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# Images Jarvis posts into chat live here and are served by the dashboard at /api/upload?f=NAME
# (same sandboxed dir the owner's uploads use). Keep this in sync with dashboard.server.UPLOADS.
UPLOADS = ROOT / "state" / "uploads"
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024


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
    os.path.expanduser("~/.ssh/"),     # protect the running user's secrets (any user, no hardcoded home)
    os.path.expanduser("~/.codex/"),
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


# Native OpenAI function specs for the chat answer loop. Sent as the `tools` param so capable models
# (DeepSeek/OpenAI-style) return DETERMINISTIC structured tool_calls instead of free-text we have to
# scrape. shell is read-only/safety-gated; read/search are read-only. Mutations (write/edit) stay on
# the explicit owner-gated text path, not auto-callable here.
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Run a READ-ONLY bash command on the owner's machine and return its output. "
                       "Destructive/mutating commands are blocked. Use for service status, host/repo "
                       "facts, logs, pipeline state, etc.",
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "the bash command to run"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {
        "name": "read",
        "description": "Read a text file on the owner's machine and return its contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "absolute or repo-relative file path"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search",
        "description": "Search files for a regex pattern under a directory and return matches.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "show_image",
        "description": "Display an image in the chat. Give a local image path or a direct http(s) image "
                       "URL (png/jpg/gif/webp); it is copied into the dashboard's served folder and "
                       "rendered inline in your reply. Use when the owner asks to see an image.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "absolute or repo-relative path to the image file"},
            "caption": {"type": "string", "description": "optional caption shown as alt text"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "add_skill",
        "description": "Create a new skill so you (and future turns) gain a reusable capability. A skill "
                       "is instructions in a SKILL.md — include a 'when_to_use' line and, if it drives a "
                       "utility, the exact command/script path and steps. Saved to the persistent skills "
                       "folder and auto-detected afterward. Use when the owner says to add/teach a skill.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "short skill name, e.g. 'create-ad'"},
            "content": {"type": "string", "description": "the SKILL.md text (instructions; may include YAML "
                        "frontmatter with description/when_to_use and any script path)"}},
            "required": ["name", "content"]}}},
]

TOOL_PROTOCOL = """Local tools are available through Jarvis, independent of the selected model provider.
If you need local evidence or need to perform an explicitly requested file change, reply with exactly one JSON object and no prose:
{"tool":"shell","args":{"cmd":"uptime"}}
{"tool":"read","args":{"path":"docs/INDEX.md"}}
{"tool":"search","args":{"pattern":"pipeline","path":"."}}
{"tool":"write","args":{"path":"example.md","content":"text"}}
{"tool":"append","args":{"path":"example.md","content":"text"}}
{"tool":"edit","args":{"path":"example.md","old":"exact text to replace","new":"replacement text"}}
{"tool":"show_image","args":{"path":"/path/to/ad.png","caption":"the generated ad"}}
{"tool":"add_skill","args":{"name":"create-ad","content":"---\\nname: create-ad\\ndescription: generate an ad image\\nwhen_to_use: owner asks for an ad\\n---\\nRun: python3 /path/generate_full_ai_ad.py --prompt ... --output ...\\nThen show_image the output."}}

Rules:
- Use tools for local files, docs, service status, tickets, pipeline state, host facts, repo facts, and other machine-local evidence.
- shell is read-only and blocks destructive or mutating commands.
- Use write for new files or full-file generation. Use edit for existing-file changes when possible.
- write/append/edit obey the configured mode: shadow blocks writes, assist requires explicit owner edit intent, autonomous allows local-safe writes.
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
        existed = p.exists() and p.is_file()
        before = p.read_text(errors="replace") if existed else ""
        if append:
            with p.open("a") as f:
                f.write(content)
        else:
            p.write_text(content)
        after = p.read_text(errors="replace") if p.exists() and p.is_file() else content
        if append:
            return {"ok": True, "tool": "write", "path": str(p), "output": "appended to file"}
        if existed and before != content:
            before_context, after_context = _rewrite_context(before, after)
            return {
                "ok": True,
                "tool": "write",
                "path": str(p),
                "output": "updated file",
                "changed_existing": True,
                "old": before_context,
                "new": after_context,
                "before_context": before_context,
                "after_context": after_context,
            }
        return {"ok": True, "tool": "write", "path": str(p), "output": "wrote file"}
    except Exception as e:
        return {"ok": False, "tool": "write", "path": raw, "error": str(e)[:300]}


def edit_file(path: str, old: str, new: str, owner_text: str = "", explicit: bool = False) -> dict:
    raw = _clean_cmd(path)
    allowed, reason = write_allowed(owner_text, explicit=explicit)
    if not allowed:
        return {"ok": False, "tool": "edit", "path": raw, "error": reason}
    if not old:
        return {"ok": False, "tool": "edit", "path": raw, "error": "old text is required"}
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = default_cwd() / p
        resolved = str(p.resolve())
        if any(resolved == x.rstrip("/") or resolved.startswith(x) for x in _PROTECTED_WRITE_PREFIXES):
            return {"ok": False, "tool": "edit", "path": str(p), "error": "protected path; use the repo/deploy path instead"}
        if not p.exists() or not p.is_file():
            return {"ok": False, "tool": "edit", "path": str(p), "error": "file not found"}
        data = p.read_text(errors="replace")
        count = data.count(old)
        if count == 0:
            return {"ok": False, "tool": "edit", "path": str(p), "error": "old text not found"}
        if count > 1:
            return {"ok": False, "tool": "edit", "path": str(p), "error": f"old text appears {count} times; provide a more specific edit"}
        before_context = _edit_context(data, old)
        updated = data.replace(old, new, 1)
        after_context = _edit_context(updated, new)
        p.write_text(updated)
        return {
            "ok": True,
            "tool": "edit",
            "path": str(p),
            "output": "edited file",
            "old": old,
            "new": new,
            "before_context": before_context,
            "after_context": after_context,
        }
    except Exception as e:
        return {"ok": False, "tool": "edit", "path": raw, "error": str(e)[:300]}


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


def _edit_context(text: str, needle: str, radius: int = 260) -> str:
    if not needle:
        return text[: radius * 2]
    idx = text.find(needle)
    if idx < 0:
        return text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    prefix = "...\\n" if start else ""
    suffix = "\\n..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _rewrite_context(before: str, after: str, max_lines: int = 18) -> tuple[str, str]:
    """Return compact before/after snippets for a full-file rewrite.

    Some models update an existing file through the write tool instead of the edit tool.
    The UI still needs an edit card, so show the changed hunk rather than the whole file.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    groups = matcher.get_grouped_opcodes(n=3)
    try:
        group = next(groups)
    except StopIteration:
        return before[:2000], after[:2000]
    a_start = max(0, min(op[1] for op in group))
    a_end = min(len(before_lines), max(op[2] for op in group))
    b_start = max(0, min(op[3] for op in group))
    b_end = min(len(after_lines), max(op[4] for op in group))
    old_chunk = before_lines[a_start:a_end][:max_lines]
    new_chunk = after_lines[b_start:b_end][:max_lines]
    old_prefix = ["..."] if a_start else []
    old_suffix = ["..."] if a_end < len(before_lines) else []
    new_prefix = ["..."] if b_start else []
    new_suffix = ["..."] if b_end < len(after_lines) else []
    return (
        "\n".join(old_prefix + old_chunk + old_suffix)[:2000],
        "\n".join(new_prefix + new_chunk + new_suffix)[:2000],
    )


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
    tool_raw = (data.get("tool") or "").strip()
    args = data.get("args") or {}
    if tool_raw.startswith("mcp__") and isinstance(args, dict):    # MCP tool — preserve case, route later
        return {"tool": tool_raw, "args": args}
    tool = tool_raw.lower()
    if tool not in {"shell", "read", "search", "write", "append", "edit", "show_image", "add_skill"} or not isinstance(args, dict):
        cmds = extract_shell_commands(text, limit=1)
        if cmds:
            return {"tool": "shell", "args": {"cmd": cmds[0]}}
        return None
    return {"tool": tool, "args": args}


def extract_json_tool_calls(text: str, limit: int = 8) -> list[dict]:
    """Recover JSON tool calls from model output, e.g. {"tool":"shell","args":{"cmd":"uptime"}}.

    Models that follow the JSON tool protocol (DeepSeek and most OpenAI-style models) emit the
    call in their answer/content rather than as XML or a fenced shell block — so the XML and
    shell-fence recoverers miss it. Scan for balanced-brace JSON objects and keep valid tool
    requests. Without this, a perfectly-formed JSON tool call is dropped and the turn dies with
    no answer (the recurring "Jarvis says it'll do something then does nothing" bug).
    """
    out: list[dict] = []
    raw = text or ""
    n = len(raw)
    i = 0
    while i < n and len(out) < limit:
        if raw[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        end = -1
        while j < n:
            ch = raw[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end < 0:
            break
        try:
            data = json.loads(raw[i:end + 1])
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("tool"):
            args = data.get("args") if isinstance(data.get("args"), dict) else {}
            # route through the alias mapper so run_command/read_file/etc. JSON also resolve
            call = _tool_call_from_parts(str(data.get("tool")), {}, {str(k).lower(): v for k, v in args.items()})
            if call:
                out.append(call)
        i = end + 1
    return out


def _xml_attrs(raw: str) -> dict:
    attrs = {}
    for key, quote, value in re.findall(r"([A-Za-z_][\w:-]*)\s*=\s*(['\"])(.*?)\2", raw or "", flags=re.S):
        attrs[key.lower()] = html.unescape(value)
    return attrs


def _xml_text(raw: str) -> str:
    raw = re.sub(r"^\s*<!\[CDATA\[|\]\]>\s*$", "", raw or "", flags=re.S)
    return html.unescape(raw).strip()


def _xml_child_args(body: str) -> dict:
    args: dict[str, str] = {}
    for attrs_raw, value in re.findall(r"<parameter\b([^>]*)>([\s\S]*?)</parameter>", body or "", flags=re.I):
        attrs = _xml_attrs(attrs_raw)
        name = (attrs.get("name") or attrs.get("key") or "").strip().lower()
        if name:
            args[name] = _xml_text(value)
    for name, value in re.findall(
        r"<(path|file|filename|cmd|command|script|code|pattern|query|text|root|dir|old|new|content)\b[^>]*>([\s\S]*?)</\1>",
        body or "",
        flags=re.I,
    ):
        args[name.lower()] = _xml_text(value)
    return args


def normalize_tool_call(name: str, args: dict | None = None) -> dict | None:
    """Normalize a (possibly aliased) tool name + args dict into a canonical {tool,args} call.
    Used for NATIVE structured tool_calls from the model. Returns None if unsupported/unsafe."""
    a = {str(k).lower(): v for k, v in (args or {}).items()}
    return _tool_call_from_parts(name or "", {}, a)


def _tool_call_from_parts(tool_name: str, attrs: dict | None = None, child_args: dict | None = None) -> dict | None:
    attrs = attrs or {}
    child_args = child_args or {}
    args = {**attrs, **child_args}
    if str(tool_name or "").startswith("mcp__"):       # MCP tool — pass through verbatim (case matters)
        return {"tool": str(tool_name), "args": args}
    tag_l = (tool_name or "").strip().lower()
    # Liberal aliasing — models invent tool names (read_file, run_command, execute, terminal, ...).
    # Map them to our canonical tools instead of dropping the call (text-recovery fallback path).
    if tag_l in {"read_file", "read", "cat", "open", "view_file", "view", "get_file"}:
        path = args.get("path") or args.get("file") or args.get("filename")
        if path:
            return {"tool": "read", "args": {"path": path}}
    elif tag_l in {"search", "grep", "find", "rg", "ripgrep", "search_files"}:
        pattern = args.get("pattern") or args.get("query") or args.get("text") or args.get("q")
        path = args.get("path") or args.get("root") or args.get("dir")
        if pattern:
            return {"tool": "search", "args": {"pattern": pattern, "path": path or str(default_cwd())}}
    elif tag_l in {"shell", "bash", "sh", "run_command", "run_shell", "shell_command", "bash_command",
                   "command", "execute", "exec", "run", "terminal", "console"}:
        cmd = args.get("cmd") or args.get("command") or args.get("script") or args.get("code") or args.get("input")
        if cmd and not shell_safety_error(cmd):
            return {"tool": "shell", "args": {"cmd": cmd}}
    elif tag_l in {"write", "append"}:
        path = args.get("path") or args.get("file")
        content = args.get("content") or args.get("text")
        if path and content is not None:
            return {"tool": tag_l, "args": {"path": path, "content": content}}
    elif tag_l in {"edit_file", "edit"}:
        path = args.get("path") or args.get("file")
        old = args.get("old")
        new = args.get("new")
        if path and old is not None and new is not None:
            return {"tool": "edit", "args": {"path": path, "old": old, "new": new}}
    elif tag_l in {"show_image", "post_image", "attach_image", "display_image", "send_image", "image"}:
        path = args.get("path") or args.get("file") or args.get("filename") or args.get("src")
        if path:
            return {"tool": "show_image", "args": {"path": path, "caption": args.get("caption") or args.get("alt") or ""}}
    elif tag_l in {"add_skill", "create_skill", "new_skill", "save_skill", "teach_skill"}:
        nm = args.get("name") or args.get("skill") or args.get("title")
        content = args.get("content") or args.get("text") or args.get("body") or args.get("skill_md")
        if nm and content is not None:
            return {"tool": "add_skill", "args": {"name": nm, "content": content}}
    return None


def extract_xml_tool_calls(text: str, limit: int = 8) -> list[dict]:
    """Recover XML-ish tool calls from models that use Anthropic/Codex-like pseudo tags.

    Examples:
      <read_file path="/tmp/a.txt" />
      <search pattern="foo" path="/repo" />
      <shell cmd="uptime" />
      <tool_call name="read_file"><path>/tmp/a.txt</path></tool_call>
      <function_calls><invoke name="read_file"><parameter name="path">/tmp/a.txt</parameter></invoke></function_calls>
    """
    out: list[dict] = []
    raw_text = text or ""
    for attrs_raw, body in re.findall(r"<tool_call\b([^>]*)>([\s\S]*?)</tool_call>", raw_text, flags=re.I):
        attrs = _xml_attrs(attrs_raw)
        call = _tool_call_from_parts(attrs.get("name") or attrs.get("tool") or "", {}, _xml_child_args(body))
        if call:
            out.append(call)
        if len(out) >= limit:
            return out
    for attrs_raw, body in re.findall(r"<invoke\b([^>]*)>([\s\S]*?)</invoke>", raw_text, flags=re.I):
        attrs = _xml_attrs(attrs_raw)
        call = _tool_call_from_parts(attrs.get("name") or attrs.get("tool") or "", {}, _xml_child_args(body))
        if call:
            out.append(call)
        if len(out) >= limit:
            return out
    tag_names = "read_file|read|search|grep|shell|bash|write|append|edit_file|edit"
    for tag, attrs_raw, body in re.findall(rf"<({tag_names})\b([^>]*)>([\s\S]*?)</\1>", raw_text, flags=re.I):
        attrs = _xml_attrs(attrs_raw)
        call = _tool_call_from_parts(tag, attrs, _xml_child_args(body))
        if call:
            out.append(call)
        if len(out) >= limit:
            break
    for tag, attrs_raw in re.findall(rf"<({tag_names})\b([^>]*)/>", raw_text, flags=re.I):
        attrs = _xml_attrs(attrs_raw)
        call = _tool_call_from_parts(tag, attrs, {})
        if call:
            out.append(call)
        if len(out) >= limit:
            break
    return out


def extract_shell_commands(text: str, limit: int = 8) -> list[str]:
    """Recover safe shell commands from models that ignore the JSON tool protocol.

    Some providers emit fenced bash blocks as a "plan". Jarvis should execute safe commands
    through its tool layer instead of showing the plan to the owner as if it were an answer.
    """
    out: list[str] = []
    blocks = [body for _lang, body in re.findall(r"```(bash|sh|shell)\s*([\s\S]*?)```", text or "", flags=re.IGNORECASE)]
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


def all_tool_specs(cfg: dict | None = None) -> list:
    """Static Jarvis tools + any MCP server tools (provider-agnostic). cfg is loaded lazily if
    omitted. Used at the chat/heavy call-sites so every backend sees MCP tools the same way."""
    specs = list(TOOL_SPECS)
    try:
        from jarvis import mcp
        if cfg is None:
            from jarvis.config import load
            cfg = load()
        specs += mcp.tool_specs(cfg)
    except Exception:
        pass
    return specs


def run_model_tool(call: dict, owner_text: str) -> dict:
    tool = (call or {}).get("tool")
    args = (call or {}).get("args") or {}
    if isinstance(tool, str) and tool.startswith("mcp__"):
        try:
            from jarvis import mcp
            res = mcp.call(tool, args)
        except Exception as e:
            res = {"ok": False, "error": str(e)[:200]}
        res["tool"] = "mcp"; res["name"] = tool
        return res
    if tool == "shell":
        return run_shell(str(args.get("cmd") or ""))
    if tool == "read":
        return read_file(str(args.get("path") or ""))
    if tool == "search":
        return search(str(args.get("pattern") or ""), str(args.get("path") or default_cwd()))
    if tool in ("write", "append"):
        return write_file(str(args.get("path") or ""), str(args.get("content") or ""),
                          append=(tool == "append"), owner_text=owner_text)
    if tool == "edit":
        return edit_file(str(args.get("path") or ""), str(args.get("old") or ""), str(args.get("new") or ""),
                         owner_text=owner_text)
    if tool == "show_image":
        return show_image(str(args.get("path") or ""), str(args.get("caption") or ""))
    if tool == "add_skill":
        return add_skill(str(args.get("name") or ""), str(args.get("content") or ""))
    return {"ok": False, "tool": tool or "unknown", "error": "unknown tool"}


def add_skill(name: str, content: str) -> dict:
    """Create a new skill (a SKILL.md) in the persistent skills folder; auto-detected afterward."""
    try:
        from jarvis import skills as _skills_mod
        res = _skills_mod.save_skill(name, content)
        res.setdefault("tool", "add_skill")
        return res
    except Exception as e:
        return {"ok": False, "tool": "add_skill", "error": str(e)[:300]}


def show_image(path: str, caption: str = "") -> dict:
    """Copy an existing local image into the dashboard's served uploads folder so it renders inline
    in chat. Read-only w.r.t. the source; never executes anything. Returns markdown the reply embeds."""
    import shutil
    import uuid as _uuid

    raw = (path or "").strip()
    if not raw:
        return {"ok": False, "tool": "show_image", "error": "missing image path"}
    if re.match(r"^https?://", raw, re.I):
        return _show_remote_image(raw, caption)
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = default_cwd() / p
    try:
        p = p.resolve()
        if not p.exists() or not p.is_file():
            return {"ok": False, "tool": "show_image", "path": raw, "error": "image file not found"}
        ext = p.suffix.lstrip(".").lower()
        if ext not in IMAGE_EXTS:
            return {"ok": False, "tool": "show_image", "path": raw,
                    "error": f"not an image (.{ext}); supported: {', '.join(sorted(IMAGE_EXTS))}"}
        if p.stat().st_size > MAX_IMAGE_BYTES:
            return {"ok": False, "tool": "show_image", "path": raw,
                    "error": f"image too large (> {MAX_IMAGE_BYTES // (1024 * 1024)}MB)"}
        UPLOADS.mkdir(parents=True, exist_ok=True)
        name = f"{_uuid.uuid4().hex[:12]}.{ext}"
        shutil.copyfile(p, UPLOADS / name)
        cap = (caption or "").strip() or "image"
        url = f"/api/upload?f={name}"
        return {"ok": True, "tool": "show_image", "path": raw, "file": name,
                "url": url, "output": f"![{cap}]({url})"}
    except Exception as e:
        return {"ok": False, "tool": "show_image", "path": raw, "error": str(e)[:300]}


def _show_remote_image(url: str, caption: str = "") -> dict:
    import uuid as _uuid

    parsed = urllib.parse.urlparse(url)
    ext = Path(parsed.path).suffix.lstrip(".").lower()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ext:
                ext = {
                    "image/jpeg": "jpg",
                    "image/png": "png",
                    "image/gif": "gif",
                    "image/webp": "webp",
                }.get(content_type, "")
            if ext not in IMAGE_EXTS:
                return {"ok": False, "tool": "show_image", "path": url,
                        "error": f"not an image URL; supported: {', '.join(sorted(IMAGE_EXTS))}"}
            data = resp.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            return {"ok": False, "tool": "show_image", "path": url,
                    "error": f"image too large (> {MAX_IMAGE_BYTES // (1024 * 1024)}MB)"}
        UPLOADS.mkdir(parents=True, exist_ok=True)
        name = f"{_uuid.uuid4().hex[:12]}.{ext}"
        (UPLOADS / name).write_bytes(data)
        cap = (caption or "").strip() or "image"
        out_url = f"/api/upload?f={name}"
        return {"ok": True, "tool": "show_image", "path": url, "file": name,
                "url": out_url, "output": f"![{cap}]({out_url})"}
    except Exception as e:
        return {"ok": False, "tool": "show_image", "path": url, "error": str(e)[:300]}


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
    if tool == "answer":
        return str(result.get("output") or "")
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
            if result.get("changed_existing"):
                marker = {
                    "path": result.get("path"),
                    "old": str(result.get("old") or "")[:1200],
                    "new": str(result.get("new") or "")[:1200],
                    "before": str(result.get("before_context") or result.get("old") or "")[:2000],
                    "after": str(result.get("after_context") or result.get("new") or "")[:2000],
                }
                return f"{result.get('output')}: {result.get('path')}\nJARVIS_EDIT {json.dumps(marker, sort_keys=True)}"
            return f"{result.get('output')}: {result.get('path')}"
        return f"{result.get('path')}\n(error: {result.get('error')})"
    if tool == "edit":
        if result.get("ok"):
            marker = {
                "path": result.get("path"),
                "old": str(result.get("old") or "")[:1200],
                "new": str(result.get("new") or "")[:1200],
                "before": str(result.get("before_context") or result.get("old") or "")[:2000],
                "after": str(result.get("after_context") or result.get("new") or "")[:2000],
            }
            return f"{result.get('output')}: {result.get('path')}\nJARVIS_EDIT {json.dumps(marker, sort_keys=True)}"
        return f"{result.get('path')}\n(error: {result.get('error')})"
    if tool == "search":
        if result.get("ok"):
            return result.get("output") or "(no matches)"
        return f"(search failed: {result.get('error')})"
    if tool == "show_image":
        if result.get("ok"):
            return result.get("output") or ""
        return f"(could not show image: {result.get('error')})"
    if tool == "add_skill":
        if result.get("ok"):
            return f"Saved skill '{result.get('name')}' ({result.get('path')}). It will be auto-detected from now on."
        return f"(could not add skill: {result.get('error')})"
    if tool == "mcp":
        if result.get("ok"):
            return f"{result.get('name')}:\n{result.get('text') or '(no output)'}"
        return f"({result.get('name')} failed: {result.get('error') or result.get('text')})"
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
        result = run_shell("uptime")
        if result.get("ok"):
            return {"ok": True, "tool": "answer", "output": _human_uptime(str(result.get("output") or ""))}
        return result
    m = re.match(r"^(?:please\s+)?(?:run|execute)\s+(?:the\s+)?(?:shell\s+)?(?:command\s+)?(.+)$", raw, re.I)
    if m:
        return run_shell(m.group(1))
    m = re.match(r"^(?:what(?:'s| is)?|show|give me|get me)\s+(?:the\s+)?(?:output of\s+)?`([^`]+)`\??$", raw, re.I)
    if m:
        return run_shell(m.group(1))
    return None


def _human_uptime(output: str) -> str:
    out = (output or "").strip()
    m = re.search(r"up\s+(.+?),\s+\d+\s+users?,\s+load average:\s*(.+)$", out)
    if not m:
        m = re.search(r"up\s+(.+?),\s+load average:\s*(.+)$", out)
    if m:
        return f"Uptime is {m.group(1).strip()}.\n\nLoad average: {m.group(2).strip()}."
    return out


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
