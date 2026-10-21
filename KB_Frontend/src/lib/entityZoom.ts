import { useCallback, useEffect, useRef, useState } from "react";

// --- Experimental: animated zoom transition between the all-entities graph and a
// single class' instances. Self-contained so it can be repealed by deleting this file
// and its (clearly marked) call sites in GraphView. ---

type Fg = any;

const FLIGHT_MS = 180; // camera flight before the data swaps
const FIT_MS = 240; // camera glide while framing the focused view
// The exit is a single glide: by the time the threshold is crossed the crossfade has
// already revealed the all-entities frame at full opacity, so the camera just keeps
// going from wherever the user's gesture left it — recentering on the overview and
// finishing the zoom-out. No snap back, no hold.
const EXIT_MS = 500;
const FIT_PAD_FOCUS = 70;
// The all-entities classes pack tightly near the hub, so a snug fit would actually be MORE
// zoomed-in than a focused class. We frame it with generous margin instead, so leaving a
// class is a genuine zoom-OUT (and matches the airy all-entities view you first land on).
const FIT_PAD_ALL = 150;
// Pop back only after a deliberate zoom-OUT to below this fraction of the zoom the focused
// view settled at. LOWER = a bigger pull-back is required before it triggers. Gentler than
// it used to be (0.3): the crossfade gives live feedback across the whole ramp, so the
// gesture no longer needs a long silent run-up to feel deliberate — and the overview's own
// fit level sits near the focused baseline (measured ~1.1×), so every extra tick of required
// pull-back is distance the exit glide has to travel back IN afterwards.
const EXIT_ZOOM_RATIO = 0.55;
// Where the ghost reveal begins, as a fraction of the settled baseline zoom. Between this
// and EXIT_ZOOM_RATIO the hidden frame fades in / the instances fade out with the gesture.
export const GHOST_START_RATIO = 0.85;
// A class with NO instances frames as a single node, and fitting one node jams the camera to a
// near-maximal zoom — so the normal ratio would demand an enormous (often unreachable) pull-back
// to escape. With nothing else to look at, a small zoom-out should already mean "let me out", so
// a sparse view triggers far more readily.
// Above 1.0 this means ANY zoom gesture — in or out — lands below the threshold, so the first
// wheel/pinch in a sparse view exits immediately.
const EXIT_ZOOM_RATIO_SPARSE = 3.0;
// At or below this many visible nodes the focused view counts as "sparse" and uses the lenient
// ratio. 1 = just the lone class node (the no-instances case the sparse ratio exists for).
const SPARSE_NODE_COUNT = 1;
// The incoming nodes are pre-seeded into a sensible layout (see GraphView), so we can
// frame and reveal the new view almost immediately rather than waiting for the sim.
const FIT_DELAY_MS = 60;

type Pending = null | "focus" | "all";

/**
 * Crossfade progress for the scroll-out exit gesture: 0 while the camera is at (or above)
 * `base * startRatio`, 1 at (and past) the exit threshold `base * exitRatio`, smoothstepped
 * in between. GraphView renders the hidden all-entities frame ghosted at this alpha (and the
 * focused instances at its complement), so the grace period IS the transition.
 *
 * When `exitRatio >= startRatio` there is no zoom range to ramp across (the sparse one-node
 * case, whose lenient ratio sits ABOVE the baseline), so this degenerates to a hard 0/1 step
 * at the threshold.
 */
export function exitProgress(
  k: number,
  base: number | null,
  exitRatio: number,
  startRatio: number = GHOST_START_RATIO,
): number {
  if (!base || base <= 0) return 0;
  const exitK = base * exitRatio;
  if (exitRatio >= startRatio) return k <= exitK ? 1 : 0;
  const startK = base * startRatio;
  if (k >= startK) return 0;
  if (k <= exitK) return 1;
  const u = (startK - k) / (startK - exitK); // 0 at the ramp start → 1 at the threshold
  return u * u * (3 - 2 * u); // smoothstep — flat near the edges, no visible kink
}

/**
 * Drives the zoom-in/zoom-out choreography for the entity graph.
 *
 *  - `zoomIntoClass(node)` flies the camera onto a class node, fades the canvas out,
 *    then (after the flight) asks the parent to focus that class. The fade hides the
 *    layout discontinuity when the graph data swaps.
 *  - `handleZoom`/`handleZoomEnd` watch the user's zoom once a focus fit has settled and
 *    publish `exitProgress` through `exitProgressRef`; GraphView reads it every frame to
 *    crossfade the hidden frame in (and the instances out) as the gesture proceeds. At
 *    progress 1 the view pops back to all-entities — the swap lands on a fully-revealed
 *    frame, so the exit glide is seamless. Zooming in is ignored and never moves the
 *    baseline; backing off mid-ramp just fades the ghosts away again.
 *  - `handleEngineStop` (and a fallback timer) frame the freshly-swapped graph and fade
 *    the canvas back in.
 *
 * `focusedClass` is the parent's selected-class id; the hook reacts to it changing.
 */
export function useEntityZoom(
  fgRef: React.MutableRefObject<Fg>,
  // The focused class' id (null = the all-entities overview). The id — not a boolean —
  // because a direct class→class switch (sidebar) never passes through "unfocused": it
  // must still re-fit, re-arm the baseline, reclassify sparse, and drop any mid-ramp
  // progress, or the old class' exit state leaks into the new view.
  focusedClass: string | null,
  isEntities: boolean,
  onSelectClass: (id: string | null) => void,
  // Predicate for the currently-visible nodes — the fit frames only these, so it ignores
  // the hidden-but-present classes/hub that share the space.
  visibleRef: React.MutableRefObject<(n: any) => boolean>,
  // How many nodes are currently visible, maintained by GraphView (which owns the data).
  // Read at arm time to classify the focused view as sparse. This MUST come from the
  // parent: react-force-graph's ref does not expose graphData(), so reading it here
  // silently yielded 0 — every class counted as sparse and the first wheel tick exited
  // instantly (the original "grace period" complaint).
  visibleCountRef: React.MutableRefObject<number>,
) {
  const [fading, setFading] = useState(false);
  // "settling" = the fit's own (animated) zoom is in flight; we ignore zoom events then so
  // the fit's motion isn't mistaken for the user zooming.
  const phaseRef = useRef<"idle" | "in" | "out" | "settling">("idle");
  const pendingFitRef = useRef<Pending>(null);
  // Zoom the focused view settled at; the exit gesture measures against it. Captured only
  // AFTER the fit animation ends (not during), and null whenever exit detection is off.
  const baselineZoomRef = useRef<number | null>(null);
  // Live crossfade progress of the exit gesture (0 = fully focused, 1 = frame fully
  // revealed). A ref, not state: it changes on every wheel tick and force-graph re-reads
  // the paint accessors each frame anyway, so no re-render is needed or wanted.
  const exitProgressRef = useRef(0);
  // The exit ratio in force for the current focused view — lenient for a sparse (one-node) view,
  // strict otherwise. Set alongside the baseline once the focus fit settles.
  const exitRatioRef = useRef<number>(EXIT_ZOOM_RATIO);

  // Gate user wheel-zoom on the idle phase. Passed to <ForceGraph2D enableZoomInteraction>,
  // which force-graph evaluates per wheel event — so while a transition is animating (in /
  // out / settling) scroll is ignored entirely and can't "keep scrolling" the camera. Stable
  // (reads the live ref), so it never re-subscribes the zoom behavior.
  const zoomInteractionAllowed = useCallback(() => phaseRef.current === "idle", []);

  const fit = useCallback(() => {
    const fg = fgRef.current;
    if (!fg || !pendingFitRef.current) return;
    const mode = pendingFitRef.current;
    pendingFitRef.current = null;
    const isFocus = mode === "focus";
    // Either way mark the phase non-idle so the zoom handlers (and the wheel-lock) ignore the
    // fit's own motion. `settleMs` is the full animation length, used to re-arm at the end.
    phaseRef.current = "settling";
    baselineZoomRef.current = null;
    let settleMs: number;
    if (isFocus) {
      settleMs = FIT_MS;
      fg.zoomToFit?.(FIT_MS, FIT_PAD_FOCUS, visibleRef.current);
    } else {
      // Exit: the data has swapped and (for a gesture exit) the crossfade already revealed
      // the frame at full opacity, so one glide from wherever the camera is finishes the
      // job — recentering on the overview and completing the zoom-out.
      settleMs = EXIT_MS;
      exitProgressRef.current = 0; // the overview is real now; the ghost layer is done
      fg.zoomToFit?.(EXIT_MS, FIT_PAD_ALL, visibleRef.current);
    }
    setFading(false); // ends the drill-in fade; a no-op on the (un-faded) zoom-out exit
    // Re-arm exit detection once the fit's own motion is over, and capture the FIXED exit
    // baseline: the zoom the focused view actually came to rest at — whether framing it was a
    // zoom-IN (sparse class) or a zoom-OUT (wide class). Exit then needs a deliberate zoom-OUT
    // far below this; zooming in never moves it. (Reading the real settled zoom here is more
    // robust than inferring it from the fit's own zoom events.)
    window.setTimeout(() => {
      phaseRef.current = "idle";
      if (isFocus) {
        exitProgressRef.current = 0;
        baselineZoomRef.current = fgRef.current?.zoom?.() ?? null;
        // Pick the exit ratio from how many nodes the focused view actually frames: a lone class
        // node (no instances) fits to an extreme zoom, so it gets the lenient sparse ratio; a
        // populated class keeps the strict one.
        exitRatioRef.current =
          visibleCountRef.current <= SPARSE_NODE_COUNT
            ? EXIT_ZOOM_RATIO_SPARSE
            : EXIT_ZOOM_RATIO;
      }
    }, settleMs + 120);
  }, [fgRef, visibleRef, visibleCountRef]);

  // Drill into a class: fly the camera onto the node, fade out, then swap views.
  const zoomIntoClass = useCallback(
    (node: any) => {
      const fg = fgRef.current;
      if (!fg || phaseRef.current !== "idle") return;
      phaseRef.current = "in";
      baselineZoomRef.current = null;
      exitProgressRef.current = 0;
      setFading(true);
      fg.centerAt?.(node.x, node.y, FLIGHT_MS);
      fg.zoom?.(Math.max((fg.zoom?.() ?? 1) * 4, 6), FLIGHT_MS);
      window.setTimeout(() => onSelectClass(node.id), FLIGHT_MS);
    },
    [fgRef, onSelectClass],
  );

  // Track the exit gesture and pop back to all-entities ONLY on a genuine zoom-OUT well
  // below the level the focused view settled at. The baseline is fixed (captured in fit()
  // once the framing settles), so zooming IN never triggers this and never moves the
  // goalposts. Active only once a focused fit has settled (phase idle + baseline captured).
  // Wired to BOTH onZoom (fires live mid-gesture, so the crossfade tracks the wheel and the
  // swap is immediate — no waiting on d3-zoom's ~150ms debounced "end" event) and onZoomEnd
  // (catches the gesture's final frame). No fade and no camera snap: the crossfade has
  // already revealed the frame by the time the threshold is crossed, so swapping the data
  // and letting fit() glide the un-faded zoomToFit reads as one continuous motion.
  const handleExitZoom = useCallback(
    (t: { k: number }) => {
      if (focusedClass == null || phaseRef.current !== "idle") return;
      const base = baselineZoomRef.current;
      if (base == null) return;
      const p = exitProgress(t.k, base, exitRatioRef.current);
      exitProgressRef.current = p;
      if (p >= 1) {
        phaseRef.current = "out";
        baselineZoomRef.current = null;
        onSelectClass(null);
      }
    },
    [focusedClass, onSelectClass],
  );

  // Frame the new view (and, for a focus, capture the exit baseline) on every focus
  // transition — whether the class was entered by clicking its node OR from the sidebar,
  // and whether the exit was the zoom-out gesture OR the sidebar. Keyed on the class ID
  // rather than the drill-in phase or a focused boolean: a direct class→class switch
  // (sidebar) never passes through "unfocused", yet it must re-fit and re-arm all the
  // same — a sparse ratio or baseline carried over from the previous class would make
  // the new view's first wheel tick exit instantly, and stale mid-ramp progress would
  // ghost the new view until the next zoom event.
  const prevFocusRef = useRef<string | null>(null);
  useEffect(() => {
    if (!isEntities) {
      prevFocusRef.current = null;
      exitProgressRef.current = 0;
      return;
    }
    const was = prevFocusRef.current;
    prevFocusRef.current = focusedClass;
    if (was === focusedClass) return; // not a focus transition (e.g. just entering entities mode)
    // The previous view's exit state is void the moment the focus changes — disarm and
    // un-ghost NOW, not when the new fit settles, so nothing leaks into the interim frames.
    exitProgressRef.current = 0;
    baselineZoomRef.current = null;
    pendingFitRef.current = focusedClass ? "focus" : "all";
    const t = window.setTimeout(fit, FIT_DELAY_MS);
    return () => clearTimeout(t);
  }, [focusedClass, isEntities, fit]);

  const handleEngineStop = useCallback(() => {
    if (pendingFitRef.current) fit();
  }, [fit]);

  return {
    fading,
    zoomIntoClass,
    handleEngineStop,
    handleZoomEnd: handleExitZoom,
    handleZoom: handleExitZoom,
    zoomInteractionAllowed,
    exitProgressRef,
  };
}
