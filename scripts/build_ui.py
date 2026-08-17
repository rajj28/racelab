"""Build the inspection UI: inject captured run data into the page template.

`docs/ui_template.html` is the source and is edited as HTML. It deliberately has
no `<!DOCTYPE>`, `<html>`, `<head>` or `<body>` tags, because that is the shape a
published Artifact wants -- the host supplies the skeleton. This script produces
the two outputs that shape needs:

  docs/ui.html        a complete standalone document a judge can double-click
  <artifact-out>      the body-only page, for publishing

One source, two wrappers, so the local file and the shared link can never drift.

The page is fully self-contained: the run data is embedded, there are no external
scripts, styles, fonts or images, and it works offline. That is a requirement
rather than a preference -- the published Artifact runs under a CSP that blocks
every external host, and a judge's laptop may have no network at all.

Run:  python scripts/capture_ui.py && python scripts/build_ui.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "docs" / "ui_template.html"
DATA = REPO / "docs" / "ui_data.json"
TOKEN = "/*__RACELAB_DATA__*/ null"

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Inspect a RaceLab race run: four arms, twenty agents, two invariants.">
</head>
<body>
"""
TAIL = "\n</body>\n</html>\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO / "docs" / "index.html"),
                    help="docs/index.html so GitHub Pages serves it at the site root")
    ap.add_argument("--artifact-out", default=None,
                    help="also write the body-only variant here, for publishing")
    args = ap.parse_args()

    if not DATA.exists():
        print(f"no captured data at {DATA}; run scripts/capture_ui.py first",
              file=sys.stderr)
        return 1

    template = TEMPLATE.read_text(encoding="utf-8")
    if TOKEN not in template:
        print(f"template is missing the data token {TOKEN!r}", file=sys.stderr)
        return 1

    payload = json.loads(DATA.read_text(encoding="utf-8"))

    # Sanity-check the payload against itself before baking it into a page. A UI
    # that renders a total the run did not reach is worse than no UI.
    for arm in payload["arms"]:
        reconstructed = 0
        for agent in arm["agents"]:
            if agent.get("outcome") == "committed" and agent.get("action", "").startswith("allocate("):
                reconstructed += int(agent["action"][len("allocate("):-1])
        if reconstructed != arm["final_sum"]:
            print(f"arm {arm['id']}: committed allocations sum to {reconstructed} "
                  f"but the run reported {arm['final_sum']}. Refusing to build a "
                  f"chart that disagrees with its own data.", file=sys.stderr)
            return 1
        if len(arm["agents"]) != payload["meta"]["agents"]:
            print(f"arm {arm['id']}: captured {len(arm['agents'])} agents of "
                  f"{payload['meta']['agents']}. Re-capture rather than shipping "
                  f"a timeline with missing rows.", file=sys.stderr)
            return 1

    # separators without spaces: this is embedded, not read by humans
    blob = json.dumps(payload, separators=(",", ":"))
    body = template.replace(TOKEN, blob)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEAD + body + TAIL, encoding="utf-8")
    print(f"wrote {out.relative_to(REPO).as_posix()}  "
          f"({out.stat().st_size / 1024:.0f} KB, standalone)")

    if args.artifact_out:
        ao = pathlib.Path(args.artifact_out)
        ao.parent.mkdir(parents=True, exist_ok=True)
        ao.write_text(body, encoding="utf-8")
        print(f"wrote {ao.as_posix()}  "
              f"({ao.stat().st_size / 1024:.0f} KB, artifact body)")

    arms = ", ".join(f"{a['id']}=${a['final_sum']}" for a in payload["arms"])
    print(f"arms: {arms}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
