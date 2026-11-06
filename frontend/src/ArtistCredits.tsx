import { useState } from "react";

export function ArtistCredits({ name, label, names, display, form, onEdited }: {
  name: "artists" | "albumartists"; label: string; names?: string[]; display: string; form?: string; onEdited?: () => void;
}) {
  const [rows, setRows] = useState(() => (names?.length ? names : [display]).map((value, id) => ({ id, value })));
  const [edited, setEdited] = useState(false);
  const update = (next: typeof rows) => { setRows(next); setEdited(true); onEdited?.(); };
  return <fieldset className="artist-credits"><legend>{label}</legend>
    <small>One entry per artist. Remove a name here to remove its associated credits.</small>
    {edited && <input form={form} type="hidden" name={`${name}_edited`} value="1"/>}
    {rows.map((row, index) => <div className="artist-credit-row" key={row.id}>
      <input form={form} name={name} aria-label={`${label} ${index + 1}`} value={row.value} required={edited}
        onChange={event => update(rows.map(current => current.id === row.id ? { ...current, value: event.target.value } : current))}/>
      <button type="button" className="text-button" disabled={rows.length === 1} aria-label={`Remove ${row.value || "artist credit"}`}
        onClick={() => update(rows.filter(current => current.id !== row.id))}>Remove</button>
    </div>)}
    <button type="button" className="text-button" onClick={() => update([...rows, { id: Math.max(-1, ...rows.map(row => row.id)) + 1, value: "" }])}>Add artist</button>
  </fieldset>;
}
