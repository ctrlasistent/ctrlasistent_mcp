#!/usr/bin/env python3
"""Standalone RAG diagnostic tool"""

# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import os
import sys
import time
import pickle
import json
import logging
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_MCP_ROOT))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

t0 = time.time()


def step(msg: str) -> None:
    print(f"\n>>> [{time.time()-t0:7.2f}s] {msg}", flush=True)


def safe_relative(path: Path, parent: Path) -> Path | str:
    try:
        return path.relative_to(parent)
    except (ValueError, OSError):
        return path


_IS_TTY = sys.stdout.isatty()


class ProgressBar:
    """Single-line progress bar with global (cross-run) progress.

    `total` is the total number of files on disk. `current` starts at the
    number already indexed in previous runs, so the bar reflects cumulative
    progress across interrupted sessions.
    """

    def __init__(self) -> None:
        self.total = 0
        self.current = 0
        self.started = None
        self.last_len = 0
        self.active = False
        self.last_decile = -1
        self.session_start = 0

    def start(self, total: int, initial: int = 0) -> None:
        self.close()
        self.total = total
        self.current = initial
        self.session_start = initial
        self.started = time.time()
        self.last_decile = int(100 * initial / total) // 10 if total > 0 else -1

    def update(self, filename: str) -> None:
        self.current += 1
        line = self._build(filename)
        if _IS_TTY:
            self._render_inplace(line)
        else:
            if self.total <= 0:
                print(line, flush=True)
                return
            pct = 100 * self.current / self.total
            decile = int(pct // 10)
            if (
                self.current == self.session_start + 1
                or self.current == self.total
                or decile > self.last_decile
            ):
                print(line, flush=True)
                self.last_decile = decile

    def close(self) -> None:
        if self.active:
            print(flush=True)
            self.active = False
            self.last_len = 0

    def _build(self, filename: str) -> str:
        if self.total <= 0:
            return f"  [{self.current}] {filename}"
        pct = 100 * self.current / self.total
        eta = ""
        session_done = self.current - self.session_start
        if self.started is not None and session_done >= 5:
            per_file = (time.time() - self.started) / session_done
            remaining = (self.total - self.current) * per_file
            eta = f" ETA {remaining:5.0f}s"
        width = 20
        filled = min(int(width * self.current / self.total), width)
        bar = "#" * filled + "-" * (width - filled)
        name = filename
        if len(name) > 40:
            name = "..." + name[-37:]
        return f"  [{bar}] {self.current}/{self.total} " f"({pct:3.0f}%){eta}  {name}"

    def _render_inplace(self, line: str) -> None:
        try:
            width = os.get_terminal_size().columns
        except (OSError, ValueError):
            width = 120
        if len(line) > width:
            line = line[: width - 3] + "..."
        padded = line.ljust(self.last_len)
        sys.stdout.write(f"\r{padded}")
        sys.stdout.flush()
        self.last_len = len(line)
        self.active = True


try:
    step("STEP 1: Load .env")

    env_file = _MCP_ROOT / ".env"
    print(f"  MCP root:    {_MCP_ROOT}")
    print(f"  .env exists: {env_file.exists()}")

    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
        print("  .env loaded")

    step("STEP 2: Resolve RAG paths")

    from tools.tool_rag import _DEFAULT_DOCS, _DEFAULT_INDEX

    docs = _DEFAULT_DOCS
    index = _DEFAULT_INDEX

    print(f"  docs folder:  {docs}")
    print(f"  index folder: {index}")

    step("STEP 3: Check HuggingFace cache")

    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    model_dir = cache_dir / "models--sentence-transformers--all-MiniLM-L6-v2"
    print(f"  cache exists: {model_dir.exists()}")

    step("STEP 4: Test imports")

    try:
        import faiss

        print("  faiss OK")
    except Exception as e:
        print(f"  [FAIL] faiss: {e}")
        sys.exit(1)

    try:
        import numpy as np

        print("  numpy OK")
    except Exception as e:
        print(f"  [FAIL] numpy: {e}")
        sys.exit(1)

    step("STEP 5: Load embedding model")

    try:
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer

        m0 = time.time()
        local_path = snapshot_download(
            repo_id="sentence-transformers/all-MiniLM-L6-v2",
            local_files_only=True,
        )
        print("  Loading SentenceTransformer (may take 5-20s)...")
        embedder = SentenceTransformer(
            local_path,
            trust_remote_code=False,
            local_files_only=True,
        )
        print(f"  [ok] loaded in {time.time()-m0:.2f}s")
    except Exception as e:
        print(f"  [FAIL] model load: {type(e).__name__}: {e}")
        sys.exit(1)

    step("STEP 6: Scan documents folder and index")

    from core.CtrlAsistent_DocumentIndexer import DocumentIndexer

    class ProgressIndexer(DocumentIndexer):
        """DocumentIndexer with silent scan, progress bar, and Ctrl+C resume.

        All _report() messages from this subclass are in English.
        Base-class messages in Croatian are translated in the handler
        for safety, but _do_check_new bypasses them entirely.
        """

        def __init__(self, *args, **kwargs):
            self._bar = ProgressBar()
            self._hash_cache: dict[str, str] = {}
            super().__init__(*args, **kwargs)

        def _hash_file(self, path):
            key = str(path)
            if key in self._hash_cache:
                return self._hash_cache[key]
            result = super()._hash_file(path)
            self._hash_cache[key] = result
            return result

        def _check_new(self):
            doc_logger = logging.getLogger("core.CtrlAsistent_DocumentIndexer")
            old_level = doc_logger.level
            doc_logger.setLevel(logging.WARNING)
            try:
                self._do_check_new()
            finally:
                doc_logger.setLevel(old_level)
                self._bar.close()

        def _save_partial_progress(self, cache, hashevi):
            print(
                f"\n  [save] writing cache ({len(cache)} files) "
                f"+ metadata ({len(hashevi)} files)...",
                flush=True,
            )
            try:
                with open(self.cache_path, "wb") as f:
                    pickle.dump(cache, f)
                print(
                    f"  [save] cache.pkl written "
                    f"({self.cache_path.stat().st_size} bytes)",
                    flush=True,
                )

                metadata = {ime: {"hash": h} for ime, h in hashevi.items()}
                with open(self.metadata_path, "w", encoding="utf-8") as f:
                    json.dump(metadata, f, indent=2, ensure_ascii=False)
                print(
                    f"  [save] metadata.json written "
                    f"({self.metadata_path.stat().st_size} bytes)",
                    flush=True,
                )

                with open(self.cache_path, "rb") as f:
                    verify = pickle.load(f)
                with open(self.metadata_path, "r", encoding="utf-8") as f:
                    verify_meta = json.load(f)

                print(
                    f"  [save] VERIFIED - cache has {len(verify)} entries, "
                    f"metadata has {len(verify_meta)} entries",
                    flush=True,
                )
                print(
                    f"  [save] next run will process "
                    f"{len(hashevi) - len(cache)} remaining files",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"  [save] FAILED: {type(e).__name__}: {e}",
                    flush=True,
                )

        def _do_check_new(self):
            cache: dict = {}
            hashevi: dict = {}
            scan_complete = False
            work_complete = False
            phase = "init"

            try:
                if not self.docs_folder.exists():
                    self.docs_folder.mkdir(parents=True, exist_ok=True)
                    work_complete = True
                    return

                # ── PHASE: scan ─────────────────────────
                phase = "scan"
                self._report("Scanning documents...")
                datoteke = self._popisi_podrzane_datoteke()
                print(f"  [scan] found {len(datoteke)} candidate files", flush=True)

                for d in datoteke:
                    try:
                        key = str(d.relative_to(self.docs_folder))
                        hashevi[key] = self._hash_file(d)
                    except (FileNotFoundError, PermissionError, OSError) as e:
                        logging.getLogger(__name__).warning("Skipping %s: %s", d, e)
                scan_complete = True

                # ── PHASE: load state (LOCAL, don't rely on self.*) ──
                phase = "load_state"

                local_metadata = {}
                if self.metadata_path.exists():
                    try:
                        with open(self.metadata_path, "r", encoding="utf-8") as f:
                            local_metadata = json.load(f)
                    except (json.JSONDecodeError, OSError) as e:
                        print(
                            f"  [state] metadata.json read failed: {e}",
                            flush=True,
                        )

                if self.cache_path.exists():
                    try:
                        with open(self.cache_path, "rb") as f:
                            cache = pickle.load(f)
                    except (EOFError, FileNotFoundError, pickle.UnpicklingError) as e:
                        print(
                            f"  [state] cache.pkl read failed: {e}",
                            flush=True,
                        )
                        cache = {}

                print(
                    f"  [state] loaded: metadata={len(local_metadata)} files, "
                    f"cache={len(cache)} files, total_on_disk={len(hashevi)}",
                    flush=True,
                )

                # ── PHASE: compute work ─────────────────
                phase = "compute"
                obrisane = [
                    ime
                    for ime in set(cache) | set(local_metadata)
                    if ime not in hashevi
                ]
                za_obradu = []
                already_done = 0
                for ime, h in hashevi.items():
                    cached_hash = local_metadata.get(ime, {}).get("hash")
                    if cached_hash != h or ime not in cache:
                        za_obradu.append(ime)
                    else:
                        already_done += 1

                print(
                    f"  [state] za_obradu={len(za_obradu)} "
                    f"(already_done={already_done}, deleted={len(obrisane)})",
                    flush=True,
                )

                if not za_obradu and not obrisane:
                    cache_chunks = sum(len(e.get("chunks", [])) for e in cache.values())
                    if len(self.chunks) == cache_chunks:
                        print(
                            f"  [{time.time()-t0:7.2f}s] Index up to date "
                            f"({len(self.chunks)} chunks)",
                            flush=True,
                        )
                        work_complete = True
                        return

                if za_obradu:
                    self._bar.start(len(hashevi), initial=already_done)
                    if already_done > 0:
                        print(
                            f"  [{time.time()-t0:7.2f}s] Resuming: "
                            f"{already_done} already indexed, "
                            f"{len(za_obradu)} remaining "
                            f"(of {len(hashevi)} total)",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [{time.time()-t0:7.2f}s] Indexing "
                            f"{len(za_obradu)} documents...",
                            flush=True,
                        )
                else:
                    self._bar.start(len(hashevi), initial=len(hashevi))

                # ── PHASE: index loop ───────────────────
                phase = "index_loop"
                for ime in za_obradu:
                    datoteka = self.docs_folder / ime
                    self._report(f"Processing: {ime}")
                    try:
                        if datoteka.suffix.lower() == ".pdf":
                            text = self._load_pdf(datoteka)
                        else:
                            text = self._load_text_file(datoteka)
                    except (FileNotFoundError, PermissionError, OSError) as e:
                        logging.getLogger(__name__).warning(
                            "Skipping %s during load: %s", ime, e
                        )
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

                # ── PHASE: post-process ─────────────────
                phase = "post_process"
                for ime in obrisane:
                    cache.pop(ime, None)

                self.metadata = {ime: {"hash": h} for ime, h in hashevi.items()}
                with open(self.metadata_path, "w", encoding="utf-8") as f:
                    json.dump(self.metadata, f, indent=2, ensure_ascii=False)

                if self.embedder is not None:
                    for ime, entry in cache.items():
                        if entry["emb"] is None and entry["chunks"]:
                            self._report(f"Updating embeddings: {ime}")
                            entry["emb"] = self._np.asarray(
                                self.embedder.encode(
                                    entry["chunks"],
                                    normalize_embeddings=True,
                                    show_progress_bar=False,
                                ),
                                dtype="float32",
                            )

                # Save cache before building vector index
                with open(self.cache_path, "wb") as f:
                    pickle.dump(cache, f)

                # ── PHASE: build index ──────────────────
                phase = "build_index"
                all_chunks = []
                all_emb = []
                for ime in sorted(cache.keys()):
                    entry = cache[ime]
                    all_chunks.extend(entry["chunks"])
                    if entry["emb"] is not None:
                        all_emb.append(entry["emb"])

                sve_emb = bool(cache) and all(
                    entry["emb"] is not None or not entry["chunks"]
                    for entry in cache.values()
                )
                self.chunks = all_chunks

                if self.chunks and self.embedder is not None and sve_emb:
                    self._report("Building vector index...")
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

                print(
                    f"  [{time.time()-t0:7.2f}s] RAG index updated: "
                    f"{len(self.chunks)} chunks, {len(self.metadata)} files",
                    flush=True,
                )
                work_complete = True

            except KeyboardInterrupt:
                print(
                    f"\n  [interrupt] caught during phase='{phase}'",
                    flush=True,
                )
                raise
            finally:
                if scan_complete and not work_complete:
                    self._save_partial_progress(cache, hashevi)
                elif not scan_complete:
                    print(
                        "  [interrupt] cannot save - scan phase incomplete",
                        flush=True,
                    )

        def _report(self, msg: str):
            elapsed = time.time() - t0

            # Per-file progress
            if msg.startswith("Processing:"):
                filename = msg.split(":", 1)[1].strip()
                self._bar.update(filename)
                return

            # Fixed-line messages - close bar, print as-is
            self._bar.close()
            print(f"  [{elapsed:7.2f}s] {msg}", flush=True)

    m0 = time.time()
    print("  Creating DocumentIndexer...")
    idx = ProgressIndexer(docs_folder=docs, index_folder=index)
    print(f"  [ok] DocumentIndexer created in {time.time()-m0:.2f}s")

    step("STEP 7: Indexer state")

    for k, v in idx.get_stats().items():
        print(f"  {k}: {v}")

    step("STEP 8: Files preview")

    files = idx._popisi_podrzane_datoteke()
    print(f"  Found {len(files)} files (showing first 10)")
    for f in files[:10]:
        rel = safe_relative(f, docs)
        try:
            size = f.stat().st_size
        except OSError:
            size = -1
        print(f"    {rel} ({size} bytes)")
    if len(files) > 10:
        print(f"    ... and {len(files) - 10} more")

    step("STEP 9: Manual search test")

    if idx.chunks and idx.index is not None:
        m0 = time.time()
        results = idx.search("test", k=3)
        print(f"  search took {time.time()-m0:.2f}s, returned {len(results)} results")
        for i, r in enumerate(results[:2], 1):
            preview_text = r[:120].replace("\n", " ")
            print(f"    [{i}] {preview_text}...")
    else:
        print("  [warn] index is empty - no chunks to search")

    step(f"DONE - total time: {time.time()-t0:.2f}s")

except KeyboardInterrupt:
    elapsed = time.time() - t0
    print(f"\n\n>>> [{elapsed:7.2f}s] [INTERRUPTED] user pressed Ctrl+C", flush=True)
    print("    Re-run to resume from where it stopped", flush=True)
    sys.exit(130)
