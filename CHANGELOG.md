# Changelog

## 0.1.0 (unreleased)

- Sync and async clients: voices, cloning with job waiting, one-shot and streamed speech.
- Realtime WebSocket sessions (async): concurrent contexts, streamed text, cancel, reconnect.
- One error hierarchy over the API's error shapes; billing-safe retries with backoff.
- Per-call `timeout`, `max_retries` and `extra_headers` on every request method.
- `async for voice in client.voices.list()` without awaiting the first page.
- `client.with_raw_response`, `client.with_options()` and `FINESSE_LOG` logging.
