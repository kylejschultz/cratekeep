"""Small, bounded MusicBrainz release-search client.

The app injects a provider in tests and deployments that need custom transport.
This default implementation deliberately performs only metadata reads.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CAA_BASE = "https://coverartarchive.org"
_RATE_LOCK = threading.Lock()
_LAST_MUSICBRAINZ_REQUEST = 0.0


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
    release_groups: dict[str, dict] = {}
    results = []
    artwork_documents: dict[tuple[str, str], dict | ProviderError] = {}
    for release in releases:
        result = _normalize_release(release, exact=bool(musicbrainz_id))
        if not result["provider_id"]:
            continue
        group_id = result.get("release_group_id")
        if group_id:
            if group_id not in release_groups:
                params = urllib.parse.urlencode({"inc": "genres", "fmt": "json"})
                release_groups[group_id] = _request_json(
                    f"https://musicbrainz.org/ws/2/release-group/{urllib.parse.quote(group_id)}?{params}", timeout
                )
            result.update(_genre_evidence(release_groups[group_id].get("genres")))
        result["artwork"] = _cover_art_evidence(
            result["provider_id"], group_id, timeout, cache=artwork_documents
        )
        results.append(result)
    return results


def _request_json(url: str, timeout: int) -> dict:
    global _LAST_MUSICBRAINZ_REQUEST
    if urllib.parse.urlparse(url).hostname == "musicbrainz.org":
        with _RATE_LOCK:
            delay = 1.0 - (time.monotonic() - _LAST_MUSICBRAINZ_REQUEST)
            if delay > 0:
                time.sleep(delay)
            _LAST_MUSICBRAINZ_REQUEST = time.monotonic()
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Cratekeep/0.1 (https://github.com/kylejschultz/cratekeep)"},
    )
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    provider = "Cover Art Archive" if hostname == "coverartarchive.org" else "MusicBrainz"
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ProviderError(
                ("No Cover Art Archive record exists for this identity"
                 if provider == "Cover Art Archive" else
                 "No MusicBrainz release exists for that ID. Check that it is a release ID, not a release-group ID."),
                code="artwork_not_found" if provider == "Cover Art Archive" else "release_not_found",
                upstream_status=exc.code,
            ) from exc
        if exc.code == 429:
            raise ProviderError(
                f"{provider} rate limit reached; try again later",
                retryable=True,
                code="artwork_http_error" if provider == "Cover Art Archive" else "provider_http_error",
                upstream_status=exc.code,
            ) from exc
        raise ProviderError(
            f"{provider} returned HTTP {exc.code}",
            retryable=exc.code >= 500,
            code="artwork_http_error" if provider == "Cover Art Archive" else "provider_http_error",
            upstream_status=exc.code,
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError(
            f"{provider} could not be reached", retryable=True,
            code="artwork_unavailable" if provider == "Cover Art Archive" else "provider_unavailable",
        ) from exc
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            f"{provider} returned an invalid response",
            code="artwork_invalid_response" if provider == "Cover Art Archive" else "provider_invalid_response",
        ) from exc

    if not isinstance(document, dict):
        raise ProviderError(
            f"{provider} returned an invalid response",
            code="artwork_invalid_response" if provider == "Cover Art Archive" else "provider_invalid_response",
        )
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
        "release_type": release_group.get("primary-type"), "country": release.get("country"),
        "media": media, "tracks": flattened,
        "retrieval": {"search_score": search_score, "source": "musicbrainz-search" if not exact else "release-id"},
    }


def _genre_evidence(genres: object) -> dict:
    """Choose one canonical MusicBrainz genre only from decisive positive evidence."""
    evidence = []
    if isinstance(genres, list):
        for genre in genres:
            if not isinstance(genre, dict):
                continue
            name = " ".join(str(genre.get("name") or "").split())
            try:
                count = int(genre.get("count", 0))
            except (TypeError, ValueError):
                continue
            if name and count > 0:
                evidence.append({"name": name, "count": count})
    evidence.sort(key=lambda item: (-item["count"], item["name"].casefold(), item["name"]))
    top = evidence[0] if evidence else None
    runner_up = evidence[1] if len(evidence) > 1 else None
    selected = top["name"] if top and top["count"] >= 2 and (runner_up is None or top["count"] > runner_up["count"]) else None
    return {
        "genre": selected,
        "genre_evidence": {
            "source": "musicbrainz-release-group-genres",
            "rule": "top positive canonical genre has at least 2 votes and strictly exceeds runner-up",
            "counts": evidence,
            "selected": selected,
        },
    }


def _cover_art_evidence(
    release_id: str,
    release_group_id: str | None,
    timeout: int,
    *,
    cache: dict[tuple[str, str], dict | ProviderError] | None = None,
) -> dict:
    """Return bounded CAA evidence without making optional art fatal to metadata."""
    attempts = []
    for entity, mbid in (("release", release_id), ("release-group", release_group_id)):
        if not mbid:
            continue
        key = (entity, mbid)
        try:
            if cache is not None and key in cache:
                document = cache[key]
                if isinstance(document, ProviderError):
                    raise document
            else:
                document = _request_json(f"{CAA_BASE}/{entity}/{urllib.parse.quote(mbid)}/", timeout)
            if cache is not None and key not in cache:
                cache[key] = document
        except ProviderError as exc:
            if cache is not None:
                cache[key] = exc
            attempts.append({
                "entity": entity, "mbid": mbid,
                "status": "missing" if exc.code == "artwork_not_found" else "lookup-error",
                "code": exc.code, "upstream_status": exc.upstream_status,
                "retryable": exc.retryable,
            })
            continue
        images = document.get("images") if isinstance(document, dict) else None
        front = next((image for image in images or [] if _is_front_image(image)), None)
        if front:
            thumbnails = front.get("thumbnails") if isinstance(front.get("thumbnails"), dict) else {}
            thumbnail = thumbnails.get("250") or thumbnails.get("small")
            return {
                "available": True,
                "source": "cover-art-archive",
                "entity": entity,
                "mbid": mbid,
                "thumbnail_url": thumbnail if _trusted_caa_url(thumbnail) else f"{CAA_BASE}/{entity}/{mbid}/front-250",
                "status": "available",
                "attempts": attempts + [{"entity": entity, "mbid": mbid, "status": "available"}],
            }
        attempts.append({"entity": entity, "mbid": mbid, "status": "missing"})
    had_error = any(attempt["status"] == "lookup-error" for attempt in attempts)
    return {
        "available": False,
        "source": "cover-art-archive",
        "status": "lookup-error" if had_error else "missing",
        "message": ("Artwork lookup was temporarily unavailable; metadata is still usable."
                    if had_error else "No front artwork is available from Cover Art Archive."),
        "attempts": attempts,
    }


def _is_front_image(image: object) -> bool:
    """CAA normally supplies both fields; accept either explicit front marker."""
    if not isinstance(image, dict):
        return False
    types = image.get("types")
    typed_front = isinstance(types, list) and any(
        isinstance(value, str) and value.casefold() == "front" for value in types
    )
    return image.get("front") is True or typed_front


def _trusted_caa_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "coverartarchive.org" or host == "archive.org" or host.endswith(".archive.org"))


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')
