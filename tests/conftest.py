import pytest


@pytest.fixture(autouse=True)
def isolated_musicbrainz_throttle(monkeypatch):
    # Transport tests must not leave a simulated Retry-After on other tests.
    import beets_mvp.musicbrainz as musicbrainz
    monkeypatch.setattr(musicbrainz, "_LAST_MUSICBRAINZ_REQUEST", 0.0)
    monkeypatch.setattr(musicbrainz, "_MUSICBRAINZ_COOLDOWN_UNTIL", 0.0)
