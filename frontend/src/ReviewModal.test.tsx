import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ReviewModal } from "./ReviewModal";

describe("ReviewModal track evidence", () => {
  it("shows local files, proposed metadata, and changed and unchanged indicators", () => {
    render(<ReviewModal source="library-import" onClose={() => undefined} onComplete={() => undefined} album={{
      id: 1, album: "Current Album", artist: "Artist", selected_candidate_id: 2,
      tracks: [
        { path: "Artist/Album/01 Same.wav", title: "Same", artist: "Artist" },
        { path: "Artist/Album/02 Local.wav", title: "Local", artist: "Artist" },
      ],
      candidates: [{ id: 2, artist: "Artist", album: "Proposed Album", confidence: .96, track_details: [
        { status: "matched", local: "Same", proposed: "Same", local_artist: "Artist", proposed_artist: "Artist", proposed_position: 1 },
        { status: "title-mismatch", local: "Local", proposed: "Canonical", local_artist: "Artist", proposed_artist: "Featured Artist", proposed_position: 2 },
      ] }],
    }}/>);

    fireEvent.click(screen.getByText("Compare tracks (2)"));
    expect(screen.getByText("01 Same.wav")).toBeInTheDocument();
    expect(screen.getByText("02 Local.wav")).toBeInTheDocument();
    expect(screen.getByText("Canonical")).toBeInTheDocument();
    expect(screen.getByLabelText("Metadata unchanged")).toBeInTheDocument();
    expect(screen.getByLabelText("Metadata changed")).toBeInTheDocument();
  });
});
