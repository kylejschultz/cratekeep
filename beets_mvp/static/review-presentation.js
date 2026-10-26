/* Shared Library/Inbox match-review presentation. Endpoint and execution controls stay with each screen. */
(() => {
  const iconPaths = {
    artist:['M20 21a8 8 0 0 0-16 0','M12 13a4 4 0 1 0 0-8 4 4 0 0 0 0 8'],
    album:['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20','M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6','M12 12h.01'],
    genre:['M20 13c0 5-3.5 8-8 8s-8-3-8-8c0-4 3-7 8-11 5 4 8 7 8 11Z','M9 13h6'],
    year:['M3 5h18v16H3z','M16 3v4','M8 3v4','M3 10h18'],
    tracks:['M4 6h16','M4 12h16','M4 18h10'], media:['M4 5h16v14H4z','M8 9h8','M8 15h8','M9 15l-1 4','M15 15l1 4'],
    note:['M5 3h14v18H5z','M8 8h8','M8 12h8','M8 16h5'], region:['M5 21V4','M5 5h11l-1 4 1 4H5'],
    local:['M6 2h8l4 4v16H6z','M14 2v5h5','m9 14 2 2 4-4'],
    online:['M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.3 1.2A3.5 3.5 0 0 0 7 18','m9 14 2 2 4-4'],
  };
  const icon = kind => {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('candidate-icon'); svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('focusable', 'false');
    iconPaths[kind].forEach(value => { const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', value); svg.append(path); });
    return svg;
  };
  const changed = (left, right) => String(left || '').trim().toLocaleLowerCase() !== String(right || '').trim().toLocaleLowerCase();
  const titleKey = value => String(value || '').normalize('NFKC').trim().toLocaleLowerCase().replace(/[‘’‚‛ʼ＇]/gu, "'").replace(/[“”„‟＂]/gu, '"');
  const releaseYear = value => {
    const match = [value?.year, value?.date].map(item => String(item || '').match(/(?:^|\D)(\d{4})(?:\D|$)/)).find(Boolean);
    return match ? match[1] : '';
  };
  const position = value => Array.isArray(value) && value.length >= 2 ? `${value[0]}.${String(value[1]).padStart(2, '0')}` : '';
  const duration = value => {
    if (value == null || value === '' || !Number.isFinite(Number(value))) return '';
    const seconds = Math.round(Number(value)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  };
  const trackEvidence = details => (details || []).map(track => {
    const localPosition = position(track.current_position), proposedPosition = position(track.proposed_position);
    const localDuration = duration(track.local_duration), proposedDuration = duration(track.proposed_duration);
    const titleChanged = titleKey(track.local) !== titleKey(track.proposed), artistChanged = changed(track.local_artist, track.proposed_artist);
    const positionChanged = localPosition !== proposedPosition;
    const rawDelta = track.local_duration != null && track.proposed_duration != null && Number.isFinite(Number(track.local_duration)) && Number.isFinite(Number(track.proposed_duration)) ? Number(track.proposed_duration) - Number(track.local_duration) : null;
    const durationDelta = rawDelta === null ? null : Math.round(rawDelta), durationChanged = rawDelta !== null && Math.abs(rawDelta) > 3;
    const unpaired = ['missing','extra','unmatched'].includes(track.status), explanations = [];
    if (track.status === 'missing') explanations.push('Local track was not found on the MusicBrainz release.');
    else if (track.status === 'extra') explanations.push('MusicBrainz track is not present locally.');
    else if (track.status === 'unmatched') explanations.push('Local and MusicBrainz tracks could not be confidently matched.');
    if (titleChanged && !unpaired) explanations.push(`Title differs: local “${track.local || 'Not provided'}” → MusicBrainz “${track.proposed || 'Not provided'}”.`);
    if (artistChanged && !unpaired) explanations.push(`Artist differs: local “${track.local_artist || 'Not provided'}” → proposed “${track.proposed_artist || 'Not provided'}”.`);
    if (positionChanged && !unpaired) explanations.push(`Position differs: local ${localPosition || 'not provided'} → MusicBrainz ${proposedPosition || 'not provided'}.`);
    if ((titleChanged || artistChanged || positionChanged || durationChanged || unpaired) && !localDuration && proposedDuration) explanations.push('Local duration unavailable.');
    if ((titleChanged || artistChanged || positionChanged || durationChanged || unpaired) && !proposedDuration && localDuration) explanations.push('MusicBrainz duration unavailable.');
    if (durationChanged) explanations.push(`Duration differs: local ${localDuration} → MusicBrainz ${proposedDuration} (${Math.abs(durationDelta)} second${Math.abs(durationDelta) === 1 ? '' : 's'} ${durationDelta > 0 ? 'longer' : 'shorter'}).`);
    return {...track, localPosition, proposedPosition, localDuration, proposedDuration, durationDelta, titleChanged, artistChanged, positionChanged, durationChanged, unpaired, explanations};
  });
  const score = confidence => {
    if (!Number.isFinite(confidence)) return {label:'Score unavailable', band:'none', accessibleLabel:'Score unavailable'};
    const percentage = Math.floor(confidence * 1000) / 10, band = confidence >= .9 ? 'high' : confidence >= .75 ? 'medium' : 'low';
    const label = `${Number.isInteger(percentage) ? percentage : percentage.toFixed(1)}%`;
    return {label, band, accessibleLabel:`${label} match confidence${band === 'high' ? '; meets the initial auto-import indicator threshold' : ''}`};
  };
  const factsFor = (album, value) => {
    const current = album.current_metadata || {}, details = value.is_as_is ? [] : (value.track_details || []), tracks = trackEvidence(details);
    const trackChanges = tracks.filter(item => item.titleChanged || item.artistChanged || item.positionChanged || item.durationChanged || item.unpaired);
    const unmatchedLocal = details.filter(item => item.local != null && ['missing','unmatched'].includes(item.status)).length;
    const unmatchedOnline = details.filter(item => item.proposed != null && ['extra','unmatched'].includes(item.status)).length;
    const formats = [...new Set(Array.isArray(value.media) ? value.media.map(item => item?.format).filter(Boolean) : value.media ? [value.media] : [])];
    const media = [value.release_type, ...formats].filter(Boolean).join(' · ') || (value.is_as_is ? `${value.disc_count || 1} ${value.disc_count === 1 ? 'disc' : 'discs'} · current files` : 'Release/media not provided');
    const mediaChanged = !value.is_as_is && ((current.release_type && changed(current.release_type, value.release_type)) || (current.media && !formats.some(format => !changed(current.media, format))));
    const localYear = releaseYear(current), proposedYear = releaseYear(value), yearChanged = !value.is_as_is && changed(localYear, proposedYear);
    const counts = Array.isArray(value.genre_evidence?.counts) ? value.genre_evidence.counts : [], runnerUp = counts[1];
    const genre = value.genre ? `${value.genre} (${counts[0]?.count ?? '?'} positive votes${runnerUp ? `; runner-up ${runnerUp.name} ${runnerUp.count}` : '; no runner-up'})` : value.is_as_is ? (current.genre || 'Not present in current metadata') : `No decisive MusicBrainz genre; preserving ${current.genre || 'no current genre'}`;
    const localCount = value.track_count || album.tracks?.length || 0;
    return [
      {kind:'artist',label:'Artist',value:value.artist || 'Unknown artist',changed:!value.is_as_is && changed(current.artist,value.artist)},
      {kind:'album',label:'Album',value:value.album || 'Unknown album',changed:!value.is_as_is && changed(current.album,value.album)},
      ...(!value.is_as_is ? [{kind:'note',label:'MusicBrainz release ID',value:value.provider_id || 'Not provided',changed:false}] : []),
      ...(!value.is_as_is && value.release_disambiguation ? [{kind:'note',label:'MusicBrainz release note',value:value.release_disambiguation,changed:false}] : []),
      ...(!value.is_as_is && value.release_status ? [{kind:'note',label:'Release status',value:value.release_status,changed:false}] : []),
      {kind:'genre',label:'Genre',value:genre,changed:!value.is_as_is && Boolean(value.genre) && changed(current.genre,value.genre)},
      {kind:'year',label:'Release Year',value:yearChanged ? `${localYear || 'Not provided'} → ${proposedYear || 'Not provided'}` : (proposedYear || (value.is_as_is ? 'Unchanged' : 'Not provided')),changed:yearChanged,comparison:yearChanged,localYear,proposedYear},
      {kind:'tracks',label:'Tracks changed',value:trackChanges.length ? `${trackChanges.length} track${trackChanges.length === 1 ? '' : 's'} changed` : 'All tracks matched',changed:Boolean(trackChanges.length),expandable:!value.is_as_is,tracks:trackChanges},
      {kind:'media',label:'Release Type/media',value:media,changed:Boolean(mediaChanged)},
      {kind:'region',label:'Release Region',value:value.country || (value.is_as_is ? 'Not present in current metadata' : 'Region not provided'),changed:!value.is_as_is && Boolean(current.country) && changed(current.country,value.country)},
      {kind:'local',label:'Local tracks found online',value:value.is_as_is ? 'Not applicable — As Is' : unmatchedLocal ? `${unmatchedLocal} local ${unmatchedLocal === 1 ? 'track was' : 'tracks were'} not found online` : `All ${localCount} local tracks found online`,changed:unmatchedLocal > 0},
      {kind:'online',label:'Online tracks present locally',value:value.is_as_is ? 'Not applicable — As Is' : unmatchedOnline ? `${unmatchedOnline} online ${unmatchedOnline === 1 ? 'track is' : 'tracks are'} not present locally` : 'All online tracks present locally',changed:unmatchedOnline > 0},
    ];
  };
  function renderCandidates({album, container, selectedId, asIsId, onSelect, openTrackComparison}) {
    const current = album.current_metadata || {}, asIs = {...current, id:asIsId, is_as_is:true, duplicate_preflight:album.duplicate_preflight};
    const options = [...(album.candidates || []), asIs];
    container.replaceChildren(...options.map((value, index) => {
      const selected = value.id === selectedId, facts = factsFor(album, value), details = document.createElement('details');
      details.className = 'candidate-option'; details.open = selected; details.setAttribute('aria-current', String(selected));
      const summary = document.createElement('summary'); summary.className = 'candidate-option-summary'; summary.setAttribute('role','radio'); summary.setAttribute('aria-checked',String(selected)); summary.tabIndex = selected ? 0 : -1;
      const marker = document.createElement('span'); marker.className='candidate-marker'; marker.setAttribute('aria-hidden','true');
      const name = document.createElement('span'); name.className='candidate-name'; const year = releaseYear(value);
      name.textContent = value.is_as_is ? 'As Is — keep current metadata' : `${value.artist} — ${value.album}${year ? ` (${year})` : ''}`;
      const confidence = document.createElement('span'); confidence.className='candidate-confidence confidence-band'; const presentation = score(value.confidence);
      confidence.dataset.band = value.is_as_is ? 'as-is' : presentation.band; confidence.textContent = `${selected ? 'Selected · ' : ''}${value.is_as_is ? 'As Is' : presentation.label}`;
      confidence.setAttribute('aria-label', value.is_as_is ? 'As Is keeps current tags. Select this option to use current metadata.' : presentation.accessibleLabel);
      const signals = document.createElement('span'); signals.className='candidate-signals'; facts.forEach(fact => { const signal=document.createElement('span'); signal.className='candidate-signal'; signal.dataset.changed=String(fact.changed); signal.title=`${fact.label}: ${fact.value}`; signal.setAttribute('aria-label',signal.title); signal.append(icon(fact.kind)); signals.append(signal); });
      const toggle=document.createElement('span'); toggle.className='candidate-toggle'; toggle.setAttribute('aria-hidden','true');
      summary.tabIndex = selected || (selectedId == null && index === 0) ? 0 : -1;
      summary.setAttribute('aria-label',`Option ${index + 1}: ${name.textContent}. ${confidence.textContent}. ${selected ? 'Selected' : 'Select'}. ${facts.map(fact => `${fact.label}: ${fact.value}`).join('. ')}. Toggle details.`); summary.append(marker,name,confidence,signals,toggle);
      const select = next => { onSelect(next); queueMicrotask(() => container.querySelector('[aria-checked="true"]')?.focus()); };
      summary.addEventListener('click', event => { if (!selected) { event.preventDefault(); select(value.id); } });
      summary.addEventListener('keydown', event => { if (!['ArrowDown','ArrowRight','ArrowUp','ArrowLeft','Home','End'].includes(event.key)) return; event.preventDefault(); const next=event.key==='Home'?0:event.key==='End'?options.length-1:(index+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+options.length)%options.length; select(options[next].id); });
      const body=document.createElement('div'); body.className='candidate-detail';
      if(value.artwork_url){const artwork=document.createElement('img'); artwork.className='candidate-artwork'; artwork.src=value.artwork_url; artwork.alt=`Cover Art Archive front artwork for ${value.album}`; body.append(artwork);} else {const missing=document.createElement('span'); missing.className='candidate-artwork-missing cover'; missing.textContent=value.is_as_is?'Current artwork unchanged':value.artwork?.status==='lookup-error'?'Artwork lookup unavailable — candidate metadata is still usable':'No candidate artwork available'; body.append(missing);}
      const list=document.createElement('dl'); list.className='candidate-facts'; facts.forEach(fact => { const wrapper=document.createElement('div'); wrapper.className='candidate-fact'; wrapper.dataset.changed=String(fact.changed); const dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=fact.label; if(fact.expandable){if(fact.tracks.length){const button=document.createElement('button');button.type='button';button.className='track-change-button';button.textContent=`View track changes (${fact.tracks.length})`;button.setAttribute('aria-haspopup','dialog');button.onclick=()=>openTrackComparison(fact.tracks,`${value.artist} — ${value.album}`,button);dd.append(button);}else dd.textContent='All tracks matched';}else if(fact.comparison){const old=document.createElement('span'),arrow=document.createElement('span'),next=document.createElement('span');old.className='metadata-old';old.textContent=fact.localYear||'Not provided';arrow.className='metadata-arrow';arrow.textContent='→';arrow.setAttribute('aria-hidden','true');next.className='metadata-new';next.textContent=fact.proposedYear||'Not provided';dd.append(old,arrow,next);}else dd.textContent=fact.value; wrapper.append(icon(fact.kind),dt,dd); list.append(wrapper); }); body.append(list); details.append(summary,body); return details;
    }));
    return options.find(value => value.id === selectedId) || asIs;
  }
  function renderTrackRows(rows, tracks) {
    const appendCell=(row,textValue,{changed=false,missing=false}={})=>{const cell=document.createElement('td');cell.textContent=textValue||'—';if(changed)cell.classList.add('track-cell-changed');if(missing)cell.classList.add('track-missing');row.append(cell);};
    const appendMetadata=(row,track,local)=>{const title=local?track.local:track.proposed,artist=local?track.local_artist:track.proposed_artist,source=local?'Local':'MusicBrainz',cell=document.createElement('td');cell.className='track-metadata-cell';cell.setAttribute('aria-label',`${source} metadata. Title: ${title||'Not provided'}${track.titleChanged?' (changed)':''}. Artist: ${artist||'Not provided'}${track.artistChanged?' (changed)':''}.`);const titleLine=document.createElement('div');titleLine.className='track-metadata-title';titleLine.textContent=title||'—';if(track.titleChanged&&title!=null)titleLine.classList.add('track-field-changed');if(title==null)titleLine.classList.add('track-missing');const artistLine=document.createElement('span');artistLine.className='track-metadata-artist';artistLine.textContent=`Artist: ${artist||'Not provided'}`;if(track.artistChanged&&artist!=null)artistLine.classList.add('track-field-changed');if(artist==null)artistLine.classList.add('track-missing');cell.append(titleLine,artistLine);const label=title==null?(local?'Not present locally':'Not present online'):track.status==='missing'?'Not found online':track.status==='extra'?'Extra online':track.status==='unmatched'?'Unmatched':'';if(label){const status=document.createElement('span');status.className='track-unmatched-label';status.textContent=label;cell.append(status);}row.append(cell);};
    rows.replaceChildren(...tracks.flatMap(track=>{const row=document.createElement('tr');if(track.unpaired)row.className='track-row-unmatched';appendCell(row,track.localPosition===track.proposedPosition?(track.localPosition||track.proposedPosition||'—'):`${track.localPosition||'—'} → ${track.proposedPosition||'—'}`,{changed:track.positionChanged});appendMetadata(row,track,true);appendMetadata(row,track,false);appendCell(row,track.localDuration,{changed:track.durationChanged&&Boolean(track.localDuration),missing:!track.localDuration});appendCell(row,track.proposedDuration,{changed:track.durationChanged&&Boolean(track.proposedDuration),missing:!track.proposedDuration});if(!track.explanations?.length)return[row];const explanation=document.createElement('tr');explanation.className='track-explanation-row';const cell=document.createElement('td');cell.colSpan=5;const text=document.createElement('p');text.className='track-change-explanation';text.textContent=track.explanations.join(' ');cell.append(text);explanation.append(cell);return[row,explanation];}));
  }
  window.CratekeepReview = {renderCandidates, renderTrackRows, trackEvidence, describeCandidate:factsFor, score};
})();
