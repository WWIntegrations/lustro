"""Documents and chunks for RAG pipelines, from any dataset with Markdown or text.

    from ww_scraper.loaders import DatasetLoader
    docs = DatasetLoader("<dataset-id>").load()                  # one document per page
    chunks = DatasetLoader("<dataset-id>", chunk_size=1500).load()  # one per chunk

Each document has `page_content` and `metadata` (url, title, headingPath, chunkIndex, ...),
the shape LangChain and most vector stores take. When `langchain_core` is installed the
loader is a real `BaseLoader` and returns `langchain_core.documents.Document`; nothing
here depends on it. `to_llama_index()` converts to LlamaIndex documents when that is
installed.

`chunk_markdown()` splits on Markdown headings first (each chunk knows the headings it
sits under), then on paragraphs, then on hard length, with a character overlap between
neighbouring pieces of one section. Fenced code blocks are never split at a "#" line.
The same chunks are exported with `ww-scraper export <dataset> --format chunks`.
"""
from __future__ import annotations

import hashlib
import re

DEFAULT_CHUNK_SIZE = 2000
DEFAULT_OVERLAP = 200
TEXT_FIELDS = ("markdown", "text", "content", "description")
META_FIELDS = ("url", "finalUrl", "title", "description", "lang", "canonical", "crawledAt")

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

try:                                   # optional: real LangChain types when available
    from langchain_core.documents import Document as _LCDocument
    from langchain_core.document_loaders import BaseLoader as _LCBaseLoader
except Exception:                      # noqa: BLE001 - any import problem means "not installed"
    _LCDocument = None
    _LCBaseLoader = object


class Document:
    """page_content + metadata, the same fields as a LangChain Document."""

    __slots__ = ("page_content", "metadata")

    def __init__(self, page_content: str, metadata: dict = None):
        self.page_content = page_content
        self.metadata = dict(metadata or {})

    def __repr__(self):
        return f"Document(page_content={self.page_content[:40]!r}..., metadata={self.metadata!r})"

    def __eq__(self, other):
        return (isinstance(other, Document) and self.page_content == other.page_content
                and self.metadata == other.metadata)


def _sections(markdown: str):
    """[(heading_path, text)] in order; text keeps its heading line."""
    path: list = []
    out = []
    buf: list = []
    in_fence = False

    def flush():
        text = "\n".join(buf).strip()
        if text:
            out.append((list(path), text))
        buf.clear()

    for line in (markdown or "").splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            del path[level - 1:]
            path.extend([""] * (level - 1 - len(path)))     # a skipped level stays empty
            path.append(m.group(2).strip())
        buf.append(line)
    flush()
    return [(p and [h for h in p if h], t) for p, t in out]


def _split(text: str, size: int, overlap: int) -> list:
    """Pieces of at most `size` characters, cut at paragraph, line or word boundaries."""
    if len(text) <= size:
        return [text]
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", " "):
                cut = window.rfind(sep)
                if cut > size // 2:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        # do not begin the next piece mid-word
        if overlap and start > 0 and not text[start - 1].isspace():
            nxt = text.find(" ", start, end)
            if nxt != -1:
                start = nxt + 1
    return pieces


def chunk_markdown(markdown: str, chunk_size: int = DEFAULT_CHUNK_SIZE,
                   overlap: int = DEFAULT_OVERLAP) -> list:
    """[{"text", "headingPath", "chunkIndex"}]. Small neighbouring sections are joined
    when they share the same top heading and fit together within chunk_size."""
    if chunk_size < 100:
        raise ValueError("chunk_size must be at least 100 characters")
    if not 0 <= overlap < chunk_size // 2:
        raise ValueError("overlap must be >= 0 and less than half of chunk_size")
    chunks: list = []
    for path, text in _sections(markdown):
        last = chunks[-1] if chunks else None
        if (last and last["headingPath"][:1] == path[:1] and len(path) > len(last["headingPath"])
                and len(last["text"]) + 2 + len(text) <= chunk_size):
            last["text"] += "\n\n" + text             # a short subsection joins its parent
            continue
        for piece in _split(text, chunk_size, overlap):
            chunks.append({"text": piece, "headingPath": path})
    for i, c in enumerate(chunks):
        c["chunkIndex"] = i
    return chunks


def _text_of(item: dict, text_field=None) -> str:
    if text_field:
        v = item.get(text_field)
        return v if isinstance(v, str) else ""
    for f in TEXT_FIELDS:
        if isinstance(item.get(f), str) and item[f].strip():
            return item[f]
    return ""


def chunk_items(items, chunk_size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP,
                text_field: str = None):
    """Yield one chunk record per chunk of each item's Markdown (or text):
    {id, url, title, headingPath, chunkIndex, chunkCount, text, charCount, ...metadata}.
    `id` is stable for the same URL and text, so re-exports upsert instead of duplicating."""
    for item in items:
        text = _text_of(item, text_field)
        if not text.strip():
            continue
        parts = chunk_markdown(text, chunk_size, overlap)
        url = item.get("url") or item.get("finalUrl") or ""
        meta = {k: item[k] for k in META_FIELDS if item.get(k) not in (None, "")}
        for c in parts:
            digest = hashlib.sha256(f"{url}\0{c['chunkIndex']}\0{c['text']}".encode("utf-8")).hexdigest()
            yield {"id": digest[:24], "url": url, "title": item.get("title") or "",
                   **{k: v for k, v in meta.items() if k not in ("url", "title")},
                   "headingPath": c["headingPath"], "chunkIndex": c["chunkIndex"],
                   "chunkCount": len(parts), "text": c["text"], "charCount": len(c["text"])}


def _make(content, metadata):
    if _LCDocument is not None:
        return _LCDocument(page_content=content, metadata=metadata)
    return Document(content, metadata)


def items_to_documents(items, *, chunk_size: int = None, overlap: int = DEFAULT_OVERLAP,
                       text_field: str = None) -> list:
    """Documents from dataset items: one per item, or one per chunk when chunk_size is set."""
    docs = []
    if chunk_size:
        for c in chunk_items(items, chunk_size, overlap, text_field):
            text = c.pop("text")
            c.pop("charCount", None)
            docs.append(_make(text, c))
        return docs
    for item in items:
        text = _text_of(item, text_field)
        if not text.strip():
            continue
        meta = {k: item[k] for k in META_FIELDS if item.get(k) not in (None, "")}
        docs.append(_make(text, meta))
    return docs


class DatasetLoader(_LCBaseLoader):
    """Load a dataset (by id or name, from a storage directory) as documents.

    Works without LangChain; with `langchain_core` installed it is a BaseLoader, so it
    plugs into any LangChain pipeline (`loader.load()`, `loader.lazy_load()`)."""

    def __init__(self, dataset: str, storage=None, *, chunk_size: int = None,
                 overlap: int = DEFAULT_OVERLAP, text_field: str = None):
        from .storage import Storage
        self.storage = storage if hasattr(storage, "dataset") else Storage(storage)
        self.dataset = dataset
        self.chunk_size, self.overlap, self.text_field = chunk_size, overlap, text_field

    def _items(self):
        from .storage import is_run_id, valid_id
        st = self.storage
        if valid_id(self.dataset) and (st.root / "datasets" / self.dataset).is_dir():
            return st.dataset(self.dataset).iter_items()
        if not is_run_id(self.dataset):
            return st.named_dataset(self.dataset).iter_items()
        raise ValueError(f"dataset not found: {self.dataset}")

    def lazy_load(self):
        if self.chunk_size:
            for c in chunk_items(self._items(), self.chunk_size, self.overlap, self.text_field):
                text = c.pop("text")
                c.pop("charCount", None)
                yield _make(text, c)
            return
        for item in self._items():
            yield from items_to_documents([item], text_field=self.text_field)

    def load(self) -> list:
        return list(self.lazy_load())


def to_llama_index(docs) -> list:
    """Convert documents to `llama_index.core.Document` (needs llama-index installed)."""
    try:
        from llama_index.core import Document as LlamaDocument
    except ImportError as e:
        raise ImportError("to_llama_index() needs llama-index: pip install llama-index-core") from e
    return [LlamaDocument(text=d.page_content, metadata=dict(d.metadata)) for d in docs]
