"""Adapters that put the conflict-aware protocol inside other frameworks.

Each adapter is optional and imports its framework lazily, so `racelab` itself
depends only on `psycopg`. Importing an adapter without its framework installed
raises with the install command rather than an unhelpful traceback.
"""
