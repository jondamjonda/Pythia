"""The trainers' MPS-to-CPU fallback must keep the warm start.

When autograd fails on Apple's MPS backend, the stage-2 trainers rebuild the model on the CPU.
The rebuilt model has to load the warm-start checkpoint again; otherwise training silently starts
from random weights. Training needs torch and the corpus, so this checks the code structure.
"""

import ast
import pathlib

import pytest

_TRAIN = pathlib.Path(__file__).resolve().parent.parent / "train"


def _cpu_fallback_handlers(tree: ast.AST) -> list[ast.ExceptHandler]:
    """Except handlers that switch the device to the CPU (`dev = "cpu"`)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "dev" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Constant)
                    and stmt.value.value == "cpu"
                ):
                    out.append(node)
                    break
    return out


@pytest.mark.parametrize("script", ["train_stage2.py", "train_stage2_caselaw.py"])
def test_cpu_fallback_reloads_the_warm_start(script):
    tree = ast.parse((_TRAIN / script).read_text(encoding="utf-8"))
    handlers = _cpu_fallback_handlers(tree)
    assert handlers, f"{script}: no CPU fallback found"
    for handler in handlers:
        calls = {n.func.id for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "warm_load_state_dict" in calls, f"{script}: CPU fallback rebuilds the model without warm-loading"
