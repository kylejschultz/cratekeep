import { useEffect, useState } from "react";
import { api, formatBytes } from "./api";
import { Queue } from "./Queue";
import { Notice, PageHeader } from "./Shell";
import type { Bootstrap } from "./types";

export function Overview({ data }: { data: Record<string, any> }) {
  const summary = data.library_summary || {};
  return <><PageHeader eyebrow="Home" title="Library overview">A calm view of incoming music and the managed collection.</PageHeader>
    <div className="metric-grid"><article><span>Pending albums</span><strong>{data.inbox_album_count || 0}</strong><small>{data.inbox_file_count || 0} inbox files</small></article><article><span>Library albums</span><strong>{summary.albums || 0}</strong><small>{summary.tracks || 0} managed tracks</small></article><article><span>Inbox size</span><strong>{formatBytes(data.inbox_bytes)}</strong><small>Across readable sources</small></article></div>
    <section className="panel-section"><header><div><h2>Inbox status</h2><p>One bounded summary per named source.</p></div><a href="/inbox">Open Inbox</a></header><div className="surface-list">{(data.inbox_summaries || []).map((row: any) => <div className="summary-row" key={row.id}><div><strong>{row.name}</strong><small>{row.error || row.path}</small></div><span>{row.pending_albums || 0} albums · {row.files || 0} files</span><span className={`status ${row.status === "error" ? "error" : "pending"}`}>{row.status}</span></div>)}{!data.inbox_summaries?.length && <p className="empty-inline">No Inbox sources are configured.</p>}</div></section>
    <section className="panel-section"><header><div><h2>Recent albums</h2><p>Recently added to the managed library.</p></div><a href="/library?sort=recent&order=desc">Open Library</a></header><div className="album-grid">{(data.recent_albums || []).map((album: any) => <a className="album-tile" href={`/library?album=${encodeURIComponent(album.album)}`} key={album.id}><span className="cover">{album.artwork_url ? <img src={album.artwork_url} alt=""/> : "♫"}</span><strong>{album.album}</strong><small>{album.albumartist || "Unknown artist"}</small></a>)}</div></section>
  </>;
}

export function Inbox({ data, threshold }: { data: Record<string, any>; threshold: number }) {
  const inboxes = data.inboxes || [];
  const [selected, setSelected] = useState(inboxes[0]?.id || "");
  return <><PageHeader eyebrow="Inbox" title="Incoming music">Match, review, and explicitly import albums from a named source.</PageHeader>
    {inboxes.length > 1 && <div className="segmented" role="tablist" aria-label="Inbox source">{inboxes.map((inbox: any) => <button role="tab" aria-selected={selected === inbox.id} onClick={() => setSelected(inbox.id)} key={inbox.id}>{inbox.name}</button>)}</div>}
    {selected ? <Queue source="inbox" threshold={threshold} inboxId={selected}/> : <div className="empty-state"><strong>No inboxes configured</strong><a href="/settings">Open Settings</a></div>}
  </>;
}

export function LibraryImport({ threshold }: { threshold: number }) {
  return <><PageHeader eyebrow="Library Import" title="Adoption queue">Review existing library files with the same folder → album → file workflow used by Inbox.</PageHeader><div className="threshold-callout">Match threshold <strong>{threshold}%</strong><a href="/settings#matching">Change in Settings</a></div><Queue source="library-import" threshold={threshold}/></>;
}

function LibraryModal({ id, onClose }: { id: number; onClose: () => void }) {
  const [album, setAlbum] = useState<any>(null), [error, setError] = useState("");
  const [track, setTrack] = useState<any>(null), [preview, setPreview] = useState<any>(null);
  const [editing, setEditing] = useState(false);
  useEffect(() => { void api<any>(`/api/library/albums/${id}`).then(setAlbum).catch(reason => setError(reason.message)); }, [id]);
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { const result = await api<any>(`/api/library/albums/${id}`, { method: "PATCH", body: JSON.stringify({ fingerprint: album.fingerprint, album: form.get("album"), albumartist: form.get("albumartist"), genre: form.get("genre"), year: Number(form.get("year") || 0) }) }); setAlbum(result.album || result); setEditing(false); } catch (reason) { setError((reason as Error).message); }
  };
  const saveTrack = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    try { const result = await api<any>(`/api/library/albums/${id}/tracks/${track.id}`, { method: "PATCH", body: JSON.stringify({ expected_fingerprint: album.fingerprint, title: form.get("title"), artist: form.get("artist"), track: Number(form.get("track") || 0), disc: Number(form.get("disc") || 0) }) }); setAlbum(result.album || result); setTrack(null); } catch (reason) { setError((reason as Error).message); }
  };
  const rematch = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const musicbrainzId = String(new FormData(event.currentTarget).get("musicbrainz_id") || "");
    try { setPreview(await api<any>(`/api/library/albums/${id}/rematch/preview`, { method: "POST", body: JSON.stringify({ musicbrainz_id: musicbrainzId, expected_fingerprint: album.fingerprint }) })); } catch (reason) { setError((reason as Error).message); }
  };
  const applyPreview = async () => {
    try { const result = await api<any>(`/api/library/albums/${id}/rematch/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint }) }); setAlbum(result.album || result); setPreview(null); } catch (reason) { setError((reason as Error).message); }
  };
  const previewArtwork = async (file: File) => {
    const form = new FormData(); form.set("artwork", file); form.set("expected_fingerprint", album.fingerprint);
    try { const response = await fetch(`/api/library/albums/${id}/artwork/upload-preview`, { method: "POST", body: form }); const result = await response.json(); if (!response.ok) throw new Error(result.error); setPreview({ ...result, kind: "artwork" }); } catch (reason) { setError((reason as Error).message); }
  };
  const applyArtwork = async () => {
    try { const result = await api<any>(`/api/library/albums/${id}/artwork/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint }) }); setAlbum(result.album || result); setPreview(null); } catch (reason) { setError((reason as Error).message); }
  };
  return <div className="modal"><div className="dialog" role="dialog" aria-modal="true"><header><h2>{album?.album || "Album details"}</h2><button onClick={onClose} aria-label="Close album">×</button></header><div className="dialog-body">{error && <Notice kind="error">{error}</Notice>}{!album ? <p>Loading…</p> : <>
    <div className="album-detail-head"><span className="album-detail-art">{album.artwork_url ? <img src={album.artwork_url} alt={`Artwork for ${album.album}`}/> : <span>No artwork</span>}</span><div><strong>{album.album}</strong><span>{album.albumartist || "Unknown artist"}</span><span>{album.genre || "No genre"} · {album.year || "No year"}</span></div>{!editing && <button type="button" onClick={() => { setEditing(true); setError(""); }}>Edit</button>}</div>
    {editing && <form className="edit-grid subpanel" onSubmit={save} id="album-form"><h3>Edit album metadata</h3><label>Album<input name="album" defaultValue={album.album}/></label><label>Album artist<input name="albumartist" defaultValue={album.albumartist}/></label><label>Genre<input name="genre" defaultValue={album.genre}/></label><label>Year<input name="year" type="number" defaultValue={album.year}/></label><div className="form-actions"><button type="submit">Save album</button><button className="secondary" type="button" onClick={() => { setEditing(false); setTrack(null); setPreview((current: any) => current?.kind === "artwork" ? null : current); }}>Cancel changes</button></div></form>}
    <h3>Tracks</h3><ul className="track-list">{(album.tracks || album.items || []).map((row: any) => <li key={row.id}>{editing ? <button className="track-edit" type="button" onClick={() => setTrack(row)}>{row.track || "—"}. {row.title}</button> : <span>{row.track || "—"}. {row.title}</span>}<small>{row.artist}</small></li>)}</ul>
    {track && <form className="edit-grid subpanel" onSubmit={saveTrack}><h3>Edit track</h3><label>Title<input name="title" defaultValue={track.title}/></label><label>Artist<input name="artist" defaultValue={track.artist}/></label><label>Track<input name="track" type="number" defaultValue={track.track}/></label><label>Disc<input name="disc" type="number" defaultValue={track.disc}/></label><button type="submit">Save track</button><button className="secondary" type="button" onClick={() => setTrack(null)}>Cancel</button></form>}
    <div className="workflow-grid"><form className="subpanel rematch-panel" onSubmit={rematch}><h3>MusicBrainz rematch</h3><p>Explicitly preview metadata from a different release. This is separate from manual editing.</p><label>Release UUID<input name="musicbrainz_id" required/></label><button type="submit">Preview rematch</button></form>{editing && <div className="subpanel"><h3>Replace artwork</h3><p>Choose an image to preview it before applying.</p><label>Upload replacement<input type="file" accept="image/*" onChange={event => event.target.files?.[0] && void previewArtwork(event.target.files[0])}/></label></div>}</div>
    {preview && <div className="subpanel"><h3>{preview.kind === "artwork" ? "Artwork preview" : "Metadata preview"}</h3>{preview.kind === "artwork" && <img className="art-preview" src={`/api/library/albums/${id}/artwork/previews/${preview.preview_id}`} alt="Proposed cover artwork"/>}<p>{preview.message || (preview.applicable === false ? "This match cannot be applied safely." : "Preview is ready to apply.")}</p><button type="button" disabled={preview.applicable === false} onClick={preview.kind === "artwork" ? applyArtwork : applyPreview}>Apply {preview.kind === "artwork" ? "artwork" : "rematch"}</button><button className="secondary" type="button" onClick={() => setPreview(null)}>Cancel</button></div>}
  </>}</div></div></div>;
}

function Help({ label, children }: { label: string; children: string }) {
  const [open, setOpen] = useState(false);
  const id = `help-${label.toLowerCase().replaceAll(" ", "-")}`;
  return <span className="help-wrap"><button className="help" type="button" aria-label={`Help: ${label}`} aria-expanded={open} aria-controls={id} title={children} onClick={() => setOpen(value => !value)}>?</button>{open && <span className="help-text" id={id} role="note">{children}</span>}</span>;
}

export function Library({ data }: { data: Record<string, any> }) {
  const [open, setOpen] = useState<number | null>(null), summary = data.summary || {};
  return <><PageHeader eyebrow="Library" title="Managed collection">Browse albums, inspect tracks, and edit managed metadata.</PageHeader><div className="metric-grid four"><article><span>Albums</span><strong>{summary.albums || 0}</strong></article><article><span>Tracks</span><strong>{summary.tracks || 0}</strong></article><article><span>Artists</span><strong>{summary.artists || 0}</strong></article><article><span>Size</span><strong>{formatBytes(summary.bytes)}</strong></article></div>
    <form className="filter-bar" method="get"><label className="sr-only" htmlFor="library-search">Search library</label><input id="library-search" name="q" defaultValue={data.query} placeholder="Search artists, albums, tracks"/><select name="sort" defaultValue={data.sort}><option value="artist">Artist</option><option value="album">Album</option><option value="year">Year</option><option value="recent">Recently added</option></select><button type="submit">Search</button></form>
    <section className="panel-section"><header><div><h2 id="browse-title">Browse albums</h2><p>{data.matching_albums || 0} matching albums</p></div></header><div className="album-grid browse">{(data.albums || []).map((album: any) => <button className="album-tile album-card" data-album-id={album.id} onClick={() => setOpen(album.id)} key={album.id}><span className="cover" data-album-cover>{album.artwork_url ? <img src={album.artwork_url} alt=""/> : "No art"}</span><strong>{album.album}</strong><small>{album.albumartist || "Unknown artist"} · {album.year || "—"}</small></button>)}</div></section>
    {open != null && <LibraryModal id={open} onClose={() => setOpen(null)}/>}</>;
}

export function Settings({ data, threshold }: { data: Record<string, any>; threshold: number }) {
  const settings = data.settings || {}, initial = data.inboxes || [];
  const [inboxes, setInboxes] = useState<any[]>(initial);
  const [tab, setTab] = useState(location.hash.slice(1) || "general");
  return <><PageHeader eyebrow="Settings" title="Cratekeep settings">Paths, matching policy, metadata behavior, and integrations.</PageHeader>{(data.flashes || []).map((row: any, index: number) => <Notice key={index} kind={row[0] === "error" ? "error" : "success"}>{row[1]}</Notice>)}
    <div className="settings-tabs" role="tablist">{[["general","General"],["matching","Matching"],["metadata","Metadata"],["advanced","Advanced"]].map(([key,label]) => <button type="button" role="tab" aria-selected={tab === key} onClick={() => { setTab(key); location.hash = key; }} key={key}>{label}</button>)}</div>
    <form method="post" className="settings-form">
      <section hidden={tab !== "general"}><div className="settings-card"><h2>Inboxes <Help label="Inboxes">Named source folders where new music waits before review and import.</Help></h2><p>Each Inbox must be a separate container-visible path outside the managed Library.</p>{inboxes.map((inbox: any, index: number) => <div className="inbox-setting" data-inbox-row key={inbox.id || index}><input type="hidden" name="inbox_id" value={inbox.id}/><label>Name<input name="inbox_name" defaultValue={inbox.name} required/></label><label>Inbox path<input name="inbox_path" defaultValue={inbox.path} required/></label><button className="secondary" type="button" onClick={() => setInboxes(rows => rows.filter((_: any, row: number) => row !== index))}>Remove</button></div>)}<button className="secondary" type="button" onClick={() => setInboxes(rows => [...rows, { id: "", name: `Inbox ${rows.length + 1}`, path: data.default_inbox }])}>Add inbox</button></div><div className="settings-card"><h2>Library <Help label="Library path">The managed destination where Cratekeep stores approved albums.</Help></h2><label>Library path<input name="library_path" defaultValue={settings.library_path || data.default_library} required/></label></div><div className="settings-card"><h2>Navidrome <Help label="Navidrome">Optional integration that asks Navidrome to rescan after Cratekeep changes the Library.</Help></h2><p>Configure this only when Navidrome serves the same Library. Cratekeep calls the rescan URL after successful changes.</p><label>Rescan URL<input type="url" name="navidrome_rescan_url" defaultValue={settings.navidrome_rescan_url}/></label><label>Bearer token<input type="password" name="navidrome_token" placeholder={data.has_token ? "Stored — leave blank to keep" : "Optional"}/></label>{data.has_token && <label className="check"><input type="checkbox" name="clear_navidrome_token"/>Clear stored token</label>}</div></section>
      <section hidden={tab !== "matching"}><div className="settings-card"><h2>Automatic matching <Help label="Match threshold">The minimum candidate confidence treated as ready for safe bulk import; lower scores remain for review.</Help></h2><label>Matched threshold (%)<input name="match_threshold" type="number" min="1" max="100" step="1" defaultValue={threshold} required/></label><p>Albums at or above this score can be included in confirmed bulk import when all other safety checks pass. Lower-scoring candidates remain in sequential review.</p>{inboxes.map((inbox: any) => <label className="check" key={inbox.id}><input type="checkbox" name="inbox_auto_match_id" value={inbox.id} defaultChecked={inbox.auto_match}/>Automatically find matches for {inbox.name}</label>)}</div></section>
      <section hidden={tab !== "metadata"}><div className="settings-card"><h2>Artwork <Help label="Artwork">Controls fetching, sidecars, embedding, and replacement during approved imports.</Help></h2><label className="check"><input type="checkbox" name="fetch_art" defaultChecked={settings.fetch_art === "1"}/>Fetch Cover Art Archive artwork</label><label className="check"><input type="checkbox" name="art_sidecar" defaultChecked={settings.art_sidecar === "1"}/>Save cover.jpg sidecars</label><label className="check"><input type="checkbox" name="art_embed" defaultChecked={settings.art_embed === "1"}/>Embed artwork in audio files</label><label className="check"><input type="checkbox" name="art_replace" defaultChecked={settings.art_replace === "1"}/>Replace existing artwork</label></div><div className="settings-card"><h2>Feature in Title <Help label="Feature in Title">Moves trailing featured-artist credits into the track title before matching, using the chosen format.</Help></h2><p>For example, “Song” by “Artist feat. Guest” can become “Song feat. Guest” by “Artist”.</p><label className="check"><input type="checkbox" name="ftintitle_enabled" defaultChecked={data.ftintitle?.enabled}/>Enable Feature in Title</label><label>Title format<input name="ftintitle_format" defaultValue={data.ftintitle?.format || "feat. {0}"}/></label><label className="check"><input type="checkbox" name="ftintitle_keep_in_artist" defaultChecked={data.ftintitle?.keep_in_artist}/>Also keep the feature in the artist field</label></div></section>
      <section hidden={tab !== "advanced"} className="advanced-settings"><div className="settings-card"><h2>Beets YAML <Help label="Beets YAML">Advanced beets configuration. Invalid YAML is rejected and the saved configuration remains unchanged.</Help></h2><label className="sr-only" htmlFor="beets-config">Beets configuration</label><textarea id="beets-config" name="beets_config" defaultValue={data.beets_config}/></div></section>
      <div className="form-footer"><button type="submit">Save settings</button></div>
    </form></>;
}

export function Page({ boot }: { boot: Bootstrap }) {
  if (boot.page === "overview") return <Overview data={boot.data}/>;
  if (boot.page === "inbox") return <Inbox data={boot.data} threshold={boot.threshold}/>;
  if (boot.page === "library-import") return <LibraryImport threshold={boot.threshold}/>;
  if (boot.page === "library") return <Library data={boot.data}/>;
  return <Settings data={boot.data} threshold={boot.threshold}/>;
}
