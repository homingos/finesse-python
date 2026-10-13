from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from .._base import AsyncHTTP, SyncHTTP, unwrap
from .._errors import CloneFailedError, CloneTimeoutError
from .._util import path_id
from ..types import CloneJob

DEFAULT_WAIT_TIMEOUT = 300.0
DEFAULT_POLL_INTERVAL = 2.0


class Jobs:
    """Clone jobs started by ``voices.clone`` and ``voices.enroll``."""

    def __init__(self, http: SyncHTTP) -> None:
        self._http = http

    def get(self, job_id: str) -> CloneJob:
        return CloneJob.model_validate(
            unwrap(self._http.request("GET", f"/jobs/{path_id(job_id)}"))
        )

    def wait(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> CloneJob:
        """Poll until the job finishes and return it.

        The first clone after the cloner has been idle can take a couple of minutes.

        Raises:
            CloneFailedError: the job ended ``failed``, ``cancelled`` or ``expired``.
            CloneTimeoutError: it did not finish within ``timeout`` seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            job = self.get(job_id)
            if on_update is not None:
                on_update(job)
            if job.is_finished:
                if job.status != "succeeded":
                    raise CloneFailedError(job)
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloneTimeoutError(job_id, timeout)
            time.sleep(min(poll_interval, remaining))


class AsyncJobs:
    """Async counterpart of :class:`Jobs`."""

    def __init__(self, http: AsyncHTTP) -> None:
        self._http = http

    async def get(self, job_id: str) -> CloneJob:
        response = await self._http.request("GET", f"/jobs/{path_id(job_id)}")
        return CloneJob.model_validate(unwrap(response))

    async def wait(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_update: Optional[Callable[[CloneJob], None]] = None,
    ) -> CloneJob:
        """Poll until the job finishes and return it. See :meth:`Jobs.wait`."""
        deadline = time.monotonic() + timeout
        while True:
            job = await self.get(job_id)
            if on_update is not None:
                on_update(job)
            if job.is_finished:
                if job.status != "succeeded":
                    raise CloneFailedError(job)
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloneTimeoutError(job_id, timeout)
            await asyncio.sleep(min(poll_interval, remaining))
