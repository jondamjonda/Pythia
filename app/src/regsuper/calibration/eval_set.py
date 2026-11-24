"""Generate the calibration evaluation set.

Picks a stratified sample of provisions (by publisher and legal_hierarchy), asks OpenAI
`gpt-4o-mini` to write a natural-language question each one answers (about $0.001 per
provision), and stores it in `retrieval_eval` with `expected_provision_id` set and `label`
null. Lawyers then label the rows in the Streamlit app, and the rerank refusal threshold is
fitted on the labelled data. Re-runs skip provisions that already have a `retrieval_eval` row,
so no question is paid for twice.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy import text as sql_text

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.observability.events import emit
from regsuper.storage.audit import hash_prompt
from regsuper.storage.database import session_scope
from regsuper.storage.models import RetrievalEval

QGEN_MODEL = "gpt-4o-mini"
QGEN_MAX_TOKENS = 250
QGEN_TEMPERATURE = 0.4

QUESTION_PROMPT = """You are building an evaluation set for a legal retrieval system.
Read the regulatory provision below and write ONE natural question that the
provision answers. The question should:

  - Be phrased the way a compliance officer or lawyer would actually ask it
  - Sound like a real query (not "what does this provision say?")
  - Mention concrete entities (AIF, AIFM, ČNB, ESMA, GDPR controller, …)
    or concrete obligations (notification deadline, capital requirement, …)
  - Be answerable from the provision text alone

PROVISION
source: {publisher} {external_id}, {legal_hierarchy}
label: {label}
text:
<<<
{provision_text}
>>>

OUTPUT (JSON object only)
{{
  "question": "<the natural question, 1 sentence>",
  "rationale": "<1 sentence explaining why this question maps to this provision>"
}}
"""
PROMPT_HASH = hash_prompt(QUESTION_PROMPT)


class _GenResult(BaseModel):
    question: str = Field(min_length=8)
    rationale: str = ""


@dataclass
class EvalProvision:
    provision_id: UUID
    label: str | None
    text: str
    publisher: str
    external_id: str | None
    legal_hierarchy: str


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------
async def sample_provisions(
    *,
    target_total: int = 100,
    min_text_chars: int = 200,
    seed: int = 17,
) -> list[EvalProvision]:
    """Return a stratified sample across (publisher, legal_hierarchy).

    Stratifying by sector_tags would be preferable, but sector_tags is empty in the current
    corpus (the tagging pass has not run), so legal_hierarchy serves as the proxy. The order is
    deterministic for a given `seed`, so re-runs are reproducible.
    """
    async with session_scope() as session:
        strata = (
            await session.execute(
                sql_text(
                    """
            SELECT sd.publisher, p.legal_hierarchy, count(*) AS n
              FROM provision p
              JOIN source_document sd ON sd.id = p.source_document_id
             WHERE length(p.text) >= :min_chars
             GROUP BY sd.publisher, p.legal_hierarchy
             ORDER BY n DESC
            """
                ),
                {"min_chars": min_text_chars},
            )
        ).all()

    if not strata:
        return []

    total = sum(n for _, _, n in strata)
    # Per-stratum quota proportional to size, with a floor of 4 so even small strata land in
    # the eval set.
    quotas: list[tuple[str, str, int]] = []
    for pub, lh, n in strata:
        q = max(4, round(target_total * n / total))
        quotas.append((pub, lh, q))
    # Trim so the total does not overshoot by much when many small strata are padded up.
    while sum(q for _, _, q in quotas) > int(target_total * 1.5):
        idx = max(range(len(quotas)), key=lambda i: quotas[i][2])
        pub, lh, q = quotas[idx]
        quotas[idx] = (pub, lh, q - 1)

    picks: list[EvalProvision] = []
    async with session_scope() as session:
        for pub, lh, q in quotas:
            # The seed is inlined as an integer literal because asyncpg's parser chokes on
            # `::text` right after a bind parameter. It passes through int(), so this is safe
            # from injection.
            sql = f"""
                SELECT p.id, p.label, p.text, sd.publisher, sd.external_id, p.legal_hierarchy
                  FROM provision p
                  JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.publisher = :pub
                   AND p.legal_hierarchy = :lh
                   AND length(p.text) >= :min_chars
                 ORDER BY md5(p.id::text || '{int(seed)}')
                 LIMIT :q
            """
            rows = (
                await session.execute(
                    sql_text(sql),
                    {
                        "pub": pub,
                        "lh": lh,
                        "min_chars": min_text_chars,
                        "q": q,
                    },
                )
            ).all()
            for r in rows:
                picks.append(
                    EvalProvision(
                        provision_id=r[0],
                        label=r[1],
                        text=r[2],
                        publisher=r[3],
                        external_id=r[4],
                        legal_hierarchy=r[5],
                    )
                )
    return picks


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------
async def _generate_question(
    client: AsyncOpenAI,
    prov: EvalProvision,
) -> tuple[str | None, str, int]:
    """Ask the LLM for one question that `prov` answers.

    Returns (question or None, error message, latency_ms).
    """
    prompt = QUESTION_PROMPT.format(
        publisher=prov.publisher,
        external_id=prov.external_id or "",
        legal_hierarchy=prov.legal_hierarchy,
        label=prov.label or "(unlabelled)",
        provision_text=prov.text.strip()[:6000],
    )
    t0 = time.perf_counter()
    try:
        resp = await tracked_chat_completion(
            client,
            source="calibration.eval_question",
            model=QGEN_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=QGEN_MAX_TOKENS,
            temperature=QGEN_TEMPERATURE,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        return None, repr(exc)[:200], int((time.perf_counter() - t0) * 1000)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    body = (resp.choices[0].message.content or "").strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, f"json: {exc}", latency_ms
    try:
        parsed = _GenResult.model_validate(data)
    except ValidationError as exc:
        return None, f"schema: {exc.errors()[:1]}", latency_ms
    return parsed.question.strip(), "", latency_ms


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _existing_expected_ids() -> set[UUID]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RetrievalEval.expected_provision_id).where(
                    RetrievalEval.expected_provision_id.is_not(None)
                )
            )
        ).all()
    return {r[0] for r in rows if r[0]}


async def _persist_eval(
    *,
    provision_id: UUID,
    question: str,
) -> UUID:
    """Persist the seed row for a generated question and return its id.

    The row carries `expected_provision_id` (the provision the question was generated from) and
    becomes the anchor of a `query_group_id`: every labelling row for this query shares that
    group id, so results can be aggregated per query (precision@k, recall@k).
    """
    async with session_scope() as session:
        row = RetrievalEval(
            query=question,
            expected_provision_id=provision_id,
            retrieved_provision_id=provision_id,
            retrieval_score=0.0,
            rerank_score=None,
            label=None,
            labeled_by=None,
            labeled_at=None,
            notes=f"seed · generated by {QGEN_MODEL}",
            rank_position=None,
            added_by_lawyer=False,
            label_kind=None,
        )
        session.add(row)
        await session.flush()
        # The anchor row uses its own id as the group id; every later row for this query
        # references it through query_group_id.
        row.query_group_id = row.id
        return row.id


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
@dataclass
class EvalGenReport:
    n_sampled: int
    n_already_had_question: int
    n_generated: int
    n_errors: int
    sample_errors: list[str]
    eval_ids: list[UUID]
    total_latency_ms: int


async def generate_eval_set(
    *,
    target_total: int = 100,
    seed: int = 17,
    max_concurrency: int = 6,
    dry_run: bool = False,
) -> EvalGenReport:
    """Sample provisions, generate a question for each new one, and persist to retrieval_eval."""
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    samples = await sample_provisions(target_total=target_total, seed=seed)
    existing = await _existing_expected_ids()
    todo = [s for s in samples if s.provision_id not in existing]

    await emit(
        "calibration.eval.start",
        target=target_total,
        sampled=len(samples),
        new=len(todo),
        already=len(samples) - len(todo),
    )
    if dry_run:
        return EvalGenReport(
            n_sampled=len(samples),
            n_already_had_question=len(samples) - len(todo),
            n_generated=0,
            n_errors=0,
            sample_errors=[],
            eval_ids=[],
            total_latency_ms=0,
        )

    client = get_openai()
    sem = asyncio.Semaphore(max_concurrency)
    eval_ids: list[UUID] = []
    sample_errors: list[str] = []
    t0 = time.perf_counter()
    done = 0
    errors = 0

    async def _one(prov: EvalProvision) -> None:
        nonlocal done, errors
        async with sem:
            q, err, _lat = await _generate_question(client, prov)
        if err or not q:
            errors += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"{prov.provision_id}: {err or 'empty question'}")
            done += 1
            return
        try:
            eid = await _persist_eval(provision_id=prov.provision_id, question=q)
            eval_ids.append(eid)
        except Exception as exc:
            errors += 1
            if len(sample_errors) < 5:
                sample_errors.append(f"{prov.provision_id}: persist: {exc!r}")
        done += 1
        if done % 10 == 0 or done == len(todo):
            await emit(
                "calibration.eval.progress",
                done=done,
                total=len(todo),
                generated=len(eval_ids),
                errors=errors,
            )

    await asyncio.gather(*(_one(p) for p in todo))
    elapsed = int((time.perf_counter() - t0) * 1000)
    await emit(
        "calibration.eval.complete",
        sampled=len(samples),
        generated=len(eval_ids),
        errors=errors,
        latency_ms=elapsed,
    )
    return EvalGenReport(
        n_sampled=len(samples),
        n_already_had_question=len(samples) - len(todo),
        n_generated=len(eval_ids),
        n_errors=errors,
        sample_errors=sample_errors[:5],
        eval_ids=eval_ids,
        total_latency_ms=elapsed,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Day-13: generate calibration eval set.")
    p.add_argument("--target", type=int, default=100, help="Target number of provisions to sample.")
    p.add_argument("--seed", type=int, default=17, help="Sampling seed for reproducibility.")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="Sample only, don't call LLM.")
    args = p.parse_args()

    rep = asyncio.run(
        generate_eval_set(
            target_total=args.target,
            seed=args.seed,
            max_concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    )
    print(
        json.dumps(
            {
                "sampled": rep.n_sampled,
                "already_had_question": rep.n_already_had_question,
                "generated": rep.n_generated,
                "errors": rep.n_errors,
                "sample_errors": rep.sample_errors,
                "latency_ms": rep.total_latency_ms,
            },
            indent=2,
        )
    )


# Keeps the otherwise unused `Any` import referenced.
_ = Any

if __name__ == "__main__":
    _main()
