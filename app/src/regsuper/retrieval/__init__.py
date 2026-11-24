"""Hybrid retrieval pipeline: search, rank fusion, LLM query expansion, reranking, expansion.

The search primitives (dense, sparse, metadata filter), reciprocal rank fusion and the LLM legs
(HyDE, query rewriting) compose into `retrieve()`, which adds cross-encoder reranking and
hierarchical context expansion on top.
"""

from regsuper.retrieval.expand import (
    DefinitionLite,
    ExpandedProvision,
    ProvisionLite,
    expand,
    expand_many,
)
from regsuper.retrieval.hybrid import (
    RetrievalFilters,
    SearchHit,
    SearchLevel,
    dense_search,
    metadata_filter_ids,
    metadata_filter_sql,
    sparse_search,
)
from regsuper.retrieval.hyde import hyde
from regsuper.retrieval.orchestrate import (
    RetrievalResult,
    RetrieveDiagnostics,
    retrieve,
    retrieve_ids,
)
from regsuper.retrieval.rerank import RerankedHit, rerank
from regsuper.retrieval.rewrite import rewrite
from regsuper.retrieval.rrf import FusedHit, reciprocal_rank_fusion

__all__ = [
    "DefinitionLite",
    "ExpandedProvision",
    "FusedHit",
    "ProvisionLite",
    "RerankedHit",
    "RetrievalFilters",
    "RetrievalResult",
    "RetrieveDiagnostics",
    "SearchHit",
    "SearchLevel",
    "dense_search",
    "expand",
    "expand_many",
    "hyde",
    "metadata_filter_ids",
    "metadata_filter_sql",
    "reciprocal_rank_fusion",
    "rerank",
    "retrieve",
    "retrieve_ids",
    "rewrite",
    "sparse_search",
]
