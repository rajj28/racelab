# RaceLab — logo / gallery image prompts

Three prompts, all **3:2** (Devpost's recommended gallery ratio). Paste any one
into an image model (Midjourney, DALL·E, Firefly, Imagen) or into Claude for an
SVG version.

**Target size:** 1536 × 1024, or 1800 × 1200 for a crisper gallery thumbnail.
Devpost caps files at 5 MB.

**Which to pick:** #1 is the safest hero and the one I'd lead with. #2 is the
most distinctive if you want to stand out in a grid of gradients. #3 is the one
that actually explains the project, and works best as the *second* gallery
image.

---

## Prompt 1 · "The fork" — recommended hero

> A minimalist technical illustration, 3:2 landscape. Dark near-black canvas
> (#0B0B0F). Centred: a single luminous indigo line (#4F46E5) travelling left to
> right, which **splits into two diverging paths** at a precise geometric fork.
> The upper path is dim, desaturated grey, and terminates in a small hollow red
> circle. The lower path stays bright indigo, is subtly thicker, and terminates
> in a solid teal-green dot (#0F766E) with a soft glow. Fine 1px hairline grid
> in the background at 4% opacity, like engineering graph paper. A thin dashed
> vertical line marks the exact fork point.
>
> Style: precise vector geometry, Swiss/International-style information design,
> the aesthetic of a Stripe or Cloudflare engineering blog diagram. Flat, no 3D,
> no bevels, no lens flare. Generous negative space — the composition should feel
> calm and confident, not busy.
>
> Absolutely no text, no letters, no numbers, no logos, no UI chrome.

**Why it works:** the fork *is* the project — one decision path replays and
fails, the other re-decides and holds. Reads instantly at thumbnail size.

---

## Prompt 2 · "Two agents, one budget" — most distinctive

> A minimalist technical illustration, 3:2 landscape. Warm off-white canvas
> (#FAFAF9). Two identical small geometric tokens — flat indigo (#4338CA) rounded
> squares — sit on the left, each with a thin line reaching right toward a single
> shared horizontal bar that represents a budget. Both lines arrive at the **same
> point** on the bar. The bar is deep teal (#0F766E) and is drawn as a container
> with a clear boundary; a portion beyond that boundary is filled in muted red
> (#B91C1C), showing an overflow past the edge.
>
> One thin dashed line loops back from the bar to the second token — a return
> path, drawn lighter, suggesting reconsideration.
>
> Style: flat vector, editorial infographic, Monocle-magazine restraint. Crisp
> 1.5px strokes, small sharp arrowheads, deliberate whitespace. Muted
> professional palette, no gradients, no shadows, no 3D, no glow.
>
> No text, no letters, no numbers, no logos.

**Why it works:** it shows the actual failure — two readers, one budget, an
overflow — plus the loop back that is the fix. Light background stands out in a
gallery where most entries are dark.

---

## Prompt 3 · "Column vs document" — the concept, best as image #2

> A minimalist conceptual illustration, 3:2 landscape, split into two balanced
> halves by a thin vertical hairline.
>
> LEFT: a clean database table abstraction — three or four stacked horizontal
> bars with one column highlighted in solid teal (#0F766E). Precise, rigid,
> gridded. A small solid check mark in green (#15803D) below it.
>
> RIGHT: an abstract document — a soft rectangle with four ragged lines of
> "text" rendered as grey bars of varying length, one line highlighted in violet
> (#7C3AED). Looser, organic, slightly rotated. A small hollow red circle
> (#B91C1C) below it.
>
> Background: warm off-white (#FAFAF9), faint 1px grid at 4% opacity.
>
> Style: flat vector, technical editorial, the visual language of a systems
> paper figure. Balanced composition, generous margins, no gradients, no
> shadows, no skeuomorphism.
>
> No text, no letters, no numbers, no logos.

**Why it works:** this is the conceptual contribution — one rule lives in a
column, one lives in a document, and only one of them is expressible in SQL.

---

## Adjustments if a result misses

| Problem | Say this |
|---|---|
| Too busy | *"Remove 40% of the elements. Much more negative space."* |
| Too generic / AI-looking | *"Flatter, more geometric. Think engineering diagram, not digital art."* |
| Colours off | *"Restrict strictly to #0B0B0F, #4338CA, #0F766E, #B91C1C. No other hues."* |
| It added text | *"No text of any kind — no letters, no numbers, no watermarks."* |
| Wrong ratio | *"Strict 3:2 landscape, 1536×1024."* |

## Suggested gallery order

1. **Prompt 1** — the hero
2. **The architecture diagram** from `docs/ARCHITECTURE_PROMPT.md`
3. **Prompt 3** — the two-rules concept
4. A **screenshot of the live demo** at <https://rajj28.github.io/racelab/>, cropped 3:2 on the five approach cards
5. A **terminal screenshot** of the compiler returning `UNENFORCEABLE` on our own policy — the most memorable single frame in the project

> **A note worth taking seriously:** a real screenshot of the running system
> often outperforms generated art with technical judges. If you only have time
> for two images, use the architecture diagram and a demo screenshot. The
> generated logo is polish; the evidence is the substance.
