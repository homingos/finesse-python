"""Turn the file argument of clone/enroll into a multipart part that survives retries."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import IO, Tuple, Union

FileContent = Union[bytes, IO[bytes]]
FileInput = Union[
    str,
    "os.PathLike[str]",
    bytes,
    IO[bytes],
    Tuple[str, FileContent],
    Tuple[str, FileContent, str],
]
"""A path, raw bytes, a binary file object, or ``(filename, content[, content_type])``."""

MultipartFile = Tuple[str, bytes, str]


def to_multipart(file: FileInput) -> MultipartFile:
    """Read ``file`` fully, so a retried upload sends the same bytes again."""
    if isinstance(file, tuple):
        name = file[0]
        content = _read(file[1])
        ctype = file[2] if len(file) > 2 else _guess(name)
        return name, content, ctype
    if isinstance(file, (str, os.PathLike)):
        path = Path(file)
        return path.name, path.read_bytes(), _guess(path.name)
    if isinstance(file, (bytes, bytearray)):
        return "audio.wav", bytes(file), "application/octet-stream"
    name = os.path.basename(str(getattr(file, "name", "") or "")) or "audio.wav"
    return name, _read(file), _guess(name)


def _read(content: FileContent) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    data = content.read()
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("File objects must be opened in binary mode ('rb').")
    return bytes(data)


def _guess(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
