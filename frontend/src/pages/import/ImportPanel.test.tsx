import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { mockApi, ok } from "../../test/api";
import { accountOut, importResult, previewAccount, previewResponse } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import { ImportPanel } from "./ImportPanel";

// The full paste → preview → map → import flows run against the page in
// ImportQif.test.tsx; this covers what only the wizard's use of the panel does.
describe("ImportPanel", () => {
  it("hands the result's Done back to its caller", async () => {
    mockApi({
      "GET /accounts": ok([accountOut()]),
      "POST /import/qif/preview": ok(
        previewResponse([
          previewAccount({ mapped: { id: "a-1", path: "assets.checking", type: "checking" } }),
        ]),
      ),
      "POST /import/qif": ok(importResult({ imported: 3 })),
    });
    const onDone = vi.fn();
    const { user } = renderWithProviders(<ImportPanel onDone={onDone} />);

    await user.click(screen.getByPlaceholderText(/!Type:Bank/));
    await user.paste("!Type:Bank\nD01/02/2026\nT-12.34\n^\n");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    // Already mapped server-side, so the row arrives resolved.
    const row = (await screen.findByText("Everyday Checking")).closest("tr")!;
    expect(within(row).getAllByRole("combobox")[0]).toHaveValue("a-1");
    await user.click(screen.getByRole("button", { name: "Import" }));
    await user.click(await screen.findByRole("button", { name: "Done" }));

    expect(onDone).toHaveBeenCalledOnce();
  });
});
