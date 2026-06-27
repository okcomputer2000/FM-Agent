"""
CodeGraph backend for FM-Agent function extraction and call graph building.

Requires the user to have run `codegraph init` in the project directory first,
which produces `.codegraph/codegraph.db` (SQLite).

To add support for a new language: add its FM-Agent lang_key to CODEGRAPH_SUPPORTED.
No other changes are needed in extract.py or generate_topdown_layers.py.
"""

import logging
import os
import sqlite3
import subprocess
from collections import defaultdict

# Languages currently routed through the codegraph backend.
# One language is added per PR; all other languages continue to use the
# existing regex fallback automatically.
CODEGRAPH_SUPPORTED = {
    "python",
}

# Maps FM-Agent lang_key → the language string stored in codegraph's SQLite
# nodes.language column. Only includes languages that codegraph actually supports.
# ArkTS is omitted (not supported by codegraph).
# CUDA maps to "c" because codegraph treats .cu files as C.
_CG_LANG = {
    "python":     "python",
    "go":         "go",
    "rust":       "rust",
    "c":          "c",
    "cpp":        "cpp",
    "cuda":       "c",
    "java":       "java",
    "javascript": "javascript",
    "typescript": "typescript",
}


class CodeGraphExtractor:
    """Query a codegraph SQLite database to extract functions and call edges."""

    def __init__(self, db_path: str):
        self._db = db_path

    @classmethod
    def from_proj_dir(cls, proj_dir: str):
        """Return an extractor if .codegraph/codegraph.db exists, else None.

        Checks both proj_dir itself and its parent directory, because
        generate_topdown_layers() receives work_dir (fm_agent/) as its
        proj_dir argument, while codegraph init runs in the real project root.
        """
        for candidate in [proj_dir, os.path.dirname(os.path.abspath(proj_dir))]:
            db_path = os.path.join(candidate, ".codegraph", "codegraph.db")
            if os.path.exists(db_path):
                return cls(db_path)
        return None

    def get_functions_by_file(self, lang_key: str, proj_dir: str = None) -> dict:
        """Return {abs_filepath: [(func_name, body_text), ...]} for all files.

        body_text is the raw source lines for that function, matching the format
        that extract_functions_from_file returns.

        proj_dir must be supplied so that the relative file paths stored by
        codegraph can be resolved to absolute paths for opening and for dict
        key lookup in run_extraction.
        """
        cg_lang = _CG_LANG.get(lang_key)
        if not cg_lang:
            return {}

        conn = sqlite3.connect(self._db)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name, file_path, start_line, end_line
            FROM nodes
            WHERE kind IN ('function', 'method') AND language = ?
            ORDER BY file_path, start_line
            """,
            (cg_lang,),
        )
        rows = cur.fetchall()
        conn.close()

        by_file = defaultdict(list)
        for name, file_path, start_line, end_line in rows:
            by_file[file_path].append((name, int(start_line), int(end_line)))

        result = {}
        for file_path, funcs in by_file.items():
            abs_path = os.path.join(proj_dir, file_path) if proj_dir else file_path
            try:
                with open(abs_path, "r", errors="replace") as f:
                    all_lines = f.readlines()
            except OSError:
                continue

            file_funcs = []
            for name, start_line, end_line in funcs:
                # codegraph uses 1-indexed lines, end_line is inclusive
                body_lines = all_lines[start_line - 1 : end_line]
                body = "".join(body_lines)
                if not body.endswith("\n"):
                    body += "\n"
                file_funcs.append((name, body))

            result[abs_path] = file_funcs

        return result

    def get_call_edges(self, lang_key: str) -> dict:
        """Return {(caller_stem, caller_basename): {callee_stem, ...}} for the given language.

        caller_stem / callee_stem are plain function names (fqn.split('::')[-1]).
        caller_basename is os.path.basename(caller_file_path), used to disambiguate
        same-name functions defined in different files.
        """
        cg_lang = _CG_LANG.get(lang_key)
        if not cg_lang:
            return {}

        conn = sqlite3.connect(self._db)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.name, s.file_path, t.name
            FROM edges e
            JOIN nodes s ON e.source = s.id
            JOIN nodes t ON e.target = t.id
            WHERE e.kind = 'calls' AND s.language = ?
            """,
            (cg_lang,),
        )
        rows = cur.fetchall()
        conn.close()

        result = defaultdict(set)
        for caller, caller_file, callee in rows:
            key = (caller, os.path.basename(caller_file))
            result[key].add(callee)
        return dict(result)


def try_codegraph_init(proj_dir: str) -> None:
    """Run `codegraph init` in proj_dir if the index does not yet exist.

    Silently skips when codegraph is not installed so the pipeline falls back
    to the regex-based extractor without any error.
    """
    db_path = os.path.join(proj_dir, ".codegraph", "codegraph.db")
    if os.path.exists(db_path):
        return
    print("[Pipeline] Building codegraph index (this runs once per project)...")
    try:
        result = subprocess.run(
            ["codegraph", "init"], cwd=proj_dir, capture_output=True, text=True
        )
    except FileNotFoundError:
        return  # codegraph not installed
    if result.returncode == 0:
        print("[Pipeline] codegraph index built.")
    else:
        logging.warning(
            "codegraph init failed (non-fatal, falling back to regex): %s",
            result.stderr[:300],
        )
