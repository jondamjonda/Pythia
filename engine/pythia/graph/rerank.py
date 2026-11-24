"""Cross-encoder reranker: a precise second-pass ordering of a retrieved shortlist.

`BAAI/bge-reranker-v2-m3` reads the query and the provision text jointly and scores relevance far
more sharply than bi-encoder cosine. Its cost is linear in the number of candidates, so it only runs
on a shortlist (at most 50). Runs locally (MPS when available), no API key needed.
"""

from __future__ import annotations

RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
_model = None


def model():
    global _model
    if _model is None:
        import torch
        from sentence_transformers import CrossEncoder

        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = CrossEncoder(RERANK_MODEL, device=dev, max_length=512)
    return _model


def rerank(query: str, candidates: list[tuple[str, str]], top_k: int | None = None) -> list[tuple[str, float]]:
    """Score `(id, text)` candidates against the query.

    Returns `(id, score)` pairs sorted by descending relevance, truncated to `top_k` when given.
    """
    if not candidates:
        return []
    scores = model().predict([(query, text) for _, text in candidates])
    ranked = sorted(((cid, float(s)) for (cid, _), s in zip(candidates, scores)), key=lambda x: -x[1])
    return ranked[:top_k] if top_k else ranked
