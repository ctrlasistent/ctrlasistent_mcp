# tools/tool_edit_file.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import logging
import shutil
from pathlib import Path

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "ai-workspace"


@register_tool
class EditFileTool(Tool):
    """Edit an existing text file in place.

    Two modes:

    String mode (default): replaces the first occurrence of `old_string`
    with `new_string`. Fails if `old_string` is not found, so nothing is
    silently left unmodified. With `replace_all=True` every occurrence
    is replaced.

    Line mode: with `line_start` (and optional `line_end`) the given
    1-based line or inclusive line range is replaced by `new_string`
    (an empty `new_string` deletes those lines). No text matching is
    involved; it always succeeds if the lines exist.

    With `append=True` no replacement is done: `new_string` is appended
    at the end of the file (creates the file if it does not exist).
    """

    @property
    def name(self):
        return "edit_file"

    @property
    def description(self):
        return (
            "Edit an existing text file. Two modes. String mode: replaces "
            "the first occurrence of 'old_string' with 'new_string'; with "
            "replace_all replaces every occurrence; fails if 'old_string' "
            "is not found (nothing is changed). Line mode: give "
            "'line_start' (and optional 'line_end') to replace that line "
            "or inclusive range of lines with 'new_string' directly, by "
            "line number (1-based), without text matching; empty "
            "new_string deletes the lines. With append=True, appends "
            "'new_string' to the end of the file instead (creates the "
            "file if needed)."
        )

    @property
    def parameters_schema(self):
        return {
            "path": {
                "type": "string",
                "description": "File path. Absolute, or relative to the AI workspace.",
            },
            "old_string": {
                "type": "string",
                "description": "String mode: exact text to find. Required unless append=True or line_start is given. Must match exactly, including whitespace and newlines.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text. Use empty string to delete the matched text / lines.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "String mode: replace every occurrence of 'old_string', not just the first. Default: false.",
            },
            "line_start": {
                "type": "integer",
                "description": "Line mode: 1-based line number whose content is replaced by new_string. Overrides string mode when set.",
            },
            "line_end": {
                "type": "integer",
                "description": "Line mode: last line of the range to replace (inclusive). Omit to replace only the line at line_start.",
            },
            "append": {
                "type": "boolean",
                "description": "If true, ignore old_string/line_start and append 'new_string' to the end of the file. Default: false.",
            },
            "backup": {
                "type": "boolean",
                "description": "Save a copy '<path>.bak' before modifying. Default: false.",
            },
        }

    def execute(
        self,
        path: str = "",
        old_string: str = "",
        new_string: str = "",
        replace_all: bool = False,
        line_start: int = 0,
        line_end: int = 0,
        append: bool = False,
        backup: bool = False,
    ) -> str:
        if not path.strip():
            return "[ERROR] Provide 'path'."

        try:
            p = Path(path).expanduser()
            target = p if p.is_absolute() else (WORKSPACE / p)

            # --- Append mode -------------------------------------------
            if append:
                target.parent.mkdir(parents=True, exist_ok=True)
                existed = target.exists()
                if existed and backup:
                    shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
                with target.open("a", encoding="utf-8") as f:
                    f.write(new_string)
                logger.info("Appended to file: %s (%d chars)", target, len(new_string))
                verb = "appended to existing file" if existed else "created and filled"
                return f"OK ({verb}): {target} (+{len(new_string)} chars)."

            # --- Common checks ------------------------------------------
            if not target.is_file():
                return f"[ERROR] {path}: file does not exist. Use 'write_file' to create it."
            if old_string == "" and not line_start:
                return (
                    "[ERROR] Provide 'old_string' (or set append=True or line_start)."
                )

            try:
                text = target.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                return f"[ERROR] {path}: not a UTF-8 text file ({e})."

            # --- Line mode ----------------------------------------------
            if line_start:
                lines = text.splitlines(keepends=True)
                total = len(lines)

                if line_start < 1:
                    return "[ERROR] line_start must be >= 1 (line numbers are 1-based)."
                if line_end and line_end < line_start:
                    return "[ERROR] line_end must be >= line_start."
                if line_start > total:
                    return (
                        f"[ERROR] {path}: file has {total} lines; "
                        f"line_start={line_start} is out of range. Nothing was changed."
                    )

                start_idx = line_start - 1
                end_idx = line_end if line_end else line_start
                if end_idx > total:
                    return (
                        f"[ERROR] {path}: file has {total} lines; "
                        f"line_end={line_end} is out of range. Nothing was changed."
                    )

                if backup:
                    shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))

                replacement = new_string
                if replacement and not replacement.endswith("\n"):
                    # keep the file well-formed: if the replaced block ended
                    # with a newline (and it was not the file's last line),
                    # the single-line replacement gets one too
                    if lines[end_idx - 1].endswith("\n"):
                        replacement += "\n"

                new_lines = lines[:start_idx]
                if replacement:
                    new_lines.append(replacement)
                new_lines.extend(lines[end_idx:])

                with target.open("w", encoding="utf-8", newline="") as f:
                    f.write("".join(new_lines))

                n = end_idx - start_idx  # number of original lines replaced
                if new_string:
                    action = f"replaced {n} line(s) ({line_start}-{end_idx}) with new content"
                else:
                    action = f"deleted {n} line(s) ({line_start}-{end_idx})"
                logger.info("File edited (line mode): %s, %s", target, action)
                if backup:
                    return f"OK: {action} in {target}. Backup: {target}.bak"
                return f"OK: {action} in {target}."

            # --- String mode --------------------------------------------
            count = text.count(old_string)
            if count == 0:
                return (
                    f"[ERROR] {path}: 'old_string' not found. Nothing was "
                    "changed. Check exact whitespace/newlines, or read the "
                    "file first."
                )
            if count > 1 and not replace_all:
                return (
                    f"[ERROR] {path}: 'old_string' occurs {count} times. "
                    "Give a longer, unique 'old_string' or set replace_all=true."
                )

            if backup:
                shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))

            if replace_all:
                new_text = text.replace(old_string, new_string)
            else:
                new_text = text.replace(old_string, new_string, 1)

            with target.open("w", encoding="utf-8", newline="") as f:
                f.write(new_text)

            n = count if replace_all else 1
            logger.info("File edited: %s (%d replacement(s))", target, n)
            if backup:
                return f"OK: {n} replacement(s) in {target}. Backup: {target}.bak"
            return f"OK: {n} replacement(s) in {target}."

        except Exception as e:
            logger.exception("edit_file failed")
            return f"[ERROR] {path}: {type(e).__name__}: {e}"
