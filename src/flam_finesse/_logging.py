"""SDK logging. Set ``FINESSE_LOG=debug`` (or ``info``) to print it to stderr, or configure the
``flam_finesse`` logger yourself. The API key, request bodies and audio are never logged."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("flam_finesse")


def setup_from_env() -> None:
    level = os.environ.get("FINESSE_LOG", "").strip().lower()
    if level not in ("debug", "info"):
        return
    logger.setLevel(logging.DEBUG if level == "debug" else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("[%(asctime)s %(name)s %(levelname)s] %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)


setup_from_env()
