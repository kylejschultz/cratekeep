import type { Album } from "./types";

export function albumStatus(album: Album, _threshold?: number) {
  void _threshold;
  if (album.imported) return { key: "imported", label: "Imported", score: null as number | null };
  if (album.selection_mode === "as-is") return { key: "matched", label: "Matched", score: null as number | null };
  if (album.candidate_status !== "complete" || album.candidate_error) return { key: "pending", label: "Pending", score: null as number | null };
  const pool = [...(album.candidates || []), ...(album.proposed_match ? [album.proposed_match] : [])];
  const candidate = album.selected_candidate_id != null ? pool.find(row => String(row.id) === String(album.selected_candidate_id)) : [...pool].sort((a, b) => (b.confidence || 0) - (a.confidence || 0))[0];
  return candidate ? { key: "matched", label: "Matched", score: (candidate.confidence || 0) * 100 } : { key: "pending", label: "Pending", score: null as number | null };
}

// Keep near-perfect matches distinguishable from exact matches.
export function formatMatchPercent(percent: number) {
  const rounded = Math.round(percent * 10) / 10;
  return `${percent < 100 ? Math.min(99.9, rounded) : 100}%`;
}
