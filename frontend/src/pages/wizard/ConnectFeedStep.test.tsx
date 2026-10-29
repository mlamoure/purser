import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { ConnectFeedStep } from "./ConnectFeedStep";

// `connect` is useFeedConnection's (its API calls are covered there); here it
// is a stand-in so the step's own wiring is what's under test.
describe("ConnectFeedStep", () => {
  it("connects with the trimmed setup token, then moves on", async () => {
    const connect = vi.fn(async () => {});
    const onConnected = vi.fn();
    const { user } = renderWithProviders(
      <ConnectFeedStep connect={connect} onConnected={onConnected} onSkip={vi.fn()} />,
    );

    await user.type(screen.getByRole("textbox"), "  synthetic-setup-token  ");
    await user.click(screen.getByRole("button", { name: "Connect & fetch" }));

    await waitFor(() => expect(onConnected).toHaveBeenCalledOnce());
    expect(connect).toHaveBeenCalledWith("synthetic-setup-token");
  });

  it("shows progress while connecting, then the reason it failed", async () => {
    let rejectConnect: (e: Error) => void = () => {};
    const connect = vi.fn(
      () => new Promise<void>((_, reject) => (rejectConnect = reject)),
    );
    const onConnected = vi.fn();
    const { user } = renderWithProviders(
      <ConnectFeedStep connect={connect} onConnected={onConnected} onSkip={vi.fn()} />,
    );

    await user.type(screen.getByRole("textbox"), "synthetic-setup-token");
    await user.click(screen.getByRole("button", { name: "Connect & fetch" }));
    expect(screen.getByRole("button", { name: "Connecting…" })).toBeDisabled();

    rejectConnect(new Error("setup token already claimed"));

    expect(await screen.findByText("setup token already claimed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect & fetch" })).toBeEnabled();
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("can be skipped without connecting", async () => {
    const connect = vi.fn(async () => {});
    const onSkip = vi.fn();
    const { user } = renderWithProviders(
      <ConnectFeedStep connect={connect} onConnected={vi.fn()} onSkip={onSkip} />,
    );

    await user.click(screen.getByRole("button", { name: "Skip for now" }));

    expect(onSkip).toHaveBeenCalledOnce();
    expect(connect).not.toHaveBeenCalled();
  });
});
