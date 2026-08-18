# Devpost gallery images

Four images, in the order they should be uploaded. Every one is a **real
screenshot** of this project running — no generated art, nothing mocked up.

All are exactly **3:2**, the ratio Devpost recommends, and all are well under
the 5 MB per-file cap.

| # | File | Size | What it carries |
|---|---|---|---|
| 1 | `1-hook-thumbnail.png` | 1200×800 · 278 KB | **The thumbnail.** The demo page's hero — *"Two agents checked the budget. Both said yes."* |
| 2 | `2-architecture-transaction-boundary.png` | 1800×1200 · 411 KB | The mechanism: one transaction, one statement reading all four inputs, and the `40001 → re-decide (not replay)` loop |
| 3 | `3-results-five-arms.png` | 1200×800 · 416 KB | The evidence: five approaches, `$450 / $540 / $225` in red, `$80` half-red, **`$45` all green** |
| 4 | `4-memory-in-action.png` | 1200×800 · 360 KB | CockroachDB memory visibly at work — the real `memories` rows, one superseded and struck through, one flagged as arriving mid-run |
| 5 | `5-demo-animation-3x2.gif` | 900×600 · 154 KB | The animation. `docs/demo.gif` was 900×520 (1.73:1); padded to 3:2 with its own background rather than cropped, because a centre-crop cuts the `$110` / `$100` figures the animation exists to show |

## Why #1 is the thumbnail

It was chosen by testing, not taste. Devpost renders gallery cards small, so the
only question that matters is what survives being shrunk:

| Rendered at | Image 1 | The architecture crop |
|---|---|---|
| 600 px | fully legible | headlines only |
| **300 px** | **headline still reads** | nothing readable |

Image 1 is a huge serif headline and six words. That is what a thumbnail needs.
Image 2 carries roughly fifteen text elements — excellent once a judge clicks
in, unreadable in a grid.

## Why #4 earns its place

The hackathon's judging guidance asks entrants to *show the CockroachDB memory
in action visibly, rather than narrate it*. Image 4 does that in one still: the
actual rows from the `memories` table, `hero-m1` struck through as superseded,
`hero-m5` flagged `ARRIVES MID-RUN` in red, and `VECTOR(1024)` + CockroachDB's
vector index + Amazon Bedrock Titan named in the copy beside them.

It is the only image that answers a scoring criterion directly.

## How these were produced

Captured from `docs/index.html` served locally, then cropped to 3:2.

Two notes for anyone regenerating them:

- **The capture viewport is fixed** at roughly 1366×633 regardless of window
  size, so resizing the browser does not help fit more on screen. Setting
  `document.body.style.zoom` (0.62 for the five cards, 0.72 for the memory
  section) shrinks the content until it fits, and keeps the result sharp instead
  of upscaled from a partial view.
- **Pad, don't crop, to reach 3:2.** The hero and the architecture panel are both
  wider than 3:2. Cropping to the ratio cuts into the type — on the architecture
  panel it severs `policy_constraints → compiled` mid-phrase. Sampling the flat
  background colour and padding top and bottom preserves everything.

## Regenerating

```bash
python scripts/capture_ui.py     # re-run the five arms against the live cluster
python scripts/build_ui.py       # rebuild docs/index.html
python -m http.server 8901 --directory docs
# then screenshot and crop to 3:2
```

Because `docs/` is published by GitHub Pages, these are also reachable directly,
which is convenient for embedding elsewhere:

```
https://rajj28.github.io/racelab/thumbnails/1-hook-thumbnail.png
```
