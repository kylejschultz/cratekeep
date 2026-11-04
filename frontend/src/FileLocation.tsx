import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Notice } from "./Shell";

type Choice = { mode: "beets" } | { mode: "manual"; directory: string };
type Folder = { name: string; path: string };
type LocationInfo = { directory: string; source_directory: string | null; manual_error?: string; beets_paths: { item_id: number; to: string }[] };

export function FileLocation({ album, choice, onChoose, disabled }: {
  album: any; choice?: Choice; onChoose: (choice: Choice) => void; disabled: boolean;
}) {
  const [info, setInfo] = useState<LocationInfo | null>(null), [error, setError] = useState("");
  const [browser, setBrowser] = useState<{ path: string; entries: Folder[] } | null>(null);
  const [loading, setLoading] = useState(false), [folderName, setFolderName] = useState("");
  const request = useRef(0);
  useEffect(() => {
    let active = true;
    void api<LocationInfo>(`/api/library/albums/${album.id}/location`).then(value => { if (active) setInfo(value); }).catch(reason => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, [album.id]);
  const browse = async (path: string) => {
    const serial = ++request.current;
    setLoading(true); setError("");
    try { const result = await api<{ path: string; entries: Folder[] }>(`/api/library/folders?path=${encodeURIComponent(path)}`); if (request.current === serial) { setBrowser(result); setFolderName(""); } }
    catch (reason) { if (request.current === serial) setError((reason as Error).message); }
    finally { if (request.current === serial) setLoading(false); }
  };
  const selectFolder = (path: string) => { onChoose({ mode: "manual", directory: path }); setError(""); };
  const newFolder = () => {
    const name = folderName.trim();
    if (!name || name === "." || name === ".." || /[/\\\0]/.test(name) || name.length > 255) { setError("Enter one folder name without slashes."); return; }
    selectFolder([browser?.path, name].filter(Boolean).join("/"));
  };
  const mode = choice?.mode || (info?.directory ? "manual" : "beets");
  const destination = choice?.mode === "manual" ? choice.directory : info?.directory;
  const crumbs = browser?.path.split("/").filter(Boolean) || [];
  return <section className="file-location" aria-label="Album file location">
    {error && <Notice kind="error">{error}</Notice>}
    <h3>Current location</h3>
    <p className="location-path">{info?.source_directory ? `Library / ${info.source_directory}` : info ? "Files span multiple folders" : "Loading folder details…"}</p>
    <details><summary>{album.tracks.length} files</summary><ul className="location-files">{album.tracks.map((track: any) => <li key={track.id}>{track.relative_path || "File path unavailable"}</li>)}</ul></details>
    <div className="location-policy"><strong>{mode === "manual" ? "Manual album folder" : "Beets naming"}</strong><p>{mode === "manual" ? `Library / ${destination}` : "Folder and filenames follow your configured Beets rules."}</p><small>{mode === "manual" ? "Remembered for this album until you choose Use Beets naming. Track filenames can still change when you edit tags." : "You can organize existing files without changing their tags."}</small></div>
    <button type="button" className="secondary" disabled={disabled || !info} onClick={() => onChoose({ mode: "beets" })}>Use Beets naming</button>
    {info && <details className="location-beets-preview"><summary>Beets destinations</summary><ul className="location-files">{info.beets_paths?.map(row => <li key={row.item_id}>{row.to}</li>)}</ul><small>Based on saved tags. Review changes recalculates destinations with your edits.</small></details>}
    {info?.manual_error && <Notice>{info.manual_error}</Notice>}
    <button type="button" className="secondary" disabled={disabled || loading || !info || !!info.manual_error} onClick={() => void browse("")}>{loading ? "Loading folders…" : "Browse folders"}</button>
    {browser && <div className="library-folder-browser" aria-label="Choose album folder" aria-busy={loading}>
      <nav aria-label="Library folder breadcrumbs"><button type="button" className="text-button" disabled={disabled || loading} onClick={() => void browse("")}>Library</button>{crumbs.map((part, index) => <span key={index}> / <button type="button" className="text-button" disabled={disabled || loading} onClick={() => void browse(crumbs.slice(0, index + 1).join("/"))}>{part}</button></span>)}</nav>
      <ul>{browser.entries.map(folder => <li key={folder.path}><button type="button" className="text-button" disabled={disabled || loading} onClick={() => void browse(folder.path)}>{folder.name}<span aria-hidden="true"> →</span></button></li>)}</ul>
      {!browser.entries.length && <p className="muted">No subfolders.</p>}
      <button type="button" className="secondary" disabled={disabled || loading || !browser.path} onClick={() => selectFolder(browser.path)}>Use this album folder</button>
      <label>New album folder<input aria-label="New album folder" value={folderName} disabled={disabled || loading} onChange={event => setFolderName(event.target.value)} placeholder="Folder name"/></label>
      <button type="button" className="text-button" disabled={disabled || loading || !folderName.trim()} onClick={newFolder}>Use new folder</button>
      <small>Choose the exact album folder. Existing filenames and disc subfolders are kept. Nothing moves or gets created until you review and save.</small>
    </div>}
  </section>;
}
