/*
 * motion-kit — optional GSAP page choreography.
 *
 * The canvas scrub in scrub.js handles the hero. This file handles everything
 * BELOW it: reveals, parallax, pinned spreads, counting numerals. That is the
 * part that was previously static, and it is where GSAP earns its place.
 *
 * OPTIONAL BY DESIGN. If gsap and ScrollTrigger are not on the page this file
 * does nothing and every element stays exactly as CSS left it. Load them from a
 * CDN before this script — there is still no build step and nothing to install.
 *
 * THE INVARIANT IS THE SAME ONE THE SCRUB ENGINE KEEPS: css shows, js hides.
 * Everything here is visible by default in styles.css. This file only ever
 * animates FROM a hidden state that it sets itself, at runtime, after
 * confirming it is going to run. So a CDN failure, a blocked script or
 * reduced-motion all leave a complete, readable page rather than a blank one.
 *
 * Opt in with attributes:
 *   data-reveal              fade and rise on entry
 *   data-reveal="stagger"    stagger the element's children instead
 *   data-parallax="0.2"      drift at a different rate to the scroll
 *   data-count               count a numeral up to its text value
 *   data-pin                 hold the section while its contents play
 */
(function () {
  "use strict";

  var RISE = 28;
  var DURATION = 0.9;
  var STAGGER = 0.09;

  function reducedMotion() {
    return !!(window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn, { once: true });
    } else { fn(); }
  }

  ready(function () {
    // Two hard stops, both leaving the page complete.
    if (!window.gsap || !window.ScrollTrigger) return;
    if (reducedMotion()) return;

    var gsap = window.gsap;
    gsap.registerPlugin(window.ScrollTrigger);

    // ── reveals ─────────────────────────────────────────────────────────────
    // gsap.from() sets the hidden state itself and always lands on the CSS
    // value, so an interrupted animation cannot strand an element invisible.
    gsap.utils.toArray("[data-reveal]").forEach(function (el) {
      var stagger = el.getAttribute("data-reveal") === "stagger";
      var targets = stagger ? el.children : el;
      gsap.from(targets, {
        opacity: 0,
        y: RISE,
        duration: DURATION,
        ease: "power3.out",
        stagger: stagger ? STAGGER : 0,
        scrollTrigger: { trigger: el, start: "top 82%", once: true }
      });
    });

    // ── parallax ────────────────────────────────────────────────────────────
    gsap.utils.toArray("[data-parallax]").forEach(function (el) {
      var rate = parseFloat(el.getAttribute("data-parallax")) || 0.2;
      gsap.to(el, {
        yPercent: rate * -100,
        ease: "none",
        scrollTrigger: {
          trigger: el.closest("section") || el,
          start: "top bottom",
          end: "bottom top",
          scrub: true
        }
      });
    });

    // ── counting numerals ───────────────────────────────────────────────────
    // The real value stays in the DOM as text, so a visitor who never triggers
    // the animation still reads the number.
    gsap.utils.toArray("[data-count]").forEach(function (el) {
      var target = parseFloat(String(el.textContent).replace(/[^0-9.]/g, ""));
      if (isNaN(target)) return;
      var suffix = String(el.textContent).replace(/[0-9.,]/g, "");
      var box = { v: 0 };
      gsap.to(box, {
        v: target,
        duration: 1.4,
        ease: "power2.out",
        scrollTrigger: { trigger: el, start: "top 88%", once: true },
        onUpdate: function () {
          el.textContent = Math.round(box.v) + suffix;
        },
        onComplete: function () { el.textContent = target + suffix; }
      });
    });

    // ── pinned spreads ──────────────────────────────────────────────────────
    gsap.utils.toArray("[data-pin]").forEach(function (el) {
      window.ScrollTrigger.create({
        trigger: el,
        start: "top top",
        end: "+=" + (el.offsetHeight * 0.8),
        pin: true,
        pinSpacing: true
      });
    });

    // The scrub engine reveals its canvas asynchronously and the bands below
    // shift as images decode, so positions computed at boot go stale.
    window.addEventListener("load", function () { window.ScrollTrigger.refresh(); });
  });
})();
