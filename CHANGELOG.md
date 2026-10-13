# Changelog

## 0.1.0 (unreleased)

- Sync and async clients: voices, cloning with job waiting, one-shot and streamed speech.
- Realtime WebSocket sessions (async): concurrent contexts, streamed text, cancel, reconnect.
- One error hierarchy over the API's error shapes; billing-safe retries with backoff.
- Per-call `timeout`, `max_retries` and `extra_headers` on every request method.
- `async for voice in client.voices.list()` without awaiting the first page.
- Creating, cloning, enrolling and synthesis send an `Idempotency-Key` and retry like reads;
  a retry while the first attempt runs waits for it.
- `client.with_raw_response`, `client.with_options()`, `FINESSE_LOG` logging, and
  `request_id` on errors.
- `voices.clone`, `voices.enroll` and `voices.clone_and_wait` require `consent=True`: the
  caller attests they are the speaker or hold their permission. The API rejects clones without
  it.
- Short-lived tokens: `client.tokens.create()` mints a single-use `fin_tok_` token;
  `Finesse(access_token=...)` uses one for speech, and `realtime.connect(access_token=...,
  get_access_token=...)` for a realtime session (a fresh token on every reconnect).
- Errors: `ConflictError` (409), `PayloadTooLargeError` (413) and `UnprocessableEntityError`
  (422); `retry_after` on every HTTP error; `code` documents the API's codes
  (`insufficient_scope`, `key_expired`, `token_invalid`, `rate_limited`, `concurrency_limited`,
  `upstream_busy`, ...).
- Realtime: close reasons `upstream_busy`, `upstream_unavailable` and `upstream_error` surface
  as retryable `RealtimeError` codes.
