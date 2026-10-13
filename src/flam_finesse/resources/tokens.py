from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from .._base import AsyncHTTP, SyncHTTP, TimeoutTypes, unwrap
from ..types import AccessToken


def _token_body(ttl_seconds: Optional[int], voice_ids: Optional[Sequence[str]]) -> Dict[str, Any]:
    body: Dict[str, Any] = {}
    if ttl_seconds is not None:
        body["ttl_seconds"] = ttl_seconds
    if voice_ids is not None:
        body["voice_ids"] = list(voice_ids)
    return body


class Tokens:
    """Short-lived tokens, so a browser or device can synthesize without your API key."""

    def __init__(self, http: SyncHTTP) -> None:
        self._http = http

    def create(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        voice_ids: Optional[Sequence[str]] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AccessToken:
        """Mint a single-use token. Call this on your server with an API key that has the
        ``tts`` scope, and send only the token to the client.

        Args:
            ttl_seconds: How long the token stays valid if unused, 10-3600 (default 300).
            voice_ids: Limit the token to these voices (yours or public platform voices).
        """
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", "/tokens", json=_token_body(ttl_seconds, voice_ids)
        )
        return AccessToken.model_validate(unwrap(response))


class AsyncTokens:
    """Async counterpart of :class:`Tokens`."""

    def __init__(self, http: AsyncHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        voice_ids: Optional[Sequence[str]] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AccessToken:
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", "/tokens", json=_token_body(ttl_seconds, voice_ids)
        )
        return AccessToken.model_validate(unwrap(response))
