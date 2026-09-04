# aesthetica — brief

Written during intake (Phase 1).

## What this is

A landing page for **Clinica Aesthetico**, an aesthetic medicine practice in
**Davao City, Philippines**, established 2009, carrying both the clinic and its practitioner. **Dr. Isel Bayuga** is a real, named
medical professional working in advanced non-invasive skin and facial
rejuvenation. Her standing is the reason to trust the clinic, so the page has to
do both jobs: establish her authority, then convert it into an enquiry.

## Who it is for

Prospective patients evaluating a practitioner rather than shopping on price —
people who want to know who is holding the device before they book. Secondary:
peers and event organisers in the aesthetic medicine circles she participates in.

## What a visitor should do

Book, or enquire. CTA ladder: nav → hero → mid-page → close.

## Constraints — read these before generating anything

- **Her likeness is never synthesised.** Dr. Bayuga is a real, identifiable
  person and the client holds a release, but a release covers *using* a likeness,
  not fabricating one. Every image of her on this page is her own photograph.
  Generation is used **only for the botanical plate, which contains no person.**
- **The three supplied references are AI mockups**, including AI depictions of
  her and garbled lettering. They are mood input for the direction only. Nothing
  from them ships.
- **Medical marketing is regulated.** No invented treatment names, credentials,
  qualifications, society memberships, patient outcomes, before/afters or
  statistics. Every such fact must come from the client and is marked `{{...}}`
  until it does. This matters more here than on any fictional brief.
- Commercial use: real client work. Check the generative model's commercial
  terms before the page ships.

## The motion, and why it is the right one

Chosen at intake: **a static subject with moving botanicals.** It is the only
move the v1 text-behind treatment supports — the static cutout needs a subject
that holds still while the plate scrubs behind it — and it happens to be the
exact thing the references gesture at.

It also solves the likeness problem structurally:

    z30  cutout   Dr. Bayuga, her real photograph, background removed
    z20  overlay  the headline passing BEHIND her
    z10  canvas   orchids, ferns, butterflies drifting — no person in frame
    z1   poster   frame 1

A photograph cannot drift between frames, so there are no face artifacts across
179 frames, and nothing of her is ever generated.

## Direction — approved

**1. Sanctuary.** Warm ivory studio, massed white orchids and pale roses,
eucalyptus and fern at the frame edges, diffused window light. Cormorant
Garamond for display, Inter for text and tracked caps for data. Rosewood accent
on rule, eyebrow and cta. Bookended grounds, editorial 6x scale, peak at the
practitioner band, hero share chapter at 200vh.

Chosen over the contrarian "Plain Air" for coherence with the clinic's existing
imagery. It also has the best light match to her photograph, which is the real
compositing constraint: a photograph cannot be relit.

## The clinic's name

**Clinica Aesthetico**, not "Aesthetica". The official logo supplied by the
client reads CLINICA AESTHETICO and carries the date 2009; that artifact is
authoritative over anything said in conversation, so the page follows it. The
project directory is still `out/aesthetica/` — that is only a slug and renaming
it would break the paths recorded in state.json.

## Notes

Photographs to be supplied by the client into `build/`. Highest resolution
available; even lighting and a separable background make the cutout clean, and
she should sit so a headline can cross behind her shoulder.
