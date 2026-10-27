"""Symbol titles pair by independent track evidence, not invented title aliases."""
from copy import deepcopy
from pathlib import Path
import pytest
from beets.library import Library
from beets_mvp.matching import score_release
from test_app import tagged_inbox_wav, exact_candidate
from test_inbox_copies import app, imports_base


def evidence():
    local={'artist':'Artist','album':'Album','tracks':[
        {'title':'Opening','track':1,'disc':1,'duration':100},
        {'title':'Human Heart','track':2,'disc':1,'duration':180},
        {'title':'Rainbow','track':3,'disc':1,'duration':200}]}
    canonical={'artist':'Artist','album':'Album','tracks':[
        {**row,'title':{'Human Heart':'♡','Rainbow':'🌈'}.get(row['title'],row['title'])} for row in local['tracks']]}
    return local,canonical


def test_symbol_pairs_remain_title_changes_without_unmatched_tracks():
    local,canonical=evidence();result=score_release(local,canonical)
    assert result['matched_track_count']==3 and result['unmatched_track_count']==0
    assert [row['status'] for row in result['track_details']]==['matched','title-mismatch','title-mismatch']
    assert 'title-mismatch' in result['track_details'][2]['issues']
    assert result['confidence']<1  # Title differences still contribute to distance.


@pytest.mark.parametrize('change',['missing-position','duplicate-local','duplicate-release','missing-duration','zero-duration','duration-conflict','wrong-position','ordinary-title','punctuation','track-count'])
def test_symbol_exception_does_not_hide_missing_or_ambiguous_evidence(change):
    local,canonical=evidence()
    if change=='missing-position': local['tracks'][2].pop('track')
    elif change=='duplicate-local': local['tracks'][1]['track']=3
    elif change=='duplicate-release': canonical['tracks'][1]['track']=3
    elif change=='missing-duration': canonical['tracks'][2].pop('duration')
    elif change=='zero-duration': canonical['tracks'][2]['duration']=0
    elif change=='duration-conflict': canonical['tracks'][2]['duration']=204
    elif change=='wrong-position': canonical['tracks'][2]['track']=4
    elif change=='ordinary-title': canonical['tracks'][2]['title']='Completely unrelated'
    elif change=='punctuation': canonical['tracks'][2]['title']='...'
    else: canonical['tracks'].append({'title':'Bonus','track':4,'duration':100})
    result=score_release(local,canonical)
    assert next(row for row in result['track_details'] if row.get('local')=='Rainbow')['status']=='unmatched'
    assert result['unmatched_track_count']>0


@pytest.mark.parametrize('delta',[0,3,3.01])
def test_symbol_duration_boundary(delta):
    local,canonical=evidence();canonical['tracks'][2]['duration']+=delta
    result=score_release(local,canonical)
    row=next(row for row in result['track_details'] if row.get('local')=='Rainbow')
    assert row['status']==('title-mismatch' if delta<=3 else 'unmatched')


def test_symbol_pairs_follow_explicit_discs_and_positions_not_list_order():
    local,canonical=evidence()
    local['tracks'][2].update(disc=2,track=1);canonical['tracks'][2].update(disc=2,track=1)
    canonical['tracks'].reverse()
    result=score_release(local,canonical)
    rainbow=next(row for row in result['track_details'] if row.get('local')=='Rainbow')
    assert rainbow['proposed']=='🌈' and rainbow['proposed_position']==[2,1]
    assert result['matched_track_count']==3


@pytest.mark.parametrize('choice',['default','explicit'])
def test_bulk_import_symbol_titles_uses_saved_release_and_keeps_originals(app,tmp_path,choice):
    sources=[]
    for index,title in enumerate(['Opening','Human Heart','Rainbow'],1):
        path=tmp_path/f'inbox/Album/{index}.wav';tagged_inbox_wav(path,title=title,track=index)
        sources.append(path)
    originals=[path.read_bytes() for path in sources]
    client=app.test_client();base=imports_base(app);client.post(base+'/scopes/refresh',json={})
    album=client.get(base+'/queue').json['albums'][0]
    def provider(query,*,limit):
        candidate=exact_candidate(query,'symbol-best')
        for row in candidate['tracks']:
            row['title']={'Human Heart':'♡','Rainbow':'🌈'}.get(row['title'],row['title'])
        return [candidate,{**deepcopy(candidate),'provider_id':'symbol-chosen','album':'Chosen edition'}]
    app.config['MUSICBRAINZ_PROVIDER']=provider
    result=client.post('/api/inbox/candidates',json={'album_id':album['id']});assert result.status_code==201,result.json
    candidates=result.json['album']['candidates'];assert all(row['matched_track_count']==3 for row in candidates)
    if choice=='explicit':
        selected=next(row for row in candidates if row['provider_id']=='symbol-chosen')
        assert selected['confidence']<max(row['confidence'] for row in candidates)
        assert client.patch(f"/api/inbox/albums/{album['id']}",json={'decision':'approved','candidate_id':selected['id']}).status_code==200
    expected='Chosen edition' if choice=='explicit' else 'Album'
    assert client.get(base+'/queue').json['albums'][0]['candidate_status']=='complete'
    app.config['MUSICBRAINZ_PROVIDER']=lambda *args,**kwargs:pytest.fail('Saved match must not refetch MusicBrainz')
    body={'album_ids':[album['id']],'scopes':['Album'],'mode':'selected'}
    preview=client.post(base+'/bulk/preview',json=body)
    assert len(preview.json['importable_albums'])==1 and not preview.json['exceptions'],preview.json
    executed=client.post(base+'/bulk/execute',json={**body,'confirmed':True})
    assert executed.status_code==201 and len(executed.json['imported'])==1,executed.json
    items=list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items())
    assert {item.album for item in items}=={expected}
    assert {item.title for item in items}=={'Opening','♡','🌈'}
    assert [path.read_bytes() for path in sources]==originals
