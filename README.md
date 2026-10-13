# flam-finesse

Python SDK for the Finesse voice API: clone voices, synthesize speech, and stream speech in
realtime over a WebSocket.

> Private preview. Install from GitHub until the package is published.

```bash
pip install "git+ssh://git@github.com/homingos/finesse-python.git"
# or
uv add "git+ssh://git@github.com/homingos/finesse-python.git"
```

Requires Python 3.10+.

## Quickstart

```python
from flam_finesse import Finesse

client = Finesse()  # reads FINESSE_API_KEY

voice = next(v for v in client.voices.list() if v.is_ready)
audio = client.speech.create(voice_id=voice.id, text="Hello from Finesse!")
audio.save("hello.wav")
```

Configuration:

| Argument | Environment variable | Default |
|---|---|---|
| `api_key` | `FINESSE_API_KEY` | required |
| `base_url` | `FINESSE_BASE_URL` | `https://fi.production.flamapis.com` |
| `timeout` | | 120 s (10 s to connect) |
| `max_retries` | | 2 |

Use `https://finesse.example.test` as `base_url` for the development environment.

Every request method also takes per-call overrides:

```python
client.speech.create(
    voice_id=voice.id,
    text="Hello!",
    timeout=30.0,  # seconds, or an httpx.Timeout
    max_retries=0,
    extra_headers={"X-Request-Id": "abc"},
)
```

(On `jobs.wait`, `wait_until_ready` and `clone_and_wait`, `timeout` is the overall wait.)

## Voices

```python
for voice in client.voices.list():  # walks every page
    print(voice.id, voice.name, voice.status, voice.labels)

async for voice in async_client.voices.list():  # the same with AsyncFinesse
    ...

voice = client.voices.get("665f1a2b3c4d5e6f7a8b9c0d")
client.voices.update(voice.id, name="Narrator (UK)", labels={"accent": "british"})
client.voices.delete(voice.id)
```

## Cloning

```python
voice = client.voices.clone_and_wait(
    name="Ana",
    file="reference.wav",  # a path, bytes, a binary file, or (name, bytes[, type])
    language="en",
    labels={"gender": "female"},
    on_update=lambda job: print(job.status),
)
print(voice.status)  # "ready"
```

`clone()` returns immediately with the voice (`processing`) and a `job_id`; pass the result to
`voices.wait_until_ready()` when you want to block later. The first clone after the cloner has
been idle can take a couple of minutes.

## Speech

One clip:

```python
audio = client.speech.create(voice_id=voice.id, text="Hello!", output_format="wav")
audio.data  # bytes
audio.sample_rate  # 24000
audio.generation_id  # id in your generation history
```

Streamed as raw PCM (`s16le`, 24 kHz):

```python
with client.speech.stream(voice_id=voice.id, text="Hello, streamed.") as stream:
    for chunk in stream:
        play(chunk.pcm)
print(stream.done.audio_duration_s)
```

## Realtime

Realtime runs on the async client. One session is one WebSocket that can run several turns
(*contexts*) at once.

```python
import asyncio
from flam_finesse import AsyncFinesse, AudioEvent


async def main() -> None:
    client = AsyncFinesse()
    async with client.realtime.connect() as rt:
        async for event in rt.say(voice_id=VOICE_ID, text="Hi! How can I help?"):
            if isinstance(event, AudioEvent):
                play(event.pcm)  # s16le, 24 kHz mono


asyncio.run(main())
```

Stream text as your LLM produces it, and cancel when the user interrupts:

```python
ctx = rt.context(voice_id=VOICE_ID)
async for token in llm_tokens():
    await ctx.send(token)
await ctx.finish()

async for pcm in ctx.audio():
    if user_started_talking():
        await ctx.cancel()  # audio still in flight is dropped
    play(pcm)
```

If the connection drops, running contexts raise `RealtimeError` (check `.retryable`), and the
next context reconnects automatically. Events are buffered per context so a slow one never
stalls the others: iterate every context you start, or `cancel()` it. If you stop iterating
`say()` early, close it with `contextlib.aclosing(...)` so the turn is cancelled right away. A workspace can hold up to 4 sessions at once; a fifth
raises `RateLimitError` with `code == "too_many_sessions"`. Run several contexts on one session
instead.

## Errors

```python
import flam_finesse as ff

try:
    client.speech.create(voice_id=voice.id, text="Hi")
except ff.PaymentRequiredError as e:  # 402: out of credits or quota
    print(e.code, e.billing_remaining)
except ff.APIStatusError as e:  # any other HTTP error
    print(e.status_code, e.message)
```

| Error | When |
|---|---|
| `BadRequestError` | 400: invalid input, text over 10,000 characters, voice not enrolled |
| `AuthenticationError` | 401: missing, unknown or revoked API key |
| `PaymentRequiredError` | 402: `insufficient_credits` or `quota_exhausted` |
| `PermissionDeniedError` | 403: `feature_disabled`, or `plan_limit_reached` (see `.feature`) |
| `NotFoundError` | 404 |
| `RateLimitError` | 429 |
| `InternalServerError` | 5xx |
| `APIConnectionError` / `APITimeoutError` | no response |
| `StreamError` | a speech stream failed after it started |
| `RealtimeError` | a realtime context or session failed |
| `CloneFailedError` / `CloneTimeoutError` | waiting for a clone |

Transient failures are retried twice with backoff. Reads, updates and deletes retry on
connection errors, timeouts, 408, 429 and 5xx. Creates, uploads and synthesis are billed or
create things, so they retry only when the server certainly did no work: the connection never
opened, 429, 502 or 503. A synthesis that fails is refunded.

## Development

```bash
uv sync
uv run pytest            # unit tests (no network)
uv run ruff check . && uv run ruff format --check . && uv run mypy src/flam_finesse

# Contract tests against the development API (billed per character):
FINESSE_LIVE=1 FINESSE_API_KEY=flv_... \
FINESSE_BASE_URL=https://finesse.example.test uv run pytest tests/live -v
```
