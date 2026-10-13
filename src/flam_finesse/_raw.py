"""``client.with_raw_response``: the same calls, returning the HTTP response with the result."""

from __future__ import annotations

import copy
import inspect
from typing import Any, Callable, Dict, Generic, List, Optional, TypeVar

import httpx

from ._errors import FinesseError

T = TypeVar("T")


class RawResponse(Generic[T]):
    """The parsed result of a call plus the HTTP response it came from.

    For calls that make several requests (``voices.list`` walking pages, ``clone_and_wait``
    polling), ``http_response`` is the first request's response.
    """

    def __init__(self, http_response: httpx.Response, parsed: T) -> None:
        self.http_response = http_response
        self._parsed = parsed

    @property
    def status_code(self) -> int:
        return self.http_response.status_code

    @property
    def headers(self) -> httpx.Headers:
        return self.http_response.headers

    @property
    def request_id(self) -> Optional[str]:
        """The service's ``X-Request-Id`` for this request, when sent."""
        value = self.http_response.headers.get("X-Request-Id")
        return str(value) if value is not None else None

    def parse(self) -> T:
        """The same result the normal method returns."""
        return self._parsed

    def __repr__(self) -> str:
        return f"RawResponse(status_code={self.status_code}, request_id={self.request_id!r})"


Build = Callable[[Any], Dict[str, Any]]


def _view(http: Any, seen: List[httpx.Response]) -> Any:
    view = copy.copy(http)
    view._owns_client = False
    view.on_response = seen.append
    return view


class _Namespace:
    def __init__(self, http: Any, build: Build, name: str, is_async: bool) -> None:
        self._http, self._build, self._name, self._async = http, build, name, is_async

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_"):
            raise AttributeError(method)
        if method == "stream":
            raise FinesseError(
                "speech.stream has no raw form: read headers from the stream itself "
                "(stream.billing_remaining, stream.done) instead."
            )

        if self._async:

            async def acall(*args: Any, **kwargs: Any) -> RawResponse[Any]:
                seen: List[httpx.Response] = []
                resource = self._build(_view(self._http, seen))[self._name]
                result = getattr(resource, method)(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                return _wrap(seen, result)

            return acall

        def call(*args: Any, **kwargs: Any) -> RawResponse[Any]:
            seen: List[httpx.Response] = []
            resource = self._build(_view(self._http, seen))[self._name]
            return _wrap(seen, getattr(resource, method)(*args, **kwargs))

        return call


def _wrap(seen: List[httpx.Response], result: Any) -> RawResponse[Any]:
    if not seen:
        raise FinesseError("This call made no HTTP request, so it has no raw response.")
    return RawResponse(seen[0], result)


class RawClient:
    """``client.with_raw_response``: ``voices``, ``jobs``, ``speech`` and ``tokens`` with the
    same methods, each returning a :class:`RawResponse`."""

    def __init__(self, http: Any, build: Build, is_async: bool) -> None:
        self.voices: Any = _Namespace(http, build, "voices", is_async)
        self.jobs: Any = _Namespace(http, build, "jobs", is_async)
        self.speech: Any = _Namespace(http, build, "speech", is_async)
        self.tokens: Any = _Namespace(http, build, "tokens", is_async)
