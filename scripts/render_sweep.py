"""Re-render a sweep report from its persisted raw cells.

`run_sweep.py` writes the raw per-cell aggregates to JSON *before* rendering,
on the argument that a sweep is expensive and the report is only a view of it.
This is the script that makes that argument true rather than aspirational: it
reads the JSON and produces the report, so the presentation can be corrected
without spending another 40 minutes of measurement.

It earned its place immediately. The corrected sweep completed all twenty cells
and then died in `write_text` on a bad `--out` path, which under the old
arrangement would have destroyed the whole run.

    python scripts/render_sweep.py results/sweep_fixed.json
    python scripts/render_sweep.py results/sweep_fixed.json --out sweep_fixed.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from racelab.arms import ArmId
from scripts.run_sweep import RESULTS, _print_safe, build_scenario, render


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw", type=pathlib.Path, help="the .json written by run_sweep")
    ap.add_argument("--out", default=None,
                    help="filename under results/ to write; omit to only print")
    args = ap.parse_args()

    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    meta = raw["meta"]

    cells = {}
    for cell in raw["cells"]:
        key = (cell["window"], ArmId(cell["arm"]))
        cells[key] = {k: v for k, v in cell.items() if k not in ("window", "arm")}

    # render() reads only these four fields off args.
    shim = types.SimpleNamespace(
        runs=meta["runs"], agents=meta["agents"],
        windows=meta["windows"], provider=meta["provider"],
    )

    report = render(cells, shim, build_scenario(), meta["elapsed_s"])
    _print_safe(report)

    if args.out:
        RESULTS.mkdir(parents=True, exist_ok=True)
        target = RESULTS / pathlib.Path(args.out).name
        target.write_text(report, encoding="utf-8")
        print(f"\nwritten to {target.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
