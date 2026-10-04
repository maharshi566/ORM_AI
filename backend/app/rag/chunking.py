"""Heading-aware chunking.

A chunk is the unit that gets embedded, searched and cited. The chunker:

1. Splits a Markdown document on its headings. Each ``##`` heading starts a section,
   and a ``###`` heading below it starts a sub-section named ``"Parent › Child"``.
   The section name becomes the citation, e.g. ``[POL-CREDIT-001 v2 §2. Credit limits]``.
   PDFs are split by page (``§Page 3``); plain text has one section, ``Text``.
2. Keeps a section whole when it fits in ``max_tokens`` (600 by default, which
   every section of the current knowledge base does).
3. Splits a longer section at paragraph boundaries. A table is split between rows,
   with its header repeated, and a very long paragraph between sentences. Each
   piece after the first starts with the last ~``overlap_tokens`` of the previous
   one (about 13%), so a sentence cut at a boundary is still findable.

Every chunk carries its document's metadata: document_id, title, source, category,
section, page, version, effective_date, status, shop_id and trust.

A chunk's ID is a SHA-256 hash of its text and metadata. Re-running ingestion
therefore skips unchanged chunks, and any edit (even to metadata such as
``status``) produces a new ID, so the old vector is replaced.
"""

import hashlib
import re
from dataclasses import dataclass

from app.rag.loaders import DocumentMetadata, LoadedDocument
from app.rag.text import estimate_tokens

# Bump when the chunking output changes shape, to force every chunk to re-embed.
CHUNKER_VERSION = "1"
DEFAULT_MAX_TOKENS = 600
DEFAULT_OVERLAP_TOKENS = 80
INTRO_SECTION = "Introduction"

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BLANK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def format_citation(document_id: str, version: int, section: str) -> str:
    """``[POL-CREDIT-001 v2 §2. Credit limits]``: what answers quote as their source."""
    return f"[{document_id} v{version} §{section}]"


@dataclass(frozen=True)
class Chunk:
    id: str
    document_key: str  # POL-CREDIT-001@v2
    chunk_index: int  # position within the document, from 0
    section: str
    page: int | None
    content: str  # the section text (or part of it)
    token_count: int
    metadata: DocumentMetadata
    path: str  # file path inside the knowledge base

    @property
    def citation(self) -> str:
        return format_citation(self.metadata.document_id, self.metadata.version, self.section)

    @property
    def embedding_text(self) -> str:
        """What gets embedded: the title and section in front of the text.

        Short sections such as "## 3. Payment terms" make sense only with their
        document's title, so the header is part of what the search compares.
        """
        return f"{self.metadata.title}\n{self.section}\n\n{self.content}"

    def store_metadata(self) -> dict[str, str | int]:
        """Flat metadata for ChromaDB, which accepts only str, int, float and bool.

        Missing values are stored as "" or 0 because ChromaDB rejects None. The date
        is also stored as a number (20260401) so it can be compared with $lte.
        """
        meta = self.metadata
        return {
            "document_id": meta.document_id,
            "document_key": meta.key,
            "title": meta.title,
            "source": meta.source,
            "category": meta.category,
            "version": meta.version,
            "status": meta.status,
            "effective_date": meta.effective_date.isoformat(),
            "effective_date_int": int(meta.effective_date.strftime("%Y%m%d")),
            "shop_id": meta.shop_id or "",
            "trust": meta.trust,
            "section": self.section,
            "page": self.page or 0,
            "chunk_index": self.chunk_index,
            "token_count": self.token_count,
            "path": self.path,
        }


@dataclass(frozen=True)
class Section:
    name: str
    text: str
    page: int | None = None


# ----------------------------------------------------------------- sections


def split_sections(doc: LoadedDocument) -> list[Section]:
    """Cut a document into named sections (see the module docstring)."""
    if doc.format == "pdf":
        return [Section(f"Page {page.number}", page.text, page.number) for page in doc.pages]
    if doc.format == "text":
        return [Section("Text", doc.text)]

    sections: list[Section] = []
    name, parent = INTRO_SECTION, None
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(Section(name, text))

    for line in doc.text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading is None:
            lines.append(line)
            continue
        level, title = len(heading.group(1)), heading.group(2).strip()
        if level == 1:
            continue  # the document title; every chunk's header carries it already
        flush()
        lines = []
        if level == 2:
            name = parent = title
        else:
            name = f"{parent} › {title}" if parent else title
    flush()
    return sections


# ------------------------------------------------------------ size splitting


def _pack(units: list[str], max_tokens: int, joiner: str, prefix: str = "") -> list[str]:
    """Greedily join units into pieces of at most ``max_tokens`` (a unit that is
    bigger on its own becomes its own piece)."""
    pieces: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = prefix + joiner.join([*current, unit])
        if current and estimate_tokens(candidate) > max_tokens:
            pieces.append(prefix + joiner.join(current))
            current = [unit]
        else:
            current.append(unit)
    if current:
        pieces.append(prefix + joiner.join(current))
    return pieces


def _split_block(block: str, max_tokens: int) -> list[str]:
    """Split one paragraph, list or table that is too big on its own."""
    if estimate_tokens(block) <= max_tokens:
        return [block]
    lines = block.split("\n")
    if len(lines) > 2 and all(line.lstrip().startswith("|") for line in lines):
        header = "\n".join(lines[:2]) + "\n"  # header row + separator row
        return _pack(lines[2:], max_tokens, "\n", prefix=header)
    if len(lines) > 1:
        return [
            piece
            for packed in _pack(lines, max_tokens, "\n")
            for piece in _split_block(packed, max_tokens)
        ]
    sentences = _SENTENCE_END.split(block)
    if len(sentences) > 1:
        return [
            piece
            for packed in _pack(sentences, max_tokens, " ")
            for piece in _split_block(packed, max_tokens)
        ]
    return _pack(block.split(" "), max_tokens, " ")  # one enormous sentence: by words


def _tail(text: str, overlap_tokens: int) -> str:
    """The last ~``overlap_tokens`` of ``text``, starting at a word boundary."""
    if overlap_tokens <= 0:
        return ""
    words = text.split()
    taken: list[str] = []
    size = 0
    for word in reversed(words):
        if size + len(word) + 1 > overlap_tokens * 4 and taken:
            break
        taken.append(word)
        size += len(word) + 1
    return "… " + " ".join(reversed(taken))


def split_text(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a section's text into pieces of at most ``max_tokens``, with overlap."""
    if estimate_tokens(text) <= max_tokens:
        return [text]
    pieces = [
        piece for block in _BLANK.split(text) if block.strip()
        for piece in _split_block(block.strip(), max_tokens)
    ]  # fmt: skip
    chunks: list[str] = []
    current: list[str] = []
    for piece in pieces:
        if current and estimate_tokens("\n\n".join([*current, piece])) > max_tokens:
            chunks.append("\n\n".join(current))
            tail = _tail(chunks[-1], overlap_tokens)
            fits = estimate_tokens(f"{tail}\n\n{piece}") <= max_tokens
            current = [tail, piece] if tail and fits else [piece]
        else:
            current.append(piece)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


# ------------------------------------------------------------------- chunks


def _chunk_id(doc: LoadedDocument, section: Section, content: str, occurrence: int) -> str:
    meta = doc.metadata
    parts = [
        CHUNKER_VERSION,
        meta.key,
        meta.title,
        meta.source,
        meta.category,
        meta.status,
        meta.effective_date.isoformat(),
        meta.shop_id or "",
        meta.trust,
        doc.path,
        section.name,
        str(section.page or ""),
        content,
        str(occurrence),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def chunk_document(
    doc: LoadedDocument,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    if max_tokens < 50:
        raise ValueError("max_tokens must be at least 50")
    if not 0 <= overlap_tokens < max_tokens // 2:
        raise ValueError("overlap_tokens must be between 0 and half of max_tokens")

    chunks: list[Chunk] = []
    seen: dict[tuple[str, str], int] = {}
    for section in split_sections(doc):
        for content in split_text(section.text, max_tokens, overlap_tokens):
            occurrence = seen.get((section.name, content), 0)
            seen[(section.name, content)] = occurrence + 1
            chunks.append(
                Chunk(
                    id=_chunk_id(doc, section, content, occurrence),
                    document_key=doc.metadata.key,
                    chunk_index=len(chunks),
                    section=section.name,
                    page=section.page,
                    content=content,
                    token_count=estimate_tokens(content),
                    metadata=doc.metadata,
                    path=doc.path,
                )
            )
    return chunks


def chunk_documents(
    docs: list[LoadedDocument],
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    return [
        chunk
        for doc in docs
        for chunk in chunk_document(doc, max_tokens=max_tokens, overlap_tokens=overlap_tokens)
    ]
