import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FileLocation } from "./FileLocation";
import { ArtistCredits } from "./ArtistCredits";
import { ActivityProgress } from "./ActivityProgress";
import { api, activityRunning, type ActivityJob } from "./api";
import { Notice } from "./Shell";

const albumFields = ["album", "albumartist", "genre", "year"];
const trackFields = ["title", "artist", "disc", "track"];
const labels: Record<string, string> = { album: "Album", albumartist: "Album artist", genre: "Genre", year: "Year", title: "Title", artist: "Artist", disc: "Disc", track: "Track" };
const display = (value: unknown): string => Array.isArray(value) ? value.join(" · ") : String(value ?? "") || "—";

function changedFields(form: HTMLFormElement, original: any, fields: string[], credits: string) {
  const data = new FormData(form), result: Record<string, unknown> = {};
  for (const key of fields) {
    const numeric = ["year", "track", "disc"].includes(key);
    const value = numeric ? Number(data.get(key) || 0) : String(data.get(key) || "");
    if (value !== (original[key] || (numeric ? 0 : ""))) result[key] = value;
  }
  if (data.has(`${credits}_edited`)) result[credits] = data.getAll(credits).map(String);
  return result;
}

export function MetadataEditor({ album, onClose, onSaved, onBusyChange }: {
  album: any; onClose: () => void; onSaved: (album: any) => void; onBusyChange: (busy: boolean) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), albumForm = useRef<HTMLFormElement>(null);
  const trackForms = useRef(new Map<number, HTMLFormElement>());
  const [tab, setTab] = useState("album"), [selected, setSelected] = useState(album.tracks?.[0]?.id);
  const [dirty, setDirty] = useState(false), [discard, setDiscard] = useState(false);
  const [activity, setActivity] = useState<ActivityJob | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [review, setReview] = useState<any>(null), [draft, setDraft] = useState<any>(null);
  const [location, setLocation] = useState<{ mode: "beets" } | { mode: "manual"; directory: string }>();
  const [locationOpened, setLocationOpened] = useState(false);
  const tracks = album.tracks || [];
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const node = dialog.current;
    if (node?.showModal) node.showModal(); else node?.setAttribute("open", "");
    return () => { node?.close?.(); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false); }, [busy, onBusyChange]);
  const close = () => { if (!busy) { if (dirty) setDiscard(true); else onClose(); } };
  const markEdited = () => { setDirty(true); setReview(null); };
  const preview = async () => {
    const forms = [albumForm.current!, ...tracks.map((track: any) => trackForms.current.get(track.id)!)];
    for (const [index, form] of forms.entries()) {
      if (!form.checkValidity()) {
        setTab(index === 0 ? "album" : "tracks");
        if (index) setSelected(tracks[index - 1].id);
        setError("Check the highlighted fields before reviewing.");
        return;
      }
    }
    const payload = {
      expected_fingerprint: album.fingerprint,
      ...(location ? { location } : {}),
      album: changedFields(albumForm.current!, album, albumFields, "albumartists"),
      tracks: tracks.map((track: any) => ({ id: track.id, ...changedFields(trackForms.current.get(track.id)!, track, trackFields, "artists") })).filter((row: any) => Object.keys(row).length > 1),
    };
    setBusy(true); setError("");
    try { setReview(await api<any>(`/api/library/albums/${album.id}/metadata/preview`, { method: "POST", body: JSON.stringify(payload) }, setActivity)); setDraft(payload); }
    catch (reason) { setError((reason as Error).message); } finally { setBusy(false); }
  };
  const save = async () => {
    setBusy(true); setError("");
    try { const result = await api<any>(`/api/library/albums/${album.id}/metadata/apply`, { method: "POST", body: JSON.stringify({ ...draft, plan_fingerprint: review.plan_fingerprint }) }, setActivity); onSaved(result.album); }
    catch (reason) { setError((reason as Error).message); setReview(null); } finally { setBusy(false); }
  };
  return createPortal(<dialog className="metadata-editor" ref={dialog} aria-labelledby="metadata-title" onCancel={event => { event.preventDefault(); close(); }}>
    <header><div><h2 id="metadata-title">Edit metadata</h2><p>{album.album} · {album.albumartist || "Unknown artist"}</p></div><button type="button" className="text-button" aria-label="Close metadata editor" disabled={busy} onClick={close}>×</button></header>
    <div className="metadata-editor-body">
      {busy && activity && activityRunning(activity) && <ActivityProgress job={activity}/>}
      {error && <Notice kind="error">{error}</Notice>}
      <section hidden={!discard} aria-label="Discard changes"><h3>Discard unsaved changes?</h3><p>Your files have not been changed.</p><div className="metadata-inline-actions"><button type="button" className="secondary" onClick={() => setDiscard(false)}>Keep editing</button><button type="button" className="danger-action" onClick={onClose}>Discard changes</button></div></section><div hidden={discard}>
      <div hidden={!!review}>
        <nav className="metadata-tabs" aria-label="Metadata sections">{[["album", "Album details"], ["tracks", `Tracks (${tracks.length})`], ["location", "File location"], ["details", "Stored details"]].map(([key, label]) => <button type="button" key={key} aria-pressed={tab === key} disabled={busy} onClick={() => { setTab(key); if (key === "location") setLocationOpened(true); }}>{label}</button>)}</nav>
        <fieldset disabled={busy} className="metadata-fields" onChange={event => { if (event.target instanceof HTMLInputElement) markEdited(); }}>
          <form ref={albumForm} hidden={tab !== "album"} onSubmit={event => { event.preventDefault(); void preview(); }}>
            <div className="edit-grid">{albumFields.map(key => <label key={key}>{labels[key]}<input name={key} type={key === "year" ? "number" : "text"} step={key === "year" ? "1" : undefined} defaultValue={album[key] || ""}/></label>)}</div>
            <ArtistCredits name="albumartists" label="Album artist credits" names={album.albumartists} display={album.albumartist || ""} onEdited={markEdited}/>
            <p className="muted">Album details apply to every track. Track artists and their credits stay separate.</p>
          </form>
          <section hidden={tab !== "tracks"} aria-label="Track metadata">
            {tracks.length ? <><label className="metadata-track-picker">Track<select aria-label="Choose track" value={selected} onChange={event => setSelected(Number(event.target.value))}>{tracks.map((track: any) => <option value={track.id} key={track.id}>{track.disc || 1}.{track.track || "—"} · {track.title}</option>)}</select></label>
            {tracks.map((track: any) => <form key={track.id} ref={node => { if (node) trackForms.current.set(track.id, node); else trackForms.current.delete(track.id); }} hidden={selected !== track.id} onSubmit={event => { event.preventDefault(); void preview(); }}>
              <div className="edit-grid">{trackFields.map(key => <label key={key}>{labels[key]}<input name={key} type={["track", "disc"].includes(key) ? "number" : "text"} min={["track", "disc"].includes(key) ? "0" : undefined} step={["track", "disc"].includes(key) ? "1" : undefined} defaultValue={track[key] || ""}/></label>)}</div>
              <ArtistCredits name="artists" label="Track artist credits" names={track.artists} display={track.artist || ""} onEdited={markEdited}/>
              <p className="metadata-file-path">{track.relative_path || "File path unavailable"}</p>
            </form>)}</> : <p>No tracks in this album.</p>}
          </section>
        </fieldset>
        <div hidden={tab !== "location"}>{locationOpened && <FileLocation album={album} choice={location} disabled={busy} onChoose={value => { setLocation(value); markEdited(); }}/>}</div>
        <section hidden={tab !== "details"} aria-label="Stored metadata details"><p className="muted">Read-only catalog IDs and current file paths.</p><dl><dt>MusicBrainz release ID</dt><dd>{album.mb_albumid || "—"}</dd></dl>{tracks.map((track: any) => <div className="metadata-stored-track" key={track.id}><strong>{track.title}</strong><p>{track.relative_path || "File path unavailable"}</p><small>Recording ID: {track.mb_trackid || "—"}</small></div>)}</section>
      </div>
      {review && <section aria-label="Review metadata changes"><h3>Review changes</h3><p>Save applies the reviewed tag and file-location changes together.</p>
        {!review.changes.length && !review.paths.length && !review.related_files.length && !review.location_change && <p>No changes to save.</p>}
        {review.changes.map((row: any) => <div className="metadata-change-group" key={row.item_id}><h4>{row.title}</h4><dl>{Object.entries(row.changes).map(([key, change]: [string, any]) => <div key={key}><dt>{labels[key] || key.replaceAll("_", " ")}</dt><dd><span>{display(change.from)}</span><span aria-hidden="true"> → </span><strong>{display(change.to)}</strong></dd></div>)}</dl></div>)}
        {review.location_change && <p><strong>Album location rule:</strong> {review.location_change.from} → {review.location_change.to}</p>}
        <h4>File locations</h4>{!review.paths.length && <p className="muted">Audio file locations stay the same.</p>}{review.paths.map((row: any) => <div className="metadata-path-change" key={row.item_id}><span>{row.from}</span><strong>→ {row.to}</strong></div>)}
        {!!review.related_files.length && <details><summary>{review.related_files.length} accompanying file changes</summary>{review.related_files.map((row: any, index: number) => <div className="metadata-path-change" key={index}><small>{row.kind}</small><span>{row.from}</span><strong>→ {row.to}</strong></div>)}</details>}
      </section>}
      </div>
    </div>
    {!discard && <footer><button type="button" className="secondary" disabled={busy} onClick={review ? () => { setReview(null); setError(""); } : close}>{review ? "Back to editing" : "Cancel changes"}</button><button type="button" disabled={busy || (!!review && !review.changes.length && !review.paths.length && !review.related_files.length && !review.location_change)} onClick={() => void (review ? save() : preview())}>{busy ? "Working…" : review ? "Save changes" : "Review changes"}</button></footer>}
  </dialog>, document.body);
}
