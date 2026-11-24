"""Regenerate tests/fixtures_browse_golden.json after the P9 corpus enlargement (browse FUNCTIONS are
unchanged — only the data grew, so the byte-stable goldens legitimately move). Keeps the same
act_tree_ext. Run: .venv/bin/python tests/_regen_browse_golden.py"""

import asyncio
import json
from pathlib import Path

from pythia.db import close_pool
from pythia.graph.browse import act_tree, browse_index

GOLDEN = Path(__file__).parent / "fixtures_browse_golden.json"


def _canon(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True))


async def main():
    old = json.loads(GOLDEN.read_text())
    ext = old["act_tree_ext"]
    ix = await browse_index()
    res = await act_tree(ext)
    stable = {k: v for k, v in res.items() if k not in ("nodes", "tree")}
    new = {
        "facets": _canon(ix["facets"]),
        "catalogue": _canon(ix["catalogue"]),
        "act_tree": _canon(stable),
        "act_tree_ext": ext,
    }
    GOLDEN.write_text(json.dumps(new, ensure_ascii=False, indent=1))
    print(f"regenerated: act_tree_ext={ext}  catalogue_docs={len(new['catalogue'])}  facet_axes={list(new['facets'])}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
