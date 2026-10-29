import { describe, expect, it } from "vitest";
import { errorDetail } from "./api";

describe("errorDetail", () => {
  it("passes a FastAPI string detail through", () => {
    expect(errorDetail({ detail: "email already exists" })).toBe("email already exists");
  });

  it("serialises a structured (validation) detail", () => {
    const detail = [{ loc: ["body", "email"], msg: "Field required", type: "missing" }];
    expect(errorDetail({ detail })).toBe(JSON.stringify(detail));
  });

  it("falls back to a generic message when there is no detail", () => {
    expect(errorDetail(undefined)).toBe("request failed");
    expect(errorDetail("Bad Gateway")).toBe("request failed");
    expect(errorDetail({ message: "nope" })).toBe("request failed");
  });
});
