"""Bounded match evidence in the ordinary container stream, with no file logging."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import sys

logger = logging.getLogger("cratekeep.matching")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def emit(event, *, source, review_id, operation, **fields):
    logger.info("%s", json.dumps({"event": event, "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source, "review_id": review_id, "operation": operation, **fields},
        ensure_ascii=True, separators=(",", ":")))


@contextmanager
def attempt(*, source, review_id, operation):
    """Record failures without leaking exception messages, URLs or filesystem paths."""
    context = dict(source=source, review_id=review_id, operation=operation)
    emit("matching.started", **context)
    try:
        yield
    except Exception as error:
        emit("matching.failed", **context, code=str(getattr(error, "code", "match_failed")),
             error_type=type(error).__name__, retryable=bool(getattr(error, "retryable", False)),
             upstream_status=getattr(error, "upstream_status", None))
        raise


def evidence(query, candidate, match):
    """Use already-computed scores; cap metadata lengths and detailed track rows."""
    text = lambda value: str(value or "")[:256]
    changes = [row for row in match["track_details"] if row.get("distance", 0) > 0
               or row.get("local") != row.get("proposed")
               or row.get("local_artist") != row.get("proposed_artist")]
    details = [{key: (text(row.get(key)) if key in {"local", "proposed", "local_artist", "proposed_artist"}
                     else row.get(key)) for key in ("position", "status", "distance", "issues", "local", "proposed",
                     "local_artist", "proposed_artist", "local_duration", "proposed_duration", "current_position", "proposed_position")}
               for row in changes[:20]]
    return {"release_id": text(candidate.get("provider_id")), "confidence": match["confidence"],
        "penalties": match["penalties"], "hard_mismatches": match["hard_mismatches"],
        "local": {key: text(query.get(key)) for key in ("artist", "album", "year", "date")},
        "proposed": {key: text(candidate.get(key)) for key in ("artist", "album", "year", "date")},
        "local_track_count": len(query.get("tracks") or []), "matched_track_count": match["matched_track_count"],
        "unmatched_track_count": match["unmatched_track_count"], "changed_track_count": len(changes),
        "track_details": details, "omitted_track_details": max(0, len(changes) - len(details))}
