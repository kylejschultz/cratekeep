"""Metadata disagreement is advisory; selected imports retain all local files."""
from pathlib import Path
import pytest
from beets.library import Library
from mediafile import MediaFile
from test_app import tagged_inbox_wav, exact_candidate
from test_inbox_copies import app, imports_base


@pytest.mark.parametrize('source',['inbox','library-import'])
@pytest.mark.parametrize('mismatch',['fewer','extra','different'])
def test_selected_import_warns_and_proceeds_with_mismatched_release(app,tmp_path,source,mismatch):
    root=tmp_path/('inbox' if source=='inbox' else 'library')
    paths=[]
    for index,title in enumerate(['First','Second','Third'],1):
        path=root/f'Incoming/{index}.wav';tagged_inbox_wav(path,artist='Local Artist',album='Local Album',title=title,track=index)
        media=MediaFile(path);media.tracktotal=3;media.mb_trackid='123e4567-e89b-42d3-a456-426614174099';media.save();paths.append(path)
    originals=[path.read_bytes() for path in paths]
    client=app.test_client();base=imports_base(app) if source=='inbox' else '/api/library-import'
    refresh=base+'/scopes/refresh' if source=='inbox' else base+'/queue/refresh'
    assert client.post(refresh,json={}).status_code in {200,201}
    album=client.get(base+'/queue').json['albums'][0]
    def provider(query,*,limit):
        candidate=exact_candidate(query,'123e4567-e89b-42d3-a456-426614174001')
        candidate.update(artist='Chosen Artist',album='Chosen Album')
        if mismatch=='fewer': candidate['tracks']=candidate['tracks'][:2]
        elif mismatch=='extra': candidate['tracks'].append({'title':'Bonus','position':4,'length_ms':1000})
        else: candidate['tracks'][1]['title']='Completely unrelated title'
        candidate['track_count']=len(candidate['tracks'])
        return [candidate]
    app.config['MUSICBRAINZ_PROVIDER']=provider
    matched=client.post('/api/inbox/candidates' if source=='inbox' else '/api/library-import/candidates',json={'album_id':album['id']})
    assert matched.status_code==201,matched.json
    candidate=matched.json['album']['candidates'][0]
    assert client.patch(f'/api/{"inbox" if source=="inbox" else "library-import"}/albums/{album["id"]}',json={'decision':'approved','candidate_id':candidate['id']}).status_code==200
    body={'mode':'selected','album_ids':[album['id']], 'scopes' if source=='inbox' else 'folders':['Incoming']}
    preview=client.post(base+'/bulk/preview',json=body)
    assert preview.status_code==200 and not preview.json['exceptions'],preview.json
    assert preview.json['importable_albums'][0]['warnings']
    result=client.post(base+'/bulk/execute',json={**body,'confirmed':True})
    assert result.status_code==201 and len(result.json['imported'])==1 and not result.json['failed'],result.json
    items=list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items())
    assert len(items)==3 and {item.album for item in items}=={'Chosen Album'}
    assert {item.title for item in items}=={'First','Second','Third'}
    if mismatch!='extra':
        preserved=next(item for item in items if item.title==('Third' if mismatch=='fewer' else 'Second'))
        assert preserved.artist=='Local Artist'
        assert preserved.track==(3 if mismatch=='fewer' else 2) and preserved.tracktotal==3
        assert preserved.mb_trackid=='123e4567-e89b-42d3-a456-426614174099'
    if source=='inbox': assert [path.read_bytes() for path in paths]==originals
    else: assert all(path.is_file() for path in paths)
