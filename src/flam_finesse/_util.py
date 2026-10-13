from urllib.parse import quote


def path_id(value: str) -> str:
    """Escape an id for use as one URL path segment, so it can't change the route."""
    value = str(value)
    if value in ("", ".", ".."):
        raise ValueError(f"Invalid id: {value!r}")
    return quote(value, safe="")
