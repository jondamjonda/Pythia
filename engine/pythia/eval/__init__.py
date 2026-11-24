"""Offline measurement modules for the claim-verification loop.

Pure, import-safe measurement code that never changes engine behaviour: the modules score the
loop's output (the claim ledger) against gold labels or an independent oracle. It is a package so a
live-database oracle and a pure self-test can live side by side without importing torch or touching
the database at import time.
"""
