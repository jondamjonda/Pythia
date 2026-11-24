"""NexusPort boundary (the #5 principle, self-enforcing on BOTH axes).

The application / reasoning / transport layer reaches Nexus ONLY through the one door (pythia.ports,
i.e. NexusPort) — never by (a) importing the pythia.graph.* / pythia.gnn.* implementation submodules,
nor (b) issuing raw SQL against the Nexus tables (provision / source_document / pythia_edge). These
tests FAIL if any consumer bypasses the door on either axis, so the single-door rule holds for all
future work, not just today.

The consumer set is AUTO-DISCOVERED by walking pythia/*.py + pythia/reasoning/**/*.py (minus the impl
packages graph/ gnn/ ingest/ and the door ports.py itself) — so a NEW consumer module added later is
caught automatically, with no hand-maintained list to forget to update.
"""

from __future__ import annotations

import pathlib
import re

_PKG = pathlib.Path(__file__).resolve().parent.parent / "pythia"
_EXEMPT_DIRS = {"graph", "gnn", "ingest", "__pycache__"}  # the impl packages — they ARE Nexus
_EXEMPT_FILES = {"ports.py"}  # the door itself

_BAD_IMPORT = re.compile(r"^\s*(?:from|import)\s+pythia\.(?:graph|gnn)\b", re.M)
_BAD_SQL = re.compile(r"\bFROM\s+(?:provision|source_document|pythia_edge)\b", re.I)


def _consumer_files() -> list[pathlib.Path]:
    files = list(_PKG.glob("*.py")) + list(_PKG.glob("reasoning/**/*.py"))
    return [f for f in files if f.relative_to(_PKG).parts[0] not in _EXEMPT_DIRS and f.name not in _EXEMPT_FILES]


def _scan(pattern: re.Pattern) -> list[str]:
    return [
        f"{f.relative_to(_PKG.parent)}: {m.group(0).strip()}"
        for f in _consumer_files()
        for m in pattern.finditer(f.read_text())
    ]


def test_consumers_do_not_import_nexus_impl():
    offenders = _scan(_BAD_IMPORT)
    assert not offenders, (
        "consumer modules must reach Nexus through pythia.ports (NexusPort), not by "
        "importing graph/gnn submodules directly:\n  " + "\n  ".join(offenders)
    )


def test_consumers_do_not_raw_sql_nexus_tables():
    offenders = _scan(_BAD_SQL)
    assert not offenders, (
        "consumer modules must read Nexus tables through NexusPort (fetch_provisions / "
        "candidates / …), not raw SQL:\n  " + "\n  ".join(offenders)
    )
