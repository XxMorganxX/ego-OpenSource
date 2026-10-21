import "@testing-library/jest-dom";

// jsdom's Storage implementation is incomplete in this environment, so install a
// small in-memory localStorage with the full API for deterministic tests.
class MemoryStorage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value));
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null;
  }
}

Object.defineProperty(globalThis, "localStorage", {
  value: new MemoryStorage(),
  writable: true,
  configurable: true,
});

// The Database page holds its session password in sessionStorage; give it the same
// in-memory shim so the gate behaves deterministically under jsdom.
Object.defineProperty(globalThis, "sessionStorage", {
  value: new MemoryStorage(),
  writable: true,
  configurable: true,
});

// jsdom has no ResizeObserver; GraphView uses one to size its canvas to the pane.
// A no-op stub is enough — layout measurements are 0 in jsdom anyway.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// jsdom has no canvas backend: its getContext() logs a "Not implemented" error with a
// full stack trace (noise in a green run) and then returns null anyway. Return null
// quietly instead; canvas callers (nodeSprites' renderSprite) already handle a null
// context. A test that needs a 2D context can still vi.spyOn this.
if (typeof HTMLCanvasElement !== "undefined") {
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
}
