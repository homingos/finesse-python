"""Speak a few sentences over a realtime session and save the PCM as realtime.wav."""

import asyncio
import wave

from flam_finesse import AsyncFinesse, AudioEvent


async def main() -> None:
    client = AsyncFinesse()
    voice = None
    async for v in client.voices.list():
        if v.is_ready:
            voice = v
            break
    assert voice is not None, "no ready voice"

    pcm = bytearray()
    async with client.realtime.connect() as rt:
        ctx = rt.context(voice_id=voice.id)
        for piece in ["Hello! ", "This text is streamed ", "a few words at a time."]:
            await ctx.send(piece)
        await ctx.finish()
        async for event in ctx:
            if isinstance(event, AudioEvent):
                pcm += event.pcm
        rate = ctx.sample_rate or 24000

    with wave.open("realtime.wav", "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)  # s16le
        f.setframerate(rate)
        f.writeframes(bytes(pcm))
    print(f"Saved realtime.wav ({len(pcm)} bytes at {rate} Hz)")


asyncio.run(main())
