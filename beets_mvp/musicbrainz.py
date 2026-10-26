"""Small, bounded MusicBrainz release-search client.

The app injects a provider in tests and deployments that need custom transport.
This default implementation deliberately performs only metadata reads.
"""

from __future__ import annotations

import json
import math
import re
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .metadata_cache import MetadataCache
from .matching import score_release

CAA_BASE = "https://coverartarchive.org"
_RATE_LOCK = threading.Lock()
_LAST_MUSICBRAINZ_REQUEST = 0.0
_MUSICBRAINZ_COOLDOWN_UNTIL = 0.0
_REQUEST_BUDGET = ContextVar("musicbrainz_request_budget", default=None)
REQUEST_BUDGET_SECONDS = 30
MAX_RETRIES_PER_MATCH = 2
TRANSIENT_STATUSES = {429, 502, 503, 504}


def cooldown_seconds() -> int:
    """Remaining shared provider cooldown, suitable for a client-side wait."""
    return max(0, math.ceil(_MUSICBRAINZ_COOLDOWN_UNTIL - time.monotonic()))


class _RequestBudget:
    def __init__(self):
        self.deadline = time.monotonic() + REQUEST_BUDGET_SECONDS
        self.retries = MAX_RETRIES_PER_MATCH
        self.requested = False

    def remaining(self):
        return max(0, self.deadline - time.monotonic())


class MusicBrainzClient:
    """One per app: persistent success cache and coalesced concurrent requests."""
    def __init__(self, cache_path: Path):
        self.cache = MetadataCache(cache_path)
        self.lock = threading.Lock()

    def __call__(self, query: dict, *, limit: int = 5, timeout: int = 10):
        refresh = query.get("force_refresh") is True
        def request_json(url, timeout):
            budget = _REQUEST_BUDGET.get()
            if not self.lock.acquire(timeout=budget.remaining()):
                raise ProviderError("MusicBrainz request queue exceeded this match's time budget; try again later.",
                                    retryable=True, code="provider_timeout")
            try:
                cached = self.cache.get(url) if not refresh else None
                if cached is not None:
                    return cached
                document = _request_json(url, timeout)
                parsed = urllib.parse.urlparse(url)
                ttl = 300 if "query" in urllib.parse.parse_qs(parsed.query) else 3600
                if parsed.hostname == "coverartarchive.org":
                    ttl = 900
                self.cache.put(url, document, ttl)
                return document
            finally:
                self.lock.release()
        return search_releases(query, limit=limit, timeout=timeout, _request=request_json)


class ProviderError(RuntimeError):
    """A safe error suitable for persisting with a candidate-generation job."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        code: str = "provider_error",
        upstream_status: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.upstream_status = upstream_status
        self.retry_after_seconds = retry_after_seconds if retry_after_seconds is not None else (cooldown_seconds() if retryable else 0)


def search_releases(query: dict, *, limit: int = 5, timeout: int = 10, _request=None) -> list[dict]:
    """Bound all requests/retries for one album; injected transports stay testable."""
    token = _REQUEST_BUDGET.set(_RequestBudget())
    try:
        return _search_releases(query, limit=limit, timeout=timeout, request_json=_request or _request_json)
    finally:
        _REQUEST_BUDGET.reset(token)


def _search_releases(query: dict, *, limit: int, timeout: int, request_json) -> list[dict]:
    """Return canonical MusicBrainz release data for one album query."""
    limit = max(1, min(int(limit), 10))
    musicbrainz_id = query.get("musicbrainz_id")
    release_group_id = query.get("musicbrainz_release_group_id")
    if musicbrainz_id:
        params = urllib.parse.urlencode({"inc": "artist-credits+recordings+release-groups", "fmt": "json"})
        url = f"https://musicbrainz.org/ws/2/release/{urllib.parse.quote(str(musicbrainz_id))}?{params}"
    elif release_group_id:
        params = urllib.parse.urlencode({
            "release-group": str(release_group_id),
            "inc": "artist-credits+recordings+release-groups", "fmt": "json", "limit": limit,
        })
        url = f"https://musicbrainz.org/ws/2/release/?{params}"
    else:
        releases = _discover_releases(query, limit, timeout, request_json)
        url = None
    if musicbrainz_id or release_group_id:
        document = request_json(url, timeout)
        releases = [document] if musicbrainz_id and document.get("id") else document.get("releases", [])[:limit]
    release_groups: dict[str, dict] = {}
    results = []
    artwork_documents: dict[tuple[str, str], dict | ProviderError] = {}
    for release in releases:
        result = _normalize_release(release, exact=bool(musicbrainz_id or release_group_id))
        if not result["provider_id"]:
            continue
        if release.get("discovery_strategy"):
            result["retrieval"]["strategy"] = release["discovery_strategy"]
        group_id = result.get("release_group_id")
        if group_id:
            if group_id not in release_groups:
                params = urllib.parse.urlencode({"inc": "genres", "fmt": "json"})
                release_groups[group_id] = request_json(
                    f"https://musicbrainz.org/ws/2/release-group/{urllib.parse.quote(group_id)}?{params}", timeout
                )
            result.update(_genre_evidence(release_groups[group_id].get("genres")))
        results.append(result)
    # Resolve required release/genre data before optional artwork can spend the
    # remaining deadline. Failed art evidence remains a non-fatal lookup error.
    for result in results:
        result["artwork"] = _cover_art_evidence(
            result["provider_id"], result.get("release_group_id"), min(timeout, 2),
            cache=artwork_documents, request_json=request_json,
        )
    return results


_MBID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _discover_releases(query, limit, timeout, request_json):
    """Use independent file evidence when strict tags cannot explain the tracks.

    Discovery is capped at ten canonical releases and two recording searches.
    Retrieval scores never become confidence or permission to import.
    """
    detailed = {}

    def search(entity, expression, count):
        params = urllib.parse.urlencode({"query": expression, "fmt": "json", "limit": count})
        return request_json(f"https://musicbrainz.org/ws/2/{entity}/?{params}", timeout).get(entity + "s", [])[:count]

    def hydrate(release, strategy):
        identity = release.get("id")
        if not identity or identity in detailed or len(detailed) >= 10:
            return
        params = urllib.parse.urlencode({"inc": "artist-credits+recordings+release-groups", "fmt": "json"})
        detail = request_json(f"https://musicbrainz.org/ws/2/release/{urllib.parse.quote(str(identity))}?{params}", timeout)
        detail["score"] = release.get("score")
        if strategy != "tags":
            detail["discovery_strategy"] = strategy
        detailed[identity] = detail

    def ranked():
        return sorted(detailed.values(), key=lambda row: -score_release(
            query, _normalize_release(row, exact=False))["confidence"])[:limit]

    def strong():
        return any(score_release(query, _normalize_release(row, exact=False))["confidence"] >= .90
                   for row in detailed.values())

    # An embedded ID is evidence, not an exact-ID user approval. Still score it
    # against every local track, and broaden if the tag points to the wrong album.
    embedded = str(query.get("release_mbid") or "")
    if _MBID.fullmatch(embedded):
        try:
            hydrate({"id": embedded}, "embedded-release-id")
        except ProviderError as exc:
            if exc.upstream_status != 404:
                raise
        if strong():
            return ranked()
    terms = [f'{field}:"{_escape(query[key])}"' for key, field in (("album", "release"), ("artist", "artist"))
             if query.get(key)]
    if terms:
        for release in search("release", " AND ".join(terms), min(limit, 5)):
            hydrate(release, "tags")
    # No synthetic broad search for a query with no usable file evidence.
    if not query.get("tracks") or strong():
        return ranked()
    # Official aliases bridge shortened/local artist credits without requiring
    # that credit to appear verbatim on the release itself.
    if query.get("artist") and query.get("album"):
        artist = _escape(query["artist"])
        artists = search("artist", f'artist:"{artist}" OR alias:"{artist}"', 3)
        identities = list(dict.fromkeys(str(row.get("id")) for row in artists
                          if isinstance(row, dict) and _MBID.fullmatch(str(row.get("id") or ""))))
        if identities:
            expression = f'release:"{_escape(query["album"])}" AND (' + " OR ".join(f"arid:{identity}" for identity in identities) + ")"
            for release in search("release", expression, min(limit, 5)):
                if len(detailed) >= 7:
                    break
                hydrate(release, "artist-alias")
            if strong():
                return ranked()
    titles = list(dict.fromkeys(str(value).strip() for value in
                  [query.get("album"), *(query.get("folder_albums") or [])[:2]] if value and str(value).strip()))
    if titles and (query.get("artist") or len(titles) > 1):
        expression = " OR ".join(f'release:"{_escape(title)}"' for title in titles)
        for release in search("release", expression, min(limit, 5)):
            if len(detailed) >= 7:
                break
            hydrate(release, "album-or-folder")
    if not strong():
        # Longer distinct titles are less ambiguous than e.g. Intro. All tracks,
        # including those not queried, participate in final canonical scoring.
        tracks = [track for track in query.get("tracks", []) if isinstance(track, dict)]
        evidence = {}
        for track in tracks:
            identity = str(track.get("recording_id") or "")
            title = str(track.get("title") or "").strip()
            expression = (f'rid:{identity}' if _MBID.fullmatch(identity) else
                          f'recording:"{_escape(title)}"' if title else None)
            if expression:
                evidence[expression] = len(title) + (10000 if _MBID.fullmatch(identity) else 0)
        for expression in sorted(evidence, key=lambda value: -evidence[value])[:2]:
            if len(detailed) >= 10:
                break
            recordings = search("recording", expression, 5)
            # Round-robin editions: one popular recording cannot fill the cap
            # before evidence from other recording hits is considered.
            before = len(detailed)
            for edition in range(3):
                for recording in recordings:
                    releases = recording.get("releases") or []
                    if edition < len(releases) and len(detailed) < before + 2:
                        hydrate(releases[edition], "recording")
    return ranked()


def _request_stage(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if "query" in urllib.parse.parse_qs(parsed.query):
        entity = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return f"{entity} search"
    if "/release-group/" in parsed.path:
        return "release-group metadata"
    return "release details"


def _retry_delay(headers, attempt: int) -> float:
    fallback = 2.0 * (2 ** attempt)
    value = headers.get("Retry-After", "") if headers else ""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return fallback
    return max(fallback, seconds)


def _request_json(url: str, timeout: int) -> dict:
    global _LAST_MUSICBRAINZ_REQUEST, _MUSICBRAINZ_COOLDOWN_UNTIL
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    musicbrainz = hostname == "musicbrainz.org"
    provider = "MusicBrainz" if musicbrainz else "Cover Art Archive"
    budget = _REQUEST_BUDGET.get() or _RequestBudget()
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "Cratekeep/0.1 (https://github.com/kylejschultz/cratekeep)"},
    )
    attempt = 0
    while True:
        if musicbrainz:
            with _RATE_LOCK:
                if not budget.requested and _MUSICBRAINZ_COOLDOWN_UNTIL > time.monotonic():
                    raise ProviderError(
                        f"MusicBrainz is cooling down after a previous failure during {_request_stage(url)}; try again later.",
                        retryable=True, code="provider_cooldown",
                    )
                delay = max(_LAST_MUSICBRAINZ_REQUEST + 1.0, _MUSICBRAINZ_COOLDOWN_UNTIL) - time.monotonic()
                if delay >= budget.remaining():
                    raise ProviderError(
                        f"MusicBrainz cooldown during {_request_stage(url)} exceeds this match's time budget; try again later or use Search MBID.",
                        retryable=True, code="provider_cooldown",
                    )
                if delay > 0:
                    time.sleep(delay)
                _LAST_MUSICBRAINZ_REQUEST = time.monotonic()
        remaining = budget.remaining()
        if remaining <= 0:
            raise ProviderError(f"{provider} lookup exceeded this match's time budget; try again later.",
                                retryable=True, code="provider_timeout")
        budget.requested = True
        try:
            with urllib.request.urlopen(request, timeout=min(timeout, remaining)) as response:
                payload = response.read(512 * 1024 + 1)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise ProviderError(
                    ("No Cover Art Archive record exists for this identity" if not musicbrainz else
                     "No MusicBrainz release exists for that ID. Check that it is a release ID, not a release-group ID."),
                    code="release_not_found" if musicbrainz else "artwork_not_found", upstream_status=exc.code,
                ) from exc
            message = f"{provider} rate limit reached" if exc.code == 429 else f"{provider} returned HTTP {exc.code}"
            retryable = exc.code == 429 or exc.code >= 500
            retry = musicbrainz and exc.code in TRANSIENT_STATUSES
            delay = _retry_delay(exc.headers, attempt)
            upstream_status = exc.code
            failure = exc
        except (urllib.error.URLError, TimeoutError) as exc:
            message = f"{provider} could not be reached"
            retryable = True
            retry = musicbrainz
            delay = _retry_delay(None, attempt)
            upstream_status = None
            failure = exc
        if retry:
            with _RATE_LOCK:
                _MUSICBRAINZ_COOLDOWN_UNTIL = max(_MUSICBRAINZ_COOLDOWN_UNTIL, time.monotonic() + delay)
        if retry and budget.retries > 0 and delay < budget.remaining():
            budget.retries -= 1
            attempt += 1
            continue
        if musicbrainz:
            message += f" during {_request_stage(url)} after {attempt + 1} attempt(s); try again later or use Search MBID."
        raise ProviderError(
            message, retryable=retryable,
            code=("provider_unavailable" if musicbrainz else "artwork_unavailable") if upstream_status is None else
                 ("provider_http_error" if musicbrainz else "artwork_http_error"),
            upstream_status=upstream_status,
        ) from failure
    try:
        if len(payload) > 512 * 1024:
            raise ValueError("response too large")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ValueError("expected a JSON object")
        if musicbrainz:
            parsed = urllib.parse.urlparse(url)
            if parsed.path.rstrip("/").endswith("/release"):
                valid = isinstance(document.get("releases"), list)
            elif parsed.path.rstrip("/").endswith("/artist"):
                valid = isinstance(document.get("artists"), list)
            elif parsed.path.rstrip("/").endswith("/recording"):
                valid = isinstance(document.get("recordings"), list)
            elif "/release-group/" in parsed.path:
                valid = isinstance(document.get("genres"), list)
            else:
                valid = bool(document.get("id")) and isinstance(document.get("media"), list)
        else:
            valid = isinstance(document.get("images"), list)
        if not valid:
            raise ValueError("metadata response is incomplete")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderError(f"{provider} returned an invalid response",
                            code="provider_invalid_response" if musicbrainz else "artwork_invalid_response") from exc
    return document


def _normalize_release(release: dict, *, exact: bool) -> dict:
    artist = _artist_credit_text(release.get("artist-credit"))
    media = []
    flattened = []
    for medium_index, source_medium in enumerate(release.get("media") or [], 1):
        medium_position = source_medium.get("position") or medium_index
        tracks = []
        for track_index, source_track in enumerate(source_medium.get("tracks") or [], 1):
            recording = source_track.get("recording") or {}
            track_artist = _artist_credit_text(source_track.get("artist-credit"))
            recording_artist = _artist_credit_text(recording.get("artist-credit"))
            if track_artist:
                artist_credit_source = "track"
            elif recording_artist:
                track_artist = recording_artist
                artist_credit_source = "recording"
            else:
                track_artist = artist
                artist_credit_source = "release"
            normalized = {
                "position": source_track.get("position") or track_index,
                "number": source_track.get("number"),
                "title": recording.get("title") or source_track.get("title", ""),
                "recording_id": recording.get("id"),
                "length_ms": source_track.get("length") if source_track.get("length") is not None else recording.get("length"),
                "track_artist": track_artist,
                "artist_credit_source": artist_credit_source,
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
        "release_disambiguation": " ".join(str(release.get("disambiguation") or "").split()),
        "year": date[:4] if date else None, "track_count": release.get("track-count", len(flattened)),
        "release_type": release_group.get("primary-type"), "country": release.get("country"),
        "media": media, "tracks": flattened,
        "retrieval": {"search_score": search_score, "source": "musicbrainz-search" if not exact else "release-id"},
    }


def _artist_credit_text(credit: object) -> str:
    """Join a MusicBrainz artist-credit without inventing malformed parts."""
    if not isinstance(credit, list):
        return ""
    pieces = []
    for part in credit:
        if not isinstance(part, dict):
            return ""
        name = part.get("name")
        if not isinstance(name, str) or not name.strip():
            artist = part.get("artist")
            name = artist.get("name") if isinstance(artist, dict) else ""
        if not isinstance(name, str) or not name.strip():
            return ""
        joinphrase = part.get("joinphrase")
        pieces.append(name.strip() + (joinphrase if isinstance(joinphrase, str) else ""))
    return "".join(pieces).strip()


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
    request_json=None,
) -> dict:
    """Return bounded CAA evidence without making optional art fatal to metadata."""
    request_json = request_json or _request_json
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
                document = request_json(f"{CAA_BASE}/{entity}/{urllib.parse.quote(mbid)}/", timeout)
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
