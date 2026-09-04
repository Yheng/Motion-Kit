# mecha-samurai — brief

Written during intake (Phase 1). An output of the conversation, and an optional
input on resume.

## What this is

A flagship unit reveal: a fictional mecha manufacturer launching its newest
model. A product-launch page in the Apple/Awwwards shape — one continuous
camera move on a single hero subject, with the specification and the case for
the machine told in real DOM copy below and over it.

The subject is a cyberpunk-era standing mech in Japanese samurai armour.

## Who it is for

Buyers and specifiers looking at a high-value machine: the page has to read as
a serious product page, not a fan render. Secondary audience is press picking
up the announcement.

## What a visitor should do

Read the unit's positioning, reach the specification, and register interest.
CTA ladder: nav → hero → mid-page → close.

## Constraints

- **Commercial use:** this is a shakedown of the toolchain, not client work.
  Generated-content IP and the model's commercial-use terms remain the user's
  responsibility if it ever ships. Google applies SynthID watermarking to
  Gemini output.
- **Likeness / IP:** "mecha samurai" is a generic concept and stays that way.
  Prompts avoid the silhouettes, colourways and markings of any recognisable
  franchise. Flag it immediately if a direction starts drifting toward one.
- **Provider:** Gemini only — no `FAL_KEY` on this machine. Gemini has no
  cutout model, so **text-behind-subject is unavailable** for this build and no
  direction may depend on it. Veo clips are 4, 6 or 8 seconds.
- **Pricing is unverified.** Estimates come from local config; published Veo
  3.1 rates span $0.15–$0.75/s, so a 6s clip could cost anywhere from $0.90 to
  $4.50.

## Notes

First real end-to-end run of the tool, per BUILD-SPEC.md §14. The Gemini adapter
has never been exercised against the live API. Finding what breaks is the point.
