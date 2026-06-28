from src.languages.codegraph import CodeGraphExtractor


def batch_extract(proj_dir: str) -> dict:
    """Return {abs_filepath: [(func_name, body)]} for all JavaScript files."""
    cg = CodeGraphExtractor.from_proj_dir(proj_dir)
    return cg.get_functions_by_file("javascript", proj_dir) if cg else {}


def call_edges(proj_dir: str) -> dict:
    """Return {(caller_stem, caller_module): {callee_stems}} for JavaScript."""
    cg = CodeGraphExtractor.from_proj_dir(proj_dir)
    return cg.get_call_edges("javascript") if cg else {}
