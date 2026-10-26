"""Deterministic transport/cache tests: no live traffic or wall-clock sleeps."""
import json
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import threading
import urllib.error

import pytest

import beets_mvp.musicbrainz as mb
import beets_mvp.metadata_cache as cache_module


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []
    def monotonic(self):
        return self.now
    def time(self):
        return 1_700_000_000 + self.now
    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, document):
        self.payload = document if isinstance(document, bytes) else json.dumps(document).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, limit):
        return self.payload[:limit]


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(mb, "time", clock)
    monkeypatch.setattr(cache_module, "time", clock)
    return clock


def release():
    return {"id": "release-1", "title": "Album", "artist-credit": [{"name": "Artist"}],
            "release-group": {"id": "group-1"}, "media": [{"tracks": [
                {"position": 1, "recording": {"id": "recording-1", "title": "Song"}}
            ]}]}


def transport(calls):
    def open_request(request, timeout):
        calls.append((request.full_url, timeout))
        if "coverartarchive.org" in request.full_url:
            return Response({"images": []})
        if "/release-group/" in request.full_url:
            return Response({"genres": []})
        if "query=" in request.full_url:
            return Response({"releases": [{"id": "release-1", "score": 99}]})
        return Response(release())
    return open_request


def test_success_cache_persists_and_does_not_share_mutable_documents(tmp_path, monkeypatch, clock):
    calls = []
    monkeypatch.setattr(mb.urllib.request, "urlopen", transport(calls))
    path = tmp_path / "cache.sqlite3"
    first = mb.MusicBrainzClient(path)({"artist": "Artist", "album": "Album"})
    assert len(calls) == 5  # search, release, genre, release art, group art
    first[0]["tracks"][0]["title"] = "changed by caller"
    again = mb.MusicBrainzClient(path)({"artist": "Artist", "album": "Album"})
    assert len(calls) == 5
    assert again[0]["tracks"][0]["title"] == "Song"
    # Lookup reuses canonical details, never a search-score-mutated object.
    exact = mb.MusicBrainzClient(path)({"musicbrainz_id": "release-1"})
    assert len(calls) == 5 and exact[0]["retrieval"]["search_score"] is None
    clock.now += 301
    mb.MusicBrainzClient(path)({"artist": "Artist", "album": "Album"})
    assert len(calls) == 6  # expired search only; details/genre/art are still current


def test_explicit_refresh_bypasses_cache_and_never_falls_back_to_stale_success(tmp_path, monkeypatch, clock):
    calls = []
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(mb.urllib.request, "urlopen", transport(calls))
    client({"musicbrainz_id": "release-1"})
    before = len(calls)
    client({"musicbrainz_id": "release-1", "force_refresh": True})
    assert len(calls) == before * 2
    def unavailable(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", unavailable)
    with pytest.raises(mb.ProviderError, match="release details"):
        client({"musicbrainz_id": "release-1", "force_refresh": True})


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_retry_is_paced_and_recovers(monkeypatch, clock, status):
    calls = []
    def request(req, timeout):
        calls.append(clock.now)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, status, "temporary", {"Retry-After": "0"}, None)
        return Response({"releases": []})
    monkeypatch.setattr(mb.urllib.request, "urlopen", request)
    assert mb.search_releases({"album": "Album"}) == []
    assert calls == [100, 102, 106]
    assert clock.sleeps == [2, 4]


def test_retry_budget_is_shared_across_search_and_detail_requests(monkeypatch, clock):
    calls = []
    def request(req, timeout):
        calls.append(req.full_url)
        if "query=" in req.full_url and len(calls) == 3:
            return Response({"releases": [{"id": "release-1"}]})
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", request)
    with pytest.raises(mb.ProviderError, match="release details") as failure:
        mb.search_releases({"album": "Album"})
    assert failure.value.upstream_status == 503
    assert len(calls) == 4  # two retries total, not two extra retries per URL


@pytest.mark.parametrize("retry_after", ["120", "date"])
def test_long_retry_after_sets_shared_cooldown_without_sleeping_past_budget(monkeypatch, clock, retry_after):
    if retry_after == "date":
        retry_after = formatdate(clock.time() + 120, usegmt=True)
    calls = []
    def unavailable(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": retry_after}, None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", unavailable)
    with pytest.raises(mb.ProviderError) as initial_failure:
        mb.search_releases({"album": "Album"})
    assert initial_failure.value.retry_after_seconds >= 120
    with pytest.raises(mb.ProviderError) as failure:
        mb.search_releases({"musicbrainz_id": "release-1"})
    assert failure.value.retry_after_seconds >= 120
    assert failure.value.code == "provider_cooldown"
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_errors_are_not_retried_or_cached(tmp_path, monkeypatch, clock, status):
    calls = []
    def error(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, status, "permanent", {}, None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", error)
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    for _ in range(2):
        with pytest.raises(mb.ProviderError):
            client({"musicbrainz_id": "release-1"})
    assert len(calls) == 2


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"{}", b'{"error":"temporary"}', b"x" * (512 * 1024 + 1)])
def test_invalid_or_oversized_responses_are_not_cached(tmp_path, monkeypatch, clock, payload):
    calls = []
    def invalid(req, timeout):
        calls.append(req.full_url)
        return Response(payload)
    monkeypatch.setattr(mb.urllib.request, "urlopen", invalid)
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    for _ in range(2):
        with pytest.raises(mb.ProviderError) as failure:
            client({"musicbrainz_id": "release-1"})
        assert failure.value.code == "provider_invalid_response"
    assert len(calls) == 2


def test_cache_enforces_ttl_entry_and_byte_limits_and_tolerates_corruption(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(cache_module, "MAX_ENTRIES", 2)
    monkeypatch.setattr(cache_module, "MAX_BYTES", 40)
    cache = cache_module.MetadataCache(tmp_path / "cache.sqlite3")
    for key in "abc":
        cache.put(key, {"title": key}, 10)
        clock.now += 1
    assert cache.get("a") is None and cache.get("b") and cache.get("c")
    cache.put("d", {"title": "12345678901234567890"}, 10)
    assert cache.get("b") is None and cache.get("c") is None and cache.get("d")
    clock.now += 11
    assert cache.get("d") is None
    cache.path.write_bytes(b"not a database")
    assert cache.get("d") is None
    cache.put("e", {"title": "e"}, 10)  # Best effort; never fails the metadata request.


def test_concurrent_identical_lookups_share_successful_requests(tmp_path, monkeypatch, clock):
    calls = []
    entered, release_request = threading.Event(), threading.Event()
    normal = transport(calls)
    def request(req, timeout):
        if not entered.is_set():
            entered.set()
            assert release_request.wait(5)
        return normal(req, timeout)
    monkeypatch.setattr(mb.urllib.request, "urlopen", request)
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client, {"musicbrainz_id": "release-1"})
        assert entered.wait(5)
        second = executor.submit(client, {"musicbrainz_id": "release-1"})
        release_request.set()
        assert first.result() == second.result()
    assert len(calls) == 4  # one release, genre and both optional art requests


def test_sustained_outage_is_bounded_and_new_matches_fail_fast_during_cooldown(tmp_path, monkeypatch, clock):
    calls = []
    def unavailable(req, timeout):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
    monkeypatch.setattr(mb.urllib.request, "urlopen", unavailable)
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    with pytest.raises(mb.ProviderError, match="release search"):
        client({"album": "Album"})
    assert len(calls) == 3
    elapsed = clock.now
    with pytest.raises(mb.ProviderError) as failure:
        client({"album": "Different Album"})
    assert failure.value.code == "provider_cooldown"
    assert len(calls) == 3 and clock.now == elapsed
    clock.now += 9
    # Failed responses are not cached: a later request tries again.
    with pytest.raises(mb.ProviderError):
        client({"album": "Album"})
    assert len(calls) == 6


def test_socket_timeouts_and_backoff_share_one_elapsed_time_budget(monkeypatch, clock):
    timeouts = []
    def timeout(req, timeout):
        timeouts.append(timeout)
        clock.now += timeout
        raise urllib.error.URLError("simulated timeout")
    monkeypatch.setattr(mb.urllib.request, "urlopen", timeout)
    with pytest.raises(mb.ProviderError) as failure:
        mb.search_releases({"album": "Album"})
    assert failure.value.code == "provider_unavailable"
    assert timeouts == [10, 10, 4]
    assert clock.now == 130


def test_optional_art_failures_do_not_discard_required_release_and_genre_data(monkeypatch, clock):
    calls = []
    def request(req, timeout):
        calls.append(req.full_url)
        if "coverartarchive.org" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)
        if "/release-group/" in req.full_url:
            return Response({"genres": []})
        if "query=" in req.full_url:
            return Response({"releases": [{"id": "release-1"}, {"id": "release-2"}]})
        document = release()
        if "release-2" in req.full_url:
            document["id"] = "release-2"
            document["release-group"]["id"] = "group-2"
        return Response(document)
    monkeypatch.setattr(mb.urllib.request, "urlopen", request)
    results = mb.search_releases({"album": "Album"})
    assert len(results) == 2
    assert all(result["artwork"]["status"] == "lookup-error" for result in results)
    first_art = next(index for index, url in enumerate(calls) if "coverartarchive.org" in url)
    assert all("release-group" in url for url in calls[3:first_art])
    assert first_art == 5  # search, both release details and both genre requests first


def test_discovery_recovers_artist_mismatch_from_album_and_folder_evidence(monkeypatch, clock):
    calls = []
    def request(url, timeout):
        from urllib.parse import urlparse, parse_qs
        calls.append(url)
        if "/artist/" in url:
            return {"artists": []}
        expression = parse_qs(urlparse(url).query).get("query", [""])[0]
        if expression:
            if ' AND artist:' in expression:
                return {"releases": []}
            assert expression == 'release:"Album" OR release:"Folder Album"'
            return {"releases": [{"id": "release-1"}]}
        if "coverartarchive" in url:
            return {"images": []}
        if "/release-group/" in url:
            return {"genres": []}
        return release()
    query = {"artist": "Bad Artist", "album": "Album", "folder_albums": ["Folder Album"],
             "tracks": [{"title": "Song"}]}
    result = mb.search_releases(query, _request=request)
    assert result[0]["provider_id"] == "release-1"
    assert result[0]["retrieval"]["strategy"] == "album-or-folder"
    assert result[0]["retrieval"]["source"] == "musicbrainz-search"
    assert not any("/recording/" in url for url in calls)


def test_discovery_recovers_wrong_album_and_artist_via_recordings(clock):
    from urllib.parse import urlparse, parse_qs
    calls = []
    def request(url, timeout):
        calls.append(url)
        if "/artist/" in url:
            return {"artists": []}
        if "query=" in url:
            expression = parse_qs(urlparse(url).query)["query"][0]
            if "/recording/" in url:
                assert expression in ['recording:"Distinct Song"', 'recording:"Second Song"']
                return {"recordings": [{"releases": [{"id": "release-1"}, {"id": "release-1"}]}]}
            return {"releases": []}
        if "coverartarchive" in url:
            return {"images": []}
        if "/release-group/" in url:
            return {"genres": []}
        doc = release()
        doc["media"][0]["tracks"][0]["recording"]["title"] = "Distinct Song"
        return doc
    result = mb.search_releases({"artist": "Incorrect", "album": "Incorrect", "tracks": [
        {"title": "Distinct Song", "duration": 123}, {"title": "Second Song", "track": 2}]}, _request=request)
    assert result[0]["retrieval"]["strategy"] == "recording"
    assert result[0]["album"] == "Album"
    assert sum("/recording/" in url for url in calls) == 2
    assert sum("/release/release-1?" in url for url in calls) == 1
    assert result[0]["retrieval"]["search_score"] is None


def test_discovery_uses_embedded_id_without_claiming_user_exact_approval(clock):
    identity = "123e4567-e89b-42d3-a456-426614174000"
    calls = []
    def request(url, timeout):
        calls.append(url)
        if "/artist/" in url:
            return {"artists": []}
        assert "query=" not in url
        if "coverartarchive" in url:
            return {"images": []}
        if "/release-group/" in url:
            return {"genres": []}
        return {**release(), "id": identity}
    result = mb.search_releases({"release_mbid": identity, "artist": "Artist", "album": "Album",
                                "tracks": [{"title": "Song"}]}, _request=request)
    assert result[0]["retrieval"]["strategy"] == "embedded-release-id"
    assert result[0]["retrieval"]["source"] == "musicbrainz-search"


def test_search_outage_never_becomes_empty_or_triggers_broader_search(clock):
    calls = []
    def request(url, timeout):
        calls.append(url)
        if "/artist/" in url:
            return {"artists": []}
        raise mb.ProviderError("MusicBrainz returned HTTP 503", upstream_status=503)
    with pytest.raises(mb.ProviderError, match="503"):
        mb.search_releases({"artist": "Artist", "album": "Album", "tracks": [{"title": "Song"}]}, _request=request)
    assert len(calls) == 1


def test_recording_search_schema_and_cache(monkeypatch, tmp_path, clock):
    calls = []
    def request(req, timeout):
        calls.append(req.full_url)
        return Response({"artists": []} if "/artist/" in req.full_url else
                        {"recordings": []} if "/recording/" in req.full_url else {"releases": []})
    monkeypatch.setattr(mb.urllib.request, "urlopen", request)
    client = mb.MusicBrainzClient(tmp_path / "cache.sqlite3")
    query = {"artist": "Artist", "album": "Album", "tracks": [{"title": "Track"}]}
    assert client(query) == []
    count = len(calls)
    assert count == 4
    assert client(query) == [] and len(calls) == count


def test_discovery_reserves_capacity_for_track_evidence_and_ranks_all_tracks(clock):
    from urllib.parse import urlparse, parse_qs
    hydrated = []
    searches = []
    recording_id = "123e4567-e89b-42d3-a456-426614174000"
    def request(url, timeout):
        if "/artist/" in url:
            return {"artists": []}
        expression = parse_qs(urlparse(url).query).get("query", [""])[0]
        if expression:
            searches.append((url, expression))
            if "/recording/" in url:
                return {"recordings": [{"releases": [{"id": "correct"}, {"id": "wrong-edition"}]}]}
            prefix = "strict" if ' AND artist:' in expression else "album"
            return {"releases": [{"id": f"{prefix}-{n}", "score": 100} for n in range(5)]}
        if "coverartarchive" in url:
            return {"images": []}
        if "/release-group/" in url:
            return {"genres": []}
        identity = urlparse(url).path.split("/")[-1]
        hydrated.append(identity)
        doc = {**release(), "id": identity, "title": "Wrong", "artist-credit": [{"name": "Wrong"}]}
        doc["media"] = [{"tracks": [
            {"position": 1, "recording": {"id": recording_id, "title": "Distinct Song"}},
            {"position": 2, "recording": {"id": "rec2", "title": "Second Song"}},
        ]}] if identity == "correct" else [{"tracks": [{"recording": {"title": "Unrelated"}}]}]
        return doc
    result = mb.search_releases({"artist": "Artist", "album": "Album", "tracks": [
        {"title": 'bad "tag"', "recording_id": recording_id}, {"title": "Second Song"}]},
        limit=5, _request=request)
    assert result[0]["provider_id"] == "correct", "All tracks outweigh equal retrieval scores"
    assert len(result) == 5 and len(hydrated) == len(set(hydrated)) <= 10
    assert any(expression == f"rid:{recording_id}" for url, expression in searches)
    assert sum("/recording/" in url for url, expression in searches) <= 2


def test_artist_alias_discovery_uses_canonical_identity_and_preserves_mismatch(clock):
    from urllib.parse import parse_qs, urlparse
    artist_id = "123e4567-e89b-42d3-a456-426614174009"
    expressions = []
    def request(url, timeout):
        expression = parse_qs(urlparse(url).query).get("query", [""])[0]
        if expression:
            expressions.append(expression)
            if "/artist/" in url:
                assert expression == 'artist:"Short Credit" OR alias:"Short Credit"'
                return {"artists": [{"id": artist_id}, {"id": "invalid"}, {"id": artist_id}]}
            if f"arid:{artist_id}" in expression:
                return {"releases": [{"id": "release-1"}]}
            return {"releases": []}
        if "coverartarchive" in url:
            return {"images": []}
        if "/release-group/" in url:
            return {"genres": []}
        return {**release(), "artist-credit": [{"name": "Canonical Credit"}]}
    result = mb.search_releases({"artist": "Short Credit", "album": "Album", "tracks": [{"title": "Song"}]}, _request=request)
    assert result[0]["provider_id"] == "release-1"
    assert result[0]["artist"] == "Canonical Credit"
    assert result[0]["retrieval"]["strategy"] == "artist-alias"
    assert result[0]["retrieval"]["search_score"] is None
    assert any(expression == f'release:"Album" AND (arid:{artist_id})' for expression in expressions)
