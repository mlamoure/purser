import { describe, expect, it } from "vitest";
import { hashRouteFor } from "./hashRoute";

describe("hashRouteFor (#542)", () => {
  it("moves an agent's path-style link into the hash, query and all", () => {
    expect(
      hashRouteFor({ pathname: "/selection", search: "?id=t-1&id=t-2&note=two+largest", hash: "" }),
    ).toBe("/#/selection?id=t-1&id=t-2&note=two+largest");
    expect(hashRouteFor({ pathname: "/settings/connections", search: "", hash: "" })).toBe(
      "/#/settings/connections",
    );
  });

  it("leaves the root and existing hash routes alone", () => {
    expect(hashRouteFor({ pathname: "/", search: "", hash: "" })).toBeNull();
    expect(hashRouteFor({ pathname: "/", search: "", hash: "#/settings" })).toBeNull();
    expect(hashRouteFor({ pathname: "/index.html", search: "", hash: "" })).toBeNull();
    expect(hashRouteFor({ pathname: "/selection", search: "", hash: "#/accounts" })).toBeNull();
  });
});
