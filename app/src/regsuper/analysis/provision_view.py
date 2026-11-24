"""A uniform provision view, so citation consumers work for either corpus.

Under law_source='nexus' a finding cites a nexus provision id whose data lives in the engine, not
the app ``provision`` table. Downstream consumers (audit export, evidence pack, digest and render
grounding) read a provision only through attribute access, so wrapping a nexus provision in a view
with the same attributes as an app ``Provision`` makes them corpus-aware with no per-consumer
branching. App-corpus ids keep using the ORM ``Provision`` directly. The cascade layers mark a
finding's corpus by stamping ``payload['law_source']``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


def _as_date(v: Any) -> date | None:
    """Coerce an engine validity date (an ISO string in JSON) to ``date`` or None.

    The evidence pack's temporal proof does date arithmetic on valid_from and valid_to. Accepts a
    ``date``, None or blank; an unparseable value becomes None (an open window) rather than raising
    into the proof.
    """
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class NexusProvisionView:
    """The provision attributes citation consumers read, for a nexus provision.

    Built from an engine ``/v1/provisions`` row. App provisions use the ORM ``Provision`` directly;
    this view gives a nexus provision the same attributes so the consumers (audit export, evidence
    pack, ...) stay corpus-agnostic. Unrelated to the pydantic response model
    ``api.provisions.ProvisionView``.
    """

    id: str
    text: str | None
    label: str | None
    eid: str | None
    hierarchy_path: str | None
    jurisdiction: str | None
    legal_hierarchy: str | None
    bindingness_tier: str | None
    temporal_status: str | None
    valid_from: date | None
    valid_to: date | None
    # Read by the digest and render-grounding consumers (not by export or evidence).
    # source_external_id feeds the case-law grounding guard; a nexus statute keeps source=None, so
    # _is_case_law is False and it is always grounded (never dropped). /v1/provisions does not carry
    # the deontic_* facets, so they are None and gap severity and action priority fall back to the
    # default bucket: safe but lossy. They should be carried before nexus obligations are served
    # through the small-business view.
    source_external_id: str | None
    source: str | None
    deontic_modal: str | None
    deontic_actor: str | None
    deontic_condition: str | None

    @classmethod
    def from_engine_dict(cls, provision_id: str, d: dict[str, Any]) -> NexusProvisionView:
        """Adapt one engine ``/v1/provisions`` row to the uniform view.

        ``provision_id`` is the requested id (the nexus id the finding cited). It is bound to
        ``.id`` so the consumer's ``str(cited_id) in by_id`` lookup works however the engine echoes
        it. Fields the engine corpus does not carry are None:

        * eid and hierarchy_path: app-only structural (LTREE) fields with no engine equivalent
          (``l2_applicability._load_provision_for_l2_via_nexus`` also sets hierarchy_path=None).
        * bindingness_tier: the engine's ``bindingness`` uses a different vocabulary
          (HARD_LAW_ERGA_OMNES, ...) from the app's (hard-law, oop, soft-law, interpretive).
          Rather than write a possibly wrong tier into the hashed audit payload, it is left None.
          Map engine to app bindingness before serving nexus exports that rely on the tier.

        valid_from and valid_to are parsed from ISO strings for the point-in-time proof.

        Two provenance caveats to resolve before serving nexus exports:

        * temporal_status keeps the engine vocabulary (for example 'IN_FORCE') rather than the
          app's lower-case set ('in-force', 'superseded', ...). A wrong mapping in the hashed
          audit payload would be worse than an honest engine value.
        * text, valid_from and valid_to are the engine's current versions: /v1/provisions applies
          no ``as_of``. A nexus export for a historical as-of date would therefore embed today's
          wording and validity window. That read must be pinned to the as-of date (or flagged as
          current) before a live nexus export.
        """
        return cls(
            id=str(provision_id),
            text=d.get("text"),
            label=d.get("label"),
            eid=None,
            hierarchy_path=None,
            jurisdiction=d.get("jurisdiction"),
            legal_hierarchy=d.get("legal_hierarchy"),
            bindingness_tier=None,
            temporal_status=d.get("temporal_status"),
            valid_from=_as_date(d.get("valid_from")),
            valid_to=_as_date(d.get("valid_to")),
            source_external_id=d.get("external_id"),
            source=d.get(
                "source"
            ),  # engine provenance; currently absent (no 'source' key), so not case law
            deontic_modal=None,
            deontic_actor=None,
            deontic_condition=None,
        )
