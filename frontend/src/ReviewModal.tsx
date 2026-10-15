import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import type { Album, Candidate } from "./types";

type Props = { album: Album; source: "inbox" | "library-import"; onClose: () => void; onComplete: (message: string) => void };

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function candidates(album: Album): Candidate[] {
  const values = album.candidates || [];
  return album.proposed_match && !values.some(row => row.id === album.proposed_match?.id) ? [album.proposed_match, ...values] : values;
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
  return value == null || value === "" ? fallback : String(value);
}

function mediaText(candidate: Candidate) {
  const values = (candidate.media || []).map(item => typeof item === "string" ? item : item?.format).filter(Boolean);
  return values.length ? values.join(", ") : "Not available";
}

function Fact({ label, value, changed }: { label: string; value: unknown; changed?: boolean }) {
  return <div className={`candidate-fact ${changed ? "changed" : "unchanged"}`}><span className="fact-indicator" aria-label={changed ? `${label} changed` : `${label} unchanged`}>{changed ? "↻" : "✓"}</span><span><small>{label}</small><strong>{text(value)}</strong></span></div>;
}

function TrackEvidence({ album, candidate }: { album: Album; candidate: Candidate }) {
  const details = candidate.track_details || [];
  if (!details.length) return null;
  return <details className="track-comparison">
    <summary>Compare tracks ({details.length})</summary>
    <div className="track-evidence" role="table" aria-label="Current and proposed track metadata">
      {details.map((detail, index) => {
        const localTrack = (album.tracks || album.files || [])[index] || {};
        const localTitle = String(detail.local ?? localTrack.title ?? localTrack.name ?? "Not present locally");
        const proposedTitle = String(detail.proposed ?? "Not found online");
        const localArtist = String(detail.local_artist ?? localTrack.artist ?? album.artist ?? album.albumartist ?? "Unknown artist");
        const proposedArtist = String(detail.proposed_artist ?? candidate.artist ?? "Unknown artist");
        const changed = detail.status !== "matched" || localTitle.localeCompare(proposedTitle, undefined, { sensitivity: "accent" }) !== 0 || localArtist.localeCompare(proposedArtist, undefined, { sensitivity: "accent" }) !== 0;
        const path = localTrack.path ?? localTrack.relative_path ?? localTrack.filename ?? localTrack.name;
        return <div className="track-evidence-row" role="row" key={String(detail.local_position ?? detail.proposed_position ?? index)}>
          <span className={`change-indicator ${changed ? "changed" : "unchanged"}`} aria-label={changed ? "Metadata changed" : "Metadata unchanged"} title={changed ? "Changed" : "Unchanged"}>{changed ? "↻" : "✓"}</span>
          <div role="cell"><small>Local file</small><strong>{fileName(path)}</strong><span>{localTitle} · {localArtist}</span></div>
          <span className="comparison-arrow" aria-hidden="true">→</span>
          <div role="cell"><small>MusicBrainz metadata</small><strong>{proposedTitle}</strong><span>{proposedArtist}{detail.proposed_position ? ` · Track ${String(detail.proposed_position)}` : ""}</span></div>
        </div>;
      })}
    </div>
  </details>;
}

function CandidateCard({ album, candidate, chosen, busy, choose }: { album: Album; candidate: Candidate; chosen: Album["selected_candidate_id"]; busy: boolean; choose: (id: Candidate["id"]) => void }) {
  const [artFailed, setArtFailed] = useState(false);
  const diff = candidate.proposed_diff || {};
  const localCount = album.track_count ?? album.file_count ?? album.tracks?.length ?? album.files?.length;
  const onlineCount = candidate.recordings?.length ?? candidate.track_details?.filter(item => item.proposed != null).length;
  const releaseFormat = [candidate.release_type, mediaText(candidate)].filter(value => value && value !== "Not available").join(" · ");
  return <article className={`candidate-card${String(chosen) === String(candidate.id) ? " selected" : ""}`}>
    <label className="candidate-heading"><input type="radio" name="candidate" checked={String(chosen) === String(candidate.id)} disabled={busy} onChange={() => choose(candidate.id)}/><span className="candidate-artwork">{candidate.artwork_url && !artFailed ? <img src={candidate.artwork_url} alt="" onError={() => setArtFailed(true)}/> : <span aria-hidden="true">♫</span>}</span><span className="candidate-title"><strong>{candidate.artist || album.artist || "Unknown"} — {candidate.album || album.album || "Untitled"}</strong><small>{candidate.confidence == null ? "Candidate" : `${Math.round(candidate.confidence * 100)}% match`}{candidate.year ? ` · ${candidate.year}` : ""}</small>{candidate.provider_id && <code title={candidate.provider_id}>{candidate.provider_id}</code>}</span></label>
    <div className="candidate-facts">
      <Fact label="Artist" value={candidate.artist} changed={Boolean(diff.artist)}/>
      <Fact label="Album" value={candidate.album} changed={Boolean(diff.album)}/>
      {(candidate.genre || diff.genre) && <Fact label="Genre" value={candidate.genre} changed={Boolean(diff.genre)}/>}
      {(candidate.year || diff.year || diff.date) && <Fact label="Release year" value={candidate.year} changed={Boolean(diff.year || diff.date)}/>}
      {releaseFormat && <Fact label="Release type / medium" value={releaseFormat} changed={Boolean(diff.release_type || diff.media)}/>}
      {candidate.country && <Fact label="Release region" value={candidate.country} changed={Boolean(diff.country)}/>}
      {localCount != null && <Fact label="Local tracks found online" value={`${candidate.matched_track_count ?? 0} of ${localCount}`} changed={Boolean(candidate.unmatched_track_count)}/>}
      {onlineCount != null && <Fact label="Online tracks present locally" value={`${candidate.matched_track_count ?? 0} of ${onlineCount}`} changed={Boolean(candidate.unmatched_track_count)}/>}
    </div>
    {Object.keys(diff).length > 0 && <details className="metadata-comparison"><summary>Compare metadata</summary><dl>{Object.entries(diff).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{text(value.from, "—")} → {text(value.to, "—")}</dd></div>)}</dl></details>}
    <TrackEvidence album={album} candidate={candidate}/>
  </article>;
}

export function ReviewModal({ album: seed, source, onClose, onComplete }: Props) {
  const [album, setAlbum] = useState(seed);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [mbidOpen, setMbidOpen] = useState(false);
  const [mbid, setMbid] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const base = source === "inbox" ? "/api/inbox/albums" : "/api/library-import/albums";
  useEffect(() => { dialog.current?.focus(); const handler = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); }; addEventListener("keydown", handler); return () => removeEventListener("keydown", handler); }, [busy, onClose]);
  const choose = async (candidateId: number | string | null) => {
    setBusy(true); setError("");
    try {
      const duplicate = duplicatePreflight(album, candidateId);
      const selection = { decision: "approved", candidate_id: candidateId, ...(duplicate.status === "possible" && album.duplicate_action ? { duplicate_action: album.duplicate_action } : {}) };
      const response = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify(selection) });
      setAlbum(albumPayload(response)); setStatus(candidateId == null ? "As Is selected. Incoming/local metadata will be retained." : "Candidate selected. Review the comparison, then import explicitly.");
    } catch (reason) { setError(String((reason as Error).message)); } finally { setBusy(false); }
  };
  const match = async () => {
    if (busy) return;
    setBusy(true); setError(""); setStatus("Matching with MusicBrainz…");
    try {
      const endpoint = source === "inbox" ? "/api/inbox/candidates" : "/api/library-import/candidates";
      await api(endpoint, { method: "POST", body: JSON.stringify({ album_id: album.id, ...(source === "library-import" ? { limit: 1 } : {}) }) });
      const refreshed = await api<Album>(`${base}/${album.id}`);
      setAlbum(refreshed); setStatus("Match complete. Review the refreshed candidates.");
    } catch (reason) { setError(`Match failed: ${(reason as Error).message}`); setStatus(""); } finally { setBusy(false); }
  };
  const searchMbid = async (event: FormEvent) => {
    event.preventDefault();
    const value = mbid.trim();
    if (!UUID_PATTERN.test(value)) { setError("Enter a canonical MusicBrainz release UUID."); return; }
    setBusy(true); setError(""); setStatus("Searching MusicBrainz by release ID…");
    try {
      const response = await api<any>(`${base}/${album.id}/rematch`, { method: "POST", body: JSON.stringify({ musicbrainz_id: value.toLowerCase() }) });
      setAlbum(albumPayload(response)); setStatus("Exact MusicBrainz release loaded. Review it before importing."); setMbidOpen(false);
    } catch (reason) { setError((reason as Error).message); setStatus(""); } finally { setBusy(false); }
  };
  const execute = async () => {
    const duplicate = duplicatePreflight(album, chosen);
    if (duplicate.status === "ambiguous") { setError("Import is blocked because managed recording identity is ambiguous."); return; }
    if (duplicate.status === "possible" && !album.duplicate_action) { setError("Choose a duplicate decision before importing this album."); return; }
    const selection = { candidate_id: chosen ?? null, ...(duplicate.status === "possible" ? { duplicate_action: album.duplicate_action } : {}) };
    setBusy(true); setError(""); setStatus("Running preflight…");
    try {
      if (source === "inbox") {
        const saved = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify({ decision: "approved", ...selection }) });
        setAlbum(albumPayload(saved));
      }
      await api(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify(source === "library-import" ? { ...selection, dry_run: true } : { dry_run: true }) });
      setStatus("Preflight passed. Importing…");
      const result = await api<any>(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify(source === "library-import" ? selection : {}) });
      onComplete(result.message || `${album.album || album.name || "Album"} imported successfully.`);
    } catch (reason) { setError(`${(reason as Error).message} Retry is available.`); setStatus(""); setBusy(false); }
  };
  const chosen = album.selected_candidate_id;
  const duplicate = duplicatePreflight(album, chosen);
  return <div className="modal" data-review-modal data-busy={busy} onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="review-title" tabIndex={-1} ref={dialog} data-review-dialog>
      <header><div><p className="eyebrow">Album review</p><h2 id="review-title" data-review-title>{album.album || album.name || album.path || "Album"}</h2></div><button type="button" onClick={onClose} disabled={busy} aria-label="Close review" data-review-close>×</button></header>
      <div className="dialog-body">
        <div className="review-tools"><button type="button" onClick={() => void match()} disabled={busy}>{busy && !mbidOpen && <span className="spinner"/>}Match</button><button className="secondary" type="button" onClick={() => { setMbidOpen(value => !value); setError(""); }} disabled={busy} aria-expanded={mbidOpen}>Search MBID</button></div>
        {mbidOpen && <form className="mbid-search" onSubmit={searchMbid}><label>MusicBrainz release UUID<input value={mbid} onChange={event => setMbid(event.target.value)} placeholder="123e4567-e89b-42d3-a456-426614174000" disabled={busy} autoFocus/></label><button type="submit" disabled={busy}>Search exact release</button></form>}
        <fieldset className="candidate-list" data-review-candidates><legend>Metadata choice</legend>
          <label className="candidate as-is"><input type="radio" name="candidate" checked={chosen == null && album.state === "approved"} disabled={busy} onChange={() => choose(null)}/><span><strong>As Is</strong><small>Retain incoming/local metadata unchanged; do not associate a MusicBrainz release.</small></span></label>
          {candidates(album).map(candidate => <CandidateCard key={candidate.id} album={album} candidate={candidate} chosen={chosen} busy={busy} choose={choose}/>) }
        </fieldset>
        {duplicate.status !== "clear" && <div className={`notice${duplicate.status === "ambiguous" ? " error" : ""}`} data-duplicate-warning><strong>{duplicate.message || (duplicate.status === "ambiguous" ? "Managed recording identity is ambiguous." : "This match may duplicate an existing library item.")}</strong></div>}
        {duplicate.status === "possible" && <label className="select-label">Duplicate decision<select required value={album.duplicate_action || ""} onChange={event => { setAlbum({ ...album, duplicate_action: event.target.value }); setError(""); }}><option value="">Choose a decision</option><option value="skip">Skip duplicate</option><option value="keep-both">Keep both</option><option value="merge">Merge</option><option value="replace">Replace</option></select></label>}
        {status && <p className="notice" aria-live="polite" data-review-status>{status}</p>}{error && <p className="notice error" role="alert" data-review-status>{error}</p>}
      </div>
      <footer><button type="button" className="secondary" onClick={onClose} disabled={busy} data-review-cancel>Cancel</button><button type="button" onClick={execute} disabled={busy || (chosen == null && album.state !== "approved")} aria-busy={busy} data-review-execute>{busy && <span className="spinner"/>}{error ? "Retry import" : "Import to Library"}</button></footer>
    </div>
  </div>;
}
