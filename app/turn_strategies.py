"""Custom turn-start strategy: suppress VAD-triggered turn starts while the bot is
speaking (plus a short cooldown after it stops).

Root cause this fixes: LocalAudioTransport has no acoustic echo cancellation (confirmed
via source - no such option exists on it). Real voice testing showed the bot's own TTS
output leaking through a wired headset enough for Sarvam STT to transcribe it, which
VAD-triggered "interruptions" then treated as genuine user speech, restarting a turn on
the bot's own words - a self-sustaining feedback loop (Hindi voices specifically; content
of the transcribed leak matched exactly what the bot was saying at that moment, ruling out
random STT hallucination as the cause).

This is a thin subclass of Pipecat's own VADUserTurnStartStrategy (the documented
extension point for customizing turn behavior - other real strategies like a
"WakePhraseUserTurnStartStrategy" follow the same pattern) - not custom low-level
audio/turn-detection logic. BotStartedSpeakingFrame/BotStoppedSpeakingFrame are pushed
both downstream and upstream by pipecat's transport output (verified in source), so they
reach this strategy's process_frame the same way VAD frames do.
"""

import time

from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame, Frame
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import VADUserTurnStartStrategy


class EchoSafeVADStartStrategy(VADUserTurnStartStrategy):
    def __init__(self, *, echo_cooldown_secs: float = 0.6, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False
        self._echo_cooldown_secs = echo_cooldown_secs
        self._cooldown_until = 0.0

    async def process_frame(self, frame: Frame) -> ProcessFrameResult | None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            return ProcessFrameResult.CONTINUE

        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._cooldown_until = time.monotonic() + self._echo_cooldown_secs
            return ProcessFrameResult.CONTINUE

        if self._bot_speaking or time.monotonic() < self._cooldown_until:
            # Swallow VAD frames entirely while the bot is speaking / just finished -
            # don't let leaked bot audio start a new user turn. Real speech after the
            # cooldown window is handled completely normally (super().process_frame).
            return ProcessFrameResult.CONTINUE

        return await super().process_frame(frame)
