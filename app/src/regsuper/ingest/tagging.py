"""Tag provisions with sector, activity and entity tags from a fixed vocabulary.

Without these tags the L1 candidate filter can only filter by jurisdiction, so every project
gets the same pool of about 7,900 provisions; that dilutes L2 precision and spends LLM calls on
plainly irrelevant provisions. One call per provision (TAG_MODEL) returns Pydantic-validated
JSON using the same buckets `l0_intake.py` maps project intake to, so project and provision
tags can match. A full-corpus run costs about $3 to $5. Re-runs skip provisions whose
`metadata->>tagged_at` is set unless `--force` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.observability.events import emit
from regsuper.observability.logging_config import configure_logging
from regsuper.storage.audit import hash_prompt
from regsuper.storage.database import session_scope

TAG_MODEL = "gpt-4.1-mini"
TAG_MAX_TOKENS = 300
TAG_TEMPERATURE = 0.1
DEFAULT_CONCURRENCY = 12  # safe for OpenAI usage tier 1; raise it if you have headroom


# Allowed vocabularies. They must match l0_intake.SECTOR_TO_TAGS and ENTITY_TO_ACTIVITY so that
# project-side tags overlap with provision-side tags at filter time.
#
# Each tuple mirrors the union of every value l0_intake can produce: sectors are the union of
# SECTOR_TO_TAGS values, activities the union of ENTITY_TO_ACTIVITY values, and entities the
# English keys of ENTITY_TO_ACTIVITY. Each tuple lists the finance tags first, then the
# non-finance tags added to cover the wider set of Czech acts. Add nothing l0_intake lacks.
# The two Czech AIF aliases (investicní_fond, investiční_fond) are intake-language synonyms of
# alternative_investment_fund rather than English tagging vocabulary, so they are deliberately
# not entity tags.
ALLOWED_SECTORS = (
    "fin_services",
    "investment_funds",
    "capital_markets",
    "banking",
    "aml",
    "consumer_credit",
    "payments",
    "insurance",
    "real_estate",
    "energy",
    "tech",
    "data_protection",
    "ai_act",
    "healthcare",
    "pharma",
    "medical_devices",
    "manufacturing",
    "product_safety",
    "transport",
    "logistics",
    "retail",
    "consumer_protection",
    "crypto",
    "mica",
    "environmental",
    "utilities",
    "e_money",
    "solvency",
    "construction",
    "leasing",
    # Non-finance sector tags emitted by l0_intake.SECTOR_TO_TAGS.
    "civil",
    "civil_obligations",
    "civil_procedure",
    "enforcement",
    "administrative",
    "public_administration",
    "foia",
    "labour",
    "employment",
    "occupational_safety",
    "social_security",
    "pension",
    "ip",
    "copyright",
    "real_property",
    "cadastre",
    "competition",
    "procurement",
    "public_procurement",
    "tax",
    "accounting",
    "criminal",
    "criminal_procedure",
    "commercial",
    "corporate",
)
ALLOWED_ACTIVITIES = (
    "fund_management",
    "marketing",
    "passporting",
    "risk_management",
    "investment_services",
    "client_funds",
    "mifid",
    "best_execution",
    "lending",
    "deposit_taking",
    "capital_requirements",
    "payments",
    "e_money",
    "caspr",
    "data_protection",
    "solvency",
    "fit_and_proper",
    "reporting",
    "disclosure",
    "valuation",
    "remuneration",
    "outsourcing",
    "complaints",
    "consumer_protection",
    # Activity tags l0_intake.ENTITY_TO_ACTIVITY emits for non-finance entity types, plus
    # finance activities that recur there.
    "aml",
    "mica",
    "insurance",
    "employment",
    "occupational_safety",
    "public_administration",
    "foia",
    "procurement",
    "public_procurement",
    "transparency",
    "real_property",
    "construction",
    "environmental",
    "copyright",
    "ip",
    "tax",
    "accounting",
)
ALLOWED_ENTITIES = (
    "aif",
    "aifm",
    "ucits",
    "investment_company",
    "investment_firm",
    "bank",
    "credit_institution",
    "payment_institution",
    "e_money",
    "fintech",
    "crypto_provider",
    "casp",
    "insurance_undertaking",
    "broker",
    "depositary",
    "auditor",
    "competent_authority",
    "member_state",
    "esma",
    "eba",
    "eiopa",
    "ecb",
    # English entity-type keys of l0_intake.ENTITY_TO_ACTIVITY for the non-finance sectors.
    "alternative_investment_fund",
    "employer",
    "public_authority",
    "contracting_authority",
    "data_controller",
    "landlord",
    "developer",
    "ip_holder",
    "taxpayer",
)


TAG_PROMPT = """You are tagging a regulatory provision for a retrieval system.
Read the provision text and assign it to up to FOUR sector tags, FOUR activity
tags, and FOUR entity tags from the controlled vocabularies below. Use ONLY
values from the lists — do not invent new tags.

Rules:
- Pick tags ONLY if the provision is *directly relevant* to that bucket, not
  if the provision merely mentions the area. Err on fewer tags rather than
  more. A definition of "investor" is `fin_services` only; the article
  governing fund-marketing notification is `fin_services` + `investment_funds`.
- "is_pure_definition": true if the provision is a definitions / scope /
  preamble article rather than an operative rule. These provisions get tags
  so the retriever finds them, but L2 should down-weight them later.
- "confidence": 0.0-1.0 reflecting how clear-cut the tagging is.

SECTOR vocabulary: {sector_list}
ACTIVITY vocabulary: {activity_list}
ENTITY vocabulary: {entity_list}

PROVISION
source: {publisher} {external_id}, {legal_hierarchy}
hierarchy: {hierarchy_path}
label: {label}
text:
<<<
{provision_text}
>>>

Output JSON only:
{{
  "sector_tags":   ["<tag>", ...],
  "activity_tags": ["<tag>", ...],
  "entity_tags":   ["<tag>", ...],
  "is_pure_definition": true|false,
  "confidence": 0.0-1.0
}}"""
PROMPT_HASH = hash_prompt(TAG_PROMPT)


class TagResult(BaseModel):
    sector_tags: list[str] = Field(default_factory=list)
    activity_tags: list[str] = Field(default_factory=list)
    entity_tags: list[str] = Field(default_factory=list)
    is_pure_definition: bool = False
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("sector_tags", "activity_tags", "entity_tags", mode="before")
    @classmethod
    def _filter_to_allowed(cls, v: Any) -> list[str]:
        """Normalise and de-duplicate tags, keeping at most four.

        The model may return duplicates or odd spellings; unknown tags are removed later in
        `_tag_one`.
        """
        if not isinstance(v, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for raw in v:
            if not isinstance(raw, str):
                continue
            t = raw.strip().lower().replace(" ", "_")
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out[:4]


@dataclass
class TagCall:
    provision_id: UUID
    result: TagResult | None
    error: str | None
    latency_ms: int


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
async def _load_pending_provisions(*, force: bool, limit: int | None) -> list[dict[str, Any]]:
    """Return the provisions that still need tagging.

    `force=True` selects every provision, e.g. to re-tag after a vocabulary change.
    """
    where = "" if force else "WHERE (p.metadata->>'tagged_at') IS NULL"
    sql = f"""
        SELECT p.id, p.label, p.text, p.hierarchy_path::text,
               p.legal_hierarchy, sd.external_id, sd.publisher
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
          {where}
         ORDER BY p.source_document_id, p.ordinal
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql))).all()
    return [
        {
            "id": r[0],
            "label": r[1],
            "text": r[2],
            "hierarchy_path": r[3],
            "legal_hierarchy": r[4],
            "external_id": r[5],
            "publisher": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
async def _tag_one(client: AsyncOpenAI, prov: dict[str, Any]) -> TagCall:
    prompt = TAG_PROMPT.format(
        sector_list=", ".join(ALLOWED_SECTORS),
        activity_list=", ".join(ALLOWED_ACTIVITIES),
        entity_list=", ".join(ALLOWED_ENTITIES),
        publisher=prov["publisher"],
        external_id=prov["external_id"] or "",
        legal_hierarchy=prov["legal_hierarchy"],
        hierarchy_path=prov["hierarchy_path"],
        label=prov["label"] or "(unlabelled)",
        provision_text=(prov["text"] or "").strip()[:6000],
    )
    t0 = time.perf_counter()
    try:
        resp = await tracked_chat_completion(
            client,
            source="ingest.tagging",
            model=TAG_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=TAG_MAX_TOKENS,
            temperature=TAG_TEMPERATURE,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return TagCall(
            provision_id=prov["id"],
            result=None,
            error=repr(exc)[:200],
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    body = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return TagCall(
            provision_id=prov["id"], result=None, error=f"json: {exc}", latency_ms=latency_ms
        )
    try:
        result = TagResult.model_validate(data)
    except ValidationError as exc:
        return TagCall(
            provision_id=prov["id"],
            result=None,
            error=f"schema: {exc.errors()[:1]}",
            latency_ms=latency_ms,
        )

    # Final allow-list check: tags outside the vocabulary are dropped silently.
    result.sector_tags = [t for t in result.sector_tags if t in ALLOWED_SECTORS]
    result.activity_tags = [t for t in result.activity_tags if t in ALLOWED_ACTIVITIES]
    result.entity_tags = [t for t in result.entity_tags if t in ALLOWED_ENTITIES]
    return TagCall(provision_id=prov["id"], result=result, error=None, latency_ms=latency_ms)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _apply_tags(call: TagCall) -> None:
    if call.result is None:
        return
    now_iso = datetime.now(UTC).isoformat()
    meta_patch = {
        "tagged_at": now_iso,
        "tagged_by": TAG_MODEL,
        "tag_confidence": call.result.confidence,
        "is_pure_definition": call.result.is_pure_definition,
    }
    # `:meta` carries the JSONB merge patch. It is typed through bindparam so asyncpg serialises
    # it natively, avoiding a `::jsonb` cast that the prepared-statement parser rejects.
    stmt = sql_text(
        """
        UPDATE provision
           SET sector_tags = :sector_tags,
               activity_tags = :activity_tags,
               entity_tags = :entity_tags,
               metadata = COALESCE(metadata, '{}'::jsonb) || :meta
         WHERE id = :pid
        """
    ).bindparams(bindparam("meta", type_=JSONB))
    async with session_scope() as session:
        await session.execute(
            stmt,
            {
                "sector_tags": call.result.sector_tags,
                "activity_tags": call.result.activity_tags,
                "entity_tags": call.result.entity_tags,
                "meta": meta_patch,
                "pid": call.provision_id,
            },
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class TagRunReport:
    n_pending: int
    n_succeeded: int
    n_errors: int
    sample_errors: list[str]
    total_latency_ms: int


async def run_tagging(
    *,
    force: bool = False,
    limit: int | None = None,
    max_concurrency: int = DEFAULT_CONCURRENCY,
) -> TagRunReport:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    pending = await _load_pending_provisions(force=force, limit=limit)
    await emit("tagging.start", total=len(pending), force=force)

    if not pending:
        return TagRunReport(
            n_pending=0, n_succeeded=0, n_errors=0, sample_errors=[], total_latency_ms=0
        )

    client = get_openai()
    sem = asyncio.Semaphore(max_concurrency)
    sample_errors: list[str] = []
    done = 0
    succeeded = 0
    errored = 0
    t0 = time.perf_counter()

    async def _one(prov: dict[str, Any]) -> None:
        nonlocal done, succeeded, errored
        async with sem:
            call = await _tag_one(client, prov)
        if call.error:
            errored += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"{call.provision_id}: {call.error}")
        else:
            try:
                await _apply_tags(call)
                succeeded += 1
            except Exception as exc:
                errored += 1
                if len(sample_errors) < 5:
                    sample_errors.append(f"{call.provision_id}: apply: {exc!r}")
        done += 1
        if done % 100 == 0 or done == len(pending):
            await emit(
                "tagging.progress",
                done=done,
                total=len(pending),
                succeeded=succeeded,
                errors=errored,
            )

    await asyncio.gather(*(_one(p) for p in pending))
    elapsed = int((time.perf_counter() - t0) * 1000)
    await emit(
        "tagging.complete",
        total=len(pending),
        succeeded=succeeded,
        errors=errored,
        latency_ms=elapsed,
    )
    return TagRunReport(
        n_pending=len(pending),
        n_succeeded=succeeded,
        n_errors=errored,
        sample_errors=sample_errors[:5],
        total_latency_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    configure_logging()
    p = argparse.ArgumentParser(description="Tag every provision with sector/activity/entity tags.")
    p.add_argument(
        "--force", action="store_true", help="Re-tag rows that already have tagged_at set."
    )
    p.add_argument("--limit", type=int, default=None, help="Cap N provisions (for smoke).")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = p.parse_args()
    rep = asyncio.run(
        run_tagging(
            force=args.force,
            limit=args.limit,
            max_concurrency=args.concurrency,
        )
    )
    print(
        json.dumps(
            {
                "pending": rep.n_pending,
                "succeeded": rep.n_succeeded,
                "errors": rep.n_errors,
                "sample_errors": rep.sample_errors,
                "latency_ms": rep.total_latency_ms,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    _main()
