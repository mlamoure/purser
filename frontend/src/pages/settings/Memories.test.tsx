import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, mockApi, ok } from "../../test/api";
import { accountOut } from "../../test/fixtures";
import { renderWithProviders } from "../../test/render";
import Memories from "./Memories";

describe("Settings, Memories: plain labels and accounts by name (#541)", () => {
  it("lists linked accounts by name and sends the picked account's path", async () => {
    mockApi({
      "GET /memories": ok([
        {
          id: "m-1",
          text: "the monthly transfer is the college fund",
          state: "active",
          pinned: false,
          archived: false,
          links: ["assets.college", "Example Payee"],
          stale: false,
          rules: [],
        },
      ]),
      "GET /accounts": ok([
        accountOut({ id: "a-1", name: "Checking", path: "assets.checking" }),
        accountOut({ id: "a-2", name: "College Fund", path: "assets.college", type: "brokerage" }),
      ]),
      "POST /memories": ok({}),
    });
    const { user } = renderWithProviders(<Memories />);

    // A link that is an account path reads as the name; a payee stays as is.
    expect(await screen.findByText("College Fund · Example Payee")).toBeInTheDocument();
    expect(screen.queryByText(/assets\./)).not.toBeInTheDocument();
    expect(screen.queryByText("Nuance")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("What should Purser remember?"), "checking is the bills account");
    await user.click(screen.getByLabelText("Checking"));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.POST).toHaveBeenCalledWith("/memories", {
      body: {
        text: "checking is the bills account",
        accounts: ["assets.checking"],
        pinned: false,
        activate: true,
      },
    });
  });
});
