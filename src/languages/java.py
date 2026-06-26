from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.languages.codegraph import CodeGraphExtractor


def batch_extract(cg: "CodeGraphExtractor", proj_dir: str) -> dict:
    return cg.get_functions_by_file("java", proj_dir)


def call_edges(cg: "CodeGraphExtractor") -> dict:
    return cg.get_call_edges("java")
