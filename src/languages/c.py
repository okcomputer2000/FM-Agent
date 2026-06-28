from src.languages.codegraph import CodeGraphExtractor


def batch_extract(proj_dir: str) -> dict:
    """Return {abs_filepath: [(func_name, body)]} for all C files."""
    cg = CodeGraphExtractor.from_proj_dir(proj_dir)
    return cg.get_functions_by_file("c", proj_dir) if cg else {}


def call_edges(proj_dir: str) -> dict:
    """Return {(caller_stem, caller_module): {callee_stems}} for C."""
    cg = CodeGraphExtractor.from_proj_dir(proj_dir)
    return cg.get_call_edges("c") if cg else {}
