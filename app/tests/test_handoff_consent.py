"""B2b-05 / WP-050 — lawful-lane hand-off API + consent.

The SAFETY INVARIANT under test (GDPR, failure mode-7 "PII-in-handoff"):
``handoff.contact_payload`` may be written ONLY ON OR AFTER ``consent_at`` is set.
A payload write while ``consent_at`` is NULL must be REJECTED in app code, before
any PII reaches the DB.

These tests drive the real ``create_handoff_offer`` / ``record_handoff_consent`` /
``_write_contact_payload`` logic against an in-memory fake AsyncSession (mirroring
the repo's mock-the-seam test style — no live Postgres needed). ``log_event`` and
``emit`` are stubbed so the unit under test is the offer/consent ordering and the
GDPR guard, nothing else.

Proven here:
  1. an offer is created with ``contact_payload`` EMPTY (no PII) and
     ``consent_at`` NULL — the row carries only non-PII trigger metadata;
  2. recording consent sets ``consent_at`` THEN writes ``contact_payload`` (PII
     allowed once consent exists);
  3. a direct payload write while ``consent_at`` is NULL is REJECTED with
     ``HandoffConsentError`` — the GDPR invariant — and the payload stays empty;
  4. an invalid trigger is rejected; a valid trigger keeps the row metadata-only.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import regsuper.api.handoff as handoff_mod
from regsuper.api.handoff import (
    HandoffConsentError,
    _write_contact_payload,
    create_handoff_offer,
    record_handoff_consent,
)
from regsuper.storage.models import Handoff


# ---------------------------------------------------------------------------
# In-memory fakes — no engine, no Postgres. The Handoff ORM object is a plain
# Python instance; we never flush it to a DB, we assert on its attributes.
# ---------------------------------------------------------------------------
class _FakeSession:
    """Minimal async-session stand-in: tracks added rows, assigns ids on flush,
    and resolves ``get`` from the in-memory store."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self._store: dict[object, object] = {}

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Handoff):
                if obj.id is None:
                    obj.id = uuid4()
                self._store[obj.id] = obj

    async def get(self, model: type, pk: object) -> object | None:
        return self._store.get(pk)


@pytest.fixture(autouse=True)
def _silence_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the audit + event seams so we exercise ONLY the offer/consent logic."""

    async def _noop_log_event(*_a, **_k) -> None:
        return None

    async def _noop_emit(*_a, **_k):
        return {}

    monkeypatch.setattr(handoff_mod, "log_event", _noop_log_event)
    monkeypatch.setattr(handoff_mod, "emit", _noop_emit)


def _new_handoff() -> Handoff:
    """A Handoff with id pre-seeded so the fake-session ``get`` resolves it."""
    return Handoff(
        id=uuid4(),
        project_id=None,
        org_id=None,
        trigger="reserved_advice",
        answer_mode="reserved_advice",
        reason="reserved legal advice",
        cited_provisions=[],
        status="offered",
        contact_payload={},
        consent_at=None,
    )


# ---------------------------------------------------------------------------
# 1. Offer creation — contact_payload NULL/empty, consent_at NULL.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_offer_created_with_no_pii() -> None:
    session = _FakeSession()
    row = await create_handoff_offer(
        session,
        trigger="reserved_advice",
        reason="reserved legal advice",
        answer_mode="reserved_advice",
        source_query="can I dissolve my s.r.o. without a lawyer?",
    )

    assert row.status == "offered"
    assert row.consent_at is None
    assert row.contact_payload == {}  # NO PII at creation
    # only non-PII trigger metadata is carried
    assert row.trigger == "reserved_advice"
    assert row.source_query  # the (non-PII) question for the lawyer's context
    assert row.id is not None  # flushed so an id exists for the response/audit


@pytest.mark.asyncio
async def test_invalid_trigger_rejected() -> None:
    session = _FakeSession()
    with pytest.raises(ValueError):
        await create_handoff_offer(
            session,
            trigger="not_a_trigger",
            reason="x",
            answer_mode="needs_review",
        )


# ---------------------------------------------------------------------------
# 2. Consent then payload — the happy path. consent_at set FIRST, PII after.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_consent_then_payload_allowed() -> None:
    session = _FakeSession()
    row = await create_handoff_offer(
        session,
        trigger="reserved_advice",
        reason="reserved legal advice",
        answer_mode="reserved_advice",
    )
    assert row.consent_at is None and row.contact_payload == {}

    updated = await record_handoff_consent(
        session,
        handoff_id=row.id,
        contact_payload={"name": "Jana N.", "email": "jana@example.cz"},
    )

    assert updated.consent_at is not None  # consent recorded
    assert updated.contact_payload["email"] == "jana@example.cz"  # PII now allowed
    assert updated.status == "accepted"


# ---------------------------------------------------------------------------
# 3. THE SAFETY INVARIANT — a payload write with NULL consent_at is REJECTED.
# ---------------------------------------------------------------------------
def test_payload_write_rejected_without_consent() -> None:
    """GDPR invariant: contact_payload may NOT be written while consent_at is NULL."""
    row = _new_handoff()
    assert row.consent_at is None

    with pytest.raises(HandoffConsentError):
        _write_contact_payload(row, {"name": "Petr K.", "email": "petr@example.cz"})

    # the PII never landed on the row
    assert row.contact_payload == {}


def test_empty_payload_write_is_allowed_without_consent() -> None:
    """Writing an EMPTY payload pre-consent is a harmless no-op (no PII), allowed."""
    row = _new_handoff()
    _write_contact_payload(row, {})  # must not raise
    assert row.contact_payload == {}


@pytest.mark.asyncio
async def test_consent_records_timestamp_before_payload() -> None:
    """record_handoff_consent must set consent_at BEFORE the payload guard runs, so
    the same call that supplies PII is the one that establishes consent."""
    session = _FakeSession()
    row = _new_handoff()
    session._store[row.id] = row  # pre-seed so get() resolves it

    updated = await record_handoff_consent(
        session,
        handoff_id=row.id,
        contact_payload={"name": "Eva S.", "email": "eva@example.cz", "phone": "+420 123"},
    )
    assert updated.consent_at is not None
    assert updated.contact_payload["phone"] == "+420 123"


@pytest.mark.asyncio
async def test_consent_on_missing_handoff_raises_lookup() -> None:
    session = _FakeSession()
    with pytest.raises(LookupError):
        await record_handoff_consent(
            session,
            handoff_id=uuid4(),
            contact_payload={"name": "X", "email": "x@example.cz"},
        )
