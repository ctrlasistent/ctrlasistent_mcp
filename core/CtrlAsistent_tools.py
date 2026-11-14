# core/CtrlAsistent_tools.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Type, Optional


def tr(text: str) -> str:
    """Passthrough — Without Qt dependency for MCP server."""
    return text


def register_tool(cls):
    """No-op dekorator"""
    return cls


class Tool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        # Default empty schema
        return {}

    @abstractmethod
    def execute(self, **kwargs) -> str:
        pass

    def process_assistant_text(self, text: str) -> Optional[str]:
        return None
