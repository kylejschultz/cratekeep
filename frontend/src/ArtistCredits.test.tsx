import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { ArtistCredits } from "./ArtistCredits";

it("submits only explicit list edits and treats commas as part of a name", () => {
  render(<form aria-label="credits"><ArtistCredits name="albumartists" label="Album artist credits" names={["gnash", "Garrett Nash"]} display="gnash"/></form>);
  const form = screen.getByRole("form", { name: "credits" }) as HTMLFormElement;
  expect(new FormData(form).has("albumartists_edited")).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Remove Garrett Nash" }));
  expect(new FormData(form).has("albumartists_edited")).toBe(true);
  expect(new FormData(form).getAll("albumartists")).toEqual(["gnash"]);
  fireEvent.change(screen.getByLabelText("Album artist credits 1"), { target: { value: "Tyler, The Creator" } });
  expect(new FormData(form).getAll("albumartists")).toEqual(["Tyler, The Creator"]);
});

it("connects track list controls to the existing track form", () => {
  render(<><form id="track-credits" aria-label="track"/><ArtistCredits name="artists" label="Track artist credits" names={["First", "Guest"]} display="First" form="track-credits"/></>);
  fireEvent.click(screen.getByRole("button", { name: "Remove Guest" }));
  const form = screen.getByRole("form", { name: "track" }) as HTMLFormElement;
  expect(new FormData(form).getAll("artists")).toEqual(["First"]);
});
