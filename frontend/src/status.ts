import type { Album } from "./types";

export function albumStatus(album: Album, threshold: number) {
  const operationalError = album.execution_status === "error" || album.candidate_status === "error" || Boolean(album.execution_error || album.candidate_error);
  if (operationalError) return { key: "error", label: "Error", score: null as number | null };
  const pool = [...(album.candidates || []), ...(album.proposed_match ? [album.proposed_match] : [])];
  const values = pool.map(row => Number(row.confidence)).filter(Number.isFinite);
  if (!values.length) return { key: "pending", label: "Pending", score: null as number | null };
  const score = Math.max(...values) * 100;
  return { key: score >= threshold ? "matched" : "threshold", label: score >= threshold ? "Matched" : "Threshold", score };
}

