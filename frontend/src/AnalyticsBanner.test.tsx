import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnalyticsBanner } from "./AnalyticsBanner";
import { api } from "./api";
vi.mock("./api", () => ({ api: vi.fn() }));
afterEach(cleanup);
const key = "cratekeep-analytics-dismissed";
beforeEach(() => { localStorage.clear(); vi.resetAllMocks(); });

describe("Analytics invitation", () => {
  it("keeps disclosure separate from choice and remembers browser dismissal", () => {
    const view = render(<AnalyticsBanner/>);
    expect(screen.getByRole("link", { name: "What’s shared" })).toHaveAttribute("href", "/settings#about");
    expect(localStorage.getItem(key)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "No thanks" }));
    expect(api).not.toHaveBeenCalled();
    expect(localStorage.getItem(key)).toBe("1");
    view.unmount();
    const second = render(<AnalyticsBanner/>);
    expect(screen.queryByText("Help improve Cratekeep")).not.toBeInTheDocument();
    second.unmount(); localStorage.clear(); render(<AnalyticsBanner/>);
    expect(screen.getByText("Help improve Cratekeep")).toBeInTheDocument();
  });
  it("waits for persisted opt-in and prevents conflicting clicks", async () => {
    let complete!: () => void;
    vi.mocked(api).mockReturnValue(new Promise(resolve => { complete = () => resolve({ enabled: true }); }));
    render(<AnalyticsBanner/>);
    fireEvent.click(screen.getByRole("button", { name: "Share analytics" }));
    expect(screen.getByRole("button", { name: "No thanks" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Enabling…" }));
    expect(api).toHaveBeenCalledTimes(1);
    expect(api).toHaveBeenCalledWith("/api/analytics/enable", { method: "POST", body: "{}" });
    expect(localStorage.getItem(key)).toBeNull();
    await act(async () => complete());
    expect(localStorage.getItem(key)).toBe("1");
    expect(screen.queryByText("Help improve Cratekeep")).not.toBeInTheDocument();
  });
  it("leaves failed consent visible and retryable", async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce({ enabled: true });
    render(<AnalyticsBanner/>);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Share analytics" })));
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t enable analytics");
    expect(localStorage.getItem(key)).toBeNull();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Share analytics" })));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("syncs dismissal between tabs and tolerates unavailable browser storage", () => {
    render(<AnalyticsBanner/>);
    localStorage.setItem(key, "1");
    act(() => window.dispatchEvent(new StorageEvent("storage", { key })));
    expect(screen.queryByText("Help improve Cratekeep")).not.toBeInTheDocument();
    localStorage.clear();
    act(() => window.dispatchEvent(new StorageEvent("storage", { key: null })));
    expect(screen.getByText("Help improve Cratekeep")).toBeInTheDocument();
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    fireEvent.click(screen.getByRole("button", { name: "No thanks" }));
    expect(screen.queryByText("Help improve Cratekeep")).not.toBeInTheDocument();
    storage.mockRestore();
  });
});
