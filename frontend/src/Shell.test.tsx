import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Shell } from "./Shell";

describe("Shell", () => {
  it("renders the primary destinations and groups adoption under Library and identifies the current page", () => {
    render(<Shell page="library-import" buildSha="123456789"><p>Content</p></Shell>);
    expect(screen.getByRole("link", { name: /Overview/ })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: /Inbox/ })).toHaveAttribute("href", "/inbox");
    expect(screen.queryByRole("link", { name: /Library Import/ })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("href", "/library");
    expect(screen.getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("navigation", { name: "Primary navigation" }).querySelector("a")).toHaveTextContent("Overview");
    expect(screen.getByText("build 1234567")).toBeInTheDocument();
  });
});
