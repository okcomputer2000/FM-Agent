from config import (
    OPENCODE_MAX_RETRIES,
    OPENCODE_SETUP_MODEL,
    OPENCODE_SPEC_MODEL,
    OPENCODE_MODEL_PROVIDER,
    LLM_MODEL,
)
from src.file_utils import collect_file_names, is_file_ready
from src.verification import streaming_reasoner
from src.extract import run_extraction, EXT_TO_LANG, _is_test_file
from src.generate_topdown_layers import generate_topdown_layers
from src.opencode_trace import (
    finish_opencode_trace,
    function_id_from_extracted_path,
    run_opencode_traced,
    start_opencode_traced,
)
from src.incremental_reasoner import run_incremental_pipeline
import os
import sys
import argparse
import json
import time
import shutil
import subprocess
import logging
import tempfile
import contextlib

def _merge_descriptions(target_desc, source_desc):
    """Append a removed module's description to the owning module's description.

    Avoids re-merging the same content if it is already present.
    """
    source_desc = (source_desc or "").strip()
    if not source_desc:
        return target_desc
    if source_desc in target_desc:
        return target_desc
    if not target_desc:
        return source_desc
    return f"{target_desc}\n\n{source_desc}"

def _deduplicate_phases(phases_dir):
    """Ensure each source file appears in at most one phase; keep the earliest."""
    phases_path = os.path.join(phases_dir, "phases.json")
    with open(phases_path, "r") as f:
        data = json.load(f)

    seen = set()
    # Maps each kept source file to the module that first claimed (owns) it.
    file_owner = {}
    phases_to_remove = []
    for phase in sorted(data["phases"], key=lambda p: p["phase"]):
        modules_to_remove = []
        for module in phase["modules"]:
            original = module["source_files"]
            deduped = []
            for sf in original:
                if sf not in seen:
                    seen.add(sf)
                    file_owner[sf] = module
                    deduped.append(sf)
                else:
                    logging.info(
                        "Removed duplicate file '%s' from phase %d module '%s'",
                        sf, phase["phase"], module["name"],
                    )
            module["source_files"] = deduped
            if not deduped:
                # Module lost all its files to deduplication. Merge its description
                # into the owning modules that now hold those same files, then drop it.
                owners = []
                for sf in original:
                    owner = file_owner.get(sf)
                    if owner is not None and owner is not module and owner not in owners:
                        owners.append(owner)
                for owner in owners:
                    owner["description"] = _merge_descriptions(
                        owner.get("description", ""),
                        module.get("description", ""),
                    )
                logging.info(
                    "Removing empty module '%s' from phase %d; merged its description into %d module(s): %s",
                    module.get("name", ""), phase["phase"], len(owners),
                    ", ".join(o.get("name", "") for o in owners),
                )
                modules_to_remove.append(module)
        for module in modules_to_remove:
            phase["modules"].remove(module)
        total_files = sum(len(m["source_files"]) for m in phase["modules"])
        if total_files == 0:
            logging.info("Removing phase %d: no source files remain after deduplication", phase["phase"])
            phases_to_remove.append(phase)
    for phase in phases_to_remove:
        data["phases"].remove(phase)

    # Renumber phases sequentially and update depends_on_phases references
    old_to_new = {}
    for idx, phase in enumerate(sorted(data["phases"], key=lambda p: p["phase"]), start=1):
        old_to_new[phase["phase"]] = idx
        phase["phase"] = idx
    for phase in data["phases"]:
        phase["depends_on_phases"] = [
            old_to_new[dep] for dep in phase.get("depends_on_phases", [])
            if dep in old_to_new
        ]

    with open(phases_path, "w") as f:
        json.dump(data, f, indent=2)

def _get_phase_files(phases_data, phase_num, input_dir):
    """Return relative paths of extracted function files for a given phase."""
    phase = next(p for p in phases_data["phases"] if p["phase"] == phase_num)
    phase_files = []
    for module in phase["modules"]:
        for src_file in module["source_files"]:
            dir_part = os.path.dirname(src_file)
            base = os.path.basename(src_file)
            dot_idx = base.rfind(".")
            if dot_idx >= 0:
                subdir = base[:dot_idx] + "-" + base[dot_idx + 1:]
            else:
                subdir = base
            extracted_dir = os.path.join(input_dir, dir_part, subdir)
            if os.path.isdir(extracted_dir):
                for fname in sorted(os.listdir(extracted_dir)):
                    fpath = os.path.join(extracted_dir, fname)
                    if os.path.isfile(fpath):
                        phase_files.append(os.path.relpath(fpath, input_dir))
    return phase_files


def _clean_previous_run(work_dir):
    """Remove the fm_agent working directory from the previous pipeline run."""
    if os.path.isdir(work_dir):
        shutil.rmtree(work_dir)


def _get_pending_batches(batches, proj_dir):
    """Return batches that still have at least one function without specs."""
    pending = []
    for batch in batches:
        for func_rel in batch.get("functions", []):
            full_path = os.path.join(proj_dir, func_rel)
            if not is_file_ready(full_path):
                pending.append(batch)
                break
    return pending


def _has_source_code(proj_dir):
    """Check whether proj_dir contains at least one source code file."""
    source_exts = set(EXT_TO_LANG.keys())
    for root, dirs, files in os.walk(proj_dir):
        # Skip hidden dirs and common non-source dirs
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   {'node_modules', '__pycache__', 'venv', '.venv', 'fm_agent'}]
        for fname in files:
            ext = fname.rsplit('.', 1)[-1] if '.' in fname else ''
            if ext in source_exts:
                return True
    return False


def _collect_project_source_files(proj_dir):
    """Return non-test source files currently present in proj_dir, relative to proj_dir."""
    source_exts = set(EXT_TO_LANG.keys())
    files = set()
    for root, dirs, names in os.walk(proj_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                   {'node_modules', '__pycache__', 'venv', '.venv', 'fm_agent'}]
        for fname in names:
            ext = fname.rsplit('.', 1)[-1] if '.' in fname else ''
            if ext not in source_exts:
                continue
            rel = os.path.relpath(os.path.join(root, fname), proj_dir).replace(os.sep, '/')
            if not _is_test_file(rel):
                files.add(rel)
    return files


def _phases_cover_current_sources(phases_json, proj_dir):
    """Return whether phases.json is valid for the current source-file set."""
    try:
        with open(phases_json, "r") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False

    listed = set()
    for phase in data.get("phases", []):
        for module in phase.get("modules", []):
            for source_file in module.get("source_files", []):
                listed.add(source_file.replace("\\", "/"))

    if not listed:
        return False
    if any(not os.path.exists(os.path.join(proj_dir, sf)) for sf in listed):
        return False
    return _collect_project_source_files(proj_dir).issubset(listed)


def _run_setup_extract(proj_dir, work_dir, script_dir, is_incremental=False):
    """Stage 2: prepare the setup workflow file and run opencode (with retries) to produce phases.json."""
    workflow_src = os.path.join(script_dir, "md", "workflow_setup_extract.md")
    workflow_dst = os.path.join(work_dir, "workflow_setup_extract.md")
    shutil.copy2(workflow_src, workflow_dst)
    _proj_dir_abs = os.path.abspath(proj_dir)
    _proj_dir_name = os.path.basename(_proj_dir_abs)
    with open(workflow_dst, "r") as _f:
        _md = _f.read()
    _old = ("- `phases[*].modules[*].source_files` — relative paths from repo root of all source files "
            "that belong to this module.")
    _new = (f"- `phases[*].modules[*].source_files` — relative paths from the project root "
            f"`{_proj_dir_abs}` of all source files that belong to this module. "
            f"For example, a file at `{_proj_dir_abs}/path/to/file.ext` must be recorded as "
            f"`path/to/file.ext`, NOT as `{_proj_dir_name}/path/to/file.ext`.")
    _md = _md.replace(_old, _new, 1)
    with open(workflow_dst, "w") as _f:
        _f.write(_md)
    fm_reminder = ("IMPORTANT: The fm_agent/ directory is NOT part of the project source code. "
                    "It is a workspace for storing your output files only. "
                    "Do NOT include fm_agent/ paths in phases.json. "
                    "Do NOT modify any existing project files.")
    incremental_reminder = ("IMPORTANT: An existing fm_agent/phases.json from a previous run is already "
                            "present. Do NOT regenerate it from scratch. Instead, inspect the current "
                            "state of the source code and UPDATE the existing fm_agent/phases.json so it "
                            "reflects the current version of the code: add modules and source files that "
                            "are new, remove entries whose files no longer exist, and adjust phases as "
                            "needed. Preserve entries that are still accurate.")

    phases_json = os.path.join(work_dir, "phases.json")
    prev_mtime = os.path.getmtime(phases_json) if os.path.exists(phases_json) else None

    for attempt in range(1, OPENCODE_MAX_RETRIES + 1):
        if attempt == 1:
            prompt = f"Follow the instructions in the attached file. {fm_reminder}"
        else:
            prompt = ("Continue where you left off. The previous run was interrupted by a network error. "
                      f"Check what has already been done and only complete the remaining steps. {fm_reminder}")
        if is_incremental:
            prompt = f"{prompt} {incremental_reminder}"
        command = ["opencode", "run", "--model", f"{OPENCODE_MODEL_PROVIDER}/{OPENCODE_SETUP_MODEL}",
                   "--file", os.path.join(proj_dir, "fm_agent", "workflow_setup_extract.md"), "--", prompt]
        try:
            run_opencode_traced(
                proj_dir=proj_dir,
                work_dir=work_dir,
                command=command,
                stage="setup_context",
                input_files=["fm_agent/workflow_setup_extract.md"],
                output_files=[
                    "fm_agent/phases.json",
                    "fm_agent/spec_prompts/domain_context/engine_overview.txt",
                ],
                summary=f"OpenCode setup context attempt {attempt}",
                metadata={"attempt": attempt},
            )
        except subprocess.CalledProcessError as e:
            logging.warning(f"Stage 2 attempt {attempt}: opencode exited with code {e.returncode}")

        # Validate that the agent produced phases.json. In incremental mode the file
        # already exists; it may legitimately remain byte-for-byte unchanged for same-file
        # edits, so accept it when it still covers the current source files.
        if os.path.exists(phases_json):
            if (
                not is_incremental
                or os.path.getmtime(phases_json) != prev_mtime
                or _phases_cover_current_sources(phases_json, proj_dir)
            ):
                break

        failure = "update phases.json" if is_incremental else "produce phases.json"
        missing = "phases.json was not updated" if is_incremental else "phases.json missing"
        if attempt < OPENCODE_MAX_RETRIES:
            delay = 10
            print(
                f"[Pipeline] Stage 2 failed to {failure} (attempt {attempt}/{OPENCODE_MAX_RETRIES}). "
                f"Retrying in {delay}s..."
            )
            logging.warning(f"Stage 2 attempt {attempt} failed: {missing}. Retrying in {delay}s.")
            time.sleep(delay)
        else:
            print(
                f"[Pipeline] ERROR: Stage 2 failed after {OPENCODE_MAX_RETRIES} attempts. "
                f"{missing}. "
                f"Check {os.path.basename(proj_dir)}/fm_agent/trace/ for details."
            )
            sys.exit(1)
    
    # Deduplicate source files across phases
    _deduplicate_phases(work_dir)


def _run_opencode_init(proj_dir, work_dir):
    agent_md = os.path.join(proj_dir, "AGENTS.md")
    if os.path.exists(agent_md):
        print("[Pipeline] Stage 1/5: AGENTS.md found, skipping opencode init.")
    else:
        print("[Pipeline] Stage 1/5: Initializing opencode...")
        command = ["opencode", "run", "--model", f"{OPENCODE_MODEL_PROVIDER}/{LLM_MODEL}", "--command", "init"]
        run_opencode_traced(
            proj_dir=proj_dir,
            work_dir=work_dir,
            command=command,
            stage="init",
            output_files=["AGENTS.md"],
            summary="Initialized OpenCode project context",
        )

@contextlib.contextmanager
def frozen_worktree(proj_dir, exclude=("fm_agent",)):
    """Freeze proj_dir's current working tree into an isolated git worktree.

    Captures committed state PLUS uncommitted edits and untracked files, so the
    yielded copy is a faithful snapshot of proj_dir at entry time. Concurrent
    edits to proj_dir afterwards do not affect the snapshot, letting the pipeline
    run against a stable copy.

    The snapshot is built through a private index (GIT_INDEX_FILE), so proj_dir's
    real index and working tree are never touched. Falls back to a plain directory
    copy when proj_dir is not a git repository with a commit. The snapshot folder
    is left in place after the run (including its fm_agent/ outputs); its path is
    logged so it can be inspected or cleaned up manually.

    The `exclude` dirs (the pipeline's own workspace) are kept out of the git
    snapshot commit so it stays clean, but are then copied into the worktree as-is.
    Incremental mode needs the previous run's fm_agent/ results to detect a prior
    full run, and those results are typically gitignored, hence absent from the
    snapshot commit.
    """
    proj_dir = os.path.abspath(proj_dir)
    # Include the repo name in the temp dir so concurrent runs across different
    # repos are distinguishable (e.g. /tmp/fm_agent_wt_myrepo_a3k9d2/snapshot).
    repo_name = os.path.basename(proj_dir.rstrip(os.sep)) or "repo"
    base = tempfile.mkdtemp(prefix=f"fm_agent_wt_{repo_name}_")
    wt = os.path.join(base, "snapshot")

    def _git(*args, **kwargs):
        return subprocess.run(
            ["git", "-C", proj_dir, *args],
            check=True, capture_output=True, text=True, **kwargs,
        ).stdout.strip()

    is_git = False
    try:
        _git("rev-parse", "--verify", "HEAD")
        is_git = True
    except subprocess.CalledProcessError:
        pass

    if is_git:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(base, "index"))
        _git("read-tree", "HEAD", env=env)
        # Stage the full working tree (tracked edits + untracked files). Using a
        # bare `git add -A` lets git silently skip gitignored paths; passing the
        # workspace dirs as :(exclude) pathspecs instead errors out when a repo
        # already gitignores them ("paths are ignored ... use -f"). Drop the
        # workspace dirs from the private index afterwards to cover repos that do
        # NOT gitignore them.
        _git("add", "-A", env=env)
        if exclude:
            _git("rm", "-r", "--cached", "--quiet", "--ignore-unmatch", "--",
                 *exclude, env=env)
        tree = _git("write-tree", env=env)
        snap = _git("commit-tree", tree, "-p", "HEAD", "-m", "fm_agent snapshot")
        _git("worktree", "add", "--detach", wt, snap)
    else:
        logging.info("frozen_worktree: %s is not a git repo; copying instead.", proj_dir)
        shutil.copytree(
            proj_dir, wt,
            ignore=shutil.ignore_patterns(*exclude),
            symlinks=True,
        )

    # Copy the excluded workspace dirs (e.g. fm_agent/ with a prior full run's
    # phases.json and extracted_functions) into the snapshot. They were kept out
    # of the git commit, but incremental mode reads them from disk to compare
    # against, so the snapshot must physically contain them.
    for name in exclude:
        src = os.path.join(proj_dir, name)
        dst = os.path.join(wt, name)
        if os.path.isdir(src) and not os.path.exists(dst):
            shutil.copytree(src, dst, symlinks=True)

    print(f"[Pipeline] Snapshot created at: {wt}")
    print(f"[Pipeline] Snapshot is kept after the run. "
          f"Remove with: git -C {proj_dir} worktree remove --force {wt}"
          if is_git else
          f"[Pipeline] Snapshot is kept after the run. Remove with: rm -rf {wt}")
    yield wt


def run_pipeline(proj_dir):
    if not os.path.isdir(proj_dir):
        print(f"[Pipeline] ERROR: proj_dir does not exist or is not a directory: {proj_dir}")
        sys.exit(1)

    if not _has_source_code(proj_dir):
        print(f"[Pipeline] ERROR: No source code files found in {proj_dir}. "
              f"Supported extensions: {', '.join(sorted(EXT_TO_LANG.keys()))}")
        sys.exit(1)

    work_dir = os.path.join(proj_dir, "fm_agent")
    input_dir = os.path.join(work_dir, "extracted_functions")
    output_dir = os.path.join(work_dir, "logic_verification_results")
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # Clean files from the previous run
    _clean_previous_run(work_dir)
    os.makedirs(work_dir, exist_ok=True)

    # Initialize opencode in the project directory (skip if AGENTS.md already exists)
    _run_opencode_init(proj_dir, work_dir)

    # Copy workflow_setup_extract.md to proj_dir and run opencode against it
    print("[Pipeline] Stage 2/5: Understanding codebase and extracting functions ...")
    _run_setup_extract(proj_dir, work_dir, script_dir)

    # Run function extraction using extract.py
    print("[Pipeline] Extracting functions from source files...")
    run_extraction(proj_dir, work_dir=work_dir, force=True, verbose=True)

    # Copy system_prompt.md to spec_prompts/system_prompt.md
    spec_prompts_dir = os.path.join(work_dir, "spec_prompts")
    os.makedirs(spec_prompts_dir, exist_ok=True)
    shutil.copy2(
        os.path.join(script_dir, "md", "system_prompt.md"),
        os.path.join(spec_prompts_dir, "system_prompt.md"),
    )
    shutil.copy2(
        os.path.join(script_dir, "src", "generate_batch_prompts.py"),
        os.path.join(spec_prompts_dir, "generate_batch_prompts.py"),
    )
    shutil.copy2(
        os.path.join(script_dir, "src", "run_batch_gen.py"),
        os.path.join(spec_prompts_dir, "run_batch_gen.py"),
    )

    print("[Pipeline] Stage 3/5: Collecting file list...")
    file_list = collect_file_names(input_dir, os.path.join(work_dir, "fm_agent_file_list.json"))

    if not file_list:
        print("[Pipeline] No functions found to verify. Skipping spec generation.")
        return

    # --- Stage 4: Generate topdown layers ---
    print("[Pipeline] Stage 4/5: Generating topdown layers...")
    phases_data = json.load(open(os.path.join(work_dir, "phases.json")))
    generate_topdown_layers(work_dir)

    # --- Stage 5: Execute spec generation workflow (per phase, per layer) ---
    print("[Pipeline] Stage 5/5: Generating specs & verification...")
    batch_md_src = os.path.join(script_dir, "md", "workflow_spec_step4_batch.md")
    batch_md_dst = os.path.join(work_dir, "workflow_spec_step4_batch.md")
    shutil.copy2(batch_md_src, batch_md_dst)

    all_processed = set()
    num_phases = len(phases_data["phases"])
    project_name = phases_data.get("project", "project")

    for phase_info in sorted(phases_data["phases"], key=lambda p: p["phase"]):
        phase_num = phase_info["phase"]
        phase_name = phase_info["name"]
        phase_files = _get_phase_files(phases_data, phase_num, input_dir)

        if not phase_files:
            logging.info(f"Phase {phase_num} ({phase_name}): no extracted files, skipping.")
            continue

        # Determine how many layers this phase has
        layers_json_path = os.path.join(
            spec_prompts_dir, f"phase_{phase_num:02d}_topdown_layers.json"
        )
        if not os.path.exists(layers_json_path):
            generate_topdown_layers(work_dir, [phase_num])
        with open(layers_json_path, "r") as f:
            layers_data = json.load(f)
        total_layers = layers_data.get("total_layers", 1)

        batch_dir = os.path.join(
            spec_prompts_dir,
            f"batch_prompts_{project_name}_phase{phase_num:02d}",
        )

        for layer_idx in range(total_layers):
            print(f"[Pipeline] Stage 5/5: Phase {phase_num}/{num_phases} — {phase_name}, Layer {layer_idx}/{total_layers - 1}")

            # Generate batch prompts for this layer
            subprocess.run(
                ["python3", "fm_agent/spec_prompts/generate_batch_prompts.py",
                 "--phase", str(phase_num), "--layers", str(layer_idx)],
                cwd=proj_dir, check=True,
            )

            # Read manifest
            manifest_path = os.path.join(batch_dir, "manifest.json")
            with open(manifest_path, "r") as f:
                manifest = json.load(f)
            all_batches = manifest.get("batches", [])

            if not all_batches:
                logging.info(f"Phase {phase_num} Layer {layer_idx}: no batches, skipping.")
                continue

            batch_rel_dir = os.path.relpath(batch_dir, proj_dir)

            # Build file list for this layer from the manifest
            layer_files = []
            for batch_info in all_batches:
                for func_rel in batch_info.get("functions", []):
                    rel = os.path.relpath(os.path.join(proj_dir, func_rel), input_dir)
                    layer_files.append(rel)

            layer_processed = set()

            for attempt in range(1, OPENCODE_MAX_RETRIES + 1):
                # Find batches with unspecced functions
                pending_batches = _get_pending_batches(all_batches, proj_dir)
                if not pending_batches:
                    break

                # Spawn concurrent opencode processes (one per pending batch)
                spec_procs = []
                spec_trace_records = []
                for batch_info in pending_batches:
                    batch_file = batch_info["file"]
                    batch_prompt_rel = os.path.join(batch_rel_dir, batch_file)
                    batch_prompt_abs = os.path.join(proj_dir, batch_prompt_rel)
                    function_files = batch_info.get("functions", [])
                    function_ids = [
                        function_id_from_extracted_path(func_rel)
                        for func_rel in function_files
                    ]
                    fm_reminder = ("IMPORTANT: fm_agent/ is your output workspace, not project source. "
                                    "Do NOT modify any existing project files.")
                    if attempt == 1:
                        prompt = (
                            f"Process the batch prompt file at {batch_prompt_rel}. "
                            f"Read it and fm_agent/spec_prompts/system_prompt.md, "
                            f"generate behavioral specs for each function listed, "
                            f"and write the complete specced files directly. {fm_reminder}"
                        )
                    else:
                        prompt = (
                            f"Continue processing the batch prompt file at {batch_prompt_rel}. "
                            f"Some functions may already have specs from a previous attempt. "
                            f"Check each function file — only generate specs for those "
                            f"that don't have [SPEC] blocks yet. "
                            f"Read fm_agent/spec_prompts/system_prompt.md for the format rules. {fm_reminder}"
                        )
                    command = ["opencode", "run", "--model", f"{OPENCODE_MODEL_PROVIDER}/{OPENCODE_SPEC_MODEL}",
                               "--file", os.path.join(proj_dir, "fm_agent", "workflow_spec_step4_batch.md"),
                               "--", prompt]
                    trace_record = start_opencode_traced(
                        proj_dir=proj_dir,
                        work_dir=work_dir,
                        command=command,
                        stage="spec_generation",
                        function_ids=function_ids,
                        input_files=[
                            "fm_agent/workflow_spec_step4_batch.md",
                            batch_prompt_rel,
                            "fm_agent/spec_prompts/system_prompt.md",
                        ],
                        output_files=function_files,
                        summary=f"OpenCode spec generation for {batch_file}",
                        metadata={
                            "attempt": attempt,
                            "phase": phase_num,
                            "layer": layer_idx,
                            "batch_file": batch_file,
                        },
                    )
                    spec_trace_records.append(trace_record)
                    spec_procs.append(trace_record.proc)

                logging.info(
                    f"Phase {phase_num} Layer {layer_idx} attempt {attempt}: "
                    f"spawned {len(spec_procs)} opencode processes for {len(pending_batches)} batches"
                )

                newly_processed = streaming_reasoner(input_dir, output_dir, file_list=layer_files,
                                   proj_dir=proj_dir, work_dir=work_dir,
                                   spec_procs=spec_procs,
                                   already_processed=all_processed | layer_processed)
                layer_processed.update(newly_processed)

                for proc in spec_procs:
                    proc.wait()
                for trace_record in spec_trace_records:
                    finish_opencode_trace(trace_record)

                # Check if any files in this layer received specs
                specs_generated = sum(
                    1 for rel in layer_files
                    if is_file_ready(os.path.join(input_dir, rel))
                )
                if specs_generated > 0 and not _get_pending_batches(all_batches, proj_dir):
                    break

                if specs_generated > 0:
                    # Partial progress — retry remaining batches without delay
                    logging.info(
                        f"Phase {phase_num} Layer {layer_idx} attempt {attempt}: "
                        f"{specs_generated} specs generated, retrying remaining batches"
                    )
                    continue

                if attempt < OPENCODE_MAX_RETRIES:
                    delay = 10
                    print(
                        f"[Pipeline] Stage 5 Phase {phase_num} Layer {layer_idx} produced no specs "
                        f"(attempt {attempt}/{OPENCODE_MAX_RETRIES}). "
                        f"Retrying in {delay}s..."
                    )
                    logging.warning(
                        f"Stage 5 Phase {phase_num} Layer {layer_idx} attempt {attempt} failed: "
                        f"no specs generated. Retrying in {delay}s."
                    )
                    time.sleep(delay)
                else:
                    print(
                        f"[Pipeline] ERROR: Stage 5 Phase {phase_num} Layer {layer_idx} failed "
                        f"after {OPENCODE_MAX_RETRIES} attempts. "
                        f"No specs were generated. "
                        f"Check {os.path.basename(proj_dir)}/fm_agent/trace/ for details."
                    )
                    sys.exit(1)

        # Mark all files from this phase as processed for subsequent phases
        for rel in phase_files:
            all_processed.add(os.path.join(input_dir, rel))

    # Print confirmed bug count
    summary_path = os.path.join(work_dir, "bug_validation", "summary.json")
    if os.path.exists(summary_path):
        with open(summary_path, "r") as f:
            summary = json.load(f)
        confirmed = summary.get("total_confirmed", 0)
        print(f"[Pipeline] Confirmed bugs: {confirmed}")

    print("[Pipeline] Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FM Agent pipeline.")
    parser.add_argument("proj_dir", help="Path to the project directory.")
    parser.add_argument(
        "--incremental",
        metavar="INTENT_FILE",
        help="Run in incremental mode. Value is the path to the intent file "
        "defining the goal of modification.",
    )
    parser.add_argument(
        "--old-commit",
        metavar="COMMIT",
        help="The commit id to compare against in incremental mode (required "
        "with --incremental).",
    )
    args = parser.parse_args()

    proj_dir = os.path.abspath(args.proj_dir)

    if args.incremental and not args.old_commit:
        parser.error("--old-commit is required when --incremental is set")

    # Resolve the intent path before snapshotting, since cwd-relative paths must
    # resolve against the real project, not the frozen worktree copy.
    intent_path = os.path.abspath(args.incremental) if args.incremental else None

    start_time = time.time()
    with frozen_worktree(proj_dir) as snap_dir:
        if args.incremental:
            run_incremental_pipeline(snap_dir, intent_path, args.old_commit)
        else:
            run_pipeline(snap_dir)
    end_time = time.time()
    logging.info(f"Total time: {end_time - start_time:.2f} seconds")
