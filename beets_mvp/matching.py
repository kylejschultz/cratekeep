"""Deterministic, metadata-only MusicBrainz release matching.

The model follows beets' distance/recommendation shape without importing or
mutating a beets library. MusicBrainz search scores are deliberately excluded:
they rank retrieval results, not the quality of a release-to-library match.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata


FIELD_WEIGHTS = {"artist": 0.20, "album": 0.30, "date": 0.10, "tracks": 0.40}
TRACK_WEIGHTS = {"title": 0.70, "duration": 0.20, "order": 0.10}


def _text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()


def _key(value: object) -> str:
    return _text(value).casefold()


def _similarity(left: object, right: object) -> float:
    return SequenceMatcher(None, _key(left), _key(right)).ratio()


def _track_title(track: object) -> str:
    value = track.get("title") if isinstance(track, dict) else track
    title = _text(value)
    return re.sub(r"^\s*(?:\d+[.-]?\s*[-–—.]?\s*)", "", title) or title


def _duration(track: object) -> float | None:
    if not isinstance(track, dict):
        return None
    value = track.get("duration")
    if value is None and track.get("length_ms") is not None:
        value = float(track["length_ms"]) / 1000
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _position(track: object, fallback: int) -> tuple[int, int]:
    if not isinstance(track, dict):
        return (1, fallback)
    try:
        return (int(track.get("medium_position") or track.get("disc") or 1),
                int(track.get("position") or track.get("track") or fallback))
    except (TypeError, ValueError):
        return (1, fallback)


def _candidate_tracks(candidate: dict) -> list[dict]:
    tracks = candidate.get("tracks")
    if isinstance(tracks, list):
        return [track if isinstance(track, dict) else {"title": track} for track in tracks]
    flattened = []
    for medium in candidate.get("media") or []:
        if not isinstance(medium, dict):
            continue
        for track in medium.get("tracks") or []:
            if isinstance(track, dict):
                flattened.append({"medium_position": medium.get("position"), **track})
    return flattened


def _pair_metrics(local: object, canonical: object, local_index: int, canonical_index: int) -> dict:
    title_distance = 1 - _similarity(_track_title(local), _track_title(canonical))
    local_duration, canonical_duration = _duration(local), _duration(canonical)
    duration_delta = None if local_duration is None or canonical_duration is None else abs(local_duration - canonical_duration)
    duration_distance = 0.0 if duration_delta is None else min(duration_delta / 30.0, 1.0)
    local_position = _position(local, local_index + 1)
    canonical_position = _position(canonical, canonical_index + 1)
    order_distance = 0.0 if local_position == canonical_position else 1.0
    distance = (title_distance * TRACK_WEIGHTS["title"] + duration_distance * TRACK_WEIGHTS["duration"]
                + order_distance * TRACK_WEIGHTS["order"])
    issues = []
    if title_distance > 0.18:
        issues.append("title-mismatch")
    if duration_delta is not None and duration_delta > 3:
        issues.append("duration-mismatch")
    if order_distance:
        issues.append("order-mismatch")
    status = issues[0] if issues else "matched"
    return {"distance": distance, "title_distance": title_distance, "duration_delta": duration_delta,
            "local_position": local_position, "canonical_position": canonical_position,
            "status": status, "issues": issues}


def _assign_tracks(local_tracks: list, canonical_tracks: list) -> tuple[list[dict], float]:
    edges = []
    for local_index, local in enumerate(local_tracks):
        for canonical_index, canonical in enumerate(canonical_tracks):
            metrics = _pair_metrics(local, canonical, local_index, canonical_index)
            edges.append((metrics["distance"], local_index, canonical_index, metrics))
    assigned_local, assigned_canonical, pairs = set(), set(), []
    for _, local_index, canonical_index, metrics in sorted(edges, key=lambda edge: edge[:3]):
        if local_index in assigned_local or canonical_index in assigned_canonical:
            continue
        # Very weak pairs are clearer as unmatched than as a misleading match.
        if metrics["distance"] > 0.78:
            continue
        assigned_local.add(local_index)
        assigned_canonical.add(canonical_index)
        pairs.append((local_index, canonical_index, metrics))

    # Preserve an explicit unmatched relationship for the weakest remaining
    # one-to-one pairs; true cardinality differences remain missing/extra.
    remaining_local = [index for index in range(len(local_tracks)) if index not in assigned_local]
    remaining_canonical = [index for index in range(len(canonical_tracks)) if index not in assigned_canonical]
    unmatched = []
    for local_index, canonical_index in zip(remaining_local, remaining_canonical):
        metrics = _pair_metrics(local_tracks[local_index], canonical_tracks[canonical_index], local_index, canonical_index)
        metrics["status"] = "unmatched"
        metrics["issues"] = [*metrics["issues"], "unmatched"]
        assigned_local.add(local_index)
        assigned_canonical.add(canonical_index)
        unmatched.append((local_index, canonical_index, metrics))
    pairs.extend(unmatched)

    details = []
    for local_index, canonical_index, metrics in sorted(pairs):
        local, canonical = local_tracks[local_index], canonical_tracks[canonical_index]
        details.append({
            "position": local_index + 1, "local": _track_title(local), "proposed": _track_title(canonical),
            "local_duration": _duration(local), "proposed_duration": _duration(canonical),
            "recording_id": canonical.get("recording_id") if isinstance(canonical, dict) else None,
            "status": metrics["status"], "issues": metrics["issues"], "distance": round(metrics["distance"], 4),
        })
    for index, track in enumerate(local_tracks):
        if index not in assigned_local:
            details.append({"position": index + 1, "local": _track_title(track), "proposed": None,
                            "status": "missing", "issues": ["missing"], "distance": 1.0})
    for index, track in enumerate(canonical_tracks):
        if index not in assigned_canonical:
            details.append({"position": index + 1, "local": None, "proposed": _track_title(track),
                            "recording_id": track.get("recording_id") if isinstance(track, dict) else None,
                            "status": "extra", "issues": ["extra"], "distance": 1.0})
    details.sort(key=lambda item: (item["position"], item["local"] is None, item.get("proposed") or ""))
    denominator = max(len(local_tracks), len(canonical_tracks), 1)
    distance = (sum(metrics["distance"] for _, _, metrics in pairs)
                + len(local_tracks) - len(assigned_local) + len(canonical_tracks) - len(assigned_canonical)) / denominator
    return details, min(distance, 1.0)


def score_release(query: dict, candidate: dict, *, exact_mbid: bool = False) -> dict:
    """Return bounded confidence, beets-style recommendation, and explainable penalties."""
    local_tracks = query.get("tracks") if isinstance(query.get("tracks"), list) else []
    canonical_tracks = _candidate_tracks(candidate)
    track_details, track_distance = _assign_tracks(local_tracks, canonical_tracks)
    artist_distance = 1 - _similarity(query.get("artist"), candidate.get("artist"))
    album_distance = 1 - _similarity(query.get("album"), candidate.get("album"))
    try:
        local_year = int(query.get("year")) if query.get("year") else None
        canonical_year = int(candidate.get("year")) if candidate.get("year") else None
    except (TypeError, ValueError):
        local_year = canonical_year = None
    if local_year is None or canonical_year is None:
        date_distance = 0.0
    else:
        date_distance = min(abs(local_year - canonical_year) / 4, 1.0)
        if date_distance == 0 and query.get("date") and candidate.get("date"):
            # A same-year edition with a different known month/day is a small,
            # explicit penalty rather than an album-level rejection.
            date_distance = 0.0 if str(query["date"]) == str(candidate["date"]) else 0.15
    components = {"artist": artist_distance, "album": album_distance, "date": date_distance, "tracks": track_distance}
    penalties = [{"field": field, "distance": round(value, 4), "weight": weight,
                  "penalty": round(value * weight, 4)} for field, weight in FIELD_WEIGHTS.items()
                 for value in [components[field]]]
    distance = min(sum(item["penalty"] for item in penalties), 1.0)
    confidence = 1.0 - distance
    count_mismatch = len(local_tracks) != len(canonical_tracks)
    if exact_mbid:
        confidence, distance, recommendation = 1.0, 0.0, "strong"
        penalties.insert(0, {"field": "release_mbid", "distance": 0.0, "weight": 1.0, "penalty": 0.0})
    else:
        recommendation = "strong" if distance <= 0.15 else "medium" if distance <= 0.30 else "low" if distance <= 0.45 else "none"
    if count_mismatch:
        confidence = min(confidence, 0.94 if not exact_mbid else 0.99)
        distance = max(distance, 1.0 - confidence)
    reasons = [f"{item['field'].replace('_', ' ').title()} penalty: {round(item['penalty'] * 100)}%" for item in penalties]
    reasons.append("Track count matches" if not count_mismatch else
                   f"Track count differs ({len(local_tracks)} local, {len(canonical_tracks)} MusicBrainz)")
    if exact_mbid:
        reasons.insert(0, "Exact MusicBrainz release ID override")
    return {"confidence": round(max(0.0, min(confidence, 1.0)), 4), "distance": round(distance, 4),
            "recommendation": recommendation, "penalties": penalties, "reasons": reasons,
            "track_details": track_details, "track_count_mismatch": count_mismatch}
