"""
Query agent — natural language Q&A over the accumulated RAG database.

Two modes:
  1. One-shot: `python main.py query "what did John say about the outage?"`
  2. Interactive REPL: `python main.py agent`

Uses Claude Sonnet (claude-sonnet-4-6) for answer quality.
Uses prompt caching (cache_control) on the system prompt — as conversations
accumulate in RAG the context grows; caching saves cost.

See Anthropic docs on cache_control:
  https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching

TODO (Step 5): implement `query()` and `repl()`.
"""

from __future__ import annotations

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
    def __init__(self, api_key: str, model: str, rag: RAGPipeline) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._rag = rag

    def query(self, question: str) -> str:
        """
        One-shot query. Retrieve context from RAG, answer with Claude Sonnet.
        Streams to stdout and returns full response text.

        TODO (Step 5):
          1. results = self._rag.query(question)
          2. context = self._rag.format_context(results)
          3. Build messages: [{role: user, content: f"Context:\n{context}\n\nQuestion: {question}"}]
          4. Use self._client.messages.create with stream=True
          5. Print streamed chunks, accumulate and return full text
          6. Add cache_control to system prompt for cost savings
        """
        raise NotImplementedError

    def repl(self) -> None:
        """
        Interactive REPL loop. Type 'exit' or Ctrl+C to quit.

        TODO (Step 6):
          - Print welcome message
          - Loop: input() → self.query() → print
          - Handle KeyboardInterrupt gracefully
        """
        raise NotImplementedError
