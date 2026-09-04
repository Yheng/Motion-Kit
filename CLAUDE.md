# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status: unimplemented

The repo contains only `LICENSE` and `BUILD-SPEC.md`. **`BUILD-SPEC.md` is the contract** — it
specifies the whole tool (CLI, provider configs, page template, scrub engine, skill) down to ffmpeg
flags and API call shapes, and is the only source of truth for anything not yet on disk. Read it in
full before writing code.

Build in the order given in §13. The `--placeholder` dry-run path (step 3) must work with **no API
keys at all** before any provider adapter is written. First shakedown project per §14 is a mecha
samurai — run it end to end and fix what breaks rather than polishing ahead of evidence.

## Commands

One entry point; invocation is identical in PowerShell, Terminal and bash:

```
python motionkit.py doctor                                          # platform/python/ffmpeg/keys — always step one
python motionkit.py init <project> [--provider fal|gemini|byo]
python motionkit.py frames --project <p> --name hero --placeholder  # $0, no clip, no API call
python motionkit.py frames --project <p> --name hero --clip build/hero.mp4
python motionkit.py serve --project <p>                             # python -m http.server inside site/
python motionkit.py cost --project <p>                              # itemised spend log
```

Paid commands are `image`, `video`, `cutout`. Full flag table in §7.

**On this machine:** use `python` (3.14.7) — `python3` resolves to the Microsoft Store shim and
fails. ffmpeg 9.0.1 is already on PATH, so a normal local run never exercises the `.ffmpeg/`
auto-download in §3; test that path by shadowing PATH deliberately.

**There is no test suite, linter or build step, and none is specified.** Verification is the manual
loop in §13: `doctor` on a clean checkout → `init` → `--placeholder` frames → a real slice from any
mp4 → `serve` and look at it in a real browser. Headless screenshots blank sticky canvas sections
when scrolled and make a working page look broken.

## Architecture

**The effect is a canvas image-sequence scrub — not video, not Three.js.** A clip is exported to
numbered frames, preloaded, and scroll position picks which frame `drawImage` paints. The "3D" is
camera movement in the source clip, so every upstream stage exists to produce one good continuous
camera move (§1).

Two halves that must stay separate:

- **`motionkit.py`** — single stdlib-only file; the only thing that spends money or touches ffmpeg.
- **`.claude/skills/scroll-cinematic/SKILL.md`** — an eight-phase consultation (intake → SEO →
  three directions → architecture → copy → still → motion → frames/QA, gated at directions, copy and
  the hero still) that *drives the CLI*. It must never call an API or ffmpeg directly, because the
  CLI is what records spend and state (§10).

### Money is the architecture

`out/<project>/` splits into `build/` (source stills and mp4s, gitignored but **kept**) and `site/`
(self-contained, deployable as-is). Re-slicing frames out of `build/` is free and unlimited; only a
scene or composition change justifies a new render, and copy, palette, layout and type changes must
never touch `build/`. This split is the single biggest cost saver in the design — preserve it.

`state.json` (`{phase, approvals, spend, assets}`) is what makes resume work after a crash, context
limit or closed terminal; without it an interrupted build re-renders and re-charges. Every paid call
prints its estimate *before* running and appends to the spend log after.

### Providers are data, not code

`providers/*.json` carry endpoint IDs, auth scheme, a field-name map, limits and pricing. **Model IDs
must never be hardcoded in Python** — they drift (Google retired Veo 3 and Veo 2 on 30 June 2026). On
an unknown-endpoint error, surface it plainly and point at the provider's catalogue instead of
guessing a replacement. Three shapes to keep straight (§6):

- **fal** — `queue.fal.run`, `Authorization: Key …`, poll `status_url` then GET `response_url`. Local
  images inline as data URIs rather than uploading. Field names differ per model (Seedance
  `image_url`, Kling `start_image_url`) — hence the `fields` map.
- **gemini** — long-running *operations*, not a queue; 8s clip cap (clamp and warn); no cutout model,
  so text-behind requires fal. The spec marks its call shapes and pricing unverified: validate
  against live docs on first real run.
- **byo** — no key, no generation, everything downstream free. A first-class path, not a fallback.

Extract asset URLs by **recursive search** for the first `http` `url`/`uri` in the response, never a
fixed JSON path — response shapes vary by model and change over time.

### The CLI → engine seam

`frames` writes `site/frames/<name>/{desktop,mobile}/frame_%04d.<fmt>` and prints a paste-ready
`SCRUB_SECTIONS` snippet. **Print the count actually written, not the count requested** — ffmpeg's
`fps` filter rounds, and a wrong count in the config makes the engine fetch frames that don't exist.
That snippet is the entire interface between the two halves (§7, §8).

### The page contract

One z-index band inside `.stage`: nav 40 / cutout 30 / overlay 20 / canvas 10 / poster 1, declared
once, no escalation war. The poster `<img>` is the LCP element and exists before any JS runs. The
canvas is `aria-hidden` and decorative — **all meaning lives in real DOM copy**, and no headline,
value proposition or keyword may live inside a generated image, because canvas content is invisible
to crawlers. Text-behind-subject is a three-layer sandwich: a BiRefNet cutout PNG sits above the
overlay text while the full frame scrubs behind it, so the type stays selectable, translatable and
indexable (§9).

The engine loads lazily one viewport ahead and skips frames entirely under `prefers-reduced-motion`
or Save-Data: **the page must be complete and sellable with zero frames loaded** — verify that
explicitly. `styles.css` is structural scaffolding only — no palette, no typefaces; the brand layer
is written per project.

## Conventions

- Python 3.9+, **standard library only** — no pip, no npm, no framework, no build step. The output is
  a static folder.
- Cross-platform, Windows first. No bash scripts; one Python CLI shelling out to ffmpeg.
- Gated interactive flow by default, `--auto` as opt-in. Ask in numbered choices ("Reply 1, 2 or 3")
  with a free-text escape, never open-ended questions a beginner can't answer.
- Licensed **MIT** (`LICENSE`), matching spec §3 and §4. Public GitHub repo.
- Mark every invented claim — brand names, statistics, testimonials, client counts — in `{{...}}` and
  list them, so nothing fictional reaches production unnoticed.

## Caveats

- Pricing in `providers/*.json` goes stale. Keep the `note` fields pointing at canonical pricing
  pages and repeat the warning in the README.
- The Gemini adapter's call shapes, model IDs and rates are unverified in the spec — validate against
  live docs on first real run and keep it marked as such.
