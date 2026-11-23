/* What a screen reader says and what the eye sees of an element (#556), for
 * text marked for one but not the other: an aria-hidden subtree is not
 * spoken, and a Tailwind `sr-only` (the widgets' `visually-hidden`) subtree is
 * not shown. jsdom applies no stylesheet, so the class is read directly. */

function collect(node: Node, skip: (el: Element) => boolean): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? "";
  if (node instanceof Element && skip(node)) return "";
  return [...node.childNodes].map((child) => collect(child, skip)).join("");
}

/** The text a screen reader reads: aria-hidden subtrees left out. */
export function spokenText(node: Node): string {
  return collect(node, (el) => el.getAttribute("aria-hidden") === "true");
}

/** The text on screen: visually hidden subtrees left out. */
export function shownText(node: Node): string {
  return collect(node, (el) => el.classList.contains("sr-only") || el.classList.contains("visually-hidden"));
}
