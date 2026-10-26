"""Match diagnostics are bounded, precise, operation-scoped and metadata-only."""
from io import StringIO
import json
import logging

import pytest
from beets_mvp import match_diagnostics as diagnostics
from beets_mvp.matching import score_release
from beets_mvp.musicbrainz import ProviderError
from test_app import exact_candidate
from test_matching_progress import fixture


@pytest.fixture
def records(monkeypatch):
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    monkeypatch.setattr(diagnostics.logger, "handlers", [handler])
    return lambda: [json.loads(line) for line in stream.getvalue().splitlines()]


def test_logs_precise_typography_evidence_without_paths_or_provider_payload(records):
    query = {"album": "We're Here", "artist": "Artist", "tracks": []}
    candidate = {"album": "We’re Here", "artist": "Artist", "provider_id": "release", "tracks": [],
                 "raw_url": "https://secret:password@example.test", "path": "/private/media"}
    score = score_release(query, candidate)
    diagnostics.emit("matching.completed", source="library", review_id=1, operation="search",
                     candidates=[diagnostics.evidence(query, candidate, score)])
    row = records()[0]
    evidence = row['candidates'][0]
    assert evidence['confidence'] == score['confidence'] < 1
    assert evidence['penalties'][1]['penalty'] > 0
    assert evidence['local']['album'] == "We're Here" and evidence['proposed']['album'] == "We’re Here"
    assert 'password' not in json.dumps(row) and '/private/media' not in json.dumps(row)


def test_track_details_are_bounded_and_failures_do_not_log_exception_text(records):
    query = {"tracks": [{"title": "Local"}] * 40}
    candidate = {"tracks": [{"title": "Proposed"}] * 40}
    evidence = diagnostics.evidence(query, candidate, score_release(query, candidate))
    assert len(evidence['track_details']) == 20 and evidence['omitted_track_details'] == 20
    with pytest.raises(ProviderError):
        with diagnostics.attempt(source="inbox", review_id=2, operation="exact-id"):
            raise ProviderError('/private/path https://user:password@example.test', code='provider_cooldown', retryable=True, upstream_status=503)
    failure = records()[-1]
    assert failure['event'] == 'matching.failed' and failure['upstream_status'] == 503
    assert failure['retryable'] and 'password' not in json.dumps(failure)


@pytest.mark.parametrize('source', ['inbox', 'library'])
def test_search_exact_and_reuse_log_once_but_queue_reads_do_not(tmp_path, source, records):
    app, client, path, endpoint, body = fixture(tmp_path, source)
    release_id = '123e4567-e89b-42d3-a456-426614174000'
    app.config['MUSICBRAINZ_PROVIDER'] = lambda query, *, limit: [exact_candidate(query, release_id)]
    before = path.read_bytes()
    response = client.post(endpoint, json=body)
    assert response.status_code == 201
    events = records()
    assert [r['event'] for r in events] == ['matching.started', 'matching.completed']
    assert events[-1]['candidates'][0]['confidence'] == 1
    client.get('/api/inbox/reviews' if source == 'inbox' else '/api/library-import/queue')
    assert records() == events
    assert client.post(endpoint, json=body).status_code == 201
    assert records()[-1]['event'] == 'matching.reused'
    rematch = f'/api/{"inbox" if source == "inbox" else "library-import"}/albums/{body["album_id"]}/rematch'
    assert client.post(rematch, json={'musicbrainz_id': release_id}).status_code == 200
    assert records()[-1]['event'] == 'matching.completed' and records()[-1]['operation'] == 'exact-id'
    assert path.read_bytes() == before
