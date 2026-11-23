import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../test/render";
import { CopyButton } from "./CopyButton";

/** Plain HTTP off localhost: the browser has no `navigator.clipboard`. */
function withoutClipboardApi() {
  const original = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  return () => {
    if (original) Object.defineProperty(navigator, "clipboard", original);
    else delete (navigator as { clipboard?: unknown }).clipboard;
  };
}

describe("CopyButton", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete (document as { execCommand?: unknown }).execCommand;
  });

  it("copies and confirms", async () => {
    const { user } = renderWithProviders(<CopyButton text="synthetic-value" />);

    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await navigator.clipboard.readText()).toBe("synthetic-value");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("falls back to copying a selection where the clipboard API does not exist", async () => {
    const { user } = renderWithProviders(<CopyButton text="synthetic-value" />);
    const restore = withoutClipboardApi();
    const copied: string[] = [];
    document.execCommand = vi.fn(() => {
      copied.push(document.querySelector("textarea")!.value);
      return true;
    });
    try {
      await user.click(screen.getByRole("button", { name: "Copy" }));
      expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    } finally {
      restore();
    }
    expect(copied).toEqual(["synthetic-value"]);
    expect(document.querySelector("textarea")).toBeNull();
  });

  it("asks for a manual copy when the browser allows neither", async () => {
    const { user } = renderWithProviders(<CopyButton text="synthetic-value" />);
    const restore = withoutClipboardApi();
    document.execCommand = vi.fn(() => false);
    try {
      await user.click(screen.getByRole("button", { name: "Copy" }));
      expect(
        await screen.findByRole("button", { name: "Select the text and copy it" }),
      ).toBeInTheDocument();
    } finally {
      restore();
    }
  });
});
