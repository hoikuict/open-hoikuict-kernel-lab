from urllib.parse import unquote, urlsplit


def safe_internal_redirect(raw: str | None, fallback: str) -> str:
    if not raw:
        return fallback
    candidate = str(raw)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
        return fallback
    if "\\" in candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return fallback

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback

    decoded = unquote(candidate)
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in decoded):
        return fallback
    if "\\" in decoded or not decoded.startswith("/") or decoded.startswith("//"):
        return fallback
    return candidate
