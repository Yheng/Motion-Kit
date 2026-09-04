# motion-kit — build specification

A CLI + Claude Code skill that builds scroll-driven cinematic landing pages.
This document is the complete design. Implement it as specified; where it is
silent, use judgement and say what you chose.

**Read this whole file before writing any code.**

---

## 1. What the thing does

The viral "3D scroll" page (Apple product pages, Awwwards sites) is **not**
Three.js and **not** a `<video>` tag. It is a **canvas image-sequence scrub**: a
short clip is exported to numbered frames, preloaded, and the frame painted to a
`<canvas>` is chosen by scroll position. Scroll down, the clip plays; scroll up,
it plays backward.

Frames beat video because video scrubbing stutters on mobile Safari, seeks are not
frame-accurate, and reverse playback is unreliable. Frames are just `drawImage`.

The "3D" comes entirely from camera movement in the source clip. Everything
upstream exists to get one good continuous camera move.

The tool has two halves:

1. **A CLI** (`motionkit.py`) that talks to image/video providers, slices clips
   into frames with ffmpeg, and tracks project state and spend.
2. **A Claude Code skill** that runs an interactive consultation — positioning,
   art direction, architecture, SEO copy — and drives the CLI.

---

## 2. Scope

**In v1:** fal.ai + Google Gemini + bring-your-own-footage. Single landing page.
Gated interactive flow with `--auto` opt-in. Fresh art direction generation.
Text-behind-subject treatment. Cost tally. State and resume. Dry run. Seam
handling. Frame-count tuning. Inactive form and analytics slots.

**Explicitly deferred — do not build:** Replicate/ModelArk adapters (design the
provider abstraction so they slot in, but ship only the three above). Subpages,
multi-page sitemaps, routing. Animated per-frame mattes.

**Non-goals:** no build step, no npm, no framework. Output is a static folder.

---

## 3. Runtime constraints

- **Cross-platform: Windows, macOS, Linux.** The user is primarily on Windows.
  No bash scripts. One Python CLI calling ffmpeg via `subprocess`.
- **Python 3.9+, standard library only.** No pip installs. `urllib`, `json`,
  `pathlib`, `zipfile`, `tarfile`, `subprocess`, `argparse`.
- **ffmpeg auto-installed** if absent — no Homebrew, no sudo, no package manager.
  Download a static build into `.ffmpeg/` (gitignored):
  - Windows → `https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip`
  - macOS → `https://evermeet.cx/ffmpeg/getrelease/zip` and the matching
    `ffprobe` archive (two separate downloads)
  - Linux x86_64 / aarch64 → `https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{amd64,arm64}-static.tar.xz`
  Extract only `ffmpeg`/`ffprobe` by basename, ignoring archive directory layout.
  Append `.exe` on Windows. `chmod 0o755` elsewhere.
- Public GitHub repo, MIT licensed.

---

## 4. Repository layout

```
motion-kit/
├── README.md
├── LICENSE                       MIT
├── .gitignore
├── .env.example                  committed
├── .env                          gitignored, copied from example on first run
├── motionkit.py                  the entire CLI
├── providers/
│   ├── fal.json
│   ├── gemini.json
│   └── byo.json
├── kit/                          pristine templates, never edited per project
│   ├── index.html
│   ├── styles.css
│   └── scrub.js
├── .claude/skills/scroll-cinematic/SKILL.md
└── out/
    └── <project>/
        ├── brief.md              written by Claude during intake
        ├── project.json          provider + model choices
        ├── state.json            phase, approvals, assets, spend log
        ├── build/                stills + mp4s — gitignored, KEPT for re-slicing
        └── site/                 the deliverable, self-contained, deployable
            ├── index.html styles.css scrub.js
            ├── poster/ cutout/
            └── frames/<name>/{desktop,mobile}/
```

`.gitignore`: `.env`, `.ffmpeg/`, `out/*/build/`, `out/*/site/frames/`,
`out/*/site/poster/`, `out/*/site/cutout/`, `__pycache__/`, `*.pyc`, `.DS_Store`.

Rationale for the `build/` vs `site/` split: `build/` holds source mp4s so frames
can be re-sliced at different counts, widths and formats **without paying again**.
`site/` is self-contained and deployable as-is.

---

## 5. `.env.example`

Pre-formatted with comment blocks explaining each provider, so the user opens it
and fills in a blank. Variable names must match each provider's own convention.

```bash
# ─── motion-kit ────────────────────────────────────────────────────────────
# Add at least one provider key below, save, then run: python motionkit.py doctor
# This file is gitignored. Never commit it, never paste a key into a chat.

# fal.ai — default provider.
# Nano Banana Pro stills, Seedance 2.0 Fast motion, BiRefNet cutouts.
# Roughly $3 per page at 720p. https://fal.ai/dashboard/keys
FAL_KEY=

# Google Gemini — fallback on a separate bill, for when fal credits run out.
# Veo caps clips at 8s and has no cutout model, so text-behind needs fal.
# Veo has no free tier; use the fast/lite tiers for cost parity.
# https://aistudio.google.com/apikey
GEMINI_API_KEY=

# ─── No key at all? ────────────────────────────────────────────────────────
# init with --provider byo, drop your own stills and mp4s into
# out/<project>/build/, and everything downstream works for free.
```

The CLI loads `.env` into `os.environ` **without overwriting** real environment
variables, and copies `.env.example` → `.env` on first run if missing.

---

## 6. Provider abstraction

The pipeline only needs three operations: *text → still*, *still + motion prompt →
mp4*, and *still → cutout with alpha*. A provider is therefore a JSON config:
endpoint IDs, auth scheme, field-name map, limits, pricing.

**Model IDs must never be hardcoded in Python.** They drift — Google retired Veo 3
and Veo 2 on 30 June 2026, and this will keep happening. On an unknown-endpoint
error, surface it plainly and point at the provider's catalogue rather than
guessing a replacement.

### `providers/fal.json`

```json
{
  "name": "fal.ai", "kind": "fal",
  "key_env": "FAL_KEY", "key_url": "https://fal.ai/dashboard/keys",
  "models": {
    "image": "fal-ai/nano-banana-pro",
    "image_cheap": "fal-ai/nano-banana-2",
    "video": "bytedance/seedance-2.0/fast/image-to-video",
    "video_quality": "bytedance/seedance-2.0/image-to-video",
    "video_cheap": "fal-ai/bytedance/seedance/v1/lite/image-to-video",
    "cutout": "fal-ai/birefnet/v2"
  },
  "fields": { "start_image": "image_url", "end_image": "end_image_url" },
  "limits": { "max_seconds": 15 },
  "pricing": {
    "image_usd": 0.15,
    "video_usd_per_second": 0.2419,
    "cutout_usd": 0.02,
    "note": "Seedance 2.0 Fast at 720p. Standard tier ~$0.3024/s. Verify at https://fal.ai/pricing"
  }
}
```

**fal call shape (verified against docs):** POST to `https://queue.fal.run/{model}`
with header `Authorization: Key {FAL_KEY}`. The response contains `request_id`,
`status_url` and `response_url`. Poll `status_url` until `status == "COMPLETED"`,
then GET `response_url`. Terminal failures: `FAILED`, `ERROR`, `CANCELLED`.
Poll every ~6s with a ~40 min ceiling. Seedance takes `image_url`; **Kling takes
`start_image_url`** — hence the `fields` map.

### `providers/gemini.json`

```json
{
  "name": "Google Gemini", "kind": "gemini",
  "key_env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey",
  "models": {
    "image": "gemini-3-pro-image-preview",
    "video": "veo-3.1-fast-generate-preview",
    "cutout": null
  },
  "limits": { "max_seconds": 8 },
  "pricing": {
    "image_usd": 0.134, "video_usd_per_second": 0.15, "cutout_usd": 0.0,
    "note": "UNVERIFIED. Model IDs and rates drift; published Veo 3.1 figures range $0.15–$0.75/s depending on tier, resolution, audio, and Vertex vs Developer API. Check https://ai.google.dev/gemini-api/docs/pricing and .../docs/models before budgeting."
  }
}
```

**Gemini call shape — treat as unverified and validate on first real run:**
- Image: POST `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key=…`
  with `generationConfig.responseModalities: ["IMAGE"]`. Result carries base64 in
  an `inlineData` part.
- Video: POST `…/models/{model}:predictLongRunning?key=…` with the start image as
  `bytesBase64Encoded`. Returns a long-running **operation** (not fal's queue).
  Poll `…/v1beta/{operation.name}?key=…` until `done`. The result URI needs the
  API key appended as a query param to download.
- **8-second cap** — clamp and warn rather than failing.
- **No cutout model.** Text-behind directions require fal.
- Verify at build time whether Veo 3.1 exposes a last-frame parameter. If not,
  `--loop` is unsupported there and seam handling falls back to `--trim-end`.
- Also worth confirming: SynthID watermarking on output. Invisible, but flag it in
  the README as a client-work consideration.

### `providers/byo.json`

`"generation": false`, no key. Image/video/cutout commands exit with a clear
message telling the user to place files in `build/` themselves. Everything
downstream — frames, assembly, serve — works normally at zero cost.

Any clip with continuous camera motion works: Blender/C4D turntables, drone
footage, existing client product video, After Effects renders. **This is a
first-class path, not a fallback.** An agency with real footage gets a better page
than any generative model produces, and it makes the tool independent of any
provider's pricing or availability.

---

## 7. The CLI

`python motionkit.py <command>` — identical invocation in PowerShell, Terminal
and bash.

| Command | Purpose |
| --- | --- |
| `doctor` | platform, Python, ffmpeg, per-provider key status, existing projects |
| `init <project> [--provider] [--force]` | scaffold `out/<project>/`, copy `kit/` → `site/`, seed `brief.md`, `project.json`, `state.json` |
| `image --project --prompt --out [--model --aspect --resolution]` | generate a still into `build/` |
| `video --project --prompt --image --out [--duration --resolution --aspect --loop]` | animate a still into `build/` |
| `cutout --project --image --out` | background removal → PNG with alpha |
| `frames --project --name [--clip] [--count --width --mobile-width --format --quality --trim-end --desktop-only --placeholder]` | slice to frames |
| `serve --project [--port]` | `python -m http.server` inside `site/` |
| `cost --project` | itemised spend log and total |

### Behaviour requirements

- **Every paid call prints its estimate before running**, and appends to
  `state.json.spend` with timestamp, kind, model, USD and a note. After each call,
  print the per-call cost and the running project total.
- `state.json` shape: `{ phase, approvals: {}, spend: [], assets: {} }`.
  This is what makes resume work after a crash, a context limit, or a closed
  terminal — without it, an interrupted build re-renders and re-charges.
- Local images are inlined as **data URIs** for fal rather than uploaded, which
  avoids a separate upload step and storage lifecycle.
- Asset-URL extraction should be a **recursive search** through the JSON response
  for the first `url`/`uri` starting with `http`, not a fixed path. Response
  shapes vary across models and change over time.
- `--loop` passes the start image as the end frame (`end_image_url`) so the
  sequence returns to its first frame and scrubs seamlessly.
- Clamp `--duration` to the provider's `limits.max_seconds` with a warning.
- Failed and moderation-rejected jobs are not billed — say so when reporting one.

### Frame slicing (verified working)

Read duration with `ffprobe -v error -show_entries format=duration -of csv=p=0`.
Then `fps = count / (duration - trim_end)` and one ffmpeg pass:

```
-vf "fps={fps:.6f},scale={width}:-2:flags=lanczos" -an
```

Encoder flags by format:
- `webp` (default) → `-c:v libwebp -quality {q} -compression_level 6`
- `jpg` → `-q:v {2..31, lower is better}`
- `avif` → `-c:v libaom-av1 -crf … -cpu-used 6` (works, but slow — opt-in only)

Write two variants per section unless `--desktop-only`:
- desktop: `--count` (default 180) at `--width` (default 1600)
- mobile: `max(count // 2, 40)` at `--mobile-width` (default 900)

Output `site/frames/<name>/<variant>/frame_%04d.<fmt>`. **Print the actual written
file count, not the requested count** — ffmpeg's fps filter rounds — and emit a
paste-ready `SCRUB_SECTIONS` snippet using those real numbers.

Measured sanity check: 180 frames at 1600px lands around 10–15 MB per section as
JPEG, less as WebP. Treat that as the ceiling before first paint drags.

`--placeholder` generates free frames with no clip and no API call, using
`-f lavfi -i testsrc2=…`. This is the dry-run path: the entire page — direction,
copy, layout, engine, mobile, SEO — can be exercised end to end for $0.

---

## 8. The scrub engine (`kit/scrub.js`)

Vanilla JS, no dependencies, no build step. Config in `index.html`:

```js
window.SCRUB_SECTIONS = [{
  section: "#hero",
  frameCount: { desktop: 180, mobile: 90 },
  format: "webp",
  bg: "#07070a",
  framePath: (i, v) => `frames/hero/${v}/frame_${String(i).padStart(4,"0")}.webp`,
}];
```

Requirements:

- **Poster-first.** A real `<img>` is the LCP element, preloaded with
  `fetchpriority="high"`, present before any JS runs. The canvas is `opacity: 0`
  and fades in over the poster only once frames are ready.
- **Lazy load.** `IntersectionObserver` with `rootMargin: "100% 0px"` starts
  loading one viewport ahead. Never at page load, and never for a section the
  visitor may not reach.
- **Two-pass loading.** Coarse pass first (every 8th frame plus the last) so
  scrubbing works early, then fill in the gaps, yielding to the event loop
  periodically so it never competes with interaction.
- **Gap tolerance.** If a frame isn't loaded yet, paint the nearest loaded frame
  within ±16. `img.onerror` must resolve, not reject — a gap is survivable, a
  stall is not.
- **Skip entirely** when `prefers-reduced-motion` or `navigator.connection.saveData`.
  Poster and all copy remain. **The page must be complete and sellable with zero
  frames loaded** — verify this explicitly.
- **Responsive variants.** Pick `mobile` under 768px. On a debounced resize that
  crosses the breakpoint, swap variant and reload.
- **Drawing:** `getContext("2d", { alpha: false })` for the plate, cover-fit,
  HiDPI capped at `devicePixelRatio` 2, repaint only when the frame index changes.
- **Scroll mapping:** `progress = clamp(-rect.top / (rect.height - innerHeight), 0, 1)`
  inside a rAF loop. Lerp toward the target at ~0.12 so trackpad flicks glide
  instead of snapping. No lerp under reduced motion.
- **Overlay copy** fades over per-line `data-in`/`data-out` scroll windows.

---

## 9. Page template (`kit/index.html`, `kit/styles.css`)

This is a **landing page**, not a demo. It ships with nav + logo, a CTA ladder
(nav, mid-page, end), content bands, and a footer.

### Layer order inside `.stage`

```
z 40  nav (sticky, above everything)
z 30  cutout   — optional PNG with alpha, ABOVE the text
z 20  overlay  — real DOM headings, indexable and selectable
z 10  canvas   — the scrubbed sequence
z  1  poster   — LCP element
```

One z-index band, declared once, no escalation war.

### Text-behind-subject treatment

The three-layer sandwich is what makes headlines appear to pass behind the
subject. The subject is separated from its background by a segmentation pass
(BiRefNet on fal, ~$0.02), kept as a PNG with alpha, and layered above the
headline while the full frame scrubs behind it.

Critical property: **the text stays real DOM the whole time** — selectable,
translatable, indexable. Transparency rules out JPEG, so cutout layers must be
PNG/WebP/AVIF.

v1 ships the **static sandwich**: one fixed cutout over a moving background. The
subject holds position while the plate behind it scrubs — light sweeping,
particles drifting, environment moving. Animated per-frame mattes are deferred:
they need 180 segmentation passes, suffer temporal flicker, and produce much
heavier files.

Delete the `.cutout` element when a direction doesn't use it.

### Also required

- Skip link. Semantic landmarks. `aria-hidden` on the canvas (decorative — all
  meaning lives in the DOM copy). Visible focus. Intact keyboard order.
- Explicit `width`/`height` on every image so the sticky stage cannot shift layout.
- Head: title, meta description, canonical, OG/Twitter cards, JSON-LD.
- **Form slot, commented out and inactive.** Static pages need a third-party
  endpoint (Formspree, Netlify Forms, Basin); ship the markup with a note rather
  than a dead button.
- **Analytics slot, commented out and inactive.** Nothing loads unless the user
  adds a snippet.
- Reduced-motion CSS unsticks the stage entirely and shows all copy.
- `styles.css` is **structural scaffolding only** — no palette, no typeface
  choices. It must say so in a header comment. The brand layer is written per
  project.

---

## 10. The skill (`.claude/skills/scroll-cinematic/SKILL.md`)

Standard skill front matter (`name`, `description`). The description must trigger
on: 3D scroll site, scroll animation, motion/cinematic landing page, animated
hero, Apple-style or Awwwards-style page, and vague asks like *"make me an amazing
landing page with [subject] and animation"*.

### Ground rules

- **Gates by default**, `--auto` as an opt-in flag. Under `--auto`, state each
  choice and its reasoning, then continue without stopping.
- **Never spend without printing the estimate first.**
- **Ask in numbered choices** — "Reply 1, 2 or 3" — always with a free-text
  escape. No tappable UI: this runs in Claude Code, CLI or desktop. Never ask
  open-ended questions a beginner can't answer ("what palette do you want?").
- **Always drive the CLI**, never call ffmpeg or an API directly. The CLI is what
  records spend and state.
- **Resume before starting.** If `state.json` exists, read it and `brief.md`, tell
  the user which phase they stopped at, and continue there.

### The eight phases

**Phase 1 — Intake.** `init` the project. If `brief.md` has content, read it and
ask only about gaps. Otherwise ask **two or three** numbered-choice questions:
what this is, who it's for, what a visitor should do, plus commercial-use and
likeness constraints. Then **write `brief.md` from the conversation**.

> `brief.md` is an **output** of intake and an **optional input** on resume. Never
> a prerequisite — conversation is the primary path.

**Phase 2 — Positioning and SEO.** Before any visual thinking, because search
intent reorders the page. Primary keyword, three or four secondaries, title tag,
meta description, H1 direction, section order.

**Phase 3 — Three directions. [GATE]** The consultative heart. Interrogate the
subject along axes the user didn't specify — **era, surface, light, register,
motion** — and cross them into three genuinely different pages, not three flavours
of one idea.

**Make the third direction argue against the obvious reading of the brief.** If
the first two are dark and adrenal, the third is bright and clinical. It is often
the one that gets picked. This also mitigates the main risk of generating fresh
every time: the same brief through the same model drifts toward the same
attractors.

Each direction is a **complete package**, because a beginner cannot assemble a
page from a parts bin — offering palette and camera move as separate questions is
the failure mode:

- Name
- Palette — five named hex values
- Type — two families with roles, or one at contrasting weights
- Scene — subject, materials, lighting, background
- Motion — the camera move in plain language
- **Text-behind — proposed only where the scene supports it**: a clean, separable
  subject with room for type to pass behind it. Never for a dense fly-through with
  no isolable subject.
- Who it's for — one line

Then the escape ladder:

> Reply **1**, **2** or **3**. Or: **mix** (say which parts of which), **more**
> (three fresh directions), **describe** (your own), or **preview N** to render
> that direction first (~$0.15, about a minute).

A preview is a **mood test, not the hero** — rendered before copy exists, so
composition will differ from the final. Say so when offering it. Text descriptions
are free and leave the direction open; a render commits to one sample from that
space, so don't render by default.

**Phase 4 — Architecture.** Section skeleton with each section's job, plus the CTA
ladder. Decide **here, in context**, how many scrub sections and where — one hero
orbit with conventional reveals below, or two scrub sections bracketing the proof
content. A pacing judgement per brief, never a hardcoded default.

**Phase 5 — Copy. [GATE]** Every word: nav labels, H1, section headings, body, CTA
microcopy, footer, alt text, meta. Keywords placed where they read naturally.

- **Mark every invented claim** — brand names, statistics, testimonials, client
  counts — in `{{...}}` and list them at the end of the phase. Nothing fictional
  reaches production unnoticed.
- **Fix hero composition here.** If the H1 sits left, the still brief says the
  subject anchors right. Retrofitting composition costs a re-render.

**Phase 6 — Hero still. [GATE]** Two or three variants at ~$0.15. Prompt a
**frozen moment** — subject, materials, lighting, background, camera angle, and no
motion words at all. Compose for the move that follows: an orbit needs margin on
all sides or the subject clips at 90°; a push-in needs depth and a vanishing
point. Run `cutout` for text-behind directions.

**Phase 7 — Motion.** Run clips in parallel. **All clips start from the same
approved still** — a second still gives a different subject and the page reads as
two unrelated shots.

Motion prompt rules, which matter more than model choice:

- One continuous move. Every prompt ends with *"single unbroken take, no cuts, no
  scene change."* Scrubbing across a cut looks broken in reverse.
- Constant speed — ease-in/ease-out fights the scroll mapping.
- Name the move explicitly: turntable, dolly-in, orbit, crane down, rack focus.
- Describe **motion only**; the still already carries the look.
- Patterns: turntable → *"smooth seamless full 360 degree rotation, one complete
  revolution, subject stays centred, locked camera"*. Fly-through → *"slow
  continuous forward dolly, deep parallax"*. Reveal → *"components drift outward
  and float in slow motion"*. Atmosphere → *"camera holds, light sweeps across the
  subject, particles drift"*.
- Moderation rejections are common on "explode" and "dissolving figure" phrasing.
  *"Bursts outward in slow motion"* passes where *"explodes"* doesn't.

**Mobile clips are opt-in.** Default is one 16:9 render per section, composed
centre-safe so the phone crop works (a 16:9 clip cover-cropped to portrait loses
roughly 60% of frame width). Offer the 9:16 pair as an upgrade, state that it
roughly doubles the bill, and render only if asked.

**Phase 8 — Frames, assembly, QA.** Slice, use the printed frame counts, write the
site, serve it.

- **Seam check:** on a looping clip, compare first and last frames. Visible jump →
  re-slice with `--trim-end 0.3` and recheck. Persists → tell the user and offer
  either a non-looping treatment or a re-render.
- Tuning: slow orbits tolerate `--count 120`, fast motion wants more.
- **Verify in a real browser.** Headless screenshots blank sticky canvas sections
  when scrolled and will make a working page look broken.

### Non-negotiables the skill must enforce

**SEO.** *No headline, value proposition or keyword may live inside a generated
image.* Canvas content is invisible to crawlers; the overlay copy is real DOM text
and that is what carries meaning. The picture is atmosphere. Heading order must
read correctly top-to-bottom in the DOM, ignoring what fades in and out visually.

**Performance.** Poster is the LCP element. Frames lazy-load one viewport ahead.
WebP default. Reduced-motion and Save-Data load zero frames. Targets: LCP < 2.5s,
CLS < 0.1. Deploy note: `site/` is static — Netlify, Vercel, Cloudflare Pages, any
bucket **with HTTP/2**, without which the browser trickles frames six at a time.

**Money.** *Copy, palette, layout and type changes never touch `build/`.* Only a
scene or composition change justifies a new render — this is the single biggest
cost saver in the design. Re-slicing frames is always free.

**Honesty.** Never claim a render succeeded if it didn't; report retries. Raise IP
and likeness concerns at intake and again if a direction drifts toward a
recognisable brand or character.

---

## 11. Cost model

| Item | Cost |
| --- | --- |
| Hero still (Nano Banana Pro) | ~$0.15 |
| Cutout (BiRefNet) | ~$0.02 |
| 6s clip, Seedance 2.0 Fast, 720p | ~$1.45 |
| **Typical page: still + 2 clips** | **~$3** |
| Optional 9:16 mobile pair | ~+$2.90 |
| Dry run (`--placeholder`) | $0 |
| Bring-your-own footage | $0 |

Prices drift. They live in `providers/*.json` with a `note` field pointing at the
canonical pricing page, and the README must warn that published figures go stale.

---

## 12. README requirements

Install and first run. `python motionkit.py doctor` as step one. Prominent cost
warnings. A note that model commercial-use terms and generated-content IP are the
user's responsibility. Windows/macOS/Linux support stated explicitly.

**Credit the prior art:** the technique and the pipeline shape come from
Higgsfield's Motion Website Generator skill and the community `scroll-cinematic`
skill for Claude Code. This implementation is independent and provider-agnostic,
but the lineage should be stated.

---

## 13. Build order

1. `motionkit.py` — `doctor`, `init`, ffmpeg bootstrap, `.env` loading, state.
2. `providers/*.json`.
3. `frames` with `--placeholder`. **Verify before going further:** the dry-run
   path must work with no keys at all.
4. fal adapter — `image`, `video`, `cutout`.
5. Gemini adapter, clearly marked unverified.
6. `kit/` templates and the scrub engine.
7. `SKILL.md`.
8. README, LICENSE, `.gitignore`, `.env.example`.

Test as you go: `doctor` on a clean checkout, `init`, `--placeholder` frames, a
real slice from any mp4, and a `serve` that renders in a browser.

---

## 14. Expect v1 to be wrong somewhere

The first real build will expose things no amount of design catches. The likely
spots: whether three genuinely distinct directions actually come out or three
flavours of one, and seam quality on looping clips. Plan a fix pass after the
first real project rather than polishing ahead of evidence.

**First shakedown project: a mecha samurai — a cyberpunk-era standing mech in
Japanese samurai armour.** Run it end to end and fix what breaks.
