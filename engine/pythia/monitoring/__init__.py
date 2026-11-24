"""Offline, pure staleness and health checks over the corpus.

Distinct from `pythia.ingest.monitoring`, the database-bound, human-in-the-loop discovery gate:
everything here is a pure function over data the caller supplies (a JSON registry plus corpus
rows), with no database access and no clock, so it is easy to test offline. A scheduled job is
expected to call these checkers.
"""
