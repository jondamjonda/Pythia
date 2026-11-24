"""HTTP client for the Pythia reasoning engine sidecar.

Pythia is the standalone Graph-RAG legal reasoning engine; it answers a legal question with a
grounded, cited answer from its own corpus. This package is a thin, feature-flagged HTTP client. It
does not replace the app's L2/L3 OpenAI classifiers (different task shapes, separate corpus
database). See `client.py` and `regsuper/api/pythia.py`.
"""
