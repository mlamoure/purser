import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockApi, ok } from "../test/api";
import { renderWithProviders } from "../test/render";
import { useFormatTimestamp, useLedgerToday } from "./clock";

function Stamp({ iso }: { iso: string }) {
  const formatTimestamp = useFormatTimestamp();
  return (
    <p>
      {useLedgerToday()} · {formatTimestamp(iso)}
    </p>
  );
}

describe("useFormatTimestamp (#540)", () => {
  it("formats in the household time zone GET /clock reports", async () => {
    mockApi({
      "GET /clock": ok({ ledger_today: "2026-11-17", timezone: "America/Chicago" }),
    });
    renderWithProviders(<Stamp iso="2026-11-17T14:42:00Z" />);
    expect(await screen.findByText("2026-11-17 · Nov 17, 2026, 8:42 AM")).toBeInTheDocument();
  });
});
