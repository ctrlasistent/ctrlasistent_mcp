# core/CtrlAsistent_DocumentIndexer.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import re
import sys
import pickle
import json
import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _default_progress(msg: str) -> None:
    """Progress reporter — writes to stderr (visible in MCP client logs)."""
    print(f"[RAG] {msg}", file=sys.stderr, flush=True)


class DocumentIndexer:
    """Hybrid RAG indexer (FAISS + BM25) — no Qt dependencies.

    Uses sentence-transformers/all-MiniLM-L6-v2 for embeddings and
    rank_bm25 for sparse retrieval. Combines both for hybrid search.
    """

    def __init__(
        self,
        docs_folder: str | Path = "documents",
        index_folder: str | Path = "index",
        progress_callback: Optional[Callable[[str], None]] = None,
    ):
        self.docs_folder = Path(docs_folder)
        self.index_folder = Path(index_folder)
        self.index_folder.mkdir(parents=True, exist_ok=True)
        self.progress_callback = progress_callback or _default_progress
        self.embedder = None

        # ---- Dependencies (lazy, optional) ----
        self._faiss = None
        self._np = None
        self._BM25Okapi = None
        deps_ok = False
        try:
            import faiss
            import numpy as np
            from rank_bm25 import BM25Okapi
            from sentence_transformers import SentenceTransformer

            self._faiss = faiss
            self._np = np
            self._BM25Okapi = BM25Okapi
            self._SentenceTransformer = SentenceTransformer
            deps_ok = True
        except ImportError as e:
            logger.warning("RAG dependencies missing: %s", e)
            self.progress_callback(
                "RAG disabled — install: faiss-cpu sentence-transformers rank_bm25 pymupdf"
            )

        # ---- Paths ----
        self.metadata_path = self.index_folder / "metadata.json"
        self.chunks_path = self.index_folder / "chunks.pkl"
        self.bm25_path = self.index_folder / "bm25.pkl"
        self.faiss_path = self.index_folder / "faiss.index"
        self.cache_path = self.index_folder / "embeddings.pkl"

        self.podrzane_ekstenzije = {
            ".pdf",
            ".txt",
            ".h",
            ".c",
            ".hpp",
            ".cpp",
            ".py",
            ".md",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            ".ini",
            ".cfg",
            ".css",
            ".html",
            ".xml",
            ".sh",
            ".sql",
        }

        # ---- Load existing state ----
        self.metadata = {}
        if self.metadata_path.exists():
            try:
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    self.metadata = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Cannot read metadata.json: %s", e)

        self.chunks = []
        try:
            with open(self.chunks_path, "rb") as f:
                self.chunks = pickle.load(f)
        except (EOFError, FileNotFoundError, pickle.UnpicklingError):
            self.chunks = []

        self.bm25 = None
        try:
            with open(self.bm25_path, "rb") as f:
                self.bm25 = pickle.load(f)
        except (EOFError, FileNotFoundError, pickle.UnpicklingError):
            self.bm25 = None

        self.index = None
        if self.faiss_path.exists() and self._faiss is not None:
            try:
                self.index = self._faiss.read_index(str(self.faiss_path))
            except Exception as e:
                logger.warning("Cannot read faiss.index: %s", e)

        if deps_ok:
            self._load_embedder()
            self._check_new()
        else:
            logger.warning(
                "RAG disabled — install: faiss-cpu sentence-transformers rank_bm25 pymupdf"
            )

    def _report(self, msg: str) -> None:
        if self.progress_callback:
            self.progress_callback(msg)

    def _save_partial_progress(self, cache: dict, hashevi: dict) -> None:
        """Save partial progress before interrupt.

        Saves cache.pkl (partial embeddings) and metadata.json (full disk state).
        chunks.pkl/bm25.pkl/faiss.index are rebuilt from cache on next run.
        """
        logger.warning(
            "Interrupted — saving partial progress (%d/%d files)",
            len(cache),
            len(hashevi),
        )
        self.metadata = {ime: {"hash": h} for ime, h in hashevi.items()}
        try:
            with open(self.cache_path, "wb") as f:
                pickle.dump(cache, f)
            with open(self.metadata_path, "w", encoding="utf-8") as f:
                json.dump(self.metadata, f, indent=2, ensure_ascii=False)
            logger.warning(
                "Progress saved. Next run will process %d remaining files.",
                len(hashevi) - len(cache),
            )
        except Exception as e:
            logger.error("Saving partial progress failed: %s", e)

    def _load_embedder(self) -> None:
        try:
            from huggingface_hub import snapshot_download

            local_model_path = snapshot_download(
                repo_id="sentence-transformers/all-MiniLM-L6-v2",
                local_files_only=True,
            )
            self.embedder = self._SentenceTransformer(
                local_model_path,
                trust_remote_code=False,
                local_files_only=True,
            )
        except Exception as e:
            logger.warning("Embedding model not available locally: %s", e)
            self.embedder = None

    def _hash_file(self, path: Path) -> str:
        sha = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()

    def _load_pdf(self, path: Path) -> str:
        import pymupdf

        doc = pymupdf.open(path)
        text = "".join(page.get_text("text") + "\n" for page in doc)
        doc.close()
        return text

    def _load_text_file(self, path: Path) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except Exception:
            with open(path, "r", encoding="latin-1") as f:
                return f.read()

    def _split_code(
        self, text: str, file_name: str, chunk_size: int = 2000, overlap: int = 300
    ):
        lines = text.splitlines()
        chunks = []
        current_chunk = []
        current_length = 0
        prefix = f"[Izvor datoteke: {file_name}]\n"
        max_eff_size = chunk_size - len(prefix)

        for line in lines:
            line_len = len(line) + 1
            if current_length + line_len > max_eff_size and current_chunk:
                chunks.append(prefix + "\n".join(current_chunk))
                overlap_lines = []
                overlap_len = 0
                for old_line in reversed(current_chunk):
                    if overlap_len + len(old_line) + 1 < overlap:
                        overlap_lines.insert(0, old_line)
                        overlap_len += len(old_line) + 1
                    else:
                        break
                current_chunk = overlap_lines
                current_length = overlap_len
            current_chunk.append(line)
            current_length += line_len

        if current_chunk:
            chunks.append(prefix + "\n".join(current_chunk))
        return chunks

    def _split(
        self, text: str, file_name: str, chunk_size: int = 2000, overlap: int = 300
    ):
        ext = Path(file_name).suffix.lower()
        if ext in {
            ".c",
            ".h",
            ".cpp",
            ".hpp",
            ".py",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
        }:
            return self._split_code(text, file_name, chunk_size, overlap)

        chunks = []
        start = 0
        prefix = f"[Izvor datoteke: {file_name}]\n"
        efektivna_velicina = chunk_size - len(prefix)
        while start < len(text):
            end = start + efektivna_velicina
            chunks.append(prefix + text[start:end])
            start += efektivna_velicina - overlap
        return chunks

    def _popisi_podrzane_datoteke(self):
        if not self.docs_folder.exists():
            return []
        skip_parts = {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            "dist",
            "build",
            ".next",
            ".cache",
        }
        try:
            index_resolved = self.index_folder.resolve()
        except Exception:
            index_resolved = None

        files = []
        for f in self.docs_folder.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in self.podrzane_ekstenzije:
                continue
            if any(part in skip_parts for part in f.parts):
                continue
            # Preskoči SAMO datoteke unutar index foldera
            if index_resolved is not None:
                try:
                    f.resolve().relative_to(index_resolved)
                    continue
                except (ValueError, OSError):
                    pass
            files.append(f)

        logger.info("Found %d candidate files in %s", len(files), self.docs_folder)
        if logger.isEnabledFor(logging.DEBUG):
            for f in files:
                logger.debug("  - %s", f.relative_to(self.docs_folder))
        return files

    def _check_new(self) -> None:
        if not self.docs_folder.exists():
            self.docs_folder.mkdir(parents=True, exist_ok=True)
            return

        self._report("Provjeravam dokumente...")
        datoteke = self._popisi_podrzane_datoteke()
        hashevi = {}
        for d in datoteke:
            try:
                key = str(d.relative_to(self.docs_folder))
                hashevi[key] = self._hash_file(d)
            except (FileNotFoundError, PermissionError, OSError) as e:
                logger.warning("Skipping %s: %s", d, e)

        cache = {}
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "rb") as f:
                    cache = pickle.load(f)
            except (EOFError, FileNotFoundError, pickle.UnpicklingError):
                cache = {}

        obrisane = [
            ime for ime in set(cache) | set(self.metadata) if ime not in hashevi
        ]
        za_obradu = [
            ime
            for ime, h in hashevi.items()
            if self.metadata.get(ime, {}).get("hash") != h or ime not in cache
        ]

        if not za_obradu and not obrisane:
            logger.info(
                "RAG index up to date: %d chunks, %d files",
                len(self.chunks),
                len(self.metadata),
            )
            return

        if za_obradu:
            self._report(f"Indeksiranje {len(za_obradu)} dokumenata...")

        work_complete = False
        try:
            for ime in za_obradu:
                datoteka = self.docs_folder / ime
                self._report(f"Obrađujem: {ime}")
                try:
                    if datoteka.suffix.lower() == ".pdf":
                        text = self._load_pdf(datoteka)
                    else:
                        text = self._load_text_file(datoteka)
                except (FileNotFoundError, PermissionError, OSError) as e:
                    logger.warning("Skipping %s during load: %s", ime, e)
                    continue
                chunks = self._split(text, ime)
                emb = None
                if self.embedder is not None and chunks:
                    emb = self._np.asarray(
                        self.embedder.encode(
                            chunks,
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        ),
                        dtype="float32",
                    )
                cache[ime] = {"chunks": chunks, "emb": emb}
            work_complete = True
        finally:
            if not work_complete:
                self._save_partial_progress(cache, hashevi)

        for ime in obrisane:
            cache.pop(ime, None)

        self.metadata = {ime: {"hash": h} for ime, h in hashevi.items()}

        if self.embedder is not None:
            for ime, entry in cache.items():
                if entry["emb"] is None and entry["chunks"]:
                    self._report(f"Ažuriram vektore: {ime}")
                    entry["emb"] = self._np.asarray(
                        self.embedder.encode(
                            entry["chunks"],
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        ),
                        dtype="float32",
                    )

        all_chunks = []
        all_emb = []
        for ime in sorted(cache.keys()):
            entry = cache[ime]
            all_chunks.extend(entry["chunks"])
            if entry["emb"] is not None:
                all_emb.append(entry["emb"])

        sve_emb = bool(cache) and all(
            entry["emb"] is not None or not entry["chunks"] for entry in cache.values()
        )
        self.chunks = all_chunks

        if self.chunks and self.embedder is not None and sve_emb:
            self._report("Stvaram vektorski indeks...")
            tokenized = [c.split() for c in self.chunks]
            self.bm25 = self._BM25Okapi(tokenized)
            emb = self._np.vstack(all_emb)
            self.index = self._faiss.IndexFlatIP(emb.shape[1])
            self.index.add(emb)

            with open(self.chunks_path, "wb") as f:
                pickle.dump(self.chunks, f)
            with open(self.bm25_path, "wb") as f:
                pickle.dump(self.bm25, f)
            self._faiss.write_index(self.index, str(self.faiss_path))
        else:
            self.bm25 = None
            self.index = None

        if cache:
            with open(self.cache_path, "wb") as f:
                pickle.dump(cache, f)
        else:
            for path in (
                self.chunks_path,
                self.bm25_path,
                self.faiss_path,
                self.cache_path,
            ):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

        with open(self.metadata_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, indent=2, ensure_ascii=False)

        logger.info(
            "RAG index updated: %d chunks, %d files",
            len(self.chunks),
            len(self.metadata),
        )

    def search(self, query: str, k: int = 3):
        if (
            not self.chunks
            or self.index is None
            or self.bm25 is None
            or self.embedder is None
        ):
            return []
        qv = self.embedder.encode(
            [query],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        scores, ids = self.index.search(qv, k)

        dense = [self.chunks[i] for i in ids[0]]
        tokens = query.split()
        bm = self.bm25.get_scores(tokens)
        top = sorted(range(len(bm)), key=lambda i: bm[i], reverse=True)[:k]
        sparse = [self.chunks[i] for i in top]
        combined = list(dict.fromkeys(dense + sparse))
        return combined[:k]

    def get_stats(self) -> dict:
        return {
            "documents": len(self.metadata),
            "chunks": len(self.chunks),
            "has_vector_index": self.index is not None,
            "has_bm25": self.bm25 is not None,
            "embedder_available": self.embedder is not None,
            "docs_folder": str(self.docs_folder),
            "index_folder": str(self.index_folder),
        }
