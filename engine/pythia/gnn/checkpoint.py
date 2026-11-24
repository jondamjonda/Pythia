"""GNN checkpoint format and the shared warm-start loader.

`save_checkpoint` writes a self-describing `pythia-gnn-ckpt/v1` envelope, `warm_load_state_dict`
loads one (or a legacy bare state dict) with strict-mode safety apart from a single sanctioned key
migration, and `migrate_bare_checkpoint` wraps a legacy bare `.pt` in the envelope. Nothing here
trains: `save_checkpoint` serialises whatever weights it is given.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import torch

from pythia.gnn.model import LegalGFM
from pythia.ingest.embed_bge import DIM as _EMB_DIM
from pythia.ingest.embed_bge import EMB_MODEL as _EMBEDDER_ID

# ── format constants ────────────────────────────────────────────────────────────
FORMAT_TAG = "pythia-gnn-ckpt/v1"
_FORMAT_PREFIX = "pythia-gnn-ckpt"  # version-tolerant prefix used to detect wrapped payloads
ARCH_VERSION = "legalgfm/v2"  # bumped when the per-relation gate became the derived rel_gate_head

# The one sanctioned migration. Older checkpoints are bare state dicts with a fixed per-relation gate
# parameter, `rel_gate` (shape [n_relations]), which the model replaced with the derived gloss head
# `rel_gate_head.*`. A plain strict load would crash on them, and a bare `strict=False` load would
# silently reinitialise any mismatched tensor (a corrupted backbone shape, a 768-d tensor from the
# older mpnet embedder). warm_load_state_dict therefore allows exactly these keys, and nothing else,
# to be reinitialised or dropped.
_SANCTIONED_MISSING = frozenset({"rel_gate_head.weight", "rel_gate_head.bias"})
_SANCTIONED_UNEXPECTED = frozenset({"rel_gate"})
# Keys excluded from the arch hash so that it stays stable across the gate migration (old
# checkpoints carry `rel_gate`, new ones `rel_gate_head.*`, and neither is part of the inductive
# backbone's identity). The hash must not move when a legacy bare checkpoint is warm-loaded into
# the current model.
_ARCH_HASH_EXCLUDE_PREFIX = "rel_gate"


# ── helpers ──────────────────────────────────────────────────────────────────────
def _is_wrapped(obj: Any) -> bool:
    """Return True if `obj` is a tagged checkpoint envelope rather than a bare state dict."""
    return isinstance(obj, Mapping) and str(obj.get("format", "")).startswith(_FORMAT_PREFIX)


def _state_dict_of(obj: Any) -> "OrderedDict[str, torch.Tensor]":
    """Return the raw parameter table from either a wrapped payload or a bare state_dict."""
    sd = obj["state_dict"] if _is_wrapped(obj) else obj
    if not isinstance(sd, Mapping):
        raise TypeError(f"expected a state_dict mapping, got {type(sd).__name__}")
    return OrderedDict(sd)


def arch_hash(state_dict: Mapping[str, torch.Tensor]) -> str:
    """Return a fingerprint of the backbone shape that is stable across the gate migration.

    The hash is sha256 over the sorted {parameter name: shape} map with all `rel_gate*` keys
    excluded. It is computed identically for a legacy bare checkpoint (carrying `rel_gate`) and the
    current model (carrying `rel_gate_head.*`), so it changes only when a backbone tensor's name or
    shape changes, which is a genuine architecture break.
    """
    shape_map = {k: list(v.shape) for k, v in state_dict.items() if not k.startswith(_ARCH_HASH_EXCLUDE_PREFIX)}
    blob = json.dumps(shape_map, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def graph_fingerprint(graph: Any) -> dict | None:
    """Return a content fingerprint of the LegalGraph a checkpoint was built against.

    Telemetry only, not a load-time gate. It is a pure function of the in-memory edge tensors (no
    database round-trip): the edge count plus an order-independent XOR fold of per-edge
    (src|dst|rel|weight) hashes, in the spirit of `intelligence.current_fingerprint`. Returns None
    when no graph, or a graph without edge tensors, is supplied.
    """
    if graph is None:
        return None
    try:
        n_edges = int(graph.edge_src.numel())
        src = graph.edge_src.tolist()
        dst = graph.edge_dst.tolist()
        rel = graph.edge_rel.tolist()
        wts = graph.edge_weight.tolist()
    except AttributeError:
        return None
    acc = 0
    for s, d, r, w in zip(src, dst, rel, wts):
        h = hashlib.blake2b(f"{s}|{d}|{r}|{w:.6g}".encode(), digest_size=8).digest()
        acc ^= int.from_bytes(h, "big")
    return {
        "n_nodes": int(getattr(graph, "n_nodes", 0)),
        "n_edges": n_edges,
        "edge_hash": f"{acc:016x}",
    }


def _model_hparams(model: LegalGFM) -> dict:
    """Read the constructor hyperparameters back from a built LegalGFM.

    No relation count is recorded; the model has no relation-count parameters by design.
    """
    return {
        "rel_emb_dim": model.rel_proj.in_features,
        "hidden_dim": model.d,
        "n_layers": model.n_layers,
        "query_dim": model.query_proj.in_features,
    }


# ── public API ────────────────────────────────────────────────────────────────────
def save_checkpoint(
    path: str | Path, model: LegalGFM, graph: Any = None, meta_extra: Mapping[str, Any] | None = None
) -> dict:
    """Serialise `model` into a self-describing `pythia-gnn-ckpt/v1` envelope at `path`.

    A pure serialiser: it writes whatever weights `model` currently holds (untrained, warm-started or
    hand-set) and stores no optimizer state. The metadata records the embedder identity, model
    hyperparameters and arch hash. `graph` (optional) adds relation telemetry and a content
    fingerprint; `meta_extra` is merged verbatim into the metadata for callers that want to stamp
    extra informational fields. Returns the metadata dict that was written.
    """
    sd = OrderedDict((k, v.detach().cpu()) for k, v in model.state_dict().items())
    rel_ids = list(getattr(graph, "rel_ids", []) or [])
    meta: dict[str, Any] = {
        "arch_version": ARCH_VERSION,
        "arch_hash": arch_hash(sd),
        "model_hparams": _model_hparams(model),
        "embedder_id": _EMBEDDER_ID,
        "emb_dim": _EMB_DIM,
        "rel_ids_present": rel_ids,  # telemetry: which relations existed when saved
        "graph_fingerprint": graph_fingerprint(graph),
    }
    if meta_extra:
        meta.update(dict(meta_extra))
    payload = {"format": FORMAT_TAG, "state_dict": sd, "meta": meta}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(payload, path)
    return meta


def warm_load_state_dict(model: LegalGFM, payload_or_path: Any) -> dict:
    """Load a checkpoint into `model`, allowing only the sanctioned gate migration.

    Transfers every shape-compatible backbone key from `payload_or_path`, which may be a wrapped v1
    payload, a bare state dict, or a path to either. Raises ValueError unless all of these hold:

    - If metadata is present, the checkpoint's `embedder_id` and `emb_dim` match the live embedder
      (this guards against the older 768-d mpnet vectors). Missing metadata is reported as a
      warning, not an error.
    - No key needs skipping for a shape mismatch, so an incompatible backbone tensor is never
      silently reinitialised.
    - After loading, the missing keys are a subset of {rel_gate_head.weight, rel_gate_head.bias}
      and the dropped keys a subset of {rel_gate}.

    Returns:
        A report dict with n_transferred, reinit_keys, dropped_keys, warnings and arch_hash. The
        engine treats an empty `warnings` list as a clean, verified warm load.
    """
    obj = _load_obj(payload_or_path)
    warnings: list[str] = []

    if _is_wrapped(obj):
        meta = obj.get("meta") or {}
        ck_embedder = meta.get("embedder_id")
        ck_dim = meta.get("emb_dim")
        if ck_embedder is not None and ck_embedder != _EMBEDDER_ID:
            raise ValueError(
                f"embedder mismatch: checkpoint was built with embedder_id={ck_embedder!r} but the "
                f"live embedder is {_EMBEDDER_ID!r} — these are different vector spaces; refusing to load."
            )
        if ck_dim is not None and int(ck_dim) != int(_EMB_DIM):
            raise ValueError(
                f"embedding-dim mismatch: checkpoint emb_dim={ck_dim} but live emb_dim={_EMB_DIM} "
                f"(e.g. the 768-d mpnet era vs 1024-d bge-m3); refusing to load."
            )
        if ck_embedder is None or ck_dim is None:
            # A wrapped envelope should be self-describing. If embedder_id or emb_dim is missing, the
            # checks above passed silently (a None value is never compared), so flag it: the engine
            # reads an empty `warnings` list as a clean, trusted warm load.
            warnings.append(
                "wrapped checkpoint meta is missing embedder_id/emb_dim — embedder identity could not be verified."
            )
    else:
        warnings.append(
            "checkpoint is a BARE state_dict with no meta — embedder identity could not be verified "
            "(run migrate_bare_checkpoint to wrap it)."
        )

    src_sd = _state_dict_of(obj)
    tgt_sd = model.state_dict()

    # Transfer only shape-compatible keys; collect the rest for a precise error message.
    compat: "OrderedDict[str, torch.Tensor]" = OrderedDict()
    shape_mismatch: list[str] = []
    for k, v in src_sd.items():
        if k in tgt_sd:
            if tuple(v.shape) == tuple(tgt_sd[k].shape):
                compat[k] = v
            else:
                shape_mismatch.append(f"{k}: ckpt{tuple(v.shape)} vs model{tuple(tgt_sd[k].shape)}")

    # A backbone tensor whose shape does not match is a hard error; never silently reinitialise it.
    if shape_mismatch:
        raise ValueError(
            "shape-incompatible backbone tensor(s) in checkpoint (refusing to silently reinit): "
            + "; ".join(sorted(shape_mismatch))
        )

    load_res = model.load_state_dict(compat, strict=False)
    missing = set(load_res.missing_keys)
    unexpected = set(load_res.unexpected_keys)

    # Restore strict-mode safety except for the one sanctioned gate migration.
    bad_missing = missing - _SANCTIONED_MISSING
    if bad_missing:
        raise ValueError(
            "checkpoint is missing backbone key(s) beyond the sanctioned gate migration: "
            f"{sorted(bad_missing)} (sanctioned-missing = {sorted(_SANCTIONED_MISSING)})"
        )
    # load_state_dict reports unexpected keys only among those it was given, and `compat` holds only
    # keys the model has. The legacy `rel_gate` was filtered out above for that reason, so account
    # for it explicitly here as a dropped key.
    dropped = [k for k in src_sd if k not in tgt_sd]
    bad_unexpected = set(dropped) - _SANCTIONED_UNEXPECTED
    if bad_unexpected:
        raise ValueError(
            "checkpoint carries unexpected key(s) beyond the sanctioned gate migration: "
            f"{sorted(bad_unexpected)} (sanctioned-unexpected = {sorted(_SANCTIONED_UNEXPECTED)})"
        )
    if unexpected:  # defensive: cannot happen while `compat` holds only in-model keys
        raise ValueError(f"load_state_dict reported unexpected keys: {sorted(unexpected)}")

    return {
        "n_transferred": len(compat),
        "reinit_keys": sorted(missing),  # the rel_gate_head.* keys, freshly zero-initialised
        "dropped_keys": sorted(dropped),  # the legacy rel_gate, dropped on migration
        "warnings": warnings,
        "arch_hash": arch_hash(src_sd),
    }


def migrate_bare_checkpoint(path: str | Path, *, out_path: str | Path | None = None, graph: Any = None) -> dict:
    """Wrap a bare `.pt` state dict in a `pythia-gnn-ckpt/v1` envelope with informational metadata.

    No weight is retrained or changed; the tensors are copied through verbatim. If `path` is
    already wrapped this is a no-op that returns its existing metadata. Writes to `out_path`
    (default: `path`, in place) and returns the metadata dict written.
    """
    path = Path(path)
    obj = _load_obj(path)
    if _is_wrapped(obj):
        return dict(obj.get("meta") or {})

    sd = _state_dict_of(obj)
    rel_ids = list(getattr(graph, "rel_ids", []) or [])
    meta: dict[str, Any] = {
        "arch_version": ARCH_VERSION,
        "arch_hash": arch_hash(sd),
        # Hyperparameters are inferred from the bare tensors (best effort; there is no model to read).
        "model_hparams": _infer_hparams_from_state_dict(sd),
        "embedder_id": _EMBEDDER_ID,
        "emb_dim": _EMB_DIM,
        "rel_ids_present": rel_ids,
        "graph_fingerprint": graph_fingerprint(graph),
        "migrated_from_bare": True,  # provenance: this was lifted from a bare .pt
    }
    payload = {"format": FORMAT_TAG, "state_dict": sd, "meta": meta}
    dst = Path(out_path) if out_path is not None else path
    dst.parent.mkdir(parents=True, exist_ok=True)
    torch.save(payload, dst)
    return meta


# ── internals ──────────────────────────────────────────────────────────────────────
def _load_obj(payload_or_path: Any) -> Any:
    """Return the checkpoint object for an in-memory payload or state dict, or load it from a path.

    The on-disk content is tensors plus JSON-safe metadata, so `weights_only=True` is both safe and
    sufficient.
    """
    if isinstance(payload_or_path, (str, Path)):
        return torch.load(payload_or_path, map_location="cpu", weights_only=True)
    return payload_or_path


def _infer_hparams_from_state_dict(sd: Mapping[str, torch.Tensor]) -> dict:
    """Reconstruct the constructor hyperparameters from a bare state dict, best effort.

    Used for migration metadata only. Reads the same tensors LegalGFM's constructor produces.
    """
    hp: dict[str, int] = {}
    if "rel_proj.weight" in sd:  # Linear(rel_emb_dim -> hidden_dim): [d, rel_emb_dim]
        hp["rel_emb_dim"] = int(sd["rel_proj.weight"].shape[1])
        hp["hidden_dim"] = int(sd["rel_proj.weight"].shape[0])
    if "query_proj.weight" in sd:  # Linear(query_dim -> hidden_dim): [d, query_dim]
        hp["query_dim"] = int(sd["query_proj.weight"].shape[1])
    # n_layers = number of distinct rel_mlp.<i>.* blocks.
    layers = {k.split(".")[1] for k in sd if k.startswith("rel_mlp.")}
    if layers:
        hp["n_layers"] = len(layers)
    return hp
