# Changelog

## 0.1.1 (unreleased)

- Token-authenticated speech (`access_token`) is retried only on 429. The token is spent just
  before synthesis runs, so a retry after a 5xx or a dropped connection failed with
  `token_invalid` and hid the real error; those are now raised as is.
- Realtime: `cancel()` on a context that never started ends it, so iterating it no longer hangs,
  and waits for a `send` in flight, so a start being sent is cancelled after it. `send` after
  `cancel` raises `RealtimeError(code="context_cancelled")`.
- Realtime: text that would make a frame over the service's 1 MiB limit is split across several
  frames instead of closing the socket (1009) and failing every context.
- Realtime: a close without a status code (1005) is a retryable `connection_lost`.
- Errors: running out of credits during synthesis has `code == "insufficient_credits"` (it was
  `payment_required`). `code` documents the rest of the API's codes: `payment_required`,
  `forbidden`, `conflict`, `internal_error`, `unavailable`, `idempotency_in_progress`,
  `idempotency_key_reused`.
- Types: `Voice.metadata`, `Voice.deleted_at` and `CloneJob.result` are removed (the API no
  longer returns them); `Voice.created_via_key_prefix` is added.
- Docs: `num_step` is 1-32, `sample_rate` 8000-48000; clone clips need at least 10 s and
  `ref_text` at most 4,000 characters; media URLs may be short-lived signed URLs.

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
