"""Conduit input schemas, shared by the MCP and REST channels.

These pydantic models are the single definition from which both the MCP tool `inputSchema` and
the REST request bodies are derived, so the two channels cannot drift apart. They carry
validation only, no logic. Output payloads travel inside `envelope.Envelope.data`, so their
shapes live in `envelope.py` and `tools.py`.

The six tools are fixed; there is deliberately no case-law input model.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# Closed enums shared across tool inputs.
Jurisdiction = Literal["CZ", "EU"]
Domain = Literal[
    "gdpr",
    "aml",
    "corporate",
    "contracts",
    "employment",
    "financial-market",
    "civil",
    "tax",
    "real-estate",
]


class SearchCzechLawInput(BaseModel):
    """Input for `search_czech_law`."""

    query: str = Field(..., min_length=3, max_length=1000)
    as_of: date | None = None  # point-in-time; defaults to today
    jurisdiction: list[Jurisdiction] | None = None  # default: both
    domains: list[Domain] | None = None  # maps to sector_tags
    top_k: int = Field(default=10, ge=1, le=20)


class GetProvisionInput(BaseModel):
    """Input for `get_provision`."""

    act: str = Field(..., min_length=1)  # external id ('240/2013') or alias ('GDPR','MiCA')
    as_of: date | None = None
    section: str | None = None  # restrict to one § (e.g. '§15'); None = whole act


class WhatsInForceInput(BaseModel):
    """Input for `whats_in_force`."""

    act: str = Field(..., min_length=1)
    as_of: date | None = None


class ListObligationsInput(BaseModel):
    """Input for `list_obligations`.

    The tool returns corpus-level "rules for businesses of this type", not a per-org analysis,
    so it never runs the blocking L0 to L4 analysis cascade.
    """

    sector: str | None = None
    entity_type: str | None = None
    jurisdiction: list[Jurisdiction] | None = None
    as_of: date | None = None


class CheckComplianceInput(BaseModel):
    """Input for `check_compliance`.

    The compliance guard always runs first: a situation that asks for reserved legal advice is
    refused without retrieval (and still metered).
    """

    situation: str = Field(..., min_length=3, max_length=4000)
    jurisdiction: list[Jurisdiction] | None = None
    as_of: date | None = None


class AnswerLegalQuestionInput(BaseModel):
    """Input for `answer_legal_question`, the highest-risk tool.

    Requires the `answer.generate` scope (top plan only). Compliance gates run both before and
    after generation, and `envelope.finalize` checks every supporting span against its cited
    provision.
    """

    question: str = Field(..., min_length=3, max_length=2000)
    as_of: date | None = None
    jurisdiction: list[Jurisdiction] | None = None


__all__ = [
    "AnswerLegalQuestionInput",
    "CheckComplianceInput",
    "Domain",
    "GetProvisionInput",
    "Jurisdiction",
    "ListObligationsInput",
    "SearchCzechLawInput",
    "WhatsInForceInput",
]
