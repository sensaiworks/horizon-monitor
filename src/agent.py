"""
Query agent — natural language Q&A over the accumulated RAG database.

Two modes:
  1. One-shot: `python main.py query "what did John say about the outage?"`
  2. Interactive REPL: `python main.py agent`

Uses Claude Sonnet (claude-sonnet-4-6) for answer quality.
Applies cache_control to the system prompt — it's stable across all queries
so the first call writes it to cache; every subsequent call reads it cheaply.
"""

from __future__ import annotations

from typing import Callable

import anthropic

from .rag import RAGPipeline

SYSTEM_PROMPT = """\
You are a personal assistant monitoring the user's remote desktop. You have access
to a log of chat conversations extracted from Microsoft Teams and Symphony running
in a Horizon remote desktop session.

Answer questions about these conversations accurately and concisely. When referencing
messages, include the speaker name and approximate time. If the information is not in
the provided context, say so — do not guess.
"""


class QueryAgent:
    def __init__(self, api_key: str, model: str, rag: RAGPipeline, max_tokens: int = 1024) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._rag = rag
        self._max_tokens = max_tokens

    def query(
        self,
        question: str,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        """
        Retrieve RAG context, stream an answer, return full response text.
        If on_chunk is provided, each streamed token is passed to it instead
        of being printed to stdout (used by the tray Ask dialog).
        """
        results = self._rag.query(question)
        context = self._rag.format_context(results)

        if context:
            user_content = f"Context (extracted chat messages):\n{context}\n\nQuestion: {question}"
        else:
            user_content = f"Question: {question}\n\n(No relevant chat messages found in the database yet.)"

        full_text = ""
        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            for text in stream.text_stream:
                if on_chunk:
                    on_chunk(text)
                else:
                    print(text, end="", flush=True)
                full_text += text
        if not on_chunk:
            print()
        return full_text

    def repl(self) -> None:
        """Interactive REPL loop. Type 'exit' or Ctrl+C to quit."""
        count = self._rag._collection.count() if self._rag._collection else 0
        print(f"horizon-monitor agent — {count} events in database. Type 'exit' to quit.\n")
        while True:
            try:
                question = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                break
            if not question:
                continue
            if question.lower() in ("exit", "quit", "bye"):
                print("Goodbye.")
                break
            print("Agent: ", end="", flush=True)
            self.query(question)
            print()
