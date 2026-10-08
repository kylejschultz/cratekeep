"""Smoke-only WSGI harness with deterministic server-side CAA bytes.

The injection is a Python config callback supplied directly to ``create_app``.
Production startup has no environment, HTTP, or persisted-data switch that can
activate it, so the seam cannot turn provider URLs into an SSRF path.
"""

from __future__ import annotations

import base64
import time

from beets_mvp import create_app as create_cratekeep_app
from beets_mvp.musicbrainz import ProviderError


ARTWORK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/"
    "2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAMAAwDASIAAhEBAxEB/"
    "8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
    "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
    "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
    "goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD53ooo"
    "r9APzI//2Q=="
)

EXISTING_ARTWORK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIUFRT/"
    "2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAAMAAwDASIAAhEBAxEB/"
    "8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2Jy"
    "ggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLD"
    "xMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3"
    "AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6"
    "goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD5Xooo"
    "rwj+kD//2Q=="
)


def deterministic_artwork_fetcher(candidate: dict) -> bytes:
    artwork = candidate.get("artwork")
    assert isinstance(artwork, dict) and artwork.get("source") == "cover-art-archive"
    return ARTWORK_JPEG


def deterministic_musicbrainz_provider(query: dict, *, limit: int) -> list[dict]:
    """Resolve exact release IDs without external traffic for browser smoke tests."""
    release_id = query.get("musicbrainz_id")
    if not release_id:
        if query["album"] in {"Safe Match", "Sequential First", "Sequential Second", "Provider Error"}:
            time.sleep(0.15)  # Keep inline scan state observable in the real-browser smoke.
        if query["album"] == "Provider Error":
            raise ProviderError("deterministic folder-queue provider error", code="fixture_error")
        base = {
            "provider_id": "queue-release-1", "artist": query["artist"], "album": query["album"],
            "tracks": [{"title": track["title"], "track_artist": track.get("artist"),
                        "position": index, "medium_position": 1,
                        "length_ms": round(float(track.get("duration") or 1) * 1000)}
                       for index, track in enumerate(query.get("tracks", []), 1)],
        }
        if query["album"] == "Tied Match":
            return [base, {**base, "provider_id": "queue-release-2"}]
        if query["album"] == "Lower Match":
            base["artist"] = f'{query["artist"]} Different'
        if query["album"] in {"Sequential First", "Sequential Second"}:
            base["artist"] = f'{query["artist"]} Different'
        return [base]
    assert limit == 1
    if release_id == "123e4567-e89b-42d3-a456-426614174011":
        return [{
            "provider_id": release_id, "artist": query["artist"], "album": query["album"],
            "track_count": 0, "media": [], "tracks": [],
            "retrieval": {"search_score": None, "source": "release-id"},
        }]
    candidate = {
        "provider_id": release_id,
        "artist": query["artist"],
        "album": f'{query["album"]} (MBID Edition)' if release_id.endswith("4077") else query["album"],
        "date": "2024-03-02",
        "year": "2024",
        "release_type": "Album",
        "country": "GB",
        "track_count": len(query.get("tracks", [])),
        "media": [{"position": 1, "format": "CD", "track_count": len(query.get("tracks", []))}],
        "tracks": [
            {"title": track.get("title"), "position": index, "medium_position": 1,
             "track_artist": track.get("artist"), "recording_id": f"managed-recording-{index}"}
            for index, track in enumerate(query.get("tracks", []), 1)
        ],
        "artwork": {
            "available": True,
            "source": "cover-art-archive",
            "entity": "release",
            "mbid": release_id,
            "thumbnail_url": f"https://coverartarchive.org/release/{release_id}/front-250",
        },
        "retrieval": {"search_score": None, "source": "release-id"},
    }
    if release_id == "123e4567-e89b-42d3-a456-426614174012":
        candidate["artwork"] = {
            "available": False, "source": "cover-art-archive", "status": "missing",
        }
    if release_id == "123e4567-e89b-42d3-a456-426614174010":
        candidate["album"] = "BULLY"
        candidate["release_disambiguation"] = "deluxe, clean"
        candidate["tracks"] = [{
            "title": "Blessings", "track_artist": "Big Sean feat. Drake",
            "artist_credit_source": "track", "position": 1, "medium_position": 1,
            "length_ms": 1000, "recording_id": "recording-blessings-deluxe",
        }]
    if release_id == "123e4567-e89b-42d3-a456-426614174002":
        candidate["genre"] = "electronic"
        candidate["genre_evidence"] = {
            "source": "musicbrainz-release-group-genres",
            "rule": "top positive canonical genre has at least 2 votes and strictly exceeds runner-up",
            "counts": [{"name": "electronic", "count": 4}, {"name": "rock", "count": 1}],
            "selected": "electronic",
        }
    return [candidate]


def create_app():
    return create_cratekeep_app({
        "ARTWORK_FETCHER": deterministic_artwork_fetcher,
        "MUSICBRAINZ_PROVIDER": deterministic_musicbrainz_provider,
    })
