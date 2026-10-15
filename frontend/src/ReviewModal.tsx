import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Album, Candidate } from "./types";

type Props = { album: Album; source: "inbox" | "library-import"; onClose: () => void; onComplete: (message: string) => void };

function candidates(album: Album): Candidate[] {
  const values = album.candidates || [];
  return album.proposed_match && !values.some(row => row.id === album.proposed_match?.id) ? [album.proposed_match, ...values] : values;
}

function fileName(value: unknown) {
  return String(value || "").split(/[\\/]/).pop() || "Unknown file";
}

function TrackEvidence({ album, candidate }: { album: Album; candidate: Candidate }) {
  const details = candidate.track_details || [];
  if (!details.length) return null;
  return <details className="track-comparison">
    <summary>Compare tracks ({details.length})</summary>
    <div className="track-evidence" role="table" aria-label="Current and proposed track metadata">
      {details.map((detail, index) => {
        const localTrack = (album.tracks || album.files || [])[index] || {};
        const localTitle = String(detail.local ?? localTrack.title ?? localTrack.name ?? "Unknown title");
        const proposedTitle = String(detail.proposed ?? "No proposed track");
        const localArtist = String(detail.local_artist ?? localTrack.artist ?? album.artist ?? album.albumartist ?? "Unknown artist");
        const proposedArtist = String(detail.proposed_artist ?? candidate.artist ?? "Unknown artist");
        const changed = detail.status !== "matched" || localTitle.localeCompare(proposedTitle, undefined, { sensitivity: "accent" }) !== 0 || localArtist.localeCompare(proposedArtist, undefined, { sensitivity: "accent" }) !== 0;
        const path = localTrack.path ?? localTrack.relative_path ?? localTrack.filename ?? localTrack.name;
        return <div className="track-evidence-row" role="row" key={String(detail.local_position ?? detail.proposed_position ?? index)}>
          <span className={`change-indicator ${changed ? "changed" : "unchanged"}`} aria-label={changed ? "Metadata changed" : "Metadata unchanged"} title={changed ? "Changed" : "Unchanged"}>{changed ? "↻" : "✓"}</span>
          <div role="cell"><small>Local file</small><strong>{fileName(path)}</strong><span>{localTitle} · {localArtist}</span></div>
          <span className="comparison-arrow" aria-hidden="true">→</span>
          <div role="cell"><small>Proposed metadata</small><strong>{proposedTitle}</strong><span>{proposedArtist}{detail.proposed_position ? ` · Track ${String(detail.proposed_position)}` : ""}</span></div>
        </div>;
      })}
    </div>
  </details>;
}

export function ReviewModal({ album: seed, source, onClose, onComplete }: Props) {
  const [album, setAlbum] = useState(seed);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const dialog = useRef<HTMLDivElement>(null);
  const base = source === "inbox" ? "/api/inbox/albums" : "/api/library-import/albums";
  useEffect(() => { dialog.current?.focus(); const handler = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); }; addEventListener("keydown", handler); return () => removeEventListener("keydown", handler); }, [busy, onClose]);
  const choose = async (candidateId: number | string | null) => {
    setBusy(true); setError("");
    try {
      const response = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify({ decision: "approved", candidate_id: candidateId, duplicate_action: album.duplicate_action || undefined }) });
      setAlbum(response.album || response); setStatus(candidateId == null ? "Incoming metadata selected As Is." : "Candidate selected. Review the comparison, then import explicitly.");
    } catch (reason) { setError(String((reason as Error).message)); } finally { setBusy(false); }
  };
  const execute = async () => {
    setBusy(true); setError(""); setStatus("Running preflight…");
    try {
      const saved = await api<any>(`${base}/${album.id}`, { method: "PATCH", body: JSON.stringify({ decision: "approved", candidate_id: chosen, duplicate_action: album.duplicate_action || undefined }) });
      setAlbum(saved.album || saved);
      await api(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify({ dry_run: true }) });
      setStatus("Preflight passed. Importing…");
      const result = await api<any>(`${base}/${album.id}/execute`, { method: "POST", body: JSON.stringify({}) });
      onComplete(result.message || `${album.album || album.name || "Album"} imported successfully.`);
    } catch (reason) { setError(`${(reason as Error).message} Retry is available.`); setStatus(""); setBusy(false); }
  };
  const chosen = album.selected_candidate_id;
  return <div className="modal" data-review-modal data-busy={busy} onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="review-title" tabIndex={-1} ref={dialog} data-review-dialog>
      <header><div><p className="eyebrow">Album review</p><h2 id="review-title" data-review-title>{album.album || album.name || album.path || "Album"}</h2></div><button type="button" onClick={onClose} disabled={busy} aria-label="Close review" data-review-close>×</button></header>
      <div className="dialog-body">
        <section className="current"><h3>Current metadata</h3><dl><div><dt>Artist</dt><dd>{album.albumartist || album.artist || "Unknown"}</dd></div><div><dt>Album</dt><dd>{album.album || album.name || "Untitled"}</dd></div><div><dt>Tracks</dt><dd>{album.track_count ?? album.file_count ?? album.files?.length ?? "—"}</dd></div></dl></section>
        <fieldset className="candidate-list" data-review-candidates><legend>Metadata choice</legend>
          <label className="candidate"><input type="radio" name="candidate" checked={chosen == null && album.state === "approved"} disabled={busy} onChange={() => choose(null)}/><span><strong>As Is</strong><small>Keep incoming metadata unchanged.</small></span></label>
          {candidates(album).map(candidate => <label className="candidate" key={candidate.id}><input type="radio" name="candidate" checked={String(chosen) === String(candidate.id)} disabled={busy} onChange={() => choose(candidate.id)}/><span><strong>{candidate.artist || album.artist || "Unknown"} — {candidate.album || album.album || "Untitled"}</strong><small>{candidate.confidence == null ? "Candidate" : `${Math.round(candidate.confidence * 100)}% match`} {candidate.year ? `· ${candidate.year}` : ""}</small>{candidate.proposed_diff && <details><summary>Compare metadata</summary><dl>{Object.entries(candidate.proposed_diff).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value.from ?? "—")} → {String(value.to ?? "—")}</dd></div>)}</dl></details>}<TrackEvidence album={album} candidate={candidate}/></span></label>)}
        </fieldset>
        <label className="select-label">Duplicate decision<select value={album.duplicate_action || ""} onChange={event => setAlbum({ ...album, duplicate_action: event.target.value })}><option value="">Choose when required</option><option value="skip">Skip duplicate</option><option value="keep-both">Keep both</option><option value="merge">Merge</option><option value="replace">Replace</option></select></label>
        {status && <p className="notice" data-review-status>{status}</p>}{error && <p className="notice error" role="alert" data-review-status>{error}</p>}
      </div>
      <footer><button type="button" className="secondary" onClick={onClose} disabled={busy} data-review-cancel>Cancel</button><button type="button" onClick={execute} disabled={busy || (chosen == null && album.state !== "approved")} aria-busy={busy} data-review-execute>{busy && <span className="spinner"/>}{error ? "Retry import" : "Import to Library"}</button></footer>
    </div>
  </div>;
}
