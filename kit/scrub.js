/*
 * motion-kit — canvas image-sequence scrub engine
 *
 * Vanilla JS. No dependencies, no build step. Paint a numbered frame to a
 * <canvas> based on scroll position; scroll down and the clip plays, scroll up
 * and it plays backward.
 *
 * THE INVARIANT THIS FILE IS BUILT AROUND: css shows, js hides.
 * Every piece of overlay copy is visible by default in styles.css. This engine
 * only ever *removes* visibility, and only once it is actually running. So a
 * script error, a 404 on this file, a blocked CSP, reduced-motion or Save-Data
 * all leave a complete, readable, sellable page. That is a structural property,
 * not something you have to remember to test.
 *
 * Config comes from window.SCRUB_SECTIONS — paste it from:
 *     python motionkit.py frames --project <project> --name <section>
 * Frame numbers are 1-based, matching ffmpeg's image2 muxer.
 */
(function () {
  "use strict";

  /* ── tunables ──────────────────────────────────────────────────────────── */

  var MOBILE_MAX = 767;        // must equal the breakpoint in styles.css
  var COARSE_STRIDE = 8;       // load every Nth frame first
  var GAP_TOLERANCE = 16;      // paint the nearest loaded frame within +/- this
  var LERP = 0.12;             // scroll easing; trackpad flicks glide
  var DPR_CAP = 2;
  var LOAD_MARGIN = "100% 0px"; // start loading one viewport ahead
  var PAINT_MARGIN = "10% 0px"; // gate the rAF loop
  var FILL_CHUNK = 12;
  var RESIZE_DEBOUNCE = 150;
  var COARSE_BUDGET_MS = 12000; // give up and stay on the poster
  var LOAD_GATE_MS = 3000;
  var LINE_RAMP = 0.08;        // default data-in/data-out window length
  var LINE_RISE = 24;          // px of travel on a fading line
  var SETTLE = 0.0005;

  /* ── utilities ─────────────────────────────────────────────────────────── */

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function lerp(a, b, t) { return a + (b - a) * t; }

  function smoothstep(a, b, x) {
    if (b <= a) return x >= b ? 1 : 0;
    var e = clamp((x - a) / (b - a), 0, 1);
    return e * e * (3 - 2 * e);
  }

  function debounce(fn, ms) {
    var timer = 0;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };
  }

  function yieldToLoop() {
    return new Promise(function (resolve) { setTimeout(resolve, 0); });
  }

  function after(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function flag(name) {
    try {
      return new URLSearchParams(window.location.search).get(name);
    } catch (e) { return null; }
  }

  /* ── environment ───────────────────────────────────────────────────────── */

  var reduceQuery = window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;

  function prefersReducedMotion() {
    return !!(reduceQuery && reduceQuery.matches);
  }

  function saveData() {
    var c = navigator.connection;
    return !!(c && (c.saveData || c.effectiveType === "2g" || c.effectiveType === "slow-2g"));
  }

  // The page must be complete and sellable with zero frames loaded, so this is
  // a hard stop rather than a degraded mode. ?scrub=on forces past it for QA.
  function shouldSkip() {
    var forced = flag("scrub");
    if (forced === "on") return false;
    if (forced === "off") return true;
    return prefersReducedMotion() || saveData();
  }

  function pickVariant() {
    return window.innerWidth <= MOBILE_MAX ? "mobile" : "desktop";
  }

  // GSAP is OPTIONAL. Without it this engine measures scroll itself and behaves
  // exactly as before. With it, ScrollTrigger becomes the single progress
  // source for the whole page, so the hero shares one scroll system with the
  // section choreography instead of running a second, independent mapping.
  //
  // What deliberately does NOT move into GSAP: frame loading, gap tolerance,
  // variant swapping and the reduced-motion/Save-Data stop. ScrollTrigger has
  // no opinion about preloading 179 images, and those behaviours are what the
  // spec's non-negotiables rest on.
  function hasGsap() {
    return !!(window.gsap && window.ScrollTrigger);
  }

  function bindScrollTrigger(s) {
    window.gsap.registerPlugin(window.ScrollTrigger);
    s.trigger = window.ScrollTrigger.create({
      trigger: s.el,
      start: "top top",
      end: "bottom bottom",
      scrub: true,
      // Progress ONLY. Visibility stays with the IntersectionObserver below:
      // ScrollTrigger is "active" only once the section's top reaches the
      // viewport top, so using it to gate painting leaves the loop stopped at
      // scroll 0 and the first frames never render.
      onUpdate: function (self) { s.target = clamp(self.progress, 0, 1); }
    });
  }

  /* ── construction ──────────────────────────────────────────────────────── */

  function parseWindow(attr, fallbackStart) {
    if (attr === null) return null;
    var parts = String(attr).trim().split(/[\s,]+/).filter(Boolean).map(function (raw) {
      var pct = raw.indexOf("%") !== -1;
      var n = parseFloat(raw);
      if (isNaN(n)) return null;
      return pct ? n / 100 : n;
    });
    if (!parts.length || parts[0] === null) return [fallbackStart, fallbackStart + LINE_RAMP];
    var a = parts[0];
    var b = parts.length > 1 && parts[1] !== null ? parts[1] : a + LINE_RAMP;
    return [a, b];
  }

  // data-beat="first last [ramp]" — 1-based FRAME numbers of the DESKTOP
  // sequence, matching the files on disk and the contact sheet the copy was
  // written against. A beat lands on a pose, not on a scroll fraction.
  //
  // The reference is deliberately the desktop count, never the live one. Mobile
  // covers the same clip in fewer frames (179 vs 89 on the reference build), so
  // frame 45 is a quarter of the way through on desktop and half way through on
  // mobile. Resolving against the live count would slide every beat onto a
  // different pose the moment the viewport crossed 768px. Converting once
  // against the desktop count yields a progress window that means the same
  // moment on every variant — which is the whole point of authoring in frames.
  function beatWindow(attr, count) {
    var n = String(attr).trim().split(/[\s,]+/).map(parseFloat)
      .filter(function (x) { return !isNaN(x); });
    if (n.length < 2 || count < 2) return null;
    var span = count - 1;
    var ramp = (n.length > 2 ? n[2] : Math.max(4, Math.round(count * 0.04))) / span;
    var a = clamp((n[0] - 1) / span, 0, 1);
    var b = clamp((n[1] - 1) / span, 0, 1);
    return { in0: a - ramp, in1: a, out0: b, out1: b + ramp };
  }

  // Must run once the counts are known. collectLines happens in buildSection,
  // where count is still 0, so frame ranges cannot be resolved there.
  function resolveWindows(s) {
    var counts = s.cfg.frameCount || {};
    var reference = counts.desktop || s.count || 0;
    for (var i = 0; i < s.lines.length; i++) {
      var l = s.lines[i];
      if (!l.beat) continue;
      var w = beatWindow(l.beat, reference);
      if (!w) continue;
      l.in0 = w.in0; l.in1 = w.in1; l.out0 = w.out0; l.out1 = w.out1;
    }
  }

  function collectLines(stage) {
    var nodes = stage.querySelectorAll("[data-in],[data-out],[data-beat]");
    return Array.prototype.map.call(nodes, function (el) {
      var inWin = parseWindow(el.getAttribute("data-in"), 0) || [0, 0];
      var outWin = parseWindow(el.getAttribute("data-out"), 1) || [1, 1];
      var riseAttr = el.getAttribute("data-rise");
      return {
        el: el,
        beat: el.getAttribute("data-beat"),   // resolved later, once count is known
        in0: inWin[0], in1: inWin[1],
        out0: outWin[0], out1: outWin[1],
        rise: riseAttr === null ? LINE_RISE : parseFloat(riseAttr) || 0,
        v: 1
      };
    });
  }

  function buildSection(cfg) {
    var el = document.querySelector(cfg.section);
    if (!el) return null;
    var stage = el.querySelector(".stage") || el;
    var canvas = stage.querySelector("canvas");
    if (!canvas) return null;

    return {
      cfg: cfg,
      el: el,
      stage: stage,
      canvas: canvas,
      ctx: canvas.getContext("2d", { alpha: false }),
      lines: collectLines(stage),
      variant: "desktop",
      count: 0,
      frames: [],
      loaded: 0,
      token: 0,
      loading: false,
      ready: false,
      visible: false,
      target: 0,
      eased: 0,
      paintedIndex: -1,
      dirty: true,
      needsSize: true,
      focusHold: false,
      pinned: null,
      trigger: null
    };
  }

  function frameCountFor(cfg, variant) {
    var counts = cfg.frameCount || {};
    return counts[variant] || counts.desktop || 0;
  }

  function resolvePath(s, index) {
    var number = index + 1; // ffmpeg's image2 muxer starts at frame_0001
    if (typeof s.cfg.framePath === "function") {
      return s.cfg.framePath(number, s.variant);
    }
    var padded = String(number);
    while (padded.length < 4) padded = "0" + padded;
    var fmt = s.cfg.format || "webp";
    return "frames/" + s.cfg.name + "/" + s.variant + "/frame_" + padded + "." + fmt;
  }

  /* ── drawing ───────────────────────────────────────────────────────────── */

  function sizeCanvas(s) {
    var rect = s.canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    var w = Math.max(1, Math.round(rect.width));
    var h = Math.max(1, Math.round(rect.height));
    s.canvas.width = Math.round(w * dpr);
    s.canvas.height = Math.round(h * dpr);
    s.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    s.cssW = w;
    s.cssH = h;
    s.needsSize = false;
    s.dirty = true; // resizing clears the backing store
  }

  // fit: "cover" (default) fills the stage and crops whichever axis is
  // relatively smaller — on a wide, short viewport that measured a 16% vertical
  // crop, taking the top and bottom off the subject.
  //
  // fit: "contain" never crops. It letterboxes instead, filling the surround
  // with the plate colour, which is invisible when the clip has a plain ground
  // (a studio cyclorama, a flat field) and is the right choice there. The fill
  // is required, not cosmetic: the context is alpha:false, so anything not
  // drawn stays opaque black.
  function drawFrame(s, img) {
    var cw = s.cssW, ch = s.cssH;
    var iw = img.naturalWidth || img.width;
    var ih = img.naturalHeight || img.height;
    if (!iw || !ih) return;
    var contain = s.cfg.fit === "contain";
    var scale = contain ? Math.min(cw / iw, ch / ih) : Math.max(cw / iw, ch / ih);
    var w = iw * scale, h = ih * scale;
    if (contain) {
      s.ctx.fillStyle = s.plate || "#000";
      s.ctx.fillRect(0, 0, cw, ch);
    }
    s.ctx.drawImage(img, (cw - w) / 2, (ch - h) / 2, w, h);
  }

  // A gap is survivable; a stall is not. Earlier frames win ties so a gap reads
  // as lag rather than a jump ahead.
  function nearestLoaded(s, index) {
    if (s.frames[index]) return index;
    for (var d = 1; d <= GAP_TOLERANCE; d++) {
      if (s.frames[index - d]) return index - d;
      if (s.frames[index + d]) return index + d;
    }
    return -1;
  }

  function paint(s, index) {
    var resolved = nearestLoaded(s, index);
    if (resolved < 0) return false;
    // Compare the *resolved* index: without this a 16-frame gap would redraw
    // identical pixels sixteen times.
    if (resolved === s.paintedIndex && !s.dirty) return true;
    drawFrame(s, s.frames[resolved]);
    s.paintedIndex = resolved;
    s.dirty = false;
    return true;
  }

  /* ── loading ───────────────────────────────────────────────────────────── */

  function loadFrame(s, index, token, lowPriority) {
    return new Promise(function (resolve) {
      if (token !== s.token || s.frames[index]) return resolve();
      var img = new Image();
      img.decoding = "async";
      if (lowPriority) img.fetchPriority = "low";
      img.onload = function () {
        if (token === s.token && !s.frames[index]) {
          s.frames[index] = img;
          s.loaded++;
          s.dirty = true;
        }
        resolve();
      };
      // Resolving rather than rejecting is the whole gap-tolerance contract.
      img.onerror = function () { resolve(); };
      img.src = resolvePath(s, index);
    });
  }

  function coarseIndices(s) {
    var list = [];
    for (var i = 0; i < s.count; i += COARSE_STRIDE) list.push(i);
    if (list[list.length - 1] !== s.count - 1) list.push(s.count - 1);
    return list;
  }

  function runCoarsePass(s, token) {
    return Promise.all(coarseIndices(s).map(function (i) {
      return loadFrame(s, i, token, false);
    }));
  }

  function nextMissing(s, pivot, howMany) {
    var out = [];
    for (var d = 0; d < s.count && out.length < howMany; d++) {
      var a = pivot + d, b = pivot - d;
      if (a < s.count && !s.frames[a] && out.indexOf(a) === -1) out.push(a);
      if (out.length >= howMany) break;
      if (d && b >= 0 && !s.frames[b] && out.indexOf(b) === -1) out.push(b);
    }
    return out;
  }

  async function runFillPass(s, token) {
    while (token === s.token && s.loaded < s.count) {
      // Recomputed per chunk, so on a slow link the gap under the visitor's
      // current position closes first.
      var pivot = Math.round(s.eased * (s.count - 1));
      var batch = nextMissing(s, pivot, FILL_CHUNK);
      if (!batch.length) break;
      await Promise.all(batch.map(function (i) { return loadFrame(s, i, token, true); }));
      await yieldToLoop();
    }
  }

  function revealCanvas(s) {
    // Paint before fading in: an alpha:false context is opaque black until
    // something is drawn, so revealing first flashes black over the poster.
    var index = Math.round(s.eased * (s.count - 1));
    if (!paint(s, index)) return false;
    s.ready = true;
    s.el.classList.add("is-live");
    return true;
  }

  async function loadSection(s) {
    if (s.loading || s.ready) return;
    s.loading = true;
    var token = s.token;

    await Promise.race([runCoarsePass(s, token), after(COARSE_BUDGET_MS)]);
    if (token !== s.token) { s.loading = false; return; }

    if (s.loaded === 0) {
      // Frames missing or the deploy is broken. Stay on the poster: that page
      // is still complete and correct.
      if (window.console) console.warn("[scrub] no frames loaded for " + s.cfg.section);
      s.loading = false;
      return;
    }

    if (s.needsSize) sizeCanvas(s);
    revealCanvas(s);
    await runFillPass(s, token);
    s.loading = false;
  }

  /* ── scroll mapping ────────────────────────────────────────────────────── */

  function measure(s) {
    var rect = s.el.getBoundingClientRect();
    return clamp(-rect.top / Math.max(rect.height - window.innerHeight, 1), 0, 1);
  }

  function updateLines(s, p) {
    for (var i = 0; i < s.lines.length; i++) {
      var l = s.lines[i];
      var v = smoothstep(l.in0, l.in1, p) * (1 - smoothstep(l.out0, l.out1, p));
      if (s.focusHold) v = 1;
      if (Math.abs(v - l.v) < 0.002) continue;
      l.v = v;
      l.el.style.opacity = v;
      l.el.style.transform = l.rise ? "translate3d(0," + ((1 - v) * l.rise) + "px,0)" : "";
      // Stop an invisible CTA from eating clicks.
      l.el.style.pointerEvents = v < 0.02 ? "none" : "";
    }
  }

  /* ── loop ──────────────────────────────────────────────────────────────── */

  var sections = [];
  var rafId = 0;

  function tick() {
    rafId = requestAnimationFrame(tick);
    var live = 0;
    var reduce = prefersReducedMotion();

    // Read pass first: batching getBoundingClientRect away from style writes
    // keeps this off the layout-thrash path.
    // With GSAP present, ScrollTrigger has already written s.target in its own
    // onUpdate, so measuring again here would fight it.
    if (!hasGsap()) {
      for (var i = 0; i < sections.length; i++) {
        if (sections[i].visible) sections[i].target = measure(sections[i]);
      }
    }
    for (var j = 0; j < sections.length; j++) {
      var s = sections[j];
      if (!s.visible) continue;
      live++;
      if (s.pinned !== null) s.eased = s.target = s.pinned;
      else if (reduce) s.eased = s.target;
      else {
        s.eased = lerp(s.eased, s.target, LERP);
        if (Math.abs(s.target - s.eased) < SETTLE) s.eased = s.target;
      }
      if (s.needsSize) sizeCanvas(s);
      if (s.ready) paint(s, Math.round(s.eased * (s.count - 1)));
      updateLines(s, s.eased);
    }

    if (!live) {
      cancelAnimationFrame(rafId);
      rafId = 0;
    }
  }

  function startLoop() {
    if (!rafId) rafId = requestAnimationFrame(tick);
  }

  /* ── resize ────────────────────────────────────────────────────────────── */

  function swapVariant(s, variant) {
    s.token++;               // abandons every in-flight load for the old variant
    s.variant = variant;
    s.count = frameCountFor(s.cfg, variant);
    resolveWindows(s);   // frame ranges are relative to THIS variant's count
    s.frames = new Array(s.count);
    s.loaded = 0;
    s.ready = false;
    s.loading = false;
    s.paintedIndex = -1;
    s.dirty = true;
    // Fade back to the poster while the new pass loads, rather than flashing.
    s.el.classList.remove("is-live");
    loadSection(s);
  }

  var onResize = debounce(function () {
    var variant = pickVariant();
    for (var i = 0; i < sections.length; i++) {
      var s = sections[i];
      s.needsSize = true;
      if (s.variant !== variant && frameCountFor(s.cfg, variant)) swapVariant(s, variant);
    }
  }, RESIZE_DEBOUNCE);

  /* ── boot ──────────────────────────────────────────────────────────────── */

  function armWhenIdle(s) {
    // The hero is already intersecting at boot, so a "100% 0px" observer fires
    // immediately and 10-15 MB of frames would race the LCP poster. Frames
    // requested after load cannot affect LCP by definition.
    if (document.readyState === "complete") return loadSection(s);
    Promise.race([
      new Promise(function (r) { window.addEventListener("load", r, { once: true }); }),
      after(LOAD_GATE_MS)
    ]).then(function () { loadSection(s); });
  }

  function boot() {
    var configs = window.SCRUB_SECTIONS;
    if (!configs || !configs.length) return;

    sections = [];
    for (var i = 0; i < configs.length; i++) {
      var s = buildSection(configs[i]);
      if (!s) {
        if (window.console) console.warn("[scrub] no such section: " + configs[i].section);
        continue;
      }
      sections.push(s);
    }
    if (!sections.length) return;

    if (shouldSkip()) return; // poster and every word of copy remain

    var pinned = parseFloat(flag("frame"));
    var hasPin = !isNaN(pinned);

    sections.forEach(function (s) {
      s.variant = pickVariant();
      s.count = frameCountFor(s.cfg, s.variant);
      resolveWindows(s);
      s.frames = new Array(s.count);
      if (s.cfg.bg) s.stage.style.backgroundColor = s.cfg.bg;
      // Letterbox fill for fit:"contain". Falls back to the stage's computed
      // background, so it matches the brand layer without being restated.
      s.plate = s.cfg.bg || getComputedStyle(s.stage).backgroundColor || "#000";
      if (hasPin) s.pinned = clamp(pinned, 0, 1);
      // Enables the optional overlapping-copy layout, independently of whether
      // any frame ever arrives — so the page looks intentional while loading.
      s.el.classList.add("is-scrubbed");

      if (hasGsap()) bindScrollTrigger(s);

      if (window.ResizeObserver) {
        new ResizeObserver(function () { s.needsSize = true; }).observe(s.canvas);
      }
      s.stage.addEventListener("focusin", function () { s.focusHold = true; });
      s.stage.addEventListener("focusout", function () { s.focusHold = false; });
    });

    if (!("IntersectionObserver" in window)) {
      sections.forEach(function (s) { s.visible = true; armWhenIdle(s); });
      startLoop();
    } else {
      var loadObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var s = sections.filter(function (x) { return x.el === entry.target; })[0];
          if (s) { loadObserver.unobserve(entry.target); armWhenIdle(s); }
        });
      }, { rootMargin: LOAD_MARGIN });

      var paintObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          var s = sections.filter(function (x) { return x.el === entry.target; })[0];
          if (!s) return;
          s.visible = entry.isIntersecting;
          if (s.visible) startLoop();
        });
      }, { rootMargin: PAINT_MARGIN });

      sections.forEach(function (s) {
        loadObserver.observe(s.el);
        paintObserver.observe(s.el);
      });
    }

    window.addEventListener("resize", onResize, { passive: true });
    window.addEventListener("orientationchange", onResize, { passive: true });
    window.addEventListener("pagehide", function () {
      // Decoded bitmaps are the reason a long sequence kills a mobile tab.
      sections.forEach(function (s) { s.token++; s.frames = []; s.loaded = 0; });
    });

    // Something truthful for an automated check to assert, instead of eyeballing
    // pixels in a screenshot that may legitimately be blank.
    window.__scrub = {
      sections: sections,
      boot: boot,
      // Resolved beat windows, so an automated check can prove no dead
      // stretch — the classic failure of this pattern is a hero that goes
      // blank for two seconds in the middle.
      windows: function (i) {
        return (sections[i || 0] || { lines: [] }).lines.map(function (l) {
          return { beat: l.beat, in0: l.in0, in1: l.in1, out0: l.out0, out1: l.out1 };
        });
      }
    };
  }

  if (reduceQuery && reduceQuery.addEventListener) {
    reduceQuery.addEventListener("change", function () {
      if (!prefersReducedMotion() && !sections.length) boot();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
