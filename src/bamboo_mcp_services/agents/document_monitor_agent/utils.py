"""Utilities for the document monitor agent.

This module provides text extraction for common document formats,
chunking, deterministic id generation, and a small JSON checkpoint
store used to avoid re-processing files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import List, Dict

from pdfminer.high_level import extract_text as pdf_extract_text
import docx

LOG = logging.getLogger(__name__)


def extract_text_from_file(path: str) -> str:
    """Extract text content from a file.

    Supported extensions: .pdf, .docx, .txt, .md. For unknown extensions
    will attempt a best-effort text decode.

    For `.pdf` files, extraction relies on pdfminer.six and requires the PDF
    to have an embedded text layer.  Scanned PDFs without OCR will return an
    empty string; a ``DEBUG`` log line is emitted so the file is skipped
    rather than silently ingested as empty.  Run ``ocrmypdf <file> <file>``
    to add a text layer before dropping scanned PDFs into the watch directory.

    Args:
        path: Path to the file.

    Returns:
        Extracted text. Empty string on failure or if no text found.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix in (".txt", ".md"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
            return strip_sphinx_index(text)
        except Exception:
            return ""
    # Fallback: try reading bytes and decode
    try:
        b = Path(path).read_bytes()
        text = b.decode("utf-8", errors="ignore")
        return strip_sphinx_index(text)
    except Exception:
        return ""


def summarise_chunks(
    path: str,
    chunks: List[str],
    *,
    preview_chars: int = 120,
) -> str:
    """Return a human-readable quality summary of the chunks produced from *path*.

    Called after a PDF (or any document) is chunked so the operator can verify
    in the log that the extraction produced sensible text rather than garbled
    or empty content.

    The summary includes:
    - Total chunk count and total character count.
    - Min / median / max chunk lengths.
    - A short preview of the first chunk's leading text.

    Args:
        path: Source file path (used only for the log label).
        chunks: List of text chunks produced by :func:`chunk_text`.
        preview_chars: Number of characters to show from the first chunk.

    Returns:
        A single-line string suitable for ``LOG.info()``.  Returns an empty
        string when *chunks* is empty (caller should not log it).
    """
    if not chunks:
        return ""

    lengths = sorted(len(c) for c in chunks)
    n = len(lengths)
    total_chars = sum(lengths)
    min_len = lengths[0]
    max_len = lengths[-1]
    median_len = lengths[n // 2]

    first_preview = chunks[0][:preview_chars].replace("\n", " ").strip()
    if len(chunks[0]) > preview_chars:
        first_preview += "…"

    filename = Path(path).name
    return (
        f"chunk quality [{filename}]: "
        f"{n} chunk(s), {total_chars} chars total — "
        f"min={min_len} median={median_len} max={max_len} — "
        f'first: "{first_preview}"'
    )


def _extract_pdf(path: str) -> str:
    """Extract text from PDF using pdfminer.six.

    Returns an empty string for scanned PDFs that have no embedded text layer;
    a ``DEBUG`` log line is emitted so the caller's skip logic produces a
    visible trace.  Operators should pre-process scanned PDFs with
    ``ocrmypdf <file> <file>`` before placing them in the watch directory.

    Args:
        path: Path to the PDF file.

    Returns:
        Extracted text or empty string on error or if no text layer present.
    """
    try:
        text = pdf_extract_text(path) or ""
        if not text.strip():
            LOG.debug(
                "PDF has no extractable text layer (scanned or image-only?): %s — "
                "skipping. Run `ocrmypdf %s %s` to add a text layer first.",
                path, path, path,
            )
        return text
    except Exception:
        return ""


def _extract_docx(path: str) -> str:
    """Extract text from DOCX using python-docx.

    Args:
        path: Path to the DOCX file.

    Returns:
        Extracted text or empty string on error.
    """
    try:
        doc = docx.Document(path)
        return "\n".join(p.text for p in doc.paragraphs)
    except Exception:
        return ""


def strip_sphinx_index(text: str, threshold: int = 5) -> str:
    """Remove Sphinx-generated index content from the end of a document.

    Sphinx HTML documentation converted to plain text often ends with an
    auto-generated index section containing entries of the form::

        SomeName (module.path attribute), 132
        __init__() (SomeClass method), 45

    These entries are useless for RAG retrieval and actively harmful because
    the page numbers (e.g. 132, 45) look like error code values to an LLM.

    This function scans the document line by line and truncates at the first
    position where ``threshold`` or more consecutive lines all look like
    Sphinx index entries.

    Args:
        text: Raw document text.
        threshold: Number of consecutive index lines that trigger truncation.
            Default is 5, which avoids false positives on normal prose that
            happens to contain one or two attribute references.

    Returns:
        str: Document text with the Sphinx index section removed.  If no
        index section is detected the original text is returned unchanged.
    """
    # Matches lines like: "SomeName (module.path attribute/method/class), 123"
    _index_line_re = re.compile(
        r"^\s*[\w\.\(\)\s]+\(["
        r"\w\.\s]+"
        r"(?:attribute|method|class|function|module|exception)"
        r"\)\s*,\s*\d+\s*$"
    )

    lines = text.splitlines(keepends=True)
    consecutive = 0
    run_start = 0

    for i, line in enumerate(lines):
        if _index_line_re.match(line):
            if consecutive == 0:
                run_start = i
            consecutive += 1
            if consecutive >= threshold:
                # Truncate just before the start of this index run
                return "".join(lines[:run_start])
        else:
            consecutive = 0

    return text


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    """Split text into overlapping chunks.

    Args:
        text: The input text.
        chunk_size: Maximum size of each chunk (characters).
        overlap: Number of characters to overlap between successive chunks.

    Returns:
        List of text chunks. Returns empty list if input is empty.
    """
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if overlap < 0:
        raise ValueError("overlap must be >= 0")
    chunks: List[str] = []
    start = 0
    L = len(text)
    while start < L:
        end = start + chunk_size
        chunks.append(text[start:end])
        # Move start forward but keep overlap
        start = end - overlap
        if start < 0:
            start = 0
    return chunks


def deterministic_chunk_id(file_path: str, content_hash: str, chunk_index: int) -> str:
    """Create a deterministic ID for a chunk using file path, content hash, and index.

    The returned ID is a SHA256 hex digest of the canonical input, prefixed
    with "doc:" to make stored IDs easily recognizable.

    Args:
        file_path: Original file path (string).
        content_hash: Hex digest of the whole file content (e.g., SHA256).
        chunk_index: Index of the chunk (0-based).

    Returns:
        Deterministic string ID suitable for vector DB primary key.
    """
    base = f"{file_path}|{content_hash}|{chunk_index}"
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return f"doc:{h}"


def content_hash(text: str) -> str:
    """Compute SHA256 hex digest of the provided text.

    Args:
        text: Input text.

    Returns:
        Hex string of SHA256(text).
    """
    h = hashlib.sha256()
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class CheckpointStore:
    """JSON-backed checkpoint store that records processed files and metadata.

    This lightweight store maps absolute file paths to metadata such as
    content hash and timestamp. It is intentionally minimal so it is easy
    to swap out for DuckDB or another store later.

    Attributes:
        path: Path to the JSON checkpoint file.
        _data: Internal dict containing checkpoint data.
    """

    def __init__(self, path: str) -> None:
        """Initialize the checkpoint store.

        Args:
            path: Filesystem path where the checkpoint JSON will be saved.
        """
        self.path = Path(path)
        self._data: Dict[str, Dict] = {"processed": {}}
        self._load()

    def _load(self) -> None:
        """Load checkpoint file if it exists; otherwise start fresh."""
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {"processed": {}}

    def save(self) -> None:
        """Persist the checkpoint store to disk (atomic-ish)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def mark_processed(self, filename: str, snapshot: Dict) -> None:
        """Mark a file as processed.

        Args:
            filename: Absolute filename.
            snapshot: Metadata dict to store (e.g., content_hash, processed_ts).
        """
        self._data.setdefault("processed", {})[filename] = snapshot
        self.save()

    def is_processed(self, filename: str, content_hash_str: str) -> bool:
        """Check whether a file has been processed with a given content hash.

        If the file exists in the store and the stored content_hash matches
        the provided hash, returns True.

        Args:
            filename: Absolute filename.
            content_hash_str: SHA256 hash of the file content.

        Returns:
            True if processed and hashes match, False otherwise.
        """
        meta = self._data.get("processed", {}).get(filename)
        if not meta:
            return False
        return meta.get("content_hash") == content_hash_str
