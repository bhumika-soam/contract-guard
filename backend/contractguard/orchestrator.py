import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid

# orchestrator.py is in backend/contractguard/, so prompts live in ./agents/
BASE_DIR = pathlib.Path(__file__).resolve().parent
AGENTS_DIR = BASE_DIR / "agents"
REPO_ROOT = BASE_DIR.parent.parent

# Automatically load BOB_API_KEY (and other variables) from the root .env file
ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# NOTE: sample_diff_report.json is kept ONLY as a reference/example for manual
# testing. It is intentionally NEVER used as a silent default anymore — see the
# argparse setup at the bottom, where diff_file is now a REQUIRED argument.
# (Root cause of the Day-2 "full_name -> name" run: this constant used to be
# argparse's silent default, so an invocation without the path argument quietly
# ran against stale Day-1 sample data instead of a real diff_report_*.json.)
SAMPLE_DIFF_FILE_FOR_REFERENCE_ONLY = AGENTS_DIR / "sample_diff_report.json"
IMPACT_PROMPT_FILE = AGENTS_DIR / "impact_agent_prompt.md"
REPAIR_PROMPT_FILE = AGENTS_DIR / "repair_agent_prompt.md"
VERIFY_PROMPT_FILE = AGENTS_DIR / "verify_agent_prompt.md"
DEFAULT_OUTPUT_FILE = AGENTS_DIR / "impact_report.json"

MAX_RETRIES = 3
MAX_PARALLEL_SUBAGENTS = 4  # Bounds concurrent Bob CLI processes

# FIX A: prompt files must live INSIDE the Bob workspace (REPO_ROOT), not the
# OS temp dir. Bob CLI enforces a workspace boundary and refuses to read
# anything outside it ("the file is outside the workspace boundary") -- this
# is what silently broke every Repair/Verify/Impact-subagent call in the Day-2
# run, since run_bob() was writing to tempfile.gettempdir(). This folder must
# NOT match .bobignore's `_current_prompt*.md` pattern (that was the earlier,
# different bug) -- the "contractguard_prompt_*.md" filename already avoids
# that, so simply relocating it here satisfies both constraints at once.
TMP_PROMPT_DIR = AGENTS_DIR / "tmp_prompts"
TMP_PROMPT_REL_PREFIX = TMP_PROMPT_DIR.relative_to(REPO_ROOT).as_posix() + "/"

# Path segments too generic to use as a candidate-file search token on their own
# (e.g. "/api/v1/items/{id}" should search for "items", not "api").
GENERIC_PATH_SEGMENTS = {"api", "v1", "v2", "v3"}


def make_change_id(diff_entry: dict) -> str:
    endpoint_slug = diff_entry["endpoint"].strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{diff_entry['change_type']}_{endpoint_slug}"


def merge_diff_entries(entries: list[dict]) -> dict:
    """Collapses multiple diff_report entries describing the SAME underlying
    breaking change (same change_type + same old/new schema fragment) into one
    diff_data dict with an `all_endpoints` list.

    RESTORED FIX: Mahi's real diff_report_*.json files are JSON arrays — e.g.
    field-renamed and type-changed each contain 3 entries (POST/GET/PUT on the
    same field). Without this, the pipeline kept only entries[0] and silently
    dropped the other two (this was previously fixed and logged as bug #5 in
    khushi-day1-log.md, but the fix is not present in the file that was
    uploaded here — this restores it).
    """
    if not entries:
        return {}
    if len(entries) == 1:
        return entries[0]

    def signature(e: dict) -> tuple:
        return (
            e.get("change_type"),
            json.dumps(e.get("old_schema_fragment"), sort_keys=True),
            json.dumps(e.get("new_schema_fragment"), sort_keys=True),
        )

    base_sig = signature(entries[0])
    same_signature = [e for e in entries if signature(e) == base_sig]
    different = [e for e in entries if signature(e) != base_sig]

    if different:
        print(
            f"⚠️ {len(different)} of {len(entries)} diff entries have a DIFFERENT "
            f"change signature than entries[0] and will NOT be merged in — they are "
            f"being ignored this run. If this diff_report file is meant to describe "
            f"more than one distinct breaking change, run each signature as its own "
            f"diff_report file instead."
        )

    merged = dict(same_signature[0])
    merged["all_endpoints"] = [
        {"endpoint": e.get("endpoint"), "method": e.get("method")} for e in same_signature
    ]
    return merged


def run_bob(prompt: str, mode: str, task_label: str = "main") -> str:
    """Thread-safe Bob CLI runner using a unique prompt file per subagent call.

    FIX A (this session): the OS temp dir (tempfile.gettempdir()) sits OUTSIDE
    the Bob workspace boundary (REPO_ROOT), and current Bob CLI refuses to
    read any path outside it -- this is exactly what broke every Impact/
    Repair/Verify call in the Day-2 run ("the file is outside the workspace
    boundary"). The earlier fix (see below) correctly solved a DIFFERENT
    problem -- .bobignore blocking `_current_prompt*.md` inside the repo --
    but moved the file too far, past the workspace boundary entirely. Writing
    to TMP_PROMPT_DIR (inside REPO_ROOT, filename still "contractguard_prompt_
    *.md" so it still avoids the .bobignore pattern) satisfies both fixes at
    once. TMP_PROMPT_DIR should be added to .gitignore and .bobignore so its
    contents are never committed or treated as candidate source files.
    """
    bob_path = shutil.which("bob")
    if not bob_path:
        print("❌ Error: 'bob' CLI not found in PATH.")
        sys.exit(1)

    TMP_PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    unique_id = f"{task_label}_{uuid.uuid4().hex[:6]}"
    prompt_file = TMP_PROMPT_DIR / f"contractguard_prompt_{unique_id}.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    short_instruction = (
        f"Read the instructions in '{prompt_file.as_posix()}' and execute them completely."
    )

    cmd = [bob_path, "run", "--accept-license", "--mode", mode, short_instruction]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=os.environ.copy(),
        )
    finally:
        # Clean up temporary prompt file so temp dir stays clean
        if prompt_file.exists():
            try:
                prompt_file.unlink()
            except OSError:
                pass

    if result.returncode != 0:
        print(f"\n❌ Bob CLI [{task_label}] exited with code {result.returncode}")
        print("--- STDOUT ---")
        print(result.stdout)
        print("--- STDERR ---")
        print(result.stderr)
        sys.exit(1)

    return result.stdout.strip()


def extract_last_json(text: str) -> dict:
    """Extracts the last valid JSON object from Bob's CLI output, fixing 120-char line wraps."""
    if "Assistant (" in text:
        text = text.split("Assistant (")[-1]

    matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    for candidate in reversed(matches):
        cleaned = re.sub(r"\s+", " ", candidate).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            continue
    return {}


def _derive_search_token(old_field: str, endpoint: str) -> str:
    """Picks a meaningful literal substring to grep for across the frontend.

    FIX: the old fallback (`endpoint.strip("/").split("/")[0]`) picked the
    FIRST path segment, which for a real endpoint like "/api/v1/items/{id}"
    is just "api" — a near-useless, overly generic token. This now skips
    generic prefix segments (api/v1/v2...) and path-parameter segments
    ("{id}") to find the actual resource name ("items").
    """
    if old_field and old_field != "unknown":
        return old_field
    segments = [s for s in endpoint.strip("/").split("/") if s and not s.startswith("{")]
    meaningful = [s for s in segments if s.lower() not in GENERIC_PATH_SEGMENTS]
    if meaningful:
        return meaningful[0]
    return segments[0] if segments else ""


def find_candidate_frontend_files(old_field: str, endpoint: str) -> list[str]:
    """Finds candidate frontend files referencing the changed field or endpoint to scan in parallel.

    FIX: now scans the ENTIRE `frontend/` tree (src, tests, everything),
    not just `frontend/src`. The Day-2 endpoint-removed run's real breaking
    references lived in `frontend/tests/user-settings.spec.ts` and
    `frontend/tests/utils/privateApi.ts` — outside the old scan root — so
    they were structurally invisible to the Impact Agent no matter what the
    diff report said. node_modules/dist/build output and the mock-report
    output folder are excluded so we don't scan generated or irrelevant files.
    """
    frontend_root = REPO_ROOT / "frontend"
    if not frontend_root.exists():
        return []

    excluded_path_fragments = ("node_modules", "/dist/", "/build/", "mocks/impact-reports")
    search_token = _derive_search_token(old_field, endpoint)
    if not search_token:
        return []

    candidates = []
    for file_path in sorted(frontend_root.rglob("*")):
        if not file_path.is_file() or file_path.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        rel_posix = file_path.relative_to(REPO_ROOT).as_posix()
        if any(fragment in f"/{rel_posix}/" or fragment.strip("/") in rel_posix for fragment in excluded_path_fragments):
            continue
        try:
            content = file_path.read_text(encoding="utf-8-sig")
            if search_token.lower() in content.lower():
                candidates.append(rel_posix)
        except OSError:
            continue

    return candidates


def analyze_single_file_impact(
    rel_file: str, diff_data: dict, impact_base_prompt: str
) -> tuple[str, bool, str, bool]:
    """Subagent worker: runs Impact Agent on a single candidate file concurrently.

    FIX: previously defaulted `is_affected` to True whenever the subagent's
    response couldn't be parsed as JSON — which silently treated a BLOCKED,
    REFUSED, or otherwise failed subagent call as a confirmed affected file.
    A real run's latest_impact_findings.txt showed exactly this: all 20
    parallel subagents were blocked reading their own prompt file (pre-dating
    the tempfile fix) and returned zero valid is_affected JSON, yet every one
    still ended up marked affected — explaining why over-broad, unrelated
    files (shadcn UI primitives, Sidebar, signup/login pages) showed up in
    impact_report.json's affected_files. Failure must default to NOT
    affected, and be reported distinctly so it's visible in the console
    instead of silently masquerading as a confirmed result.

    Returns (rel_file, is_affected, section_output, subagent_ok).
    """
    file_prompt = (
        f"{impact_base_prompt}\n\n"
        f"PARALLEL SUBAGENT TASK: Inspect ONLY the file `{rel_file}`.\n"
        f"Determine if `{rel_file}` uses the changed contract field/endpoint from this Diff Report, "
        f"and list the exact line numbers and code context where it appears.\n\n"
        f"Diff Report JSON:\n{json.dumps(diff_data, indent=2)}\n\n"
        f"At the very end of your response, include a valid JSON block in this exact format:\n"
        f'{{"is_affected": true, "file": "{rel_file}", "line_summary": "Exact lines and usage details"}}'
    )
    label = pathlib.Path(rel_file).stem
    output = run_bob(file_prompt, mode="ask", task_label=f"impact_{label}")
    parsed = extract_last_json(output)

    if not parsed:
        # Subagent failed to return the expected JSON at all (blocked,
        # refused, errored, or malformed). Fail SAFE: do not count this as
        # affected. Flag it distinctly so a human can spot-check the file.
        return (
            rel_file,
            False,
            f"### File: {rel_file}\n"
            f"⚠️ SUBAGENT FAILED — no parseable is_affected JSON returned. "
            f"Treated as NOT affected by default; check this file manually.\n{output}",
            False,
        )

    is_affected = bool(parsed.get("is_affected", False))
    line_summary = parsed.get("line_summary", "")
    return rel_file, is_affected, f"### File: {rel_file}\n{line_summary}\n{output}", True


def run_parallel_impact_agents(
    diff_data: dict, old_field: str, new_field: str
) -> tuple[list[str], str, str]:
    """
    Dispatches parallel Impact Agent subagents across candidate files,
    with an automatic fallback to the single bulk Impact Agent if needed.
    Returns (affected_files, summary_plain_english, combined_impact_output).
    """
    impact_base_prompt = IMPACT_PROMPT_FILE.read_text(encoding="utf-8-sig")
    endpoint = str(diff_data.get("endpoint", ""))
    candidates = find_candidate_frontend_files(old_field, endpoint)

    if candidates:
        print(
            f"⚡ Step 1: Dispatching {len(candidates)} parallel Impact Subagents "
            f"(max {MAX_PARALLEL_SUBAGENTS} concurrent workers)..."
        )
        for c in candidates:
            print(f"   ↳ Queued subagent for: {c}")

        affected_files: list[str] = []
        combined_sections: list[str] = []

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_SUBAGENTS) as executor:
            future_to_file = {
                executor.submit(
                    analyze_single_file_impact, rel_file, diff_data, impact_base_prompt
                ): rel_file
                for rel_file in candidates
            }
            for future in as_completed(future_to_file):
                rel_file = future_to_file[future]
                try:
                    file_path, is_affected, section_output, subagent_ok = future.result()
                    if is_affected:
                        affected_files.append(file_path)
                        combined_sections.append(section_output)
                        print(f"   ✅ Subagent confirmed impact in: {file_path}")
                    elif not subagent_ok:
                        print(f"   ❌ Subagent FAILED (no valid result) on: {file_path} — excluded, check manually")
                    else:
                        print(f"   ➖ Subagent cleared (not affected): {file_path}")
                except Exception as exc:
                    print(f"   ⚠️ Subagent error on {rel_file}: {exc}")

        # Keep affected_files in deterministic sorted order
        affected_files.sort()

        if affected_files:
            file_count = len(affected_files)
            summary_plain_english = (
                f"The '{old_field}' field was changed on {endpoint} "
                f"affecting {file_count} files."
            )
            combined_output = "\n\n".join(combined_sections)
            return affected_files, summary_plain_english, combined_output

    # Fallback: Run the original single bulk Impact Agent if no pre-filtered candidates matched
    print("🔍 Step 1 (Fallback): Running bulk Impact Agent (Ask mode)...")
    impact_prompt = (
        f"{impact_base_prompt}\n\n"
        f"Diff Report JSON:\n{json.dumps(diff_data, indent=2)}\n\n"
        f"At the very end of your response, include a valid JSON block in this exact format:\n"
        f'{{"summary_plain_english": "...", "affected_files": ["frontend/src/..."]}}'
    )
    impact_output = run_bob(impact_prompt, mode="ask", task_label="impact_bulk")
    impact_json = extract_last_json(impact_output)
    affected_files = impact_json.get("affected_files", [])
    file_count = len(affected_files)
    summary_plain_english = impact_json.get(
        "summary_plain_english",
        f"The '{old_field}' field was renamed to '{new_field}' on {endpoint} across {file_count} files.",
    )
    return affected_files, summary_plain_english, impact_output


def get_git_dirty_paths() -> set[str]:
    """Returns the set of paths `git status --porcelain` currently reports as
    changed, normalized to forward-slash paths relative to REPO_ROOT.

    Used by Fix D (see enforce_file_allowlist) to snapshot "already dirty
    before this run started" state, so the allowlist guardrail can tell that
    apart from "the Repair Agent just changed this." Without this, on a
    working tree that already has uncommitted changes (e.g. your own
    in-progress fixes to orchestrator.py, .gitignore, .bobignore) sitting
    there when the pipeline starts, `enforce_file_allowlist` had no way to
    know those predated the run -- it reverted them as "out-of-scope Repair
    Agent edits," which is exactly what happened on 2026-09-27: the fixed
    orchestrator.py, .gitignore and .bobignore were all silently
    `git checkout --`'d back to their last-committed (pre-fix) state.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ Could not run 'git status' to snapshot baseline dirty state: {exc}")
        return set()

    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path_part = line[3:].strip().strip('"')
        if "->" in path_part:
            path_part = path_part.split("->")[-1].strip().strip('"')
        paths.add(path_part.replace("\\", "/"))
    return paths


def enforce_file_allowlist(affected_files: list[str], baseline_dirty: set[str]) -> list[str]:
    """Reverts any file the Repair Agent touched that wasn't in affected_files
    AND wasn't already dirty before this run started (Fix D -- see
    get_git_dirty_paths above for why the baseline_dirty exclusion exists).

    NEW GUARDRAIL. Zero Bobcoin cost — pure git/python, runs after Repair
    Agent and before Verify Agent. If the agent edited anything outside the
    Impact Agent's approved file list (e.g. backend/app/, backend/tests/, or
    an unrelated frontend component), those specific files are reverted (or,
    if newly created, deleted) in place, and the violation is reported so the
    caller can treat the attempt as failed WITHOUT spending a Verify call
    on a doomed run.

    Uses `git status --porcelain` rather than `git diff --name-only`: the
    latter only shows changes to files git already tracks, so a Repair Agent
    that CREATES a new out-of-scope file would slip past it entirely.
    `--porcelain` also reports untracked (`??`) files, which are handled by
    deletion rather than `git checkout --` (checkout can't restore a file
    that was never tracked).

    Returns the list of out-of-scope files that were found and reverted.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ Could not run 'git status' to check the file allowlist: {exc}")
        return []

    changed = []  # list of (status_code, path)
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status_code = line[:2]
        path_part = line[3:].strip().strip('"')
        if "->" in path_part:  # rename/copy: "old -> new"
            path_part = path_part.split("->")[-1].strip().strip('"')
        changed.append((status_code, path_part.replace("\\", "/")))

    # FIX C: the orchestrator writes its own housekeeping files every run
    # (latest_impact_findings.txt before Repair even starts) regardless of
    # how many files Impact found. When affected_files is empty -- as
    # happened when every Impact subagent failed -- `allowed` was also
    # empty, so the orchestrator's OWN write got flagged and reverted as an
    # "out-of-scope Repair Agent edit," which it never was. Whitelist these
    # explicitly so a real scope violation isn't confused with our own
    # bookkeeping, and exclude the tmp-prompt dir as a belt-and-braces check
    # (its files are deleted immediately after each run_bob call, but this
    # guards against any left behind by an interrupted run).
    orchestrator_owned = {
        (AGENTS_DIR / "latest_impact_findings.txt").relative_to(REPO_ROOT).as_posix(),
        (AGENTS_DIR / "impact_report.json").relative_to(REPO_ROOT).as_posix(),
    }

    allowed = {f.replace("\\", "/") for f in affected_files}
    violations = [
        path for _, path in changed
        if path not in allowed
        and path not in orchestrator_owned
        and not path.startswith(TMP_PROMPT_REL_PREFIX)
        and path not in baseline_dirty
    ]

    for status_code, path in changed:
        if path not in violations:
            continue
        try:
            if status_code.strip() == "??":
                # Untracked new file — nothing for git to revert to, so delete it.
                target = REPO_ROOT / path
                if target.exists():
                    target.unlink()
            else:
                subprocess.run(["git", "checkout", "--", path], check=True, cwd=REPO_ROOT)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"⚠️ Could not revert out-of-scope file {path}: {exc}")

    return violations


def _build_task_instruction(diff_data: dict, old_field: str, new_field: str, file_count: int) -> str:
    """Change-type-aware Repair Agent instructions.

    FIX: the old prompt always said "rename '{old_field}' to '{new_field}'"
    no matter what actually happened — actively misleading for
    field_type_changed (same name, new type) and endpoint_removed (no field
    mapping at all, an endpoint disappeared). This was already flagged as an
    open risk in khushi-day1-log.md before branch B/C were ever run for real.
    """
    change_type = str(diff_data.get("change_type", "")).lower()

    if change_type == "field_renamed":
        return (
            f"Update ONLY the {file_count} files listed above: rename the field '{old_field}' to "
            f"'{new_field}' everywhere it is used (type definitions, destructuring, JSX rendering, "
            f"form schemas). Do not touch any unrelated variables, formatting, or other lines in those files."
        )
    if change_type in ("field_type_changed", "type_changed"):
        return (
            f"Update ONLY the {file_count} files listed above: the field '{old_field}' changed type "
            f"(see old_schema_fragment/new_schema_fragment in the Diff Report above for the exact old/new "
            f"type). This is NOT a rename — the field name is unchanged, only its type changed. Update type "
            f"definitions and any code that assumes the old type (parsing, formatting, comparisons). "
            f"Do not touch any unrelated variables, formatting, or other lines in those files."
        )
    if change_type == "endpoint_removed":
        return (
            f"Update ONLY the {file_count} files listed above: the endpoint "
            f"{diff_data.get('method', '')} {diff_data.get('endpoint', '')} has been removed entirely. "
            f"This is NOT a rename or type change — there is no old/new field mapping to apply. Remove or "
            f"safely guard every call site that depended on this endpoint (delete the API call, remove or "
            f"disable any UI control that triggered it — e.g. a delete button — and handle the resulting "
            f"UI state gracefully). Do not touch any unrelated variables, formatting, or other lines in those files."
        )
    return (
        f"Update ONLY the {file_count} files listed above to resolve the breaking change described in the "
        f"Diff Report. Do not touch any unrelated variables, formatting, or other lines in those files."
    )


def run_orchestrator(
    diff_input: str | pathlib.Path | dict,
    output_path: str | pathlib.Path = DEFAULT_OUTPUT_FILE,
) -> dict:
    """
    Runs Parallel Impact Subagents -> Repair Agent -> [allowlist guardrail] ->
    Verify Agent (with bounded retries) and writes impact_report.json + frontend
    mock files.

    NOTE: diff_input no longer has a default value. Passing no diff file is a
    hard error now (see argparse below) instead of silently running against
    the stale sample_diff_report.json — that silent fallback is what produced
    the Day-2 "full_name -> name" run against the wrong (fake) data.
    """
    if isinstance(diff_input, dict):
        raw_data = diff_input
    else:
        diff_path = pathlib.Path(diff_input)
        if not diff_path.exists():
            print(f"❌ Missing diff report file: {diff_path}")
            sys.exit(1)
        raw_data = json.loads(diff_path.read_text(encoding="utf-8-sig"))

    if isinstance(raw_data, list):
        raw_data = merge_diff_entries(raw_data)

    diff_data: dict = raw_data if isinstance(raw_data, dict) else {}

    old_field = next(iter(diff_data.get("old_schema_fragment", {})), "unknown")
    new_field = next(iter(diff_data.get("new_schema_fragment", {})), "unknown")
    change_id = diff_data.get("change_id") or make_change_id(diff_data)

    print(f"\n📄 Using diff report: change_type={diff_data.get('change_type')} "
          f"endpoint={diff_data.get('endpoint')} method={diff_data.get('method')}")

    # Fix D: snapshot what's already dirty BEFORE touching anything, so the
    # allowlist guardrail later can tell "pre-existing uncommitted work" apart
    # from "the Repair Agent just changed this."
    baseline_dirty = get_git_dirty_paths()
    if baseline_dirty:
        print(
            f"\n⚠️ Working tree was not clean before this run started "
            f"({len(baseline_dirty)} pre-existing changed path(s)). These will "
            f"NOT be reverted as scope violations, but commit or stash them "
            f"between runs so nothing gets lost track of:"
        )
        for p in sorted(baseline_dirty):
            print(f"   - {p}")

    # 1. Run Parallel Impact Subagents (Ask mode)
    affected_files, summary_plain_english, impact_output = run_parallel_impact_agents(
        diff_data, old_field, new_field
    )
    file_count = len(affected_files)

    print(f"\n--- Impact Subagents Summary ({file_count} affected files) ---")
    for f in affected_files:
        print(f" - {f}")

    findings_file = AGENTS_DIR / "latest_impact_findings.txt"
    findings_file.write_text(impact_output, encoding="utf-8")

    # 2 & 3. Run Repair Agent + [allowlist guardrail] + Verify Agent Loop (max 3 attempts)
    repair_base_prompt = REPAIR_PROMPT_FILE.read_text(encoding="utf-8-sig")
    verify_base_prompt = VERIFY_PROMPT_FILE.read_text(encoding="utf-8-sig")

    verify_status = "fail"
    verify_log = "Verification did not run."
    patch_applied = False
    patch_description = "No patch applied."
    last_error_feedback = ""

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n🛠️ Step 2 (Attempt {attempt}/{MAX_RETRIES}): Running Repair Agent (Agent mode)...")
        task_instruction = _build_task_instruction(diff_data, old_field, new_field, file_count)
        repair_prompt = (
            f"{repair_base_prompt}\n\n"
            f"Diff Report:\n{json.dumps(diff_data, indent=2)}\n\n"
            f"Affected files ({file_count} files total): {json.dumps(affected_files)}\n"
            f"Detailed findings from parallel Impact Subagents:\n{impact_output}\n\n"
            f"{task_instruction}\n"
            f"STRICT SCOPE: You may ONLY modify the files listed above. Do NOT modify any file under "
            f"backend/app/ or backend/tests/, and do NOT modify any frontend file not in the list, even if "
            f"it looks related. If you believe a file outside this list needs changes, STOP and report that "
            f"instead of editing it — an automated check will revert and reject any out-of-scope edit.\n"
            f"In your patch_description, explicitly state '{file_count} files' so the count matches affected_files.\n"
            f'At the very end of your response, include a JSON block: {{"patch_description": "Brief summary of changes made"}}'
        )
        if last_error_feedback:
            repair_prompt += (
                f"\n\nWARNING: Previous repair attempt failed verification with these errors. "
                f"Fix them carefully:\n{last_error_feedback}"
            )

        repair_output = run_bob(repair_prompt, mode="agent", task_label=f"repair_{attempt}")
        print("\n--- Repair Agent Output ---")
        print(repair_output)

        repair_json = extract_last_json(repair_output)
        patch_description = repair_json.get(
            "patch_description",
            f"Updated {file_count} affected files.",
        )

        # --- NEW: allowlist guardrail, runs BEFORE Verify so a scope violation
        # never burns a Verify call on a doomed attempt. ---
        violations = enforce_file_allowlist(affected_files, baseline_dirty)
        if violations:
            print(f"\n⚠️ Repair Agent attempt {attempt} touched {len(violations)} out-of-scope "
                  f"file(s), reverted: {violations}")
            verify_status = "fail"
            verify_log = (
                f"Repair Agent modified out-of-scope files not in affected_files: {violations}. "
                f"These have been automatically reverted. The Repair Agent must only modify the exact "
                f"files listed in affected_files."
            )
            patch_applied = False
            last_error_feedback = verify_log
            if attempt < MAX_RETRIES:
                print(f"\n⚠️ Scope violation on attempt {attempt}. Looping back to Repair Agent "
                      f"(skipping Verify Agent this attempt to save Bobcoins)...")
                continue
            else:
                print(f"\n❌ Max retries ({MAX_RETRIES}) reached with scope violations. Setting patch_applied = False.")
                break

        # Run Verify Agent
        print(f"\n🧪 Step 3 (Attempt {attempt}/{MAX_RETRIES}): Running Verify Agent (Agent mode)...")
        verify_output = run_bob(verify_base_prompt, mode="agent", task_label=f"verify_{attempt}")
        print("\n--- Verify Agent Output ---")
        print(verify_output)

        verify_json = extract_last_json(verify_output)
        verify_status = str(verify_json.get("verify_status", "")).lower()
        verify_log = verify_json.get("verify_log", verify_output)

        # FIX B: fail-safe default. Previously, if Bob's response had no
        # explicit {"verify_status": ...} JSON block, this defaulted to
        # "pass" unless the literal substring "error ts" appeared anywhere
        # in the output -- so a Verify Agent that never ran tsc/the build at
        # all (e.g. blocked from reading its own prompt file, refused, or
        # errored before touching the codebase) was silently reported as a
        # PASS. This mirrors bug #9's fix for the Impact Agent: an
        # unparseable or incomplete result must default to FAIL, and a
        # workspace-boundary refusal must be recognized explicitly rather
        # than relying on one narrow substring check.
        REFUSAL_MARKERS = (
            "outside the workspace",
            "outside the current workspace",
            "cannot read",
            "i cannot access",
            "tools are blocked",
            "tool access to it is blocked",
        )
        if verify_status not in ("pass", "fail"):
            lowered = verify_output.lower()
            if any(marker in lowered for marker in REFUSAL_MARKERS):
                verify_status = "fail"
                verify_log = (
                    "Verify Agent could not read its own instructions or was "
                    "blocked from executing (workspace-boundary refusal). "
                    "Treated as FAIL, not a silent pass. Raw output:\n"
                    + verify_output
                )
            elif "error ts" in lowered:
                verify_status = "fail"
            else:
                # No explicit verify_status JSON AND no recognizable
                # failure/refusal text either: still FAIL. Never assume a
                # build passed just because a specific error string wasn't
                # spotted -- absence of evidence of failure is not evidence
                # of a pass.
                verify_status = "fail"
                verify_log = (
                    "Verify Agent did not return a parseable "
                    '{"verify_status": ...} result, and no pass/fail signal '
                    "could be confirmed in its output. Treated as FAIL by "
                    "default (fail-safe) rather than assumed pass. Raw "
                    "output:\n" + verify_output
                )

        if verify_status == "pass":
            patch_applied = True
            print(f"\n✅ Verification PASSED on attempt {attempt}!")
            break
        else:
            patch_applied = False
            last_error_feedback = verify_log
            if attempt < MAX_RETRIES:
                print(f"\n⚠️ Verification FAILED on attempt {attempt}. Looping back to Repair Agent...")
            else:
                print(f"\n❌ Max retries ({MAX_RETRIES}) reached. Setting patch_applied = False.")

    if not patch_applied:
        patch_description = f"Failed to produce a passing patch across {file_count} files after {MAX_RETRIES} attempts."

    # 4. Write final impact_report.json in the locked schema
    final_report = {
        "change_id": change_id,
        "change_type": diff_data.get("change_type"),
        "endpoint": diff_data.get("endpoint"),
        "method": diff_data.get("method"),
        "summary_plain_english": summary_plain_english,
        "severity": diff_data.get("severity"),
        "affected_files": affected_files,
        "patch_applied": patch_applied,
        "patch_description": patch_description,
        "verify_status": verify_status,
        "verify_log": verify_log,
        "old_schema_fragment": diff_data.get("old_schema_fragment"),
        "new_schema_fragment": diff_data.get("new_schema_fragment"),
        "detected_at": diff_data.get("detected_at"),
        "all_endpoints": diff_data.get("all_endpoints"),
    }

    # Route directly to the matching frontend mock file based on change_type
    output_map = {
        "field_renamed": REPO_ROOT / "frontend/src/mocks/impact-reports/drift-field-renamed.json",
        "field_type_changed": REPO_ROOT / "frontend/src/mocks/impact-reports/drift-type-changed.json",
        "type_changed": REPO_ROOT / "frontend/src/mocks/impact-reports/drift-type-changed.json",
        "endpoint_removed": REPO_ROOT / "frontend/src/mocks/impact-reports/drift-endpoint-removed.json",
    }

    change_type = str(final_report.get("change_type", ""))
    frontend_mock_file = output_map.get(change_type)

    if frontend_mock_file:
        frontend_mock_file.parent.mkdir(parents=True, exist_ok=True)
        frontend_mock_file.write_text(json.dumps(final_report, indent=2), encoding="utf-8")
        print(f"\n✅ Frontend mock automatically updated:\n{frontend_mock_file}")

    # Also keep a local copy in agents/impact_report.json
    out_file = pathlib.Path(output_path)
    out_file.write_text(json.dumps(final_report, indent=2), encoding="utf-8")
    print(f"🎉 Pipeline complete! Backup report written to:\n{out_file}")
    return final_report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ContractGuard Subagent Orchestrator")
    parser.add_argument(
        "diff_file",
        help=(
            "Path to a real diff_report_*.json file. REQUIRED — no default, no silent "
            "fallback to sample_diff_report.json. This used to default silently and caused "
            "a run against stale Day-1 sample data; that fallback has been removed."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Path to write impact_report.json",
    )
    args = parser.parse_args()
    run_orchestrator(args.diff_file, args.output)