"""RAG over the writer's documents and reference materials."""

import hashlib
import re
from pathlib import Path

import chromadb

CHROMA_PATH = Path(__file__).resolve().parents[1] / "chroma_data"
RAG_ERRORS = (chromadb.errors.ChromaError, OSError, ValueError, RuntimeError)
_client = None


def _get_client():
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    return _client


def _collection(doc_id: int):
    return _get_client().get_or_create_collection(f"doc_{doc_id}")


def _chunk(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    step = size - overlap
    return [
        text[i : i + size]
        for i in range(0, len(text), step)
        if text[i : i + size].strip()
    ]


def _get_ids_by_source(collection, source: str) -> list[str]:
    entries = collection.get(where={"source": source}, include=["metadatas"])
    return entries["ids"]


def index_document(doc_id: int, text: str) -> int:
    collection = _collection(doc_id)
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    metadata = dict(collection.metadata or {})
    if metadata.get("document_hash") == content_hash:
        return len(_get_ids_by_source(collection, "document"))

    existing = collection.get(include=["metadatas"])
    obsolete_ids = [
        chunk_id
        for chunk_id, chunk_metadata in zip(existing["ids"], existing["metadatas"])
        if not chunk_metadata or chunk_metadata.get("source") == "document"
    ]
    if obsolete_ids:
        collection.delete(ids=obsolete_ids)

    chunks = _chunk(text)
    if chunks:
        collection.add(
            ids=[f"document-{index}" for index in range(len(chunks))],
            documents=chunks,
            metadatas=[
                {"source": "document", "content_hash": content_hash} for _ in chunks
            ],
        )

    metadata["document_hash"] = content_hash
    collection.modify(metadata=metadata)
    return len(chunks)


def index_reference(doc_id: int, reference_id: int, text: str) -> int:
    collection = _collection(doc_id)
    chunks = _chunk(text)
    if not chunks:
        return 0
    collection.add(
        ids=[f"reference-{reference_id}-{index}" for index in range(len(chunks))],
        documents=chunks,
        metadatas=[
            {
                "source": "reference",
                "reference_id": reference_id,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            for _ in chunks
        ],
    )
    return len(chunks)


def delete_reference(doc_id: int, reference_id: int) -> None:
    collection = _collection(doc_id)
    entries = collection.get(
        where={"reference_id": reference_id},
        include=["metadatas"],
    )
    if entries["ids"]:
        collection.delete(ids=entries["ids"])


def delete_document(doc_id: int) -> None:
    try:
        _get_client().delete_collection(f"doc_{doc_id}")
    except chromadb.errors.NotFoundError:
        return


def _is_cursor_copy(chunk: str, text_before_cursor: str) -> bool:
    normalize = lambda text: re.sub(r"\s+", " ", text).strip().casefold()
    candidate = normalize(chunk)
    cursor_text = normalize(text_before_cursor)
    return bool(
        candidate
        and cursor_text
        and (candidate == cursor_text or cursor_text.endswith(candidate) or candidate.endswith(cursor_text))
    )


def retrieve(
    doc_id: int,
    query: str,
    k: int = 3,
    text_before_cursor: str = "",
) -> list[str]:
    collection = _collection(doc_id)
    count = collection.count()
    if count == 0 or k <= 0:
        return []
    result = collection.query(
        query_texts=[query],
        n_results=count,
        include=["documents"],
    )
    chunks = result["documents"][0]
    return [
        chunk
        for chunk in chunks
        if not _is_cursor_copy(chunk, text_before_cursor)
    ][:k]
