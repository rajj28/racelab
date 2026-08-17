# Superseded results — do not cite

These are the outputs of the **first** swept experiment. They are kept because
`docs/METHODOLOGY.md` Entry 10 discusses them and a reader should be able to see
what was actually discarded, not just read a description of it.

**They are invalid for the memory ablation.** Each agent opened its own
connection to CockroachDB Cloud after its arrival offset; that TLS handshake
costs ~391 ms and sat in front of the first memory read, so the superseding
policy always committed before any agent could read the old one. The arm defined
by reasoning over stale memory was never stale. The `C − C-ops` effects reported
here (`−28.0` at 1000 ms and 1500 ms) are jitter in that race, not a response to
the arrival window.

The `A`/`B` comparisons and hard-limit results here are not affected by the
artifact, but there is no reason to cite this file for them either — the
corrected sweep measures the same thing with a working instrument.

**Current results:** `results/sweep_fixed.md`, raw cells in
`results/sweep_fixed.json`.
