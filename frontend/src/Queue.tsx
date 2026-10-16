import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatBytes } from "./api";
import { Notice } from "./Shell";
import { ReviewModal } from "./ReviewModal";
import type { Album, QueueScope } from "./types";
import { albumStatus } from "./status";

type Source = "inbox" | "library-import";
type QueueProps = { source: Source; threshold: number; inboxId?: string };

function FileRow({ file }: { file: Record<string, any> }) {
  const name = String(file.name || file.filename || file.path || "Unknown file").split(/[\\/]/).pop();
  const type = String(file.type || file.extension || name?.split(".").pop() || "file").toUpperCase();
  return <li className="file-row"><span>{name}</span><span>{type} · {formatBytes(file.size ?? file.bytes)}</span></li>;
}

function AlbumRow({ album, threshold, matching, busy, selected, onSelect, onMatch, onReview }: { album: Album; threshold: number; matching: boolean; busy: boolean; selected: boolean; onSelect: (value: boolean) => void; onMatch: () => Promise<void>; onReview: (album: Album) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [menu, setMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const state = albumStatus(album, threshold);
  const canImport = state.key === "matched" || state.key === "threshold";
  const files = album.files || album.tracks || [];
  useEffect(() => { const close = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(false); }; addEventListener("mousedown", close); return () => removeEventListener("mousedown", close); }, []);
  return <li className={`album-node${matching ? " is-matching" : ""}`} data-album-id={album.id} aria-busy={matching}>
    <div className="album-row">
      <input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} aria-label={`Select album ${album.album || album.name || "Untitled album"}`}/>
      <button className="expand" type="button" aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} ${album.album || album.name || "album"}`} onClick={() => setExpanded(value => !value)}>{expanded ? "⌄" : "›"}</button>
      <div className="row-identity"><strong>{album.album || album.name || album.relative_path || "Untitled album"}</strong><small>{album.albumartist || album.artist || album.path || "Unknown artist"}</small></div>
      <span className="row-count">{album.track_count ?? album.file_count ?? files.length} tracks</span>
      <span className={`status ${matching ? "pending" : state.key}`} title={state.key === "error" ? album.execution_error || album.candidate_error : undefined}>{matching ? "Matching…" : state.label}</span>
      <span className="match">{state.score == null ? "—" : `${Math.round(state.score)}%`}</span>
      <div className="overflow" ref={menuRef}><button type="button" aria-haspopup="menu" aria-expanded={menu} aria-label={`Actions for ${album.album || album.name || "album"}`} onClick={() => setMenu(value => !value)}>•••</button>{menu && <div role="menu"><button role="menuitem" type="button" disabled={busy} title="Match albums in the containing folder" onClick={() => { void onMatch(); }}>{matching && <span className="spinner"/>}{matching ? "Matching…" : "Match"}</button><button role="menuitem" type="button" disabled={busy || !canImport} onClick={() => onReview(album)}>Import</button><button role="menuitem" type="button" disabled={busy} onClick={() => onReview(album)}>Review</button></div>}</div>
    </div>
    {expanded && <ul className="file-list">{files.length ? files.map((file, index) => <FileRow key={file.id || file.path || index} file={file}/>) : <li className="empty-inline">No file detail is available until this album is matched.</li>}</ul>}
  </li>;
}

function ScopeRow({ scope, source, threshold, inboxId, selected, excludedAlbums, matching, busy, refreshVersion, onSelect, onAlbumSelect, onAlbumsLoaded, onMatch, onReview }: { scope: QueueScope; source: Source; threshold: number; inboxId?: string; selected: boolean; excludedAlbums: Set<number>; matching: boolean; busy: boolean; refreshVersion: number; onSelect: (value: boolean) => void; onAlbumSelect: (albums: Album[], albumId: number, value: boolean) => void; onAlbumsLoaded: (albums: Album[]) => void; onMatch: (scope: string) => Promise<void>; onReview: (album: Album) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [albums, setAlbums] = useState<Album[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const checkbox = useRef<HTMLInputElement>(null);
  const name = scope.name || scope.folder || scope.path || "Folder";
  const scopeValue = scope.path || scope.folder || scope.name || "";
  const loadAlbums = useCallback(async () => {
    if (loading) return;
    setLoading(true); setError("");
    try {
      const url = source === "inbox"
        ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes/albums?scope=${encodeURIComponent(scopeValue)}`
        : `/api/library-import/folders/albums?folder=${encodeURIComponent(scopeValue)}`;
      const result = await api<any>(url);
      const next = result.albums || result.reviews || [];
      setAlbums(next); onAlbumsLoaded(next);
    } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); }
  }, [inboxId, loading, onAlbumsLoaded, scopeValue, source]);
  useEffect(() => { if (expanded) void loadAlbums(); }, [refreshVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  const expand = () => {
    const next = !expanded; setExpanded(next);
    if (next && !albums.length) void loadAlbums();
  };
  const selectedCount = selected ? albums.filter(album => !excludedAlbums.has(album.id)).length : 0;
  useEffect(() => { if (checkbox.current) checkbox.current.indeterminate = selected && albums.length > 0 && selectedCount > 0 && selectedCount < albums.length; }, [albums.length, selected, selectedCount]);
  return <li className={`folder-node${matching ? " is-matching" : ""}`} aria-busy={matching}>
    <div className="folder-row"><input ref={checkbox} type="checkbox" checked={selected && (!albums.length || selectedCount === albums.length)} onChange={event => onSelect(event.target.checked)} aria-label={`Select folder ${name}`}/><button className="expand" type="button" onClick={expand} aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} folder ${name}`}>{expanded ? "⌄" : "›"}</button><div className="row-identity"><strong>{name}</strong><small>{scope.path && scope.path !== name ? scope.path : "Folder"}</small></div><span className="row-count">{scope.album_count ?? scope.albums ?? 0} albums · {scope.file_count ?? scope.files ?? 0} files</span></div>
    {expanded && <div className="scope-children">{loading ? <p className="loading">Loading albums…</p> : error ? <Notice kind="error">{error}</Notice> : albums.length ? <ul className="album-list">{albums.map(album => <AlbumRow key={album.id} album={album} threshold={threshold} matching={matching} busy={busy} selected={selected && !excludedAlbums.has(album.id)} onSelect={value => onAlbumSelect(albums, album.id, value)} onMatch={() => onMatch(scopeValue)} onReview={onReview}/>)}</ul> : <p className="empty-inline">No albums in this folder.</p>}</div>}
  </li>;
}

export function Queue({ source, threshold, inboxId }: QueueProps) {
  const [scopes, setScopes] = useState<QueueScope[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reviewQueue, setReviewQueue] = useState<Album[]>([]);
  const [success, setSuccess] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [excludedAlbums, setExcludedAlbums] = useState<Set<number>>(new Set());
  const [loadedAlbums, setLoadedAlbums] = useState<Map<string, Album[]>>(new Map());
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(1);
  const [total, setTotal] = useState(0);
  const selectPage = useRef<HTMLInputElement>(null);
  const [scanning, setScanning] = useState(false);
  const [matchingScopes, setMatchingScopes] = useState<Set<string>>(new Set());
  const [refreshVersion, setRefreshVersion] = useState(0);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const baseUrl = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes` : "/api/library-import/folders";
      const url = page === 1 ? baseUrl : `${baseUrl}?page=${page}`;
      const result = await api<any>(url);
      setScopes(result.scopes || result.folders || []);
      setPage(result.page || 1); setPageCount(result.page_count || 1); setTotal(result.total ?? (result.scopes || result.folders || []).length);
    } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); }
  }, [source, inboxId, page]);
  useEffect(() => { void load(); }, [load]);
  const matchScopes = async (values: string[]) => {
    if (!values.length) return;
    setScanning(true); setMatchingScopes(new Set(values)); setError(""); setSuccess("");
    try {
      const url = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes/scan` : "/api/library-import/folders/scan";
      await api(url, { method: "POST", body: JSON.stringify(source === "inbox" ? { scopes: values } : { folders: values }) });
      await load();
      setRefreshVersion(version => version + 1);
      setSuccess(`${values.length} folder${values.length === 1 ? "" : "s"} matched.`);
    } catch (reason) { setError(`Match failed: ${(reason as Error).message}`); } finally { setScanning(false); setMatchingScopes(new Set()); }
  };
  const match = () => matchScopes([...selected]);
  const refresh = async () => {
    await load();
    setRefreshVersion(version => version + 1);
  };
  const bulkBody = () => ({ ...(source === "inbox" ? { scopes: [...selected] } : { folders: [...selected] }), ...(excludedAlbums.size ? { excluded_album_ids: [...excludedAlbums] } : {}) });
  const bulkBase = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/bulk` : "/api/library-import/bulk";
  const importReady = async () => {
    if (!selected.size) return;
    setScanning(true); setError("");
    try {
      const preview = await api<any>(`${bulkBase}/preview`, { method: "POST", body: JSON.stringify(bulkBody()) });
      const ready = preview.ready_album_count ?? preview.available_album_count ?? preview.ready?.length ?? 0;
      if (!ready) { setSuccess("No selected albums are currently ready for safe bulk import."); return; }
      if (!window.confirm(`Import ${ready} ready album${ready === 1 ? "" : "s"}? Exceptions will remain in the queue.`)) return;
      const result = await api<any>(`${bulkBase}/execute`, { method: "POST", body: JSON.stringify({ ...bulkBody(), confirmed: true }) });
      setSuccess(`${result.imported?.length ?? ready} albums imported; ${result.failed?.length ?? 0} failed.`);
      const remaining = await api<any>(`${bulkBase}/exceptions`, { method: "POST", body: JSON.stringify(bulkBody()) });
      setReviewQueue(remaining.albums || remaining.exceptions || []);
      await load();
    } catch (reason) { setError((reason as Error).message); } finally { setScanning(false); }
  };
  const reviewExceptions = async () => {
    if (!selected.size) return;
    setScanning(true); setError("");
    try {
      const result = await api<any>(`${bulkBase}/exceptions`, { method: "POST", body: JSON.stringify(bulkBody()) });
      const albums = result.albums || result.exceptions || [];
      if (albums.length) setReviewQueue(albums); else setSuccess("No selected exceptions need review.");
    } catch (reason) { setError((reason as Error).message); } finally { setScanning(false); }
  };
  const visibleValues = scopes.map(scope => String(scope.path || scope.folder || scope.name || ""));
  const visibleSelected = visibleValues.filter(value => selected.has(value)).length;
  useEffect(() => { if (selectPage.current) selectPage.current.indeterminate = visibleSelected > 0 && visibleSelected < visibleValues.length; }, [visibleSelected, visibleValues.length]);
  const selectVisible = (checked: boolean) => {
    setSelected(current => { const next = new Set(current); visibleValues.forEach(value => checked ? next.add(value) : next.delete(value)); return next; });
    setExcludedAlbums(current => { const next = new Set(current); visibleValues.forEach(value => (loadedAlbums.get(value) || []).forEach(album => next.delete(album.id))); return next; });
  };
  const setAlbumSelection = (scopeValue: string, albums: Album[], albumId: number, checked: boolean) => {
    setLoadedAlbums(current => new Map(current).set(scopeValue, albums));
    const nextExcluded = new Set(excludedAlbums);
    if (!selected.has(scopeValue) && checked) albums.forEach(album => { if (album.id !== albumId) nextExcluded.add(album.id); });
    if (checked) nextExcluded.delete(albumId); else nextExcluded.add(albumId);
    const anySelected = albums.some(album => !nextExcluded.has(album.id));
    setExcludedAlbums(nextExcluded);
    setSelected(current => { const next = new Set(current); if (anySelected) next.add(scopeValue); else next.delete(scopeValue); return next; });
  };
  return <section className="queue">
    {success && <Notice kind="success">{success}</Notice>}
    <div className="queue-toolbar"><div><strong>{total} folders</strong><small>{selected.size ? `${selected.size} selected` : "Folder → album → file"}</small></div><div className="toolbar-actions"><button type="button" onClick={match} disabled={!selected.size || scanning} aria-busy={scanning && matchingScopes.size > 0}>{scanning && matchingScopes.size > 0 && <span className="spinner"/>}{scanning && matchingScopes.size > 0 ? "Matching…" : "Match Selected"}</button><button type="button" onClick={importReady} disabled={!selected.size || scanning}>Import</button><button className="secondary" type="button" onClick={reviewExceptions} disabled={!selected.size || scanning}>Review</button><button className="secondary" type="button" onClick={refresh} disabled={loading || scanning}>Refresh</button></div></div>
    {scopes.length > 0 && <div className="queue-pagination"><label><input ref={selectPage} type="checkbox" checked={visibleValues.length > 0 && visibleSelected === visibleValues.length} onChange={event => selectVisible(event.target.checked)}/> Select this page</label><span>Page {page} of {pageCount}</span><button className="secondary" type="button" disabled={page <= 1 || loading} onClick={() => setPage(value => value - 1)}>Previous</button><button className="secondary" type="button" disabled={page >= pageCount || loading} onClick={() => setPage(value => value + 1)}>Next</button></div>}
    {loading && !scopes.length ? <p className="loading">Loading queue…</p> : error ? <Notice kind="error">{error}</Notice> : scopes.length ? <ul className="folder-list">{scopes.map((scope, index) => { const value = String(scope.path || scope.folder || scope.name || ""); return <ScopeRow key={scope.id || value || index} scope={scope} source={source} threshold={threshold} inboxId={inboxId} selected={selected.has(value)} excludedAlbums={excludedAlbums} matching={matchingScopes.has(value)} busy={scanning} refreshVersion={refreshVersion} onSelect={checked => { setSelected(current => { const next = new Set(current); if (checked) next.add(value); else next.delete(value); return next; }); setExcludedAlbums(current => { const next = new Set(current); (loadedAlbums.get(value) || []).forEach(album => next.delete(album.id)); return next; }); }} onAlbumSelect={(albums, albumId, checked) => setAlbumSelection(value, albums, albumId, checked)} onAlbumsLoaded={albums => setLoadedAlbums(current => new Map(current).set(value, albums))} onMatch={async scopeValue => { await matchScopes([scopeValue]); }} onReview={album => setReviewQueue([album])}/>; })}</ul> : <div className="empty-state"><strong>Queue is clear</strong><p>No folders are waiting for review.</p></div>}
    {reviewQueue[0] && (
      <ReviewModal key={`${source}-${reviewQueue[0].id}`} album={reviewQueue[0]} source={source} onClose={() => setReviewQueue([])} onComplete={message => { setSuccess(message); setReviewQueue(current => current.slice(1)); void load(); }}/>
    )}
  </section>;
}
