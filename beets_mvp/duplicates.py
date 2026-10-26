"""Recording identity within an album context, shared by review and execution."""
from collections import Counter
import unicodedata


def _text(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def _album(value):
    return (
        _text(value.get('release_group_id') or value.get('mb_releasegroupid')),
        _text(value.get('provider_id') or value.get('mb_albumid') or value.get('release_mbid')),
        _text(value.get('album')),
        _text(value.get('albumartist') or value.get('artist')),
    )


def _same_album(incoming, managed):
    group, release, title, artist = _album(incoming)
    other_group, other_release, other_title, other_artist = _album(managed)
    if group and other_group:
        return group == other_group
    if release and other_release and release == other_release:
        return True
    return bool(title and artist and title == other_title and artist == other_artist)


def _position(item):
    try:
        track = int(item.get('track') or 0)
        return (int(item.get('disc') or 1), track) if track > 0 else None
    except (ValueError, TypeError):
        return None


def duplicate_matches(items, recording_id, incoming, detail=None):
    """Never infer deletion/replacement identity from recording ID alone.

    Separate releases of one group may collide. Separate occurrences of a repeated
    recording remain distinct by disc/track position; unknown positions stay
    conservative and cannot disambiguate multiple managed targets.
    """
    recording_id = str(recording_id or '').strip()
    if not recording_id:
        return []
    matches = [item for item in items if str(item.get('mb_trackid') or '').strip() == recording_id
               and _same_album(incoming, item)]
    repeated = sum(str(track.get('recording_id') or '').strip() == recording_id
                   for track in incoming.get('tracks', []) if isinstance(track, dict)) > 1
    album_counts = Counter(item.get('album_id') or _album(item)[1:] for item in matches if _position(item))
    repeated = repeated or any(count > 1 for count in album_counts.values())
    position = (detail or {}).get('proposed_position')
    if repeated and position and len(position) == 2:
        position = tuple(int(value) for value in position)
        matches = [item for item in matches if _position(item) is None or _position(item) == position]
    return matches
