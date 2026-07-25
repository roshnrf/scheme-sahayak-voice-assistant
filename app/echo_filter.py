"""Frame filter: drop STT transcription frames while the bot is speaking (+ a short
cooldown after), so leaked TTS audio never reaches the user-message aggregation buffer.

Root cause this fixes: EchoSafeVADStartStrategy (turn_strategies.py) stops leaked bot
audio from *starting a new turn*, but Pipecat's LLMUserAggregator appends every
TranscriptionFrame's text to its pending buffer unconditionally, clearing it only when a
turn actually completes (confirmed via source:
pipecat/processors/aggregators/llm_response_universal.py, `_handle_transcription` appends
unconditionally, `reset()` only runs from `push_aggregation()` at turn-stop). With the
turn-start suppressed but transcription still flowing, leaked audio from one answer sits
in the buffer and gets prepended to the *next* real question's text - confirmed via real
testing: a wrong-scheme match and an empty LLM response were both traced to the buffer
containing the previous answer's echoed content concatenated with the real query.

Placed as a normal FrameProcessor between the STT service and the user aggregator in the
pipeline (not custom audio/turn-detection code - same Frame-filtering pattern already used
in EchoSafeVADStartStrategy, applied at the transcription-frame level instead of the
VAD-frame level). BotStartedSpeakingFrame/BotStoppedSpeakingFrame propagate upstream from
transport.output() back through the pipeline (verified in source, same fact already used
by EchoSafeVADStartStrategy), so this processor sees them the same way.
"""

import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class EchoSuppressedTranscriptFilter(FrameProcessor):
    def __init__(self, *, echo_cooldown_secs: float = 0.6, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self._echo_cooldown_secs = echo_cooldown_secs
        self._cooldown_until = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._cooldown_until = time.monotonic() + self._echo_cooldown_secs
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if self._bot_speaking or time.monotonic() < self._cooldown_until:
                # Drop it - leaked bot audio, never reaches the aggregation buffer.
                return

        await self.push_frame(frame, direction)
