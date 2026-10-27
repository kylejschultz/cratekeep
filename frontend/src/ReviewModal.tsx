import { ArtworkPlaceholder } from "./ArtworkPlaceholder";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useToast } from "./toastContext";
import { api, ApiError } from "./api";
import { formatMatchPercent } from "./status";
import type { Album, Candidate, DuplicatePreflight } from "./types";

type Props = { onDeleteAlbum?: () => void; onDeleteTrack?: (trackId: number, title: string) => void; album: Album; source: "inbox" | "library-import"; onClose: () => void; onComplete: (message: string) => void; embedded?: boolean; onBusyChange?: (busy: boolean) => void; onSelectionChange?: (album: Album) => void };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function candidates(album: Album): Candidate[] {
  const values = album.candidates || [];
  return album.proposed_match && !values.some(row => row.id === album.proposed_match?.id) ? [album.proposed_match, ...values] : values;
}

function initialChoice(album: Album): Album["selected_candidate_id"] {
  if (album.selected_candidate_id != null) return album.selected_candidate_id;
  if (album.selection_mode === "as-is" || (album.decision ?? album.state) === "approved") return null;
  return [...candidates(album)].sort((a, b) => (b.confidence ?? -1) - (a.confidence ?? -1))[0]?.id;
}

function duplicatePreflight(album: Album, candidateId: Album["selected_candidate_id"]) {
  const candidate = candidates(album).find(row => String(row.id) === String(candidateId));
  return candidate?.duplicate_preflight || album.duplicate_preflight || { status: "clear" as const };
}

function albumPayload(response: Album | { album?: Album }) {
  return response && typeof response.album === "object" ? response.album : response as Album;
}

function fileName(value: unknown) {
  return String(value || "").split(/[\\/]/).pop() || "Unknown file";
}

function text(value: unknown, fallback = "Not available") {
  if (value == null || value === "") return fallback;
  if (Array.isArray(value)) return value.map(item => typeof item === "object" && item ? (item as Record<string, unknown>).format || JSON.stringify(item) : String(item)).join(", ") || fallback;
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function mediaText(candidate: Candidate) {
  const values = (candidate.media || []).map(item => typeof item === "string" ? item : item?.format).filter(Boolean);
  return values.length ? values.join(", ") : "Not available";
}

type FieldKind = "artist" | "album" | "genre" | "year" | "medium" | "region" | "local" | "online" | "track";

const fieldIcons: Record<FieldKind, string> = {
  track: "M9 18V5l12-2v13 M9 8l12-2 M9 18a3 3 0 1 1-3-3c2 0 3 1 3 3 M21 16a3 3 0 1 1-3-3c2 0 3 1 3 3",
  artist: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M20 8v6 M17 11h6",
  album: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20 M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6",
  genre: "M3 3h8l10 10-8 8L3 11Z M7 7h.01",
  year: "M4 5h16v16H4Z M8 2v6 M16 2v6 M4 11h16",
  medium: "M3 5h18v14H3Z M7 9h10 M7 15h.01 M17 15h.01",
  region: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20 M2 12h20 M12 2c6 6 6 14 0 20 M12 2c-6 6-6 14 0 20",
  local: "M3 3h18v12H3Z M8 21h8 M12 15v6 M8 9h8 M13 6l3 3-3 3",
  online: "M4 17a5 5 0 0 1 0-10 7 7 0 0 1 13-1 5 5 0 0 1 3 9 M12 12v9 M8 17l4 4 4-4",
};

function FieldIcon({ kind, label, changed = false }: { kind: FieldKind; label: string; changed?: boolean }) {
  const status = `${label} ${changed ? "changed" : "unchanged"}`;
  return <span className={`field-icon ${changed ? "changed" : "unchanged"}`} role="img" aria-label={status} title={status}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={fieldIcons[kind]}/></svg></span>;
}

function Fact({ kind, label, value, changed }: { kind: FieldKind; label: string; value: unknown; changed?: boolean }) {
  return <div className={`candidate-fact ${changed ? "changed" : "unchanged"}`}><FieldIcon kind={kind} label={label} changed={changed}/><span><small>{label}</small><strong>{text(value)}</strong></span></div>;
}

function TrackEvidence({ album, candidate, comparisonRef }: { album: Album; candidate: Candidate; comparisonRef: React.RefObject<HTMLDetailsElement | null> }) {
  const details = candidate.track_details || [];
  if (!details.length) return null;
  return <details className="track-comparison" ref={comparisonRef}>
    <summary>Compare tracks ({details.length})</summary>
    <table className="track-evidence" aria-label="Current and proposed track metadata">
      <thead><tr><th scope="col">#</th><th scope="col">Current title</th><th scope="col">Proposed title</th></tr></thead>
      <tbody>{details.map((detail, index) => {
        const localTrack = detail.local == null ? {} : (album.tracks || album.files || [])[Number(detail.position ?? index + 1) - 1] || {};
        const localTitle = String(detail.local == null ? "Not on disk" : detail.local);
        const proposedTitle = String(detail.proposed ?? "Not in MusicBrainz");
        const localArtist = String(detail.local_artist ?? localTrack.artist ?? album.artist ?? album.albumartist ?? "Unknown artist");
        const proposedArtist = String(detail.proposed_artist ?? candidate.artist ?? "Unknown artist");
        const titleChanged = localTitle !== proposedTitle;
        const artistChanged = localArtist !== proposedArtist;
        const changed = detail.status !== "matched" || titleChanged || artistChanged;
        const path = localTrack.path ?? localTrack.relative_path ?? localTrack.filename ?? localTrack.name;
        return <tr className="track-evidence-row" key={index}>
          <th scope="row"><span>{(Array.isArray(detail.proposed_position) ? detail.proposed_position.join(".") : String(detail.proposed_position ?? index + 1))}</span><FieldIcon kind="track" label="Metadata" changed={changed}/></th>
          <td><strong>{localTitle}</strong><span>{localArtist}</span>{path && <small>{fileName(path)}</small>}{detail.status === "unmatched" && <small>Match needs review</small>}</td>
          <td><strong className={titleChanged ? "track-changed" : undefined}>{proposedTitle}</strong><span className={artistChanged ? "track-changed" : undefined}>{proposedArtist}</span></td>
        </tr>;
      })}</tbody>
    </table>
  </details>;
}

function CandidateCard({ album, candidate, chosen, expanded, busy, choose, onExpand }: { album: Album; candidate: Candidate; chosen: Album["selected_candidate_id"]; expanded: boolean; busy: boolean; choose: (id: Candidate["id"]) => void; onExpand: () => void }) {
  const [artFailed, setArtFailed] = useState(false);
  const comparisonRef = useRef<HTMLDetailsElement>(null);
  const diff = candidate.proposed_diff || {};
  const albumDiff = Object.entries(diff).filter(([key]) => key !== "tracks");
  const changeCount = albumDiff.length + (diff.tracks ? 1 : 0);
  const changedTitles = candidate.track_details?.filter(row => row.local !== row.proposed).length;
  const localCount = album.track_count ?? album.file_count ?? album.tracks?.length ?? album.files?.length;
  const onlineCount = candidate.recordings?.length ?? candidate.track_details?.filter(item => item.proposed != null).length;
  const comparisonCount = localCount != null && onlineCount != null ? Math.max(localCount, onlineCount) : undefined;
  const totalsDiffer = localCount != null && onlineCount != null && localCount !== onlineCount;
  const matchesDiffer = comparisonCount != null && candidate.matched_track_count != null && candidate.matched_track_count < comparisonCount;
  const trackTotals = `${localCount ?? "—"} / ${onlineCount ?? "—"}`;
  const trackMatches = `${candidate.matched_track_count ?? "—"} / ${comparisonCount ?? "—"}`;
  const releaseFormat = [candidate.release_type, mediaText(candidate)].filter(value => value && value !== "Not available").join(" · ");
  const fields: { kind: FieldKind; label: string; changed: boolean }[] = [
    { kind: "artist", label: "Artist", changed: Boolean(diff.artist) },
    { kind: "album", label: "Album", changed: Boolean(diff.album) },
    { kind: "genre", label: "Genre", changed: Boolean(diff.genre) },
    { kind: "year", label: "Release year", changed: Boolean(diff.year || diff.date) },
    { kind: "medium", label: "Release type / medium", changed: Boolean(diff.release_type || diff.media) },
    { kind: "region", label: "Release region", changed: Boolean(diff.country) },
    { kind: "local", label: "On disk / MusicBrainz", changed: totalsDiffer },
    { kind: "online", label: "Track matches", changed: matchesDiffer },
  ];
  const changed = fields.filter(field => field.changed).map(field => field.label);
  return <article className={`candidate-card${String(chosen) === String(candidate.id) ? " selected" : ""}`}>
    <div className="candidate-heading"><label><input type="radio" name="candidate" checked={String(chosen) === String(candidate.id)} disabled={busy} onChange={() => { choose(candidate.id); if (!expanded) onExpand(); }}/><span className="sr-only">Select {candidate.album || album.album || "candidate"}</span></label><span className="candidate-artwork">{candidate.artwork_url && !artFailed ? <img src={candidate.artwork_url} alt="" onError={() => setArtFailed(true)}/> : <ArtworkPlaceholder/>}</span><button className="candidate-summary" type="button" aria-expanded={expanded} onClick={onExpand}><span className="candidate-title"><strong>{candidate.artist || album.artist || "Unknown"} — {candidate.album || album.album || "Untitled"}</strong><small>{candidate.confidence == null ? "Candidate" : `${formatMatchPercent(candidate.confidence * 100)} match`}{candidate.year ? ` · ${candidate.year}` : ""}</small></span><span className="candidate-change-icons" aria-label={changed.length ? `Changes: ${changed.join(", ")}` : "No album field changes"}>{fields.map(field => <FieldIcon key={field.kind} {...field}/>)}</span><span aria-hidden="true">{expanded ? "⌃" : "⌄"}</span></button></div>
    {expanded && <div className="candidate-expanded">
      {changeCount > 0 && <section className="review-change-summary" aria-label="Proposed changes"><h3>{changeCount} proposed {changeCount === 1 ? "change" : "changes"}</h3><dl>{albumDiff.map(([key, value]) => <div key={key}><dt><FieldIcon kind={fields.find(field => field.kind === key)?.kind || (key === "country" ? "region" : key === "date" ? "year" : key === "media" || key === "release_type" ? "medium" : "album")} label={key.replaceAll("_", " ")} changed/>{key.replaceAll("_", " ")}</dt><dd><span>{text(value.from, "—")}</span><span aria-hidden="true">→</span><strong>{text(value.to, "—")}</strong></dd></div>)}{diff.tracks && <div className="track-change-link"><dt><FieldIcon kind="track" label="Tracks" changed/>Tracks</dt><dd><button className="text-button" type="button" onClick={() => { if (comparisonRef.current) { comparisonRef.current.open = true; comparisonRef.current.scrollIntoView?.({ block: "nearest" }); comparisonRef.current.querySelector("summary")?.focus(); } }}>{changedTitles ? `${changedTitles} track ${changedTitles === 1 ? "title" : "titles"} changed` : "Track metadata changed"} — Compare tracks →</button></dd></div>}</dl></section>}
      <div className="review-track-coverage"><div>On disk / MusicBrainz<strong>{trackTotals}</strong></div><div>Track matches<strong>{trackMatches}</strong></div></div>
      <details className="release-metadata"><summary>Release metadata and ID</summary>{candidate.provider_id && <p><code>{candidate.provider_id}</code></p>}<div className="candidate-facts">
      <Fact kind="artist" label="Artist" value={candidate.artist} changed={Boolean(diff.artist)}/>
      <Fact kind="album" label="Album" value={candidate.album} changed={Boolean(diff.album)}/>
      {(candidate.genre || diff.genre) && <Fact kind="genre" label="Genre" value={candidate.genre} changed={Boolean(diff.genre)}/>}
      {(candidate.year || diff.year || diff.date) && <Fact kind="year" label="Release year" value={candidate.year} changed={Boolean(diff.year || diff.date)}/>}
      {releaseFormat && <Fact kind="medium" label="Release type / medium" value={releaseFormat} changed={Boolean(diff.release_type || diff.media)}/>}
      {candidate.country && <Fact kind="region" label="Release region" value={candidate.country} changed={Boolean(diff.country)}/>}
      <Fact kind="local" label="On disk / MusicBrainz" value={trackTotals} changed={totalsDiffer}/>
      <Fact kind="online" label="Track matches" value={trackMatches} changed={matchesDiffer}/>
    </div>
    </details>
    {albumDiff.length > 0 && <details className="metadata-comparison"><summary>Compare metadata</summary><dl>{albumDiff.map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{text(value.from, "—")} → {text(value.to, "—")}</dd></div>)}</dl></details>}
    <TrackEvidence album={album} candidate={candidate} comparisonRef={comparisonRef}/></div>}
  </article>;
}

export function ReviewModal({ album: seed, source, onClose, onComplete, embedded = false, onBusyChange, onSelectionChange, onDeleteAlbum, onDeleteTrack }: Props) {
  const [album, setAlbum] = useState(seed);
  const notify = useToast();
  const [operation, setOperation] = useState<"match" | "search" | "import" | "selection" | null>(null);
  const busy = operation !== null;
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [mbidOpen, setMbidOpen] = useState(false);
  const [mbid, setMbid] = useState("");
  const [chosen, setChosen] = useState(() => initialChoice(seed));
  const [recovery, setRecovery] = useState(seed.execution_status === "cleanup-pending" ? "inbox_cleanup_failed" : seed.execution_status === "artwork-pending" ? "artwork_finalize_failed" : ["stale", "error"].includes(seed.candidate_status || "") && initialChoice(seed) !== null ? "candidate_stale" : "");
  const [cleanupPending, setCleanupPending] = useState(seed.execution_status === "cleanup-pending");
  const [moved, setMoved] = useState(["artwork-pending", "cleanup-pending", "complete"].includes(seed.execution_status || ""));
  const initialExpanded = initialChoice(seed) ?? "as-is";
  const [expanded, setExpanded] = useState<number | string | null>(initialExpanded);
  const dialog = useRef<HTMLDivElement>(null);
  const base = source === "inbox" ? "/api/inbox/albums" : "/api/library-import/albums";
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  useEffect(() => { if (embedded) dialog.current?.focus(); }, [embedded]);
  useEffect(() => { if (embedded) return; dialog.current?.focus(); const handler = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); }; addEventListener("keydown", handler); return () => removeEventListener("keydown", handler); }, [busy, onClose, embedded]);
  const choose = async (candidateId: number | string | null) => {
    setOperation("selection"); setError("");
    try {
      const duplicate = duplicatePreflight(album, candidateId);
      const selection = { decision: "approved", candidate_id: candidateId, ...(duplicate.status === "possible" && album.duplicate_action ? { duplicate_action: album.duplicate_action } : {}) };
      const response = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify(selection) });
      setAlbum(albumPayload(response)); onSelectionChange?.(albumPayload(response)); setChosen(candidateId); setRecovery(""); setStatus("");
    } catch (reason) { setError(String((reason as Error).message)); } finally { setOperation(null); }
  };
  const saveDuplicate = async (action: string) => {
    setOperation("selection");
    try {
      const updated = albumPayload(await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify({ duplicate_action: action || null }) }));
      setAlbum(updated); onSelectionChange?.(updated);
    } catch (reason) { setError((reason as Error).message); }
    finally { setOperation(null); }
  };
  const match = async () => {
    if (busy) return;
    setOperation("match"); setError(""); setStatus("Matching with MusicBrainz…");
    try {
      const selected = candidates(album).find(row => String(row.id) === String(chosen));
      const refreshSelected = ["candidate_stale", "candidate_unavailable"].includes(recovery) && UUID_PATTERN.test(selected?.provider_id || "");
      let refreshed: Album;
      let refreshError = "";
      if (refreshSelected) {
        const response = await api<Album>(`${base}/${album.id}/rematch`, { method: "POST", body: JSON.stringify({ musicbrainz_id: selected!.provider_id }) });
        refreshed = albumPayload(response);
      } else {
        const endpoint = source === "inbox" ? "/api/inbox/candidates" : "/api/library-import/candidates";
        const result = await api<any>(endpoint, { method: "POST", body: JSON.stringify({ album_id: album.id, ...(source === "library-import" ? { limit: 1 } : {}) }) });
        refreshError = result.errors?.[0]?.error || result.result?.errors?.[0]?.error || "";
        refreshed = await api<Album>(`${base}/${album.id}`);
      }
      const selection = refreshSelected ? candidates(refreshed).find(row => row.provider_id === selected?.provider_id)?.id : initialChoice(refreshed);
      setAlbum(refreshed); onSelectionChange?.(refreshed); setChosen(selection); setExpanded(selection ?? "as-is");
      if (refreshed.candidate_status === "stale" || refreshed.candidate_status === "error") {
        setRecovery("candidate_stale"); setError(refreshed.candidate_error || "Matching evidence is still unavailable. Match again or search an exact release ID."); setStatus("");
      } else {
        setRecovery(""); setStatus("");
        if (refreshError) setError(`Refresh failed: ${refreshError}. Your saved match is still available.`);
        else if (candidates(refreshed).length) notify("Match complete. Review the refreshed candidates.");
      }
    } catch (reason) {
      setError(reason instanceof ApiError && reason.status === 503
        ? `${reason.message}. MusicBrainz is temporarily unavailable; try again later. Your selection has been kept.`
        : `Match failed: ${(reason as Error).message}`);
      setStatus("");
    } finally { setOperation(null); }
  };
  const searchMbid = async (event: FormEvent) => {
    event.preventDefault();
    const value = mbid.trim();
    if (!UUID_PATTERN.test(value)) { setError("Enter a canonical MusicBrainz release UUID."); return; }
    setOperation("search"); setError(""); setStatus("Searching MusicBrainz by release ID…");
    try {
      const response = await api<any>(`${base}/${album.id}/rematch`, { method: "POST", body: JSON.stringify({ musicbrainz_id: value.toLowerCase() }) });
      const refreshed = albumPayload(response); setAlbum(refreshed); onSelectionChange?.(refreshed); setChosen(initialChoice(refreshed)); setRecovery(""); setExpanded(initialChoice(refreshed) ?? "as-is"); setStatus(""); notify("Exact MusicBrainz release loaded. Review it before importing."); setMbidOpen(false);
    } catch (reason) { setError((reason as Error).message); setStatus(""); } finally { setOperation(null); }
  };
  const execute = async (skipArtwork = false) => {
    const duplicate: DuplicatePreflight = moved ? { status: "clear" } : duplicatePreflight(album, chosen);
    if (duplicate.status === "ambiguous") { setError("Import is blocked because managed recording identity is ambiguous."); return; }
    if (duplicate.status === "possible" && (!album.duplicate_action || !(source === "inbox" ? ["keep-existing", "replace", "keep-both"] : ["keep-existing", "skip", "keep-both"]).includes(album.duplicate_action))) { setError("Choose a duplicate decision before importing this album."); return; }
    const selection = { candidate_id: chosen ?? null, ...(duplicate.status === "possible" ? { duplicate_action: album.duplicate_action } : {}) };
    const artworkOption = skipArtwork ? { skip_artwork: true } : {};
    setOperation("import"); setError(""); setStatus("Running preflight…");
    try {
      if (source === "inbox" && !moved) {
        const saved = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify({ decision: "approved", ...selection }) });
        setAlbum(albumPayload(saved));
      }
      await api(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify({ ...(source === "library-import" ? selection : {}), ...artworkOption, dry_run: true }) });
      setStatus(cleanupPending ? "Finishing Inbox cleanup…" : moved ? "Finalizing artwork…" : "Preflight passed. Importing…");
      const result = await api<any>(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify({ ...(source === "library-import" ? selection : {}), ...artworkOption }) });
      onComplete(result.message || `${album.album || album.name || "Album"} imported successfully.`);
    } catch (reason) {
      const code = reason instanceof ApiError ? reason.code || "" : "";
      setRecovery(code);
      if (code === "artwork_finalize_failed" || code === "inbox_cleanup_failed") setMoved(true);
      if (code === "inbox_cleanup_failed") setCleanupPending(true);
      const message = (reason as Error).message;
      setError(code === "candidate_stale" || code === "candidate_unavailable"
        ? `${message} Refresh matches, review the new evidence, then import again.`
        : `${message} Retry is available.`);
      setStatus(""); setOperation(null);
    }
  };
  const duplicate: DuplicatePreflight = moved ? { status: "clear" } : duplicatePreflight(album, chosen);
  return <div className={embedded ? "embedded-review" : "modal"} data-review-modal data-busy={busy} onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="dialog" role={embedded ? "region" : "dialog"} aria-modal={embedded ? undefined : "true"} aria-labelledby="review-title" tabIndex={-1} ref={dialog} data-review-dialog>
      <header><div><p className="eyebrow">Album review</p><h2 id="review-title" data-review-title>{album.album || album.name || album.path || "Album"}</h2></div><button type="button" onClick={onClose} disabled={busy} aria-label="Close review" data-review-close>×</button></header>
      <div className="dialog-body">
        <div className="review-tools"><button type="button" onClick={() => void match()} disabled={busy || moved} aria-busy={operation === "match"}>{operation === "match" && <span className="spinner"/>}{["candidate_stale", "candidate_unavailable"].includes(recovery) ? "Refresh matches" : "Match"}</button><button className="secondary" type="button" onClick={() => { setMbidOpen(value => !value); setError(""); }} disabled={busy || moved} aria-expanded={mbidOpen}>Search MBID</button></div>
        {mbidOpen && <form className="mbid-search" onSubmit={searchMbid}><label>MusicBrainz release UUID<input value={mbid} onChange={event => setMbid(event.target.value)} placeholder="123e4567-e89b-42d3-a456-426614174000" disabled={busy} autoFocus/></label><button type="submit" disabled={busy} aria-busy={operation === "search"}>{operation === "search" && <span className="spinner"/>}Search exact release</button></form>}
        {source === "inbox" && onDeleteTrack && <details className="inbox-local-tracks"><summary>Inbox files ({(album.tracks || []).length})</summary><ul className="track-list">{(album.tracks || []).map(track => <li key={track.id}><div><strong>{track.title || fileName(track.path)}</strong><small>{track.path}</small></div><button type="button" className="danger-action" disabled={busy || moved || !Number.isInteger(track.id)} aria-label={`Delete track ${track.title || fileName(track.path)}`} onClick={() => onDeleteTrack(track.id, String(track.title || fileName(track.path)))}>Delete track</button></li>)}</ul></details>}
        <fieldset className="candidate-list" data-review-candidates><legend>Metadata choice</legend>
          {candidates(album).map(candidate => <CandidateCard key={candidate.id} album={album} candidate={candidate} chosen={chosen} expanded={String(expanded) === String(candidate.id)} busy={busy || moved} choose={choose} onExpand={() => setExpanded(current => String(current) === String(candidate.id) ? null : candidate.id)}/>) }
          <article className={`candidate-card as-is${chosen === null ? " selected" : ""}`}><div className="candidate-heading"><label><input type="radio" name="candidate" checked={chosen === null} disabled={busy || moved} onChange={() => { choose(null); setExpanded("as-is"); }}/><span className="sr-only">Select As Is</span></label><span className="candidate-artwork"><ArtworkPlaceholder/></span><button className="candidate-summary" type="button" aria-expanded={expanded === "as-is"} onClick={() => setExpanded(current => current === "as-is" ? null : "as-is")}><span className="candidate-title"><strong>As Is</strong><small>Retain incoming/local metadata unchanged</small></span><span className="candidate-change-icons" aria-label="No MusicBrainz association">—</span><span aria-hidden="true">{expanded === "as-is" ? "⌃" : "⌄"}</span></button></div>{expanded === "as-is" && <p className="as-is-detail">Do not associate a MusicBrainz release.</p>}</article>
        </fieldset>
        {!busy && !error && album.candidate_status === "complete" && !candidates(album).length && <p className="notice" role="status" data-no-matches>No matches found. Try Search MBID or choose As Is to keep the current metadata.</p>}
        {duplicate.status !== "clear" && <div className={`notice${duplicate.status === "ambiguous" ? " error" : ""}`} data-duplicate-warning><strong>{duplicate.message || (duplicate.status === "ambiguous" ? "Managed recording identity is ambiguous." : "This match may duplicate an existing library item.")}</strong></div>}
        {duplicate.status === "possible" && <label className="select-label">Duplicate decision<select required value={source === "library-import" && album.duplicate_action === "skip" ? "keep-existing" : !(source === "inbox" ? ["keep-existing", "replace", "keep-both"] : ["keep-existing", "keep-both"]).includes(album.duplicate_action || "") ? "" : album.duplicate_action || ""} disabled={busy || moved} onChange={event => { setAlbum({ ...album, duplicate_action: event.target.value }); setError(""); void saveDuplicate(event.target.value); }}><option value="">Choose a decision</option>{source === "inbox" ? <><option value="keep-existing">Keep existing; retain Inbox originals</option><option value="replace">Replace with incoming files</option><option value="keep-both">Keep both</option></> : <><option value="keep-existing">Keep existing; leave duplicate files untouched</option><option value="keep-both">Keep both</option></>}</select></label>}
        {moved && <p className="notice">{cleanupPending ? "Library destinations are verified. Retry only finishes incoming file cleanup; it will not import or replace audio again." : "Audio has already been imported. Recovery only handles the pending artwork; it will not move or import the audio again."}</p>}
        {["artwork_fetch_failed", "artwork_finalize_failed"].includes(recovery) && <div className="notice"><p>{moved ? "You can retry the pending sidecar, or finish without it when no incoming artwork needs recovery. Already embedded artwork is retained." : "You can retry artwork retrieval or continue with the selected metadata while preserving existing artwork. This applies only to this import."}</p><button type="button" className="secondary" disabled={busy} onClick={() => void execute(true)}>{moved ? "Finish without new sidecar" : "Continue without new artwork"}</button></div>}
        {status && <p className="notice" aria-live="polite" data-review-status>{status}</p>}{error && <p className="notice error" role="alert" data-review-status>{error}</p>}
      </div>
      <footer><button type="button" className="secondary" onClick={onClose} disabled={busy} data-review-cancel>Cancel</button>{onDeleteAlbum && <button type="button" className="danger-action" disabled={busy || moved} onClick={onDeleteAlbum}>Delete from Inbox</button>}<button type="button" onClick={() => void execute()} disabled={busy || chosen === undefined || ["candidate_stale", "candidate_unavailable"].includes(recovery)} aria-busy={operation === "import"} data-review-execute>{operation === "import" && <span className="spinner"/>}{["candidate_stale", "candidate_unavailable"].includes(recovery) ? "Refresh matches before importing" : cleanupPending ? "Finish Inbox cleanup" : moved ? "Retry artwork finalization" : error ? "Retry import" : "Import to Library"}</button></footer>
    </div>
  </div>;
}
