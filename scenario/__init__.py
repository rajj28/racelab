"""The allocation scenario: a demonstration of `racelab`, not the library.

The library is a conflict-aware transaction wrapper. This package is the
worked example that gives the experiment something concrete to measure -- a
shared authorization budget that several agents allocate against concurrently.
Everything scenario-specific lives here: the memory corpus, the ceiling
inference, the bounded action space. `racelab` itself knows about none of it.
"""
