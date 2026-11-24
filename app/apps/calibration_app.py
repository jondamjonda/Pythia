"""Streamlit labeling UI for calibrating the retrieval refusal threshold.

Pooled-relevance labeling: for each query, a lawyer judges the top-N retrieved provisions and can
add provisions the system missed. The page also fits and charts the threshold from the labels so
far. UI strings are Czech for the reviewing lawyer; comments stay in English.

Usage (from app/): streamlit run apps/calibration_app.py
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any
from uuid import UUID

import streamlit as st
from sqlalchemy import text as sql_text

from regsuper.calibration.pooled import (
    append_lawyer_provision,
    label_row,
    list_query_groups,
    materialise_top_n,
    reject_group,
    search_provisions,
)
from regsuper.calibration.threshold import fit_threshold, load_labeled_evals
from regsuper.storage.database import session_scope

st.set_page_config(page_title="RegSuper · Kalibrace", layout="wide")
DEFAULT_LABELER = os.environ.get("REGSUPER_LABELER", "lawyer-anon")
TOP_N = 10


# ---------------------------------------------------------------------------
# Persistent background event loop. Streamlit reruns the script on every interaction;
# running every coroutine on one long-lived loop keeps the SQLAlchemy async pool from
# being bound to a loop that an earlier rerun has closed.
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _bg_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name="streamlit-async", daemon=True)
    t.start()
    from regsuper.storage.database import dispose_engine

    asyncio.run_coroutine_threadsafe(dispose_engine(), loop).result()
    return loop


def _run(coro: Any) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop()).result()


# ---------------------------------------------------------------------------
# Provision lookup helper
# ---------------------------------------------------------------------------
async def _lookup(pid: UUID) -> tuple[str | None, str | None, str | None, str]:
    """Return (external_id, label, source_title, full_text) for a provision id."""
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text(
                    "SELECT sd.external_id, p.label, sd.title, p.text "
                    "FROM provision p JOIN source_document sd ON sd.id = p.source_document_id "
                    "WHERE p.id = :pid"
                ),
                {"pid": pid},
            )
        ).first()
    if not row:
        return (None, None, None, "")
    return (row[0], row[1], row[2], row[3] or "")


# ---------------------------------------------------------------------------
# Header and sidebar
# ---------------------------------------------------------------------------
st.title("📝 Kalibrace vyhledávání — Den 13")
st.caption(
    "Pro každý dotaz označte u všech zobrazených předpisů jejich relevanci. "
    "Pokud systém vynechal důležitý předpis, můžete ho přidat ručně."
)

with st.sidebar:
    st.header("Nastavení")
    labeler = st.text_input("Vaše jméno (do auditu)", value=DEFAULT_LABELER)
    st.markdown("---")
    show_threshold = st.checkbox("Zobrazit přehled prahové hodnoty", value=True)
    target_precision = st.slider(
        "Cílová přesnost",
        min_value=0.80,
        max_value=0.99,
        value=0.95,
        step=0.01,
        help=(
            "Kalibrační algoritmus najde nejnižší skóre přerangování, "
            "při kterém je alespoň tato část výsledků relevantních."
        ),
    )
    st.markdown("---")
    st.caption(
        "**Význam tlačítek**  \n"
        "✅ **Relevantní** — předpis přímo odpovídá na otázku.  \n"
        "🟡 **Částečně** — souvisí, ale neodpovídá přímo.  \n"
        "❌ **Nerelevantní** — nesouvisí.  \n"
        "⏭ **Přeskočit** — vrátit se k tomu později."
    )


# ---------------------------------------------------------------------------
# Load all open query groups
# ---------------------------------------------------------------------------
groups = _run(list_query_groups(only_unfinished=True, limit=200))
labeled = _run(load_labeled_evals())

c1, c2, c3, c4 = st.columns(4)
c1.metric("Zbývá dotazů", len(groups))
c2.metric(
    "Relevantních",
    sum(1 for r in labeled if (r.label_kind in ("relevant", "partial") or r.label == "relevant")),
)
c3.metric(
    "Nerelevantních",
    sum(
        1
        for r in labeled
        if (r.label_kind == "not_relevant" or (r.label == "not_relevant" and r.label_kind is None))
    ),
)
c4.metric("Označených řádků celkem", len(labeled))

if not groups:
    st.success("✓ Všechny dotazy jsou označeny. Spusťte vyhodnocení prahové hodnoty níže.")
else:
    # ---- Current query group ----
    grp = groups[0]
    st.markdown("---")
    st.subheader(f"Dotaz {1} z {len(groups)}")
    st.markdown(f"**Otázka:** *{grp.query}*")

    if grp.expected_provision_id:
        ext, lbl, title, full = _run(_lookup(grp.expected_provision_id))
        with st.expander(
            f"Zdrojový předpis (referenční) · {ext or '?'} {lbl or ''}",
            expanded=False,
        ):
            st.caption((title or "")[:140])
            st.code(full[:3000], language="text")

    # Materialise top-N if not done yet for this group.
    if not grp.is_materialised:
        with st.spinner("Vyhledávám 10 nejlepších předpisů pro tento dotaz…"):
            n_new = _run(materialise_top_n(group_id=grp.group_id, top_n=TOP_N))
            st.toast(f"Načteno {n_new} kandidátů.")
            st.rerun()

    # ---- The top-N labeling list ----
    st.markdown("### Top 10 nalezených předpisů")
    candidate_rows = [
        r for r in grp.rows if r.id != grp.anchor_row_id and r.rank_position is not None
    ]
    candidate_rows.sort(key=lambda r: r.rank_position or 999)
    lawyer_added = [r for r in grp.rows if r.added_by_lawyer]

    for r in candidate_rows:
        ext, lbl, title, full = _run(_lookup(r.retrieved_provision_id))
        current = r.label_kind or r.label or ""
        # Header line
        hdr_cols = st.columns([0.5, 2, 1.5, 1.2, 4.8])
        hdr_cols[0].markdown(f"**#{r.rank_position}**")
        hdr_cols[1].markdown(f"`{ext or '?'}` {lbl or ''}")
        hdr_cols[2].markdown(
            f"skóre: **{r.rerank_score:.3f}**" if r.rerank_score is not None else "*skóre n/a*"
        )
        # Verdict marker
        emoji = {
            "relevant": "✅",
            "partial": "🟡",
            "not_relevant": "❌",
        }.get(current, "·")
        hdr_cols[3].markdown(f"### {emoji}")
        # Buttons
        btn_cols = hdr_cols[4].columns(4)
        if btn_cols[0].button("✅ Relevantní", key=f"rel-{r.id}", use_container_width=True):
            _run(label_row(row_id=r.id, label_kind="relevant", labeler=labeler))
            st.rerun()
        if btn_cols[1].button("🟡 Částečně", key=f"par-{r.id}", use_container_width=True):
            _run(label_row(row_id=r.id, label_kind="partial", labeler=labeler))
            st.rerun()
        if btn_cols[2].button("❌ Nerelevantní", key=f"no-{r.id}", use_container_width=True):
            _run(label_row(row_id=r.id, label_kind="not_relevant", labeler=labeler))
            st.rerun()
        # Snippet expander
        with st.expander("Zobrazit text předpisu", expanded=False):
            st.caption((title or "")[:140])
            st.code(full[:3000], language="text")

    # ---- Lawyer-added provisions ----
    if lawyer_added:
        st.markdown("### Vámi přidané předpisy")
        for r in lawyer_added:
            ext, lbl, title, full = _run(_lookup(r.retrieved_provision_id))
            current = r.label_kind or r.label or ""
            cc = st.columns([3, 1.5, 1.5, 4])
            cc[0].markdown(f"`{ext or '?'}` {lbl or ''}")
            cc[1].markdown(f"{'✅' if current in ('relevant', 'partial') else '❌'} {current}")
            cc[2].markdown("*ručně*")
            if cc[3].button("Odstranit", key=f"rm-{r.id}"):

                async def _rm(rid: UUID) -> None:
                    async with session_scope() as session:
                        from sqlalchemy import delete as sa_delete

                        from regsuper.storage.models import RetrievalEval as _RE

                        await session.execute(sa_delete(_RE).where(_RE.id == rid))

                _run(_rm(r.id))
                st.rerun()

    # ---- Add a missing provision ----
    st.markdown("### ➕ Přidat předpis, který by zde měl být")
    add_cols = st.columns([3, 1])
    search_text = add_cols[0].text_input(
        "Hledat předpis (zadejte CELEX číslo, paragraf, nebo klíčové slovo)",
        key=f"search-{grp.group_id}",
        placeholder="např. 240/2013 § 303  nebo  AIFM passporting",
    )
    do_search = add_cols[1].button("Vyhledat", key=f"go-{grp.group_id}", use_container_width=True)

    if do_search and search_text:
        hits = _run(search_provisions(search_text, limit=10))
        if not hits:
            st.warning("Nenalezen žádný předpis. Zkuste jiná klíčová slova.")
        else:
            st.markdown("**Výsledky vyhledávání** — vyberte ten, který chcete přidat:")
            for h in hits:
                hc = st.columns([3, 2, 5, 1])
                hc[0].markdown(f"`{h.external_id or '?'}` {h.label or ''}")
                hc[1].caption((h.title or "")[:60])
                hc[2].caption(h.snippet[:160])
                if hc[3].button("Přidat ✅", key=f"add-{h.provision_id}-{grp.group_id}"):
                    _run(
                        append_lawyer_provision(
                            group_id=grp.group_id,
                            provision_id=h.provision_id,
                            labeler=labeler,
                            label_kind="relevant",
                            notes=f"přidáno hledáním: '{search_text}'",
                        )
                    )
                    st.rerun()

    # ---- Next / reject ----
    st.markdown("---")
    nav_cols = st.columns([2, 2, 2, 4])
    if nav_cols[0].button("➡️ Další dotaz", type="primary", use_container_width=True):
        # Mark every still-unlabeled candidate row as not_relevant (the lawyer
        # moved on, or nothing else matched), skipping the anchor row.
        async def _mark_unlabeled_no() -> None:
            for r in grp.rows:
                if r.id == grp.anchor_row_id:
                    continue
                if not (r.label_kind or r.label):
                    await label_row(row_id=r.id, label_kind="not_relevant", labeler=labeler)

        _run(_mark_unlabeled_no())
        st.rerun()
    if nav_cols[1].button("🚫 Zamítnout dotaz (nesmysl)", use_container_width=True):
        _run(reject_group(grp.group_id, labeler=labeler))
        st.toast("Dotaz zamítnut.")
        st.rerun()


# ---------------------------------------------------------------------------
# Threshold report
# ---------------------------------------------------------------------------
if show_threshold:
    st.markdown("---")
    st.header("📈 Přehled prahové hodnoty")
    report = fit_threshold(_run(load_labeled_evals()), target_precision=target_precision)
    rcol1, rcol2, rcol3 = st.columns(3)
    rcol1.metric("Označené řádky", report.n_labeled)
    rcol2.metric(
        "Doporučená prahová hodnota",
        f"{report.chosen_threshold:.4f}" if report.chosen_threshold else "—",
    )
    rcol3.metric(
        "Přesnost při této hranici",
        f"{report.chosen_precision:.0%}" if report.chosen_precision else "—",
    )
    st.caption(report.reason)
    if report.curve:
        import pandas as pd

        df = pd.DataFrame(
            [
                {
                    "threshold": p.threshold,
                    "precision": p.precision,
                    "recall": p.recall,
                    "n_above": p.n_above,
                }
                for p in report.curve
            ]
        )
        st.line_chart(df, x="threshold", y=["precision", "recall"], height=260)
        st.caption(
            "Modrá křivka = přesnost (precision), oranžová = pokrytí (recall). "
            "S rostoucí prahovou hodnotou roste přesnost, ale klesá pokrytí. "
            "Hledáme nejmenší práh, kde přesnost překročí cílovou hodnotu."
        )
