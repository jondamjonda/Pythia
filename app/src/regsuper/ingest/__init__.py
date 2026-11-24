"""Ingestion pipelines that turn EUR-Lex, e-Sbírka and ČNB sources into provision rows.

Each source has its own walker that converts the source XML or PDF into GFM Markdown (the
format choice is explained in amendments/01-markdown-ingestion.md) and emits provision and
cross-reference rows.
"""
