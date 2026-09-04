---
name: scroll-cinematic
description: >-
  Build a scroll-driven cinematic landing page — a canvas image-sequence scrub
  where the clip plays as the visitor scrolls. Use for: a 3D scroll site, scroll
  animation, a motion or cinematic landing page, an animated hero, an
  Apple-style product page, an Awwwards-style site, a frame-scrub or
  image-sequence hero, and for vague asks like "make me an amazing landing page
  with <subject> and animation" or "a website where the thing spins as I
  scroll". Runs a consultation — positioning, art direction, copy, SEO — and
  drives the motionkit.py CLI to generate stills and clips, slice frames, and
  assemble a static site.
---

# scroll-cinematic

You are running a design consultation that ends in a deployable static folder.
The visual effect is a **canvas image-sequence scrub**: a short clip is exported
to numbered frames and the frame painted to a `<canvas>` is chosen by scroll
position. The "3D" comes entirely from camera movement in the source clip, so
everything upstream exists to get one good continuous camera move.

Read `BUILD-SPEC.md` if you need detail this file does not cover.

## Ground rules

- **Gate by default.** Stop at the three gates below and wait. If the user
  passed `--auto`, state each choice and your reasoning, then continue without
  stopping.
- **Never spend without printing the estimate first.** The CLI does this for
  you; do not suppress it or paraphrase it away.
- **Ask in numbered choices** — "Reply 1, 2 or 3" — always with a free-text
  escape. There is no tappable UI here. Never ask an open-ended question a
  beginner cannot answer: not "what palette do you want?" but three complete
  directions to choose between.
- **Always drive the CLI.** Never call ffmpeg or a provider API directly. The
  CLI is what records spend and state; work around it and the ledger is wrong.
- **Resume before starting.** If `out/<project>/state.json` exists, read it and
  `brief.md`, tell the user which phase they stopped at, and continue there.

Run `python motionkit.py doctor` first, every time. It reports ffmpeg, which
providers have keys, and any project already in flight.

## The eight phases

`state.json` tracks `phase`. Set it as you go so a crash or a closed terminal
resumes in the right place.

### Phase 1 — Intake

`python motionkit.py init <project> [--provider fal|gemini|byo]`

If `brief.md` already has content, read it and ask only about gaps. Otherwise
ask **two or three** numbered-choice questions: what this is, who it is for,
what a visitor should do. Also settle commercial-use and likeness constraints —
raise them here rather than after a render.

Then **write `brief.md` from the conversation.** It is an *output* of intake and
an optional *input* on resume; it is never a prerequisite. Conversation is the
primary path.

### Phase 2 — Positioning and SEO

Before any visual thinking, because search intent reorders the page. Decide the
primary keyword, three or four secondaries, the title tag, the meta description,
the H1 direction, and the section order.

### Phase 3 — Three directions. **[GATE]**

The consultative heart. Interrogate the subject along axes the user did not
specify — **era, surface, light, register, motion** — and cross them into three
genuinely different pages, not three flavours of one idea.

**Make the third direction argue against the obvious reading of the brief.** If
the first two are dark and adrenal, the third is bright and clinical. It is
often the one that gets picked, and it guards against every brief drifting
toward the same attractor.

Each direction is a **complete package**, because a beginner cannot assemble a
page from a parts bin:

- **Name**
- **Palette** — five named hex values
- **Type** — two families with roles, or one at contrasting weights
- **Scene** — subject, materials, lighting, background
- **Motion** — the camera move in plain language
- **Text-behind** — propose it *only* where the scene supports it, which means
  BOTH of these, not just the first:
  1. a clean, separable subject with room for type to pass behind it; and
  2. **a subject that holds still while the background moves.**
  v1 ships the static sandwich: one fixed cutout over a moving plate. If the
  subject itself moves — and a **turntable rotates the subject**, so it always
  does — the static cutout diverges from the frames behind it within a few
  degrees and the page shows two subjects, a frozen ghost over a turning one.
  Measured on the shakedown: by frame 45 of 179 the mech had turned to profile
  and left the cutout's silhouette entirely.
  So: text-behind suits *atmosphere* moves (light sweeping, particles drifting,
  environment moving past a static subject) and dolly moves where the subject
  stays roughly frontal. It does **not** suit turntables, orbits, or any reveal
  that re-poses the subject. Never for a dense fly-through with no isolable
  subject either.
- **Who it's for** — one line

Then the escape ladder, verbatim:

> Reply **1**, **2** or **3**. Or: **mix** (say which parts of which), **more**
> (three fresh directions), **describe** (your own), or **preview N** to render
> that direction first (~0.15 USD, about a minute).

A preview is a **mood test, not the hero** — it is rendered before copy exists,
so composition will differ from the final. Say so when offering it. Text
descriptions are free and leave the direction open; a render commits to one
sample from that space, so do not render by default.

Record the choice in `state.json` approvals.

### Phase 4 — Architecture

The section skeleton, each section's job, and the CTA ladder. Decide **here, in
context**, how many scrub sections and where — one hero orbit with conventional
reveals below, or two scrub sections bracketing the proof content. This is a
pacing judgement per brief, never a hardcoded default.

### Phase 5 — Copy. **[GATE]**

Every word: nav labels, H1, section headings, body, CTA microcopy, footer, alt
text, meta description. Place keywords where they read naturally.

- **Mark every invented claim** — brand names, statistics, testimonials, client
  counts — in `{{...}}` and list them at the end of the phase. Nothing fictional
  reaches production unnoticed.
- **Fix hero composition here.** If the H1 sits left, the still brief says the
  subject anchors right. Retrofitting composition costs a re-render.

### Phase 6 — Hero still. **[GATE]**

```
python motionkit.py image --project <p> --prompt "..." --out hero_v1.png
```

Two or three variants at ~0.15 USD each. Prompt a **frozen moment** — subject,
materials, lighting, background, camera angle — and **no motion words at all.**

Compose for the move that follows: an orbit needs margin on all sides or the
subject clips at 90°; a push-in needs depth and a vanishing point.

For a text-behind direction, run `cutout` on the approved still. It writes a PNG
with alpha to `build/` and publishes a copy into `site/cutout/`.

Get the still approved before spending on motion.

### Phase 7 — Motion

```
python motionkit.py video --project <p> --prompt "..." --image hero_v2.png \
    --out hero_orbit.mp4 --duration 6 --loop
```

Run clips in parallel where there is more than one. **All clips start from the
same approved still** — a second still gives a different subject and the page
reads as two unrelated shots.

Motion prompt rules, which matter more than model choice:

- One continuous move. End every prompt with *"single unbroken take, no cuts, no
  scene change."* Scrubbing across a cut looks broken in reverse.
- Constant speed. Ease-in/ease-out fights the scroll mapping.
- Name the move explicitly: turntable, dolly-in, orbit, crane down, rack focus.
- Describe **motion only** — the still already carries the look.

Patterns:

| Move | Prompt |
| --- | --- |
| Turntable | *smooth seamless full 360 degree rotation, one complete revolution, subject stays centred, locked camera* |
| Fly-through | *slow continuous forward dolly, deep parallax* |
| Reveal | *components drift outward and float in slow motion* |
| Atmosphere | *camera holds, light sweeps across the subject, particles drift* |

Moderation rejections are common on "explode" and "dissolving figure" phrasing.
*"Bursts outward in slow motion"* passes where *"explodes"* does not. A rejected
job is not billed, and the CLI says so.

**Mobile clips are opt-in.** Default to one 16:9 render per section, composed
centre-safe so the phone crop works — a 16:9 clip cover-cropped to portrait
loses roughly 60% of frame width. Offer the 9:16 pair as an upgrade, state that
it roughly doubles the bill, and render only if asked.

### Phase 8 — Frames, assembly, QA

```
python motionkit.py frames --project <p> --name hero
python motionkit.py serve --project <p>
```

1. Slice, then **paste the printed `SCRUB_SECTIONS` snippet** into
   `site/index.html`. Use the counts it prints — they are files measured on
   disk, and ffmpeg's fps filter rounds.
2. Write the copy from Phase 5 into `site/index.html`.
3. Write the direction from Phase 3 into **`site/brand.css`** — palette, type,
   texture. That is the only design file you edit. `site/styles.css` is
   structural scaffolding and stays untouched; it is layered so `brand.css`
   overrides it without `!important`.
4. Delete the `.stage__cutout` element if the direction has no text-behind. It
   is a clean delete — no CSS or JS change is needed.
5. **Seam check** on a looping clip: compare the first and last frames. A
   visible jump means re-slice with `--trim-end 0.3` and recheck. If it
   persists, tell the user and offer either a non-looping treatment or a
   re-render.
6. Tuning: slow orbits tolerate `--count 120`; fast motion wants more.
7. **Verify in a real browser.** Headless screenshots blank sticky canvas
   sections when scrolled and will make a working page look broken. Note that a
   backgrounded tab pauses `requestAnimationFrame`, so scrolling the page by
   script leaves progress pinned at 0 — drive it with real input. The page
   exposes `window.__scrub.sections[0]` (`ready`, `loaded`, `paintedIndex`) for
   an automated check, plus `?scrub=on|off` and `?frame=0.5`.

## Non-negotiables

**SEO.** *No headline, value proposition or keyword may live inside a generated
image.* Canvas content is invisible to crawlers; the overlay copy is real DOM
text and that is what carries meaning. The picture is atmosphere. Heading order
must read correctly top-to-bottom in the DOM, ignoring what fades in and out.
Never give hero copy a `data-in` starting at 0 — it would be invisible on
arrival.

**Performance.** The poster is the LCP element. Frames lazy-load one viewport
ahead. WebP by default. Reduced-motion and Save-Data load zero frames, and the
page must still be complete and sellable — verify that explicitly. Targets: LCP
under 2.5s, CLS under 0.1. `site/` is static and deploys to Netlify, Vercel,
Cloudflare Pages or any bucket **with HTTP/2**, without which the browser
trickles frames six at a time.

**Money.** *Copy, palette, layout and type changes never touch `build/`.* Only a
scene or composition change justifies a new render — this is the single biggest
cost saver in the design. Re-slicing frames is always free. A typical page is
one still plus two clips, around 3 USD. `--placeholder` exercises the whole
pipeline for 0 USD, and `--provider byo` does the same with the user's own footage.

**Honesty.** Never claim a render succeeded if it did not; report retries. Raise
IP and likeness concerns at intake, and again if a direction drifts toward a
recognisable brand or character.
