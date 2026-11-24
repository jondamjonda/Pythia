"""Deterministic slot planner for the structured answer.

Routes every retrieved provision into the action-plan slots (the `sections._HEADINGS` vocabulary)
using its analysis roles, section kind, legal hierarchy and practical facts. The structured answer
path uses the plan to decide which provision seeds which slot. The planner only adds structure: a
provision may land in several slots but is never dropped. The routing is pure (no LLM, database,
clock or randomness); an optional, separately flagged LLM pass may refine it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pythia.ports import ROLES

# Canonical role name (from `pythia.ports.ROLES`, the map assemble.py also reads) to action-plan
# slot, in the same reading order assemble.py uses.
_ROLE_SLOT: dict[str, str] = {
    "scope_rule": "applies",
    "condition_rule": "applies",
    "definition_rule": "applies",
    "obligation_rule": "obligations",
    "prohibition_rule": "obligations",
    "permission_rule": "obligations",
    "exception_rule": "obligations",
    "procedure_rule": "steps",
    "sanction_rule": "risks",
}

# section_kind to slot, only for kinds that add a slot beyond the legislative/case-law base.
_SECTION_KIND_SLOT: dict[str, str] = {"penalties": "risks"}

# fact_type to slot. Must match `slot_verify._FACT_SLOT` exactly (the shared fact-to-slot contract).
_FACT_SLOT: dict[str, str] = {
    "deadline": "obligations",
    "authority": "where",
    "penalty": "risks",
    "form": "steps",
    "channel": "steps",
    "fee": "risks",
    "procedure_step": "steps",
}

# Slots that never receive provisions: they hold the restated user profile, the synthesis verdict
# and the corpus-gap notes.
_FORBIDDEN_SLOTS: frozenset[str] = frozenset({"situation", "assessment", "gaps"})


@dataclass(frozen=True)
class SlotPlan:
    """One slot and the provision ids routed into it, de-duplicated, in retrieval-rank order."""

    slot: str
    provision_ids: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    """The full slot plan: per-slot `SlotPlan`s in order, plus read-only projections."""

    slots: tuple[SlotPlan, ...]

    def ids_for(self, slot: str) -> list[str]:
        """Return the ordered provision ids in `slot`, or [] if the plan has no such slot."""
        for sp in self.slots:
            if sp.slot == slot:
                return list(sp.provision_ids)
        return []

    def all_ids(self) -> set[str]:
        """Return the union of provision ids across all slots."""
        out: set[str] = set()
        for sp in self.slots:
            out.update(sp.provision_ids)
        return out

    def slots_for(self, pid: str) -> list[str]:
        """Return every slot `pid` landed in, in slot order."""
        return [sp.slot for sp in self.slots if pid in sp.provision_ids]

    def as_dict(self) -> dict[str, list[str]]:
        """Return the plan as `{slot: [ids]}`, the shape recorded in the audit trace."""
        return {sp.slot: list(sp.provision_ids) for sp in self.slots}


def _hierarchy_base_slot(legal_hierarchy: object) -> str:
    """Return the base slot every provision gets: 'case_law' or 'legislative'.

    A hierarchy containing 'CASE_LAW' or 'JUDGMENT' (case-insensitive substring) maps to 'case_law'.
    """
    h = str(legal_hierarchy or "").upper()
    if "CASE_LAW" in h or "JUDGMENT" in h:
        return "case_law"
    return "legislative"


def _canonical_roles(analysis_roles: object) -> list[str]:
    """Map raw `analysis_roles` int codes (as carried in meta) to canonical role names via ROLES.

    Total: a non-iterable value, None or an unknown code contributes nothing; it never raises.
    """
    out: list[str] = []
    try:
        codes = list(analysis_roles or [])
    except TypeError:
        return out
    for code in codes:
        role = ROLES.get(code)
        if role is not None:
            out.append(role)
    return out


def _deterministic_routing(
    meta: dict[str, dict],
    ordered: list[str],
    facts: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Route provisions to slots as `{slot: [ids]}`, in retrieval rank and de-duplicated per slot.

    Every pid in `ordered` lands in at least one slot (its hierarchy base) and may land in several.
    Missing or empty fields are tolerated, and nothing routes to a forbidden slot.
    """
    routed: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    def _add(slot: str, pid: str) -> None:
        if slot in _FORBIDDEN_SLOTS:
            return
        s = seen.setdefault(slot, set())
        if pid in s:
            return
        s.add(pid)
        routed.setdefault(slot, []).append(pid)

    for pid in ordered:
        m = meta.get(pid) or {}
        # 1. Base slot from the hierarchy; guarantees every pid lands in at least one slot.
        _add(_hierarchy_base_slot(m.get("legal_hierarchy")), pid)
        # 2. By canonical role.
        for role in _canonical_roles(m.get("analysis_roles")):
            slot = _ROLE_SLOT.get(role)
            if slot:
                _add(slot, pid)
        # 3. By section kind.
        slot = _SECTION_KIND_SLOT.get(m.get("section_kind"))
        if slot:
            _add(slot, pid)
        # 4. By practical facts.
        for fact_type in (facts or {}).get(pid) or []:
            slot = _FACT_SLOT.get(fact_type)
            if slot:
                _add(slot, pid)

    return routed


def _plan_from_routing(routing: dict[str, list[str]]) -> Plan:
    """Freeze a `{slot: [ids]}` routing into an immutable Plan, keeping slot insertion order."""
    return Plan(slots=tuple(SlotPlan(slot=s, provision_ids=tuple(ids)) for s, ids in routing.items()))


def _llm_refine(
    routing: dict[str, list[str]],
    ordered: list[str],
    meta: dict[str, dict],
    llm_json,
) -> dict[str, list[str]]:
    """Ask the LLM, in one cheap JSON call, to move provisions into better-fitting slots.

    The reply is a `{slot: [provision_ids]}` mapping. Only ids present in `ordered` are kept
    (dropping hallucinated ids) and forbidden slots are ignored. Any provision the LLM left out of
    every slot is re-added to its deterministic slots, so the LLM can re-slot a provision but never
    drop one. Raises on a malformed reply so the caller falls back to the deterministic plan.
    """
    order_rank = {pid: i for i, pid in enumerate(ordered)}
    allowed = set(ordered)

    prompt = _build_prompt(routing, ordered, meta)
    schema = {
        "type": "object",
        "additionalProperties": {"type": "array", "items": {"type": "string"}},
    }
    reply = llm_json(prompt, schema)
    if not isinstance(reply, dict):
        raise ValueError("planner llm reply is not a dict")

    refined: dict[str, list[str]] = {}
    refined_seen: dict[str, set[str]] = {}

    def _add(slot: str, pid: str) -> None:
        if slot in _FORBIDDEN_SLOTS or pid not in allowed:
            return
        s = refined_seen.setdefault(slot, set())
        if pid in s:
            return
        s.add(pid)
        refined.setdefault(slot, []).append(pid)

    for slot, ids in reply.items():
        if not isinstance(slot, str):
            continue
        for pid in ids or []:
            _add(slot, pid)

    # Re-add any provision the LLM dropped from every slot to its deterministic slot(s).
    placed = {pid for ids in refined.values() for pid in ids}
    for slot, ids in routing.items():
        for pid in ids:
            if pid not in placed:
                _add(slot, pid)

    # Re-sort each slot to retrieval rank so the within-slot order stays stable and auditable.
    for slot in refined:
        refined[slot] = sorted(refined[slot], key=lambda p: order_rank.get(p, len(order_rank)))
    return refined


def _build_prompt(routing: dict[str, list[str]], ordered: list[str], meta: dict[str, dict]) -> str:
    """Build a compact, deterministic prompt listing each provision with its deterministic slots."""
    hint: dict[str, list[str]] = {}
    for slot, ids in routing.items():
        for pid in ids:
            hint.setdefault(pid, []).append(slot)
    lines = [
        "Refine the slot assignment of these legal provisions into the action-plan slots.",
        "Target slots: applies, obligations, steps, where, risks, legislative, case_law.",
        "Return {slot: [provision_id,...]}. You may MOVE a provision to a better-fitting slot, but",
        "include EVERY provision id at least once and invent NO id.",
        "",
        "Provisions:",
    ]
    for pid in ordered:
        label = (meta.get(pid) or {}).get("label") or ""
        lines.append(f"- {pid} ({label}) [deterministic: {', '.join(hint.get(pid, []))}]")
    return "\n".join(lines)


def plan_slots(
    meta: dict[str, dict],
    ordered: list[str],
    *,
    facts: dict[str, list[str]] | None = None,
    settings=None,
    llm_json=None,
) -> Plan:
    """Route the retrieved provisions into the seven-slot action plan; never raises.

    Guarantees, pinned by tests:

    - `plan.all_ids()` is a subset of `ordered`: no id is invented.
    - Every pid in `ordered` lands in at least one slot.
    - Empty or missing fields never raise.
    - No provision routes to 'situation', 'assessment' or 'gaps'.

    When `structured_plan_llm_enabled` is on and an `llm_json` bridge is supplied, one LLM call may
    move provisions between slots (see `_llm_refine`); any error falls back to the deterministic
    plan. Otherwise no LLM call is made.

    Args:
        meta: `{provision_id: {'analysis_roles': [code, ...], 'section_kind': str | None,
            'legal_hierarchy': str | None, 'label': str, ...}}`; any field may be absent.
        ordered: Provision ids in retrieval rank; defines within-slot order and the allowed ids.
        facts: Optional `{provision_id: [fact_type, ...]}` for extra fact-to-slot routing.
        settings: A PythiaSettings-like object; only `structured_plan_llm_enabled` is read.
        llm_json: Optional cheap-tier bridge `(prompt, schema) -> dict | None`; None means no LLM
            call.
    """
    routing = _deterministic_routing(meta, ordered, facts)

    if getattr(settings, "structured_plan_llm_enabled", False) and llm_json is not None:
        try:
            routing = _llm_refine(routing, ordered, meta, llm_json)
        except Exception:  # noqa: BLE001 - any LLM error keeps the deterministic plan
            routing = _deterministic_routing(meta, ordered, facts)

    return _plan_from_routing(routing)
