from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from .._base import AsyncHTTP, SyncHTTP, TimeoutTypes, unwrap
from ..types import DirectSession


def _session_body(voice_ids: Sequence[str], budget_chars: Optional[int]) -> Dict[str, Any]:
    body: Dict[str, Any] = {"voice_ids": list(voice_ids)}
    if budget_chars is not None:
        body["budget_chars"] = budget_chars
    return body


class DirectSessions:
    """``client.realtime.sessions``: direct sessions, which run straight against the GPU."""

    def __init__(self, http: SyncHTTP) -> None:
        self._http = http

    def create(
        self,
        *,
        voice_ids: Sequence[str],
        budget_chars: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> DirectSession:
        """Create a direct session. Call this on your server with an API key that has the
        ``tts`` scope; it reserves ``budget_chars`` from your balance now. Connect within 60 s
        (``connect_by``) with ``AsyncFinesse.realtime.connect(session=...)`` or
        ``connect_direct``, or hand ``url``, ``protocol`` and ``token`` to a browser.

        Args:
            voice_ids: The voices the session may use: 1 to 16 of your voices or public
                platform voices.
            budget_chars: Characters to reserve, 1-200000 (default 20000). Unused characters
                are returned when the session ends. If the balance can't cover it, 1000 are
                reserved instead (see ``DirectSession.budget_chars``).
        """
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", "/realtime/sessions", json=_session_body(voice_ids, budget_chars)
        )
        return DirectSession.model_validate(unwrap(response))


class AsyncDirectSessions:
    """Async counterpart of :class:`DirectSessions`."""

    def __init__(self, http: AsyncHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        voice_ids: Sequence[str],
        budget_chars: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> DirectSession:
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", "/realtime/sessions", json=_session_body(voice_ids, budget_chars)
        )
        return DirectSession.model_validate(unwrap(response))
