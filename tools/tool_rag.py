# tools/tool_rag.py
# MCP tool wrapper for DocumentIndexer.

import os
import re
import time
import logging
from pathlib import Path
from typing import Any, Dict
import threading
from datetime import datetime

from core.CtrlAsistent_tools import Tool

logger = logging.getLogger(__name__)

_MCP_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DOCS = Path(os.environ.get("RAG_DOCS_DIR", _MCP_ROOT / "documents"))
_DEFAULT_INDEX = Path(
    os.environ.get("RAG_INDEX_DIR", _MCP_ROOT / "documents" / "__index__")
)
_INDEXER = None
_INDEXER_LOCK = threading.Lock()


def _get_shared_indexer():
    global _INDEXER
    if _INDEXER is None:
        with _INDEXER_LOCK:
            if _INDEXER is None:  # double-checked locking
                from core.CtrlAsistent_DocumentIndexer import DocumentIndexer

                _INDEXER = DocumentIndexer(
                    docs_folder=_DEFAULT_DOCS,
                    index_folder=_DEFAULT_INDEX,
                )
    return _INDEXER


class RAGTool(Tool):
    """Search indexed local documents (PDF, TXT, code, Markdown)."""

    def __init__(self):
        self._indexer = None

    @property
    def name(self) -> str:
        return "rag_search"

    @property
    def description(self) -> str:
        return (
            "Searches LOCAL INDEXED DOCUMENTS (PDF, TXT, Markdown, code) using "
            "hybrid retrieval (vector + BM25). Use this when the user asks about "
            "content from the local knowledge base, their own documents, or "
            "project files. "
            "Returns the most relevant text excerpts with their source filenames."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": "Search query for the local document index",
            },
            "k": {
                "type": "integer",
                "description": "Number of results to return (default: 3)",
                "default": 3,
            },
        }

    def _get_indexer(self):
        return _get_shared_indexer()

    def execute(self, query: str, k: int = 3) -> str:
        try:
            indexer = self._get_indexer()
        except Exception as e:
            logger.exception("Failed to initialize RAG indexer")
            return (
                f"❌ RAG indexer failed to initialize: {type(e).__name__}: {e}\n"
                "Install required packages: "
                "pip install faiss-cpu sentence-transformers rank_bm25 pymupdf"
            )

        stats = indexer.get_stats()

        if not stats["embedder_available"]:
            return (
                "❌ RAG embedding model not available. Install:\n"
                "  pip install faiss-cpu sentence-transformers rank_bm25 pymupdf\n"
                "And make sure the model is cached:\n"
                '  python -c "from sentence_transformers import SentenceTransformer; '
                "SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')\"\n"
                f"Docs folder: {stats['docs_folder']}"
            )

        if stats["chunks"] == 0:
            return (
                f"📂 RAG index is empty — no documents found.\n"
                f"Put .pdf/.txt/.md/.py files into: {stats['docs_folder']}\n"
                f"Then restart the MCP server to trigger indexing."
            )

        try:
            chunks = indexer.search(query, k=max(1, min(k, 10)))
        except Exception as e:
            logger.exception("RAG search failed")
            return f"❌ RAG search error: {type(e).__name__}: {e}"

        if not chunks:
            return f"No results found for: {query}"

        output = [
            f"RAG RESULTS (query: {query})",
            f"Indexed documents: {stats['documents']}, total chunks: {stats['chunks']}",
            f"Returned: {len(chunks)} excerpts",
            "",
        ]
        for i, chunk in enumerate(chunks, 1):
            output.append(f"--- EXCERPT {i} ---")
            output.append(chunk.rstrip())
            output.append("")

        return "\n".join(output)


_REINDEX_LOCK = threading.Lock()
_REINDEX_STATE = {
    "running": False,
    "progress": "idle",
    "started_at": None,
    "started_at_ts": None,
    "finished_at": None,
    "error": None,
    "documents": 0,
    "chunks": 0,
    "files_total": 0,
    "files_done": 0,
    "current_file": "",
}


class RAGReindexTool(Tool):
    """Start RAG reindex in background. Returns immediately."""

    @property
    def name(self) -> str:
        return "rag_index"

    @property
    def description(self) -> str:
        return (
            "Starts a background re-scan of the local documents folder and "
            "rebuilds the RAG index. Returns IMMEDIATELY — the actual indexing "
            "runs in the background and may take minutes. "
            "Call `rag_status` afterwards to check progress. "
            "Use this after adding, modifying or deleting files in the "
            "documents directory."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {}

    def execute(self) -> str:
        with _REINDEX_LOCK:
            if _REINDEX_STATE["running"]:
                return (
                    f"⏳ Reindex already running (since "
                    f"{_REINDEX_STATE['started_at']}). "
                    f"Call rag_status to check progress."
                )
            _REINDEX_STATE.update(
                running=True,
                progress="working",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=None,
                error=None,
                files_total=0,
                files_done=0,
            )

        threading.Thread(target=_reindex_worker, daemon=True).start()

        return (
            "⏳ RAG reindex started in background.\n"
            "Call `rag_status` to check progress.\n"
            "First run may take several minutes (loads embedding model + "
            "indexes all files)."
        )


def _reindex_worker():
    global _INDEXER
    try:
        indexer = _get_shared_indexer()

        def _progress_hook(msg: str):
            """Update _REINDEX_STATE from DocumentIndexer's progress messages."""
            with _REINDEX_LOCK:
                # "Indeksiranje 19379 dokumenata..." → set total
                m = re.match(r"Indeksiranje (\d+)", msg)
                if m:
                    _REINDEX_STATE["files_total"] = int(m.group(1))
                    _REINDEX_STATE["started_at_ts"] = time.time()
                    _REINDEX_STATE["progress"] = "indexing"
                    return

                # "Obrađujem: docs/file.md" → increment counter
                if msg.startswith("Obrađujem:"):
                    _REINDEX_STATE["files_done"] += 1
                    _REINDEX_STATE["current_file"] = msg.split(":", 1)[1].strip()
                    return

                # Anything else (Provjeravam, Stvaram vektorski indeks) → phase
                _REINDEX_STATE["progress"] = msg

        old_callback = indexer.progress_callback
        indexer.progress_callback = _progress_hook

        with _REINDEX_LOCK:
            _REINDEX_STATE["progress"] = "starting"

        try:
            indexer._check_new()
        finally:
            indexer.progress_callback = old_callback

        stats = indexer.get_stats()
        with _REINDEX_LOCK:
            _REINDEX_STATE.update(
                progress="done",
                finished_at=datetime.now().isoformat(timespec="seconds"),
                documents=stats["documents"],
                chunks=stats["chunks"],
            )
    except Exception as e:
        logger.exception("RAG reindex failed")
        with _REINDEX_LOCK:
            _REINDEX_STATE.update(
                progress="failed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
                error=f"{type(e).__name__}: {e}",
            )
    finally:
        with _REINDEX_LOCK:
            _REINDEX_STATE["running"] = False


class RAGReindexStatusTool(Tool):
    """Check status of RAG reindex."""

    @property
    def name(self) -> str:
        return "rag_status"

    @property
    def description(self) -> str:
        return (
            "Returns the current status of the RAG reindex operation: "
            "idle / running / done / failed, with document and chunk counts."
        )

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {}

    def execute(self) -> str:
        with _REINDEX_LOCK:
            state = dict(_REINDEX_STATE)

        if state["progress"] == "idle":
            return (
                "ℹ️ No reindex has been started in this session.\n"
                "Call `rag_index` to start one."
            )

        if state["running"]:
            lines = ["⏳ Reindex RUNNING", f"Started: {state['started_at']}"]

            total = state.get("files_total", 0)
            done = state.get("files_done", 0)

            if total > 0:
                pct = 100 * done / total
                width = 20
                filled = int(width * done / total)
                bar = "#" * filled + "-" * (width - filled)
                lines.append(f"[{bar}] {done}/{total} ({pct:.0f}%)")

                if state.get("started_at_ts") and done >= 5:
                    elapsed = time.time() - state["started_at_ts"]
                    per_file = elapsed / done
                    remaining = (total - done) * per_file
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    lines.append(f"ETA: ~{mins}m {secs}s")

                if state.get("current_file"):
                    lines.append(f"Current: {state['current_file']}")
            else:
                lines.append(f"Progress: {state['progress']}")

            return "\n".join(lines)

        if state["progress"] == "done":
            return (
                f"✅ Reindex COMPLETE\n"
                f"Started: {state['started_at']}\n"
                f"Finished: {state['finished_at']}\n"
                f"Documents: {state['documents']}\n"
                f"Chunks: {state['chunks']}"
            )

        return (
            f"❌ Reindex FAILED\n"
            f"Started: {state['started_at']}\n"
            f"Finished: {state['finished_at']}\n"
            f"Error: {state['error']}"
        )
