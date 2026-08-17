"""racelab -- a conflict-aware transaction wrapper.

The library's claim is narrow and specific: when a decision is derived from
state read inside a transaction, and that transaction fails to serialize, the
decision is not automatically still valid. Replaying it is a choice, and often
the wrong one. `racelab` gives you a place to put the re-reasoning step.

The allocation scenario in `scenarios/` demonstrates this. It is not the
library.
"""

__version__ = "0.1.0"
