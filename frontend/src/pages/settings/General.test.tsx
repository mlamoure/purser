import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { configOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import General from "./General";

const CONFIG = configOut();

describe("Settings, General: household time zone (#471)", () => {
  it("shows the effective zone and saves a new one", async () => {
    mockApi({ "GET /config": ok(CONFIG), "PUT /config": ok(CONFIG) });
    const { user } = renderWithProviders(<General />);
    const input = await screen.findByLabelText("Household time zone");
    expect(input).toHaveValue("UTC");
    await user.clear(input);
    await user.type(input, "America/New_York");
    const card = screen.getByRole("heading", { name: "Time zone" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(apiMock.PUT).toHaveBeenCalledWith("/config", {
        body: { timezone: "America/New_York" },
      }),
    );
  });

  it("shows the server's refusal of an unknown zone", async () => {
    mockApi({
      "GET /config": ok(CONFIG),
      "PUT /config": fail(422, "timezone: must be an IANA time zone name"),
    });
    const { user } = renderWithProviders(<General />);
    const input = await screen.findByLabelText("Household time zone");
    await user.clear(input);
    await user.type(input, "Mars/Olympus_Mons");
    const card = screen.getByRole("heading", { name: "Time zone" }).closest("div")!;
    await user.click(within(card).getByRole("button", { name: "Save" }));
    expect(await within(card).findByText(/IANA time zone/)).toBeInTheDocument();
  });
});


describe("Settings, General: labelled fields (#538)", () => {
  it("associates every visible field with its label", async () => {
    mockApi({ "GET /config": ok(CONFIG) });
    renderWithProviders(<General />);

    expect(await screen.findByLabelText("Household time zone")).toHaveValue("UTC");
    expect(screen.getByLabelText("Auto-link strong pairs")).toHaveValue("on");
    expect(screen.getByLabelText("Grafana URL")).toHaveValue("");
    // The security and agent topics moved to their own sections (#542).
    expect(screen.queryByLabelText("Issuer URL")).not.toBeInTheDocument();
  });
});
