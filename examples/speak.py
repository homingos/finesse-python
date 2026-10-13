"""Synthesize one sentence with the first ready voice and save it as speak.wav."""

from flam_finesse import Finesse

client = Finesse()
voice = next(v for v in client.voices.list() if v.is_ready)
audio = client.speech.create(voice_id=voice.id, text="Hello from Finesse!")
audio.save("speak.wav")
print(f"Saved speak.wav ({len(audio.data)} bytes, {audio.sample_rate} Hz) with {voice.name}")
