"""Python SDK for the Finesse voice API.

>>> from flam_finesse import Finesse
>>> client = Finesse()  # reads FINESSE_API_KEY
"""

from ._client import AsyncFinesse, Finesse
from ._errors import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    CloneFailedError,
    CloneTimeoutError,
    ConflictError,
    FinesseError,
    InternalServerError,
    NotFoundError,
    PayloadTooLargeError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    StreamError,
    UnprocessableEntityError,
)
from ._pagination import AsyncVoicePage, AsyncVoicePageRequest, VoicePage
from ._raw import RawResponse
from ._version import __version__
from .realtime import (
    AudioEvent,
    RealtimeContext,
    RealtimeError,
    RealtimeEvent,
    RealtimeSession,
    SentenceDone,
    TaskCancelled,
    TaskDone,
    TaskStarted,
)
from .resources import AsyncSpeechStream, SpeechStream
from .types import (
    AccessToken,
    Audio,
    AudioChunk,
    CloneJob,
    CloneResult,
    StreamDone,
    Voice,
    VoiceSettings,
)

__all__ = [
    "__version__",
    "Finesse",
    "AsyncFinesse",
    "Voice",
    "VoiceSettings",
    "VoicePage",
    "AsyncVoicePage",
    "AsyncVoicePageRequest",
    "CloneJob",
    "CloneResult",
    "AccessToken",
    "Audio",
    "RawResponse",
    "AudioChunk",
    "StreamDone",
    "SpeechStream",
    "AsyncSpeechStream",
    "FinesseError",
    "APIConnectionError",
    "APITimeoutError",
    "APIStatusError",
    "BadRequestError",
    "AuthenticationError",
    "PaymentRequiredError",
    "PermissionDeniedError",
    "NotFoundError",
    "ConflictError",
    "PayloadTooLargeError",
    "UnprocessableEntityError",
    "RateLimitError",
    "InternalServerError",
    "StreamError",
    "CloneFailedError",
    "CloneTimeoutError",
    "RealtimeSession",
    "RealtimeContext",
    "RealtimeError",
    "RealtimeEvent",
    "TaskStarted",
    "AudioEvent",
    "SentenceDone",
    "TaskDone",
    "TaskCancelled",
]
