# tool_memory.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.


import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from core.CtrlAsistent_tools import Tool

logger = logging.getLogger(__name__)

# Unicode-aware: matches runs of letters in ANY script.
WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)

# fmt: off
# Minimal English stop-words. Filtering is best-effort for English input;
# other languages keep all their words (never over-filters).
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "he", "her", "his", "i", "in", "is", "it", "its", "me",
    "my", "of", "on", "or", "our", "she", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "will", "with", "you", "your", "please", "just",
    # action words — should not become search/delete keywords
    "save", "remember", "store", "search", "find", "query", "delete",
    "remove", "forget", "list", "memory", "note", "add",
}

MIN_KEYWORD_LEN = 2

VALID_ACTIONS_HINT = "Valid actions (English): save, search, delete, list."

# Canonical actions are English. Aliases accept common English synonyms,
# abbreviations, and typos. Non-English action words are NOT accepted:
# models translate user intent to English before calling.
ACTION_ALIASES = {
    # save
    "save": "save", "remember": "save", "store": "save", "memorize": "save",
    "memorise": "save", "note": "save", "add": "save", "keep": "save",
    "record": "save", "write": "save", "put": "save", "set": "save",
    "sav": "save", "savee": "save", "savr": "save", "remembr": "save",
    # search
    "search": "search", "find": "search", "query": "search", "lookup": "search",
    "look_up": "search", "recall": "search", "retrieve": "search",
    "get": "search", "show": "search", "read": "search", "look": "search",
    "searc": "search", "serch": "search",
    # delete
    "delete": "delete", "remove": "delete", "forget": "delete", "erase": "delete",
    "drop": "delete", "clear": "delete", "discard": "delete", "wipe": "delete",
    "delet": "delete", "remov": "delete",
    # list
    "list": "list", "ls": "list", "all": "list", "dump": "list",
    "print": "list", "show_all": "list", "enumerate": "list", "display": "list",
}
# fmt: on


def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords from text in any language/script."""
    if not text:
        return []
    words = WORD_PATTERN.findall(text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) >= MIN_KEYWORD_LEN]


def calculate_relevance(item_text: str, keywords: List[str]) -> int:
    """Score item relevance by counting keyword hits."""
    item_lower = item_text.lower()
    return sum(1 for kw in keywords if kw in item_lower)


def _resolve_action(raw: str) -> str:
    """Map a raw action string to a canonical action, or '' if unknown."""
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in ACTION_ALIASES:
        return ACTION_ALIASES[key]
    # Prefix match — handles truncation like 'sear', 'delet'
    for alias, canonical in ACTION_ALIASES.items():
        if len(key) >= 3 and (alias.startswith(key) or key.startswith(alias)):
            return canonical
    return ""


class MemoryTool(Tool):
    """Persistent assistant memory for general (non-sensitive) information."""

    ROOT_DIR = Path(__file__).resolve().parent.parent
    MEMORY_FILE = str((ROOT_DIR / "memory" / "memory.json").resolve())

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return (
            "Persistent memory for general (non-sensitive) information. \n"
            "Actions are in English: save, search, delete, list. "
            "Stored text may be in ANY language.\n"
            "Examples:\n"
            '  {"action": "save", "text": "User has a mobile phone"}\n'
            '  {"action": "search", "text": "mobile phone"}\n'
            '  {"action": "delete", "text": "user mobile phone"}\n'
            '  {"action": "list"}'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "enum": ["save", "search", "delete", "list"],
                "description": (
                    "English action name. save = store text, "
                    "search = find stored items, delete = remove items, "
                    "list = show everything."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Text to store (save) or keywords to match (search/delete). "
                    "Any language is fine. Ignored for 'list'."
                ),
            },
        }

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.MEMORY_FILE):
            return []
        try:
            with open(self.MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupted memory file: %s — backing up", e)
            self._backup_corrupted_file()
            return []
        if not isinstance(data, list):
            logger.warning("Unexpected memory format — resetting")
            return []
        normalized = []
        for item in data:
            if isinstance(item, str):
                normalized.append({"text": item, "ts": None})
            elif isinstance(item, dict) and "text" in item:
                normalized.append(item)
        return normalized

    def _backup_corrupted_file(self) -> None:
        try:
            backup = f"{self.MEMORY_FILE}.corrupt.{int(time.time())}"
            os.replace(self.MEMORY_FILE, backup)
            logger.info("Backup created: %s", backup)
        except OSError:
            logger.exception("Failed to create backup")

    def _save(self, data: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.MEMORY_FILE), exist_ok=True)
        tmp = f"{self.MEMORY_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, self.MEMORY_FILE)

    def execute(self, action: str, text: str = "") -> str:
        try:
            canonical = _resolve_action(action)
            if not canonical:
                return (
                    f"❌ Unknown action: '{action}'. {VALID_ACTIONS_HINT}\n"
                    "Translate the user's intent to one of these English "
                    "actions before calling. Stored text may be any language."
                )
            text = (text or "").strip()
            memory = self._load()
            if canonical == "save":
                return self._memorize(text, memory)
            if canonical == "search":
                return self._query(text, memory)
            if canonical == "delete":
                return self._delete(text, memory)
            if canonical == "list":
                return self._list(memory)
            return f"❌ Unhandled action '{canonical}'. {VALID_ACTIONS_HINT}"
        except Exception as e:
            logger.exception("memory tool failed")
            return f"❌ Memory tool error: {type(e).__name__}: {e}"

    def _list(self, memory: List[Dict[str, Any]]) -> str:
        if not memory:
            return "❌ Memory is empty — nothing stored."
        lines = [f"📋 Total items in memory: {len(memory)}\n"]
        for idx, item in enumerate(memory, 1):
            lines.append(f"{idx}. {item['text']}")
        return "\n".join(lines)

    def _memorize(self, text: str, memory: List[Dict[str, Any]]) -> str:
        if not text:
            return "❌ Nothing to save — text is empty."
        memory.append({"text": text, "ts": time.time()})
        self._save(memory)
        return "✅ Saved to memory."

    def _query(self, query_text: str, memory: List[Dict[str, Any]]) -> str:
        if not memory:
            return "❌ Memory is empty — nothing stored."
        if not query_text:
            return "\n".join(f"• {item['text']}" for item in memory)
        keywords = extract_keywords(query_text) or [query_text.lower()]
        matches = [
            (calculate_relevance(item["text"], keywords), item["text"])
            for item in memory
        ]
        matches = [(score, t) for score, t in matches if score > 0]
        if not matches:
            return f"❌ Nothing stored about '{query_text}'."
        matches.sort(key=lambda x: x[0], reverse=True)
        return "\n".join(f"• {t}" for _, t in matches)

    def _delete(self, delete_text: str, memory: List[Dict[str, Any]]) -> str:
        keywords = extract_keywords(delete_text)
        if not keywords:
            return (
                "❌ Not sure what to delete. Provide at least two meaningful "
                "words (e.g. 'delete user mobile phone')."
            )
        if len(keywords) == 1:
            return (
                "❌ Too vague. Provide at least two words so deletion is "
                "unambiguous.\n"
                f"Example: 'delete user mobile phone' instead of '{keywords[0]}'."
            )
        deleted, remaining = [], []
        for item in memory:
            item_lower = item["text"].lower()
            if all(kw in item_lower for kw in keywords):
                deleted.append(item["text"])
            else:
                remaining.append(item)
        if not deleted:
            return f"❌ Nothing matched '{delete_text}'."
        self._save(remaining)
        deleted_list = "\n".join(f"• {t}" for t in deleted)
        return f"✅ Deleted:\n{deleted_list}"
