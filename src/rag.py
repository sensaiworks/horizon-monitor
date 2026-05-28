"""
RAG pipeline — ChromaDB ingest and semantic search.

Collection schema:
  - document : MessageEvent.message (the text to embed and search)
  - metadata  : speaker, app, timestamp (ISO), directed_at_user, window_title
  - id        : MessageEvent.doc_id() — hash of speaker+message for deduplication

Embedding providers (config.rag.embedding_provider):
  "voyage"  — voyageai.Client().embed(), model="voyage-3"  (recommended, needs VOYAGE_API_KEY)
  "local"   — sentence_transformers.SentenceTransformer("all-MiniLM-L6-v2") (offline)

ChromaDB persists to config.rag.db_path (./data/chromadb by default, gitignored).

TODO (Step 3): implement `ingest()` and `query()`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import chromadb
from chromadb.config import Settings

from .models import MessageEvent


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
        self._embedder = None

    def connect(self) -> None:
        """
        Initialize ChromaDB client and embedding function.

        TODO (Step 3):
          - chromadb.PersistentClient(path=self._db_path)
          - If provider=="voyage": use chromadb.utils.embedding_functions.VoyageEmbeddingFunction
          - If provider=="local": use chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction
          - get_or_create_collection with the embedding function
        """
        raise NotImplementedError

    def ingest(self, events: list[MessageEvent]) -> int:
        """
        Add events to ChromaDB. Returns number of new documents added (deduped).

        TODO (Step 3):
          - Skip events already in collection (check by doc_id)
          - collection.add(documents=[...], metadatas=[...], ids=[...])
        """
        raise NotImplementedError

    def query(self, text: str, filter_directed: bool = False) -> list[dict]:
        """
        Semantic search. Returns top-k results as dicts with keys:
          document, speaker, app, timestamp, directed_at_user

        filter_directed=True: only return messages directed at user.

        TODO (Step 3):
          - collection.query(query_texts=[text], n_results=self._top_k, where=...)
          - Flatten results and return list of dicts
        """
        raise NotImplementedError

    def format_context(self, results: list[dict]) -> str:
        """Format RAG results as context string for the query agent prompt."""
        lines = []
        for r in results:
            ts = r.get("timestamp", "")[:16]  # trim seconds
            lines.append(f"[{ts}] {r['speaker']} ({r['app']}): {r['document']}")
        return "\n".join(lines)
