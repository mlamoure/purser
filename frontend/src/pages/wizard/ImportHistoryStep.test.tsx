import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, ok } from "../../test/api";
import { renderWithProviders } from "../../test/render";
import { ImportHistoryStep } from "./ImportHistoryStep";

describe("ImportHistoryStep", () => {
  it("mounts the shared import panel and can be skipped", async () => {
    mockApi({ "GET /accounts": ok([]) }); // the panel's account picker
    const onDone = vi.fn();
    const { user } = renderWithProviders(<ImportHistoryStep onDone={onDone} />);

    expect(screen.getByRole("heading", { name: "Import bank history (optional)" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/!Type:Bank/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled(); // nothing pasted yet

    await user.click(screen.getByRole("button", { name: "Skip for now" }));

    expect(onDone).toHaveBeenCalledOnce();
  });
});
