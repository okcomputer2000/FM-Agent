from dataclasses import dataclass
from typing import Callable

from src.languages import python as _python
from src.languages import go as _go
from src.languages import c as _c
from src.languages import cpp as _cpp
from src.languages import java as _java
from src.languages import rust as _rust
from src.languages import javascript as _javascript
from src.languages import typescript as _typescript


@dataclass
class LanguageHandler:
    """Extraction and call-graph backend for one language.

    batch_extract(proj_dir) -> {abs_filepath: [(func_name, body)]}
    call_edges(proj_dir)    -> {(caller_stem, caller_module): {callee_stems}}

    Each function handles its own backend (e.g. codegraph) internally and
    returns an empty dict when the backend is unavailable.

    To add a new language:
      1. Create src/languages/<lang>.py implementing batch_extract and call_edges
      2. Import it here and add one entry to REGISTRY
    No other files need to change.
    """
    batch_extract: Callable
    call_edges: Callable


REGISTRY: dict = {
    "python":     LanguageHandler(batch_extract=_python.batch_extract,     call_edges=_python.call_edges),
    "go":         LanguageHandler(batch_extract=_go.batch_extract,         call_edges=_go.call_edges),
    "c":          LanguageHandler(batch_extract=_c.batch_extract,          call_edges=_c.call_edges),
    "cpp":        LanguageHandler(batch_extract=_cpp.batch_extract,        call_edges=_cpp.call_edges),
    "java":       LanguageHandler(batch_extract=_java.batch_extract,       call_edges=_java.call_edges),
    "rust":       LanguageHandler(batch_extract=_rust.batch_extract,       call_edges=_rust.call_edges),
    "javascript": LanguageHandler(batch_extract=_javascript.batch_extract, call_edges=_javascript.call_edges),
    "typescript": LanguageHandler(batch_extract=_typescript.batch_extract, call_edges=_typescript.call_edges),
}


def batch_extract_all(proj_dir: str) -> tuple:
    """Call batch_extract for every registered language and merge results.

    Returns (funcs, langs) where funcs is {abs_filepath: [(func_name, body)]}
    and langs is the set of language keys that returned data.
    """
    funcs = {}
    langs = set()
    for lang, handler in REGISTRY.items():
        result = handler.batch_extract(proj_dir)
        if result:
            funcs.update(result)
            langs.add(lang)
    return funcs, langs


def call_edges_all(proj_dir: str, lang_keys) -> tuple:
    """Call call_edges for each language in lang_keys and merge results.

    Returns (edges, langs) where edges is {(caller_stem, caller_module): {callee_stems}}
    and langs is the set of language keys that returned data.
    """
    edges = {}
    langs = set()
    for lang in lang_keys:
        if lang not in REGISTRY:
            continue
        result = REGISTRY[lang].call_edges(proj_dir)
        if result:
            langs.add(lang)
            for key, callees in result.items():
                edges.setdefault(key, set()).update(callees)
    return edges, langs
