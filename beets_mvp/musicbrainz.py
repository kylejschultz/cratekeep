"""Small, bounded MusicBrainz release-search client.

The app injects a provider in tests and deployments that need custom transport.
This default implementation deliberately performs only metadata reads.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(RuntimeError):
    """A safe error suitable for persisting with a candidate-generation job."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def search_releases(query: dict, *, limit: int = 5, timeout: int = 10) -> list[dict]:
    """Return normalized MusicBrainz release candidates for one album query."""
    limit = max(1, min(int(limit), 10))
    terms = []
    if query.get("album"):
        terms.append(f'release:"{_escape(query["album"])}"')
    if query.get("artist"):
        terms.append(f'artist:"{_escape(query["artist"])}"')
    params = urllib.parse.urlencode({"query": " AND ".join(terms), "fmt": "json", "limit": limit})
    request = urllib.request.Request(
        f"https://musicbrainz.org/ws/2/release/?{params}",
        headers={"Accept": "application/json", "User-Agent": "Cratekeep/0.1 (https://github.com/kylejschultz/cratekeep)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise ProviderError("MusicBrainz rate limit reached; try again later", retryable=True) from exc
        raise ProviderError(f"MusicBrainz returned HTTP {exc.code}", retryable=exc.code >= 500) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError("MusicBrainz could not be reached", retryable=True) from exc
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("MusicBrainz returned an invalid response") from exc

    results = []
    for release in document.get("releases", [])[:limit]:
        artist_credit = release.get("artist-credit") or []
        artist = "".join(part.get("name", "") if isinstance(part, dict) else str(part) for part in artist_credit)
        results.append({
            "provider_id": release.get("id", ""),
            "artist": artist,
            "album": release.get("title", ""),
            "year": str(release.get("date", ""))[:4] or None,
            "track_count": release.get("track-count"),
            "score": release.get("score", 0),
        })
    return [result for result in results if result["provider_id"]]


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
