import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Shell } from "./Shell";

describe("Shell", () => {
  it("renders all five required destinations and identifies the current page", () => {
    render(<Shell page="library-import" buildSha="123456789"><p>Content</p></Shell>);
    expect(screen.getByRole("link", { name: /Overview/ })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: /Inbox/ })).toHaveAttribute("href", "/inbox");
    expect(screen.getByRole("link", { name: /Library Import/ })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Library" })).toHaveAttribute("href", "/library");
    expect(screen.getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings");
    expect(screen.getByText("build 1234567")).toBeInTheDocument();
  });
});

