"""Pages for ``voices.list``. Iterating a page walks this page and every page after it."""

from __future__ import annotations

from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Generator, Iterator, List

from .types import Voice


class VoicePage:
    """One page of voices.

    Attributes are the page itself (``voices``, ``page``, ``page_size``, ``total``,
    ``has_more``). ``for voice in page`` yields every voice from this page onwards, fetching
    later pages as needed.
    """

    def __init__(self, data: Dict[str, Any], fetch: Callable[[int], VoicePage]) -> None:
        self.voices: List[Voice] = [Voice.model_validate(v) for v in data.get("voices") or []]
        self.page: int = int(data.get("page", 1))
        self.page_size: int = int(data.get("page_size", len(self.voices)))
        self.total: int = int(data.get("total", len(self.voices)))
        self.has_more: bool = bool(data.get("has_more", False))
        self._fetch = fetch

    def next_page(self) -> VoicePage:
        return self._fetch(self.page + 1)

    def iter_pages(self) -> Iterator[VoicePage]:
        page = self
        while True:
            yield page
            if not page.has_more or not page.voices:
                return
            page = page.next_page()

    def __iter__(self) -> Iterator[Voice]:
        for page in self.iter_pages():
            yield from page.voices

    def __repr__(self) -> str:
        return (
            f"VoicePage(page={self.page}, voices={len(self.voices)}, total={self.total}, "
            f"has_more={self.has_more})"
        )


class AsyncVoicePage:
    """Async counterpart of :class:`VoicePage`: ``async for voice in page``."""

    def __init__(
        self, data: Dict[str, Any], fetch: Callable[[int], Awaitable[AsyncVoicePage]]
    ) -> None:
        self.voices: List[Voice] = [Voice.model_validate(v) for v in data.get("voices") or []]
        self.page: int = int(data.get("page", 1))
        self.page_size: int = int(data.get("page_size", len(self.voices)))
        self.total: int = int(data.get("total", len(self.voices)))
        self.has_more: bool = bool(data.get("has_more", False))
        self._fetch = fetch

    async def next_page(self) -> AsyncVoicePage:
        return await self._fetch(self.page + 1)

    async def iter_pages(self) -> AsyncIterator[AsyncVoicePage]:
        page = self
        while True:
            yield page
            if not page.has_more or not page.voices:
                return
            page = await page.next_page()

    async def __aiter__(self) -> AsyncIterator[Voice]:
        async for page in self.iter_pages():
            for voice in page.voices:
                yield voice

    def __repr__(self) -> str:
        return (
            f"AsyncVoicePage(page={self.page}, voices={len(self.voices)}, total={self.total}, "
            f"has_more={self.has_more})"
        )


class AsyncVoicePageRequest:
    """What async ``voices.list()`` returns: ``await`` it for the first page, or iterate it
    directly to walk every voice::

        page = await client.voices.list()
        async for voice in client.voices.list():
            ...
    """

    def __init__(self, fetch: Callable[[], Awaitable[AsyncVoicePage]]) -> None:
        self._fetch = fetch

    def __await__(self) -> Generator[Any, None, AsyncVoicePage]:
        return self._fetch().__await__()

    async def __aiter__(self) -> AsyncIterator[Voice]:
        page = await self._fetch()
        async for voice in page:
            yield voice
