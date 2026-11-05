"""Minimal Wyoming STT client that measures the latency the way Home Assistant experiences it.

Sequence (as the Assist pipeline sends it): Transcribe(language), AudioStart, AudioChunk x N,
AudioStop, then wait for Transcript. Latency is measured from just before AudioStop is written to
the arrival of the Transcript, i.e. what the server needs after the audio ended. The chunks are
sent without real-time pacing (the server only transcribes on AudioStop, so streaming pace does
not change this latency; with --vad-endpointing it does, see `realtime`).

Runs on the image's Python 3.10 with the `wyoming` package already installed there.
"""
import asyncio
import time
import wave

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.info import Describe, Info

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_SAMPLES = 1024


def read_pcm(path):
    """16 kHz 16-bit mono WAV -> raw PCM bytes."""
    with wave.open(str(path), "rb") as w:
        assert (w.getframerate(), w.getsampwidth(), w.getnchannels()) == (RATE, WIDTH, CHANNELS), path
        return w.readframes(w.getnframes())


def chunks(pcm, samples=CHUNK_SAMPLES):
    step = samples * WIDTH
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


async def transcribe(uri, pcm, language="es", timeout=60.0, realtime=False, tail_silence_s=0.0,
                     settle_s=0.03):
    """Returns (text, latency_s, t_stop) with latency from AudioStop to Transcript.

    realtime: pace the chunks like a live microphone and keep sending `tail_silence_s` of silence
    afterwards (needed to test --vad-endpointing, where the server answers before AudioStop). In
    that mode the latency is measured from the end of the original audio (before the tail) to the
    Transcript, and no AudioStop is sent until the transcript arrived or the tail ended.
    """
    async with AsyncClient.from_uri(uri) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        step_s = CHUNK_SAMPLES / RATE
        parts = chunks(pcm)
        if not realtime:
            for c in parts:
                await client.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=c).event())
            await asyncio.sleep(settle_s)
            t0 = time.perf_counter()
            await client.write_event(AudioStop().event())
            return await _wait(client, t0, timeout)

        silence = bytes(CHUNK_SAMPLES * WIDTH)
        n_tail = int(round(tail_silence_s / step_s))
        t_start = time.perf_counter()
        t_audio_end = None
        reader = asyncio.ensure_future(_wait(client, None, timeout))
        for i, c in enumerate(parts + [silence] * n_tail):
            if reader.done():
                break
            await client.write_event(AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=c).event())
            if i == len(parts) - 1:
                t_audio_end = time.perf_counter()
            await asyncio.sleep(max(0.0, t_start + (i + 1) * step_s - time.perf_counter()))
        if not reader.done():
            await client.write_event(AudioStop().event())
        text, t_recv = await reader
        return text, t_recv - (t_audio_end or t_start), t_audio_end or t_start


async def _wait(client, t0, timeout):
    while True:
        event = await asyncio.wait_for(client.read_event(), timeout)
        if event is None:
            raise ConnectionError("server closed the connection before a transcript")
        if Transcript.is_type(event.type):
            t = time.perf_counter()
            text = Transcript.from_event(event).text
            return (text, t - t0, t0) if t0 is not None else (text, t)


async def describe(uri, timeout=5.0):
    """Round trip of a Describe request (liveness check of a server); returns seconds or None."""
    t0 = time.perf_counter()
    try:
        async with AsyncClient.from_uri(uri) as client:
            await client.write_event(Describe().event())
            while True:
                event = await asyncio.wait_for(client.read_event(), timeout)
                if event is None:
                    return None
                if Info.is_type(event.type):
                    return time.perf_counter() - t0
    except Exception:
        return None
