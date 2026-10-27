import { useInspectorResize } from "./useInspectorResize";
import { ArtworkPlaceholder } from "./ArtworkPlaceholder";
import { useEffect, useRef, useState } from "react";
import { useToast } from "./toastContext";
import { api, formatBytes } from "./api";
import { Queue } from "./Queue";
import { Notice, PageHeader } from "./Shell";
import type { Bootstrap } from "./types";

export function Overview({ data }: { data: Record<string, any> }) {
  const summary = data.library_summary || {};
  return <><PageHeader eyebrow="Home" title="Library overview">A calm view of incoming music and the managed collection.</PageHeader>
    <div className="metric-grid four"><article><span>Library albums</span><strong>{summary.albums || 0}</strong><small>Managed collection</small></article><article><span>Tracks</span><strong>{summary.tracks || 0}</strong><small>{summary.artists || 0} album artists</small></article><article><span>Library size</span><strong>{formatBytes(summary.bytes)}</strong><small>From library inventory</small></article><article><span>Pending albums</span><strong>{data.inbox_album_count || 0}</strong><small>{data.inbox_file_count || 0} inbox files</small></article></div>
    <section className="panel-section" aria-labelledby="inbox-status-title"><header><div><h2 id="inbox-status-title">Inbox status</h2><p>Incoming music, by source.</p></div><a href="/inbox">Open Inbox</a></header><div className="inbox-status-cards"><article className="inbox-total-card"><p className="eyebrow">All inboxes</p><h3>Inbox size</h3><strong>{formatBytes(data.inbox_bytes)}</strong><small>{data.inbox_file_count || 0} files · {data.inbox_album_count || 0} pending albums</small>{data.inbox_totals_stale && <small>Some sources are unavailable.</small>}</article>{(data.inbox_summaries || []).map((row: any) => <article className="inbox-source-card" key={row.id} aria-label={`${row.name} status`}><a className="inbox-source-link" href={`/inbox?source=${encodeURIComponent(row.id)}`} aria-label={`Open ${row.name} Inbox`}><header><h3>{row.name}</h3><span className={`source-status${row.status === "error" ? " is-error" : ""}`}>{row.status}</span></header><dl><div><dt>Albums</dt><dd>{row.pending_albums || 0}</dd></div><div><dt>Files</dt><dd>{row.files || 0}</dd></div><div><dt>Size</dt><dd>{formatBytes(row.bytes)}</dd></div></dl>{row.error && <p className="source-error">{row.error}</p>}</a></article>)}</div>{!data.inbox_summaries?.length && <p className="empty-inline">No Inbox sources are configured.</p>}</section>
    <section className="panel-section"><header><div><h2>Recent albums</h2><p>Recently added to the managed library.</p></div><a href="/library?sort=recent&order=desc">Open Library</a></header><div className="album-grid">{(data.recent_albums || []).map((album: any) => <a className="album-tile" href={`/library?album=${encodeURIComponent(album.album)}`} key={album.id}><span className="cover">{album.artwork_url ? <img src={album.artwork_url} alt=""/> : <ArtworkPlaceholder/>}</span><strong>{album.album}</strong><small>{album.albumartist || "Unknown artist"}</small></a>)}</div></section>
  </>;
}

export function Inbox({ data, threshold }: { data: Record<string, any>; threshold: number }) {
  const inboxes = data.inboxes || [];
  const [selected, setSelected] = useState(() => { const requested = new URLSearchParams(location.search).get("source"); return inboxes.find((row: any) => row.id === requested)?.id || inboxes[0]?.id || ""; });
  const [busy, setBusy] = useState(false);
  return <><PageHeader eyebrow="A place for every record" title="Incoming music">Review the details. Give every album its place.</PageHeader>
    {inboxes.length > 1 && <div className="segmented" role="tablist" aria-label="Inbox source">{inboxes.map((inbox: any) => <button role="tab" disabled={busy} aria-selected={selected === inbox.id} onClick={() => { setSelected(inbox.id); history.replaceState(null, "", `?source=${encodeURIComponent(inbox.id)}`); }} key={inbox.id}>{inbox.name}</button>)}</div>}
    {selected ? <Queue key={selected} source="inbox" threshold={threshold} inboxId={selected} onBusyChange={setBusy} sourceName={inboxes.find((row: any) => row.id === selected)?.name}/> : <div className="empty-state"><strong>No inboxes configured</strong><a href="/settings">Open Settings</a></div>}
  </>;
}

export function LibraryImport({ threshold }: { threshold: number }) {
  return <><div className="adoption-heading"><a href="/library" className="library-return">← Library</a><PageHeader eyebrow="Library Import" title="Adoption queue">Bring existing files into Cratekeep. Audio stays in its current location.</PageHeader></div><Queue source="library-import" threshold={threshold}/></>;
}

function LibraryInspector({ id, onClose, onBusyChange }: { id: number; onClose: () => void; onBusyChange: (value: boolean) => void }) {
  const notify = useToast();
  const [album, setAlbum] = useState<any>(null), [error, setError] = useState("");
  const [track, setTrack] = useState<any>(null), [preview, setPreview] = useState<any>(null);
  const [editing, setEditing] = useState(false);
  const [removal, setRemoval] = useState<any>(null), [deleteFiles, setDeleteFiles] = useState(false);
  const [removing, setRemoving] = useState(false), [removalResult, setRemovalResult] = useState<any>(null);
  const [pending, setPending] = useState(false);
  const region = useRef<HTMLDivElement>(null);
  const busy = pending || removing;
  useEffect(() => { onBusyChange(busy); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange(false), [onBusyChange]);
  useEffect(() => { region.current?.focus(); if (window.matchMedia?.("(max-width: 880px)")?.matches) region.current?.scrollIntoView?.({ block: "start" }); }, [id]);
  const previewRemoval = async () => {
    setError(""); setDeleteFiles(false); setRemovalResult(null);
    setPending(true);
    try { setRemoval(await api<any>(`/api/library/albums/${id}/removal/preview`, { method: "POST", body: JSON.stringify({ expected_fingerprint: album.fingerprint }) })); }
    catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const removeAlbum = async () => {
    setRemoving(true); setError("");
    setPending(true);
    try {
      const result = await api<any>(`/api/library/albums/${id}/removal/apply`, { method: "POST", body: JSON.stringify({ preview_id: removal.preview_id, expected_fingerprint: removal.fingerprint, delete_files: deleteFiles, confirmed: true }) });
      setRemovalResult(result);
      if (result.status === "complete") notify(result.message);
    } catch (reason) { setError((reason as Error).message); } finally { setRemoving(false); setPending(false); }
  };
  useEffect(() => { void api<any>(`/api/library/albums/${id}`).then(setAlbum).catch(reason => setError(reason.message)); }, [id]);
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    setPending(true); setError("");
    try { const result = await api<any>(`/api/library/albums/${id}`, { method: "PATCH", body: JSON.stringify({ expected_fingerprint: album.fingerprint, album: form.get("album"), albumartist: form.get("albumartist"), genre: form.get("genre"), year: Number(form.get("year") || 0) }) }); setAlbum(result.album || result); setEditing(false); setTrack(null); notify("Album metadata saved."); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const saveTrack = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const form = new FormData(event.currentTarget);
    setPending(true); setError("");
    try { const result = await api<any>(`/api/library/albums/${id}/tracks/${track.id}`, { method: "PATCH", body: JSON.stringify({ expected_fingerprint: album.fingerprint, title: form.get("title"), artist: form.get("artist"), track: Number(form.get("track") || 0), disc: Number(form.get("disc") || 0) }) }); setAlbum(result.album || result); setTrack(null); notify("Track metadata saved."); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const rematch = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); const musicbrainzId = String(new FormData(event.currentTarget).get("musicbrainz_id") || "");
    setPending(true); setError("");
    try { setPreview(await api<any>(`/api/library/albums/${id}/rematch/preview`, { method: "POST", body: JSON.stringify({ musicbrainz_id: musicbrainzId, expected_fingerprint: album.fingerprint }) })); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const applyPreview = async () => {
    setPending(true); setError("");
    try { const result = await api<any>(`/api/library/albums/${id}/rematch/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint }) }); setAlbum(result.album || result); setPreview(null); notify("Album updated."); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const previewArtwork = async (file: File) => {
    const form = new FormData(); form.set("artwork", file); form.set("expected_fingerprint", album.fingerprint);
    setPending(true); setError("");
    try { const response = await fetch(`/api/library/albums/${id}/artwork/upload-preview`, { method: "POST", body: form }); const result = await response.json(); if (!response.ok) throw new Error(result.error); setPreview({ ...result, kind: "artwork" }); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  const applyArtwork = async () => {
    setPending(true); setError("");
    try { const result = await api<any>(`/api/library/albums/${id}/artwork/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint }) }); setAlbum(result.album || result); setPreview(null); notify("Album updated."); } catch (reason) { setError((reason as Error).message); } finally { setPending(false); }
  };
  return <div className="managed-record-inspector"><div className="dialog" role="region" aria-labelledby={album ? "managed-album-title" : "managed-record-title"} tabIndex={-1} ref={region}><header><div><p className="eyebrow">Your collection / {editing ? "Edit record" : "Record details"}</p><h2 id="managed-record-title">{editing ? "Edit metadata" : "Album details"}</h2></div><div className="record-header-actions"><button className="inspector-back text-button" type="button" disabled={busy} onClick={onClose}>Back to albums</button>{album && !editing && !removal && !removalResult && <button type="button" className="metadata-edit text-button" disabled={busy} onClick={() => { setEditing(true); setError(""); }}><span aria-hidden="true">✎</span> Edit metadata</button>}<button disabled={busy} onClick={() => removalResult ? location.reload() : onClose()} aria-label="Close album">×</button></div></header><div className="dialog-body"><fieldset className="record-controls" disabled={busy} aria-busy={busy}>{error && <Notice kind="error">{error}</Notice>}{removalResult ? <>{removalResult.status !== "complete" && <Notice kind="error">{removalResult.message}</Notice>}{removalResult.attempting && <p>Outcome requires inspection: {removalResult.attempting}</p>}<p>{removalResult.removed.length} tracks removed from Cratekeep; {removalResult.remaining.length} remain.</p>{removalResult.errors.map((row: any) => <p key={row.path}>{row.path}: {row.error}{row.file_deleted ? " (audio already deleted; database cleanup needs review)" : ""}</p>)}</> : removal ? <section aria-label="Remove album confirmation"><h3>Remove from library</h3><p>{removal.tracks.length} managed tracks will be removed from Cratekeep.</p><ul>{removal.tracks.map((row: any) => <li key={row.id}>{row.title} — {row.relative_path}</li>)}</ul><label className="check"><input type="checkbox" checked={deleteFiles} disabled={removing || !removal.delete_allowed} onChange={event => setDeleteFiles(event.target.checked)}/>Also delete files</label>{removal.delete_error && <Notice kind="error">File deletion unavailable: {removal.delete_error}</Notice>}<p>{deleteFiles ? "The listed audio files will be permanently deleted. Artwork, other files and folders will be kept. This cannot be undone." : "Audio files, tags and artwork will be kept. Other music players will still see these files. You can explicitly adopt them again through Library Import."}</p></section> : !album ? <p>Loading…</p> : <>
    <div className="album-detail-head"><span className="album-detail-art">{album.artwork_url ? <img src={album.artwork_url} alt={`Artwork for ${album.album}`}/> : <ArtworkPlaceholder/>}{<label className="artwork-control"><span>Replace artwork</span><input type="file" aria-label="Upload replacement" accept="image/*" onChange={event => event.target.files?.[0] && void previewArtwork(event.target.files[0])}/></label>}</span><div><h3 id="managed-album-title" className="managed-album-title">{album.album}</h3><strong>{album.albumartist || "Unknown artist"}</strong><span>{album.genre || "No genre"} · {album.year || "No year"}</span><small>{(album.tracks || album.items || []).length} {(album.tracks || album.items || []).length === 1 ? "track" : "tracks"}</small></div></div>
    <div className="managed-release-id"><span>MusicBrainz release MBID</span>{album.mb_albumid ? <a href={`https://musicbrainz.org/release/${encodeURIComponent(album.mb_albumid)}`} target="_blank" rel="noreferrer"><code>{album.mb_albumid}</code></a> : <span>Not associated with a MusicBrainz release</span>}</div>
    {editing && <form className="edit-grid subpanel" onSubmit={save} id="album-form"><h3 className="editor-heading">Edit album metadata</h3><label>Album<input name="album" defaultValue={album.album}/></label><label>Album artist<input name="albumartist" defaultValue={album.albumartist}/></label><label>Genre<input name="genre" defaultValue={album.genre}/></label><label>Year<input name="year" type="number" defaultValue={album.year || ""}/></label></form>}
    <div className="record-tracks-heading"><h3>Tracks</h3><small>{(album.tracks || album.items || []).length} in this record{editing ? " · Select a title to edit its tags" : ""}</small></div>
    {track && <form id="track-form" key={track.id} onSubmit={saveTrack}/>}
    <table className={`record-tracks${editing ? " record-tracks-editable" : ""}`}><caption className="sr-only">Tracks on {album.album}</caption><thead><tr><th scope="col">{editing ? "Track" : "#"}</th>{editing && <th scope="col">Disc</th>}<th scope="col">Title</th><th scope="col">Artist</th></tr></thead><tbody>{(album.tracks || album.items || []).map((row: any) => {
      const active = track?.id === row.id;
      return <tr key={row.id} className={active ? "is-editing" : undefined}>
        <td>{active ? <input form="track-form" aria-label="Track" name="track" type="number" min="0" step="1" defaultValue={row.track}/> : <>{!editing && row.disc > 1 ? `${row.disc}.` : ""}{row.track || "—"}</>}</td>
        {editing && <td>{active ? <input form="track-form" aria-label="Disc" name="disc" type="number" min="0" step="1" defaultValue={row.disc}/> : row.disc || "—"}</td>}
        <th scope="row">{active ? <input form="track-form" aria-label="Title" name="title" defaultValue={row.title} autoFocus/> : editing ? <button className="track-edit" type="button" aria-label={`Edit track ${row.title}`} onClick={() => setTrack(row)}>{row.title}</button> : row.title}</th>
        <td>{active ? <input form="track-form" aria-label="Artist" name="artist" defaultValue={row.artist}/> : row.artist || "—"}</td>
      </tr>;
    })}</tbody></table>
    <form className="record-rematch" onSubmit={rematch}><div><h3>MusicBrainz rematch</h3><p>Preview a different release before applying its metadata.</p></div><label>Release UUID<input name="musicbrainz_id" required placeholder="MusicBrainz release ID"/></label><button type="submit" className="secondary">Preview rematch</button></form>

    {preview && <div className="subpanel"><h3>{preview.kind === "artwork" ? "Artwork preview" : "Metadata preview"}</h3>{preview.kind === "artwork" && <img className="art-preview" src={`/api/library/albums/${id}/artwork/previews/${preview.preview_id}`} alt="Proposed cover artwork"/>}<p>{preview.message || (preview.applicable === false ? "This match cannot be applied safely." : "Preview is ready to apply.")}</p></div>}
  </>}</fieldset></div><footer className="record-action-footer" aria-label="Album actions">
    {removalResult ? <button type="button" disabled={busy} onClick={() => location.reload()}>Refresh Library</button> : removal ? <><button className="secondary" type="button" disabled={busy} onClick={() => setRemoval(null)}>Cancel removal</button><button className="danger-action" type="button" disabled={busy} onClick={() => void removeAlbum()}>{removing ? "Removing…" : deleteFiles ? "Delete listed files and remove album" : "Remove from Cratekeep; keep files"}</button></> : preview ? <><button className="secondary" type="button" disabled={busy} onClick={() => setPreview(null)}>Cancel</button><button type="button" disabled={busy || preview.applicable === false} onClick={preview.kind === "artwork" ? applyArtwork : applyPreview}>Apply {preview.kind === "artwork" ? "artwork" : "rematch"}</button></> : track ? <><button className="secondary" type="button" disabled={busy} onClick={() => setTrack(null)}>Cancel</button><button type="submit" form="track-form" disabled={busy}>Save track</button></> : editing ? <><button className="secondary" type="button" disabled={busy} onClick={() => { setEditing(false); setTrack(null); }}>Cancel changes</button><button type="submit" form="album-form" disabled={busy}>Save album</button></> : album ? <button className="secondary danger-action" type="button" disabled={busy} onClick={() => void previewRemoval()}>Remove from library</button> : null}
  </footer></div></div>;
}

function Help({ label, children }: { label: string; children: string }) {
  const [open, setOpen] = useState(false);
  const id = `help-${label.toLowerCase().replaceAll(" ", "-")}`;
  return <span className="help-wrap"><button className="help" type="button" aria-label={`Help: ${label}`} aria-expanded={open} aria-controls={id} title={children} onClick={() => setOpen(value => !value)} onKeyDown={event => { if (event.key === "Escape") setOpen(false); }}>?</button>{open && <span className="help-text" id={id} role="note">{children}</span>}</span>;
}

export function Library({ data }: { data: Record<string, any> }) {
  const { workspace: workspaceRef, divider, style: splitStyle } = useInspectorResize();
  const [open, setOpen] = useState<number | null>(null), [busy, setBusy] = useState(false), summary = data.summary || {};
  const page = data.page || 1, pages = data.page_count || 1;
  const artistView = data.view === "artists";
  const recordCount = artistView ? data.matching_artists ?? data.artists?.length ?? 0 : data.matching_albums ?? data.albums?.length ?? 0;
  const pageUrl = (number: number) => `/library?${new URLSearchParams({ q: data.query || "", sort: data.sort || (artistView ? "artist" : "recent"), order: data.order || (artistView ? "asc" : "desc"), artist: data.artist_filter || "", album: data.album_filter || "", ...(artistView ? { view: "artists" } : {}), page: String(number) })}`;
  return <div className="library-collection"><PageHeader eyebrow="The listening desk / Library" title="Managed collection">The records you've made room for.</PageHeader>
    <div className="collection-bar"><dl className="collection-counts"><div><dt>Albums</dt><dd>{summary.albums || 0}</dd></div><div><dt>Tracks</dt><dd>{summary.tracks || 0}</dd></div><div><dt>Artists</dt><dd>{summary.artists || 0}</dd></div><div><dt>Size</dt><dd>{formatBytes(summary.bytes)}</dd></div></dl><a className="library-import-link" href="/library-import">Import existing files →</a></div>
    <div ref={workspaceRef} style={splitStyle} className={`desk-workspace library-workspace${artistView ? " is-artists" : ""}${open != null ? " is-detail-open" : ""}`}><div className="desk-queue"><nav className="library-views" aria-label="Library view"><a href="/library" aria-current={!artistView ? "page" : undefined}>Albums</a><a href="/library?view=artists" aria-current={artistView ? "page" : undefined}>Artists</a></nav>{data.artist_filter && <p className="artist-breadcrumb"><a href="/library?view=artists">All artists</a><span aria-hidden="true"> / </span>{data.artist_filter}</p>}<form className="collection-search" method="get" onSubmit={event => { if (busy) event.preventDefault(); }}><label><span className="sr-only">Search library</span><input name="q" defaultValue={data.query} placeholder={artistView ? "Find an artist" : "Find an artist, album or song"}/>{artistView && <input type="hidden" name="view" value="artists"/>}{data.artist_filter && <input type="hidden" name="artist" value={data.artist_filter}/>}</label>{!artistView && <label><span className="sr-only">Sort library</span><select name="sort" defaultValue={data.sort || (artistView ? "artist" : "recent")}><option value="artist">Artist</option><option value="album">Album</option><option value="year">Year</option><option value="recent">Recently added</option></select></label>}<label><span className="sr-only">Sort order</span><select name="order" defaultValue={data.order || (artistView ? "asc" : "desc")}><option value="asc">Ascending</option><option value="desc">Descending</option></select></label><button type="submit" className="secondary collection-search-submit">Search</button></form>
    <section className="record-catalog" aria-labelledby="browse-title"><header><h2 id="browse-title">{artistView ? "Browse artists" : "Browse albums"}</h2><p>{recordCount} {artistView ? recordCount === 1 ? "artist" : "artists" : recordCount === 1 ? "record" : "records"}{data.artist_filter ? ` · ${data.artist_filter}` : ""}{data.album_filter ? ` · ${data.album_filter}` : ""}</p>{(data.query || data.artist_filter || data.album_filter) && <a href={artistView ? "/library?view=artists" : "/library"}>Clear filters</a>}</header>
      {artistView ? <><div className="artist-columns" aria-hidden="true"><span>Artist</span><span>Albums</span><span>Genre</span></div><ul className="artist-ledger">{(data.artists || []).map((artist: any) => <li key={artist.artist}><a className="artist-row" href={`/library?${new URLSearchParams({ artist: artist.artist })}`}><strong>{artist.artist}</strong><span>{artist.album_count}</span><small>{artist.genres || "—"}</small></a></li>)}</ul>{!recordCount && <div className="empty-state"><strong>No artists in this view</strong><p>Try a different search or import an album.</p></div>}</> : <>
      <div className="collection-columns" aria-hidden="true"><span>Album / Artist</span><span>Year</span><span>Tracks</span><span>Genre</span></div>
      <ul className="collection-ledger">{(data.albums || []).map((album: any) => <li key={album.id}><button type="button" className={`album-card library-record${open === album.id ? " is-active" : ""}`} disabled={busy} aria-pressed={open === album.id} data-album-id={album.id} onClick={() => setOpen(album.id)}><span className="record-identity"><span className="record-cover" data-album-cover>{album.artwork_url ? <img src={album.artwork_url} alt=""/> : <ArtworkPlaceholder/>}</span><span><strong>{album.album}</strong><small>{album.albumartist || "Unknown artist"}<span className="record-mobile-meta"> · {album.year || "—"} · {album.genre || "No genre"}</span></small></span></span><span className="record-year">{album.year || "—"}</span><span className="record-count">{album.track_count ?? "—"}</span><span className="record-genre">{album.genre || "—"}</span></button></li>)}</ul>
      {!recordCount && <div className="empty-state"><strong>No records in this view</strong><p>{data.query || data.artist_filter || data.album_filter ? "Try a different search or clear your filters." : "Import an album to begin your collection."}</p></div>}
      </>}
      <nav className="collection-pagination" aria-label="Library pages"><span>Page {page} of {pages} · {data.page_size || 25} per page</span>{page > 1 ? <a href={pageUrl(page-1)} aria-disabled={busy} onClick={event => { if (busy) event.preventDefault(); }}>Previous</a> : <span aria-disabled="true">Previous</span>}{page < pages ? <a href={pageUrl(page+1)} aria-disabled={busy} onClick={event => { if (busy) event.preventDefault(); }}>Next</a> : <span aria-disabled="true">Next</span>}</nav>
    </section>
    </div>{!artistView && divider}{!artistView && <aside className="desk-inspector library-inspector" aria-label="Album inspector">{open != null ? <LibraryInspector key={open} id={open} onBusyChange={setBusy} onClose={() => setOpen(null)}/> : <div className="inspector-empty"><p className="eyebrow">Your collection</p><h2>Every record has a story.</h2><p>Select an album to inspect its tracks, artwork and metadata.</p></div>}</aside>}</div>
  </div>;
}

type InboxSetting = { key: string; id: string; name: string; path: string; auto_match: boolean; discovery?: string };
type InboxDraft = Pick<InboxSetting, "key" | "name" | "path"> & { isNew?: boolean };

export function Settings({ data, threshold }: { data: Record<string, any>; threshold: number }) {
  const settings = data.settings || {}, initial = data.inboxes || [];
  const notify = useToast(), flashed = useRef(false);
  useEffect(() => {
    if (flashed.current) return;
    flashed.current = true;
    (data.flashes || []).filter((row: any) => row[0] !== "error").forEach((row: any) => notify(row[1]));
  }, [data.flashes, notify]);
  const nextInbox = useRef(0);
  const [inboxes, setInboxes] = useState<InboxSetting[]>(() => initial.map((inbox: InboxSetting) => ({ ...inbox, key: inbox.id })));
  const [draft, setDraft] = useState<InboxDraft | null>(null);
  const editor = useRef<HTMLTableRowElement>(null);
  const [tab, setTab] = useState(() => {
    const hash = location.hash.slice(1);
    return hash === "matching" ? "metadata" : ["general", "metadata", "advanced", "about"].includes(hash) ? hash : "general";
  });
  function finishInbox() {
    if (!draft || !Array.from(editor.current?.querySelectorAll("input:not([type=hidden])") || []).every(input => (input as HTMLInputElement).reportValidity())) return;
    setInboxes(rows => rows.map(row => row.key === draft.key ? { ...row, name: draft.name.trim(), path: draft.path.trim() } : row));
    setDraft(null);
  }
  function cancelInbox() {
    if (draft?.isNew) setInboxes(rows => rows.filter(row => row.key !== draft.key));
    setDraft(null);
  }
  function addInbox() {
    const inbox = { key: `new-${nextInbox.current++}`, id: "", name: `Inbox ${inboxes.length + 1}`, path: data.default_inbox || "", auto_match: false, discovery: "5" };
    setInboxes(rows => [...rows, inbox]);
    setDraft({ ...inbox, isNew: true });
  }
  return <div className="compact-settings">
    <PageHeader eyebrow="Settings" title="Cratekeep settings">Library paths, matching and metadata.</PageHeader>
    {(data.flashes || []).filter((row: any) => row[0] === "error").map((row: any, index: number) => <Notice key={index} kind="error">{row[1]}</Notice>)}
    <div className="settings-layout"><div className="settings-tabs" role="tablist" aria-label="Settings sections">
      {[["general", "General"], ["metadata", "Metadata"], ["advanced", "Advanced"], ["about", "About"]].map(([key, label]) => <button type="button" role="tab" aria-selected={tab === key} onClick={() => { setTab(key); location.hash = key; }} key={key}>{label}</button>)}
    </div>
    <form method="post" className="settings-form" onSubmit={event => { if (draft) event.preventDefault(); }}>
      <input type="hidden" name="settings_tab" value={tab}/>
      <section hidden={tab !== "general"} className="settings-section">
        <div className="settings-card inbox-settings-card">
          <h2>Inboxes <Help label="Inboxes">Named source folders where new music waits before review and import. Each Inbox must be a separate container-visible path outside the managed Library. Edit or delete configuration rows, then Save settings. Deleting a row does not delete files. Discovery is independent per Inbox. Choose a timed interval or Manual only for downloads that arrive one file at a time. Instant waits briefly for files to settle and keeps a five-minute safety scan; it cannot know whether a download is complete.</Help></h2>
          <table className="inbox-settings-table"><caption className="sr-only">Configured Inboxes</caption>
            <thead><tr><th scope="col">Name</th><th scope="col">Inbox path</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{inboxes.map(inbox => <tr data-inbox-row key={inbox.key} ref={draft?.key === inbox.key ? editor : undefined}>
              <td><input type="hidden" name="inbox_id" value={inbox.id}/><input type="hidden" name="inbox_name" value={inbox.name}/>
                {draft?.key === inbox.key ? <label><span className="sr-only">Name for {inbox.name}</span><input autoFocus value={draft.name} pattern=".*\S.*" onChange={event => setDraft({ ...draft, name: event.target.value })} required/></label> : <strong>{inbox.name}</strong>}
              </td>
              <td><input type="hidden" name="inbox_path" value={inbox.path}/>
                {draft?.key === inbox.key ? <label><span className="sr-only">Inbox path for {inbox.name}</span><input value={draft.path} pattern=".*\S.*" onChange={event => setDraft({ ...draft, path: event.target.value })} required/></label> : <code className="inbox-printed-path">{inbox.path}</code>}
                <label className="inbox-discovery-setting">Discovery for {inbox.name}<select name="inbox_discovery" value={inbox.discovery || "5"} onChange={event => setInboxes(rows => rows.map(row => row.key === inbox.key ? { ...row, discovery: event.target.value } : row))}>
                  {[["manual", "Manual only"], ["instant", "Instant"], ["5", "Every 5 minutes"], ["15", "Every 15 minutes"], ["30", "Every 30 minutes"], ["60", "Every hour"], ["360", "Every 6 hours"], ["1440", "Every 24 hours"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select></label>
              </td>
              <td><div className="inbox-row-actions">{draft?.key === inbox.key ? <><button className="text-button" type="button" onClick={finishInbox}>Done</button><button className="text-button" type="button" onClick={cancelInbox}>Cancel</button></> : <><button className="text-button" type="button" aria-label={`Edit ${inbox.name}`} disabled={draft !== null} onClick={() => setDraft({ key: inbox.key, name: inbox.name, path: inbox.path })}>Edit</button><button className="text-button" type="button" aria-label={`Delete ${inbox.name}`} disabled={draft !== null} onClick={() => setInboxes(rows => rows.filter(row => row.key !== inbox.key))}>Delete</button></>}</div></td>
            </tr>)}</tbody>
          </table>
          <button className="text-button" type="button" disabled={draft !== null} onClick={addInbox}>+ Add inbox</button>
        </div>
        <div className="settings-card"><h2>Library <Help label="Library path">The managed destination where Cratekeep stores approved albums.</Help></h2><label>Library path<input name="library_path" defaultValue={settings.library_path || data.default_library} required/></label></div>
        <div className="settings-card"><h2>Notifications <Help label="Notifications">Automatically dismiss notifications after the configured interval. Timing pauses while a notification is hovered or focused.</Help></h2><label>Toast duration (seconds)<input className="short-setting" type="number" name="toast_duration_seconds" min="1" max="300" step="1" defaultValue={settings.toast_duration_seconds || 5} required/></label></div>
      </section>
      <section hidden={tab !== "metadata"} className="settings-section metadata-settings">
        <div className="settings-stack">
        <div className="settings-card"><h2>Artwork <Help label="Artwork">Controls fetching, sidecars, embedding, and replacement during approved imports.</Help></h2>
          <label className="check"><input type="checkbox" name="fetch_art" defaultChecked={settings.fetch_art === "1"}/>Fetch Cover Art Archive artwork</label>
          <label className="check"><input type="checkbox" name="art_sidecar" defaultChecked={settings.art_sidecar === "1"}/>Save cover.jpg sidecars</label>
          <label className="check"><input type="checkbox" name="art_embed" defaultChecked={settings.art_embed === "1"}/>Embed artwork in audio files</label>
          <label className="check"><input type="checkbox" name="art_replace" defaultChecked={settings.art_replace === "1"}/>Replace existing artwork</label>
        </div>
        <div className="settings-card"><h2>Feature in Title <Help label="Feature in Title">Moves trailing featured-artist credits into the track title before matching, using the chosen format. For example, “Song” by “Artist feat. Guest” can become “Song feat. Guest” by “Artist”.</Help></h2>
          <label className="check"><input type="checkbox" name="ftintitle_enabled" defaultChecked={data.ftintitle?.enabled}/>Enable Feature in Title</label>
          <label>Title format<input name="ftintitle_format" defaultValue={data.ftintitle?.format || "feat. {0}"}/></label>
          <label className="check"><input type="checkbox" name="ftintitle_keep_in_artist" defaultChecked={data.ftintitle?.keep_in_artist}/>Also keep the feature in the artist field</label>
        </div>
        </div>
        <div className="settings-card"><h2>Matching <Help label="Match threshold">The minimum candidate confidence treated as ready for safe bulk import when all other safety checks pass. Lower scores remain for review. Library Import selected actions can explicitly approve saved matches regardless of score.</Help></h2>
          <label>Matched threshold (%)<input className="short-setting" name="match_threshold" type="number" min="1" max="100" step="1" defaultValue={threshold} required/></label>
          <label>Automatically find matches for<select multiple className="inbox-match-select" name="inbox_auto_match_id" size={Math.min(6, Math.max(3, inboxes.length))} value={inboxes.filter(row => row.auto_match && row.id).map(row => row.id)} onChange={event => { const selected = new Set(Array.from(event.target.selectedOptions, option => option.value)); setInboxes(rows => rows.map(row => ({ ...row, auto_match: selected.has(row.id) }))); }}>{inboxes.map(inbox => <option key={inbox.key} value={inbox.id} disabled={!inbox.id}>{inbox.name}{!inbox.id ? " (save first)" : ""}</option>)}</select></label>
          <div className="match-selection-actions"><button className="text-button" type="button" onClick={() => setInboxes(rows => rows.map(row => ({ ...row, auto_match: Boolean(row.id) })))}>Select all</button><button className="text-button" type="button" onClick={() => setInboxes(rows => rows.map(row => ({ ...row, auto_match: false })))}>Clear selection</button><Help label="Automatic Inbox matching">Select the named Inboxes to match automatically during discovery. This does not import music. Use Command or Control to select multiple entries, or Select all / Clear selection. Save new Inboxes first to enable automatic matching for them.</Help></div>
        </div>
      </section>
      <section hidden={tab !== "advanced"} className="advanced-settings"><div className="settings-card"><h2>Beets YAML <Help label="Beets YAML">Advanced beets configuration. Invalid YAML is rejected and the saved configuration remains unchanged.</Help></h2><p>Folder and file names come from <code>paths:</code> in this YAML. <a href="https://beets.readthedocs.io/en/stable/reference/pathformat.html" target="_blank" rel="noreferrer">Beets naming guide ↗</a></p><details><summary>Naming examples</summary><p><code>$albumartist/$album/$track $title</code> puts each album in its artist’s folder.</p><p><code>$albumartist/$year - $album/$track $title</code> adds the year before the album name.</p><p>Use <code>$albumartist</code> to keep compilation tracks together. Beets adds the file extension. Saving these settings does not rename existing files.</p></details><label className="sr-only" htmlFor="beets-config">Beets configuration</label><textarea id="beets-config" name="beets_config" defaultValue={data.beets_config}/></div></section>
      <section hidden={tab !== "about"} className="about-settings settings-section">
        <div className="settings-card">
          <div className="about-brand"><img src="/static/cratekeep-logo.png" width="64" height="64" alt=""/><div><h2>Cratekeep</h2><p>Music library management with Beets and MusicBrainz.</p></div></div>
          <dl className="about-version"><dt>Version</dt><dd>{data.about?.version && data.about.version !== "development" ? `${data.about.version} · ` : ""}{data.about?.build && data.about.build !== "unknown" ? `Build ${data.about.build.slice(0, 12)}` : "Development"}</dd><dt>License</dt><dd><a href="https://github.com/kylejschultz/cratekeep/blob/main/LICENSE" target="_blank" rel="noreferrer">GPLv3</a></dd></dl>
          <div className="about-links"><a href={data.about?.discord_url} target="_blank" rel="noreferrer">Join the Discord</a><a href="https://github.com/kylejschultz/cratekeep" target="_blank" rel="noreferrer">Source code</a><a href="https://github.com/kylejschultz/cratekeep/issues" target="_blank" rel="noreferrer">Report an issue</a></div>
        </div>
        <div className="settings-card">
          <h2>Anonymous analytics</h2>
          <label className="check"><input type="checkbox" name="analytics_enabled" defaultChecked={settings.analytics_enabled === "1"}/>Share anonymous install analytics</label>
          <p>Help us understand how Cratekeep is used. When enabled, the server sends Beacon a random install ID, build, architecture, operating system and total artist, album and song counts once per day.</p>
          <p>No artist or track names, paths, account details or listening history are included. Beacon receives the connection’s IP address as part of the request. Analytics are off by default and can be disabled here at any time.</p>
          <a href="https://github.com/kylejschultz/beacon" target="_blank" rel="noreferrer">How Beacon works</a>
        </div>
      </section>
      <div className="form-footer">{draft && <small>Finish or cancel the Inbox edit before saving.</small>}<button type="submit" disabled={draft !== null}>Save settings</button></div>
    </form></div>
  </div>;
}

export function Page({ boot }: { boot: Bootstrap }) {
  if (boot.page === "overview") return <Overview data={boot.data}/>;
  if (boot.page === "inbox") return <Inbox data={boot.data} threshold={boot.threshold}/>;
  if (boot.page === "library-import") return <LibraryImport threshold={boot.threshold}/>;
  if (boot.page === "library") return <Library data={boot.data}/>;
  return <Settings data={boot.data} threshold={boot.threshold}/>;
}
