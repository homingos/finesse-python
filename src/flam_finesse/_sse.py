"""A minimal Server-Sent Events parser (only what ``speech.stream`` needs)."""

from __future__ import annotations

from typing import AsyncIterator, Iterator, List, NamedTuple, Optional


class SSEEvent(NamedTuple):
    event: str
    data: str


class _Decoder:
    def __init__(self) -> None:
        self._event: Optional[str] = None
        self._data: List[str] = []

    def feed(self, line: str) -> Optional[SSEEvent]:
        line = line.rstrip("\r")
        if not line:
            if not self._data and self._event is None:
                return None
            ev = SSEEvent(self._event or "message", "\n".join(self._data))
            self._event, self._data = None, []
            return ev
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None

    def flush(self) -> Optional[SSEEvent]:
        return self.feed("")


def iter_sse(lines: Iterator[str]) -> Iterator[SSEEvent]:
    decoder = _Decoder()
    for line in lines:
        ev = decoder.feed(line)
        if ev is not None:
            yield ev
    ev = decoder.flush()
    if ev is not None:
        yield ev


async def aiter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    decoder = _Decoder()
    async for line in lines:
        ev = decoder.feed(line)
        if ev is not None:
            yield ev
    ev = decoder.flush()
    if ev is not None:
        yield ev
