import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useToast } from "./toastContext";
import { ToastProvider } from "./Toasts";

function Harness() {
  const notify = useToast();
  return <button onClick={() => notify("Imported album")}>Notify</button>;
}
afterEach(() => { cleanup(); vi.useRealTimers(); });
it("shows polite dismissible notifications, auto-dismisses, and pauses for interaction", () => {
  vi.useFakeTimers();
  render(<StrictMode><ToastProvider><Harness/></ToastProvider></StrictMode>);
  fireEvent.click(screen.getByText("Notify"));
  const region = screen.getByRole("region", { name: "Notifications" });
  expect(within(region).getByRole("status")).toHaveTextContent("Imported album");
  act(() => vi.advanceTimersByTime(1000));
  fireEvent.mouseEnter(screen.getByRole("status"));
  act(() => vi.advanceTimersByTime(10000));
  expect(screen.getByRole("status")).toBeInTheDocument();
  fireEvent.mouseLeave(screen.getByRole("status"));
  fireEvent.focus(screen.getByLabelText("Dismiss notification"));
  act(() => vi.advanceTimersByTime(10000));
  expect(screen.getByRole("status")).toBeInTheDocument();
  fireEvent.blur(screen.getByLabelText("Dismiss notification"));
  act(() => vi.advanceTimersByTime(15000));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  fireEvent.click(screen.getByText("Notify"));
  fireEvent.click(screen.getByLabelText("Dismiss notification"));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
it("keeps at most three repeated notifications with independent dismissal", () => {
  render(<StrictMode><ToastProvider><Harness/></ToastProvider></StrictMode>);
  for (let index = 0; index < 4; index++) fireEvent.click(screen.getByText("Notify"));
  expect(screen.getAllByRole("status")).toHaveLength(3);
  fireEvent.click(screen.getAllByLabelText("Dismiss notification")[1]);
  expect(screen.getAllByRole("status")).toHaveLength(2);
});

it.each([undefined, 3])("uses the configured auto-dismiss interval (%s)", seconds => {
  vi.useFakeTimers();
  render(<ToastProvider durationSeconds={seconds}><Harness/></ToastProvider>);
  fireEvent.click(screen.getByText("Notify"));
  const duration = (seconds ?? 5) * 1000;
  act(() => vi.advanceTimersByTime(duration - 1));
  expect(screen.getByRole("status")).toBeInTheDocument();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
