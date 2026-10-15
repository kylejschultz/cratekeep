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

function AlbumRow({ album, threshold, scanning, onScan, onReview }: { album: Album; threshold: number; scanning: boolean; onScan: () => Promise<void>; onReview: (album: Album) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [menu, setMenu] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const state = albumStatus(album, threshold);
  const canImport = state.key === "matched" || state.key === "threshold";
  const files = album.files || album.tracks || [];
  useEffect(() => { const close = (event: MouseEvent) => { if (!menuRef.current?.contains(event.target as Node)) setMenu(false); }; addEventListener("mousedown", close); return () => removeEventListener("mousedown", close); }, []);
  return <li className="album-node" data-album-id={album.id}>
    <div className="album-row">
      <button className="expand" type="button" aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} ${album.album || album.name || "album"}`} onClick={() => setExpanded(value => !value)}>{expanded ? "⌄" : "›"}</button>
      <div className="row-identity"><strong>{album.album || album.name || album.relative_path || "Untitled album"}</strong><small>{album.albumartist || album.artist || album.path || "Unknown artist"}</small></div>
      <span className="row-count">{album.track_count ?? album.file_count ?? files.length} tracks</span>
      <span className={`status ${state.key}`} title={state.key === "error" ? album.execution_error || album.candidate_error : undefined}>{state.label}</span>
      <span className="match">{state.score == null ? "—" : `${Math.round(state.score)}%`}</span>
      <div className="overflow" ref={menuRef}><button type="button" aria-haspopup="menu" aria-expanded={menu} aria-label={`Actions for ${album.album || album.name || "album"}`} onClick={() => setMenu(value => !value)}>•••</button>{menu && <div role="menu"><button role="menuitem" type="button" disabled={scanning} title="Scan the containing folder" onClick={() => { setMenu(false); void onScan(); }}>Scan</button><button role="menuitem" type="button" disabled={!canImport} onClick={() => onReview(album)}>Import</button><button role="menuitem" type="button" onClick={() => onReview(album)}>Review</button></div>}</div>
    </div>
    {expanded && <ul className="file-list">{files.length ? files.map((file, index) => <FileRow key={file.id || file.path || index} file={file}/>) : <li className="empty-inline">No file detail is available until this album is scanned.</li>}</ul>}
  </li>;
}

function ScopeRow({ scope, source, threshold, inboxId, selected, scanning, refreshVersion, onSelect, onScan, onReview }: { scope: QueueScope; source: Source; threshold: number; inboxId?: string; selected: boolean; scanning: boolean; refreshVersion: number; onSelect: (value: boolean) => void; onScan: (scope: string) => Promise<void>; onReview: (album: Album) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [albums, setAlbums] = useState<Album[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
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
      setAlbums(result.albums || result.reviews || []);
    } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); }
  }, [inboxId, loading, scopeValue, source]);
  useEffect(() => { if (expanded) void loadAlbums(); }, [refreshVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  const expand = () => {
    const next = !expanded; setExpanded(next);
    if (next && !albums.length) void loadAlbums();
  };
  return <li className="folder-node">
    <div className="folder-row"><input type="checkbox" checked={selected} onChange={event => onSelect(event.target.checked)} aria-label={`Select folder ${name}`}/><button className="expand" type="button" onClick={expand} aria-expanded={expanded} aria-label={`${expanded ? "Collapse" : "Expand"} folder ${name}`}>{expanded ? "⌄" : "›"}</button><div className="row-identity"><strong>{name}</strong><small>{scope.path && scope.path !== name ? scope.path : "Folder"}</small></div><span className="row-count">{scope.album_count ?? scope.albums ?? 0} albums · {scope.file_count ?? scope.files ?? 0} files</span></div>
    {expanded && <div className="scope-children">{loading ? <p className="loading">Loading albums…</p> : error ? <Notice kind="error">{error}</Notice> : albums.length ? <ul className="album-list">{albums.map(album => <AlbumRow key={album.id} album={album} threshold={threshold} scanning={scanning} onScan={() => onScan(scopeValue)} onReview={onReview}/>)}</ul> : <p className="empty-inline">No albums in this folder.</p>}</div>}
  </li>;
}

export function Queue({ source, threshold, inboxId }: QueueProps) {
  const [scopes, setScopes] = useState<QueueScope[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [review, setReview] = useState<Album | null>(null);
  const [success, setSuccess] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scanning, setScanning] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const url = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes` : "/api/library-import/folders";
      const result = await api<any>(url);
      setScopes(result.scopes || result.folders || []);
    } catch (reason) { setError((reason as Error).message); } finally { setLoading(false); }
  }, [source, inboxId]);
  useEffect(() => { void load(); }, [load]);
  const scanScopes = async (values: string[]) => {
    if (!values.length) return;
    setScanning(true); setError("");
    try {
      const url = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes/scan` : "/api/library-import/folders/scan";
      await api(url, { method: "POST", body: JSON.stringify(source === "inbox" ? { scopes: values } : { folders: values }) });
      setSuccess(`${values.length} selected folder${values.length === 1 ? "" : "s"} scanned.`);
      await load();
      setRefreshVersion(version => version + 1);
    } catch (reason) { setError((reason as Error).message); } finally { setScanning(false); }
  };
  const scan = () => scanScopes([...selected]);
  const refresh = async () => {
    await load();
    setRefreshVersion(version => version + 1);
  };
  const bulkBody = () => source === "inbox" ? { scopes: [...selected] } : { folders: [...selected] };
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
      await load();
    } catch (reason) { setError((reason as Error).message); } finally { setScanning(false); }
  };
  const reviewExceptions = async () => {
    if (!selected.size) return;
    setScanning(true); setError("");
    try {
      const result = await api<any>(`${bulkBase}/exceptions`, { method: "POST", body: JSON.stringify(bulkBody()) });
      const albums = result.albums || result.exceptions || [];
      if (albums.length) setReview(albums[0]); else setSuccess("No selected exceptions need review.");
    } catch (reason) { setError((reason as Error).message); } finally { setScanning(false); }
  };
  return <section className="queue">
    {success && <Notice kind="success">{success}</Notice>}
    <div className="queue-toolbar"><div><strong>{scopes.length} folders</strong><small>{selected.size ? `${selected.size} selected` : "Folder → album → file"}</small></div><div className="toolbar-actions"><button type="button" onClick={scan} disabled={!selected.size || scanning}>Match Selected</button><button type="button" onClick={importReady} disabled={!selected.size || scanning}>Import</button><button className="secondary" type="button" onClick={reviewExceptions} disabled={!selected.size || scanning}>Review</button><button className="secondary" type="button" onClick={refresh} disabled={loading}>Refresh</button></div></div>
    {loading && !scopes.length ? <p className="loading">Loading queue…</p> : error ? <Notice kind="error">{error}</Notice> : scopes.length ? <ul className="folder-list">{scopes.map((scope, index) => { const value = String(scope.path || scope.folder || scope.name || ""); return <ScopeRow key={scope.id || value || index} scope={scope} source={source} threshold={threshold} inboxId={inboxId} selected={selected.has(value)} scanning={scanning} refreshVersion={refreshVersion} onSelect={checked => setSelected(current => { const next = new Set(current); if (checked) next.add(value); else next.delete(value); return next; })} onScan={async value => { await scanScopes([value]); }} onReview={setReview}/>; })}</ul> : <div className="empty-state"><strong>Queue is clear</strong><p>No folders are waiting for review.</p></div>}
    {review && <ReviewModal album={review} source={source} onClose={() => setReview(null)} onComplete={message => { setReview(null); setSuccess(message); void load(); }}/>} 
  </section>;
}
