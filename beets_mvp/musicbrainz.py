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

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "provider_error",
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.upstream_status = upstream_status


def search_releases(query: dict, *, limit: int = 5, timeout: int = 10) -> list[dict]:
    """Return canonical MusicBrainz release data for one album query."""
    limit = max(1, min(int(limit), 10))
    musicbrainz_id = query.get("musicbrainz_id")
    if musicbrainz_id:
        params = urllib.parse.urlencode({"inc": "artist-credits+recordings+release-groups", "fmt": "json"})
        url = f"https://musicbrainz.org/ws/2/release/{urllib.parse.quote(str(musicbrainz_id))}?{params}"
    else:
        terms = []
        if query.get("album"):
            terms.append(f'release:"{_escape(query["album"])}"')
        if query.get("artist"):
            terms.append(f'artist:"{_escape(query["artist"])}"')
        params = urllib.parse.urlencode({"query": " AND ".join(terms), "fmt": "json", "limit": limit})
        url = f"https://musicbrainz.org/ws/2/release/?{params}"
    document = _request_json(url, timeout)
    releases = [document] if musicbrainz_id and document.get("id") else document.get("releases", [])[:limit]
    # Search responses are retrieval records and generally omit recordings.
    # Resolve each bounded hit to its canonical release before matching.
    if not musicbrainz_id:
        detailed = []
        for release in releases:
            release_id = release.get("id")
            if not release_id:
                continue
            params = urllib.parse.urlencode({"inc": "artist-credits+recordings+release-groups", "fmt": "json"})
            detail_url = f"https://musicbrainz.org/ws/2/release/{urllib.parse.quote(str(release_id))}?{params}"
            detail = _request_json(detail_url, timeout)
            detail["score"] = release.get("score")
            detailed.append(detail)
        releases = detailed
    return [result for release in releases if (result := _normalize_release(release, exact=bool(musicbrainz_id)))["provider_id"]]


def _request_json(url: str, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Cratekeep/0.1 (https://github.com/kylejschultz/cratekeep)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ProviderError(
                "No MusicBrainz release exists for that ID. Check that it is a release ID, not a release-group ID.",
                code="release_not_found",
                upstream_status=exc.code,
            ) from exc
        if exc.code == 429:
            raise ProviderError(
                "MusicBrainz rate limit reached; try again later",
                retryable=True,
                code="provider_http_error",
                upstream_status=exc.code,
            ) from exc
        raise ProviderError(
            f"MusicBrainz returned HTTP {exc.code}",
            retryable=exc.code >= 500,
            code="provider_http_error",
            upstream_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError("MusicBrainz could not be reached", retryable=True, code="provider_unavailable") from exc
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError("MusicBrainz returned an invalid response", code="provider_invalid_response") from exc

    if not isinstance(document, dict):
        raise ProviderError("MusicBrainz returned an invalid response", code="provider_invalid_response")
    return document


def _normalize_release(release: dict, *, exact: bool) -> dict:
    artist_credit = release.get("artist-credit") or []
    artist = "".join((part.get("name", "") + part.get("joinphrase", ""))
                     if isinstance(part, dict) else str(part) for part in artist_credit)
    media = []
    flattened = []
    for medium_index, source_medium in enumerate(release.get("media") or [], 1):
        medium_position = source_medium.get("position") or medium_index
        tracks = []
        for track_index, source_track in enumerate(source_medium.get("tracks") or [], 1):
            recording = source_track.get("recording") or {}
            normalized = {
                "position": source_track.get("position") or track_index,
                "number": source_track.get("number"),
                "title": recording.get("title") or source_track.get("title", ""),
                "recording_id": recording.get("id"),
                "length_ms": source_track.get("length") if source_track.get("length") is not None else recording.get("length"),
            }
            tracks.append(normalized)
            flattened.append({"medium_position": medium_position, **normalized})
        media.append({"position": medium_position, "format": source_medium.get("format"),
                      "track_count": source_medium.get("track-count", len(tracks)), "tracks": tracks})
    date = str(release.get("date") or "") or None
    release_group = release.get("release-group") or {}
    score = release.get("score")
    try:
        search_score = max(0, min(int(score), 100)) if score is not None and not exact else None
    except (TypeError, ValueError):
        search_score = None
    return {
        "provider_id": release.get("id", ""), "release_group_id": release_group.get("id"),
        "artist": artist, "album": release.get("title", ""), "date": date,
        "year": date[:4] if date else None, "track_count": release.get("track-count", len(flattened)),
        "media": media, "tracks": flattened,
        "retrieval": {"search_score": search_score, "source": "musicbrainz-search" if not exact else "release-id"},
    }


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
