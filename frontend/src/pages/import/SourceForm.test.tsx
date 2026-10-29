import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { SourceForm } from "./SourceForm";

function renderForm(over: Partial<Parameters<typeof SourceForm>[0]> = {}) {
  const props = {
    content: "",
    format: "qif" as const,
    dateFormat: "mdy",
    busy: false,
    hasPreview: false,
    error: null,
    onSource: vi.fn(),
    onFormat: vi.fn(),
    onDateFormat: vi.fn(),
    onPreview: vi.fn(),
    ...over,
  };
  return { props, ...renderWithProviders(<SourceForm {...props} />) };
}

describe("SourceForm", () => {
  it("passes pasted text on", async () => {
    const { props, user } = renderForm();

    await user.click(screen.getByPlaceholderText(/!Type:Bank/));
    await user.paste("!Type:Bank\n");

    expect(props.onSource).toHaveBeenCalledWith("!Type:Bank\n");
  });

  it("reads a chosen file, letting its .csv extension set the format", async () => {
    const { props, user, container } = renderForm();
    const file = new File(["Date,Payee,Amount\n"], "export.CSV", { type: "text/csv" });

    await user.upload(container.querySelector<HTMLInputElement>('input[type="file"]')!, file);

    await waitFor(() => expect(props.onSource).toHaveBeenCalledWith("Date,Payee,Amount\n", "csv"));
  });

  it("only previews once there is something to read", async () => {
    renderForm({ content: "   " });
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
  });

  it("previews on request, and says it is reading meanwhile", async () => {
    const { props, user, rerender } = renderForm({ content: "!Type:Bank\n" });

    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(props.onPreview).toHaveBeenCalledOnce();

    rerender(<SourceForm {...props} busy />);
    expect(screen.getByRole("button", { name: "Reading…" })).toBeDisabled();
  });

  it("passes format and date-order choices on, and shows the error", async () => {
    const { props, user } = renderForm({ error: "line 3: unparseable date" });

    await user.selectOptions(screen.getByDisplayValue("QIF"), "csv");
    await user.selectOptions(screen.getByDisplayValue("MM/DD/YYYY"), "dmy");

    expect(props.onFormat).toHaveBeenCalledWith("csv");
    expect(props.onDateFormat).toHaveBeenCalledWith("dmy");
    expect(screen.getByText("line 3: unparseable date")).toBeInTheDocument();
  });
});
