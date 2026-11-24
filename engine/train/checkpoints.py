"""GNN checkpoint reproducibility: a committed manifest plus one-command verify and regenerate.

The trained GNN weights (`train/gnn_stage1.pt`, `train/gnn_stage2.pt`) are large and gitignored
(`*.pt`), so they usually exist on a single machine. If that copy is lost, or someone clones the
repository, the engine falls back to an untrained GNN (`GNNRetriever` degrades without failing) and
nothing records which weights were the approved ones. This module closes that gap without committing
binaries:

- `checkpoints.manifest.json` (committed) records, for each shipped checkpoint, its SHA-256, byte
  size, mtime and, when torch is available, the metadata the checkpoint carries about itself
  (embedder_id, emb_dim, arch_hash). It also records the exact command that regenerates it.
- `verify` recomputes the SHA-256 of each local file and compares it with the manifest, catching a
  corrupted, truncated, stale or wrong-embedder checkpoint before it is loaded. It needs no torch.
- `write` regenerates the manifest from the local files. Run it after an approved retrain so git
  tracks the new checksums.
- `regen` prints the regeneration recipe. It does not train by itself: training needs the database,
  the `[gnn]` extra and minutes of compute, so the step stays explicit and auditable.

Optional object-storage backup: set PYTHIA_CKPT_REMOTE to an `s3://...` or `gs://...` prefix and
`push`/`pull` call the matching CLI (aws or gcloud), so the manifest checksums verify a real backup
and not only the local copy. There is no SDK dependency; without the variable, push and pull do
nothing.

Run from engine/ (`train/` is a script directory, not a package, so call the file directly):
  .venv/bin/python train/checkpoints.py verify     # check local .pt files against the manifest
  .venv/bin/python train/checkpoints.py write      # regenerate the manifest from local .pt files
  .venv/bin/python train/checkpoints.py regen      # print the regeneration recipe
  .venv/bin/python train/checkpoints.py push|pull  # backup to PYTHIA_CKPT_REMOTE

`verify` exits non-zero on any mismatch or missing file, so it can gate CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

_DIR = pathlib.Path(__file__).resolve().parent
_MANIFEST = _DIR / "checkpoints.manifest.json"

# The shipped checkpoints and the exact command that regenerates each. Run them in order: stage 2
# warm-starts from stage 1. Backup variants (`.bak`, `.prev`) are deliberately not tracked.
_TRACKED = {
    "gnn_stage1.pt": ".venv/bin/python train/pretrain_stage1.py 1500 8",
    "gnn_stage2.pt": ".venv/bin/python train/train_stage2.py 60",
}
_CHUNK = 1 << 20


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _ckpt_meta(path: pathlib.Path) -> dict | None:
    """Return the metadata stored in a `pythia-gnn-ckpt/v1` envelope, or None.

    The embedder id and architecture hash make the manifest record more informative. This is
    best-effort: on a core install without torch it returns None, which is fine.
    """
    try:
        import torch  # lazy: the [gnn] extra; verify itself never needs it
    except ImportError:
        return None
    try:
        obj = torch.load(path, map_location="cpu", weights_only=False)
    except Exception:  # noqa: BLE001
        return None
    meta = obj.get("meta") if isinstance(obj, dict) else None
    if not isinstance(meta, dict):
        return None
    return {k: meta.get(k) for k in ("arch_version", "arch_hash", "embedder_id", "emb_dim")}


def _entry(name: str) -> dict | None:
    p = _DIR / name
    if not p.exists():
        return None
    st = p.stat()
    e = {
        "sha256": _sha256(p),
        "size_bytes": st.st_size,
        "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
        "regenerate": _TRACKED[name],
    }
    meta = _ckpt_meta(p)
    if meta:
        e["meta"] = meta
    return e


def write_manifest() -> dict:
    """Regenerate the manifest from the local .pt files; run after an approved retrain."""
    files = {}
    missing = []
    for name in _TRACKED:
        e = _entry(name)
        if e is None:
            missing.append(name)
        else:
            files[name] = e
    manifest = {
        "_comment": "GNN checkpoint reproducibility manifest — committed; the .pt binaries are gitignored. "
        "Verify: python train/checkpoints.py verify. Regenerate recipe: python train/checkpoints.py regen.",
        "embedder_note": "All checkpoints MUST match the live bge-m3 embedder (emb_dim 1024); a 768-d "
        "mpnet-era checkpoint will fail warm_load_state_dict's embedder hard-check.",
        "files": files,
    }
    _MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(
        f"wrote {_MANIFEST.relative_to(_DIR.parent)} — {len(files)} checkpoint(s)"
        + (f"; MISSING (not on disk, recorded as absent): {missing}" if missing else "")
    )
    return manifest


def verify() -> int:
    """Compare each local .pt file with the committed manifest; non-zero on any mismatch or gap."""
    if not _MANIFEST.exists():
        print(f"NO manifest at {_MANIFEST} — run `python -m train.checkpoints write` first", file=sys.stderr)
        return 2
    manifest = json.loads(_MANIFEST.read_text())
    files = manifest.get("files", {})
    if not files:
        print("manifest tracks no files", file=sys.stderr)
        return 2
    bad = 0
    for name, rec in files.items():
        p = _DIR / name
        if not p.exists():
            print(f"  ✗ {name}: MISSING on disk (regenerate: {rec.get('regenerate')})")
            bad += 1
            continue
        got = _sha256(p)
        if got != rec["sha256"]:
            print(
                f"  ✗ {name}: sha256 MISMATCH (disk {got[:12]}… vs manifest {rec['sha256'][:12]}…) "
                f"— stale/corrupt; regenerate: {rec.get('regenerate')}"
            )
            bad += 1
        else:
            print(f"  ✓ {name}: sha256 ok ({rec['size_bytes']:,} bytes)")
    print(f"verify: {len(files) - bad}/{len(files)} checkpoint(s) match" + (" — FAILED" if bad else " — OK"))
    return 1 if bad else 0


def regen() -> int:
    """Print the regeneration recipe; deliberately does not start training."""
    print("Regenerate the GNN checkpoints (cwd = Build/, needs the live DB + the [gnn] extra):")
    print("  pip install -e '.[gnn]'      # torch + sentence-transformers, once")
    for name, cmd in _TRACKED.items():
        print(f"  {cmd:<48} # → train/{name}")
    print("Then re-stamp the manifest:  python train/checkpoints.py write")
    return 0


def _remote() -> str | None:
    return os.environ.get("PYTHIA_CKPT_REMOTE") or None


def _push_pull(direction: str) -> int:
    """Copy checkpoints to or from object storage with the aws or gcloud CLI.

    The tool is chosen from the PYTHIA_CKPT_REMOTE scheme. Without the variable this does nothing;
    the manifest still verifies the local copies.
    """
    remote = _remote()
    if not remote:
        print(
            f"PYTHIA_CKPT_REMOTE not set — skipping object-storage {direction} (manifest still verifies local copies)."
        )
        return 0
    if remote.startswith("s3://"):
        tool = ["aws", "s3", "cp"]
    elif remote.startswith("gs://"):
        tool = ["gcloud", "storage", "cp"]
    else:
        print(f"unsupported PYTHIA_CKPT_REMOTE scheme: {remote!r} (use s3:// or gs://)", file=sys.stderr)
        return 2
    rc = 0
    for name in _TRACKED:
        local, dst = _DIR / name, remote.rstrip("/") + "/" + name
        src, tgt = (str(local), dst) if direction == "push" else (dst, str(local))
        if direction == "push" and not local.exists():
            print(f"  ⤫ {name}: not on disk, skip push")
            continue
        print(f"  {direction} {name} ↔ {dst}")
        rc |= subprocess.run([*tool, src, tgt], check=False).returncode
    return rc


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "verify"
    if cmd == "verify":
        return verify()
    if cmd == "write":
        write_manifest()
        return 0
    if cmd == "regen":
        return regen()
    if cmd in ("push", "pull"):
        return _push_pull(cmd)
    print(f"usage: python -m train.checkpoints [verify|write|regen|push|pull]  (got {cmd!r})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
