# motion-kit

Build scroll-driven cinematic landing pages — the kind where a product turns as
you scroll, the clip runs backward when you scroll up, and the headline passes
behind the subject.

The effect is **not** Three.js and **not** a `<video>` tag. It is a **canvas
image-sequence scrub**: a short clip is exported to numbered frames, preloaded,
and the frame painted to a `<canvas>` is chosen by scroll position. Frames beat
video because video scrubbing stutters on mobile Safari, seeks are not
frame-accurate, and reverse playback is unreliable. Frames are just `drawImage`.

The "3D" comes entirely from camera movement in the source clip. Everything
upstream exists to get one good continuous camera move.

There are two halves: a **CLI** (`motionkit.py`) that talks to image and video
providers, slices clips into frames with ffmpeg, and tracks project state and
spend; and a **Claude Code skill** that runs the consultation — positioning, art
direction, architecture, SEO copy — and drives the CLI.

---

## Requirements

- **Python 3.9 or newer.** Standard library only — nothing to pip install.
- **ffmpeg.** If it is not on your PATH, `doctor` downloads a static build into
  `.ffmpeg/` for you. No Homebrew, no sudo, no package manager.
- **Windows, macOS and Linux** are all supported, with identical commands in
  PowerShell, Terminal and bash.

## First run

```
git clone https://github.com/Yheng/Motion-Kit.git
cd Motion-Kit
python motionkit.py doctor
```

`doctor` is always step one. It reports your platform, finds or installs ffmpeg,
says which providers have keys, and lists any project already in flight.

On first run it copies `.env.example` to `.env`. Open that file and add one key:

```bash
FAL_KEY=            # default provider — stills, motion and cutouts
GEMINI_API_KEY=     # fallback, on a separate bill
```

`.env` is gitignored. Never commit it, and never paste a key into a chat.

### No key at all

Two paths cost nothing and both are first-class:

```
python motionkit.py init demo --provider byo
python motionkit.py frames --project demo --name hero --placeholder
python motionkit.py serve --project demo
```

`--placeholder` generates frames from a test pattern with no clip and no API
call, so the entire page — direction, copy, layout, engine, mobile, SEO — can be
exercised end to end for **$0**.

`--provider byo` means bring your own footage. Drop your own stills and mp4s
into `out/<project>/build/` and everything downstream works for free. Any clip
with continuous camera motion does: Blender or C4D turntables, drone footage, an
existing client product video, After Effects renders. An agency with real
footage gets a better page than any generative model produces, and it makes the
tool independent of any provider's pricing or availability.

## Commands

| Command | What it does |
| --- | --- |
| `doctor` | platform, Python, ffmpeg, per-provider key status, existing projects |
| `init <project> [--provider] [--force]` | scaffold `out/<project>/`, copy `kit/` into `site/` |
| `image --project --prompt --out` | generate a still into `build/` |
| `video --project --prompt --image --out [--duration --loop]` | animate a still into `build/` |
| `cutout --project --image` | background removal to a PNG with alpha |
| `frames --project --name [--clip \| --placeholder]` | slice to frames, write the poster |
| `serve --project [--port]` | serve `site/` locally |
| `cost --project` | itemised spend log and total |

Run any of them with `--help` for the full flag list.

## Using it with Claude Code

The skill in `.claude/skills/scroll-cinematic/` runs an eight-phase
consultation: intake, positioning and SEO, three art directions, architecture,
copy, hero still, motion, then frames and QA. It gates at the three phases where
a wrong turn costs money, and it drives the CLI rather than calling APIs
directly, so the spend log stays accurate.

Just ask for what you want — "a scroll site for a mecha samurai", "an
Apple-style page for my product" — and it will pick the skill up.

---

## Cost

**Every paid call prints an estimate before it runs**, and every charge is
appended to `state.json` so `cost --project <p>` can itemise it.

| Item | Roughly |
| --- | --- |
| Hero still | $0.15 |
| Cutout | $0.02 |
| 6s clip at 720p | $1.45 |
| **Typical page: one still + two clips** | **~$3** |
| Optional 9:16 mobile pair | +$2.90 |
| Dry run (`--placeholder`) | $0 |
| Bring your own footage | $0 |

> **Published prices go stale.** The figures above and the rates in
> `providers/*.json` are estimates from local config, not an invoice. Each
> provider file carries a `note` pointing at its canonical pricing page. Check
> there before budgeting, especially for anything above 720p — a flat
> per-second rate understates higher resolutions.

Two design decisions keep the bill down, and it is worth knowing them:

- `out/<project>/` splits into `build/` (source stills and mp4s, kept) and
  `site/` (the deployable folder). **Re-slicing frames from `build/` is free and
  unlimited.** Only a scene or composition change justifies a new render — copy,
  palette, layout and type changes never touch `build/`.
- An interrupted run resumes an in-flight job instead of submitting a second
  one. If the process dies after a job is created you are still billed for it,
  so re-running the identical command collects that job rather than paying
  twice.

## Deploying

`site/` is self-contained and static. Netlify, Vercel, Cloudflare Pages, or any
bucket **with HTTP/2** — without it the browser trickles frames six at a time.
Serve `frames/` with a long `Cache-Control`.

The page is complete and sellable with zero frames loaded: under
`prefers-reduced-motion` or Save-Data the engine loads nothing, the stage
unsticks, and every word of copy remains. That is a structural property of how
the CSS and JS are split, not something you have to remember to test.

## Your responsibilities

Generated-content IP and each model's commercial-use terms are **yours** to
check, not this tool's. Raise likeness and trademark questions before you
render, not after. Google applies SynthID watermarking to Gemini output — it is
invisible, but it is a consideration for client work.

## Status

The fal adapter is written to fal's documented queue API and its endpoint slug,
pricing and field names were confirmed against fal.ai. **The Gemini adapter has
never been run against the live API** and is marked unverified in
`providers/gemini.json` and by `doctor`. Two of its model IDs had already
drifted from the original design when this was built, which is exactly why model
IDs live in JSON and an unknown endpoint is reported rather than guessed around.

## Prior art

The technique and the shape of this pipeline come from **Higgsfield's Motion
Website Generator** skill and the community **`scroll-cinematic`** skill for
Claude Code. This implementation is independent and provider-agnostic, but the
lineage should be stated.

Two earlier projects of my own solve much of the same problem in a different
stack — [`motion-site-studio`](https://github.com/Yheng/motion-site-studio) and
`velta`, both GSAP and TypeScript. motion-kit is a deliberate clean-room rebuild
against a written specification, with no dependencies and no build step.

## Licence

MIT. See [LICENSE](LICENSE).
