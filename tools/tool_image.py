# tool_image.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

# tools/tool_image.py
# MCP version — adapted from GUI original (tool_image.py in CtrlAsistent)
# Uses FastMCP Image for vision-capable clients, falls back to text.

import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastmcp.utilities.types import Image

from core.CtrlAsistent_tools import Tool

logger = logging.getLogger(__name__)

SUPPORTED_EXT = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_SIZE_MB = 20


class LoadImageTool(Tool):
    @property
    def name(self) -> str:
        return "image"

    @property
    def description(self) -> str:
        return (
            "Loads an image from the local disk and attaches it to the model "
            "for visual analysis. Supported formats: svg, png, jpg, jpeg, "
            "webp, bmp, gif. Use THIS tool when the user asks you to look at, "
            "describe, analyze or recognize the content of an image, or when "
            "they include a path to an image (e.g. 'file:///home/user/pic.png'). "
            "If the current model does not support vision, the tool returns "
            "the path and metadata so the user can open the image manually."
        )

    @property
    def parameters_schema(self):
        return {
            "path": {
                "type": "string",
                "description": (
                    "Path to the image. May be a 'file:///...' URL or a "
                    "plain absolute path (e.g. '/home/user/pic.png')."
                ),
            }
        }

    def _normalize_path(self, raw: str) -> Path:
        raw = raw.strip()
        if raw.lower().startswith("file://"):
            p = raw[7:]
            # Windows: file:///C:/... → /C:/... → C:/...
            if len(p) > 2 and p[0] == "/" and p[2] == ":":
                p = p[1:]
            return Path(unquote(p)).expanduser().resolve()
        return Path(raw).expanduser().resolve()

    def _read_svg_as_png(self, p: Path) -> bytes:
        """Convert SVG to PNG bytes. Requires cairosvg in MCP context."""
        try:
            import cairosvg
            return cairosvg.svg2png(url=str(p), output_width=1024)
        except ImportError:
            raise RuntimeError(
                "SVG in MCP context requires 'cairosvg'. "
                "Install: pip install cairosvg"
            )

    def execute(self, path: str) -> Any:
        try:
            p = self._normalize_path(path)

            if not p.exists():
                return f"Error: Image not found: {p}"
            if not p.is_file():
                return f"Error: Path is not a file: {p}"

            ext = p.suffix.lower()
            if ext not in SUPPORTED_EXT:
                return (
                    f"Error: Unsupported format '{ext}'. "
                    f"Supported: {', '.join(sorted(SUPPORTED_EXT))}"
                )

            size_bytes = p.stat().st_size
            size_mb = size_bytes / (1024 * 1024)
            if size_mb > MAX_SIZE_MB:
                return (
                    f"Error: Image too large ({size_mb:.1f} MB, "
                    f"max {MAX_SIZE_MB} MB)."
                )

            logger.info("Loading image: %s (%.1f KB)", p, size_bytes / 1024)

            info = (
                f"Image: {p.name} ({ext[1:].upper()}, "
                f"{size_mb:.2f} MB, path: {p})"
            )

            # Try to attach image for vision-capable clients
            try:
                if ext == ".svg":
                    data = self._read_svg_as_png(p)
                    return [info, Image(data=data, format="png")]
                return [info, Image(path=str(p))]
            except Exception as e:
                logger.warning("Cannot attach image to model: %s", e)
                return (
                    f"{info}\n"
                    f"Note: the current model cannot visually analyze the "
                    f"image, but the user can open it at the given path."
                )

        except Exception as e:
            logger.exception("Error loading image")
            return f"Error loading image: {e}"