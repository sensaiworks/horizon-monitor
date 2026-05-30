"""
RAG pipeline — ChromaDB ingest and semantic search.

Collection schema:
  - document : MessageEvent.message (the text to embed and search)
  - metadata  : speaker, app, timestamp (ISO), directed_at_user, window_title
  - id        : MessageEvent.doc_id() — hash of speaker+message for deduplication

Embedding providers (config.rag.embedding_provider):
  "voyage"  — voyageai.Client().embed(), model="voyage-3"  (needs VOYAGE_API_KEY)
  "local"   — sentence_transformers "all-MiniLM-L6-v2"  (offline, ~100 MB download)

Falls back to "local" automatically if "voyage" is selected but no API key is set.
ChromaDB persists to config.rag.db_path (./data/chromadb, gitignored).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import chromadb
from chromadb import EmbeddingFunction, Documents, Embeddings

from .models import MessageEvent


class _VoyageEF(EmbeddingFunction):
    def __init__(self, api_key: str) -> None:
        import voyageai
        self._client = voyageai.Client(api_key=api_key)

    def __call__(self, input: Documents) -> Embeddings:
        result = self._client.embed(list(input), model="voyage-3", input_type="document")
        return result.embeddings


class _LocalEF(EmbeddingFunction):
    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer("all-MiniLM-L6-v2")

    def __call__(self, input: Documents) -> Embeddings:
        return self._model.encode(list(input)).tolist()


class RAGPipeline:
    def __init__(
        self,
        db_path: str,
        collection_name: str,
        embedding_provider: Literal["voyage", "local"] = "voyage",
        voyage_api_key: str | None = None,
        top_k: int = 8,
    ) -> None:
        self._db_path = db_path
        self._collection_name = collection_name
        self._provider = embedding_provider
        self._voyage_key = voyage_api_key
        self._top_k = top_k
        self._client: chromadb.PersistentClient | None = None
        self._collection = None

    def connect(self) -> None:
        """Initialize ChromaDB client, embedding function, and collection."""
        Path(self._db_path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=self._db_path)

        if self._provider == "voyage" and self._voyage_key:
            try:
                ef = _VoyageEF(api_key=self._voyage_key)
            except Exception as exc:
                print(f"RAG: Voyage init failed ({exc}) — falling back to local embeddings", flush=True)
                ef = _LocalEF()
        else:
            if self._provider == "voyage":
                print("RAG: VOYAGE_API_KEY not set — falling back to local embeddings", flush=True)
            ef = _LocalEF()

        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )

    def count(self) -> int:
        """Number of documents currently stored in the collection."""
        if self._collection is None:
            return 0
        return self._collection.count()

    def ingest(self, events: list[MessageEvent]) -> int:
        """Add events to ChromaDB. Returns number of new documents added."""
        assert self._collection is not None, "call connect() first"
        if not events:
            return 0

        # Deduplicate within the batch before hitting ChromaDB
        seen: dict[str, MessageEvent] = {}
        for e in events:
            seen.setdefault(e.doc_id(), e)
        unique_ids = list(seen.keys())
        unique_events = list(seen.values())

        existing_ids = set(self._collection.get(ids=unique_ids)["ids"])
        new = [(e, i) for e, i in zip(unique_events, unique_ids) if i not in existing_ids]
        if not new:
            return 0

        self._collection.add(
            ids=[i for _, i in new],
            documents=[e.message for e, _ in new],
            metadatas=[
                {
                    "speaker": e.speaker,
                    "app": e.app,
                    "timestamp": e.timestamp.isoformat(),
                    "directed_at_user": e.directed_at_user,
                    "window_title": e.window_title,
                }
                for e, _ in new
            ],
        )
        return len(new)

    def query(self, text: str, filter_directed: bool = False) -> list[dict]:
        """Semantic search. Returns top-k results as dicts."""
        assert self._collection is not None, "call connect() first"
        count = self._collection.count()
        if count == 0:
            return []

        kwargs: dict = {
            "query_texts": [text],
            "n_results": min(self._top_k, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if filter_directed:
            kwargs["where"] = {"directed_at_user": True}

        results = self._collection.query(**kwargs)

        output = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            output.append({
                "document": doc,
                "speaker": meta["speaker"],
                "app": meta["app"],
                "timestamp": meta["timestamp"],
                "directed_at_user": meta["directed_at_user"],
                "distance": dist,
            })
        return output

    def format_context(self, results: list[dict]) -> str:
        """Format RAG results as context string for the query agent prompt."""
        lines = []
        for r in results:
            ts = r.get("timestamp", "")[:16]
            lines.append(f"[{ts}] {r['speaker']} ({r['app']}): {r['document']}")
        return "\n".join(lines)
