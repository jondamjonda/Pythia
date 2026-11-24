#!/usr/bin/env python3
"""Generate the lawyer-review sample for the corpus cutover without enabling it for live traffic.

Before the app serves law from the engine corpus (law_source='nexus'), a lawyer reviews a batch of
the findings the cutover would produce (at least 50, at least 80% confirmed, at most 5% incorrect).
This script runs the L2, L3 and L4 cascade under law_source='nexus' inside `cutover_eval_mode()`,
writes the findings only to an isolated eval database (see `assert_isolated_sink`), reads current
law from the engine over HTTP, and exports the nexus findings as JSON. The default is a dry run
that writes nothing; `--apply` runs the cascade (bounded LLM calls; needs the engine and
OPENAI_API_KEY).

Usage (from app/):
    python scripts/cutover_eval_harness.py --project-id <uuid> --eval-database-url <dsn>
        [--apply] [--out sample.json] [--scope crosswalk|full] [--top-k N] [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from uuid import UUID

from sqlalchemy.engine import make_url

# Fallback for an unconfigured environment; the real live DSNs are read from the app config at run
# time.
_LIVE_REGSUPER_DEFAULT = "postgresql://regsuper:regsuper@localhost:5432/regsuper"
# Host aliases for the local machine, collapsed so a different spelling of 'localhost' cannot slip
# past the guard.
_LOCAL_HOST_ALIASES = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}
# An eval database name must contain one of these, unless --i-understand-isolated is passed.
_EVAL_NAME_TOKENS = ("eval", "test", "sandbox", "scratch")


def _db_identity(dsn: str) -> tuple[str, int, str | None]:
    """Return the physical database a DSN points at, as (host, port, database).

    Local host aliases collapse to 'localhost', the port defaults to 5432 and the database name
    stays case-sensitive (Postgres semantics); driver, scheme, query and credentials are ignored.
    Two DSNs with the same identity hit the same database even if spelled differently.
    """
    u = make_url(dsn)
    host = (u.host or "").lower()
    if host in _LOCAL_HOST_ALIASES:
        host = "localhost"
    # make_url keeps trailing slashes in the database name ('sample_eval/'); strip them so they
    # cannot be used to slip past the guard.
    database = (u.database or "").strip().rstrip("/") or None
    return (host, u.port or 5432, database)


def assert_isolated_sink(
    eval_dsn: str | None, forbidden_dsns: list[str | None], *, allow_any_name: bool = False
) -> None:
    """Refuse, with SystemExit, unless the eval sink is an isolated database and never a live one.

    The eval DSN is compared with each forbidden DSN by physical identity (host, port, database),
    not as a raw string, and its database name must contain one of _EVAL_NAME_TOKENS unless
    ``allow_any_name`` is set.
    """
    if not eval_dsn:
        raise SystemExit(
            "REFUSE: --eval-database-url is required — point it at an ISOLATED eval database, "
            "never a live corpus. (No findings were written.)"
        )
    try:
        eid = _db_identity(eval_dsn)
    except Exception as exc:
        raise SystemExit(f"REFUSE: --eval-database-url is not a valid DSN ({exc}).") from exc

    name = eid[2] or ""
    if not allow_any_name and not any(tok in name.lower() for tok in _EVAL_NAME_TOKENS):
        raise SystemExit(
            f"REFUSE: eval DB name {name!r} does not look isolated (expected one of {_EVAL_NAME_TOKENS}). "
            "Use a dedicated throwaway DB, or pass --i-understand-isolated if you are certain it is one. "
            "(No findings were written.)"
        )

    for forbidden in forbidden_dsns:
        if not forbidden:
            continue
        try:
            fid = _db_identity(forbidden)
        except Exception:
            continue
        if fid == eid:
            raise SystemExit(
                f"REFUSE: --eval-database-url resolves to a LIVE database (host={eid[0]} port={eid[1]} "
                f"db={eid[2]}) — the off-flip eval must write to an ISOLATED database, never the live corpus. "
                "(No findings were written.)"
            )


def _live_dsns_from_config() -> list[str | None]:
    """Return the live DSNs to forbid, read the way the app reads them (secrets.env and .env).

    A bare os.getenv would usually miss them. Covers the regsuper database (async and sync URLs) and
    the live pythia corpus.
    """
    from regsuper.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    return [
        s.database_url,
        getattr(s, "database_url_sync", None),
        s.pythia_database_url,
        _LIVE_REGSUPER_DEFAULT,
    ]


async def _run_eval(
    project_id: UUID,
    *,
    eval_dsn: str,
    limit: int | None,
    scope: str = "crosswalk",
    top_k: int = 200,
) -> list[dict]:
    """Run the cascade in cutover_eval_mode against the eval database and return its nexus findings.

    Current law is read from the live engine. Imports are local so that DATABASE_URL and LAW_SOURCE
    are set before the database engine is built.

    scope='crosswalk' (default) restricts L1 to provisions the app corpus already holds (those with
    a regsuper-to-nexus crosswalk row): current-law analysis of the app's own acts, so every nexus
    finding's cited document resolves in the app and nothing is dropped for missing coverage. This
    is the mode that yields a reviewable sample. scope='full' runs the broad nexus L1 over the whole
    engine corpus; it is kept for diagnostics and currently persists almost no findings because of
    the cited-document coverage limit (see crosswalk_scoped_nexus_candidates).
    """
    from sqlalchemy import select

    from regsuper.analysis import cutover_eval_active, cutover_eval_mode
    from regsuper.analysis.l1_candidates import crosswalk_scoped_nexus_candidates
    from regsuper.analysis.l2_applicability import run_l2
    from regsuper.analysis.l3_obligations import run_l3
    from regsuper.analysis.l4_gaps import run_l4_gaps
    from regsuper.config import get_settings
    from regsuper.storage.database import dispose_engine, session_scope
    from regsuper.storage.models import Finding

    # Drop any engine built before the environment was pinned, so session_scope rebuilds against the
    # eval database.
    await dispose_engine()
    bound = get_settings().database_url
    if _db_identity(bound) != _db_identity(eval_dsn):
        raise SystemExit(
            f"REFUSE: the runtime DB ({_db_identity(bound)}) is not the eval sink "
            f"({_db_identity(eval_dsn)}) — aborting before any write."
        )

    with cutover_eval_mode():
        assert cutover_eval_active(), "eval-mode must be active inside the harness run"
        if scope == "crosswalk":
            # Scope L1 to crosswalked (shared-act) provisions so cited documents always resolve. L3
            # and L4 follow from L2's persisted applicable findings, so scoping L2's candidates is
            # sufficient.
            candidates = await crosswalk_scoped_nexus_candidates(project_id, top_k=top_k)
            await run_l2(project_id, candidates=candidates, limit=limit)
        else:
            await run_l2(project_id, limit=limit)
        await run_l3(project_id, limit=limit)
        await run_l4_gaps(project_id)

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Finding).where(
                        Finding.project_id == project_id,
                        Finding.payload["law_source"].astext
                        == "nexus",  # only this eval run's nexus findings
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": str(f.id),
            "analysis_layer": f.analysis_layer,
            "finding_type": f.finding_type,
            "statement": f.statement,
            "severity": getattr(f, "severity", None),
            "review_state": getattr(f, "review_state", None),
            "cited_provisions": [str(p) for p in (f.cited_provisions or [])],
            "law_source": (f.payload or {}).get("law_source"),
            "payload": f.payload,
        }
        for f in rows
    ]


def main(argv: list[str] | None = None) -> int:
    """Guard the eval sink, then print the dry-run plan or run the cascade and export findings."""
    ap = argparse.ArgumentParser(
        description="Generate the B3-06a lawyer-review sample off-flip (isolated sink)."
    )
    ap.add_argument(
        "--project-id", required=True, type=UUID, help="the project to analyse off-flip"
    )
    ap.add_argument(
        "--eval-database-url",
        required=True,
        help="ISOLATED eval DB DSN to write findings into (NEVER a live corpus)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually run the cascade (real bounded LLM calls + engine reads); default is dry-run",
    )
    ap.add_argument(
        "--i-understand-isolated",
        action="store_true",
        help="bypass the eval-DB-name allow-list (only if you are certain the DB is a throwaway)",
    )
    ap.add_argument(
        "--scope",
        choices=["crosswalk", "full"],
        default="crosswalk",
        help="crosswalk (default): scope L1 to the app's own acts via the crosswalk so cited "
        "documents resolve and the sample is non-empty (current-law-of-the-app's-acts). "
        "full: the broad nexus L1 over the whole engine corpus (hits the cited-document "
        "coverage ceiling → ~0 persistable findings today; diagnostics/parity only).",
    )
    ap.add_argument(
        "--top-k",
        type=int,
        default=200,
        help="L1 candidate pool size (crosswalk scope) — raise it if the sample falls short of "
        "the B3-06a ≥50-finding target",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="max provisions per layer (bound the LLM spend)"
    )
    ap.add_argument("--out", default=None, help="write the findings JSON here (default: stdout)")
    args = ap.parse_args(argv)

    forbidden = (
        _live_dsns_from_config()
    )  # reads secrets.env/.env: the real live DSNs (regsuper sync and async, plus pythia)
    assert_isolated_sink(
        args.eval_database_url, forbidden, allow_any_name=args.i_understand_isolated
    )

    from regsuper.config import get_settings

    s = get_settings()
    engine_ready = bool(s.pythia_enabled and s.pythia_url)

    scope_note = (
        "crosswalk — L1 scoped to the app's own acts (current-law sample resolves end-to-end)"
        if args.scope == "crosswalk"
        else "full — broad nexus L1 (hits the cited-document coverage ceiling; ~0 persisted today)"
    )
    if not args.apply:
        print(
            "DRY-RUN — isolated sink OK; nothing written.\n"
            f"  would analyse project : {args.project_id}\n"
            f"  off-flip cascade      : L2→L3→L4 under law_source='nexus' inside cutover_eval_mode()\n"
            f"  candidate scope       : {scope_note}\n"
            f"  L1 top_k (crosswalk)  : {args.top_k}\n"
            f"  findings written to   : {args.eval_database_url} (ISOLATED — live corpora untouched)\n"
            f"  provision limit/layer : {args.limit if args.limit is not None else 'none'}\n"
            f"  engine (pythia) ready : {engine_ready}  (pythia_enabled={s.pythia_enabled}, "
            f"pythia_url={'set' if s.pythia_url else 'UNSET'})\n"
            "Re-run with --apply to generate the sample (needs the engine reachable + OPENAI_API_KEY). "
            "The live gate stays shut; this is the sanctioned off-flip exception only."
        )
        return 0

    if not engine_ready:
        raise SystemExit(
            "REFUSE --apply: pythia_enabled + pythia_url must be set — the off-flip eval reads "
            "current law from the live engine. (No findings were written.)"
        )

    # Pin the isolated sink, law_source='nexus' and the eval process flag before the cascade imports
    # build the database engine. cutover_eval_mode() is double-locked: it needs both its ContextVar
    # and this process-level flag, so a concurrent live request never sees the exception.
    os.environ["DATABASE_URL"] = args.eval_database_url
    os.environ["LAW_SOURCE"] = "nexus"
    os.environ["REGSUPER_CUTOVER_EVAL"] = "1"  # process flag required by cutover_eval_mode()
    get_settings.cache_clear()

    findings = asyncio.run(
        _run_eval(
            args.project_id,
            eval_dsn=args.eval_database_url,
            limit=args.limit,
            scope=args.scope,
            top_k=args.top_k,
        )
    )
    payload = {
        "project_id": str(args.project_id),
        "n_findings": len(findings),
        "scope": args.scope,
        "top_k": args.top_k,
        "note": "OFF-FLIP eval sample for B3-06a lawyer review — current-law (nexus) findings from an "
        "ISOLATED DB. The live product is unaffected. scope='crosswalk' means these are the "
        "app's own acts read at CURRENT-law text from the engine (current-vs-frozen).",
        "findings": findings,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(
            f"APPLIED — {len(findings)} nexus findings written to {args.out} "
            f"(isolated sink: {args.eval_database_url})."
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
