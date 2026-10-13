"""Response models.

Models accept fields the SDK does not know yet (``extra="allow"``), so a newer API never breaks
an older SDK. Timestamps arrive as Unix milliseconds and are exposed as timezone-aware
``datetime`` objects. Labels arrive as ``[{"key": ..., "value": ...}]`` and are exposed as a
plain ``dict``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "Voice",
    "VoiceSettings",
    "CloneJob",
    "CloneResult",
    "Audio",
    "AudioChunk",
    "StreamDone",
    "AccessToken",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


def _millis(value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return value


class VoiceSettings(_Model):
    stability: Optional[float] = None
    similarity: Optional[float] = None
    style: Optional[float] = None
    speed: Optional[float] = None
    pitch: Optional[float] = None
    volume: Optional[float] = None
    use_speaker_boost: Optional[bool] = None
    emotion: Optional[str] = None
    engine_params: Optional[Dict[str, Any]] = None


class Voice(_Model):
    """A voice. A voice can synthesize once ``status == "ready"``."""

    id: str
    org_id: str
    name: str
    status: str
    """``draft``, ``processing``, ``ready``, ``failed``, ... (see the API reference)."""
    clone_method: str = "instant"
    visibility: str = "private"
    """``private`` for your voices, ``public`` for platform catalog voices."""
    primary_language: str = ""
    description: Optional[str] = None
    model: Optional[str] = None
    alias: Optional[str] = None
    """Human-friendly handle, also usable as ``voice`` on the OpenAI-compatible endpoint."""
    source: Optional[str] = None
    language: Optional[str] = None
    gender: Optional[str] = None
    labels: Dict[str, str] = {}
    settings: Optional[VoiceSettings] = None
    metadata: Optional[Dict[str, Any]] = None
    external_ref: Optional[str] = None
    original_audio_url: Optional[str] = None
    sample_audio_url: Optional[str] = None
    avatar_url: Optional[str] = None
    base_voice_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    @field_validator("labels", mode="before")
    @classmethod
    def _labels(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, list):
            return {
                str(item["key"]): str(item.get("value", ""))
                for item in value
                if isinstance(item, dict) and "key" in item
            }
        return value

    @field_validator(
        "created_at", "updated_at", "last_used_at", "expires_at", "deleted_at", mode="before"
    )
    @classmethod
    def _times(cls, value: Any) -> Any:
        return _millis(value)


class CloneJob(_Model):
    """An asynchronous clone job."""

    id: str
    org_id: str
    status: str
    """``pending``, ``running``, ``succeeded``, ``failed``, ``cancelled`` or ``expired``."""
    job_type: str = "instant_clone"
    voice_id: Optional[str] = None
    progress: int = 0
    """Percent complete. Not updated while running; 100 once succeeded."""
    attempts: int = 0
    error: Optional[Dict[str, Any]] = None
    """Failure detail, ``{"message": ..., "status_code": ...}``."""
    result: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: Optional[datetime] = None

    @property
    def is_finished(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled", "expired")

    @field_validator("started_at", "finished_at", "created_at", mode="before")
    @classmethod
    def _times(cls, value: Any) -> Any:
        return _millis(value)


class CloneResult(_Model):
    """What ``voices.clone`` / ``voices.enroll`` return: the voice (``processing``) and its job."""

    voice: Voice
    job_id: str


class Audio(_Model):
    """A synthesized clip from ``speech.create``."""

    data: bytes
    content_type: str
    """``audio/wav`` for WAV, ``application/octet-stream`` for raw PCM."""
    sample_rate: Optional[int] = None
    generation_id: Optional[str] = None
    """Id of the generation history record (when object storage is enabled)."""
    audio_url: Optional[str] = None
    billing_remaining: Optional[int] = None

    def save(self, path: Any) -> None:
        """Write the audio bytes to ``path``."""
        with open(path, "wb") as f:
            f.write(self.data)

    def __repr__(self) -> str:
        return (
            f"Audio(content_type={self.content_type!r}, bytes={len(self.data)}, "
            f"sample_rate={self.sample_rate!r}, generation_id={self.generation_id!r})"
        )


class AudioChunk(_Model):
    """One chunk of raw PCM from ``speech.stream``."""

    pcm: bytes
    sample_rate: Optional[int] = None
    audio_format: Optional[str] = None
    """PCM encoding, e.g. ``s16le``."""
    index: Optional[int] = None

    def __repr__(self) -> str:
        return (
            f"AudioChunk(bytes={len(self.pcm)}, sample_rate={self.sample_rate!r}, "
            f"audio_format={self.audio_format!r}, index={self.index!r})"
        )


class StreamDone(_Model):
    """The final event of ``speech.stream``."""

    audio_duration_s: Optional[float] = None
    generation_id: Optional[str] = None
    audio_url: Optional[str] = None


class AccessToken(_Model):
    """A short-lived, single-use token from ``tokens.create``.

    Hand ``token`` to a browser or device: it is good for one ``speech.create`` /
    ``speech.stream`` call or one realtime session, carries the ``tts`` scope only, and dies
    when the key that minted it is revoked or expires. It is shown once.
    """

    token: str
    expires_at: Optional[datetime] = None
    ttl_seconds: Optional[int] = None

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires(cls, value: Any) -> Any:
        return _millis(value)

    def __repr__(self) -> str:
        # Never print the secret itself.
        return f"AccessToken(token='{self.token[:12]}...', expires_at={self.expires_at!r})"
