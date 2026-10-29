import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { apiMock, fail, mockApi, ok } from "../../test/api";
import { accountLabel, useFeedConnection, type Discovered } from "./useFeedConnection";

const CONNECTION = {
  id: "c-1",
  provider: "simplefin",
  status: "active",
  created_at: "2026-01-05T12:00:00Z",
  last_fetch_at: null,
};
const row = (mappedPath: string | null = null): Discovered => ({
  external_id: "ext-1",
  name: "Everyday Checking",
  org: "Example Bank",
  currency: "USD",
  balance: "1250.00",
  mapped_account_id: mappedPath ? "a-1" : null,
  mapped_account_path: mappedPath,
  drift_basis_pinned: false,
});

/** Run `fn` inside act and return what it threw. The error is caught inside
 * act on purpose: when act's callback rejects, React drops the updates it
 * queued, so state set before the throw would never render. */
async function actCatching(fn: () => Promise<void>): Promise<unknown> {
  let thrown: unknown;
  await act(async () => {
    try {
      await fn();
    } catch (e) {
      thrown = e;
    }
  });
  return thrown;
}

describe("accountLabel", () => {
  it("turns the bank's account name into a ledger label", () => {
    expect(accountLabel({ name: "Joint Checking (x1234)", external_id: "ext-1" })).toBe("joint_checking_x1234");
    expect(accountLabel({ name: null, external_id: "ACT-99" })).toBe("act_99");
  });
});

describe("useFeedConnection", () => {
  it("connects, runs the first fetch and lists the discovered accounts", async () => {
    mockApi({
      "POST /connections": ok(CONNECTION, 201),
      "POST /connections/{connection_id}/sync": ok({}),
      "GET /connections/{connection_id}/discovered": ok([row()]),
    });
    const { result } = renderHook(() => useFeedConnection());

    await act(() => result.current.connect("synthetic-setup-token"));

    expect(result.current.connectionId).toBe("c-1");
    expect(result.current.discovered).toEqual([row()]);
    expect(apiMock.POST.mock.calls.map(([path]) => path)).toEqual([
      "/connections",
      "/connections/{connection_id}/sync",
    ]);
  });

  it("throws the server's reason when the setup token is refused", async () => {
    mockApi({ "POST /connections": fail(400, "setup token already claimed") });
    const { result } = renderHook(() => useFeedConnection());

    expect(await actCatching(() => result.current.connect("synthetic-setup-token"))).toEqual(
      new Error("setup token already claimed"),
    );
    expect(result.current.connectionId).toBeNull();
  });

  it("keeps the connection when the first fetch fails", async () => {
    mockApi({
      "POST /connections": ok(CONNECTION, 201),
      "POST /connections/{connection_id}/sync": fail(502, "SimpleFIN Bridge unavailable"),
    });
    const { result } = renderHook(() => useFeedConnection());

    expect(await actCatching(() => result.current.connect("synthetic-setup-token"))).toEqual(
      new Error("SimpleFIN Bridge unavailable"),
    );
    expect(result.current.connectionId).toBe("c-1");
    expect(result.current.discovered).toEqual([]);
  });

  it("maps an account to a new checking account under assets. and refreshes", async () => {
    let mapped = false;
    mockApi({
      "POST /connections": ok(CONNECTION, 201),
      "POST /connections/{connection_id}/sync": ok({}),
      "GET /connections/{connection_id}/discovered": () =>
        ok([row(mapped ? "assets.everyday_checking" : null)]),
      "POST /connections/{connection_id}/map": () => {
        mapped = true;
        return ok({});
      },
    });
    const { result } = renderHook(() => useFeedConnection());
    await act(() => result.current.connect("synthetic-setup-token"));

    await act(() => result.current.mapAccount(row()));

    expect(apiMock.POST).toHaveBeenCalledWith("/connections/{connection_id}/map", {
      params: { path: { connection_id: "c-1" } },
      body: {
        external_id: "ext-1",
        create: { name: "Everyday Checking", type: "checking", path: "assets.everyday_checking" },
      },
    });
    expect(result.current.discovered[0].mapped_account_path).toBe("assets.everyday_checking");
  });

  it("refuses to map before a feed is connected", async () => {
    const { result } = renderHook(() => useFeedConnection());

    await expect(result.current.mapAccount(row())).rejects.toThrow("connect a SimpleFIN feed first");
    expect(apiMock.POST).not.toHaveBeenCalled();
  });
});
