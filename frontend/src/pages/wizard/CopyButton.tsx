import { useEffect, useRef, useState } from "react";
import { Button } from "../../components/ui";

/** Put `text` on the clipboard. `navigator.clipboard` exists only on HTTPS and
 * on localhost, and Purser is also opened over plain HTTP from another
 * machine on a home network, so fall back to copying a selection. Resolves
 * false when the browser allows neither. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Refused (permissions, an embedded frame): try the selection instead.
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
  }
}

/** A copy button that says what happened: "Copied", or a request to select
 * the text by hand when the browser refuses. */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      onClick={async () => {
        setState((await copyText(text)) ? "copied" : "failed");
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setState("idle"), 2500);
      }}
    >
      <span aria-live="polite">
        {state === "copied" ? "Copied" : state === "failed" ? "Select the text and copy it" : label}
      </span>
    </Button>
  );
}
