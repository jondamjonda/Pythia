"""The Nexus corpus name that the regsuper-to-nexus crosswalk is keyed on.

The crosswalk table (`provision_id_map`) is unique on (nexus_id, nexus_corpus), so every write to it
(scripts/sync_regsuper_from_nexus.py) and every lookup (L2/L3) must use this exact string. If they
diverged, the join would silently match nothing: no error, but one layer could ship preview-only
results while another ships the full cascade on the same run. This is a leaf module that imports
nothing from regsuper, so the ORM, the cascade and the populator script can all share it safely.
"""

from __future__ import annotations

# The canonical Nexus corpus served by the engine that the crosswalk maps into. Adding a second
# corpus is a deliberate change in several places (populate it, then teach the lookups to select
# it), never an implicit drift.
NEXUS_CORPUS = "pythia"
