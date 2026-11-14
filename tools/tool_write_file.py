# tools/tool_write_file.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import logging
from pathlib import Path

from core.CtrlAsistent_tools import Tool, register_tool

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = PROJECT_ROOT / "ai-workspace"
try:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
except OSError as e:
    logger.warning("Could not create AI workspace: %s", e)


@register_tool
class WriteFileTool(Tool):
    @property
    def name(self):
        return "write_file"

    @property
    def description(self):
        return "Write a text file. Content is written as-is. "

    @property
    def parameters_schema(self):
        return {
            "path": {
                "type": "string",
                "description": "File path. Absolute, or relative to the current working directory.",
            },
            "content": {
                "type": "string",
                "description": "Full file contents.",
            },
        }

    def execute(self, path: str = "", content: str = "") -> str:
        if not path.strip():
            return "[ERROR] Provide 'path'."

        try:
            p = Path(path).expanduser()
            target = p if p.is_absolute() else (WORKSPACE / p)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="") as f:
                f.write(content)
            logger.info("File written: %s (%d chars)", target, len(content))
            return f"File saved: {target} ({len(content)} chars)."
        except Exception as e:
            logger.exception("write_workspace_file failed")
            return f"[ERROR] {path}: {type(e).__name__}: {e}"
