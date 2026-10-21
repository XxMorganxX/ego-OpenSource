import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchAgeDecay = vi.fn();
const setAgeDecay = vi.fn();
const fetchPolicy = vi.fn();
vi.mock("../api/client", () => ({
  fetchAgeDecay: () => fetchAgeDecay(),
  setAgeDecay: (e: boolean) => setAgeDecay(e),
  fetchPolicy: () => fetchPolicy(),
}));

let feedCb: ((tables: Set<string>) => void) | undefined;
vi.mock("../hooks/useChangeFeed", () => ({
  useChangeFeed: (cb: (tables: Set<string>) => void) => {
    feedCb = cb;
  },
}));

import { AgeDecayToggle } from "./AgeDecayToggle";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("AgeDecayToggle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchAgeDecay.mockResolvedValue({ enabled: true });
    fetchPolicy.mockResolvedValue({
      source: "policy.toml", is_default: false, overlays: [],
      policy: { decay: [{ name: "email", match: { tags: ["email"] }, half_life_days: 17, floor: 0.2 }] },
    });
  });

  it("renders the server-confirmed state", async () => {
    render(<AgeDecayToggle />);
    const sw = await screen.findByRole("switch", { name: "Age decay" });
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(sw).toHaveTextContent("On");
  });

  it("flip POSTs and renders the response, with no optimistic update", async () => {
    const flip = deferred<{ enabled: boolean }>();
    setAgeDecay.mockReturnValue(flip.promise);
    render(<AgeDecayToggle />);
    const sw = await screen.findByRole("switch", { name: "Age decay" });
    expect(sw).toHaveAttribute("aria-checked", "true");

    await userEvent.click(sw);
    expect(setAgeDecay).toHaveBeenCalledWith(false);
    // Still showing the last confirmed state — the write hasn't resolved yet.
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(sw).toHaveTextContent("On");

    await act(async () => flip.resolve({ enabled: false }));
    await waitFor(() => expect(sw).toHaveAttribute("aria-checked", "false"));
    expect(sw).toHaveTextContent("Off");
  });

  it("surfaces a rejected flip via role=alert and keeps the last confirmed state", async () => {
    setAgeDecay.mockRejectedValue(new Error("500: broken"));
    render(<AgeDecayToggle />);
    const sw = await screen.findByRole("switch", { name: "Age decay" });
    await userEvent.click(sw);
    expect(await screen.findByRole("alert")).toHaveTextContent("500");
    // No optimistic update: still showing the last confirmed (enabled) state.
    expect(sw).toHaveAttribute("aria-checked", "true");
    expect(sw).toHaveTextContent("On");
  });

  it("reloads when age_decay_state changes on the feed", async () => {
    render(<AgeDecayToggle />);
    await screen.findByRole("switch", { name: "Age decay" });
    expect(fetchAgeDecay).toHaveBeenCalledTimes(1);

    fetchAgeDecay.mockResolvedValue({ enabled: false });
    feedCb?.(new Set(["age_decay_state"]));
    await waitFor(() => expect(fetchAgeDecay).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Age decay" })).toHaveAttribute(
        "aria-checked",
        "false",
      ),
    );
  });

  it("lists the active rules in the hint", async () => {
    render(<AgeDecayToggle />);
    await screen.findByRole("switch", { name: "Age decay" });
    expect(await screen.findByText(/Rules: email \(17 d\)/)).toBeInTheDocument();
  });

  it("disables the switch and explains when no rules are configured", async () => {
    fetchPolicy.mockResolvedValue({ source: null, is_default: true, overlays: [],
                                    policy: { decay: [] } });
    render(<AgeDecayToggle />);
    const sw = await screen.findByRole("switch", { name: "Age decay" });
    expect(sw).toBeDisabled();
    expect(await screen.findByText(/No decay rules configured/)).toBeInTheDocument();
  });
});
