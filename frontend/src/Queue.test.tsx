import { describe, expect, it } from "vitest";
import { albumStatus } from "./status";

describe("albumStatus", () => {
  it("uses the persisted threshold without conflating candidate absence and failure", () => {
    expect(albumStatus({ id: 1 }, 90)).toMatchObject({ key: "pending", score: null });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .92 }] }, 90)).toMatchObject({ key: "matched", score: 92 });
    expect(albumStatus({ id: 1, candidates: [{ id: 2, confidence: .89 }] }, 90)).toMatchObject({ key: "threshold", score: 89 });
    expect(albumStatus({ id: 1, candidate_status: "error", candidate_error: "provider unavailable" }, 90)).toMatchObject({ key: "error", score: null });
  });
});

