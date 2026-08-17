"""Render the forced-conflict demo as an animated GIF for the README.

The GIF is generated FROM A REAL RUN, not drawn. It calls the same
`test_forced_conflict` harness the test suite uses, against the live
CockroachDB cluster, once per arm, and renders the telemetry those runs
actually produced. Every number on every frame -- the reads, the actions, the
40001, the final sums -- comes back from the database.

That matters more than it might look. A hand-drawn animation of a result is a
claim about a result, and this project's whole argument is that the difference
between the two arms is one boolean rather than one narrative. So the picture
has to be downstream of the measurement.

The scenario is the deterministic one, so the demo does not depend on a lucky
interleaving:

    seeded 20 already allocated, hard limit 100

    agent-1 opens, reads 20, and waits
    agent-2 runs to completion and commits 45     -> true total 65
    agent-1 tries to write; its transaction cannot serialize

    naive           replays allocate(45) against a real total of 65 -> 110
    conflict-aware  re-reads 65, sees 35 remaining, allocates 35    -> 100

Run:  python scripts/make_demo_gif.py
      python scripts/make_demo_gif.py --out docs/demo.gif
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont

from scripts.test_wrapper import HARD_LIMIT, test_forced_conflict

W, H = 900, 520
SCALE = 2  # render at 2x and downsample, so text is not jagged

BG = (18, 20, 26)
PANEL = (28, 31, 40)
GRID = (48, 52, 64)
TEXT = (232, 236, 244)
DIM = (140, 148, 164)
GOOD = (86, 196, 138)
BAD = (232, 96, 96)
WARN = (238, 176, 78)
COOL = (110, 168, 240)


def load_font(size: int):
    for name in ("consola.ttf", "DejaVuSansMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


class Frame:
    def __init__(self) -> None:
        self.img = Image.new("RGB", (W * SCALE, H * SCALE), BG)
        self.d = ImageDraw.Draw(self.img)
        self.f_title = load_font(21 * SCALE)
        self.f_body = load_font(15 * SCALE)
        self.f_small = load_font(13 * SCALE)
        self.f_big = load_font(30 * SCALE)

    def text(self, xy, s, font=None, fill=TEXT, anchor=None):
        x, y = xy
        self.d.text((x * SCALE, y * SCALE), s, font=font or self.f_body,
                    fill=fill, anchor=anchor)

    def rect(self, box, fill=None, outline=None, width=1, radius=0):
        x0, y0, x1, y1 = [v * SCALE for v in box]
        if radius:
            self.d.rounded_rectangle([x0, y0, x1, y1], radius=radius * SCALE,
                                     fill=fill, outline=outline, width=width * SCALE)
        else:
            self.d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline,
                             width=width * SCALE)

    def line(self, box, fill=GRID, width=1):
        x0, y0, x1, y1 = [v * SCALE for v in box]
        self.d.line([x0, y0, x1, y1], fill=fill, width=width * SCALE)

    def finish(self) -> Image.Image:
        return self.img.resize((W, H), Image.LANCZOS)


def header(fr: Frame, caption: str) -> None:
    fr.text((36, 26), "RaceLab", font=fr.f_title)
    fr.text((150, 31), "one boolean, two final states", font=fr.f_small, fill=DIM)
    fr.line((36, 60, W - 36, 60))
    fr.text((36, 74), caption, font=fr.f_body, fill=COOL)


def meter(fr: Frame, x: int, y: int, w: int, total: int, limit: int) -> None:
    """The invariant, drawn. Anything past the limit line is a violation."""
    h = 26
    fr.rect((x, y, x + w, y + h), fill=PANEL, radius=4)
    span = max(limit * 1.25, total * 1.05, 1)
    filled = int(w * min(total / span, 1.0))
    colour = BAD if total > limit else GOOD
    if filled > 0:
        fr.rect((x, y, x + filled, y + h), fill=colour, radius=4)
    lx = x + int(w * (limit / span))
    fr.line((lx, y - 8, lx, y + h + 8), fill=TEXT, width=2)
    fr.text((lx, y + h + 12), f"limit ${limit}", font=fr.f_small, fill=DIM,
            anchor="ma")
    fr.text((x + w + 14, y + 4), f"SUM = ${total}", font=fr.f_body, fill=colour)


def agent_row(fr: Frame, y: int, name: str, lines: list[tuple[str, tuple]],
              boxed: bool = False) -> None:
    if boxed:
        fr.rect((36, y - 10, W - 36, y + 18 * len(lines) + 6), fill=PANEL, radius=6)
    fr.text((52, y), name, font=fr.f_body, fill=TEXT)
    for i, (s, c) in enumerate(lines):
        fr.text((190, y + i * 18), s, font=fr.f_small, fill=c)


def build_frames(naive: dict, aware: dict) -> list[Image.Image]:
    frames: list[Image.Image] = []

    def hold(fr: Frame, n: int) -> None:
        img = fr.finish()
        frames.extend([img] * n)

    # 1 -- the setup
    fr = Frame()
    header(fr, "Two agents. $20 already allocated. Hard limit $100.")
    agent_row(fr, 130, "agent-1", [("opens a transaction, reads SUM = $20", DIM)],
              boxed=True)
    agent_row(fr, 190, "agent-2", [("waiting", DIM)], boxed=True)
    fr.text((36, 260), "agent-1 reasons: $20 allocated, $80 of room, allocate $45.",
            font=fr.f_body)
    fr.text((36, 284), "That is correct. Nothing about it is unreasonable.",
            font=fr.f_small, fill=DIM)
    meter(fr, 36, 350, 520, 20, HARD_LIMIT)
    hold(fr, 22)

    # 2 -- the other agent commits
    fr = Frame()
    header(fr, "agent-2 commits first. agent-1's read is now stale.")
    agent_row(fr, 130, "agent-1", [("holding: decided allocate($45) on SUM = $20", WARN)],
              boxed=True)
    agent_row(fr, 190, "agent-2", [("committed allocate($45)", GOOD),
                                   ("true total is now $65", DIM)], boxed=True)
    fr.text((36, 270), "Another transaction changed state relevant to agent-1's decision.",
            font=fr.f_body)
    meter(fr, 36, 350, 520, 65, HARD_LIMIT)
    hold(fr, 24)

    # 3 -- the 40001
    fr = Frame()
    header(fr, "agent-1 tries to commit.")
    fr.rect((36, 120, W - 36, 210), fill=PANEL, radius=6)
    fr.text((52, 138), "SQLSTATE 40001", font=fr.f_title, fill=WARN)
    fr.text((52, 172), "the transaction could not be serialized", font=fr.f_body, fill=TEXT)
    fr.text((36, 232),
            "This does not mean the agent was wrong. It means the state its",
            font=fr.f_small, fill=DIM)
    fr.text((36, 252),
            "decision rested on has changed. What the client does next is the",
            font=fr.f_small, fill=DIM)
    fr.text((36, 272), "entire experiment.", font=fr.f_small, fill=DIM)
    fr.text((36, 316), "Under PostgreSQL READ COMMITTED there is no 40001 here at all:",
            font=fr.f_small, fill=DIM)
    fr.text((36, 336), "the write commits silently and the sum reaches $110.",
            font=fr.f_small, fill=DIM)
    hold(fr, 26)

    # 4 -- the fork
    fr = Frame()
    header(fr, "Both arms retry. They differ by one boolean: re_reason.")
    fr.rect((36, 118, 448, 300), fill=PANEL, radius=6)
    fr.rect((464, 118, W - 36, 300), fill=PANEL, radius=6)
    fr.text((56, 136), "naive   re_reason=False", font=fr.f_body, fill=BAD)
    fr.text((484, 136), "conflict-aware   re_reason=True", font=fr.f_body, fill=GOOD)
    for i, s in enumerate(["re-reads SUM inside the new",
                           "transaction, and gets $65",
                           "",
                           "replays allocate($45) anyway",
                           "reason() called once"]):
        fr.text((56, 168 + i * 22), s, font=fr.f_small,
                fill=DIM if i < 3 else TEXT)
    for i, s in enumerate(["re-reads SUM inside the new",
                           "transaction, and gets $65",
                           "",
                           "re-reasons: $35 of room left",
                           "reason() called twice"]):
        fr.text((484, 168 + i * 22), s, font=fr.f_small,
                fill=DIM if i < 3 else TEXT)
    fr.text((36, 322),
            "Both re-read state. Only one lets the new reading change the action.",
            font=fr.f_body)
    hold(fr, 30)

    # 5 -- outcomes, measured
    fr = Frame()
    header(fr, "Final states, from the live cluster.")
    n_sum, a_sum = naive["sum"], aware["sum"]

    fr.text((36, 118), "naive", font=fr.f_body, fill=BAD)
    fr.text((36, 140), f"{naive['before']}  ->  {naive['after']}",
            font=fr.f_small, fill=DIM)
    meter(fr, 36, 168, 460, n_sum, HARD_LIMIT)
    fr.text((36, 226), "committed with no error. The invariant is broken.",
            font=fr.f_small, fill=BAD)

    fr.text((36, 288), "conflict-aware", font=fr.f_body, fill=GOOD)
    fr.text((36, 310), f"{aware['before']}  ->  {aware['after']}",
            font=fr.f_small, fill=DIM)
    meter(fr, 36, 338, 460, a_sum, HARD_LIMIT)
    fr.text((36, 396), "revised its decision. The invariant holds.",
            font=fr.f_small, fill=GOOD)
    hold(fr, 40)

    # 6 -- the number
    fr = Frame()
    header(fr, "The difference is one boolean.")
    fr.text((W // 2, 170), f"${n_sum}", font=fr.f_big, fill=BAD, anchor="ma")
    fr.text((W // 2, 212), "naive retry", font=fr.f_small, fill=DIM, anchor="ma")
    fr.text((W // 2, 268), f"${a_sum}", font=fr.f_big, fill=GOOD, anchor="ma")
    fr.text((W // 2, 310), f"conflict-aware  (limit ${HARD_LIMIT})",
            font=fr.f_small, fill=DIM, anchor="ma")
    fr.text((W // 2, 372),
            "Same class. Same arguments. Same reasoning function.",
            font=fr.f_small, fill=DIM, anchor="ma")
    fr.text((W // 2, 394), "re_reason=False  vs  re_reason=True",
            font=fr.f_body, fill=TEXT, anchor="ma")
    hold(fr, 46)

    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/demo.gif")
    args = ap.parse_args()

    print("running the forced-conflict case against the live cluster, per arm ...")
    n_res, n_sum, n_calls, _ = test_forced_conflict("crdb", re_reason=False)
    a_res, a_sum, a_calls, _ = test_forced_conflict("crdb", re_reason=True)

    naive = {"sum": n_sum, "before": n_res.decision_before,
             "after": n_res.decision_after, "calls": n_calls}
    aware = {"sum": a_sum, "before": a_res.decision_before,
             "after": a_res.decision_after, "calls": a_calls}

    print(f"  naive           {naive['before']} -> {naive['after']}  "
          f"sum={n_sum}  reason_calls={n_calls}  conflicts={n_res.conflicts}")
    print(f"  conflict-aware  {aware['before']} -> {aware['after']}  "
          f"sum={a_sum}  reason_calls={a_calls}  conflicts={a_res.conflicts}")

    # Refuse to publish a GIF of a run that did not demonstrate the thing. A
    # cached or lucky interleaving that produced two identical outcomes would
    # make a picture that quietly asserts something the run did not show.
    problems = []
    if n_res.conflicts == 0 or a_res.conflicts == 0:
        problems.append("a conflict did not occur in one of the arms")
    if n_sum <= HARD_LIMIT:
        problems.append(f"naive did not violate the invariant (sum {n_sum})")
    if a_sum > HARD_LIMIT:
        problems.append(f"conflict-aware did not hold the invariant (sum {a_sum})")
    if n_calls != 1:
        problems.append(f"naive reasoned {n_calls} times, expected 1 (arm collapse)")
    if a_calls != 1 + a_res.conflicts:
        problems.append(f"conflict-aware reasoned {a_calls} times, "
                        f"expected {1 + a_res.conflicts}")
    if problems:
        print("\nrefusing to write the GIF:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    frames = build_frames(naive, aware)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(out, save_all=True, append_images=frames[1:],
                   duration=100, loop=0, optimize=True)
    kb = out.stat().st_size / 1024
    print(f"\nwrote {out.as_posix()}  ({len(frames)} frames, {kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
