# tools/tool_cmd.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import io
import logging
import platform
import re
import shlex
import subprocess
import tokenize
from pathlib import Path

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "ai-workspace"
try:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logger.warning("Could not create AI workspace: %s", e)

os_name = platform.system()
IS_WIN = os_name == "Windows"

MAX_OUTPUT_CHARS = 4000
OP_CHARS = "();<>|&"
MAX_DEPTH = 3
PLACEHOLDER = "__SUBST__"

# fmt: off
DELETE_PATTERNS = [
    r"\bos\s*\.\s*(remove|unlink|rmdir|removedirs)\s*\(",
    r"\bshutil\s*\.\s*rmtree\s*\(",
    r"\bPath\s*\([^)]*\)\s*\.\s*(unlink|rmdir)\s*\(",
    r"(^|[;&|]\s*)rm\s", r"(^|[;&|]\s*)rmdir\s",
    r"(^|[&|]\s*)del\s", r"(^|[&|]\s*)erase\s",
    r"(^|[&|]\s*)rmdir\s", r"(^|[&|]\s*)rd\s",
    r"\bRemove-Item\b",
]

FORBIDDEN_CMD = {
    "sudo", "su", "rm", "chown", "dd", "kill", "pkill", "passwd", "pkexec",
    "rmdir", "mount", "umount", "shutdown", "reboot", "poweroff", "mkfs",
    "del", "erase", "rd", "format",
    "eval", "exec", "xargs", "source", ".",
    "curl", "wget", "fetch", "nc", "ncat", "netcat", "telnet", "rsh", "ssh",
    "scp", "sftp", "ftp", "tftp", "nmap",
}
PY_SKIP = {"del", "exec", "eval", "format", "source", ".", "erase", "rd",
           "fetch", "su", "dd", "nc"}
PY_FORBIDDEN = FORBIDDEN_CMD - PY_SKIP

KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{"}
WRAPPERS = {"env", "nohup", "time", "command", "builtin", "nice", "timeout",
            "stdbuf", "setsid", "watch", "ionice"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "cmd", "powershell", "pwsh"}
BATCH_SHELLS = {"cmd", "powershell", "pwsh"}
SHELL_C_FLAGS = {"-c", "-lc", "/c", "/k", "-command"}
FIND_BAD = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}

FORBIDDEN_ARG = [
    "/root/", "/proc/", "/sys/", "/boot/",
    "/etc/passwd", "/etc/shadow", "/etc/sudoers", "/etc/sudoers.d/",
    ".env", ".ssh", "id_rsa",
]

_HEREDOC_RE = re.compile(
    r"<<[-~]?\s*(['\"]?)(?P<delim>[A-Za-z_]\w*)\1[ \t]*\r?\n"
    r"(?P<body>.*?)\r?\n(?P=delim)[ \t]*(?=\r?\n|$)",
    re.DOTALL,
)
# fmt: on


def contains_delete_command(source: str) -> str | None:
    for pattern in DELETE_PATTERNS:
        if re.search(pattern, source, flags=re.IGNORECASE | re.MULTILINE):
            return pattern
    return None


def _strip_py_comments(src: str) -> str:
    try:
        lines = src.splitlines(keepends=True)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                r, c = tok.start
                nl = "\n" if lines[r - 1].endswith("\n") else ""
                lines[r - 1] = lines[r - 1][:c] + nl
        return "".join(lines)
    except Exception:
        return src


def _split_lines(text: str, batch: bool = False) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if batch:
        return text.split("\n")

    lines: list[str] = []
    cur: list[str] = []
    in_s = in_d = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_s:
            cur.append(c)
            if c == "'":
                in_s = False
        elif c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                continue
            cur.append(text[i : i + 2])
            i += 2
            continue
        elif c == "#" and not in_d and (not cur or cur[-1] in " \t;&|("):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "'" and not in_d:
            in_s = True
            cur.append(c)
        elif c == '"':
            in_d = not in_d
            cur.append(c)
        elif c == "\n" and not in_d:
            lines.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    lines.append("".join(cur))
    return lines


def _find_close(s: str, start: int) -> int | None:
    depth = 0
    in_s = in_d = False
    i = start
    while i < len(s):
        c = s[i]
        if in_s:
            if c == "'":
                in_s = False
        elif c == "\\":
            i += 1
        elif c == "'" and not in_d:
            in_s = True
        elif c == '"':
            in_d = not in_d
        elif not in_d:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return None


def _scan(line: str, batch: bool = False) -> tuple[str, list[str], str | None]:
    if batch:
        return line, [], None

    out: list[str] = []
    subs: list[str] = []
    in_s = in_d = False
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if in_s:
            out.append(c)
            if c == "'":
                in_s = False
            i += 1
            continue
        if c == "\\":
            out.append(line[i : i + 2])
            i += 2
            continue
        if c == "'" and not in_d:
            in_s = True
        elif c == '"':
            in_d = not in_d
        elif c == "`":
            return line, [], "Backticks are not allowed."
        elif c == "$":
            nxt = line[i + 1 : i + 2]
            if nxt == "(":
                j = _find_close(line, i + 1)
                if j is None:
                    return line, [], "Unbalanced parentheses in $(...)."
                is_arith = (
                    line[i + 2 : i + 3] == "(" and _find_close(line, i + 2) == j - 1
                )
                if is_arith:
                    _, inner_subs, err = _scan(line[i + 3 : j - 1])
                    if err:
                        return line, [], err
                    subs.extend(inner_subs)
                else:
                    subs.append(line[i + 2 : j])
                out.append(PLACEHOLDER)
                i = j + 1
                continue
            if nxt == "{":
                m = re.match(r"\$\{\w+\}", line[i:])
                if not m:
                    return line, [], "Only simple ${NAME} is allowed."
                out.append(m.group(0))
                i += len(m.group(0))
                continue
        out.append(c)
        i += 1
    return "".join(out), subs, None


def _is_op(tok: str) -> bool:
    return bool(tok) and all(c in OP_CHARS for c in tok)


def _tokenize(line: str, batch: bool) -> list[str]:
    lex = shlex.shlex(line, posix=not batch, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    toks = list(lex)
    if batch:
        toks = [t if _is_op(t) else t.strip("\"'") for t in toks]
    return toks


def _base(word: str) -> str:
    if word in (".", ".."):
        return word
    name = Path(word.replace("\\", "/")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _check_arg(tok: str) -> str | None:
    for pattern in FORBIDDEN_ARG:
        if pattern in tok:
            return f"Argument contains forbidden pattern ('{pattern}')."
    return None


def _validate_text(text: str, depth: int, batch: bool) -> str | None:
    for line in _split_lines(text, batch):
        if line.strip():
            err = _validate_line(line, depth, batch)
            if err:
                return err
    return None


def _check_command(word: str, rest: list[str], depth: int) -> str | None:
    base = _base(word)
    if base in FORBIDDEN_CMD:
        return f"Command '{base}' is not allowed."

    args = []
    for t in rest:
        if _is_op(t):
            break
        args.append(t)

    if base in WRAPPERS:
        for t in args:
            b = _base(t)
            if b != "." and b in FORBIDDEN_CMD:
                return f"Command '{b}' is not allowed."

    if base in SHELLS:
        for i, t in enumerate(args[:-1]):
            if t.lower() in SHELL_C_FLAGS:
                return _validate_text(args[i + 1], depth + 1, base in BATCH_SHELLS)

    if base.startswith("python") or base == "py":
        for i, t in enumerate(args[:-1]):
            if t == "-c":
                code = _strip_py_comments(args[i + 1])
                if contains_delete_command(code):
                    return "Delete commands are not allowed."
                for piece in re.split(r"[\s;&|()<>$\"'=,]+", code):
                    if piece and _base(piece) in PY_FORBIDDEN:
                        return f"Code uses forbidden command '{_base(piece)}'."

    if base == "find" and any(a in FIND_BAD for a in args):
        return "find with -delete/-exec is not allowed."
    return None


def _validate_line(line: str, depth: int = 0, batch: bool = False) -> str | None:
    if depth > MAX_DEPTH:
        return "Command nested too deeply."
    if contains_delete_command(line):
        return "Delete commands are not allowed."

    masked, subs, err = _scan(line, batch)
    if err:
        return err
    for inner in subs:
        err = _validate_text(inner, depth + 1, batch)
        if err:
            return err

    try:
        tokens = _tokenize(masked, batch)
    except ValueError as e:
        return f"Invalid syntax: {e}"

    expect_cmd, pending_redirect = True, False
    for i, tok in enumerate(tokens):
        if _is_op(tok):
            if "(" in tok:
                expect_cmd, pending_redirect = True, False
            elif "<" in tok or ">" in tok:
                pending_redirect = True
            else:
                expect_cmd, pending_redirect = True, False
            continue
        if pending_redirect:
            pending_redirect = False
            err = _check_arg(tok)
            if err:
                return err
            continue
        if expect_cmd:
            if re.match(r"^\w+=", tok):
                err = _check_arg(tok)
                if err:
                    return err
                continue
            if tok in KEYWORDS:
                continue
            if tok.startswith("$") or PLACEHOLDER in tok:
                return "Dynamic command names ($VAR, $(...)) are not allowed."
            expect_cmd = False
            err = _check_command(tok, tokens[i + 1 :], depth)
            if err:
                return err
            continue
        err = _check_arg(tok)
        if err:
            return err
    return None


def validate_command(command: str, batch: bool = IS_WIN) -> str | None:
    if not command.strip():
        return "Empty command."

    m = re.match(r'^\s*python[0-9.]*\s+-c\s+(["\'])(.*)\1\s*$', command, re.DOTALL)
    if m:
        if IS_WIN and "\n" in m.group(2):
            return (
                "[ERROR] Multiline `python -c` doesn't work on Windows. "
                "Use `write_file` to create a .py file, then `cmd` to run it."
            )
        return _validate_line(command, 0, batch)

    if "\n" not in command and "\r" not in command:
        if contains_delete_command(command):
            return "Delete commands are not allowed."
        return _validate_line(command, 0, batch)

    heredocs = list(_HEREDOC_RE.finditer(command))
    for m in heredocs:
        body = m.group("body")
        if contains_delete_command(body):
            return "Delete commands are not allowed in heredoc body."

    masked = command
    for i, m in enumerate(heredocs):
        masked = masked.replace(m.group(0), f"__HEREDOC{i}__", 1)

    for line in masked.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"\s*__HEREDOC\d+__\s*", " ", line).strip()
        if not line:
            continue
        if contains_delete_command(line):
            return "Delete commands are not allowed."
        err = _validate_line(line, 0, batch)
        if err:
            return err

    return None


@register_tool
class ExecuteCommandTool(Tool):
    @property
    def name(self):
        return "cmd"

    @property
    def description(self):
        return (
            f"You are in a {os_name} environment!\n"
            "\n"
            "The cmd tool allows you to run shell commands directly. "
            f"It has a 60-second timeout limit and output is truncated to {MAX_OUTPUT_CHARS} characters. "
            f"Use this tool for quick command execution in the terminal.\n"
        )

    @property
    def parameters_schema(self):
        return {
            "command": {
                "type": "string",
                "description": "Shell command to execute, e.g. 'python script.py'.",
            },
        }

    def execute(self, command: str = "", **kwargs) -> str:
        if kwargs:
            logger.debug("Ignoring extra tool params: %s", list(kwargs.keys()))
        if not command.strip():
            return "[ERROR] Provide 'command'."

        err = validate_command(command)
        if err:
            logger.warning("Blocked command (%s): %s", err, command)
            return f"[ERROR] {err}"

        return self._run_command(command)

    def _run_command(self, command: str) -> str:
        logger.info("Executing: %s", command)
        try:
            r = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
                cwd=WORKSPACE,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return "[ERROR] Command exceeded the 60 second timeout."
        except Exception as e:
            logger.exception("Unexpected error while executing command")
            return f"[ERROR] {type(e).__name__}: {e}"

        out = (r.stdout or "").strip()
        errtxt = (r.stderr or "").strip()
        if r.returncode == 0:
            return f"[exit 0] {(out or 'Command executed.')[:MAX_OUTPUT_CHARS]}"
        return (
            f"[ERROR] returncode={r.returncode}\n"
            f"stdout: {out[:MAX_OUTPUT_CHARS] or '(empty)'}\n"
            f"stderr: {errtxt[:MAX_OUTPUT_CHARS] or '(empty)'}\n"
            "-> Fix the script or command and try again."
        )
