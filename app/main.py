"""Scheme Sahayak — Phase 4: full voice-in -> RAG retrieval -> grounded answer -> voice-out.

Sarvam STT (saaras:v3) -> RAGLLMService (Phase 3's app/rag.py: FAISS retrieval + confidence
threshold + grounded Sarvam LLM generation) -> Sarvam TTS (bulbul:v3), single language per
run, selected via --lang.
"""

import argparse
import asyncio
import os

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIProcessor
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from pipecat_tail.observer import TailObserver

from echo_filter import EchoSuppressedTranscriptFilter
from rag_llm_service import RAGLLMService
from turn_strategies import EchoSafeVADStartStrategy

LANGUAGES = {
    "hi": {"language": Language.HI_IN, "voice": "shubh"},
    "ml": {"language": Language.ML_IN, "voice": "shubh"},
}


async def run(lang: str) -> None:
    load_dotenv(override=True)
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY not set - copy .env.example to .env and fill it in")

    lang_cfg = LANGUAGES[lang]

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    stt = SarvamSTTService(
        api_key=api_key,
        settings=SarvamSTTService.Settings(
            model="saaras:v3",
            language=lang_cfg["language"],
        ),
    )

    # Drops leaked-echo transcription frames before they reach the aggregator's buffer -
    # see app/echo_filter.py docstring for the exact bug this closes (buffer contamination
    # across turns, found via real testing: wrong-scheme retrieval + empty LLM responses
    # both traced back to this).
    echo_filter = EchoSuppressedTranscriptFilter()

    llm = RAGLLMService(lang=lang)

    tts = SarvamTTSService(
        api_key=api_key,
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice=lang_cfg["voice"],
            language=lang_cfg["language"],
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            # Defaults (confidence=0.7, start_secs=0.2, min_volume=0.6) were triggering
            # some false "user started speaking" events. Originally pushed much higher
            # (0.9/0.4/0.75) while trying to also suppress the TTS feedback loop below -
            # that was overcorrecting: VAD became so strict it stopped firing for real
            # speech too (confirmed - STT was transcribing genuine questions but no turn
            # ever started). Now that the feedback loop's actual cause (the transcription
            # fallback strategy, see below) is removed, VAD only needs to reliably catch
            # real speech - dialed back to a modest bump over defaults.
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(confidence=0.75, start_secs=0.25, min_volume=0.65)
            ),
            # Default turn-stop timeout (stop_secs=3) was cutting sentences off mid-word
            # if the user paused 3+ seconds while thinking, even though the turn-detection
            # model itself judged the turn incomplete - raised to 5s to allow natural pauses.
            #
            # Default start strategies are [VADUserTurnStartStrategy,
            # TranscriptionUserTurnStartStrategy]. The transcription-based one exists as a
            # fallback for soft speech, but it fires whenever STT produces any text *while
            # the bot is speaking*, with no volume/confidence gate at all - confirmed via
            # real testing that it was creating a literal feedback loop: leaked bot TTS
            # audio still got transcribed by Sarvam STT, which restarted a "user turn" on
            # the bot's own words, got a not-certain reply, which leaked and got
            # transcribed again, on repeat. Dropped it.
            #
            # Even VAD-only wasn't fully clean (Hindi specifically - confirmed via real
            # testing that switching TTS voice, VAD sensitivity, and test order all didn't
            # change it: real audio leak, content-matched to the bot's own speech). Root
            # cause: LocalAudioTransport has no acoustic echo cancellation. Real fix:
            # EchoSafeVADStartStrategy (app/turn_strategies.py) - suppresses VAD-triggered
            # turn starts while the bot is speaking + a short cooldown after, using
            # Pipecat's own turn-strategy subclassing extension point (not custom
            # low-level audio/turn-detection code - see that file's docstring).
            user_turn_strategies=UserTurnStrategies(
                start=[EchoSafeVADStartStrategy()],
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3(
                            params=SmartTurnParams(stop_secs=5)
                        )
                    )
                ],
            ),
        ),
    )

    # TailObserver is itself an RTVIObserver subclass, so PipelineWorker's auto-created
    # RTVIProcessor (its normal default, enable_rtvi=True) gets skipped once we pass our
    # own observer - it requires the RTVIProcessor to be explicit too, or it errors
    # ("RTVIObserver found in observers but no RTVIProcessor in pipeline"). Confirmed via
    # source, not guessed.
    rtvi = RTVIProcessor()

    pipeline = Pipeline(
        [
            rtvi,
            transport.input(),
            stt,
            echo_filter,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    # Phase 6: observability. enable_metrics=True (since Phase 1) already produces the
    # per-service TTFB/TTFA latency numbers seen throughout every test log - TailObserver
    # adds a live way to view them (plus logs/conversation/audio levels) instead of
    # scrolling raw DEBUG output. Run `pipecat tail` in a second terminal to watch it.
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        observers=[TailObserver()],
    )
    runner = WorkerRunner(handle_sigint=True)
    await runner.add_workers(worker)

    logger.info(f"Listening for {lang_cfg['language'].value} speech — speak into your mic")
    await runner.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scheme Sahayak — voice + RAG loop")
    parser.add_argument("--lang", choices=["hi", "ml"], required=True)
    args = parser.parse_args()
    asyncio.run(run(args.lang))
