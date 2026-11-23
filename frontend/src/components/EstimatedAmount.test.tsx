import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { shownText, spokenText } from "../test/a11y";
import { EstimateText, EstimatedAmount } from "./EstimatedAmount";

describe("EstimatedAmount (#556)", () => {
  it("shows ≈ −$95.00 and is heard as estimated −$95.00", () => {
    render(
      <table>
        <tbody>
          <tr>
            <td>
              <EstimatedAmount value="-95.0000" />
            </td>
          </tr>
        </tbody>
      </table>,
    );
    // The cell's accessible name leaves the aria-hidden mark out.
    const cell = screen.getByRole("cell", { name: "estimated −$95.00" });
    expect(shownText(cell)).toBe("≈ −$95.00");
    expect(spokenText(cell)).toBe("estimated −$95.00");
    expect(spokenText(cell)).not.toMatch(/≈|approximately|~/);
  });

  it("keeps the formatter's rendering: currency, positive amounts, no minus on zero", () => {
    const { container, rerender } = render(<EstimatedAmount value="1200" currency="EUR" />);
    expect(shownText(container)).toBe("≈ €1,200.00");
    rerender(<EstimatedAmount value="-0.001" />);
    expect(shownText(container)).toBe("≈ $0.00");
    rerender(<EstimatedAmount value={null} />);
    expect(container.textContent).toBe("—"); // a missing amount is not marked
  });
});

describe("EstimateText (#556)", () => {
  it("reads a server headline's mark as estimated", () => {
    const { container } = render(<EstimateText text={"Electric: monthly ≈ −95.00 on Checking"} />);
    expect(shownText(container)).toBe("Electric: monthly ≈ −95.00 on Checking");
    expect(spokenText(container)).toBe("Electric: monthly estimated −95.00 on Checking");
  });

  it("leaves text without an estimate as it is", () => {
    const { container } = render(<EstimateText text="Gym: monthly -15.99 on Checking" />);
    expect(container.innerHTML).toBe("Gym: monthly -15.99 on Checking");
  });
});
