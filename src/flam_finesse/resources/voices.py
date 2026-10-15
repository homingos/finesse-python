from __future__ import annotations

import json
from typing import Any, Callable, Dict, Mapping, Optional

from .._base import AsyncHTTP, SyncHTTP, TimeoutTypes, unwrap
from .._errors import FinesseError
from .._files import FileInput, to_multipart
from .._pagination import AsyncVoicePage, AsyncVoicePageRequest, VoicePage
from .._util import path_id
from ..types import CloneJob, CloneResult, Voice
from .jobs import DEFAULT_POLL_INTERVAL, DEFAULT_WAIT_TIMEOUT, AsyncJobs, Jobs


def _drop_none(**fields: Any) -> Dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def _consent(consent: bool) -> str:
    """The API requires an explicit attestation on every clone and enroll."""
    if consent is not True:
        raise FinesseError(
            "Cloning requires consent=True: you attest that you are the speaker or hold their "
            "explicit permission to clone their voice."
        )
    return "true"


def _clone_form(
    name: str,
    description: Optional[str],
    language: Optional[str],
    labels: Optional[Mapping[str, str]],
    ref_text: Optional[str],
    remove_background_noise: Optional[bool],
) -> Dict[str, str]:
    form = _drop_none(name=name, description=description, language=language, ref_text=ref_text)
    form["consent"] = "true"
    if labels is not None:
        form["labels"] = json.dumps(dict(labels))
    if remove_background_noise is not None:
        form["remove_background_noise"] = "true" if remove_background_noise else "false"
    return form


class Voices:
    """Create, list, clone and manage voices."""

    def __init__(self, http: SyncHTTP, jobs: Jobs) -> None:
        self._http = http
        self._jobs = jobs

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> VoicePage:
        """List your voices (newest first), then the public platform voices.

        Iterate the result to walk every voice across all pages. ``page_size`` is capped at 100.
        """

        def fetch(n: int) -> VoicePage:
            response = self._http.with_options(timeout, max_retries, extra_headers).request(
                "GET", "/voices", params={"page": n, "page_size": page_size}
            )
            return VoicePage(unwrap(response), fetch)

        return fetch(page)

    def get(
        self,
        voice_id: str,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        """Get one of your voices or a public platform voice."""
        return Voice.model_validate(
            unwrap(
                self._http.with_options(timeout, max_retries, extra_headers).request(
                    "GET", f"/voices/{path_id(voice_id)}"
                )
            )
        )

    def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        model: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        """Create an empty ``draft`` voice. Give it a reference clip with :meth:`enroll`, or use
        :meth:`clone` to do both at once."""
        body = _drop_none(name=name, description=description, language=language, model=model)
        if labels is not None:
            body["labels"] = dict(labels)
        return Voice.model_validate(
            unwrap(
                self._http.with_options(timeout, max_retries, extra_headers).request(
                    "POST", "/voices", json=body, idempotent=True
                )
            )
        )

    def update(
        self,
        voice_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        """Change a voice's metadata. Only the arguments you pass change; ``labels`` replaces the
        whole label set (``{}`` clears it)."""
        body = _drop_none(name=name, description=description, language=language)
        if labels is not None:
            body["labels"] = dict(labels)
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "PATCH", f"/voices/{path_id(voice_id)}", json=body
        )
        return Voice.model_validate(unwrap(response))

    def delete(
        self,
        voice_id: str,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        """Delete one of your voices."""
        self._http.with_options(timeout, max_retries, extra_headers).request(
            "DELETE", f"/voices/{path_id(voice_id)}"
        )

    def clone(
        self,
        *,
        name: str,
        file: FileInput,
        consent: bool,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ref_text: Optional[str] = None,
        remove_background_noise: Optional[bool] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> CloneResult:
        """Create a voice from a reference clip (at most 20 MiB; at least 10 s, and 10-30 s of
        clear speech works best). ``ref_text``, the clip's transcript, is at most 4,000
        characters. Returns at once with the voice in ``processing`` and a ``job_id``; use
        :meth:`wait_until_ready` to block until the voice can speak, or :meth:`clone_and_wait`
        to do both.

        ``consent=True`` is required: you attest that you are the speaker or hold their
        explicit permission to clone their voice. The attestation is recorded on the voice."""
        _consent(consent)
        form = _clone_form(name, description, language, labels, ref_text, remove_background_noise)
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST",
            "/voices/clone",
            data=form,
            files={"file": to_multipart(file)},
            idempotent=True,
        )
        return CloneResult.model_validate(unwrap(response))

    def enroll(
        self,
        voice_id: str,
        *,
        file: FileInput,
        consent: bool,
        ref_text: Optional[str] = None,
        language: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> CloneResult:
        """Clone a reference clip into an existing voice of yours. Returns at once; see
        :meth:`wait_until_ready`. ``consent=True`` is required, as on :meth:`clone`."""
        form = {**_drop_none(ref_text=ref_text, language=language), "consent": _consent(consent)}
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST",
            f"/voices/{path_id(voice_id)}/enroll",
            data=form,
            files={"file": to_multipart(file)},
            idempotent=True,
        )
        return CloneResult.model_validate(unwrap(response))

    def wait_until_ready(
        self,
        clone: CloneResult,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> Voice:
        """Wait for a clone job to finish and return the voice, now ``ready``.

        Raises:
            CloneFailedError: cloning failed.
            CloneTimeoutError: it did not finish within ``timeout`` seconds.
        """
        self._jobs.wait(
            clone.job_id, timeout=timeout, poll_interval=poll_interval, on_update=on_update
        )
        return self.get(clone.voice.id)

    def clone_and_wait(
        self,
        *,
        name: str,
        file: FileInput,
        consent: bool,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ref_text: Optional[str] = None,
        remove_background_noise: Optional[bool] = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> Voice:
        """:meth:`clone`, then :meth:`wait_until_ready`. Returns the ready voice."""
        clone = self.clone(
            name=name,
            file=file,
            consent=consent,
            description=description,
            language=language,
            labels=labels,
            ref_text=ref_text,
            remove_background_noise=remove_background_noise,
        )
        return self.wait_until_ready(
            clone, timeout=timeout, poll_interval=poll_interval, on_update=on_update
        )


class AsyncVoices:
    """Async counterpart of :class:`Voices`."""

    def __init__(self, http: AsyncHTTP, jobs: AsyncJobs) -> None:
        self._http = http
        self._jobs = jobs

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncVoicePageRequest:
        """List voices. ``async for voice in client.voices.list()`` walks every page;
        ``await client.voices.list()`` returns just the first page."""

        async def fetch(n: int) -> AsyncVoicePage:
            response = await self._http.with_options(timeout, max_retries, extra_headers).request(
                "GET", "/voices", params={"page": n, "page_size": page_size}
            )
            return AsyncVoicePage(unwrap(response), fetch)

        return AsyncVoicePageRequest(lambda: fetch(page))

    async def get(
        self,
        voice_id: str,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "GET", f"/voices/{path_id(voice_id)}"
        )
        return Voice.model_validate(unwrap(response))

    async def create(
        self,
        *,
        name: str,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        model: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        body = _drop_none(name=name, description=description, language=language, model=model)
        if labels is not None:
            body["labels"] = dict(labels)
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", "/voices", json=body, idempotent=True
        )
        return Voice.model_validate(unwrap(response))

    async def update(
        self,
        voice_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Voice:
        body = _drop_none(name=name, description=description, language=language)
        if labels is not None:
            body["labels"] = dict(labels)
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "PATCH", f"/voices/{path_id(voice_id)}", json=body
        )
        return Voice.model_validate(unwrap(response))

    async def delete(
        self,
        voice_id: str,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        await self._http.with_options(timeout, max_retries, extra_headers).request(
            "DELETE", f"/voices/{path_id(voice_id)}"
        )

    async def clone(
        self,
        *,
        name: str,
        file: FileInput,
        consent: bool,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ref_text: Optional[str] = None,
        remove_background_noise: Optional[bool] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> CloneResult:
        _consent(consent)
        form = _clone_form(name, description, language, labels, ref_text, remove_background_noise)
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST",
            "/voices/clone",
            data=form,
            files={"file": to_multipart(file)},
            idempotent=True,
        )
        return CloneResult.model_validate(unwrap(response))

    async def enroll(
        self,
        voice_id: str,
        *,
        file: FileInput,
        consent: bool,
        ref_text: Optional[str] = None,
        language: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> CloneResult:
        form = {**_drop_none(ref_text=ref_text, language=language), "consent": _consent(consent)}
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST",
            f"/voices/{path_id(voice_id)}/enroll",
            data=form,
            files={"file": to_multipart(file)},
            idempotent=True,
        )
        return CloneResult.model_validate(unwrap(response))

    async def wait_until_ready(
        self,
        clone: CloneResult,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> Voice:
        await self._jobs.wait(
            clone.job_id, timeout=timeout, poll_interval=poll_interval, on_update=on_update
        )
        return await self.get(clone.voice.id)

    async def clone_and_wait(
        self,
        *,
        name: str,
        file: FileInput,
        consent: bool,
        description: Optional[str] = None,
        language: Optional[str] = None,
        labels: Optional[Mapping[str, str]] = None,
        ref_text: Optional[str] = None,
        remove_background_noise: Optional[bool] = None,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> Voice:
        clone = await self.clone(
            name=name,
            file=file,
            consent=consent,
            description=description,
            language=language,
            labels=labels,
            ref_text=ref_text,
            remove_background_noise=remove_background_noise,
        )
        return await self.wait_until_ready(
            clone, timeout=timeout, poll_interval=poll_interval, on_update=on_update
        )
