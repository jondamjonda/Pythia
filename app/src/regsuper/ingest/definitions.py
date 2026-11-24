"""Extract defined terms from provisions into the `defined_term` table.

Candidate provisions are picked with Czech and English keyword heuristics; an OpenAI chat
model then extracts (term, definition_text) pairs as JSON validated against a strict
json_schema. A full run over the corpus is expected to yield 200 to 600 terms. The default
model, gpt-4o-mini, costs about $0.10 for the 323 candidates; gpt-4o costs about $1.50.

Usage:
    python -m regsuper.ingest.definitions                     # all docs
    python -m regsuper.ingest.definitions --doc 240/2013      # one document
    python -m regsuper.ingest.definitions --dry-run           # only report counts
    python -m regsuper.ingest.definitions --limit 10          # smoke test
    python -m regsuper.ingest.definitions --model gpt-4o      # upgrade from mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from openai import AsyncOpenAI
from sqlalchemy import delete, select

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability import killswitch
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.observability.events import emit
from regsuper.observability.logging_config import configure_logging
from regsuper.storage.database import session_scope
from regsuper.storage.models import DefinedTerm, Provision, SourceDocument

DEFAULT_MODEL = "gpt-4o-mini"


# Czech and English markers used to pre-filter candidate provisions. They favour recall:
# if any of them match, the provision is sent to the model, which decides whether
# definitions are actually present.
_DEF_TEXT_RE = re.compile(
    r"("
    r"se\s+rozumí|rozumí\s+se|pro\s+účely\s+tohoto|"
    r"shall\s+mean|is\s+defined\s+as|for\s+the\s+purposes\s+of\s+this|"
    r"\bmeans\b"
    r")",
    re.IGNORECASE,
)
_DEF_LABEL_RE = re.compile(
    r"\b(definitions?|definice|pojmy|výklad\s+pojmů|terminology)\b",
    re.IGNORECASE,
)


def is_definition_candidate(provision: Provision) -> bool:
    if provision.label and _DEF_LABEL_RE.search(provision.label):
        return True
    if provision.text and _DEF_TEXT_RE.search(provision.text):
        return True
    return False


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {
                        "type": "string",
                        "description": (
                            "The exact term being defined, verbatim from "
                            "the source text (e.g. 'investiční nástroj', "
                            "'high-risk third country')."
                        ),
                    },
                    "definition_text": {
                        "type": "string",
                        "description": (
                            "The verbatim definition sentence(s) copied from the provision."
                        ),
                    },
                },
                "required": ["term", "definition_text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["definitions"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You extract legal term definitions from regulatory provisions. "
    "A definition is a statement that EXPLICITLY defines what a term means "
    "for the purposes of the regulation (e.g. 'X means ...', "
    "'Y se rozumí ...', 'pro účely tohoto zákona se Z rozumí ...'). "
    "Extract every defined term verbatim, paired with its verbatim "
    "definition. Skip cross-references, recitals, and statements that "
    "merely USE a term without defining it. If the provision is not a "
    "definition provision at all, return an empty array."
)


async def _extract_one(
    client: AsyncOpenAI,
    sem: asyncio.Semaphore,
    *,
    idx: int,
    provision: Provision,
    model: str,
) -> tuple[int, list[tuple[str, str]]]:
    async with sem:
        # Honour the kill switch between work units so an operator can halt a long batch
        # without killing the process.
        if killswitch.check_kill_switch(raise_exc=False):
            return idx, []
        try:
            resp = await tracked_chat_completion(
                client,
                source="definitions",
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Provision label: {provision.label or '(none)'}\n"
                            f"Provision text:\n{provision.text}"
                        ),
                    },
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "definitions_response",
                        "strict": True,
                        "schema": _RESPONSE_SCHEMA,
                    },
                },
                temperature=0,
                # Some definition articles (MiFID II Article 4 has about 50 terms) produce
                # over 10k characters of JSON; 16k is gpt-4o-mini's output cap.
                max_tokens=16000,
            )
        except Exception as exc:
            await emit(
                "definitions.error",
                provision_id=str(provision.id),
                error=repr(exc),
            )
            return idx, []

    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        return idx, []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        await emit(
            "definitions.parse_error",
            provision_id=str(provision.id),
            error=repr(exc),
            raw=raw[:500],
        )
        return idx, []

    defs = data.get("definitions") or []
    out: list[tuple[str, str]] = []
    for d in defs:
        term = (d.get("term") or "").strip()
        txt = (d.get("definition_text") or "").strip()
        if term and txt:
            out.append((term, txt))
    return idx, out


async def extract_all(
    *,
    doc_external_id: str | None = None,
    concurrency: int = 5,
    dry_run: bool = False,
    limit: int | None = None,
    model: str = DEFAULT_MODEL,
) -> dict:
    settings = get_settings()

    if not dry_run:
        key = settings.openai_api_key or ""
        if not key or not key.startswith("sk-") or len(key) < 30:
            raise RuntimeError(
                "OPENAI_API_KEY is missing or looks like a placeholder. "
                "Replace the value in secrets.env with a real key from "
                "platform.openai.com (real keys start 'sk-' and are 40+ chars)."
            )

    stats = {
        "candidates": 0,
        "documents": 0,
        "definitions": 0,
        "provisions_with_defs": 0,
        "model": model,
        "dry_run": dry_run,
    }

    async with session_scope() as session:
        stmt = select(Provision).order_by(Provision.source_document_id, Provision.ordinal)
        if doc_external_id:
            doc = (
                await session.execute(
                    select(SourceDocument).where(SourceDocument.external_id == doc_external_id)
                )
            ).scalar_one_or_none()
            if doc is None:
                raise RuntimeError(f"no source_document with external_id={doc_external_id}")
            stmt = stmt.where(Provision.source_document_id == doc.id)

        provisions = (await session.execute(stmt)).scalars().all()
        candidates = [p for p in provisions if is_definition_candidate(p)]
        if limit is not None:
            candidates = candidates[:limit]
        stats["candidates"] = len(candidates)
        stats["documents"] = len({p.source_document_id for p in candidates})

        await emit(
            "definitions.start",
            candidates=stats["candidates"],
            documents=stats["documents"],
            model=model,
            dry_run=dry_run,
        )

        if dry_run or not candidates:
            return stats

        # Wipe existing rows for these provisions (idempotent re-run).
        cand_ids = [p.id for p in candidates]
        await session.execute(
            delete(DefinedTerm).where(DefinedTerm.definition_provision_id.in_(cand_ids))
        )

        client = get_openai()
        sem = asyncio.Semaphore(concurrency)

        tasks = [
            _extract_one(client, sem, idx=i, provision=p, model=model)
            for i, p in enumerate(candidates)
        ]
        results: list[list[tuple[str, str]]] = [[] for _ in candidates]
        done = 0
        for coro in asyncio.as_completed(tasks):
            idx, r = await coro
            results[idx] = r
            done += 1
            if done % 25 == 0 or done == len(tasks):
                await emit(
                    "definitions.progress",
                    processed=done,
                    total=len(tasks),
                    so_far=sum(len(x) for x in results),
                )

        for provision, defs in zip(candidates, results, strict=True):
            for term, txt in defs:
                session.add(
                    DefinedTerm(
                        term=term,
                        scope_document_id=provision.source_document_id,
                        definition_provision_id=provision.id,
                        definition_text=txt,
                        extracted_by=model,
                        confidence=0.85,
                    )
                )
                stats["definitions"] += 1
            if defs:
                stats["provisions_with_defs"] += 1

    await emit("definitions.complete", **stats)
    return stats


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(description="Extract defined terms via OpenAI.")
    p.add_argument("--doc", help="Restrict to one source_document.external_id.")
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI chat model (default: {DEFAULT_MODEL}). Use 'gpt-4o' for higher accuracy.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print candidate counts without calling the API.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of provisions processed (smoke-test before full run).",
    )
    args = p.parse_args()
    stats = asyncio.run(
        extract_all(
            doc_external_id=args.doc,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
            limit=args.limit,
            model=args.model,
        )
    )
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
