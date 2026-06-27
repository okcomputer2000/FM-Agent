from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.languages.codegraph import CodeGraphExtractor


def batch_extract(cg: "CodeGraphExtractor", proj_dir: str) -> dict:
    """Return {abs_filepath: [(func_name, body)]} for all Go files."""
    return cg.get_functions_by_file("go", proj_dir)


def call_edges(cg: "CodeGraphExtractor") -> dict:
    """Return {(caller_stem, caller_basename): {callee_stems}} for Go."""
    return cg.get_call_edges("go")
