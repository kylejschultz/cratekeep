import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { useInspectorResize } from "./useInspectorResize";

function Harness() {
  const { workspace: workspaceRef, divider, style } = useInspectorResize();
  return <div ref={workspaceRef} style={style}>{divider}</div>;
}
afterEach(() => { cleanup(); localStorage.clear(); });
it("clamps keyboard resizing, persists between screens and resets", () => {
  const first = render(<Harness/>);
  const divider = screen.getByRole("separator", { name: "Resize album details" });
  fireEvent.keyDown(divider, { key: "ArrowLeft" });
  expect(divider).toHaveAttribute("aria-valuenow", "430");
  first.unmount();
  render(<Harness/>);
  const restored = screen.getByRole("separator");
  expect(restored).toHaveAttribute("aria-valuenow", "430");
  for (let i = 0; i < 20; i++) fireEvent.keyDown(restored, { key: "ArrowRight", shiftKey: true });
  expect(restored).toHaveAttribute("aria-valuenow", "320");
  fireEvent.keyDown(restored, { key: "Home" });
  expect(restored).toHaveAttribute("aria-valuenow", "420");
  fireEvent.keyDown(restored, { key: "ArrowLeft", shiftKey: true });
  fireEvent.doubleClick(restored);
  expect(restored).toHaveAttribute("aria-valuenow", "420");
});
it("ignores invalid saved widths", () => {
  localStorage.setItem("cratekeep-inspector-width", "invalid");
  render(<Harness/>);
  expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "420");
});
