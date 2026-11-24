import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fixtureFetch } from "../test/fixtures";
import { PersistedWorkspace } from "./PersistedWorkspace";
import { ExplorationClient } from "../api/client";

let posts: { url: string; body: Record<string, unknown>; key: string | null }[] = [];
let calls: string[] = [];
let fail: Response | null = null;

const created = (question: string) => ({
  id: "inv_new_test",
  question,
  budget: { max_searches: 2, max_fetched_urls: 8, max_llm_calls: 6, searches_used: 0, urls_fetched: 0, llm_calls_used: 0 },
  status: "created",
  usage: {},
  created_at: "2026-11-20T00:00:00Z",
  updated_at: "2026-11-20T00:00:00Z",
  metadata: {},
});

beforeEach(() => {
  posts = [];
  calls = [];
  fail = null;
  window.localStorage.clear();
  window.history.replaceState(null, "", "/?mode=live");
  vi.stubGlobal(
    "fetch",
    fixtureFetch(calls, {
      handle: async (url, init) => {
        if (init?.method === "POST") {
          const headers = new Headers(init.headers);
          const body = JSON.parse(String(init.body ?? "{}"));
          posts.push({ url, body, key: headers.get("Idempotency-Key") });
          await new Promise((r) => setTimeout(r, 30));
          if (fail) return fail;
          return new Response(JSON.stringify(created(body.question)), { status: 200 });
        }
        if (url.includes("/investigations?")) return new Response(JSON.stringify({ items: [], total: 0, limit: 100, offset: 0 }), { status: 200 });
        return undefined;
      },
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("New investigation (live mode)", () => {
  it("stores the question once with an idempotency key, then opens it", async () => {
    render(<PersistedWorkspace render3D={() => <div />} />);
    fireEvent.change(await screen.findByTestId("new-question"), { target: { value: "How do GPU export rules affect AI capacity?" } });
    const save = screen.getByTestId("create-investigation");
    act(() => {
      fireEvent.click(save);
      fireEvent.click(save);
    });
    await waitFor(() => expect(window.location.search).toContain("investigation=inv_new_test"));
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/api/exploration/investigations");
    expect(posts[0].body).toMatchObject({ question: "How do GPU export rules affect AI capacity?", budget: { max_searches: 2 } });
    expect(posts[0].key).toMatch(/.{8,}/);
    expect(posts.some((p) => p.url.endsWith("/step"))).toBe(false);
  });

  it("uses a fresh key after a 4xx (the backend did not act)", async () => {
    render(<PersistedWorkspace render3D={() => <div />} />);
    fireEvent.change(await screen.findByTestId("new-question"), { target: { value: "Which suppliers gained from HBM demand?" } });
    fail = new Response(JSON.stringify({ detail: "bad" }), { status: 422 });
    fireEvent.click(screen.getByTestId("create-investigation"));
    await screen.findByTestId("create-error");
    fireEvent.click(screen.getByTestId("create-investigation"));
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].key).not.toBe(posts[0].key);
    // Let the second request settle before the test ends; otherwise its state update can
    // land after the environment is torn down (seen on CI as "window is not defined").
    await waitFor(() => expect(screen.getByTestId("create-investigation").textContent).toContain("Save question"));
  });

  it("refuses very short questions", async () => {
    render(<PersistedWorkspace render3D={() => <div />} />);
    fireEvent.change(await screen.findByTestId("new-question"), { target: { value: "why" } });
    expect((screen.getByTestId("create-investigation") as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("client: billable step is gated", () => {
  it("stepInvestigation rejects without touching the network when billing is off", async () => {
    const f = vi.fn();
    const client = new ExplorationClient(f as unknown as typeof fetch);
    await expect(client.stepInvestigation("inv_x", { confirmedBillable: true })).rejects.toThrow(/Billable research is disabled/);
    expect(f).not.toHaveBeenCalled();
  });
});
