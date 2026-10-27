import { useInboxDiscovery } from "./useInboxDiscovery";
import { useInspectorResize } from "./useInspectorResize";
import { ArtworkPlaceholder } from "./ArtworkPlaceholder";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useToast } from "./toastContext";
import { Notice } from "./Shell";
import { ReviewModal } from "./ReviewModal";
import type { Album } from "./types";
import { InboxActionDialog, type InboxActionSelection } from "./InboxImportActions";
import { MatchingDialog } from "./MatchingDialog";
import { BulkImportDialog, type ImportRow } from "./BulkImportDialog";
import { albumStatus, formatMatchPercent } from "./status";

type Source = "inbox" | "library-import";
type QueueProps = { source: Source; threshold: number; inboxId?: string; sourceName?: string; onBusyChange?: (busy: boolean) => void };
type QueuePage = { albums: Album[]; total: number; page: number; page_count: number };

function Artwork({ album }: { album: Album }) {
  const [failed, setFailed] = useState(false);
  const url = album.current_metadata?.artwork_url || album.proposed_match?.artwork_url || album.candidates?.[0]?.artwork_url;
  return <span className="desk-artwork">{url && !failed ? <img src={url} alt="" onError={() => setFailed(true)}/> : <ArtworkPlaceholder/>}</span>;
}
function scopeFor(album: Album) {
  const path = album.tracks?.[0]?.path || album.files?.[0]?.path || album.source_paths?.[0] || ".";
  return String(path).includes("/") ? String(path).split("/")[0] : ".";
}
export function Queue({ source, threshold, inboxId, onBusyChange }: QueueProps) {
  const { workspace: workspaceRef, divider, style: splitStyle } = useInspectorResize();
  const notify = useToast();
  const [result, setResult] = useState<QueuePage>({ albums: [], total: 0, page: 1, page_count: 1 });
  const [page, setPage] = useState(1), [query, setQuery] = useState(""), [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true), [reviewBusy, setReviewBusy] = useState(false);
  const [scanning, setScanning] = useState(source === "inbox");
  const inventoryScan = useRef<Promise<unknown> | null>(null);
  const [operation, setOperation] = useState<"refresh" | "match" | "import" | null>(null);
  const busy = operation !== null;
  const [matching, setMatching] = useState<Album[] | null>(null);
  const [bulk, setBulk] = useState<{ body: Record<string, unknown>; rows: ImportRow[] } | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Map<number, Album>>(new Map());
  const [active, setActive] = useState<Album | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [inboxAction, setInboxAction] = useState<InboxActionSelection | null>(null);
  const requestCounter = useRef({ value: 0 }).current;
  const selectPage = useRef<HTMLInputElement>(null);
  const queueBase = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/queue` : "/api/library-import/queue";
  const bulkBase = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/bulk` : "/api/library-import/bulk";
  const locked = busy || scanning || reviewBusy || bulk !== null || matching !== null || inboxAction !== null;
  useEffect(() => { onBusyChange?.(locked); }, [locked, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  const backgroundBlocked = useRef(true);
  useEffect(() => { backgroundBlocked.current = locked || detailOpen || selected.size > 0 || loading; }, [locked, detailOpen, selected.size, loading]);
  const load = useCallback(async (quiet = false) => {
    const version = ++requestCounter.value;
    if (!quiet) { setLoading(true); setError(""); }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      if (source === "inbox" && !quiet) {
        if (!inventoryScan.current) {
          setScanning(true);
          inventoryScan.current = api(`${queueBase}/refresh`, { method: "POST", body: "{}", signal: controller.signal })
            .catch(reason => { inventoryScan.current = null; throw reason; });
        }
        await inventoryScan.current;
        if (version !== requestCounter.value) return;
        setScanning(false);
      }
      const next = await api<QueuePage>(`${queueBase}?${new URLSearchParams({ page: String(page), q: query, status })}`, { signal: controller.signal });
      if (version !== requestCounter.value) return;
      if (controller.signal.aborted) throw new Error("Album loading timed out.");
      if (!Array.isArray(next.albums)) throw new Error("The album response was invalid. Reload the page or try Refresh.");
      if (quiet && backgroundBlocked.current) throw new Error("Inbox update deferred during review");
      setResult(next);
      setSelected(current => new Map(next.albums.filter(album => current.has(album.id)).map(album => [album.id, album])));
      setActive(current => current ? next.albums.find(album => album.id === current.id) || next.albums[0] || null : next.albums[0] || null);
    } catch (reason) {
      if (quiet) throw reason; // Keep the revision pending so polling can retry.
      if (version === requestCounter.value) setError(controller.signal.aborted ? "Album loading timed out. Reload the page or try Refresh." : (reason as Error).message);
    } finally { clearTimeout(timeout); if (!quiet && version === requestCounter.value) { setLoading(false); setScanning(false); } }
  }, [queueBase, page, query, status, requestCounter, source]);
  const loadBackground = useCallback(() => load(true), [load]);
  const discovery = useInboxDiscovery(source === "inbox" ? inboxId : undefined,
    locked || detailOpen || selected.size > 0 || loading, loadBackground);
  useEffect(() => { const timer = setTimeout(() => void load(), query ? 200 : 0); return () => { clearTimeout(timer); ++requestCounter.value; }; }, [load, query, requestCounter]);
  useEffect(() => { setSelected(new Map()); }, [page, query, status, source, inboxId]);
  const visibleSelected = result.albums.filter(album => selected.has(album.id)).length;
  useEffect(() => { if (selectPage.current) selectPage.current.indeterminate = visibleSelected > 0 && visibleSelected < result.albums.length; }, [visibleSelected, result.albums.length]);
  const selectVisible = (checked: boolean) => setSelected(current => { const next = new Map(current); result.albums.forEach(album => checked ? next.set(album.id, album) : next.delete(album.id)); return next; });
  const importableSelected = [...selected.values()].filter(album => !album.imported);
  const deletableSelected = importableSelected.filter(album => !album.execution_status || album.execution_status === "not-run");
  const importedSelected = [...selected.values()].filter(album => album.imported);
  const bulkBody = () => ({ [source === "inbox" ? "scopes" : "folders"]: [...new Set(importableSelected.map(scopeFor))], album_ids: importableSelected.map(album => album.id) });
  const refresh = async () => {
    setOperation("refresh"); setError("");
    try {
      const url = source === "inbox" ? `/api/inbox/${encodeURIComponent(inboxId || "")}/scopes/refresh` : `${queueBase}/refresh`;
      const refreshed = await api<any>(url, { method: "POST", body: "{}" });
      if (source === "inbox") inventoryScan.current = Promise.resolve(refreshed);
      setSelected(new Map()); setActive(null); setDetailOpen(false); await load();
      if (refreshed.errors?.length || refreshed.cleanup?.errors?.length) setError(refreshed.errors?.[0]?.error || refreshed.cleanup.errors[0].error);
      else notify(source === "inbox" ? `Inbox refreshed: ${refreshed.inventory.files} tracks found; ${refreshed.cleanup.removed.length} empty folders removed.` : "Library files refreshed. Ready to review.");
    } catch (reason) { setError(`Refresh failed: ${(reason as Error).message}`); }
    finally { setOperation(null); }
  };
  const match = () => { setError(""); setMatching(importableSelected); };
  const importReady = async () => {
    setOperation("import"); setError("");
    try {
      const body = { ...bulkBody(), mode: "selected" };
      const preview = await api<any>(`${bulkBase}/preview`, { method: "POST", body: JSON.stringify(body) });
      const eligible = preview.importable_albums || preview.ready_albums || [];
      const rows: ImportRow[] = [
        ...eligible.map((row: any) => ({ id: row.id, album: row.album, status: "queued" as const, warnings: row.warnings })),
        ...(preview.exceptions || []).map((row: any) => ({ id: row.id, album: row.album, status: "blocked" as const, reason: row.reason })),
      ];
      if (!rows.length) { setError("The selected albums are no longer available. Refresh the queue."); return; }
      setBulk({ body, rows });
    } catch (reason) { setError((reason as Error).message); }
    finally { setOperation(null); }
  };
  return <section ref={workspaceRef} style={splitStyle} className={`desk-workspace${detailOpen ? " is-detail-open" : ""}`} aria-label={source === "inbox" ? "Inbox albums" : "Library adoption albums"}>{inboxAction && <InboxActionDialog inboxId={inboxId!} {...inboxAction} onClose={() => setInboxAction(null)} onDone={() => { setSelected(new Map()); setActive(null); setDetailOpen(false); void load(); }}/>}
    {bulk && <BulkImportDialog source={source} rows={bulk.rows} onRemoveImported={jobs => { setBulk(null); setInboxAction({ kind: "delete-imported", jobs }); }} onClose={() => setBulk(null)} onRunning={running => setOperation(running ? "import" : null)} execute={id => api(`${bulkBase}/execute`, { method: "POST", body: JSON.stringify({ ...bulk.body, album_ids: [id], confirmed: true }) })} onFinish={rows => {
      const imported = new Set(rows.filter(row => row.status === "imported").map(row => row.id));
      notify(`${imported.size} albums imported.`);
      setSelected(current => new Map([...current].filter(([id]) => !imported.has(id))));
      setActive(null); setDetailOpen(false); void load();
    }}/>}
    {matching && <MatchingDialog albums={matching} onClose={() => setMatching(null)} onRunning={running => setOperation(running ? "match" : null)} execute={(id, reuseCurrent) => api(source === "inbox" ? "/api/inbox/candidates" : "/api/library-import/candidates", { method: "POST", body: JSON.stringify({ album_id: id, ...(source === "library-import" ? { limit: 1 } : {}), reuse_current: reuseCurrent }) })} onFinish={rows => { notify(rows.some(row => ["needs-retry", "not-started"].includes(row.status)) ? "Matching paused. Successful results have been kept." : "Matching finished. Review the album results.", "info"); setDetailOpen(false); void load(); }}/>}
    <div className="desk-queue">
      <div className="desk-filters"><div className="desk-tabs" aria-label="Album filter">{[["all", "All albums"], ["pending", "Pending"], ["matched", "Matched"], ...(source === "library-import" ? [["perfect", "Perfect match"], ["duplicates", "Duplicates"]] : [["imported", "Imported"]])].map(([key, label]) => <button key={key} className="text-button" aria-pressed={status === key} disabled={locked} onClick={() => { setStatus(key); setPage(1); }}>{label}</button>)}</div><label className="desk-search"><span className="sr-only">Find an album</span><input type="search" placeholder="Find an album" value={query} disabled={locked} onChange={event => { setQuery(event.target.value); setPage(1); }}/></label></div>
      <div className="desk-selection"><label><input ref={selectPage} type="checkbox" disabled={locked || loading} checked={result.albums.length > 0 && visibleSelected === result.albums.length} onChange={event => selectVisible(event.target.checked)}/> Select this page</label><span>{selected.size ? `${selected.size} selected` : `${result.total} albums`}</span><button className="text-button" disabled={locked || loading} onClick={refresh}>Refresh</button></div>
      {selected.size > 0 && <div className="desk-batch toolbar-actions" aria-label="Selected album actions"><button disabled={locked || !importableSelected.length} onClick={match} aria-busy={operation === "match"}>{operation === "match" && <span className="spinner"/>}{operation === "match" ? "Matching…" : "Match Selected"}</button><button disabled={locked || !importableSelected.length} onClick={importReady} aria-busy={operation === "import"}>{operation === "import" && <span className="spinner"/>}{operation === "import" ? "Importing…" : "Import selected"}</button>{source === "inbox" && deletableSelected.length > 0 && <button className="danger-action" disabled={locked} onClick={() => setInboxAction({ kind: "delete-albums", albumIds: deletableSelected.map(album => album.id), names: deletableSelected.map(album => String(album.album)) })}>Delete from Inbox</button>}{source === "inbox" && importedSelected.length > 0 && <button className="danger-action" disabled={locked} onClick={() => setInboxAction({ kind: "delete-imported", jobs: importedSelected.map(album => album.imported.job_id) })}>Remove imported</button>}<button className="text-button" disabled={locked} onClick={() => setSelected(new Map())}>Clear selection</button></div>}
      {discovery.status && <div className="inbox-discovery-status" role="status">
        <span>{discovery.status.mode === "manual" ? "Manual discovery" : discovery.status.mode === "instant" ? "Instant discovery" : `Scan every ${discovery.status.mode} minutes`}</span>
        <span>{discovery.status.scanned_at ? `Last scan: ${new Date(discovery.status.scanned_at).toLocaleString()}` : "Waiting for first scan"}</span>
      </div>}
      {discovery.status?.watcher_error && <Notice kind="error">{discovery.status.watcher_error}</Notice>}
      {discovery.status?.error && <Notice kind="error">{discovery.status.error}</Notice>}
      {error && <Notice kind="error">{error}</Notice>}
      <div className="desk-column-head" aria-hidden="true"><span>Album / Artist</span><span>Tracks</span><span>Match</span><span>Status</span></div>
      <ul className="desk-albums" aria-busy={loading || busy}>{result.albums.map(album => { const state = albumStatus(album, threshold); const count = album.track_count ?? album.tracks?.length ?? album.files?.length ?? 0; return <li className={`desk-album${active?.id === album.id ? " is-active" : ""}`} data-album-id={album.id} key={album.id}>
        <input type="checkbox" disabled={locked || loading} aria-label={`Select album ${album.album}`} checked={selected.has(album.id)} onChange={event => setSelected(current => { const next = new Map(current); if (event.target.checked) next.set(album.id, album); else next.delete(album.id); return next; })}/>
        <button type="button" className="desk-album-open" aria-label={`Review ${album.album}`} aria-pressed={active?.id === album.id} disabled={locked || loading} onClick={() => { setActive(album); setDetailOpen(true); }}><Artwork album={album}/><span><strong>{album.album || "Untitled album"}</strong><small>{album.artist || "Unknown artist"}<span className="queue-mobile-meta"> · {count} tracks</span></small><span className="desk-source-path">{album.source_paths?.join(" · ")}</span></span></button>
        <span className="desk-track-count">{count}</span><span className="desk-match">{state.score == null ? "—" : formatMatchPercent(state.score)}</span><span className={`desk-decision ${state.key}`} title={album.execution_error || album.candidate_error || album.readiness_reason}>{state.label}</span>
      </li>; })}</ul>
      {loading && <p className="loading" role="status">{scanning ? "Scanning Inbox…" : "Loading albums…"}</p>}
      {!loading && !error && !result.albums.length && <div className="empty-state"><strong>{query || status !== "all" ? "No albums match this view" : "Nothing waiting here"}</strong><p>{query || status !== "all" ? "Try another search or All albums." : "No audio files found. Check the Inbox path in Settings and your folder mount, then Refresh."}</p></div>}
      <div className="desk-pagination"><span>Page {result.page} of {result.page_count} · 25 per page</span><button className="text-button" disabled={result.page <= 1 || loading || locked} onClick={() => setPage(result.page - 1)}>Previous</button><button className="text-button" disabled={result.page >= result.page_count || loading || locked} onClick={() => setPage(result.page + 1)}>Next</button></div>
    </div>
    {divider}    <aside className="desk-inspector" aria-label="Album inspector"><button type="button" className="inspector-back text-button" disabled={locked} onClick={() => setDetailOpen(false)}>Back to albums</button>{active ? active.imported ? <div className="embedded-review"><div className="dialog"><header><h2>{active.album}</h2><button type="button" className="text-button" disabled={locked} aria-label="Close imported album" onClick={() => { setActive(null); setDetailOpen(false); }}>×</button></header><div className="dialog-body"><Artwork album={active}/><h3>Imported</h3><p>{active.artist}</p><p>Library copies are managed. The original files remain in Inbox.</p><p>{active.imported.originals?.length || 0} retained originals</p>{active.imported.undo_reason && <p>{active.imported.undo_reason}</p>}</div><footer><button disabled={locked || !active.imported.undoable} onClick={() => setInboxAction({ kind: "undo", jobs: [active.imported.job_id] })}>Undo import</button><button className="danger-action" disabled={locked} onClick={() => setInboxAction({ kind: "delete-imported", jobs: [active.imported.job_id] })}>Remove imported</button></footer></div></div> : <ReviewModal embedded key={`${source}-${active.id}-${active.candidate_status}-${(active.candidates || []).map(row => row.id).join(",")}-${active.execution_status}`} album={active} source={source} onDeleteAlbum={source === "inbox" && (!active.execution_status || active.execution_status === "not-run") ? () => setInboxAction({ kind: "delete-albums", albumIds: [active.id], names: [String(active.album)] }) : undefined} onDeleteTrack={source === "inbox" && (!active.execution_status || active.execution_status === "not-run") ? (trackId, title) => setInboxAction({ kind: "delete-track", albumId: active.id, trackId, names: [String(active.album), title] }) : undefined} onBusyChange={setReviewBusy} onSelectionChange={updated => { setActive(updated); setResult(current => ({ ...current, albums: current.albums.map(album => album.id === updated.id ? updated : album) })); setSelected(current => current.has(updated.id) ? new Map(current).set(updated.id, updated) : current); }} onClose={() => { setActive(null); setDetailOpen(false); }} onComplete={message => { notify(message); setSelected(current => { const next = new Map(current); next.delete(active.id); return next; }); void load(); }}/>
      : <div className="inspector-empty"><h2>Select an album</h2><p>Choose its metadata here, then import it on its own or with your selection.</p></div>}</aside>
  </section>;
}
