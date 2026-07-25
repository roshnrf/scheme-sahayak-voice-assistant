"""Bridges app/rag.py (Phase 3, pipecat-free) into the Pipecat voice pipeline.

Implements the same frame contract every real Pipecat LLM service follows (verified
against pipecat.services.openai.base_llm.BaseOpenAILLMService.process_frame): on
LLMContextFrame, push LLMFullResponseStartFrame -> LLMTextFrame -> LLMFullResponseEndFrame.
Downstream (assistant aggregator, TTS) only cares about this frame shape, not which class
produced it, so nothing else in the pipeline needs to change.
"""

import asyncio

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

import rag


class RAGLLMService(LLMService):
    def __init__(self, *, lang: str, **kwargs):
        # None of these apply here - RAGLLMService bypasses the normal LLM settings
        # path entirely and calls app/rag.py's own Sarvam client directly. Every field
        # must be explicitly given (None for "unsupported") - pipecat's ServiceSettings
        # validates a store-mode settings object has no NOT_GIVEN fields left.
        settings = LLMSettings(
            model=None,
            system_instruction=None,
            temperature=None,
            max_tokens=None,
            top_p=None,
            top_k=None,
            frequency_penalty=None,
            presence_penalty=None,
            seed=None,
            filter_incomplete_user_turns=None,
            user_turn_completion_config=None,
        )
        super().__init__(settings=settings, **kwargs)
        self._lang = lang

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        query = ""
        for message in reversed(frame.context.messages):
            if message.get("role") == "user":
                content = message.get("content")
                query = content if isinstance(content, str) else str(content)
                break

        await self.push_frame(LLMFullResponseStartFrame())
        try:
            result = await asyncio.to_thread(rag.answer, query, self._lang)
            logger.info(
                f"RAG: grounded={result.grounded} confidence={result.confidence:.4f} "
                f"source={result.source_slug}"
            )
            await self.push_frame(LLMTextFrame(text=result.answer))
        finally:
            await self.push_frame(LLMFullResponseEndFrame())
