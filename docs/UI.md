# ApplicantOS — `docs/UI.md`

> **Binding design system.** Everything in this file is a specification, not a suggestion.
> Where `docs/CONTRACTS.md` and this file disagree on data shapes, names, routes, or enum values,
> CONTRACTS wins. Where they disagree on anything visual, spatial, or temporal, this file wins.
> Deviations require an entry in `docs/OPEN_QUESTIONS.md` — not a local decision.
>
> **Provenance markers used throughout:**
> `[E]` = grounded in the research evidence (a measured value from Linear, Cursor, Vercel Geist,
> shipwright.cc, Kokonut, Raycast, Docker Desktop, or vendor documentation).
> `[J]` = design judgment by the author of this spec; defensible, but not measured from a shipped
> product. Argue with `[J]` calls; implement them until they are changed.
>
> **Every color value in this file was computed and contrast-checked** with a script, and every
> chart palette was run through the `dataviz` validator against *our* surfaces. Ratios quoted are
> real, not estimated.

---

## Table of contents

1. [Design principles](#1-design-principles)
2. [Color system](#2-color-system)
3. [Typography](#3-typography)
4. [Spacing, sizing, radius, elevation](#4-spacing-sizing-radius-elevation)
5. [Layout](#5-layout)
6. [Motion](#6-motion)
7. [Component specs](#7-component-specs)
8. [Screens](#8-screens)
9. [Interaction & keyboard](#9-interaction--keyboard)
10. [The instant-feel contract](#10-the-instant-feel-contract)
11. [Charts](#11-charts)
12. [Accessibility](#12-accessibility)
13. [Do / Don't](#13-do--dont)

---

## 0. The one-sentence design brief

**ApplicantOS is a night-shift instrument panel.** The user sleeps; an agent works; in the morning
the app must report — with total precision and zero ceremony — what happened, what needs a human,
and what to do next. It is a *readout*, not a *destination*. It should feel like looking at an
aircraft panel: dense, dark, calm, monospaced where it counts, and the only bright thing on screen
is the thing you are supposed to touch.

Three structural decisions carry that idea, and they are what make this app not look like every
other dark dashboard:

1. **Chrome is darker than content.** The sidebar and titlebar sit *below* the data plane, not
   above it. The content region is the lit surface. `[E — Cursor "Anysphere Dark": chrome #141414,
   editor #181818; Linear's redesign lowered sidebar brightness for the same reason]`
2. **Elevation is borders, not shadows.** A card is a background step *and* a border step, moved
   together, always. Shadows exist only on things that genuinely float above the page.
   `[E — shipwright.cc pairs bg-surface+border-line and bg-surface-raised+border-line-strong;
   Linear falls back to a background-level shift plus a border where shadow can't work]`
3. **Monospace is a semantic, not a decoration.** Every machine-produced value — ids, scores,
   salaries, durations, timestamps, counts, URLs — is set in the mono face. This is the app's
   typographic signature and it replaces `tabular-nums` in most places.
   `[E — shipwright.cc uses font-mono 59× vs font-bold 50×, and tabular-nums zero times]`

---

## 1. Design principles

Seven principles. Each is one sentence plus the consequence it forces.

### P1. Latency is a design property, not an engineering detail.
The user's stated requirement is *"there should never be 1 second of delay ever,"* so responsiveness
is specified in this document alongside color and type, and is testable.
**Which means:** every screen has a defined cache-warm path; `isPending` is the *only* flag allowed
to gate content; a spinner over data we already hold is a design bug filed against the component,
not a performance ticket. See §10.

### P2. The morning read comes first.
The primary job-to-be-done is a 30-second scan after waking: *what did it do, what broke, what needs
me.*
**Which means:** the Dashboard leads with one hero figure and a review-queue count that is reachable
in one keystroke; nothing on the first screen requires a click to become legible; no carousel, no
tabs, no "expand to see" on the overview.

### P3. Density over comfort, always.
This is a desktop app on a wide monitor operated by a power user, not a marketing page.
**Which means:** 36px table rows, 13px body text, 7–12px gaps, a 232px sidebar; and a hard refusal
of the marketing-scale patterns the component libraries ship (`rounded-3xl`, `p-8`, `max-w-3xl`)
`[E — Kokonut's apple-activity-card is rounded-3xl p-8 max-w-3xl; that is landing-page scale]`.

### P4. Color is spent, not distributed.
There is exactly one accent hue, and solid accent appears at most **once per view** — on the primary
action.
**Which means:** everything else that wants to be accent-colored gets a 6–15% wash instead
`[E — shipwright uses ~19 solid accent instances vs ~42 surface/background, and most accent usage is
fractional]`; status color never borrows the accent; and if two things on a screen are both solid
accent, one of them is wrong.

### P5. Motion is rationed by frequency.
An animation the user sees 200 times a day is a tax; an animation they see twice a week is a gift.
**Which means:** command palette, sidebar navigation, tab switches, route changes, keyboard-triggered
anything, and live number updates have **zero** animation `[E — Raycast ships no open/close
animation; Linear's `--speed-highlightFadeIn` is literally `0s`]`; modals, sheets, toasts, and
onboarding get the full vocabulary.

### P6. Nothing is fabricated, and the UI says so.
CONTRACTS §18.7 requires every resume bullet to trace to a `KnowledgeFact.id`; the interface must
make provenance visible rather than implied.
**Which means:** generated content always renders its source affordance (a fact chip, a
"from GitHub · README.md" line, a screenshot link); "unknown" is an em-dash `—`, never `N/A`, never
blank `[E — Vercel Geist table rules]`; and confidence below `min_answer_confidence` is shown as a
number, not softened into prose.

### P7. Safety states are louder than success states.
`auto_apply_enabled` and `dry_run` are kill switches (CONTRACTS §18.3) and `needs_review` is the one
queue where a stale count is real user harm.
**Which means:** dry-run mode is a persistent, non-dismissible bar in the titlebar, not a settings
toggle you have to remember; the review count is a live badge; and destructive/irreversible actions
are on `Ctrl`, never `⌘`/`Alt` `[E — Raycast reserves Ctrl for destructive actions]`.

### Anti-goals — what we deliberately refuse to build

| We will not build | Why |
|---|---|
| A light-first design that got a dark mode later | The app is used at 7am and 11pm on a desktop; dark is the design, light is the accommodation. `[J]` |
| Ambient/decorative background animation (beams, particles, gradient meshes, flow fields, spotlight cards) | It competes with live data for attention and burns GPU on a machine that is also running Playwright. `[E — the entire "reject on sight" list from the Kokonut/Skiper research]` |
| Route transition animations | The user navigates dozens of times per session; any transition is pure added latency. `[E]` |
| A second accent hue, gradients as fills, or glassmorphism as a tile treatment | One accent + fractional washes covers every case; `backdrop-filter` on many simultaneous tiles is a known frame-rate cliff. `[E — Kokonut's own liquid-glass-card source warns it degrades with multiple instances]` |
| More than three text colors + one disabled tier | Four tiers force hierarchy decisions; a fifth invites improvisation. `[E — shipwright ships three; Linear ships four]` |
| A preloader, splash screen, or app-level loading gate | It delays first paint of the data the user opened the app to read. Cache hydration happens *before* `createRoot().render()` instead. `[E]` |
| Skeletons used as empty states | A permanent shimmer tells the user the app is broken. `[E — explicit Geist rule]` |
| Toast for every mutation | Optimistic UI already showed the result; a toast is only for failures, undo affordances, and background completions. `[J]` |
| Breadcrumbs | The IA is two levels deep and the sidebar + palette are always present. `[E — shipwright has zero breadcrumbs; Linear has none]` |
| Charts with two y-axes, pie charts, or donut charts with more than 2 slices | Hard `dataviz` prohibition; a stat tile or a bar is always the better answer. `[E]` |

---

## 2. Color system

### 2.1 Architecture

Three parallel systems, and they do not mix:

| System | Purpose | Form |
|---|---|---|
| **Surface tokens** (opaque) | Structural planes: chrome, page, card, popover | Opaque hex — the elevation ladder |
| **State layers** (alpha-on-white) | hover / selected / pressed / focus, on *any* surface | `rgb(255 255 255 / α)` — composites correctly at every level `[E — Cursor's entire state ramp; Linear's parallel `#ffffff08/12/26` set]` |
| **Semantic colors** (opaque, fixed) | Status, score bands, chart series | Opaque hex, **never derived from the accent** `[E — shipwright keeps status outside the token system as literal hex so it survives accent changes]` |

**The elevation rule (highest-leverage rule in this document):** background and border move together
as one step. `--bg-surface` always pairs with `--border-default`; `--bg-elevated` always pairs with
`--border-strong`. Never one without the other. `[E — shipwright]`

The accent is stored as **space-separated RGB channels**, not hex, so every alpha wash comes off one
variable and a future theme swap is one line. `[E — shipwright's `--accent-rgb: 59 130 246` and
`rgb(var(--accent-rgb)/…)` compile target]`

### 2.2 Dark theme — the binding token set

```css
/* desktop/src/styles/tokens.css */
:root,
:root[data-theme="dark"] {
  color-scheme: dark;

  /* ── Surfaces: a 5-step ladder. Chrome is DARKEST; content is the lit plane. ── */
  --bg-chrome:      #08090C;  /* titlebar, sidebar, status bar — recedes below content */
  --bg-base:        #0C0E12;  /* the content region / page ground */
  --bg-surface:     #12151A;  /* cards, table containers, panels, inputs' parent */
  --bg-elevated:    #171B21;  /* selected/hovered rows, featured cards, inset wells' sibling */
  --bg-overlay:     #1B1F26;  /* dialogs, popovers, command palette, tooltips, dropdowns */
  --bg-inset:       #08090C;  /* recessed wells INSIDE a surface: mono chips, code, log gutter */

  /* ── Borders: three luminance steps, one width (1px). Never a border-width scale. ── */
  --border-subtle:  #1A1E25;  /* intra-card dividers, table row rules, disabled edges */
  --border-default: #262B33;  /* every card, panel, input, and button edge */
  --border-strong:  #363C46;  /* hover, selected, focused-within, featured */

  /* ── Text: 3 tiers + disabled. Never pure #FFF. ── */
  --fg-primary:     #F2F4F8;  /* company, role, headings, values                    */
  --fg-secondary:   #A8B0BD;  /* descriptions, secondary cells, sidebar labels       */
  --fg-muted:       #7E8795;  /* metadata: "applied 3d ago", counts, axis labels     */
  --fg-disabled:    #4F5764;  /* disabled controls only — exempt from WCAG 1.4.3     */
  --fg-on-accent:   #FFFFFF;  /* text on a solid accent fill                         */
  --fg-on-status:   #08090C;  /* text on a solid status fill (offer badge only)      */

  /* ── Accent: ONE hue. Channels, not hex. ── */
  --accent-rgb:      91 95 214;             /* #5B5FD6 "Iris" — OKLCH(0.55 0.17 278) */
  --accent:          rgb(var(--accent-rgb));           /* #5B5FD6 solid fill          */
  --accent-hover:    #6165DE;                          /* +1 lightness step           */
  --accent-active:   #5155C4;                          /* −1 lightness step (pressed) */
  --accent-text:     #8E93FF;   /* accent as TEXT/ICON on dark — never the fill color */
  --accent-subtle:   rgb(var(--accent-rgb) / 0.12);    /* active pill / selected row  */
  --accent-wash:     rgb(var(--accent-rgb) / 0.06);    /* highlighted region          */
  --accent-border:   rgb(var(--accent-rgb) / 0.45);    /* active filter pill edge     */
  --accent-glow:     rgb(var(--accent-rgb) / 0.35);    /* colored shadow on primary   */

  /* ── State layers: alpha-on-white, valid on every surface above. ── */
  --state-hover:     rgb(255 255 255 / 0.045);
  --state-selected:  rgb(255 255 255 / 0.075);
  --state-pressed:   rgb(255 255 255 / 0.11);
  --state-divider:   rgb(255 255 255 / 0.05);   /* intra-card hairlines */
  --state-track:     rgb(255 255 255 / 0.07);   /* progress/score bar track */
  --focus-ring:      rgb(var(--accent-rgb) / 0.75);

  /* ── Semantic status families (see 2.4 for the ApplicationStatus mapping) ── */
  --st-neutral:      #8A93A1;   /* idle / not-started            */
  --st-dim:          #5C6472;   /* dormant / dead-ended          */
  --st-progress:     #4D9BFF;   /* machine is working            */
  --st-success:      #35C67C;   /* it worked                     */
  --st-review:       #F0A93B;   /* a human is required           */
  --st-danger:       #F05C56;   /* it broke                      */
  --st-rejected:     #C4636E;   /* a negative human outcome      */
  --st-interview:    #E97BC8;   /* a positive human outcome      */
  --st-offer:        #2FE0A6;   /* the best outcome              */

  /* ── Score bands: an ORDINAL ramp of the accent hue. Not the status palette. ── */
  --score-0:         #454B61;   /*  0–39  reject                 */
  --score-1:         #4F55B4;   /* 40–59  weak                   */
  --score-2:         #5A60D0;   /* 60–69  borderline             */
  --score-3:         #7074F2;   /* 70–84  at/above auto-apply    */
  --score-4:         #9BA2FF;   /* 85–100 strong                 */
  --score-threshold: #A8B0BD;   /* the min_score tick mark       */

  /* ── Shadows: dark-mode alphas are ~5× light-mode. Only on floating layers. ── */
  --shadow-raised: 0 1px 2px 0 rgb(0 0 0 / 0.45),
                   inset 0 1px 0 0 rgb(255 255 255 / 0.030);
  --shadow-float:  0 8px 28px -6px rgb(0 0 0 / 0.60),
                   0 2px 6px -2px rgb(0 0 0 / 0.45),
                   inset 0 1px 0 0 rgb(255 255 255 / 0.050);
  --shadow-dialog: 0 24px 64px -12px rgb(0 0 0 / 0.72),
                   0 4px 12px -4px rgb(0 0 0 / 0.50),
                   inset 0 1px 0 0 rgb(255 255 255 / 0.060);
  --shadow-accent: 0 4px 16px -6px var(--accent-glow);

  /* ── Chart chrome (see §11) ── */
  --chart-surface:  var(--bg-surface);
  --chart-grid:     #1F242B;
  --chart-axis:     #2B313A;
  --chart-ink:      var(--fg-muted);
}
```

### 2.3 Light theme

Light is a real, supported theme — not an inversion. The structural invariant is preserved:
**chrome stays one step away from the content plane** (in light, chrome is *darker* than the white
content plane, which is the same relationship, not a flip). `[E — Cursor Midnight keeps the same
3–5 point chrome/content delta across a hue change; the delta is the invariant, not the hex]`

```css
:root[data-theme="light"] {
  color-scheme: light;

  --bg-chrome:      #ECEEF1;
  --bg-base:        #F4F5F7;
  --bg-surface:     #FFFFFF;
  --bg-elevated:    #FFFFFF;   /* separation comes from border-strong + shadow-raised */
  --bg-overlay:     #FFFFFF;
  --bg-inset:       #F4F5F7;

  --border-subtle:  #ECEEF1;
  --border-default: #DDE1E6;
  --border-strong:  #C3C9D2;

  --fg-primary:     #14161A;
  --fg-secondary:   #4C545F;
  --fg-muted:       #6E7684;
  --fg-disabled:    #A2A9B4;
  --fg-on-accent:   #FFFFFF;
  --fg-on-status:   #FFFFFF;

  --accent-rgb:      79 83 201;   /* #4F53C9 */
  --accent:          rgb(var(--accent-rgb));
  --accent-hover:    #454AB8;     /* light mode DARKENS on hover */
  --accent-active:   #3C4099;
  --accent-text:     #4A4EC4;
  --accent-subtle:   rgb(var(--accent-rgb) / 0.10);
  --accent-wash:     rgb(var(--accent-rgb) / 0.05);
  --accent-border:   rgb(var(--accent-rgb) / 0.40);
  --accent-glow:     rgb(var(--accent-rgb) / 0.28);

  --state-hover:     rgb(9 11 15 / 0.040);
  --state-selected:  rgb(9 11 15 / 0.065);
  --state-pressed:   rgb(9 11 15 / 0.095);
  --state-divider:   rgb(9 11 15 / 0.07);
  --state-track:     rgb(9 11 15 / 0.08);
  --focus-ring:      rgb(var(--accent-rgb) / 0.70);

  --st-neutral:      #5D6673;
  --st-dim:          #8B929C;
  --st-progress:     #1F6FEB;
  --st-success:      #127C48;
  --st-review:       #96590A;
  --st-danger:       #C0322C;
  --st-rejected:     #9A3F49;
  --st-interview:    #A0369C;
  --st-offer:        #0A7A5B;

  --score-0:         #B6BBC6;
  --score-1:         #8E93D8;
  --score-2:         #6E73D2;
  --score-3:         #4F53C9;
  --score-4:         #3A3EA8;
  --score-threshold: #6E7684;

  --shadow-raised: 0 1px 2px 0 rgb(9 11 15 / 0.06),
                   0 0 0 1px rgb(9 11 15 / 0.02);
  --shadow-float:  0 8px 24px -6px rgb(9 11 15 / 0.12),
                   0 2px 6px -2px rgb(9 11 15 / 0.08);
  --shadow-dialog: 0 24px 56px -12px rgb(9 11 15 / 0.20),
                   0 4px 12px -4px rgb(9 11 15 / 0.10);
  --shadow-accent: 0 4px 16px -6px var(--accent-glow);

  --chart-surface:  #FFFFFF;
  --chart-grid:     #ECEEF1;
  --chart-axis:     #DDE1E6;
  --chart-ink:      var(--fg-muted);
}
```

**Theme resolution.** Three states, exactly as the platform expects: `data-theme="dark"` and
`data-theme="light"` are explicit user choices persisted in `electron-store`; absent the attribute,
follow `prefers-color-scheme`. Write the dark palette on bare `:root` (dark is the default), then
mirror it under `:root[data-theme="dark"]`, and put light under both `:root[data-theme="light"]` and
`@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { … } }`. Never let a color
exist only inside a media query.

### 2.4 `ApplicationStatus` → visual mapping (binding)

All thirteen values from `app/models/enums.py`. The **family** column is the color token; the
**dot** column is the shape, because color alone never carries state (§12).

| `ApplicationStatus` | Label | Token | Dot | Animated? | Badge fill |
|---|---|---|---|---|---|
| `draft` | Draft | `--st-neutral` | ring (hollow) | no | wash 12% |
| `preparing` | Preparing | `--st-progress` | filled | **yes** — 2.4s pulse ring | wash 12% |
| `ready` | Ready | `--st-progress` | ring (hollow) | no | wash 12% |
| `submitting` | Submitting | `--st-progress` | filled | **yes** — 2.4s pulse ring | wash 12% |
| `submitted` | Submitted | `--st-success` | ring (hollow) | no | wash 12% |
| `confirmed` | Confirmed | `--st-success` | filled | no | wash 12% |
| `needs_review` | Needs review | `--st-review` | filled + 1px outer ring | **yes** — 2.4s pulse ring | wash 14% |
| `failed` | Failed | `--st-danger` | filled | no | wash 14% |
| `abandoned` | Abandoned | `--st-dim` | ring (hollow), 1px dashed | no | wash 10% |
| `rejected` | Rejected | `--st-rejected` | filled | no | wash 12% |
| `interview` | Interview | `--st-interview` | filled | no | wash 14% |
| `offer` | **Offer** | `--st-offer` | filled | no | **solid fill**, `--fg-on-status` text |
| `ghosted` | Ghosted | `--st-dim` | ring (hollow), 1px dashed | no | wash 10% |

Four rules that make this readable across a 40-row table without reading a single word:

1. **Animated = in flight.** Only `preparing`, `submitting`, and `needs_review` animate. Everything
   else is static. This is scannable in peripheral vision. `[E — Vercel StatusDot: animate only in
   non-terminal states, and never pair the dot with a separate spinner]`
2. **Hollow vs filled = "waiting on them" vs "settled."** `submitted` (hollow) → `confirmed`
   (filled) is a shape change, not a color change. `[J]`
3. **Dashed = dead.** `abandoned` and `ghosted` share `--st-dim` and are distinguished from each
   other only by label — correct, because they mean nearly the same thing to the user.
4. **`offer` is the only status that gets a solid fill.** Exactly one status in the whole product
   is allowed to be as loud as the primary action, and it is the one the user is doing all of this
   for. `[J]`

`PostingStatus`, `SessionStatus`, `IndexStatus`, and `CheckpointStatus` reuse the same nine family
tokens; they get **badges only, never the animated dot**, which is reserved for the application
lifecycle. `[E — Geist reserves the animated StatusDot for deployments and forces everything else to
a Badge]`

Suggested reuse (non-exhaustive; extend by family, never by adding a hue):
`discovered/deduped` → neutral · `scored/queued` → progress-hollow · `processing/indexing` →
progress-filled · `applied/indexed/succeeded/completed` → success · `skipped/expired/stale` → dim ·
`needs_review/pending` → review · `failed/cancelled` → danger.

### 2.5 Score bands

`JobScore.normalized` is 0–100 and `UserPreferences.min_score` defaults to 70.

| Band | Range | Token | Verdict word | Where it appears |
|---|---|---|---|---|
| 0 | 0–39 | `--score-0` | Reject | ScoreBar fill, score cell |
| 1 | 40–59 | `--score-1` | Weak | " |
| 2 | 60–69 | `--score-2` | Borderline | " |
| 3 | 70–84 | `--score-3` | Apply | " |
| 4 | 85–100 | `--score-4` | Strong | " |

This is a **one-hue ordinal ramp in the accent hue**, deliberately *not* a red→green gradient.
Reasons: (a) `dataviz` forbids reusing reserved status colors for a magnitude scale, and a
red→amber→green score bar is exactly that; (b) score is a magnitude, and magnitude is a sequential
job — one hue, more-is-lighter-on-dark; (c) it keeps green/amber/red unambiguously meaning
*outcome*, not *quality*. `[E — dataviz color-formula: sequential = one hue; status colors are
reserved]`

All five steps clear 2:1 against `--bg-surface` (measured: 2.12 / 2.87 / 3.50 / 4.76 / 7.85), which
is the `dataviz` ordinal floor for the step nearest the surface. The **numeric score is always
rendered next to the bar in mono**, so the color is never the only channel.

The `min_score` threshold is drawn as a **1px vertical tick in `--score-threshold`** on the track at
`min_score%`. That single mark is what turns a decorative bar into an instrument.

### 2.6 Measured contrast ratios (WCAG 2.1)

Computed, not estimated. Normal text needs **4.5:1** (AA), large text (≥18.66px bold / ≥24px) and
non-text UI boundaries need **3:1**.

**Dark theme — text on surfaces**

| Token | Hex | on `--bg-base` | on `--bg-surface` | on `--bg-elevated` | on `--bg-overlay` | AA |
|---|---|---|---|---|---|---|
| `--fg-primary` | `#F2F4F8` | 17.54 | 16.61 | 15.69 | 15.01 | ✅ AAA |
| `--fg-secondary` | `#A8B0BD` | 8.84 | 8.37 | 7.91 | 7.56 | ✅ AAA |
| `--fg-muted` | `#7E8795` | 5.32 | 5.04 | 4.78 | 4.55 | ✅ AA |
| `--fg-disabled` | `#4F5764` | 2.65 | 2.51 | 2.37 | 2.27 | n/a — disabled text is exempt (WCAG 1.4.3) |

**Dark theme — status & accent as text on `--bg-surface` / `--bg-base`**

| Token | Hex | on surface | on base | AA |
|---|---|---|---|---|
| `--st-neutral` | `#8A93A1` | 5.90 | 6.23 | ✅ |
| `--st-dim` | `#5C6472` | 3.07 | 3.24 | ⚠️ non-text only — see rule below |
| `--st-progress` | `#4D9BFF` | 6.49 | 6.85 | ✅ |
| `--st-success` | `#35C67C` | 8.29 | 8.76 | ✅ |
| `--st-review` | `#F0A93B` | 9.10 | 9.61 | ✅ |
| `--st-danger` | `#F05C56` | 5.55 | 5.86 | ✅ |
| `--st-rejected` | `#C4636E` | 4.67 | 4.93 | ✅ |
| `--st-interview` | `#E97BC8` | 7.08 | 7.48 | ✅ |
| `--st-offer` | `#2FE0A6` | 10.74 | 11.34 | ✅ |
| `--accent-text` | `#8E93FF` | 6.77 | 7.14 | ✅ |
| `--accent` | `#5B5FD6` | 3.53 | 3.73 | ✅ 3:1 for fills/borders — **never used as text** |

> **`--st-dim` rule (binding):** at 3.07:1 it is legal as a dot, a border, or a badge edge, but it
> **must not be used as label text**. `abandoned` and `ghosted` badges therefore render their label
> in `--fg-muted` with a `--st-dim` dot. This is deliberate: those rows should read as receded, and
> the dim tone is carried by the mark, not the words.

**Dark theme — text on accent fill**

| Pair | Ratio | AA |
|---|---|---|
| `#FFFFFF` on `--accent` `#5B5FD6` | 5.18 | ✅ |
| `#FFFFFF` on `--accent-hover` `#6165DE` | 4.75 | ✅ |
| `#FFFFFF` on `--accent-active` `#5155C4` | 6.09 | ✅ |
| `--fg-on-status` `#08090C` on `--st-offer` `#2FE0A6` | 11.34 | ✅ |

> This is why hover **brightens by only one step**. A larger brightening (e.g. `#7074F2`) drops
> white text to 3.84:1 and fails AA in the hover state. `hover:brightness(1.08)` is the CSS form and
> it survives an accent swap; verify any new accent against the same three ratios before shipping it.

**Light theme — text on surfaces**

| Token | Hex | on `--bg-base` `#F4F5F7` | on `--bg-surface` `#FFFFFF` | AA |
|---|---|---|---|---|
| `--fg-primary` | `#14161A` | 16.60 | 18.11 | ✅ AAA |
| `--fg-secondary` | `#4C545F` | 7.02 | 7.66 | ✅ AAA |
| `--fg-muted` | `#6E7684` | 4.20 | 4.58 | ⚠️ AA on white only |
| `--fg-disabled` | `#A2A9B4` | 2.17 | 2.37 | exempt |

> **Light-theme muted rule (binding):** `--fg-muted` is AA on `--bg-surface` (4.58) but **4.20 on
> `--bg-base`**. In light theme, muted metadata may only be placed on a `--bg-surface` (white) plane
> — which is where all of it lives anyway, because tables and cards are surfaces. Metadata rendered
> directly on the page ground in light theme must step up to `--fg-secondary`. Enforce with a lint
> rule on `text-muted` usage outside a `Card`/`Table` subtree.

**Light theme — status as text on `#FFFFFF` / `#F4F5F7`**

| Token | Hex | on white | on base | AA |
|---|---|---|---|---|
| `--st-neutral` | `#5D6673` | 5.81 | 5.33 | ✅ |
| `--st-progress` | `#1F6FEB` | 4.63 | 4.25 | ✅ on white |
| `--st-success` | `#127C48` | 5.25 | 4.81 | ✅ |
| `--st-review` | `#96590A` | 5.63 | 5.16 | ✅ |
| `--st-danger` | `#C0322C` | 5.62 | 5.16 | ✅ |
| `--st-rejected` | `#9A3F49` | 6.60 | 6.05 | ✅ |
| `--st-interview` | `#A0369C` | 6.00 | 5.50 | ✅ |
| `--st-offer` | `#0A7A5B` | 5.32 | 4.88 | ✅ |
| `#FFFFFF` on `--accent` `#4F53C9` | — | 6.15 | — | ✅ |

**Verdict: the system is WCAG 2.1 AA-clean in both themes** for every text pair that ships, with the
two documented exceptions above (`--st-dim` is non-text; `--fg-disabled` is exempt).

### 2.7 Rules for using color

1. **Solid accent appears once per view.** If two elements on screen are `background: var(--accent)`,
   one is wrong. Secondary actions get `--accent-subtle` + `--accent-border` + `--accent-text`.
2. **Hover changes color, not position, for 95% of the UI.** `transition: background-color 140ms`
   is the default hover. Movement is reserved for cards and buttons. `[E — shipwright uses
   `transition-colors` 65× vs `transition-transform` 32×]`
3. **Status color never appears without a label or an `aria-label`.** Ever. §12.
4. **Never colorize text with a chart series color.** Series hues live on marks; labels wear
   `--fg-*`. `[E — dataviz]`
5. **The focus-ring offset color must be the token of the surface the element sits on**, not a
   hardcoded value, or the ring reads wrong on elevated surfaces. `[E — shipwright]`
6. **No gradients as fills.** The single sanctioned gradient in the product is the 1px accent
   hairline described in §7 (`linear-gradient(90deg, transparent, var(--accent), transparent)`) on
   the top edge of dialogs, sheets, and the command palette. `[E — shipwright's mega-menu hairline]`

---

## 3. Typography

### 3.1 Font stack

```css
:root {
  --font-sans:
    ui-sans-serif, system-ui,
    -apple-system, BlinkMacSystemFont,          /* macOS: SF Pro Text          */
    "Segoe UI Variable Text", "Segoe UI",       /* Windows 11 / 10             */
    Roboto, "Helvetica Neue", Arial, sans-serif;

  --font-display: var(--font-sans);             /* aliased on purpose — see below */

  --font-mono:
    "Geist Mono", ui-monospace,
    "SF Mono", "Cascadia Mono", "Segoe UI Mono",
    "Roboto Mono", Menlo, Consolas, monospace;
}
```

**Why system-first for the sans face.** In Electron there is no network, but there *is* a font-load
frame: a bundled webfont paints on the second frame at best and can cause a FOUT on cold start. The
platform UI face is in memory before the window exists, matches OS text rendering and hinting
exactly, and costs zero bytes. Given P1, that trade is not close. `[J, forced by the instant-feel
requirement]`

**Why the mono face is bundled.** Geist Mono (OFL-1.1) ships as a **local woff2 in
`desktop/src/assets/fonts/`**, referenced with `@font-face { font-display: block; }` — from local
disk it resolves in the same frame, and `block` is safe precisely because there is no network. The
mono face is where this product's typographic identity lives (§0.3), so it must be the same glyphs
on every machine. macOS SF Mono and Windows Cascadia Mono have very different advance widths;
letting the platform choose would make every table column reflow between operating systems.

**`--font-display` is aliased to `--font-sans` on purpose.** Tag every heading `font-display` from
day one even though it renders identically. Swapping in a display face later becomes a one-line
change with zero markup churn. `[E — shipwright does exactly this]`

**Font features.** On `body`:

```css
font-feature-settings: "cv01", "cv02", "ss01";  /* no-ops on system faces; active if a face is swapped in */
font-variant-numeric: tabular-nums;             /* see 3.5 */
text-rendering: optimizeLegibility;
-webkit-font-smoothing: antialiased;
-moz-osx-font-smoothing: grayscale;
```

### 3.2 Root size and the density decision

**`html { font-size: 16px }`. Do not change it.**

The shipwright research documents a 14px root as the density mechanism — it silently rescales
Tailwind's entire rem system by 87.5% at once `[E]`. **We reject it**, for three reasons:
(a) Radix/shadcn primitives compute internal offsets in rem and would rescale invisibly;
(b) Electron zoom levels multiply on top of it, so a user at 110% zoom lands on non-integer device
pixels and 1px hairlines disappear; (c) a spec whose numbers are all 87.5% of the number written in
the class name is unreviewable. We buy the same density **explicitly**, via a small type scale and
small component heights specified below. `[J — dissent from the evidence, stated openly]`

### 3.3 Type scale

Every size is a token. Nothing outside this table ships.

| Token | rem | px | Weight | Line-height | Tracking | Used for |
|---|---|---|---|---|---|---|
| `--text-micro` | 0.6875 | **11** | 590 | 1.0 | +0.06em | caps eyebrows, mono chips, keycaps, axis ticks |
| `--text-mini` | 0.75 | **12** | 400/500 | 1.25 | +0.005em | badges, table meta cells, sidebar counts, log lines |
| `--text-sm` | 0.8125 | **13** | 400/500 | 1.35 | 0 | **the workhorse** — table cells, descriptions, list rows |
| `--text-base` | 0.875 | **14** | 400/500 | 1.5 | 0 | body copy, buttons, inputs, form labels, menu items |
| `--text-md` | 0.9375 | **15** | 500/590 | 1.4 | −0.005em | row titles, card titles, dialog titles |
| `--text-lg` | 1.125 | **18** | 590 | 1.25 | −0.011em | section headings, panel titles |
| `--text-xl` | 1.375 | **22** | 590 | 1.2 | −0.018em | page title (h1) |
| `--text-2xl` | 1.75 | **28** | 590 | 1.1 | −0.022em | stat-tile value |
| `--text-hero` | 2.5 | **40** | 650 | 1.05 | −0.028em | the one hero figure on the Dashboard |

13px and 14px carry ~90% of all text in the product. `[E — shipwright's real body scale is
9/10/11/12/13/15px, with 13px used 100× on a single page]`

### 3.4 Weights

Variable-font axis values where available, integer fallbacks otherwise:

| Name | Variable | Fallback | Used for |
|---|---|---|---|
| Regular | 400 | 400 | body, cell values, descriptions |
| Medium | 510 | 500 | row titles, buttons, active nav, table headers |
| Semibold | 590 | 600 | headings, stat values, dialog titles |
| Bold | 650 | 700 | hero figure only |

Fractional weights are deliberate: text visually thickens on dark backgrounds, and 510/590 keep
medium from reading as semibold. `[E — Linear ships 400/510/590/680]` Where the platform face is
non-variable, the integer fallback applies and the tokens absorb the difference.

### 3.5 The monospace rule (binding)

**Set in `--font-mono`:**

- every **id**: application id, posting `external_id`, `confirmation_id`, `correlation_id`,
  `dedupe_key`, `content_hash`, `sha256`, `session_id`, `checkpoint.key`
- every **URL** and file path, including `apply_url` and `storage_key`
- every **timestamp** and **duration**: `submitted_at`, `duration_seconds`,
  `avg_application_seconds`, relative times inside a column (`2d ago`), log-line timestamps
- every **number in a table column**: score, salary min/max, token counts, attempt counts, page
  counts, row counts, percentages
- every **stat-tile value** and the **hero figure**
- **log/terminal output** and any raw JSON/HTML fragment
- **keyboard keycaps** in the palette and the shortcut sheet

**Not mono:** prose, descriptions, company names, job titles, locations, button labels, fact text,
resume bullets, cover-letter body, error *messages* (the error *code* is mono).

Because the mono face carries figure alignment, `font-variant-numeric: tabular-nums` is set globally
on `body` as a belt-and-braces default for the rare proportional number in a column.
**Exception, per `dataviz`:** the hero figure and stat-tile values use
`font-variant-numeric: proportional-nums` — tabular figures make a large standalone number look
loose. `[E — dataviz marks-and-anatomy: proportional for big numbers, tabular only in columns]`

### 3.6 Label recipes

Exactly two micro-label forms exist. Nothing else. `[E — shipwright]`

```css
/* A. Caps eyebrow — section headers, group labels, table section rules */
.label-caps {
  font: 590 var(--text-micro)/1 var(--font-sans);
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--fg-muted);
}

/* B. Inset mono chip — a machine VALUE, recessed below its card so it reads as data, not a button */
.chip-mono {
  display: inline-flex; align-items: center; gap: 6px;
  height: 20px; padding: 0 6px;
  border: 1px solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-inset);            /* darker than the card it sits in — this is the trick */
  font: 400 var(--text-micro)/1 var(--font-mono);
  color: var(--fg-secondary);
}
```

`text-transform: uppercase` is rationed: **the caps eyebrow is its only use in the product.** No
uppercase buttons, no uppercase badges, no uppercase table headers — headers are Title-case noun
phrases (`Last activity`, `Score`, `Applied at`). `[E — Geist table header rule; shipwright uses
uppercase 22× on an entire page]`

### 3.7 Prose and copy rules

- Titles: **Title case**, no trailing period.
- Descriptions: **sentence case**, must carry *new* information — never restate the title.
- CTA labels: **Title case, verb + noun** (`Add Source`, `Retry Application`, `Clear Filter`).
- Never `N/A`, `null`, `None`, or an empty cell. Unknown is an **em-dash `—`** in `--fg-muted`.
- Relative time in cells (`2d ago`); absolute ISO-8601 timestamp in `title` and in the tooltip.
- Numbers ≥ 10,000 in stat tiles compact to `12.9K` / `4.2M`; numbers in table cells never compact.
`[E — Geist empty-state and table copy rules; dataviz stat-tile contract]`

---

## 4. Spacing, sizing, radius, elevation

### 4.1 Spacing scale — 4px base

```css
--space-0: 0;      --space-px: 1px;
--space-0-5: 2px;  --space-1: 4px;    --space-1-5: 6px;  --space-2: 8px;
--space-2-5: 10px; --space-3: 12px;   --space-4: 16px;   --space-5: 20px;
--space-6: 24px;   --space-8: 32px;   --space-10: 40px;  --space-12: 48px;
--space-16: 64px;
```

**Defaults to reach for first** — these cover most of the product:

| Situation | Value |
|---|---|
| Icon → label gap | **6px** |
| Gap between inline controls in a toolbar | **8px** |
| Gap between form fields | **12px** vertical |
| Card interior padding | **16px** (compact) / **20px** (detail panel) |
| Table cell padding | **0 12px** |
| Grid gutter between cards / stat tiles | **12px** |
| Page horizontal padding | **24px** |
| Page top padding, below the header | **16px** |
| Heading → its content | **12px**; between sections **24px** |

Never use a value that is not on the scale. `6px` and `10px` are on it; `7px`, `9px`, `14px`, `18px`
are not.

### 4.2 Component heights (binding)

Every interactive control in the product is one of these heights. There are no others.

| Component | `sm` | `md` (default) | `lg` |
|---|---|---|---|
| Button (text) | 26px | **30px** | 34px |
| Button (icon-only) | 26×26 | **30×30** | 34×34 |
| Input / Select trigger | 26px | **30px** | 34px |
| Textarea | min 66px (3 rows @13px) | — | — |
| Checkbox / Radio | 14×14 | **16×16** | — |
| Switch | 16×28 | **18×32** | — |
| Badge | 18px | **20px** | 22px |
| StatusDot | 6px | **8px** | 10px |
| Tab | — | **30px** | — |
| Sidebar item | — | **28px** | — |
| Table header row | — | **32px** | — |
| **Table body row** | 30px (compact) | **36px** | 44px (comfortable) |
| Log line | — | **20px** | — |
| Command-palette row | — | **40px** | — |
| Toolbar / filter bar | — | **44px** | — |
| Page header | — | **56px** | — |
| Titlebar | — | **38px** | — |
| StatTile | — | **92px** | — |

Horizontal padding: buttons `sm 8px / md 10px / lg 14px`; icon-only buttons are square with the icon
optically centered. Icon sizes: **14px** inside `sm`/`md` controls and table cells, **16px** inside
`lg` controls and sidebar items, **20px** in empty states; **1.5px stroke** at 14–16px, **1.75px** at
20px, `stroke-linecap: round`. `lucide-react` is the icon library; import icons individually so they
tree-shake — never `import * as Icons`.

### 4.3 Radius

```css
--radius-xs:   3px;    /* checkbox, tiny inset chips, score-bar ends           */
--radius-sm:   5px;    /* mono chips, badges, skeletons, keycaps               */
--radius-md:   7px;    /* buttons, inputs, selects, tabs, menu items, rows     */
--radius-lg:   10px;   /* cards, panels, table containers, popovers, tooltips  */
--radius-xl:   14px;   /* dialogs, sheets, command palette                     */
--radius-full: 9999px; /* status dots, avatars, pills, filter chips            */
```

Effectively a two-value system — 7px for controls, 10px for containers — plus `full` for pills.
`[E — shipwright collapses to 10.5px containers + full pills + 5.25px chips]`

**Shape separates button tiers, not just color:** primary is `--radius-md`; a chip-style filter
control is `--radius-full`. `[E]`

**Nested-radius rule:** inner radius = outer radius − inset. A 10px card with 4px inner padding gets
a 6px inner radius. Never nest two elements at the same radius.

### 4.4 Elevation model — stated plainly

> **ApplicantOS encodes elevation with backgrounds and borders. Shadows appear only on layers that
> genuinely float above the page, and only as a secondary cue.**

Five levels. Each is a *pair*: background step + border step. Never move one without the other.

| Level | Background | Border | Shadow | What lives here |
|---|---|---|---|---|
| **0 — Chrome** | `--bg-chrome` | `--border-subtle` (outer edge only) | none | titlebar, sidebar, status bar |
| **1 — Page** | `--bg-base` | — | none | the content region ground |
| **2 — Surface** | `--bg-surface` | `--border-default` | `--shadow-raised` | cards, table containers, panels, inputs |
| **3 — Elevated** | `--bg-elevated` | `--border-strong` | `--shadow-raised` | selected rows, featured card, sticky table header |
| **4 — Overlay** | `--bg-overlay` | `--border-strong` | `--shadow-float` | popovers, dropdowns, tooltips, context menus, toasts |
| **5 — Modal** | `--bg-overlay` | `--border-strong` | `--shadow-dialog` | dialogs, sheets, command palette |

`--shadow-raised` is deliberately almost invisible; the part doing the work is the
**`inset 0 1px 0 rgb(255 255 255 / .03)` top hairline**, which fakes a lit top edge. That inset is
what sells a card as raised on a dark field — the drop shadow barely registers. `[E — shipwright;
Linear reaches the same conclusion by making dark shadows ~5× darker than light]`

**Dark shadow alphas are ~5× their light counterparts** (`0.45–0.72` vs `0.06–0.20`). Reusing
light-mode shadow values in dark is why most dark dashboards look flat. `[E — Linear:
`0 7px 24px rgba(0,0,0,.06)` light vs `0 7px 32px rgba(0,0,0,.35)` dark; Kokonut multiplies alpha
4–5× in its `dark:` variants]`

**Level 3 is not "level 2 with a lighter background."** Hovering a row applies `--state-hover` (an
alpha layer, transient, additive). *Selecting* a row is structural: `--bg-elevated` +
`--border-strong` + a 2px accent left rail. Do not conflate them.

**Backdrop blur is rationed to two places:** the command-palette scrim and the dialog scrim, both
`blur(3px)` over `rgb(6 7 10 / 0.55)`. Never on a card, a tile, or the sidebar. `[E — shipwright
uses backdrop-blur 5× across an entire site; Kokonut's own glass component documents that it
degrades with multiple instances]`

### 4.5 Borders

One width: **1px**. Three colors (§2.2). Weight is expressed as luminance, never as `border-width`;
`border-2` does not exist in this product. `[E — shipwright: three border colors, one width]`

Intra-card dividers use `--state-divider` (`rgb(255 255 255 / .05)`), one step *below*
`--border-subtle`, so internal rules never compete with the card's own edge. `[E — shipwright uses
`border-white/5` for card footers rather than a border token]`

---

## 5. Layout

### 5.1 The shell

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ●●●   ApplicantOS          ⟨ dry run ⟩                    ⌘K   ⚙   ◐   – □ ✕     │ 38  titlebar   bg-chrome
├────────────────┬─────────────────────────────────────────────────────────────────┤
│                │                                                                 │
│  SIDEBAR       │  PAGE HEADER                                              56px  │     bg-base
│  232px         │  ─────────────────────────────────────────────────────────────  │
│  bg-chrome     │  TOOLBAR / FILTER BAR                                     44px  │
│                │  ─────────────────────────────────────────────────────────────  │
│                │                                                                 │
│                │  CONTENT  (max-width 1440px, centered, 24px side padding)       │
│                │                                                                 │
│                │                                                                 │
├────────────────┴─────────────────────────────────────────────────────────────────┤
│ ● idle · last run 06:12 · 47 applications · 3 need review          v0.4.1  24px  │     bg-chrome
└──────────────────────────────────────────────────────────────────────────────────┘
```

**The shell never unmounts.** Titlebar, sidebar, page header container, toolbar container, and status
bar live in the root route; only `<Outlet/>` swaps. This is the single largest contributor to
"navigation feels instant." `[E — TanStack Router persistent-shell guidance]`

### 5.2 Titlebar (Electron)

**Frameless on both platforms, with platform-native control affordances.**

```ts
// electron/window.ts
new BrowserWindow({
  show: false,
  backgroundColor: '#08090C',                // must equal --bg-chrome of the persisted theme
  width, height, minWidth: 1120, minHeight: 720,
  titleBarStyle: 'hidden',                   // both platforms
  trafficLightPosition: { x: 14, y: 12 },    // macOS: vertically centers in a 38px bar
  titleBarOverlay: {                         // Windows/Linux: native min/max/close, our colors
    color: '#08090C', symbolColor: '#A8B0BD', height: 38,
  },
  webPreferences: { preload, contextIsolation: true, sandbox: true,
                    nodeIntegration: false, spellcheck: false },
});
win.once('ready-to-show', () => win.show());
```

- **Height 38px**, background `--bg-chrome`, bottom border `--border-subtle`.
- **macOS:** 78px left inset reserved for traffic lights; the wordmark starts at x=92.
- **Windows/Linux:** 138px right inset reserved for `titleBarOverlay`; our right-side controls stop
  at `calc(100% - 138px)`.
- The whole bar is `-webkit-app-region: drag`; every interactive child sets
  `-webkit-app-region: no-drag`. Forgetting this on a button is the #1 frameless-window bug.
- Contents, left→right: wordmark (13px, weight 590, `--fg-secondary`) · **safety chip** ·
  spacer · `⌘K` hint button · settings icon · theme toggle.
- **Safety chip (P7).** When `dry_run === true` **or** `auto_apply_enabled === false`, a
  non-dismissible 20px pill renders: `--st-review` dot + `Dry run` / `Auto-apply off` in
  `--text-mini`, background `rgb(240 169 59 / .12)`, border `rgb(240 169 59 / .35)`. Clicking it
  opens Settings → Safety. It is not hideable and has no close affordance.
- **White-flash prevention is three-layered, all required** `[E]`: `show:false` +
  `ready-to-show`; `backgroundColor` on the window; and an inline
  `<style>html,body{background:#08090C}</style>` in `index.html` *above* any stylesheet link.
  Restore saved bounds from `electron-store` **before** constructing the window.

### 5.3 Sidebar

| Property | Value |
|---|---|
| Expanded width | **232px** |
| Collapsed width | **52px** (icons only, labels in tooltips after 400ms) |
| Background | `--bg-chrome` (darker than content — §0.1) |
| Right border | `--border-subtle` |
| Padding | `8px` block, `8px` inline |
| Item height | **28px**, radius `--radius-md`, padding `0 8px`, icon 16px, gap 8px |
| Item font | `--text-sm` / weight 400; active weight 510 |
| Group label | `.label-caps`, 24px tall, `0 8px`, 12px top margin |
| Toggle | `⌘.` / `Ctrl+.`, or the chevron button in the sidebar footer |

**Structured by lifecycle, not recency** `[E — Arc's model]`:

```
  ── WORK ─────────────
  ▸ Dashboard        g d
  ▸ Review queue     g r      ⟨3⟩       ← count badge, --st-review when > 0
  ▸ Applications     g a
  ▸ Postings         g p
  ── LIBRARY ──────────
  ▸ Knowledge        g k
  ▸ Resumes          g s
  ── SYSTEM ───────────
  ▸ Runs             g n
  ▸ Analytics        g i
  ▸ Logs             g l
  ── (footer, pinned bottom) ──
  ▸ Settings         g ,
  ▸ ⟨collapse⟩
```

- **Active item:** background `--accent-subtle`, text `--fg-primary`, icon `--accent-text`, plus a
  **2px × 14px accent rail** on the left edge, vertically centered, `--radius-full`. No bold, no
  scale, no glow.
- **Hover:** `--state-hover` background only. 140ms `background-color` transition. No movement.
- **Count badge:** right-aligned, `--text-mini`, mono, `--fg-muted`; turns `--st-review` and gains a
  `rgb(240 169 59 / .14)` pill when the review queue is non-empty.
- **Chrome recedes, it does not shrink.** Icons are 16px with **no colored backgrounds**; label
  color is `--fg-secondary`, not primary. `[E — Linear's redesign: reduce nav contrast, strip
  colored icon backgrounds, "don't compete for attention you haven't earned"]`
- The sidebar never scrolls: there are exactly ten destinations.

### 5.4 Page header

56px tall, sticky, `--bg-base` with a `--border-subtle` bottom rule that appears only once the
content region has scrolled (`scrollTop > 0`), cross-faded over 140ms.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Applications                                     ⟨ ⌕ Search ⟩ ⟨ + New ⟩  │
│  47 total · 3 need review · last sync 2m ago                              │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Title:** `--text-xl` (22px), weight 590, `--fg-primary`, `font-display`.
- **Subtitle:** one line, `--text-sm`, `--fg-muted`. It carries live counts, so it is a
  `setQueryData` consumer and must be `tabular-nums` to avoid reflow on every tick. `[E]`
- **Actions:** right-aligned, max **one primary + two secondary/icon**. The primary is the only
  solid-accent element on the page.
- **No breadcrumbs.** Detail routes put the parent link in the back affordance (`⌘[`) and put the
  entity identity in the title.

### 5.5 Toolbar / filter bar

44px, sits directly under the page header, background `--bg-base`, bottom border `--border-subtle`,
sticky with the header. Left→right: search input (`sm`, 240px, grows to 320px on focus over 140ms),
filter chips, spacer, view controls (density toggle, column picker), result count in mono
`--fg-muted`.

**Filters are chips, not selects** — `--radius-full`, 24px, `--text-mini`. Inactive: `--bg-surface`
+ `--border-default` + `--fg-secondary`. Active: `--accent-subtle` + `--accent-border` +
`--accent-text`, with a `×` at 11px. `[E — shipwright's active filter pill is one of only three
solid/tinted accent uses on its whole page]`

**The 2px background-fetch bar lives here**, pinned to the bottom edge of the toolbar
(§7 IndeterminateBar). Never an overlay, never a content replacement.

### 5.6 Content region and grid

| Property | Value |
|---|---|
| Background | `--bg-base` |
| Max width | **1440px**, centered (`margin-inline: auto`) |
| Side padding | 24px |
| Top padding | 16px; bottom padding 48px |
| Scroll container | the content region itself, `scrollbar-gutter: stable both-edges` |
| Grid | 12 columns, 12px gutter, `minmax(0, 1fr)` tracks |

**Standard column spans**

| Pattern | Spans |
|---|---|
| KPI row | 4 stat tiles × 3 cols (or 3 × 4 cols) |
| Chart + side list | chart 8, list 4 |
| List + detail (master–detail) | list `minmax(420px, 1fr)`, detail `minmax(480px, 620px)` |
| Full-width table | 12 |
| Settings form | content 8, help rail 4 |

**Tables ignore `max-width: 1440px`** and run to the content-region edges; a 1440px cap on a
40-column data grid wastes a widescreen monitor. Reading-oriented pages (Settings, Onboarding,
Application detail body) cap at **760px** measure.

**`scrollbar-gutter: stable both-edges` on every scroll container is mandatory.** Windows Electron
uses classic non-overlay scrollbars, so a route with overflow is 15px narrower than one without;
without the gutter, every navigation shifts the layout. `[E]`

---

## 6. Motion

### 6.1 The governing rule

> **Motion is rationed by frequency (P5). If the user triggers it more than ~20×/day, it does not
> animate.** `[E — Raycast ships no palette open/close animation; Linear's `--speed-highlightFadeIn`
> is `0s`, meaning user-initiated feedback appears on the same frame with zero ramp-up and only the
> *removal* fades]`

### 6.2 Tokens

```css
:root {
  /* durations */
  --dur-0:    0ms;     /* feedback appears on the same frame — see 6.4     */
  --dur-1:  100ms;     /* press / release                                   */
  --dur-2:  140ms;     /* hover, color change, focus ring                   */
  --dur-3:  180ms;     /* dropdown, popover, tooltip, badge swap  (enter)   */
  --dur-3-out: 120ms;  /* the same, exiting (~2/3 of enter)                 */
  --dur-4:  240ms;     /* dialog enter                                      */
  --dur-4-out: 160ms;  /* dialog exit                                       */
  --dur-5:  320ms;     /* sheet / drawer slide                              */
  --dur-6:  400ms;     /* toast enter (exit 200ms)                          */

  /* easings — two families with assigned jobs */
  --ease-out:      cubic-bezier(0.23, 1, 0.32, 1);      /* interaction feedback   */
  --ease-out-quad: cubic-bezier(0.25, 0.46, 0.45, 0.94);/* color/hover            */
  --ease-in-out:   cubic-bezier(0.77, 0, 0.175, 1);     /* on-screen movement     */
  --ease-drawer:   cubic-bezier(0.32, 0.72, 0, 1);      /* anything that resizes/slides */
  --ease-exit:     cubic-bezier(0.4, 0, 1, 1);          /* exits only             */
}
```

`cubic-bezier(0.23,1,0.32,1)` for interaction feedback and `cubic-bezier(0.32,0.72,0,1)` for
sliding/resizing are the two curves that do 90% of the work. **`ease-in` is banned on UI** — it
delays the initial movement, the exact moment the user is watching, so a 300ms `ease-in` dropdown
*feels* slower than a 300ms `ease-out` one. `[E — emil-design-eng; Kokonut's house curve is the same
Vaul `[0.32,0.72,0,1]`]`

### 6.3 Copy-pasteable Framer Motion transitions

```ts
// desktop/src/lib/motion.ts
import type { Transition } from 'framer-motion';

export const T = {
  /** Press feedback. Must be faster than hover. */
  press:      { duration: 0.10, ease: [0.23, 1, 0.32, 1] } as Transition,
  /** Hover / color. */
  hover:      { duration: 0.14, ease: [0.25, 0.46, 0.45, 0.94] } as Transition,
  /** Popover, dropdown, tooltip, context menu — entering. */
  pop:        { duration: 0.18, ease: [0.23, 1, 0.32, 1] } as Transition,
  /** …and exiting. Exits are ~2/3 of enters and use the exit curve. */
  popOut:     { duration: 0.12, ease: [0.4, 0, 1, 1] } as Transition,
  /** Dialog. */
  dialog:     { duration: 0.24, ease: [0.23, 1, 0.32, 1] } as Transition,
  dialogOut:  { duration: 0.16, ease: [0.4, 0, 1, 1] } as Transition,
  /** Sheet / drawer — the iOS curve. */
  sheet:      { duration: 0.32, ease: [0.32, 0.72, 0, 1] } as Transition,
  sheetOut:   { duration: 0.22, ease: [0.32, 0.72, 0, 1] } as Transition,
  /** Toast — slightly slower and softer than UI, on purpose. */
  toast:      { duration: 0.40, ease: [0.32, 0.72, 0, 1] } as Transition,
  toastOut:   { duration: 0.20, ease: [0.4, 0, 1, 1] } as Transition,
  /** Height / width auto-resize (accordion, expanding answer, log detail). */
  resize:     { duration: 0.30, ease: [0.32, 0.72, 0, 1] } as Transition,

  /** Springs — only for state-driven indicators and gestures. */
  /** Small snappy UI: segmented-control pill, tab indicator, switch knob. */
  springSnap:  { type: 'spring', stiffness: 400, damping: 30 } as Transition,
  /** Panels and drawers driven by state rather than duration. */
  springPanel: { type: 'spring', stiffness: 300, damping: 30, mass: 0.8 } as Transition,
  /** Large/heavy surfaces. */
  springCard:  { type: 'spring', stiffness: 220, damping: 28, mass: 1 } as Transition,
  /** Gestures (sheet drag-to-dismiss). Apple form — easier to reason about. */
  gesture:     { type: 'spring', duration: 0.5, bounce: 0.2 } as Transition,
} as const;

/** Canonical variants. Enter from below, exit upward — directional, never flickery. */
export const V = {
  fade:   { initial: { opacity: 0 }, animate: { opacity: 1 }, exit: { opacity: 0 } },
  popIn:  { initial: { opacity: 0, transform: 'scale(0.97) translateY(-2px)' },
            animate: { opacity: 1, transform: 'scale(1) translateY(0px)' },
            exit:    { opacity: 0, transform: 'scale(0.98) translateY(-2px)' } },
  listRow:{ initial: { opacity: 0, transform: 'translateY(8px)' },
            animate: { opacity: 1, transform: 'translateY(0px)' },
            exit:    { opacity: 0, transform: 'translateY(-8px)' } },
  dialog: { initial: { opacity: 0, transform: 'scale(0.97)' },
            animate: { opacity: 1, transform: 'scale(1)' },
            exit:    { opacity: 0, transform: 'scale(0.985)' } },
  sheetR: { initial: { transform: 'translateX(100%)' },
            animate: { transform: 'translateX(0%)' },
            exit:    { transform: 'translateX(100%)' } },
} as const;
```

Three details in there that are load-bearing:

1. **Full `transform` strings, not `x`/`y`/`scale` shorthands.** Motion's shorthands drive a
   `requestAnimationFrame` loop on the main thread and drop frames when the renderer is busy — which,
   in this app, is exactly when the WebSocket is fanning out and a virtualized table is
   re-rendering. The full string takes the WAAPI path. `[E]`
2. **Never `scale(0)`.** Entrances start at `0.97`; nothing in the real world appears from nothing.
   `[E — emil-design-eng]`
3. **Exit is ~2/3 of enter, and only exits use `--ease-exit`.** `[E]`

### 6.4 What animates, and what must not

**Zero animation — `--dur-0`, feedback on the same frame** `[E — Linear's `highlightFadeIn: 0s`]`:

| Surface | Why |
|---|---|
| Command palette open/close | Used dozens of times a day; Raycast ships none |
| Route/page transitions | Pure added latency on the most frequent action in the app |
| Sidebar navigation, tab switches, segment switches | Same |
| **Anything triggered by a keyboard shortcut** | The action must land before the key is released |
| Row selection, checkbox toggle, filter chip toggle | High-frequency |
| Live-updating numbers (stat tiles, counts, badges) | Swap the digits; mono + tabular means zero reflow. **No count-up animation, ever.** |
| Status badge changing value over WebSocket | It is data changing, not a UI event |
| Virtualized row enter/exit | `AnimatePresence` and virtualization actively fight: AP keeps exiting nodes mounted while the virtualizer wants them gone, costing a forced layout per row `[E]` |
| Table sort / column resize | Layout properties; never animated |

**Animated:**

| Surface | Spec |
|---|---|
| Button press | `transform: scale(0.97)`, `T.press` (100ms) |
| Card / row hover *on cards only* | `translateY(-1px)` + border step, `T.hover` |
| Dropdown / popover / context menu | `V.popIn` + `T.pop` / `T.popOut`, `transform-origin: var(--radix-popper-transform-origin)` |
| Tooltip | `V.popIn` at `scale(0.97)`, 125ms; **skip delay and animation entirely for the 2nd+ tooltip within 300ms** — `data-instant` sets `transition-duration: 0ms` `[E — emil-design-eng]` |
| Dialog | `V.dialog` + `T.dialog`; scrim `V.fade`; `transform-origin: center` (modals are the documented exception to origin-awareness) |
| Sheet (right drawer) | `V.sheetR` + `T.sheet`; drag-to-dismiss uses `T.gesture` with velocity dismissal at `>0.11 px/ms` `[E]` |
| Toast | enter `T.toast` from `translateY(100%)`, exit `T.toastOut`; **CSS transitions, not keyframes**, so rapid stacking retargets instead of restarting `[E — Sonner]` |
| Accordion / expanding answer / log-detail | `height: 0 → auto` with `T.resize`; opacity exits faster (0.2s) than height (0.3s) `[E — Kokonut]` |
| Tab / segmented indicator | `layoutId` + `T.springSnap` — **only** for a single indicator element, never for the tab content |
| Onboarding step transition | `V.listRow` + `T.pop`, one of the few genuinely rare surfaces |
| Status dot pulse (in-flight statuses only) | 2.4s `ease-in-out` infinite ring scale 1→1.9 + opacity 0.5→0; CSS keyframes, `will-change: transform, opacity` |
| Skeleton shimmer | 1.4s linear infinite; disabled under reduced motion |

**Properties that must never be animated:** `width`, `height` (except the sanctioned
`height: auto` accordion), `top/left/right/bottom`, `margin`, `padding`, `gap`, `border-width`,
`font-size`. Animate `transform` and `opacity`. `transition: all` is banned — name your properties.
`[E]`

**`layout` / `layoutId` policy:** allowed on **exactly one element per view** (a tab indicator, a
segmented-control pill). Banned on table rows, virtualized rows, cards in a grid, and any list over
~20 items — shared-layout animations run a FLIP cycle with forced synchronous layout reads on every
participant. `[E — Vercel's dashboard tab animation dropped frames for exactly this reason]`

### 6.5 List stagger

- **Cap: 30ms per item, max 6 items, max 180ms total.** `[E — Kokonut's dashboard-safe band is
  0.04–0.07s; bento-grid's `staggerChildren: 0.15 + delayChildren: 0.3` would take a 12-row list
  over 2 seconds and is rejected]`
- Stagger is allowed **only** on: the Dashboard KPI row on first mount, Onboarding step content, and
  the Review-queue card list on first mount.
- Stagger is **forbidden** on: any table body, any virtualized list, any list that re-renders on a
  WebSocket event, search results, and the command palette.
- Stagger never blocks interaction — rows are clickable at `opacity: 0`.

### 6.6 Page transitions

**There are none.** `<Outlet/>` swaps synchronously. The persistent shell means the sidebar, header,
and toolbar do not remount, so the swap reads as instant rather than abrupt.

The only sanctioned exception: if a route's data is genuinely uncached and takes >400ms, the content
region cross-fades to a skeleton over 120ms (opacity only, no transform). With the caching strategy
in §10 this path should be unreachable outside first launch.

### 6.7 `prefers-reduced-motion` policy

**Reduced motion means less and gentler motion, not zero feedback.** Opacity and color transitions
survive — they carry the comprehension cue ("this changed"). Transforms and position changes go.
`[E — emil-design-eng; Motion's own `MotionConfig reducedMotion="user"` preserves opacity and
backgroundColor while disabling transform and layout animations]`

Three layers, all required:

```tsx
// 1. Global — auto-disables transform + layout animations, preserves opacity/color
<MotionConfig reducedMotion="user">{children}</MotionConfig>
```

```tsx
// 2. Bespoke branches — remove the transform, don't just shorten it
const reduce = useReducedMotion();
const y = reduce ? 0 : 8;
const sheetX = reduce ? '0%' : '100%';   // sheet fades in place instead of sliding
```

```css
/* 3. CSS backstop for everything Motion doesn't own */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
  /* …but explicitly re-enable the comprehension cues: */
  [data-motion-safe-color] { transition-duration: 140ms !important; }
  .skeleton { animation: none !important; background: var(--state-track); }
  .status-dot-pulse::after { animation: none !important; opacity: 0.35; }
}
```

**Strictest part, copied deliberately:** purely decorative keyframes (`shimmer`, `pulse-ring`) are
**defined inside `@media (prefers-reduced-motion: no-preference)`**, so they do not exist at all for
users who opt out — rather than being defined and then overridden. `[E — shipwright]`

The in-flight status dot degrades to a **static filled dot at 35% opacity plus its text label**,
which preserves the "still moving" signal without motion.

---

## 7. Component specs

**Legend.** `shadcn` = install the shadcn/ui primitive and re-skin it with our tokens.
`shadcn+` = shadcn primitive with a mandatory custom wrapper. `custom` = build from scratch.

Two shared behavior classes go on everything interactive. Compose them; do not reinvent them.
`[E — shipwright's `.lift` / `.pressable`]`

```css
.pressable {                       /* transform ONLY, 100ms — press must feel instant */
  transition: transform var(--dur-1) var(--ease-out);
}
.pressable:active { transform: scale(0.97); }

.lift {                            /* for cards and card-like tiles only */
  transition: transform var(--dur-2) var(--ease-out),
              border-color var(--dur-2) var(--ease-out),
              box-shadow var(--dur-2) var(--ease-out);
}
@media (hover: hover) and (pointer: fine) {
  .lift:hover { transform: translateY(-1px); border-color: var(--border-strong); }
}
/* Disabled/archived drops BOTH classes so it structurally cannot respond. [E — shipwright] */
```

**Universal focus ring.** One spec, everywhere:

```css
:where(button, a, input, textarea, select, [tabindex]):focus-visible {
  outline: 2px solid var(--focus-ring);
  outline-offset: 2px;
  border-radius: inherit;   /* Chromium follows border-radius on outline — we ship on Chromium */
}
```

Never `:focus` — always `:focus-visible`. Never remove the ring without replacing it. The ring's
apparent offset color is whatever surface the element sits on, which is why we use `outline-offset`
rather than a `ring-offset-color` token: it cannot go stale. `[E — shipwright's offset-color pitfall]`

---

### 7.1 Button — `shadcn`

**Anatomy** `[ leading icon 14px ] [ 6px ] [ label ] [ 6px ] [ trailing icon / kbd hint ]`

| Variant | Background | Border | Text | Radius | Hover | Rule |
|---|---|---|---|---|---|---|
| **primary** | `--accent` | none | `--fg-on-accent`, 510 | `md` | `--accent-hover` + `--shadow-accent` | **One per view.** The only solid accent. |
| **secondary** | `--bg-surface` | `--border-default` | `--fg-primary` | `md` | bg `--bg-elevated`, border `--border-strong` | Default for everything else |
| **ghost** | transparent | none | `--fg-secondary` | `md` | bg `--state-hover`, text `--fg-primary` | Toolbars, table row actions, icon buttons |
| **outline-accent** | `--accent-subtle` | `--accent-border` | `--accent-text` | `md` | `--accent-subtle` → 0.18 alpha | A second-tier accent action; use instead of a second primary |
| **danger** | `rgb(240 92 86 / .12)` | `rgb(240 92 86 / .38)` | `--st-danger` | `md` | bg → 0.18 alpha | Destructive. Never a solid red fill. |
| **chip** | `--bg-surface` | `--border-default` | `--fg-secondary` | `full` | `--state-hover` | Filters only |

**Sizes** `sm 26px / md 30px / lg 34px`; padding `8/10/14px`; font `--text-sm` at `sm`,
`--text-base` at `md`/`lg`, weight 510.

**States**
- `:hover` — `background-color` transition only, `--dur-2`. Primary uses `filter: brightness(1.08)`
  *in addition to* the token so it survives an accent change. `[E]`
- `:active` — `.pressable` (`scale(0.97)`, 100ms) plus `--accent-active` for primary.
- `:focus-visible` — universal ring.
- `[disabled]` — `opacity: 0.45`, `pointer-events: none`, `.pressable`/`.lift` removed.
- **loading** — the label stays in place, the leading icon slot cross-fades to a 14px spinner
  (`filter: blur(2px)` on the outgoing glyph over 200ms masks the swap `[E]`), width is pinned to
  the pre-loading measured width so nothing reflows, and the button becomes `aria-busy="true"`
  `[disabled]`. **Never replace the label with "Loading…".**

**Usage rule.** Every button has a verb. If a screen has two solid-accent buttons, downgrade one to
`outline-accent`. Submit-type buttons in dialogs bind `⌘Enter`, shown as a trailing `kbd`.

---

### 7.2 Input — `shadcn`

**Anatomy** `[ 8px ] [ leading icon 14px ] [ 6px ] [ value ] [ trailing slot ] [ 8px ]`

| Property | Value |
|---|---|
| Height | `sm 26 / md 30 / lg 34` |
| Background | `--bg-inset` (recessed *below* its card — that is what makes it read as a field) |
| Border | `--border-default`; hover `--border-strong`; focus `--accent-border` + ring |
| Radius | `--radius-md` |
| Font | `--text-base` (mono when the field holds an id, URL, number, or path — §3.5) |
| Placeholder | `--fg-muted` |
| Invalid | border `rgb(240 92 86 / .5)`, message below in `--text-mini` `--st-danger`, `aria-invalid` |
| Disabled | bg `--bg-surface`, text `--fg-disabled`, border `--border-subtle` |

**Search input variant:** leading `⌕` icon, trailing `Esc` keycap while focused with content,
240px→320px width transition on focus (`width` is animated here **only** because it is a single
isolated element with no siblings to reflow — this is the one documented exception to the
no-width-animation rule, and it is `--dur-2` `ease-out-quad`). `[J]`

**Usage rule.** Labels sit above at `--text-mini` weight 510 `--fg-secondary`, 6px gap. Help text
below at `--text-mini` `--fg-muted`. Error text replaces help text; never both.

---

### 7.3 Select — `shadcn`

Trigger is identical to Input (same heights, background, border, radius) plus a 14px chevron in
`--fg-muted`. Content panel is **level 4**: `--bg-overlay`, `--border-strong`, `--shadow-float`,
`--radius-lg`, 4px padding, `V.popIn` + `T.pop`,
`transform-origin: var(--radix-select-content-transform-origin)`.

Item: 28px, `--radius-md`, `0 8px`, `--text-base`. Highlighted: `--state-hover`. Selected: a 14px
check in `--accent-text` on the right, text weight 510. Group label: `.label-caps`, 24px.

**Usage rule.** ≤ 8 options → Select. > 8 → Combobox (`shadcn` Command inside a Popover) with
type-ahead. > 40 → the command palette scoped to that field. Never a native `<select>`.

---

### 7.4 Textarea — `shadcn`

Min height 66px (3 rows at 13px), `--text-sm`, `line-height: 1.5`, resize `vertical` only, same
background/border/focus as Input. Character counter bottom-right in `--text-micro` mono
`--fg-muted`, turning `--st-review` at 90% of `max_length` and `--st-danger` at 100%.

**Auto-grow variant** (essay answers in the Review queue): grows to a max of 12 rows then scrolls;
height changes use `T.resize`. This is the second and last sanctioned height animation.

---

### 7.5 Checkbox / Radio — `shadcn`

| Property | Checkbox | Radio |
|---|---|---|
| Size | 16×16 (`sm` 14) | 16×16 |
| Radius | `--radius-xs` (3px) | `full` |
| Unchecked | bg `--bg-inset`, border `--border-strong` |  same |
| Checked | bg `--accent`, border `--accent`, 10px white check / 6px white dot | same |
| Indeterminate | bg `--accent`, 8×1.5px white bar | n/a |
| Hover | border `--accent-border` | same |
| Disabled | bg `--bg-surface`, border `--border-subtle`, glyph `--fg-disabled` | same |

Transition: `background-color, border-color` at `--dur-2`. The glyph does **not** animate in — it
appears on the same frame (P5/`--dur-0`). Label is `--text-base`, 8px gap, and the whole
label+control is one 24px-tall click target.

**Usage rule.** Table row selection uses a checkbox that only becomes visible on row hover, focus,
or when any row is selected; otherwise the cell shows the row index in mono `--fg-muted`. `[J]`

---

### 7.6 Badge — `shadcn+`

**Anatomy** `[ 6px ] [ StatusDot 8px (optional) ] [ 6px ] [ label ] [ 6px ]`

Height 20px (`sm` 18, `lg` 22), `--radius-full`, `--text-mini` weight 510, `--fg-*` or status text
color, background = the status color at **12%** (14% for `review`/`danger`/`interview`), border
= the status color at **22%**.

The **only** solid-filled badge in the product is `offer`: background `--st-offer`, text
`--fg-on-status`, no border. `[J — §2.4 rule 4]`

**Usage rule.** A badge always carries a text label. A badge is never clickable — if it needs to be
clickable it is a `chip` Button. Status vocabulary comes from §2.4 and nowhere else.

---

### 7.7 StatusDot — `custom`

```
   ●        static, filled           terminal + settled  (confirmed, failed, rejected, interview, offer)
   ○        static, 1.5px ring       terminal + waiting  (draft, ready, submitted)
   ◌        static, dashed ring      dead                (abandoned, ghosted)
   ◉))      filled + pulsing ring    in flight           (preparing, submitting, needs_review)
```

Size 8px (`sm` 6, `lg` 10). Pulse: an `::after` ring, same color, `scale(1) → scale(1.9)` and
`opacity 0.5 → 0`, **2.4s `ease-in-out` infinite**, `will-change: transform, opacity`, keyframes
defined inside `@media (prefers-reduced-motion: no-preference)`.

**Binding rules** `[E — Vercel StatusDot]`
1. Animate **only** non-terminal states. Terminal states are static.
2. **Never** pair a StatusDot with a separate spinner. The dot *is* the spinner.
3. `aria-hidden="true"` whenever adjacent text names the state; otherwise supply
   `role="img" aria-label="Status: Needs review"`.
4. Reserved for `ApplicationStatus`. Everything else gets a Badge. `[E]`

---

### 7.8 Card — `shadcn+`

```
┌─ 1px --border-default ────────────────────────────┐   bg --bg-surface
│  [16px]                                           │   radius --radius-lg
│   ┌ header ─────────────────────── [actions] ┐    │   shadow --shadow-raised
│   │ title 15/510  ·  subtitle 13 --fg-muted  │    │
│   └──────────────────────────────────────────┘    │
│   [12px]                                          │
│   body                                            │
│   [12px]                                          │
│   ─ --state-divider ──────────────────────────    │   footer rule
│   footer: mono 11 --fg-muted                      │
│  [16px]                                           │
└───────────────────────────────────────────────────┘
```

| State | Spec |
|---|---|
| default | `--bg-surface` + `--border-default` + `--shadow-raised` |
| interactive (hover) | `.lift` → `translateY(-1px)` + `--border-strong` |
| selected / featured | `--bg-elevated` + `--border-strong` + `box-shadow: 0 0 48px -22px var(--accent-glow)` |
| disabled / archived | `--bg-surface` at 40% + `--border-subtle` + `opacity: 0.6` + `cursor: not-allowed`, **`.lift` and `.pressable` removed** `[E]` |

Padding 16px (compact) / 20px (detail panel). Card footers use the `border-top: 1px solid
var(--state-divider)` + 12px padding-top + mono 11px pattern for metadata. `[E — shipwright]`

**Usage rule.** A card must have a title. Never nest a card inside a card — use a divider and a
`.label-caps` group heading instead.

---

### 7.9 StatTile — `custom`

```
┌──────────────────────────────┐  92px tall, --radius-lg
│ Applications submitted       │  label   13 / 400 / --fg-muted, sentence case
│ 1,284          ▲ 12%         │  value   28 / 590 / --font-mono / proportional-nums
│                              │  delta   12 / 510 / direction-colored + ▲▼ glyph
│  ▁▂▃▅▆▇▆▅▇█                  │  sparkline 12 pts, 28px tall, full-bleed to the tile edges
└──────────────────────────────┘
```

- **Label** on top, value below, delta inline-right of the value, sparkline pinned to the bottom
  edge with 0 horizontal padding (it bleeds to the card's inner radius).
- **Delta** = signed, always names its comparison period in the tooltip
  (`vs. previous 7 days`). Color is `direction × whether up is good`: submissions up = `--st-success`;
  failures up = `--st-danger`; **never green-for-up unconditionally**. Always paired with a ▲/▼
  glyph so it is not color-alone. `[E — dataviz]`
- **Sparkline**: 2px line, `--fg-muted` at 55% for history, the final segment + end dot in
  `--accent-text`; no axes, no gridlines, no labels. Area wash only if the tile has no delta.
- **Value never animates.** No count-up. It swaps. `tabular-nums` is *off* here
  (`proportional-nums`) because a large standalone number looks loose in tabular figures. `[E]`
- Skeleton state: label bar `h-3 w-24`, value bar `h-7 w-20`, sparkline bar `h-7 w-full`.

**Usage rule.** 3 or 4 per row, never 5+. If a tile has no delta and no sparkline, it is not a tile
— put it in the page-header subtitle instead. `[E — dataviz: "is it even a chart?"]`

---

### 7.10 Table / DataGrid row — `custom` (TanStack Table + TanStack Virtual)

**Do not use shadcn's `Table`.** It is a styled `<table>`; we need virtualization, column pinning,
and 5,000-row scroll. Build on `@tanstack/react-table` for state + `@tanstack/react-virtual` for
rendering, with `role="grid"` semantics on divs.

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│ ☐  Company / Role                 Status        Score   Provider   Applied   ⋯     │ 32px header
├────────────────────────────────────────────────────────────────────────────────────┤
│ ☐  Stripe                         ◉)) Submitting  ▓▓▓▓░ 82  greenhouse  2m ago  ⋯  │ 36px row
│    Senior Backend Engineer                                                          │
│ ☐  Vercel                         ○  Submitted    ▓▓▓░░ 74  lever       1h ago  ⋯  │
└────────────────────────────────────────────────────────────────────────────────────┘
```

| Property | Value |
|---|---|
| Header row | 32px, sticky, `--bg-elevated`, bottom border `--border-default`, `--text-mini` weight 510 `--fg-secondary`, Title case |
| Body row | **36px** (compact 30 / comfortable 44), cell padding `0 12px` |
| Row separator | **none by default.** Rows separate by hover fill and spacing. `[E — Geist: borders off by default is the premium tell]` Add `border-bottom: 1px solid var(--border-subtle)` only when a column is multi-line. |
| Two-line row | 44px, title `--text-sm` 510 `--fg-primary`, subtitle `--text-mini` `--fg-muted` |
| Hover | `background: var(--state-hover)` — **color only, no transform**, `--dur-2` `[E]` |
| Focused (keyboard) | universal focus ring, `outline-offset: -2px` so it stays inside the row |
| Selected | `--bg-elevated` + a 2px `--accent` left rail, full row height |
| Loading-more | rows keep rendering; `IndeterminateBar` in the toolbar |
| Numeric / id / date cells | `--font-mono`, `--text-mini`, right-aligned for numbers, `--fg-secondary` |
| Unknown value | `—` in `--fg-muted` — **never `N/A`, `null`, or blank** `[E]` |
| Row actions | `⋯` ghost icon button, visible on hover/focus only, opens a DropdownMenu |

**Fixed geometry (anti-layout-shift, binding):** the body wrapper carries
`min-height: calc(var(--row-h) * var(--page-size))` so a 12-row page and a 50-row page occupy the
same box. `[E]`

**Usage rule.** Every table has: a sticky header, a keyboard-focusable row (`j`/`k`), an empty
state that distinguishes "no data" from "no results" (§7.16), and an `aria-rowcount`. Sort
indicators are a 12px chevron in `--accent-text` next to the header label. No zebra striping, ever.

---

### 7.11 Tabs — `shadcn+`

Underline style, not pill style, for **detail-view lenses** (Timeline / Answers / Documents /
Artifacts / Activity):

```
  Timeline   Answers   Documents   Artifacts   Activity
  ─────────                                              2px --accent, layoutId indicator
─────────────────────────────────────────────────────    1px --border-subtle rail
```

- Tab height 30px, `--text-base`, inactive `--fg-secondary`, active `--fg-primary` weight 510.
- Indicator: 2px, `--accent`, `layoutId="tab-indicator"`, `T.springSnap`. **This is one of the two
  places `layoutId` is permitted.**
- Tab **content does not animate.** It swaps. `[E — P5]`
- Hover: `--fg-primary`, `--dur-2`, color only.

**Segmented control** (density toggle, chart range picker) uses the pill form: 26px tall track in
`--bg-inset`, 2px padding, active segment `--bg-elevated` + `--shadow-raised` with the same
`layoutId` + `T.springSnap` treatment.

**Usage rule.** Tabs are lenses over *one object* (Docker Desktop's container view is the model) —
switching a tab must never lose the selected row or the scroll position, and must never change the
route. `[E]`

---

### 7.12 Dialog — `shadcn`

| Property | Value |
|---|---|
| Width | `sm 400 / md 520 / lg 680`, `max-width: calc(100vw - 96px)` |
| Background | `--bg-overlay`, border `--border-strong`, radius `--radius-xl`, `--shadow-dialog` |
| Top hairline | `linear-gradient(90deg, transparent, var(--accent), transparent)`, 1px, `inset-x-0 top-0`, at 55% opacity — **the one sanctioned gradient** `[E]` |
| Scrim | `rgb(6 7 10 / 0.55)` + `backdrop-filter: blur(3px)`, `V.fade` `T.dialog` |
| Header | 20px padding, title `--text-md` 590, description `--text-sm` `--fg-secondary` |
| Body | 20px padding, max-height `60vh`, scrolls with `scrollbar-gutter: stable` |
| Footer | 16px padding, top border `--state-divider`, right-aligned, `Cancel` (secondary) then primary |
| Motion | `V.dialog` + `T.dialog` in / `T.dialogOut` out, `transform-origin: center` |

**Usage rule.** Dialogs are for *decisions* (confirm destructive action, pick a resume variant,
resolve one review field). Anything that is a *workspace* is a Sheet. `Esc` closes; `⌘Enter`
submits; focus moves to the first focusable on open and returns to the trigger on close.

---

### 7.13 Sheet — `shadcn`

Right-side drawer, width `480px` (`lg` 640px, `xl` 60vw), full height minus titlebar,
`--bg-overlay`, left border `--border-strong`, `--shadow-dialog`, radius `--radius-xl` on the left
corners only. Enters with `V.sheetR` + `T.sheet` (the iOS `[0.32,0.72,0,1]` curve).

- **Drag-to-dismiss** from the left edge: pointer capture on drag start, damping past the boundary,
  and velocity dismissal at `> 0.11 px/ms` regardless of distance. Ignore additional touch points
  once a drag begins. `[E]`
- Header 56px with title + `✕`; body scrolls; footer sticky.
- Under reduced motion the sheet **fades in place** (no translate).

**Usage rule.** Sheets hold a working surface that keeps the list visible behind it — Application
detail, Posting detail, Review resolution. If the user needs the list *and* the detail
simultaneously, use the master–detail layout instead of a Sheet.

---

### 7.14 Tooltip — `shadcn`

`--bg-overlay`, `--border-strong`, `--radius-md`, padding `4px 8px`, `--text-mini`, `--fg-primary`,
`--shadow-float`. Max width 280px. Delay **400ms** on first hover; **0ms and no animation** for any
subsequent tooltip within 300ms of the last one closing (`data-instant` →
`transition-duration: 0ms`). `[E — emil-design-eng]`

Motion: `V.popIn` at `scale(0.97)`, 125ms,
`transform-origin: var(--radix-tooltip-content-transform-origin)`.

Keyboard shortcut hints render inside tooltips as `kbd` chips (§7.22).

**Usage rule.** Tooltips explain, they do not contain. No links, no buttons, no wrapping paragraphs.
Every icon-only button **must** have one, and the tooltip text must equal the `aria-label`.

---

### 7.15 Toast — `sonner`

Use `sonner`, not shadcn's deprecated `toast`. Bottom-right stack, max 3 visible, width 356px,
`--bg-overlay` + `--border-strong` + `--shadow-float` + `--radius-lg`, 12px padding,
`--text-sm`.

- Enter `translateY(100%) → 0` with `T.toast` (400ms); exit `T.toastOut` (200ms).
- **CSS transitions, not keyframes**, so rapid stacking retargets smoothly. `[E — Sonner]`
- Timers pause when the window is hidden or the stack is hovered.
- Swipe-right to dismiss; enter and exit share a direction so the gesture reads as natural.
- Variants: `error` (`--st-danger` 14px icon), `warning` (`--st-review`), `success`
  (`--st-success`), `info` (`--accent-text`). Action slot on the right: one `ghost sm` button.

**Usage rule (binding).** Toasts fire for exactly four things:
1. a mutation **failed** (with a Retry action),
2. an action is **undoable** (`Application archived · Undo`, 8s),
3. a **background job finished** while the user was elsewhere (`Run finished · 12 applied`),
4. a **file was written to disk** (resume export) with a Reveal action.
Successful optimistic mutations **do not toast** — the UI already showed the result. `[J]`

---

### 7.16 Skeleton — `shadcn+`

`background: var(--state-track)`, `--radius-sm`, `animation: shimmer 1.4s linear infinite` defined
inside `@media (prefers-reduced-motion: no-preference)`, `aria-hidden="true"`.

**Every skeleton must declare explicit `width`/`height` matching the real content** so there is zero
layout shift on fill. `[E — Geist]` Canonical sizes:

| Slot | Size |
|---|---|
| Page title | `h-6 w-56` |
| Page subtitle | `h-4 w-full max-w-md` |
| Table row | `h-9 w-full` × page-size (36px rows) |
| Stat tile | `h-[92px] w-full` |
| Card | `h-40 w-full` `--radius-lg` |
| Avatar | `h-6 w-6` `--radius-full` |

**Gating (binding).** A skeleton renders only after **400ms** of `isPending`, and once rendered
stays for at least **500ms**. Implement once as `useDelayedFlag(isPending, { delay: 400, minDuration: 500 })`.
A 150ms skeleton flash is worse than no skeleton. `[E]`

**Never use a skeleton as an empty state.** `[E — Geist hard rule]`

---

### 7.17 EmptyState — `custom`

```
              ⬡  20px icon, --fg-muted, in a 40px --bg-inset circle
        No applications yet                    --text-md / 590 / --fg-primary
   Start a run and the agent will apply         --text-sm / --fg-secondary / max-w-[42ch]
   to matching postings overnight.
        [ Start a Run ]  [ Import Postings ]    1 primary + 1 secondary, MAX
```

**Two distinct empty states are mandatory per list — write both** `[E — Geist]`:

| Case | Title | Description | CTAs |
|---|---|---|---|
| **Blank slate** (no data ever) | `No applications yet` | Names the next action: *"Start a run and the agent will apply to matching postings overnight."* | `Start a Run` + `Import Postings` |
| **No results** (filters active) | `No applications match "senior backend"` — **quote the query verbatim** | *"Clear the filter to see all 47."* | `Clear Filters` (primary), nothing else |

CTAs are real `<button>`/`<a>` elements so tab order and roles work. Max one primary + one
secondary. Titles Title case; descriptions sentence case carrying new information.

**Binding suppression rule:** the empty state does **not** render while `isPending` is true and the
search box is empty. This kills the "No applications found" flash before data lands, which is the
single most common reason a fast app feels broken. `[E — Raycast's documented List rule]`

---

### 7.18 CommandPalette — `cmdk` via `shadcn` Command

```
┌─────────────────────────────────────────────────────────────┐  640px, --radius-xl
│ ⌕  apply to stripe                                     Esc  │  48px input, --bg-overlay
├─────────────────────────────────────────────────────────────┤  1px --state-divider
│  APPLICATIONS                                               │  .label-caps, 24px
│  ○  Stripe · Senior Backend Engineer      Submitted    ↵     │  40px row
│  ◉  Stripe · Staff Platform Engineer      Submitting   ↵     │
│  ACTIONS                                                    │
│  ▸  Start a run                                    ⌘ ⇧ R    │
│  ▸  Retry failed applications                       ⌃ R      │
│  GO TO                                                      │
│  →  Review queue                                     g r    │
└─────────────────────────────────────────────────────────────┘
   ↑↓ navigate   ↵ open   ⌘↵ open in new sheet   ⌃X delete
```

| Property | Value |
|---|---|
| Trigger | `⌘K` / `Ctrl+K` from anywhere, including inside inputs |
| Width | 640px, top-anchored at `18vh` |
| Surface | `--bg-overlay`, `--border-strong`, `--shadow-dialog`, accent top hairline |
| Scrim | `rgb(6 7 10 / 0.55)` + `blur(3px)` |
| **Motion** | **NONE.** Open and close are instant, both scrim and panel. `[E — Raycast]` |
| Input | 48px, transparent, `--text-md`, no border, bottom `--state-divider` |
| Row | 40px, `--radius-md`, 8px inline padding, icon 14px + label + right-aligned meta/kbd |
| Highlighted row | `--state-selected` (no transition — it must track arrow keys at key-repeat rate) |
| Filtering | **local fuzzy, in-memory, zero network in the loop** `[E — Raycast]` |
| Groups | `Applications`, `Postings`, `Actions`, `Go to`, `Recent` — in that fixed order |

**Every result row shows its own keyboard shortcut inline.** This is the only mechanism that
reliably graduates users from searching to muscle memory. `[E — Arc]`

**Action priority is encoded in modifiers** `[E — Raycast]`: `↵` primary · `⌘↵` secondary
(open in sheet) · `⌘⇧↵` tertiary · `Ctrl+X` destructive. The footer bar (28px, `--bg-inset`,
`--text-micro`) always shows the current row's available modifiers.

**Usage rule.** The palette is the *discoverable* path to every action; single-letter shortcuts are
the accelerator. **No action may exist only as a shortcut.** `[E]`

---

### 7.19 ScoreBar — `custom`

```
   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░│░░░░░  82        inline, table-cell form
   └── fill --score-3 ┘ └ threshold tick at min_score
```

| Property | Value |
|---|---|
| Track | 4px tall (`lg` 6px), `--state-track`, `--radius-full` |
| Fill | `--score-{band}` by §2.5, `--radius-full`, width = `normalized%` |
| Threshold tick | 1px × 8px, `--score-threshold`, absolutely positioned at `min_score%`, `z-index` above the fill |
| Value | `--font-mono`, `--text-mini`, `--fg-primary`, 8px to the right, `min-width: 3ch` |
| Inline width | 72px in a table cell; 100% in the detail panel |
| Transition | `width` **is not animated** in tables. In the detail panel only, on first paint, `width 400ms var(--ease-drawer)` from 0. |

**Detail-panel form** adds the `JobScore.breakdown` as a stacked horizontal component list: each
`ScoreComponent` is a row with `label · mono value · mini bar`, all in the same ordinal ramp, with
negative components shown as `--st-danger` text and a `−` prefix. The `verdict` renders as a Badge,
and `rationale` as `--text-sm` `--fg-secondary` prose beneath.

**Usage rule.** The bar never appears without its number. Color is never the only channel.

---

### 7.20 Timeline — `custom`

Vertical, for `ApplicationEvent[]` and `RunSession` history.

```
   ●───┐  09:41:02   Application created                          mono 11 --fg-muted
   │   └─ posting scored 82 · greenhouse                          13 --fg-secondary
   ●───┐  09:41:18   Resume tailored                    ⟨ v3 ⟩
   │   └─ 14 facts selected · 1,204 tokens
   ◉───┐  09:41:44   Submitting…                                  in-flight: pulsing
   │
   ○      —          Awaiting confirmation
```

| Property | Value |
|---|---|
| Rail | 1px `--border-default`, x = 4px (centered under an 8px dot) |
| Node | StatusDot (§7.7), 8px, colored by event kind |
| Row spacing | 16px between nodes; 4px between a node's title and its detail line |
| Time | `--font-mono`, `--text-micro`, `--fg-muted`, fixed 64px column, left-aligned |
| Title | `--text-sm`, weight 510, `--fg-primary` |
| Detail | `--text-mini`, `--fg-muted`, may contain mono chips |
| Future/pending nodes | hollow dot, `--fg-disabled` text, rail becomes `--border-subtle` |
| New event arriving | fades in over 180ms (`V.fade`); **no slide, no stagger** — events arrive live over the WebSocket and must not shuffle the list |

**Usage rule.** The timeline is bound to the selected entity, never to scroll position. (Skiper's
`Timeline calendar` is the right visual but its scroll-driven trigger must be rebound to state.)
`[E]`

---

### 7.21 LogStream — `custom` (the most performance-sensitive component in the app)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ⌕ regex|exact  [ .* ]   ↑↓ 3/47   ⟨ level ⟩ ⟨ source ⟩   ⟨ ts ⟩ ⟨ copy ⟩ ⟨ ⏸ ⟩│ 36px tool row
├──────────────────────────────────────────────────────────────────────────────┤
│ 09:41:02.113  INFO   pipeline      posting.scored posting_id=8f3a… total=82  │ 20px line
│ 09:41:02.331  DEBUG  browser       autofill.field label="Full name"          │
│ 09:41:04.902  ERROR  apply         submit_not_found provider=workday         │
│                              ⟨ 14 new lines ↓ ⟩                              │ pill, when detached
└──────────────────────────────────────────────────────────────────────────────┘
```

| Property | Value |
|---|---|
| Line height | **20px**, `--font-mono`, `--text-mini`, `white-space: pre`, no wrap |
| Columns | timestamp 96px `--fg-muted` · level 56px · logger 88px `--fg-secondary` · message `--fg-primary` |
| Level colors | DEBUG `--fg-muted` · INFO `--fg-secondary` · WARNING `--st-review` · ERROR `--st-danger` · CRITICAL `--st-danger` on a `rgb(240 92 86 / .10)` row wash |
| Container | `--bg-inset`, `--border-default`, `--radius-lg`, `contain: strict` |
| Row hover | `--state-hover`; click expands the JSON `payload` inline with `T.resize` |
| Search match | `rgb(91 95 214 / .28)` highlight; current match `--accent` fill with `--fg-on-accent` |
| Motion | **none on lines.** No `AnimatePresence`, ever. `[E]` |

**Behavior (binding):**
1. **Never a query.** The tail is a module-level ring buffer capped at **10,000 lines**, exposed via
   `useSyncExternalStore`, never persisted. `GET /api/v1/logs` is a separate paginated query for
   historical search only. `[E — an append-per-line query entry churns the cache and triggers the
   persister on every line]`
2. **rAF-coalesced flush.** All lines arriving within one frame become one state update. When the
   user is scrolled away from the bottom, drop the flush cadence to **250ms**.
3. **Stick-to-bottom only within 50px of the bottom.** Otherwise freeze and show the
   `N new lines ↓` pill, which calls `scrollToIndex(count-1, { align: 'end' })`. `[E — the threshold
   is what distinguishes a deliberate scroll-up from layout jitter]`
4. **It is a tool, not a feed** `[E — Docker Desktop]`: regex-or-exact search toggle,
   `Enter`/`Shift+Enter` to step matches with a `3/47` counter, level filter, source filter,
   timestamp toggle, copy-all, clear, and a follow/pause toggle.
5. `aria-live="polite" aria-atomic="false"` on the viewport so screen readers announce increments
   rather than re-reading everything. `[E]`

---

### 7.22 ProgressRing, IndeterminateBar, Kbd — `custom`

**ProgressRing** — determinate only, used for `knowledge.index_progress`, run progress, and the
resume shrink loop. 16/24/40px, 2px (16px) or 3px (24/40px) stroke, `stroke-linecap: round`, track
`--state-track`, fill `--accent`, starts at 12 o'clock. Center label in mono `--text-micro` at 40px
only. `transition: stroke-dashoffset 300ms var(--ease-out)`.
**Rule:** if the percentage is not genuinely known, this is the wrong component — use
`IndeterminateBar` or a StatusDot. `[E — Geist's four-way loading split]`

**IndeterminateBar** — 2px tall, full width, pinned to the bottom edge of the toolbar.
`background: --state-track`; a 30%-wide `--accent` segment travels left→right, 1.1s
`cubic-bezier(0.4,0,0.2,1)` infinite. Appears for any background refetch. **Content stays on screen
and stays interactive.** `[E — Raycast's loading bar lives in the chrome, never over the content]`

**Kbd** — 18px tall, min-width 18px, `--bg-inset`, `--border-default`, `--radius-xs`,
`--font-mono`, `--text-micro`, `--fg-secondary`, `0 4px`, centered. Chords render as separate
adjacent caps with a 2px gap (`g` `r`), modifier chords with no separator (`⌘` `K`). Platform
glyphs: `⌘⇧⌥⌃↵⌫` on macOS; `Ctrl Shift Alt Enter Backspace` spelled out on Windows.

---

### 7.23 Sidebar item — `custom`

Covered in §5.3. Restated as a component contract:

| State | Background | Text | Icon | Rail |
|---|---|---|---|---|
| default | transparent | `--fg-secondary` | `--fg-muted` | none |
| hover | `--state-hover` | `--fg-primary` | `--fg-secondary` | none |
| active | `--accent-subtle` | `--fg-primary` (510) | `--accent-text` | 2×14px `--accent` |
| active + hover | `--accent-subtle` at 0.16 | `--fg-primary` | `--accent-text` | same |
| collapsed | icon centered in 52px; label becomes a 400ms-delayed tooltip |

Height 28px, radius `--radius-md`, icon 16px, gap 8px, `--text-sm`.
`transition: background-color var(--dur-2) var(--ease-out-quad)` — **background only**. No
transform, no icon scale, no color fade on the rail (it appears on the same frame).

---

## 8. Screens

Route paths follow `desktop/src/routes/` in CONTRACTS §17. Every screen uses the same template:
persistent shell → 56px page header (title + one-line description) → optional 44px toolbar →
content. No breadcrumbs.

---

### 8.1 Onboarding — `/onboarding`

Full-window takeover: **no sidebar, no toolbar**, titlebar only. Content column 560px centered.
Backed by `GET /onboarding/steps` and `POST /onboarding/steps/{step}`.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ●●●   ApplicantOS                                                   ✕    │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│              ●───●───●───○───○───○        6 steps, 8px dots, 32px rail    │
│              Identity  Sources  …                                        │
│                                                                          │
│         Where should we learn about you?              22 / 590           │
│         Point ApplicantOS at anything that already                       │
│         describes your work. Nothing is scraped.       14 / --fg-secondary│
│                                                                          │
│   ┌────────────────────────────────────────────────────────────┐         │
│   │  ⬡  GitHub                                    ⟨ Connect ⟩  │  Card   │
│   │     Repos, languages, READMEs, commit activity             │         │
│   ├────────────────────────────────────────────────────────────┤         │
│   │  ⬡  Personal website          https://…       ⟨ Add ⟩      │         │
│   │  ⬡  Resume (PDF/DOCX)         drag or browse  ⟨ Upload ⟩   │         │
│   │  ⬡  Project folder            local path      ⟨ Choose ⟩   │         │
│   │  ⬡  LinkedIn export (.zip)    user-supplied   ⟨ Upload ⟩   │         │
│   └────────────────────────────────────────────────────────────┘         │
│                                                                          │
│   ⟨ Skip for now ⟩                              ⟨ Continue  ⌘↵ ⟩         │
└──────────────────────────────────────────────────────────────────────────┘
```

**Steps (fixed order):** Identity → Sources → Preferences → Safety → Index → Review.
The **Safety** step is not skippable and states `auto_apply_enabled` / `dry_run` in plain language
with the consequence spelled out (P7).
The **Index** step shows a live `ProgressRing` per source driven by
`knowledge.index_started|index_progress|index_finished` events, with per-source `IndexStatus`
badges, and permits `Continue` while indexing runs in the background.

**Inventory:** Card, Input, Button, FileDropzone (custom, wraps `POST /knowledge/sources`),
Checkbox, Select, Switch, ProgressRing, Badge, StepDots (custom), Toast (errors only).
**Motion:** the one screen where the full vocabulary is allowed — step content uses `V.listRow` +
`T.pop` with a 30ms stagger over ≤6 items. Seen once per install. `[E — P5's "rare/first-time" tier]`

---

### 8.2 Dashboard — `/` (the morning read)

```
┌ Overnight                                                    ⟨ Start a Run ⟩ ┐  primary = only solid accent
│ Last run finished 06:12 · 4h 31m · 12 applications · 3 need review           │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│    12                    ┌────────┬────────┬────────┬────────┐               │
│    applications          │Reviewed│ Failed │  Avg   │ Tokens │  4 StatTiles  │
│    submitted overnight   │   3    │   1    │  82    │ 214K   │               │
│    ▲ 4 vs. previous run  │  ▲1    │  ▬0    │  ▲3    │ ▼12%   │               │
│    hero figure, 40px     └────────┴────────┴────────┴────────┘               │
│                                                                              │
├───────────────────────────────────────────┬──────────────────────────────────┤
│  Needs review                        3    │  Pipeline · last 14 days         │
│  ┌──────────────────────────────────────┐ │  ┌────────────────────────────┐  │
│  │ ◉)) Datadog · Staff SRE              │ │  │  stacked bar, 14 columns   │  │
│  │     too_many_essays · 4 questions  ↵ │ │  │  discovered/scored/applied │  │
│  │ ◉)) Ramp · Backend Eng               │ │  └────────────────────────────┘  │
│  │     low_confidence · work auth     ↵ │ │                                  │
│  └──────────────────────────────────────┘ │  Funnel                          │
│  ⟨ Open review queue   g r ⟩              │  ▇▇▇▇▇▇▇▇ discovered   1,204     │
│                                           │  ▇▇▇▇▇    qualified      312     │
│  Recent activity                          │  ▇▇▇      applied         87     │
│  ┌── Timeline, last 12 events ──────────┐ │  ▇        interview        6     │
│  └──────────────────────────────────────┘ │  ▏        offer            1     │
└───────────────────────────────────────────┴──────────────────────────────────┘
```

- **Exactly one hero figure** (40px, mono, proportional-nums) and it answers "what did it do." `[E]`
- The KPI row is 4 StatTiles × 3 columns; the review card and pipeline chart split 6/6.
- The review card is the only card with an accent bloom
  (`box-shadow: 0 0 48px -22px var(--accent-glow)`) and only while `count > 0`.
- Everything on this screen is legible without a click (P2).

**Inventory:** StatTile ×4, HeroFigure (custom), Card, Timeline, Badge, StatusDot, Button,
BarChart (stacked, §11), FunnelBar (custom, §11), EmptyState (`No runs yet` → `Start a Run`).
**Data:** `GET /analytics/overview`, `/analytics/funnel`, `/analytics/timeseries?days=14`,
`GET /reviews?limit=3`, `GET /sessions?limit=1`. **Motion:** KPI row staggers 30ms on first mount
only; nothing else animates.

---

### 8.3 Applications list — `/applications`

```
┌ Applications                                        ⟨ ⌕ ⟩ ⟨ + Add Manually ⟩ ┐
│ 87 total · 3 need review · synced 2m ago                                     │
├──────────────────────────────────────────────────────────────────────────────┤
│ ⌕ senior backend   ⟨ Status ▾ ⟩ ⟨ Provider ▾ ⟩ ⟨ Score ≥70 ✕ ⟩   87 rows  ▤▥ │ 44 toolbar
│▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁│ 2px IndeterminateBar
├──────────────────────────────────────────────────────────────────────────────┤
│ ☐  Company / Role              Status         Score   Provider  Applied   ⋯  │ 32 header
│ ☐  Stripe                      ◉)) Submitting ▓▓▓▓░82 greenhouse  2m ago  ⋯  │ 36
│    Senior Backend Engineer                                                   │
│ ☐  Vercel                      ○  Submitted   ▓▓▓░░74 lever       1h ago  ⋯  │
│ ☐  Datadog                     ◉)) Needs rev. ▓▓▓▓▓91 greenhouse  3h ago  ⋯  │
│ ☐  Airtable                    ●  Rejected    ▓▓▓░░71 ashby       2d ago  ⋯  │
│ ☐  Notion                      ◌  Ghosted     ▓▓░░░64 lever      14d ago  ⋯  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two-line rows (44px) because company + role both matter. Row click opens the **detail Sheet**;
`⌘↵` opens the full detail route. Row hover prefetches `GET /applications/{id}` after 60ms.

**Inventory:** DataGrid (virtualized past 100 rows), Checkbox, StatusDot, Badge, ScoreBar,
DropdownMenu (row `⋯`), Input (search), Button (chip filters), IndeterminateBar, EmptyState ×2,
Sheet (detail), Skeleton (first launch only).
**Data:** `GET /applications?limit=50&offset=…` with `placeholderData: keepPreviousData`.
**Live:** `application.created|status_changed|submitted|needs_review` → `setQueryData`.

---

### 8.4 Application detail — `/applications/$id`

Master–detail: the list stays on the left at `minmax(420px, 1fr)`, detail at
`minmax(480px, 620px)`. Switching rows never remounts the detail shell.

```
┌ ◀  Stripe · Senior Backend Engineer                     ⟨ ⋯ ⟩ ⟨ Retry ⟩      ┐
│ ◉)) Submitting · greenhouse · score 82 · app_id 8f3ad2c1  ← mono chips        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Timeline │ Answers │ Documents │ Artifacts │ Activity      ← tabs, layoutId   │
│──────────┴─────────────────────────────────────────────────────────────────  │
│                                                                              │
│  ●── 09:41:02  Application created                                           │
│  ●── 09:41:18  Resume tailored          ⟨ v3 ⟩ ⟨ 14 facts ⟩ ⟨ 1,204 tok ⟩    │
│  ●── 09:41:31  Cover letter written     ⟨ when_required ⟩                    │
│  ◉── 09:41:44  Submitting…                                                   │
│  ○    —        Awaiting confirmation                                         │
│                                                                              │
│  ── Score breakdown ──────────────────────────────────────────────────────   │
│  Skill match          ▓▓▓▓▓▓▓░░  +28   Location        ▓▓▓░░░░░░  +9         │
│  Seniority            ▓▓▓▓▓░░░░  +18   Sponsorship     ─────────   0         │
│  Salary               ▓▓▓▓░░░░░  +15   Blocked company ─────────  −0         │
│  "Strong overlap on Go + distributed systems…"  ← rationale, 13/--fg-secondary│
└──────────────────────────────────────────────────────────────────────────────┘
```

**Tabs are lenses over one object** — switching never changes the route, never loses scroll. `[E]`

| Tab | Content |
|---|---|
| Timeline | `ApplicationEvent[]` (§7.20) + score breakdown (§7.19) |
| Answers | `answers` JSON as a two-column table: field label · answer · confidence (mono) · source chip. Confidence < `min_answer_confidence` is flagged `--st-review`. |
| Documents | ResumeVersion card (with `fact_ids` count and a Download button) + CoverLetter body |
| Artifacts | Screenshot grid from `GET /applications/{id}/artifacts`; click opens a lightbox Dialog. `browser_log` in a LogStream. |
| Activity | Filtered LogStream on `application_id` |

**Instant-open rule.** The detail is seeded from the list cache via `placeholderData` (never
`initialData` — the list DTO is a subset). Company, role, status, and score paint at frame 1; the
breakdown, description, and screenshots render **fixed-height** skeletons so nothing reflows when
the full payload lands. `[E]`

**Inventory:** Tabs, Timeline, ScoreBar (detail form), Badge, StatusDot, Card, Table, Button,
DropdownMenu, Dialog (lightbox), LogStream, Skeleton (fixed-height inline only).

---

### 8.5 Postings — `/postings`

Same table shell as Applications, different columns and a **discovery** action.

```
┌ Postings                                     ⟨ ⌕ ⟩ ⟨ Discover  ⌘⇧D ⟩          ┐
│ 1,204 discovered · 312 qualified · 47 queued · last poll 12m ago              │
├──────────────────────────────────────────────────────────────────────────────┤
│ ⌕  ⟨ Provider ▾ ⟩ ⟨ Status ▾ ⟩ ⟨ Score ≥70 ⟩ ⟨ Remote ⟩ ⟨ Posted ≤7d ⟩        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Title / Company            Score   Salary        Arrangement  Posted  Status  │
│ Staff Platform Eng         ▓▓▓▓▓91 $210–260K     remote       2d ago  queued  │
│ Stripe · greenhouse                                                           │
│ Senior Backend Eng         ▓▓▓▓░82 $180–220K     hybrid       4d ago  applied │
│ Vercel · lever                                                                │
│ Backend Engineer           ▓▓░░░58 —             onsite       9d ago  skipped │
│ Palantir · greenhouse                            ⚠ is_defense                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

- Salary renders as `$180–220K` in mono, or `—` when unknown. `[E]`
- Policy flags (`is_defense`, `is_startup`, blocked company/industry) render as `--st-review`
  micro-badges under the company line, and their presence explains a low score without a click.
- `Discover` opens a Dialog: providers (multi-select from `GET /settings/plugins`), keywords,
  locations, `posted_within_days`, limit → `POST /postings/discover`. It closes immediately and
  reports progress through `posting.discovered` events + the IndeterminateBar.
- Row action `Apply now` → `POST /postings/{id}/apply` with an optimistic status flip to `preparing`.

**Inventory:** DataGrid, ScoreBar, Badge, Dialog, Select (multi), Input, Button, EmptyState ×2.

---

### 8.6 Review queue — `/reviews` (the highest-stakes screen)

Card list, not a table: each item needs a decision, and decisions need context.

```
┌ Review queue                                              ⟨ Resolve All Safe ⟩┐
│ 3 items · oldest 4h ago · blocking 3 applications                             │
├──────────────────────────────────────────────────────────────────────────────┤
│ ┌ ◉)) Datadog · Staff SRE                    too_many_essays      4h ago  ┐   │
│ │  greenhouse · score 91 · app_id 8f3ad2c1                                │   │
│ │  ────────────────────────────────────────────────────────────────────   │   │
│ │  Q1  "Why do you want to work at Datadog?"            600 chars max     │   │
│ │      ┌────────────────────────────────────────────────────────────┐     │   │
│ │      │ Draft answer from your knowledge graph…                    │     │   │
│ │      └────────────────────────────────────────────────────────────┘     │   │
│ │      sources: ⟨ github/obsrv ⟩ ⟨ resume ⟩ ⟨ blog_post ⟩  conf 0.62 ⚠    │   │
│ │  Q2  "Describe a time you debugged a production incident."             │   │
│ │      …                                                                  │   │
│ │  ────────────────────────────────────────────────────────────────────   │   │
│ │  ⟨ Dismiss  ⌃X ⟩            ⟨ Save Draft ⟩   ⟨ Approve & Submit  ⌘↵ ⟩   │   │
│ └────────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **The card is expanded by default** for the first item and collapsed for the rest; `j`/`k` moves
  and auto-expands. No accordion animation on keyboard movement (P5).
- **Confidence is shown as a number**, never softened (P6). Below `min_answer_confidence` it gets a
  `--st-review` chip and the field gets a `--st-review` left rail.
- **Source chips are mandatory** on every generated answer, linking to the `KnowledgeDocument`.
- `Approve & Submit` is the primary; `Dismiss` is `danger` variant on `Ctrl+X` (P7).
- `Resolve All Safe` only ever resolves items whose every field is above the confidence threshold,
  and always opens a confirmation Dialog naming the exact count.
- Empty state here is a **good** state: `Nothing needs you` / *"The agent handled everything on its
  own. 12 applications submitted since 02:00."* with no CTA. `[J]`

**Inventory:** Card, Textarea (auto-grow), Badge, StatusDot, Button (primary/danger/ghost),
Tooltip, Dialog, EmptyState, Kbd.
**Data:** `GET /reviews` at `staleTime: 15_000` — the one query where a stale count is real harm.
**Live:** `application.needs_review` → `setQueryData` + sidebar badge.

---

### 8.7 Knowledge — `/knowledge`

Three panes: sources rail, graph canvas, inspector.

```
┌ Knowledge                                        ⟨ ⌕ ⟩ ⟨ + Add Source ⟩       ┐
│ 6 sources · 412 documents · 1,908 facts · 340 entities · indexed 12m ago       │
├───────────────┬──────────────────────────────────────────┬────────────────────┤
│ SOURCES       │  Entities · graph                        │ INSPECTOR          │
│ ● github      │                    ○ Kubernetes          │ Go                 │
│   indexed 12m │        ○ Go ───── ○ Distributed          │ technology         │
│ ● website     │       ╱   ╲          Systems             │ 47 mentions        │
│   indexed 1h  │   ○ obsrv  ○ gRPC                        │ first seen 2019    │
│ ◉ resume      │                                          │ ── Facts ────────  │
│   indexing 62%│   ○ Stripe ── ○ Backend Eng              │ • Built an obser-  │
│ ◌ linkedin    │                                          │   vability plat-   │
│   skipped     │  ⟨ kind ▾ ⟩ ⟨ depth 1 ▾ ⟩  340 / 500     │   form in Go…      │
│ ⚠ project_dir │                                          │   ⟨ github/obsrv ⟩ │
│   failed  ⟨↻⟩ │                                          │   impact 8 ✓verified│
└───────────────┴──────────────────────────────────────────┴────────────────────┘
```

- **The graph renders to `<canvas>`, never SVG or DOM.** `GET /knowledge/graph` returns up to 500
  nodes; a force layout of 500 DOM nodes is a guaranteed frame-rate failure. Node = 6px circle in
  the `EntityKind` categorical color (§11.3), edge = 1px `--border-default`, label shown only above
  a zoom threshold or on hover. Pan/zoom, no auto-rotation, no ambient motion.
- **Facts view** (toggle in the toolbar) is a virtualized table: fact text · kind · organization ·
  impact (mono) · confidence (mono) · `user_verified` checkbox · source chip. Editing calls
  `PATCH /knowledge/facts/{id}` optimistically.
- Source rows show `IndexStatus` badge + `last_error` in `--st-danger` `--text-mini` + a reindex
  button. Failed sources sort first.

**Inventory:** GraphCanvas (custom), DataGrid (facts), Card, Badge, StatusDot, ProgressRing,
Input, Select, Button, EmptyState, Sheet (add source).

---

### 8.8 Resumes — `/resumes`

```
┌ Resumes                                                    ⟨ + New Variant ⟩ ┐
│ 3 variants · 41 versions · default "Backend — modern"                        │
├─────────────────────────────┬────────────────────────────────────────────────┤
│ VARIANTS                    │  Backend — modern            v12 · 2d ago      │
│ ● Backend — modern  default │  template modern · 1 page · 18 bullets         │
│   12 versions               │  ┌──────────────────────────────────────────┐  │
│ ○ Platform / SRE            │  │                                          │  │
│   9 versions                │  │        PDF preview (lazy chunk)          │  │
│ ○ ATS plain                 │  │                                          │  │
│   20 versions               │  └──────────────────────────────────────────┘  │
│                             │  ⟨ Download ⟩ ⟨ Preview HTML ⟩ ⟨ Set Default ⟩ │
│ VERSIONS                    │  ── Facts used (14) ────────────────────────   │
│ v12  2d ago  Stripe · SRE   │  ⟨ obsrv platform ⟩ ⟨ gRPC migration ⟩ …       │
│ v11  3d ago  Vercel · BE    │  ── Reasoning ──────────────────────────────   │
│ v10  4d ago  manual         │  "Prioritized distributed-systems facts…"      │
└─────────────────────────────┴────────────────────────────────────────────────┘
```

- Every version lists its `fact_ids` as clickable chips that deep-link into Knowledge (P6).
- The PDF preview is **the one route-level code-split** in the app (`React.lazy` on the viewer);
  everything else ships in one chunk (§10.7).
- `POST /resumes/preview` drives a live preview in the New Variant sheet, debounced 400ms.

**Inventory:** Card, List (custom), Badge, Button, Tabs (PDF / HTML / JSON), Sheet, Skeleton
(preview only), Toast (download written to disk).

---

### 8.9 Runs — `/sessions`

```
┌ Runs                                              ⟨ Stop  ⌃S ⟩ ⟨ Start a Run ⟩┐
│ 1 running · 47 total · avg 3h 12m · avg 41s per application                   │
├──────────────────────────────────────────────────────────────────────────────┤
│ ◉)) Running · started 02:00 · 4h 31m elapsed                                  │
│  found 312   qualified 87   resumes 12   applied 12   review 3   failed 1     │
│  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░  progress by qualified/applied           │
│  ── live: Timeline (last 20 events) ──────────────────────────────────────    │
├──────────────────────────────────────────────────────────────────────────────┤
│ Started       Status      Found  Qualified  Applied  Review  Failed  Duration │
│ Aug 6 02:00   ● completed   289        74       11       2       0   4h 02m   │
│ Aug 5 02:00   ● completed   301        81       14       1       2   4h 19m   │
│ Aug 4 02:00   ● failed       12         0        0       0       1   0h 04m   │
└──────────────────────────────────────────────────────────────────────────────┘
```

The running session is a pinned card above the history table; every counter is mono +
`tabular-nums` and updates via `session.updated` with **no animation** (P5). Clicking a row opens a
Sheet with `config_snapshot`, `token_usage`, and the session-filtered LogStream.

**Inventory:** Card, StatusDot, DataGrid, ProgressRing, Timeline, LogStream, Button, Sheet, Dialog
(Start a Run config), EmptyState.

---

### 8.10 Analytics — `/analytics`

```
┌ Analytics                                    ⟨ 7d ⟩⟨ 30d ⟩⟨ 90d ⟩⟨ All ⟩     ┐
│ 87 applications · 6 interviews · 1 offer · 6.9% interview rate                │
├──────────────────────────────────────────────────────────────────────────────┤
│ ┌ Applications over time ─────────────────┐ ┌ Funnel ──────────────────────┐  │
│ │  stacked bar, day buckets, 3 series     │ │ ▇▇▇▇▇▇▇▇ discovered  1,204   │  │
│ │  submitted / needs_review / failed      │ │ ▇▇▇▇▇    qualified     312   │  │
│ └─────────────────────────────────────────┘ │ ▇▇▇      applied        87   │  │
│                                             │ ▇        interview       6   │  │
│ ┌ Outcome by provider ────────────────────┐ │ ▏        offer           1   │  │
│ │  horizontal stacked bar, 3 providers    │ └──────────────────────────────┘  │
│ └─────────────────────────────────────────┘                                   │
│ ┌ What gets interviews ───────────────────────────────────────────────────┐   │
│ │  emphasis bar chart: score band → interview rate, one band highlighted  │   │
│ └─────────────────────────────────────────────────────────────────────────┘   │
│ ┌ Score distribution ─────────────────────┐ ┌ Time to response ───────────┐   │
│ │  histogram, one hue, threshold tick     │ │  dot plot, median line      │   │
│ └─────────────────────────────────────────┘ └─────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Every chart form here is justified in §11.1. `GET /analytics/insights` renders as a plain list of
sentences with a mono figure, **not** as an "AI insight card" with a sparkle icon.

**Inventory:** BarChart, StackedBar, FunnelBar, Histogram, DotPlot, StatTile row, Segmented control
(range), Card, EmptyState, TableView toggle (every chart has one — §12).

---

### 8.11 Settings — `/settings`

Two-column: 180px section rail + 8-column form, 760px measure.

```
┌ Settings                                                                     ┐
├──────────────┬───────────────────────────────────────────────────────────────┤
│ Safety     ● │  Safety                                                       │
│ Matching     │  ┌─────────────────────────────────────────────────────────┐  │
│ Providers    │  │ ⚠  Automatic submission is OFF                          │  │
│ Documents    │  │    Nothing will be submitted until both switches are on. │  │
│ AI & Tokens  │  ├─────────────────────────────────────────────────────────┤  │
│ Knowledge    │  │ Enable auto-apply            ⟨ ○── ⟩  auto_apply_enabled │  │
│ Appearance   │  │ Dry run (never submits)      ⟨ ──● ⟩  dry_run            │  │
│ Advanced     │  │ Minimum score to apply       ⟨ 70 ⟩   ▓▓▓▓░ threshold    │  │
│              │  │ Max applications per day     ⟨ 50 ⟩                      │  │
│              │  │ Max essay questions          ⟨ 3  ⟩                      │  │
│              │  │ Min answer confidence        ⟨ 0.75 ⟩                    │  │
│              │  └─────────────────────────────────────────────────────────┘  │
│              │  Changes save automatically.            ⟨ Reset Local Cache ⟩ │
└──────────────┴───────────────────────────────────────────────────────────────┘
```

- **Autosave**, optimistic, with a 1.2s debounce; the only feedback is a mono `saved 12:04:31`
  timestamp under the section title. No toast per field.
- The Safety card is `--st-review`-washed while submission is disabled and `--st-success`-washed
  when armed — the one place a whole card takes a status tint (P7).
- Appearance: theme (System / Dark / Light), density (Compact 30 / Default 36 / Comfortable 44),
  and a "Reduce motion" override that forces the reduced-motion branch regardless of OS setting.
- Advanced: `Reset Local Cache` (clears IndexedDB + `queryClient.clear()`), plugin list from
  `GET /settings/plugins`, scoring-rules YAML editor.

**Inventory:** Switch, Input (number/mono), Select, Slider (custom, score threshold), Card, Badge,
Button, Dialog (destructive confirm), Tabs (rail).

---

### 8.12 Logs — `/logs`

The full-screen LogStream (§7.21) with a historical-search mode.

```
┌ Logs                                        ⟨ Live ⟩ ⟨ Historical ⟩          ┐
│ 10,000 buffered · 4 sources · ERROR 12 · WARNING 31                           │
├──────────────────────────────────────────────────────────────────────────────┤
│ ⌕ [ submit_not_found ]  ⦿regex ○exact   ↑↓ 3/47   ⟨level ▾⟩⟨logger ▾⟩ ⟨⏸⟩⟨⧉⟩ │
├──────────────────────────────────────────────────────────────────────────────┤
│ 09:41:02.113  INFO   pipeline   posting.scored posting_id=8f3a… total=82      │
│ 09:41:04.902  ERROR  apply      submit_not_found provider=workday             │
│   ▾ { "correlation_id": "…", "application_id": "…", "attempt": 2 }            │
│ …                                                                             │
│                            ⟨ 14 new lines ↓ ⟩                                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Live** reads the ring buffer (never the query cache). **Historical** switches to
`GET /logs?limit=200&offset=…` with the same rendering, and shows the IndeterminateBar while
paging. Clicking a `correlation_id` chip filters both modes to that trace.

**Inventory:** LogStream, Input, Select, Button (segmented), Kbd, Badge, EmptyState
(`No log lines yet` / `No lines match "…"`).

---

## 9. Interaction & keyboard

### 9.1 The model

Three principles, taken from Linear and Raycast because they are now cross-product convention and
deviating costs the user's existing muscle memory for nothing `[E]`:

1. **Unmodified single letters act on the focused row**, mnemonically matched to the property they
   change. Side effect: macOS and Windows bindings are byte-identical — no `⌘`/`Ctrl` fork, no docs
   divergence.
2. **`g` is a navigation namespace.** One letter reserved for all navigation frees the other 25 for
   actions.
3. **Modifiers encode priority; `Ctrl` means irreversible.** `↵` primary · `⌘↵` secondary ·
   `⌘⇧↵` tertiary · `Ctrl+X` destructive.

### 9.2 Global

| Keys | Action |
|---|---|
| `⌘K` / `Ctrl+K` | Command palette (works inside inputs) |
| `/` | Focus the page's search input |
| `⌘.` / `Ctrl+.` | Toggle sidebar |
| `⌘[` / `⌘]` | Back / forward |
| `Esc` | Pop one level: close popover → close sheet/dialog → clear search → deselect |
| `⌘Esc` / `Shift+Esc` | Pop all the way to Dashboard |
| `?` | Keyboard cheatsheet (a Dialog, from anywhere except a text field) |
| `⌘,` | Settings |
| `⌘⇧D` | Discover postings |
| `⌘⇧R` | Start a run |
| `Ctrl+S` | Stop the running session (destructive → confirm Dialog) |
| `⌘\` | Toggle the detail pane (master–detail screens) |
| `⌘⇧L` | Toggle theme |

### 9.3 Navigation — `g` then key

| Chord | Destination | Mnemonic |
|---|---|---|
| `g d` | Dashboard | **d**ashboard |
| `g a` | Applications | **a**pplications |
| `g p` | Postings | **p**ostings |
| `g r` | Review queue | **r**eview |
| `g k` | Knowledge | **k**nowledge |
| `g s` | Resumes | ré**s**umés |
| `g n` | Runs | ru**n**s |
| `g i` | Analytics | **i**nsights (matches `/analytics/insights`) |
| `g l` | Logs | **l**ogs |
| `g ,` | Settings | matches `⌘,` |

Chord timeout **1200ms**; a 20px `g …` indicator appears bottom-left in the status bar while the
chord is pending, listing the available second keys. `Esc` cancels.

### 9.4 List / table

| Key | Action |
|---|---|
| `j` / `↓` | Next row |
| `k` / `↑` | Previous row |
| `⌘↓` / `⌘↑` | Jump to next/previous section or status group `[E — Raycast]` |
| `⌥↓` / `⌥↑` | Page down / up |
| `Home` / `End` | First / last row |
| `↵` | Open the focused row (detail Sheet) |
| `⌘↵` | Open in the full detail route |
| `Space` | Quick-look peek (a non-focus-stealing preview popover) |
| `x` | Toggle row selection |
| `⇧j` / `⇧k` | Extend selection |
| `⌘a` | Select all *visible* rows (never the whole unfetched set) |
| `Esc` | Clear selection |

Focus is roving-tabindex on the row container; only one row is in the tab order. Arrow keys never
scroll the page — they move focus, and the virtualizer scrolls the focused row into view with
`block: 'nearest'`.

### 9.5 Row actions (unmodified, mnemonic)

| Key | Action | Applies to |
|---|---|---|
| `s` | Change **s**tatus (opens an inline Select) | Application |
| `n` | Add a **n**ote | Application |
| `r` | **R**etry | Application (`failed`) |
| `a` | **A**pply now | Posting |
| `v` | **V**iew artifacts | Application |
| `c` | **C**opy posting URL | Posting, Application |
| `o` | **O**pen posting in the system browser | Posting, Application |
| `f` | **F**avorite / pin | Posting |
| `e` | **E**dit (fact text, answer, note) | Knowledge fact, Review answer |
| `Ctrl+X` | Archive / abandon — **destructive, always confirms** | Application, Source |
| `Ctrl+⇧X` | Archive all selected — **destructive, always confirms** | Application |

Review queue adds: `↵` expand · `⌘↵` Approve & Submit · `Ctrl+X` Dismiss · `d` Save draft.

### 9.6 Forms and dialogs

`⌘↵` submits from anywhere inside the form, including a textarea. `Esc` cancels (and, if the form is
dirty, opens a confirm Dialog rather than discarding silently). `Tab` order follows visual order;
a dialog traps focus, moves focus to the first focusable on open, and returns it to the trigger on
close. Number inputs accept `↑`/`↓` to step and `⇧↑`/`⇧↓` to step by 10.

### 9.7 Focus management

- **Focus-visible only.** The ring never shows on mouse click. `[E]`
- **Route change moves focus to the page `<h1>`** (which carries `tabindex="-1"`), so screen readers
  and keyboard users land in the right place. This is the only "focus jump" in the app.
- **Optimistic mutations never move focus.** If a row's status changes under the cursor, focus stays
  on the row.
- **Deleted/archived row:** focus moves to the next sibling, or the previous one if it was last.
- **Sheets and dialogs** restore focus to their trigger. If the trigger has unmounted (row was
  removed), focus goes to the table container.
- **Focus-ring spec** (repeated because it is binding):
  `outline: 2px solid var(--focus-ring); outline-offset: 2px;` — `-2px` inside table rows so the
  ring stays within the row box. Never `outline: none` without a replacement.

---

## 10. The instant-feel contract

> **The user requirement is: "Website should be able to click around with no delay, cache everything
> if needed. There should never be 1 second of delay ever."** This section is how that is achieved
> and how it is enforced. Everything here is testable.

### 10.1 The core invariant (greppable, CI-enforced)

> **Components may branch on `isPending` only.**
> `isFetching`, `isLoading`, `isRefetching`, and `isRefetchError` must never gate content rendering.

In TanStack Query v5, `isPending` means *"this query has literally never had data."* With the
persisted cache in §10.3, `isPending` is unreachable after the first-ever launch. Background
refreshing gets the 2px `IndeterminateBar` in the toolbar chrome and **nothing else**. `[E]`

CI rule: fail the build on `isLoading`, `isFetching`, or `isRefetching` appearing inside a JSX
conditional in `src/routes/**` or `src/components/**`.

### 10.2 QueryClient defaults

```ts
// desktop/src/lib/query/client.ts
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      gcTime: 1000 * 60 * 60 * 24 * 7,        // 7 days — MUST be >= persister maxAge
      refetchOnWindowFocus: false,             // desktop users alt-tab constantly
      refetchOnReconnect: true,
      refetchOnMount: true,
      retry: 2,
      retryDelay: (a) => Math.min(1000 * 2 ** a, 8000),
      networkMode: 'offlineFirst',             // serve from disk before the network is confirmed
      structuralSharing: true,
    },
    mutations: { networkMode: 'online', retry: 0 },
  },
});
```

`gcTime` **must** be ≥ the persister's `maxAge`, or restored entries are garbage-collected five
minutes after hydration (hydration's default `gcTime` is 300000ms). `refetchOnWindowFocus: false`
because the WebSocket already keeps data fresh; leaving it on causes a refetch storm on every
alt-tab. `[E]`

### 10.3 `staleTime` / `gcTime` per query family (binding)

**Do not use one global `staleTime`.** Anything the event stream covers gets `Infinity` and is
invalidated by push, which is strictly better than polling: zero redundant requests *and* zero
staleness. `[E]`

| Query family | Key | `staleTime` | `gcTime` | Persisted? | Invalidated by |
|---|---|---|---|---|---|
| `/postings` list | `qk.postingList(f)` | `Infinity` | 7d | ✅ | `posting.discovered\|scored` |
| `/postings/{id}` | `qk.postingDetail(id)` | `5 * 60_000` | 7d | ✅ | `posting.scored` |
| `/applications` list | `qk.applicationList(f)` | `Infinity` | 7d | ✅ | `application.*` |
| `/applications/{id}` | `qk.applicationDetail(id)` | `5 * 60_000` | 7d | ✅ | `application.*` |
| `/applications/{id}/artifacts` | `qk.applicationArtifacts(id)` | `Infinity` | 7d | ✅ | `application.submitted` |
| `/reviews` | `qk.reviews()` | **`15_000`** | 1d | ✅ | `application.needs_review` |
| `/analytics/overview\|funnel` | `qk.analytics(...)` | `30_000` | 1d | ✅ | `session.finished` |
| `/analytics/timeseries` | `qk.timeseries(days)` | `30_000` | 1d | ✅ | `session.finished` |
| `/sessions` list | `qk.sessions()` | `Infinity` | 7d | ✅ | `session.*` |
| `/knowledge/*` (sources, facts, entities, stats) | `qk.knowledge*()` | `Infinity` | 7d | ✅ | `knowledge.index_finished` |
| `/knowledge/graph` | `qk.knowledgeGraph(f)` | `5 * 60_000` | 1d | ✅ | `knowledge.index_finished` |
| `/resumes`, `/resumes/versions/{id}` | `qk.resumes*()` | `Infinity` | 7d | ✅ | mutation only |
| `/profile`, `/profile/preferences`, `/settings` | `qk.settings*()` | `Infinity` | 7d | ✅ | mutation only |
| `/settings/plugins` | `qk.plugins()` | `Infinity` | 7d | ✅ | never (restart) |
| `/onboarding/status\|steps` | `qk.onboarding()` | `0` | 1h | ❌ | every step POST |
| `/logs` (historical) | `qk.logs(f)` | `10_000` | 1h | ❌ | never |
| **live log tail** | — | **not a query at all** | — | ❌ | §7.21 ring buffer |

`/reviews` is the only queue where a stale count is user-visible harm, hence 15s. `[E]`

### 10.4 Query keys

```ts
// desktop/src/lib/query/keys.ts
const norm = (f: object) => Object.fromEntries(
  Object.entries(f).filter(([, v]) => v !== undefined && v !== null && v !== '')
);

export const qk = {
  all: ['aos'] as const,
  postings: () => [...qk.all, 'postings'] as const,
  postingList: (f: PostingFilters) => [...qk.postings(), 'list', norm(f)] as const,
  postingDetail: (id: string) => [...qk.postings(), 'detail', id] as const,
  applications: () => [...qk.all, 'applications'] as const,
  applicationList: (f: AppFilters) => [...qk.applications(), 'list', norm(f)] as const,
  applicationDetail: (id: string) => [...qk.applications(), 'detail', id] as const,
  reviews: () => [...qk.all, 'reviews'] as const,
  analytics: (kind: string) => [...qk.all, 'analytics', kind] as const,
  // …
} as const;
```

**`normalizeFilters` is not optional.** TanStack hashes keys with a stable stringify that sorts
object keys, so `{a,b}` and `{b,a}` are the same key — **but `q: ''` and `q: undefined` hash
differently** and silently fragment the cache into unreachable duplicates. That is precisely what
makes a "cached" app still show spinners. `[E]` Also clamp `limit` to the fixed page size (50) so
paging never mints new key shapes.

### 10.5 Persisted cache & hydration (the cold-start strategy)

**Layer 1 — synchronous localStorage micro-snapshot (the only thing that makes cold start truly
instant).**

```ts
// main.tsx — at MODULE SCOPE, before createRoot().render()
const snap = localStorage.getItem('aos-hot');
if (snap) hydrate(queryClient, JSON.parse(snap));
createRoot(el).render(<App />);
```

Written on `visibilitychange → hidden` and on `before-quit` (via IPC), containing only the hot set —
dashboard overview, first page of applications, review queue, sidebar counts, user prefs — capped at
**500 KB**. `localStorage.getItem` is synchronous and sub-millisecond at that size, so **the first
React render already has data: zero frames of empty state.** IndexedDB is async, so even a perfect
IDB persister costs at least one frame of nothing. `[E]`

**Layer 2 — per-query IndexedDB persister for the long tail.**

```ts
import { experimental_createQueryPersister } from '@tanstack/query-persist-client-core';
import { get, set, del, createStore } from 'idb-keyval';

const store = createStore('applicantos', 'qcache');
const idbStorage = {
  getItem: (k: string) => get(k, store),
  setItem: (k: string, v: unknown) => set(k, v, store),
  removeItem: (k: string) => del(k, store),
};

export const persister = experimental_createQueryPersister({
  storage: idbStorage,
  maxAge: 1000 * 60 * 60 * 24 * 7,                     // <= gcTime
  buster: `${__APP_VERSION__}:${__API_SCHEMA_VERSION__}`,
  prefix: 'aos-qc',
  filters: { predicate: (q) => !q.queryKey.includes('logs') },
});
// queryClient.defaultOptions.queries.persister = persister.persisterFn
```

**Do not use `persistQueryClient` + `createAsyncStoragePersister`.** That serializes the entire
dehydrated client under one key and rewrites the whole blob on every cache mutation — with a
WebSocket calling `setQueryData` continuously it pins the CPU and hammers the disk. This is the
documented pathology in TanStack/query#58799 (Claude Desktop: ~25% idle CPU, ~5 MB/s sustained
writes from a 45 MB blob). The per-query persister writes one key per query hash. `[E]`

**Layer 3 — main-process warm fetch for a genuinely fresh install.** Immediately after
`app.whenReady()`, in parallel with window creation, `net.fetch` `/api/v1/analytics/overview` and
page 1 of `/api/v1/applications` from the local FastAPI and push them over IPC. Main starts
fetching several hundred ms before the renderer has parsed its bundle, so the payload is usually
resolved by the time React mounts. `[E]`

**Never gate the tree on `useIsRestoring`.** Render the full shell (sidebar, header, toolbar,
correctly-sized skeletons) immediately and let restoration fill in — gating just moves the
blank-then-pop flash earlier in the boot. `[E]`

**Cache invalidation safety.**
- `buster = ${appVersion}:${API_SCHEMA_VERSION}`; bump `API_SCHEMA_VERSION` (a constant in
  `src/lib/api/types.ts`) on **any** DTO shape change.
- At boot, `GET /health`; compare `build_id` against the value in `electron-store`; on mismatch,
  `queryClient.clear()` + clear the IDB store, then refetch everything.
- `Settings → Reset Local Cache` does the same on demand.
- **`shouldDehydrateMutation: () => false` — never persist mutations.** A resumed `apply.submit`
  replaying on app restart would violate CONTRACTS §18.1 *"never apply twice."* This is the one
  cache bug that can cause real-world harm. `[E]`

### 10.6 Prefetch on intent

```ts
const timer = useRef<number>();
const warm = () => {
  timer.current = window.setTimeout(() => {
    queryClient.prefetchQuery({ ...applicationDetailOptions(id), staleTime: 60_000 });
  }, 60);
};
const cancel = () => clearTimeout(timer.current);
// <div onMouseEnter={warm} onMouseLeave={cancel} onFocus={warm} onBlur={cancel} />
```

- Attach to **both `onMouseEnter` and `onFocus`** — keyboard users tabbing through the table get the
  same warm cache as mouse users.
- The **60ms hover delay** means a mouse sweep across 50 rows fires one request, not fifty.
- `staleTime` on the prefetch call is the throttle: `prefetchQuery` is a no-op when the cached data
  is fresher.
- Use `prefetchQuery` (returns `Promise<void>`, never throws), not `fetchQuery`. `[E]`

**Route preloading** (TanStack Router):

```ts
createRouter({
  routeTree, context: { queryClient },
  defaultPreload: 'intent',
  defaultPreloadDelay: 50,
  defaultPreloadStaleTime: 0,     // let TanStack Query own freshness
  defaultPendingMs: 1000,         // suppress the pending component unless a load exceeds 1s
  defaultPendingMinMs: 500,       // and if it does show, don't flash-and-vanish
  defaultStaleTime: Infinity,
  scrollRestoration: true,
});
```

Loaders call **`ensureQueryData`, never `fetchQuery`** (the former returns cached data immediately
and ignores `staleTime`), against a shared `queryOptions()` object that the component also consumes
via `useSuspenseQuery`. A mismatched key between loader and component is the classic cause of "the
loader warmed the cache but the component still suspends." `[E]`

**Pagination prefetch:** when `!isPlaceholderData && data.items.length === limit`, prefetch
`offset + limit`; when `offset > 0`, prefetch `offset - limit`. Next-page click becomes a pure cache
read.

### 10.7 Bundle and boot

- **Do not code-split the renderer.** Ship one chunk: Vite `build.rollupOptions.output.manualChunks:
  undefined`, no `React.lazy` on routes. Code-splitting optimizes *network transfer*, which does not
  exist in Electron — it only introduces a per-route async chunk fetch that can become the one
  visible navigation delay. `[E]`
- Split exactly two heavy leaves that most sessions never open: the **PDF preview** (`/resumes`) and
  the **graph canvas** (`/knowledge`).
- `build.target: 'chrome130'` (match the shipped Electron's Chromium) removes all downlevel
  transpilation and shrinks parse time. `build.sourcemap: true`.
- Serve production from a **privileged `app://` scheme** via `protocol.handle` + `net.fetch` with an
  SPA fallback and a path-traversal guard — not `file://` (not a secure context; breaks relative
  fetch and history routing) and not a localhost HTTP server (port binding, firewall prompts, a real
  network origin). Keep Vite `base: '/'` so dev and prod behave identically. `[E]`

### 10.8 Live updates: WebSocket → `setQueryData`

CONTRACTS §14 specifies `GET /ws` (WebSocket) and `app/api/events.py` publishes payloads that are
*the same pydantic schemas the REST endpoints return*, explicitly so the desktop app can
`setQueryData` directly without refetching. That is the contract; use it.

> **Research dissent, recorded:** the perf research recommends SSE over WebSocket for a
> server→client-only stream. CONTRACTS is binding, so we ship the WebSocket. The *hosting* advice
> still applies in full and is adopted below. If SSE is ever revisited, file it in
> `docs/OPEN_QUESTIONS.md`.

**Host the socket in the Electron main process**, relay to the renderer via
`webContents.send('events:message', msg)`. This (a) survives renderer reloads and devtools, (b) is
immune to renderer background throttling — so `backgroundThrottling` stays at its default `true`,
avoiding electron#42378's blank-window bug and the battery cost, and (c) shares one connection
across windows. Separately, always
`app.commandLine.appendSwitch('disable-features', 'CalculateNativeWinOcclusion')` on Windows — it is
the cause of "the app was frozen when I uncovered it." `[E]`

```ts
// One module-level subscriber, OUTSIDE React.
window.applicantos.onEvent((msg) => {
  switch (msg.type) {
    case 'application.status_changed':
    case 'application.submitted':
    case 'application.needs_review': {
      const app = msg.data;
      notifyManager.batch(() => {
        queryClient.setQueryData(qk.applicationDetail(app.id), (old) => ({ ...old, ...app }));
        queryClient.setQueriesData<Page<Application>>(
          { queryKey: [...qk.applications(), 'list'] },
          (old) => old && {
            ...old,
            // SAME REFERENCE for untouched rows — this line decides 60fps vs 12fps
            items: old.items.map((i) => (i.id === app.id ? { ...i, ...app } : i)),
          },
        );
        queryClient.setQueryData(qk.analytics('overview'), (s) => s && recompute(s, app));
      });
      break;
    }
    case 'log.entry':
      logBuffer.push(msg.data);        // ring buffer, rAF-flushed — never the query cache
      break;
    default:
      // Payload-less events only:
      queryClient.invalidateQueries({ queryKey: qk.knowledge(), refetchType: 'active' });
  }
});
```

**Four binding rules:**

1. **`setQueryData`, never `invalidateQueries`, whenever the event carries the entity.**
   `setQueryData` writes data and bumps `dataUpdatedAt`; it is *structurally incapable* of producing
   a pending state, so live updates can never flash a spinner. `invalidateQueries` marks stale and
   refetches. Use it only for payload-less events, and always with `refetchType: 'active'` so
   background queries don't stampede. `[E]`
2. **Preserve referential identity for untouched rows.** `items.map(i => i.id === x.id ? {...i,...x} : i)`
   — never a blanket `{...i}` spread, which defeats `structuralSharing` and re-renders every
   memoized row on every event. `[E]`
3. **Batch and coalesce.** Wrap multi-key writes in `notifyManager.batch()` (one render pass instead
   of N). Buffer high-frequency events in a module-level array and flush on `requestAnimationFrame`,
   so 500 events/s becomes at most 60 cache writes/s. `[E]`
4. **`notifyOnChangeProps: ['data']`** on high-churn queries so `isFetching` flips don't re-render.
   `select` narrows subscriptions further, but the selector must be module-level or `useCallback`'d
   or structural sharing is defeated.

### 10.9 Optimistic mutations

Every mutation is optimistic with rollback. The pattern, once:

```ts
useMutation({
  mutationFn: (v) => api.updateApplication(id, v),
  onMutate: async (v) => {
    await queryClient.cancelQueries({ queryKey: qk.applicationDetail(id) });
    const prevDetail = queryClient.getQueryData(qk.applicationDetail(id));      // capture OLD value
    const prevLists  = queryClient.getQueriesData({ queryKey: [...qk.applications(), 'list'] });
    queryClient.setQueryData(qk.applicationDetail(id), (o) => ({ ...o, ...v }));
    queryClient.setQueriesData({ queryKey: [...qk.applications(), 'list'] }, patchRow(id, v));
    return { prevDetail, prevLists };
  },
  onError: (_e, _v, ctx) => {
    queryClient.setQueryData(qk.applicationDetail(id), ctx!.prevDetail);
    ctx!.prevLists.forEach(([k, d]) => queryClient.setQueryData(k, d));
    toast.error('Could not update application', { action: { label: 'Retry', onClick: retry } });
  },
  onSettled: () => queryClient.invalidateQueries({ queryKey: qk.applicationDetail(id) }),
});
```

**Capturing the old value at mutation time is what makes optimistic UI safe rather than reckless.**
Without it, this is not optimistic UI — it is a race. `[E]`

**Mutations that must NOT be optimistic** (they are irreversible and CONTRACTS §18 guards them
server-side): `POST /postings/{id}/apply`, `POST /reviews/{id}/resolve`, `POST /sessions/start|stop`,
`POST /onboarding/complete`. These flip to an in-flight status (`preparing`, `submitting`) locally
and wait for the real event. Showing "Submitted" optimistically and rolling back would be a lie
about an action that touches the outside world. `[J, forced by CONTRACTS §18.1]`

### 10.10 Loading affordance policy (four-way split)

Choose by **what information you have**, not by habit. `[E — Geist]`

| You know… | Use | Never |
|---|---|---|
| The layout of the incoming content | **Skeleton** with explicit width/height | a spinner |
| A single action is in flight | **Inline spinner in the button** (§7.1) | a full-page overlay |
| Something is loading, shape and duration unknown | **`IndeterminateBar` in the chrome** | a content-replacing skeleton |
| A real percentage | **`ProgressRing` / progress bar** | an indeterminate bar |
| There is genuinely no data | **`EmptyState`** | a skeleton (hard rule) |

**Exact thresholds (binding):**

| Threshold | Value | Rule |
|---|---|---|
| Cached data present | **0ms** | Render it. No spinner. Ever. |
| Background refetch | **0ms** | `IndeterminateBar` only; stale content stays visible **and interactive** |
| `isPending` → skeleton delay | **400ms** | Below this, render nothing (the shell already occupies the space) |
| Skeleton minimum duration | **500ms** | Once shown, it stays — a 150ms skeleton flash is worse than none |
| Button spinner delay | **150ms** | Below this the mutation resolved; showing a spinner is noise |
| Route pending component | **1000ms** (`defaultPendingMs`), min **500ms** | Unreachable with a warm cache |
| EmptyState suppression | while `isPending && query === ''` | Kills the "No results" flash `[E]` |

### 10.11 Virtualization thresholds

| List | Threshold | Config |
|---|---|---|
| Applications, Postings | **> 100 rows** | `estimateSize: () => 44`, `overscan: 8`, `getItemKey: i => rows[i].id` |
| Knowledge facts | **> 100 rows** | `estimateSize: () => 36`, `overscan: 8` |
| Log lines | **always** | `estimateSize: () => 20`, `overscan: 20` |
| Command palette results | **> 200 rows** | `estimateSize: () => 40`, `overscan: 6` |
| Timeline, review cards, resume versions | **never** | bounded by design |

**Binding virtualizer rules** `[E]`:
- **Fixed row heights. Never `measureElement`.** Dynamic measurement attaches a `ResizeObserver` per
  row and forces layout reads every frame — it is the #1 cause of sub-60fps virtualized tables.
- Truncate with `text-overflow: ellipsis; white-space: nowrap`; never wrap in a virtualized row.
- Render rows inside a spacer div of `getTotalSize()` px and offset the visible window with **one**
  `transform: translateY(${items[0].start}px)` on a wrapper — not per-row absolute `top`.
- `overscan: 1` (the default) shows blank strips on fast trackpad scroll; 8 at 44px covers ~350px of
  runway.
- `getItemKey` returns the **entity id**, never the index, so React reuses row DOM when a row is
  prepended.
- **No Framer Motion on virtualized rows.** Ever.

### 10.12 Typing and filtering

```ts
const [q, setQ] = useState('');
const dq = useDeferredValue(q);           // input binds to q; query key uses dq
// list query: { queryKey: qk.applicationList({ ...f, q: dq }), placeholderData: keepPreviousData }
```

Drive `isPlaceholderData` into `opacity: 0.65; pointer-events: none` with a **120ms** opacity
transition — never a spinner, never an empty table. Use `useTransition` for tab/segment switches
that change a large subtree. `[E]`

### 10.13 Layout-shift budget: zero

Four defaults, all mandatory:
1. Persistent shell in the root route; only `<Outlet/>` swaps.
2. `scrollbar-gutter: stable both-edges` on every scroll container.
3. `font-variant-numeric: tabular-nums` on every number outside a stat tile.
4. `min-height: calc(var(--row-h) * var(--page-size))` on every table body.

Plus: skeletons match final box metrics exactly; images and screenshots declare `width`/`height`;
the accent hairline and status dots are absolutely positioned so they never affect flow.

### 10.14 Performance budget (testable)

Measured on the reference machine (Windows 11, 8-core, 16 GB, NVMe, integrated GPU), with the
backend on `127.0.0.1`. `p50` / `p99` where both are given.

| Metric | Budget | How it is measured |
|---|---|---|
| **Cold start → first meaningful paint** (real data, not skeletons) | **≤ 800ms p50 / 1200ms p99** | `ready-to-show` → the frame where the Dashboard hero figure has a real value. Requires the localStorage hot-snapshot path. |
| **Cold start → interactive** | **≤ 1500ms** | CONTRACTS §17 |
| **Warm window restore → paint** | **≤ 120ms** | `restore` → first composited frame |
| **Route change (visited route)** | **≤ 16ms p50 / 50ms p99** | click → next paint. One frame. CONTRACTS §17 says <100ms; we hold ourselves to one frame because the cache is warm and the shell persists. |
| **Route change (unvisited, warm cache)** | **≤ 100ms** | CONTRACTS §17 |
| **Interaction → paint** (hover, select, filter chip, checkbox, tab) | **≤ 50ms p99** | `pointerdown` → next paint. CONTRACTS §17 |
| **Keystroke → input paint** (search box) | **≤ 16ms** | `useDeferredValue` guarantees this regardless of list size |
| **Keystroke → filtered list paint** | **≤ 120ms p99** | with `keepPreviousData`, the old list never disappears |
| **List scroll** | **60fps sustained**, no frame > 20ms, over a 5,000-row table at max trackpad velocity | Chrome DevTools Performance, 10s scroll |
| **Log stream** | **60fps at 1,000 lines/sec** | rAF-coalesced flush + ring buffer |
| **Command palette open → painted + focused** | **≤ 16ms** | Zero animation; local fuzzy filter |
| **Optimistic mutation → UI reflects it** | **≤ 16ms** | In-memory write, one frame |
| **WebSocket event → cell repaint** | **≤ 50ms p99** | property-granular subscriptions |
| **Idle CPU** (window visible, session running) | **≤ 3%** | If it exceeds this, the persister is the first suspect (§10.5) |
| **Idle CPU** (window hidden) | **≤ 0.5%** | Socket lives in main; renderer is throttled |
| **Renderer heap after 1h with 5,000 rows** | **≤ 350 MB** | ring buffer capped at 10k lines; `gcTime` bounded |
| **Renderer bundle** | **≤ 2.5 MB** parsed, one chunk | plus 2 lazy leaves |

**Regression gate:** these run in CI as a Playwright + CDP trace job on every PR touching
`desktop/src/`. A budget miss fails the build.

---

## 11. Charts

Library: **Recharts** for the standard forms (bar, stacked bar, line, dot plot, histogram) — it is
SVG-based, tree-shakes, and needs no canvas plumbing. **Custom SVG** for the sparkline, funnel, and
score-breakdown bars, because those are ~30 lines each and pulling a chart library into a stat tile
is not worth the render cost. **Canvas** for the knowledge graph only (§8.7).

### 11.1 Form selection — the question decides

| Question the user is asking | Form | Color job |
|---|---|---|
| "How many did it do overnight?" | **Hero figure** (40px, mono) — *not* a one-bar chart | none |
| "How are the headline numbers moving?" | **StatTile row** (value + delta + sparkline) | none |
| "How did volume change day to day?" | **Stacked column**, day buckets | status (the series *are* states) |
| "Where do postings drop out?" | **Funnel bar** (horizontal, descending) | one hue + gray |
| "Which provider produces outcomes?" | **Horizontal stacked bar**, one row per provider | status |
| "Which score band actually gets interviews?" | **Emphasis bar** — one band in accent, the rest gray | 1 hue + gray |
| "How are scores distributed?" | **Histogram**, one hue, threshold tick at `min_score` | sequential |
| "How long until a response?" | **Dot plot** with a median line | 1 hue |
| "How does this application compare?" | **ScoreBar with a threshold tick** (§7.19) | ordinal |
| "How many sources / facts / entities?" | **Table**, not a chart | none |

**Forms that are banned outright:** pie and donut charts (a 2-slice pie is a meter; a 5-slice pie is
a bar chart), any dual-axis chart, 3D anything, radar/spider, gauge dials, and animated
"racing bar" charts. `[E — dataviz anti-patterns; the dual-axis prohibition is the #1 chart mistake]`

**Emphasis is the most underused form** and is usually the honest answer to "make this chart
clearer": one series in the accent, everything else in `--fg-muted` at 45%. Use it whenever the
story is "this one is different." `[E]`

### 11.2 Series palette — validated, do not modify

Taken from the `dataviz` reference palette and **re-validated against our own surfaces**:

```
node scripts/validate_palette.js "<slots>" --mode dark  --surface "#12151A"   → ALL CHECKS PASS
node scripts/validate_palette.js "<slots>" --mode light --surface "#FFFFFF"   → ALL PASS, 1 contrast WARN
```

| Slot | Hue | Dark (`--bg-surface` #12151A) | Light (`#FFFFFF`) |
|---|---|---|---|
| 1 | blue | `#3987E5` | `#2A78D6` |
| 2 | orange | `#D95926` | `#EB6834` |
| 3 | aqua | `#199E70` | `#1BAF7A` |
| 4 | yellow | `#C98500` | `#EDA100` |
| 5 | magenta | `#D55181` | `#E87BA4` |
| 6 | green | `#008300` | `#008300` |
| 7 | violet | `#9085E9` | `#4A3AA7` |
| 8 | red | `#E66767` | `#E34948` |

**Measured results** (OKLab ΔE ×100):
- Dark, adjacent pairs: worst CVD **8.4** (`yellow↔aqua`, protan), worst normal-vision **19.3**.
- Light, adjacent pairs: worst CVD **9.1**, worst normal-vision **19.6**; three light slots
  (aqua 2.82, yellow 2.17, magenta 2.69) fall below 3:1 on white → **relief rule applies: those
  charts must ship visible direct labels or the table view.** It is not dismissable.
- Dark, **all-pairs** (scatter / small multiples): the first **three** slots pass (worst CVD 9.4,
  worst normal-vision 20.9). **Past three series in an all-pairs form, fold the tail into "Other"
  or facet into small multiples.** Never generate a 9th hue.

**Binding assignment rules:**
1. **Slots are assigned in fixed order and never cycled.** Color follows the entity, not its rank —
   a filter that removes a provider must not repaint the survivors. `[E]`
2. **Provider identity (categorical) uses the slots, permanently:**
   `greenhouse → slot 1` · `lever → slot 2` · `ashby → slot 3` · `workday → slot 4` ·
   `linkedin → slot 7` · `manual → slot 6`. Written once in `lib/chart/series.ts`.
3. **When the series *are* application states, use the status palette (§2.2), not the categorical
   slots** — with an icon or label attached, never color alone. A stacked column of
   submitted/needs_review/failed is a *status* chart, and using categorical hues there would make
   "failed" a random color.
4. **Sequential (histograms, heatmaps, density):** one hue — the accent iris ramp from §2.5. Never a
   rainbow.
5. **Diverging (delta vs. target, above/below `min_score`):** blue ↔ red with a **gray** midpoint
   (`#383835` dark, `#F0EFEC` light). Never a hue at the midpoint.
6. **Text never wears a series color.** Values, labels, legends, and axis text use `--fg-*`; a
   colored dot or line-key beside the text carries identity. `[E]`

### 11.3 Chart chrome

| Element | Spec |
|---|---|
| Chart surface | `--chart-surface` (= `--bg-surface`) |
| Gridlines | **1px solid** `--chart-grid`, horizontal only, **never dashed**, behind the marks |
| Axis line | 1px `--chart-axis`; the y-axis line is omitted entirely when gridlines are present |
| Tick labels | `--font-mono`, `--text-micro`, `--chart-ink`, `tabular-nums` |
| Y ticks | round numbers (0 / 250 / 500), thousands-comma'd, ~4–5 ticks max |
| X ticks | show every nth label so they never collide; rotate **never** — drop labels instead |
| Bar/column thickness | **≤ 24px** (cap it; leftover band width is air), **4px rounded data-end, square at the baseline** |
| Line | **2px**, round join and cap |
| Marker / end-dot | **≥ 8px** (r ≥ 4), filled with the series color, **2px ring in the surface color** |
| Area fill | series hue at **10% opacity** — a wash, never a saturated block |
| **Surface gap** | **2px** in the surface color between every stacked segment and every adjacent bar. This — not a stroke — is what separates touching marks. `[E]` |
| Chart padding | 16px inside the card; 8px between the plot and the axis labels |
| Chart height | 200px (card), 280px (full-width analytics) |
| Empty chart | `EmptyState` inside the card, never an empty axis frame |

### 11.4 Legend, labels, tooltip

- **A legend is always present for ≥ 2 series; a single-series chart gets none** (the title already
  names it). Legend is a horizontal row above the plot, right-aligned, `--text-mini` `--fg-secondary`,
  each item a 8px `--radius-full` dot + label, 12px gap. `[E]`
- **Direct labels are selective, never on every point.** Label the endpoint, the extreme, or the one
  series the story is about. A label that does not fit inside its segment moves outside the bar end
  or drops to the tooltip — **never `overflow: hidden`**. `[E]`
- **Legend items are clickable** to toggle a series; toggling **must not reassign colors** to the
  survivors.
- **Tooltip is mandatory** on every chart form except a bare stat tile. Spec:
  `--bg-overlay` + `--border-strong` + `--shadow-float` + `--radius-md`, 8px padding, header
  `--text-mini` `--fg-secondary`, rows of `[8px dot] label … mono value`, values right-aligned and
  `tabular-nums`. **Line and area charts get a 1px `--border-strong` vertical crosshair** and show
  all series at that x. Bar/dot/cell charts get a per-mark tooltip. Hit targets are larger than the
  mark (minimum 24px tall band per row). Tooltip follows the cursor with **no transition** — it must
  track at pointer-move rate. `[E]`
- **Every chart has a table view toggle** (`⌥T` when focused), rendering the same data as a real
  `<table>`. This is the accessibility relief channel and it is not optional (§12).

### 11.5 StatTile & sparkline (restating §7.9 as the chart contract)

- **Label** — sentence case, no trailing colon, `--text-sm` `--fg-muted`.
- **Value** — `--text-2xl` (28px), weight 590, `--font-mono`, **`proportional-nums`**, auto-compact
  above 10,000 (`1,284` → `12.9K` → `4.2M`).
- **Delta** — signed, with a ▲/▼ glyph, `--text-mini` weight 510; color = *direction × whether up is
  good*, so `failures ▲` is `--st-danger` and `submissions ▲` is `--st-success`. The comparison
  period is named in the tooltip (`vs. previous 7 days`), never guessed at by the reader.
- **Sparkline** — 12 points, 28px tall, full-bleed to the tile's inner edges, 2px line,
  `--fg-muted` at 55% for history with the **final segment and end dot in `--accent-text`**. No
  axes, no gridlines, no labels, no tooltip. It answers "which way," not "how much."
- **The value never animates.** `[E — P5]`

### 11.6 Funnel bar (custom)

Horizontal bars, descending, one row per stage (discovered → qualified → applied → interview →
offer). Single hue (accent iris ramp, darkest at the top), 20px thick, 4px rounded right end,
**2px surface gap** between rows, stage label left in `--text-sm`, count right in mono, and the
**conversion percentage from the previous stage** in `--text-micro` `--fg-muted` between rows. A
funnel is a part-to-whole with an ordering — never a pie. `[E]`

---

## 12. Accessibility

Target: **WCAG 2.1 AA**, plus the keyboard completeness that a power-user desktop app implies.

### 12.1 Contrast

All ratios are measured in §2.6 and pass AA in both themes, with two documented, deliberate
exceptions:
- `--fg-disabled` (2.51:1) — disabled controls are exempt under WCAG 1.4.3.
- `--st-dim` (3.07:1) — **non-text only**; `abandoned`/`ghosted` labels render in `--fg-muted`.
- Light-theme `--fg-muted` is AA on `--bg-surface` (4.58) but not on `--bg-base` (4.20) — enforced
  by the placement rule in §2.6.

Non-text UI (borders, focus rings, status dots, chart marks) clears 3:1 against its own background.
The accent fill clears 3:1 (3.53 on surface) and white-on-accent clears 4.5:1 (5.18).

### 12.2 Color is never the only channel

| Signal | Channel 1 | Channel 2 | Channel 3 |
|---|---|---|---|
| Application status | color | **text label** (always) | dot shape: filled / hollow / dashed / pulsing |
| Score band | color | **numeric value in mono** | bar length + threshold tick |
| Delta direction | color | **▲ / ▼ glyph** | sign |
| Chart series | color | **legend** (≥2 series) | direct labels / table view |
| Validation error | color | **message text** | `aria-invalid` + icon |
| Log level | color | **level word** (`ERROR`) | position (fixed column) |

The `dataviz` **texture channel** (one hand-drawn 45°/135° directional fill, tone-on-tone) is
implemented behind `Settings → Appearance → High-contrast chart fills`, and is also triggered
automatically by `@media (forced-colors: active)` and by print. `[E]`

### 12.3 Keyboard

- **Every action is reachable without a mouse**, and the command palette is the discoverable path to
  all of them. No action exists *only* as a shortcut. `[E]`
- Roving tabindex on lists/tables; one row in the tab order.
- No keyboard trap anywhere except modals (which trap by design and release on `Esc`).
- Focus visible on every focusable via the universal `:focus-visible` outline (§7 preamble).
- `Skip to content` link as the first focusable in the DOM, visually hidden until focused.
- Shortcuts are suspended while a text input or textarea has focus, except `Esc`, `⌘K`, and `⌘↵`.
- `?` opens a searchable cheatsheet listing every binding, grouped exactly as §9.

### 12.4 Semantics & ARIA

| Surface | Requirement |
|---|---|
| Data grid | `role="grid"`, `aria-rowcount` (total, not page), `aria-colcount`, `role="row"`/`gridcell`, `aria-selected`, `aria-sort` on sortable headers |
| Virtualized rows | `aria-rowindex` is the **absolute** index, not the rendered one |
| StatusDot | `aria-hidden="true"` when adjacent text names the state; otherwise `role="img"` + `aria-label="Status: Needs review"` |
| StatTile | `<figure>` + `<figcaption>`; the delta gets `aria-label="up 12 percent versus previous 7 days"` |
| Chart | `role="img"` + `aria-label` summarizing the takeaway, plus the table-view toggle as the real accessible alternative |
| ScoreBar | `role="meter"`, `aria-valuenow/min/max`, `aria-label="Match score 82 of 100, threshold 70"` |
| ProgressRing | `role="progressbar"` with `aria-valuenow`; indeterminate omits `aria-valuenow` |
| Skeleton | `aria-hidden="true"`, always `[E]` |
| Dialog / Sheet | `role="dialog" aria-modal="true"`, labelled by its title, focus trapped, focus restored |
| Toast | `role="status"` (`role="alert"` for errors), `aria-live="polite"`/`"assertive"` |
| Command palette | `role="combobox"` + `aria-expanded` + `aria-activedescendant` (cmdk provides this) |

### 12.5 Live regions

The app updates continuously; announcements must be surgical or they become noise.

| Region | Markup | Why |
|---|---|---|
| LogStream viewport | `aria-live="polite" aria-atomic="false"` | announces **increments**, not the whole region `[E]` |
| Sidebar review count | `aria-live="polite"` on a visually-hidden `<span>` reading `3 items need review` | the one count worth interrupting for |
| Status change on the *focused* row | `aria-live="polite"` in a shared visually-hidden announcer | so a keyboard user learns their row changed |
| Status changes on **unfocused** rows | **no live region** | 40 rows updating would be unusable |
| Stat tiles, chart data | **no live region** | polled visually; the numbers are not urgent |
| Toasts | `role="status"` / `role="alert"` | already covered |

One shared announcer element (`#a11y-announcer`) is mounted in the root route; components call
`announce(message)` rather than minting their own live regions.

### 12.6 Reduced motion, and the rest

- Full policy in §6.7: `MotionConfig reducedMotion="user"` + bespoke `useReducedMotion` branches +
  a CSS backstop; opacity and color transitions **survive**, transforms do not; decorative keyframes
  are defined inside `@media (prefers-reduced-motion: no-preference)` so they do not exist otherwise.
- `Settings → Appearance → Reduce motion` can force the reduced branch independent of the OS.
- **Hover animations are gated** behind `@media (hover: hover) and (pointer: fine)`. `[E]`
- **Zoom:** the layout must survive `⌘+`/`⌘-` to 150% without horizontal page scroll. Fixed pixel
  heights are in CSS pixels, so Electron's zoom scales them correctly.
- **`forced-colors: active`:** borders switch to `CanvasText`, the accent to `Highlight`, and chart
  fills switch to the texture channel. Never rely on `background-color` alone for state in
  forced-colors mode.
- **Text spacing:** the layout must survive the WCAG 1.4.12 overrides (line-height 1.5×, letter
  spacing 0.12em, word spacing 0.16em, paragraph spacing 2×) without clipping — which is why table
  rows use `min-height` rather than fixed `height` for the two-line variant.

---

## 13. Do / Don't

| ✅ Do | ❌ Don't | Why |
|---|---|---|
| Branch on `isPending` only | Branch on `isFetching` / `isLoading` / `isRefetching` | The second class is the entire population of "why is there a spinner over data I already have" bugs (§10.1) |
| Render a **skeleton** when the layout is known | Render a **spinner** where a skeleton belongs | A spinner says "wait"; a skeleton says "here is the shape of what is coming" |
| Show a 2px `IndeterminateBar` in the chrome on refetch | Blank the content and re-skeleton it | Stale-but-visible beats correct-but-blank on every refetch |
| Show an `EmptyState` when there is no data | Leave a skeleton shimmering forever | The worst signal a data dashboard can send |
| Write **two** empty states — "none yet" and "none match `<query>`" | Ship one sad illustration for both | The second one must quote the query and offer a one-click clear |
| Suppress the empty state while `isPending && query === ''` | Let "No applications found" flash for 200ms | It is the single most common reason a fast app feels broken |
| Animate `transform` and `opacity` | Animate `width`, `height`, `top/left`, `margin`, `padding`, `gap`, `font-size` | Only the first pair skips layout and paint |
| Name the transitioned properties | `transition: all` | `all` animates layout properties you did not intend |
| Use `transform: 'translateY(0px)'` strings in Motion | Use the `x` / `y` / `scale` shorthands | The shorthands run rAF on the main thread and stutter exactly when the socket is busy |
| Start entrances at `scale(0.97)` | Start at `scale(0)` | Nothing in the real world appears from nothing |
| Use `ease-out` for entrances, `ease-in-out` for on-screen movement | Use `ease-in` on any UI element | `ease-in` delays the first frame, the moment the user is watching |
| Make exits ~2/3 the duration of enters | Use one duration for both | Asymmetry is what makes dismissal feel responsive |
| Ship **one** accent hue | Add a second accent, or a gradient fill | One accent + fractional washes covers every case (§P4) |
| Reserve solid accent for **one** element per view | Give the toolbar three accent buttons | If two things are solid accent, one is wrong |
| Move background **and** border together as one elevation step | Change the background alone | The paired step is what reads as elevation on dark |
| Use the inset white top hairline for raised surfaces | Reuse light-mode shadow values in dark | Dark shadows need ~5× the alpha to register at all |
| Keep chrome darker than content | Make the sidebar lighter than the table | The content region must read as the lit surface |
| Use mono for ids, numbers, dates, durations, URLs | Use `tabular-nums` and call it done | Mono is doing semantics *and* alignment; it is the product's signature |
| Use `proportional-nums` on the hero figure and stat values | Use `tabular-nums` on large standalone numbers | Tabular figures make `121` look loose at 28–40px |
| Render `—` for unknown values | Render `N/A`, `null`, `None`, or a blank cell | The em-dash is what makes a sparse table look designed |
| Default tables to no borders and no stripes | Add zebra striping and full grid lines | Borders-off-by-default is the premium tell; hover does the separating |
| Pair every status color with a label and an `aria-label` | Encode state in color alone | 8% of men cannot read your green/red distinction |
| Animate the status dot only for in-flight states | Animate every dot, or add a spinner beside it | Animated-vs-static is readable in peripheral vision across a whole table |
| Use `setQueryData` for events that carry the entity | Use `invalidateQueries` for everything | `invalidateQueries` can flash a pending state; `setQueryData` structurally cannot |
| Return the same object reference for untouched rows | `items.map(i => ({ ...i }))` | One blanket spread re-renders every memoized row on every event |
| Use the per-query IndexedDB persister | Use `persistQueryClient` with a whole-client persister | It rewrites the entire cache blob on every write and melts the disk under a live stream |
| Persist queries | Persist mutations | A replayed `apply.submit` would violate "never apply twice" |
| Prefetch on `onMouseEnter` **and** `onFocus`, with a 60ms delay | Prefetch on mouseover with no delay, mouse-only | Keyboard users deserve the warm cache; a mouse sweep must not fire 50 requests |
| Seed detail views with `placeholderData` | Seed them with `initialData` | `initialData` writes a partial object into the cache and respects `staleTime` — it persists a lie |
| Use fixed row heights in the virtualizer | Use `measureElement` | Per-row `ResizeObserver` + layout reads every frame is the #1 cause of sub-60fps tables |
| Keep the log tail in a ring buffer | Model log lines as a query | Append-per-line churns the cache and triggers the persister on every line |
| Ship the renderer as one chunk | Code-split routes | There is no network in Electron; splitting only adds a chunk fetch that can be the visible delay |
| Host the WebSocket in the Electron **main** process | Run it in the renderer with `backgroundThrottling: false` | The main-process socket survives reloads and avoids electron#42378's blank-window bug |
| Give `layoutId` to at most one indicator per view | Put `layout` on table rows or grid cards | Shared-layout runs a FLIP cycle with forced sync layout on every participant |
| Cap stagger at 30ms × 6 items | Use `staggerChildren: 0.15` + `delayChildren: 0.3` | On a 12-row list that is over two seconds before the last row lands |
| Ship zero animation on ⌘K, tabs, nav, and route changes | Add a 200ms fade "for polish" | An action performed 100×/day must be instant; Raycast ships none |
| Swap live numbers instantly | Count-up animate them | Users read the number, not the animation, and count-up reflows unless it is tabular |
| Gate a reachable skeleton at 400ms with a 500ms minimum | Show it immediately | A 150ms skeleton flash is strictly worse than nothing |
| Use `outline` + `outline-offset` for the focus ring | Use `box-shadow` rings or remove the ring | Chromium follows `border-radius` on outline, and the offset can never go stale |
| Put `scrollbar-gutter: stable both-edges` on every scroll container | Ignore it | Windows Electron scrollbars are non-overlay; every navigation shifts 15px without it |
| Use the validated `dataviz` slots in fixed order | Generate a 9th hue for a 9th series | A generated hue is indistinguishable under CVD and breaks every check |
| Use the status palette when the series *are* statuses | Use categorical hues for status series | Otherwise "failed" ends up a random color |
| Ship a table view for every chart | Rely on the chart alone | It is the accessibility relief channel, and it is mandatory when a slot is sub-3:1 |
| Use a stat tile or a bar | Use a pie, a donut, a gauge, or a dual axis | Hard `dataviz` prohibitions |
| Put `Ctrl` on destructive actions | Put delete on `⌘` next to save | A whole modifier meaning "irreversible" is cheap insurance |
| Show shortcuts inline in palette result rows | Hide them in a help page | It is the only thing that graduates users to muscle memory |
| Keep `dry_run` visible in the titlebar at all times | Bury the kill switch in Settings | P7 — safety states are louder than success states |
| Show a source chip on every generated answer | Present generated text as fact | CONTRACTS §18.7 — nothing is fabricated, and the UI must say so |

---

## Open questions for `docs/OPEN_QUESTIONS.md`

1. **Transport.** CONTRACTS §14 specifies WebSocket `/ws`; the perf research argues SSE is the
   better fit for a server→client-only stream (auto-reconnect with `Last-Event-ID`, no ping/pong
   state machine, one auth path). We ship WebSocket per contract. Revisit?
2. **Bundled sans face.** We ship system-first for the sans (§3.1). If a unified typographic identity
   is wanted, bundling Geist Sans alongside Geist Mono is a one-line change with zero network cost —
   but it costs a font-load frame on cold start, which P1 currently forbids.
3. **`--st-dim` at 3.07:1.** Deliberately below AA-as-text so `abandoned`/`ghosted` recede. If a user
   reports these rows are unreadable, raise it to `#6B7280` (≈3.9:1) rather than adding a hue.
4. **All-pairs chart cap of three series.** If Analytics ever needs a 4-provider scatter, we must
   either fold to "Other," facet into small multiples, or re-step the palette and re-run the
   validator. Do not seat a 4th slot silently.
5. **Density default.** Shipping 36px rows as the default with 30/44 available. If telemetry shows
   most users switch, change the default rather than adding a fourth density.
6. **Optimistic submit.** `POST /postings/{id}/apply` is deliberately *not* optimistic (§10.9).
   If the round trip on localhost is consistently <50ms, the in-flight status flip may be enough and
   this note can be closed.
