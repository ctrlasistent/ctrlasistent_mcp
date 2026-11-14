# tool_memory_encrypted.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.


import base64
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.CtrlAsistent_tools import Tool

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet

    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

WORD_PATTERN = re.compile(r"[^\W\d_]+", re.UNICODE)

# fmt: off
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "he", "her", "his", "i", "in", "is", "it", "its", "me",
    "my", "of", "on", "or", "our", "she", "that", "the", "their", "them",
    "they", "this", "to", "was", "we", "were", "what", "when", "where",
    "which", "who", "will", "with", "you", "your", "please", "just",
    # action words
    "save", "remember", "store", "search", "find", "query", "delete",
    "remove", "forget", "list", "memory", "note", "add",
    # encryption noise
    "encrypted", "encrypt", "password", "secret", "secretly", "private",
}

MIN_KEYWORD_LEN = 2

VALID_ACTIONS_HINT = "Valid actions (English): save, search, delete, list."

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
    if not text:
        return []
    words = WORD_PATTERN.findall(text.lower())
    return [w for w in words if w not in STOP_WORDS and len(w) >= MIN_KEYWORD_LEN]


def calculate_relevance(item_text: str, keywords: List[str]) -> int:
    item_lower = item_text.lower()
    return sum(1 for kw in keywords if kw in item_lower)


def _resolve_action(raw: str) -> str:
    if not raw:
        return ""
    key = raw.strip().lower()
    if key in ACTION_ALIASES:
        return ACTION_ALIASES[key]
    for alias, canonical in ACTION_ALIASES.items():
        if len(key) >= 3 and (alias.startswith(key) or key.startswith(alias)):
            return canonical
    return ""


def _get_fernet_key(password: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(password.encode()).digest())


def _encrypt_text(text: str, password: str) -> str:
    if HAS_CRYPTOGRAPHY:
        f = Fernet(_get_fernet_key(password))
        return f.encrypt(text.encode()).decode()
    key = hashlib.sha256(password.encode()).digest()
    text_bytes = text.encode("utf-8")
    encrypted = bytes(text_bytes[i] ^ key[i % len(key)] for i in range(len(text_bytes)))
    return base64.b64encode(encrypted).decode("utf-8")


def _decrypt_text(enc_text: str, password: str) -> Optional[str]:
    if HAS_CRYPTOGRAPHY:
        f = Fernet(_get_fernet_key(password))
        try:
            return f.decrypt(enc_text.encode()).decode()
        except Exception:
            return None
    try:
        key = hashlib.sha256(password.encode()).digest()
        encrypted = base64.b64decode(enc_text.encode("utf-8"))
        decrypted = bytes(
            encrypted[i] ^ key[i % len(key)] for i in range(len(encrypted))
        )
        return decrypted.decode("utf-8")
    except Exception:
        return None


def _parse_password_fallback(text: str) -> tuple:
    """Last-resort extraction of 'password: X ...' from the text field."""
    if not text:
        return None, ""
    m = re.search(
        r"password\s*[:=]\s*(\S+)\s+(.*)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, text


class EncryptedMemoryTool(Tool):
    """Password-encrypted memory. Use ONLY when user explicitly requests it."""

    ROOT_DIR = Path(__file__).resolve().parent.parent
    MEMORY_FILE = str((ROOT_DIR / "memory" / "memory_encrypted.json").resolve())

    @property
    def name(self) -> str:
        return "memory_encrypted"

    @property
    def description(self) -> str:
        return (
            "Password-encrypted memory. Use ONLY when the user explicitly "
            "asks for encryption\n"
            "IMPORTANT: never use the stored content itself as the encryption key, "
            "if the user does not supply a distinct password, ask for one. "
            "Stored text may be in ANY language.\n"
            "Examples (password required):\n"
            '  {"action": "save", "password": "1234", "text": "..."}\n'
            '  {"action": "search", "password": "1234", "text": "..."}\n'
            '  {"action": "delete", "password": "1234", "text": "..."}\n'
            '  {"action": "list", "password": "1234"}'
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "action": {
                "type": "string",
                "enum": ["save", "search", "delete", "list"],
                "description": "English action name.",
            },
            "password": {
                "type": "string",
                "description": "Key used to encrypt/decrypt. Required. ",
            },
            "text": {
                "type": "string",
                "description": "Content or keywords. Ignored for 'list'.",
                "default": "",
            },
        }

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.MEMORY_FILE):
            return []
        try:
            with open(self.MEMORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Corrupted encrypted memory: %s — backing up", e)
            self._backup_corrupted_file()
            return []
        if not isinstance(data, list):
            logger.warning("Unexpected encrypted memory format — resetting")
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

    def execute(
        self,
        action: str,
        password: Optional[str] = None,
        text: Optional[str] = None,
    ) -> str:
        try:
            canonical = _resolve_action(action)
            if not canonical:
                return (
                    f"❌ Unknown action: '{action}'. {VALID_ACTIONS_HINT}\n"
                    "Translate the user's intent to one of these English "
                    "actions before calling."
                )

            text = (text or "").strip()
            password = (password or "").strip()

            if not password:
                parsed_pw, parsed_text = _parse_password_fallback(text)
                if parsed_pw:
                    password, text = parsed_pw, parsed_text

            if not password:
                return (
                    "❌ This tool requires a password. If the user did not "
                    "provide a password, ask for one. "
                )

            memory = self._load()
            if canonical == "save":
                return self._memorize(text, password, memory)
            if canonical == "search":
                return self._query(text, password, memory)
            if canonical == "delete":
                return self._delete(text, password, memory)
            if canonical == "list":
                return self._list(password, memory)
            return f"❌ Unhandled action '{canonical}'. {VALID_ACTIONS_HINT}"
        except Exception as e:
            logger.exception("memory_encrypted tool failed")
            return f"❌ Memory tool error: {type(e).__name__}: {e}"

    def _memorize(self, text: str, password: str, memory: List[Dict[str, Any]]) -> str:
        if not text:
            return "❌ Nothing to save — text is empty."
        memory.append({"text": _encrypt_text(text, password), "ts": time.time()})
        self._save(memory)
        return "✅ Saved to encrypted memory."

    def _query(
        self, query_text: str, password: str, memory: List[Dict[str, Any]]
    ) -> str:
        if not memory:
            return "❌ Encrypted memory is empty."
        decrypted = []
        failed = 0
        for item in memory:
            plain = _decrypt_text(item["text"], password)
            if plain is None:
                failed += 1
            else:
                decrypted.append(plain)
        if not decrypted:
            if failed:
                return (
                    "❌ Could not decrypt any item with that password. "
                    f"(attempted {failed} items — wrong password?)"
                )
            return "❌ No items belong to that password."
        if not query_text:
            return "\n".join(f"• {t}" for t in decrypted)
        keywords = extract_keywords(query_text) or [query_text.lower()]
        scored = [(calculate_relevance(t, keywords), t) for t in decrypted]
        scored = [(s, t) for s, t in scored if s > 0]
        if not scored:
            return (
                f"❌ Decrypted {len(decrypted)} items with that password, "
                f"but none contain '{query_text}'."
            )
        scored.sort(key=lambda x: x[0], reverse=True)
        return "\n".join(f"• {t}" for _, t in scored)

    def _list(self, password: str, memory: List[Dict[str, Any]]) -> str:
        if not memory:
            return "❌ Encrypted memory is empty."
        decrypted = []
        for item in memory:
            plain = _decrypt_text(item["text"], password)
            if plain is not None:
                decrypted.append(plain)
        if not decrypted:
            return "❌ No items decrypt with that password."
        lines = [f"📋 Encrypted memory ({len(decrypted)} items):"]
        for i, t in enumerate(decrypted, 1):
            lines.append(f"{i}. {t}")
        return "\n".join(lines)

    def _delete(
        self, delete_text: str, password: str, memory: List[Dict[str, Any]]
    ) -> str:
        if not memory:
            return "❌ Encrypted memory is empty."
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
        deleted, deleted_indices = [], set()
        for idx, item in enumerate(memory):
            plain = _decrypt_text(item["text"], password)
            if plain is None:
                continue
            if all(kw in plain.lower() for kw in keywords):
                deleted.append(plain)
                deleted_indices.add(idx)
        if not deleted:
            return f"❌ Nothing matched '{delete_text}' with that password."
        remaining = [it for i, it in enumerate(memory) if i not in deleted_indices]
        self._save(remaining)
        return "✅ Deleted encrypted items:\n" + "\n".join(f"• {t}" for t in deleted)
