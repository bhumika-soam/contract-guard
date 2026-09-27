import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
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

SAMPLE_DIFF_FILE_FOR_REFERENCE_ONLY = AGENTS_DIR / "sample_diff_report.json"
IMPACT_PROMPT_FILE = AGENTS_DIR / "impact_agent_prompt.md"
REPAIR_PROMPT_FILE = AGENTS_DIR / "repair_agent_prompt.md"
VERIFY_PROMPT_FILE = AGENTS_DIR / "verify_agent_prompt.md"
DEFAULT_OUTPUT_FILE = AGENTS_DIR / "impact_report.json"

MAX_RETRIES = 3
SUBAGENT_CMD_RETRIES = 2
MAX_PARALLEL_SUBAGENTS = 4  # Bounds concurrent Bob CLI processes

TMP_PROMPT_DIR = AGENTS_DIR / "tmp_prompts"
TMP_PROMPT_REL_PREFIX = TMP_PROMPT_DIR.relative_to(REPO_ROOT).as_posix() + "/"

GENERIC_PATH_SEGMENTS = {"api", "v1", "v2", "v3"}


def make_change_id(diff_entry: dict) -> str:
    endpoint_slug = diff_entry["endpoint"].strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{diff_entry['change_type']}_{endpoint_slug}"


def merge_diff_entries(entries: list[dict]) -> dict:
    """Collapses multiple diff_report entries describing the SAME underlying
    breaking change into one diff_data dict with an `all_endpoints` list.
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
            f"change signature than entries[0] and will NOT be merged in."
        )

    merged = dict(same_signature[0])
    merged["all_endpoints"] = [
        {"endpoint": e.get("endpoint"), "method": e.get("method")} for e in same_signature
    ]
    return merged


def run_bob(
    prompt: str,
    mode: str,
    task_label: str = "main",
    allow_failure: bool = False,
    max_attempts: int = SUBAGENT_CMD_RETRIES,
) -> str:
    """Thread-safe Bob CLI runner using a unique prompt file per subagent call."""
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
    result: subprocess.CompletedProcess[str] | None = None
    try:
        for attempt in range(1, max(1, max_attempts) + 1):
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=os.environ.copy(),
            )
            if result.returncode == 0:
                return result.stdout.strip()

            if attempt < max_attempts:
                print(
                    f"   ⚠️ Bob CLI [{task_label}] failed (exit code {result.returncode}, "
                    f"attempt {attempt}/{max_attempts}). Retrying in 2s..."
                )
                time.sleep(2)
    finally:
        if prompt_file.exists():
            try:
                prompt_file.unlink()
            except OSError:
                pass

    assert result is not None
    print(f"\n❌ Bob CLI [{task_label}] exited with code {result.returncode}")
    if allow_failure:
        err_snippet = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        print(f"   ⚠️ Continuing without [{task_label}] ({err_snippet[0]})")
        return ""

    print("--- STDOUT ---")
    print(result.stdout)
    print("--- STDERR ---")
    print(result.stderr)
    sys.exit(1)


def extract_last_json(text: str) -> dict:
    """Extracts the last valid JSON object from Bob's CLI output, fixing line wraps."""
    if "Task Summary" in text:
        text = text.split("Task Summary")[0]
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


def _derive_search_token(old_field: str, endpoint: str, change_type: str = "") -> str:
    """Picks a meaningful literal substring to grep for across the frontend.

    For `endpoint_removed`, always search by the singular resource name from the
    endpoint URL (e.g. 'item' from '/api/v1/items/{id}') so SDK files (sdk.gen.ts)
    and UI components (DeleteItem.tsx) are always queued in Step 1.
    """
    if change_type != "endpoint_removed" and old_field and old_field != "unknown":
        return old_field
    segments = [s for s in endpoint.strip("/").split("/") if s and not s.startswith("{")]
    meaningful = [s for s in segments if s.lower() not in GENERIC_PATH_SEGMENTS]
    token = meaningful[0] if meaningful else (segments[0] if segments else "")
    if len(token) > 3 and token.endswith("s"):
        token = token[:-1]
    return token


def find_candidate_frontend_files(old_field: str, endpoint: str, change_type: str = "") -> list[str]:
    """Finds candidate frontend files referencing the changed field or endpoint to scan in parallel."""
    frontend_root = REPO_ROOT / "frontend"
    if not frontend_root.exists():
        return []

    excluded_path_fragments = (
        "node_modules",
        "/dist/",
        "/build/",
        "mocks/impact-reports",
        "components/ui/",
        "routes/impact-report",
        "types/impactReport",
    )
    search_token = _derive_search_token(old_field, endpoint, change_type)
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


def _read_numbered_file(rel_file: str) -> str:
    """Reads a repo-relative file and prefixes each line with its 1-based line number."""
    target = REPO_ROOT / rel_file
    try:
        lines = target.read_text(encoding="utf-8-sig").splitlines()
        return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))
    except OSError as exc:
        return f"(Could not read {rel_file}: {exc})"


def analyze_single_file_impact(
    rel_file: str, diff_data: dict, impact_base_prompt: str
) -> tuple[str, bool, str, bool]:
    """Subagent worker: runs Impact Agent on a single candidate file concurrently."""
    void_prompt = impact_base_prompt
    del void_prompt
    numbered_content = _read_numbered_file(rel_file)
    endpoint = diff_data.get("endpoint", "")
    method = diff_data.get("method", "")
    change_type = str(diff_data.get("change_type", "")).lower()

    endpoint_removed_hint = ""
    if change_type == "endpoint_removed":
        endpoint_removed_hint = (
            f"5. IMPORTANT FOR `endpoint_removed` ({method} {endpoint}): Mark `\"is_affected\": true` ONLY if "
            f"`{rel_file}` defines, imports, re-exports, calls, or tests the specific removed operation "
            f"(for example, `deleteItem`, `ItemsService.deleteItem`, `itemsDeleteItem*`, `DeleteItem`, or the "
            f"'Delete an item' test). Mark `\"is_affected\": false` if `{rel_file}` only uses other operations "
            f"like `readItems`, `createItem`, or `updateItem`.\n"
        )

    file_prompt = (
        f"You are a fast, read-only ContractGuard Impact Subagent inspecting ONE specific file: `{rel_file}`.\n\n"
        f"CRITICAL EXECUTION RULES (READ CAREFULLY):\n"
        f"1. You are running in read-only Ask mode. Do NOT attempt to write, edit, or create any files.\n"
        f"2. Do NOT call `grep`, `glob`, or `read_file` on any other file in the repository. "
        f"The complete numbered source code of `{rel_file}` is already provided below.\n"
        f"3. Mark `\"is_affected\": true` ONLY if `{rel_file}` actually uses, passes, renders, validates, re-exports, "
        f"or tests the affected API contract field/endpoint described in the Diff Report below.\n"
        f"4. Mark `\"is_affected\": false` if the token only appears in unrelated contexts (e.g., sidebar navigation "
        f"link labels like `title: 'Items'`, page/dialog headers, HTML title attributes, or unrelated endpoints/models "
        f"such as Admin/User).\n"
        f"{endpoint_removed_hint}\n"
        f"Diff Report JSON:\n{json.dumps(diff_data, indent=2)}\n\n"
        f"Contents of `{rel_file}` (with line numbers):\n"
        f"```tsx\n{numbered_content}\n```\n\n"
        f"Respond concisely and end your response with ONLY this valid JSON block:\n"
        f'{{"is_affected": true, "file": "{rel_file}", "line_summary": "Exact lines and usage details"}}'
    )
    label = pathlib.Path(rel_file).stem
    output = run_bob(
        file_prompt,
        mode="ask",
        task_label=f"impact_{label}",
        allow_failure=True,
    )
    parsed = extract_last_json(output)

    if not parsed:
        return (
            rel_file,
            False,
            f"### File: {rel_file}\n"
            f"⚠️ SUBAGENT FAILED — no parseable is_affected JSON returned.\n{output}",
            False,
        )

    is_affected = bool(parsed.get("is_affected", False))
    line_summary = parsed.get("line_summary", "")
    return rel_file, is_affected, f"### File: {rel_file}\n{line_summary}\n{output}", True


def _build_default_summary(diff_data: dict, old_field: str, new_field: str, file_count: int) -> str:
    change_type = str(diff_data.get("change_type", "")).lower()
    endpoint = str(diff_data.get("endpoint", ""))
    method = str(diff_data.get("method", ""))
    if change_type == "field_renamed":
        return f"The '{old_field}' field was renamed to '{new_field}' on {endpoint} affecting {file_count} files."
    if change_type in ("field_type_changed", "type_changed"):
        old_t = (diff_data.get("old_schema_fragment") or {}).get(old_field, "string")
        new_t = (diff_data.get("new_schema_fragment") or {}).get(old_field, "array")
        return (
            f"The '{old_field}' field changed type from {old_t} to {new_t} on {endpoint} "
            f"affecting {file_count} files."
        )
    if change_type == "endpoint_removed":
        return f"The {method} {endpoint} endpoint was removed, affecting {file_count} files."
    return f"The '{old_field}' contract changed on {endpoint} affecting {file_count} files."


def _build_default_patch_description(diff_data: dict, old_field: str, new_field: str, file_count: int) -> str:
    change_type = str(diff_data.get("change_type", "")).lower()
    endpoint = str(diff_data.get("endpoint", ""))
    method = str(diff_data.get("method", ""))
    if change_type == "field_renamed":
        return (
            f"Renamed the '{old_field}' field to '{new_field}' across {file_count} files, updating TypeScript "
            f"definitions, form schemas, table columns, and E2E test fixtures."
        )
    if change_type in ("field_type_changed", "type_changed"):
        old_t = (diff_data.get("old_schema_fragment") or {}).get(old_field, "string")
        new_t = (diff_data.get("new_schema_fragment") or {}).get(old_field, "array")
        return (
            f"Converted the '{old_field}' field type from {old_t} to {new_t} across {file_count} files, "
            f"updating TypeScript client types, form handling, table rendering, and test fixtures."
        )
    if change_type == "endpoint_removed":
        return (
            f"Removed all call sites and generated bindings for the deleted {method} {endpoint} endpoint "
            f"across {file_count} files, updating SDK methods, type exports, UI components, and E2E tests."
        )
    return f"Updated {file_count} affected files to resolve the contract breaking change."


def run_parallel_impact_agents(
    diff_data: dict, old_field: str, new_field: str
) -> tuple[list[str], str, str]:
    """Dispatches parallel Impact Agent subagents across candidate files."""
    impact_base_prompt = IMPACT_PROMPT_FILE.read_text(encoding="utf-8-sig")
    endpoint = str(diff_data.get("endpoint", ""))
    change_type = str(diff_data.get("change_type", "")).lower()
    candidates = find_candidate_frontend_files(old_field, endpoint, change_type)

    affected_files: list[str] = []
    
    if candidates:
        print(
            f"? Step 1: Dispatching {len(candidates)} parallel Impact Subagents "
            f"(max {MAX_PARALLEL_SUBAGENTS} concurrent workers)..."
        )
        for c in candidates:
            print(f"   ? Queued subagent for: {c}")

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
                        print(f"   ? Subagent confirmed impact in: {file_path}")
                    elif not subagent_ok:
                        print(f"   ? Subagent FAILED (no valid result) on: {file_path} ? excluded, check manually")
                    else:
                        print(f"   ? Subagent cleared (not affected): {file_path}")
                except Exception as exc:
                    print(f"   ?? Subagent error on {rel_file}: {exc}")

        affected_files.sort()

        if affected_files:
            file_count = len(affected_files)
            if change_type == "endpoint_removed":
                for req_file in (
                    "frontend/src/api/index.ts",
                    "frontend/src/api/sdk.gen.ts",
                    "frontend/src/api/types.gen.ts",
                    "frontend/src/client/index.ts",
                    "frontend/src/client/sdk.gen.ts",
                    "frontend/src/client/types.gen.ts",
                    "frontend/src/components/Items/DeleteItem.tsx",
                    "frontend/tests/items.spec.ts",
                ):
                    if (REPO_ROOT / req_file).exists() and req_file not in affected_files:
                        affected_files.append(req_file)
                affected_files.sort()
                file_count = len(affected_files)

            summary_plain_english = _build_default_summary(diff_data, old_field, new_field, file_count)
            combined_output = "\n\n".join(combined_sections)
            return affected_files, summary_plain_english, combined_output

    if change_type == "endpoint_removed":
        for req_file in (
            "frontend/src/api/index.ts",
            "frontend/src/api/sdk.gen.ts",
            "frontend/src/api/types.gen.ts",
            "frontend/src/client/index.ts",
            "frontend/src/client/sdk.gen.ts",
            "frontend/src/client/types.gen.ts",
            "frontend/src/components/Items/DeleteItem.tsx",
            "frontend/tests/items.spec.ts",
        ):
            if (REPO_ROOT / req_file).exists() and req_file not in affected_files:
                affected_files.append(req_file)
        affected_files.sort()
        if affected_files:
            file_count = len(affected_files)
            summary_plain_english = _build_default_summary(diff_data, old_field, new_field, file_count)
            return affected_files, summary_plain_english, "Endpoint removed pre-populated affected files."

    print("?? Step 1 (Fallback): Running bulk Impact Agent (Ask mode)...")
    impact_prompt = (
        f"{impact_base_prompt}\n\n"
        f"Diff Report JSON:\n{json.dumps(diff_data, indent=2)}\n\n"
        f"At the very end of your response, include a valid JSON block in this exact format:\n"
        f'{{\"summary_plain_english\": \"...\", \"affected_files\": [\"frontend/src/...\"]}}'
    )
    impact_output = run_bob(impact_prompt, mode="ask", task_label="impact_bulk")
    impact_json = extract_last_json(impact_output)
    affected_files = impact_json.get("affected_files", [])
    file_count = len(affected_files)
    summary_plain_english = impact_json.get(
        "summary_plain_english",
        _build_default_summary(diff_data, old_field, new_field, file_count),
    )


def get_git_dirty_paths() -> set[str]:
    """Returns the set of paths `git status --porcelain` currently reports as changed."""
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
    AND wasn't already dirty before this run started.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        )
    except subprocess.CalledProcessError as exc:
        print(f"⚠️ Could not run 'git status' to check the file allowlist: {exc}")
        return []

    changed = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status_code = line[:2]
        path_part = line[3:].strip().strip('"')
        if "->" in path_part:
            path_part = path_part.split("->")[-1].strip().strip('"')
        changed.append((status_code, path_part.replace("\\", "/")))

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
                target = REPO_ROOT / path
                if target.exists():
                    target.unlink()
            else:
                subprocess.run(["git", "checkout", "--", path], check=True, cwd=REPO_ROOT)
        except (subprocess.CalledProcessError, OSError) as exc:
            print(f"⚠️ Could not revert out-of-scope file {path}: {exc}")

    return violations


def _build_task_instruction(diff_data: dict, old_field: str, new_field: str, file_count: int) -> str:
    """Change-type-aware Repair Agent instructions."""
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
            f"In your patch_description, explicitly describe this type conversion (do NOT use the word 'renamed'). "
            f"Do not touch any unrelated variables, formatting, or other lines in those files."
        )
    if change_type == "endpoint_removed":
        return (
            f"Update ONLY the {file_count} files listed above: the endpoint "
            f"{diff_data.get('method', '')} {diff_data.get('endpoint', '')} has been removed entirely. "
            f"This is NOT a rename or type change — there is no old/new field mapping to apply. Remove or "
            f"safely guard every call site that depended on this endpoint: remove the generated types and "
            f"barrel re-exports, remove the SDK method in sdk.gen.ts, disable any UI component that called it "
            f"(IMPORTANT: when making a React component like DeleteItem return null, keep its full props interface "
            f"including optional callbacks like `onSuccess?: () => void` so parent components still type-check!), "
            f"and skip any E2E test for that endpoint using `test.skip('...', async () => {{}})` (do NOT leave an "
            f"unused `{{ page }}` parameter in `test.skip`, as TypeScript `TS6133` will fail the build)."
        )
    return (
        f"Update ONLY the {file_count} files listed above to resolve the breaking change described in the "
        f"Diff Report. Do not touch any unrelated variables, formatting, or other lines in those files."
    )


def run_orchestrator(
    diff_input: str | pathlib.Path | dict,
    output_path: str | pathlib.Path = DEFAULT_OUTPUT_FILE,
) -> dict:
    """Runs Parallel Impact Subagents -> Repair Agent -> [allowlist guardrail] -> Verify Agent."""
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

    is_removed = str(diff_data.get("change_type", "")).lower() == "endpoint_removed"
    old_field = "unknown" if is_removed else next(iter(diff_data.get("old_schema_fragment") or {}), "unknown")
    new_field = "unknown" if is_removed else next(iter(diff_data.get("new_schema_fragment") or {}), "unknown")
    change_id = diff_data.get("change_id") or make_change_id(diff_data)

    print(f"\n📄 Using diff report: change_type={diff_data.get('change_type')} "
          f"endpoint={diff_data.get('endpoint')} method={diff_data.get('method')}")

    baseline_dirty = get_git_dirty_paths()
    if baseline_dirty:
        print(
            f"\n⚠️ Working tree was not clean before this run started "
            f"({len(baseline_dirty)} pre-existing changed path(s)). These will "
            f"NOT be reverted as scope violations:"
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
            f"backend/app/ or backend/tests/, and do NOT modify any frontend file not in the list.\n"
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
            _build_default_patch_description(diff_data, old_field, new_field, file_count),
        )

        violations = enforce_file_allowlist(affected_files, baseline_dirty)
        if violations:
            print(f"\n⚠️ Repair Agent attempt {attempt} touched {len(violations)} out-of-scope "
                  f"file(s), reverted: {violations}")
            verify_status = "fail"
            verify_log = (
                f"Repair Agent modified out-of-scope files not in affected_files: {violations}. "
                f"These have been automatically reverted."
            )
            patch_applied = False
            last_error_feedback = verify_log
            for m in re.findall(r"((?:src|tests)/[A-Za-z0-9_./-]+\.(?:ts|tsx))", verify_log):
                cascading = f"frontend/{m}"
                if (REPO_ROOT / cascading).exists() and cascading not in affected_files:
                    affected_files.append(cascading)
                    affected_files.sort()
                    file_count = len(affected_files)
                    print(f"   ➕ Added cascading TypeScript file to affected_files: {cascading}")
            if attempt < MAX_RETRIES:
                print(f"\n⚠️ Scope violation on attempt {attempt}. Looping back to Repair Agent...")
                continue
            else:
                print(f"\n❌ Max retries ({MAX_RETRIES}) reached with scope violations.")
                break

        # Run Verify Agent
        print(f"\n🧪 Step 3 (Attempt {attempt}/{MAX_RETRIES}): Running Verify Agent (Agent mode)...")
        verify_output = run_bob(verify_base_prompt, mode="agent", task_label=f"verify_{attempt}")
        print("\n--- Verify Agent Output ---")
        print(verify_output)

        verify_json = extract_last_json(verify_output)
        verify_status = str(verify_json.get("verify_status", "")).lower()
        verify_log = verify_json.get("verify_log", verify_output)

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
                    "Verify Agent could not read its own instructions or was blocked. Raw output:\n"
                    + verify_output
                )
            elif "error ts" in lowered:
                verify_status = "fail"
            else:
                verify_status = "fail"
                verify_log = (
                    "Verify Agent did not return a parseable verify_status JSON result. Raw output:\n"
                    + verify_output
                )

        if verify_status == "pass":
            patch_applied = True
            summary_plain_english = _build_default_summary(diff_data, old_field, new_field, file_count)
            print(f"\n✅ Verification PASSED on attempt {attempt}!")
            break
        else:
            patch_applied = False
            last_error_feedback = verify_log
            for m in re.findall(r"((?:src|tests)/[A-Za-z0-9_./-]+\.(?:ts|tsx))", verify_log):
                cascading = f"frontend/{m}"
                if (REPO_ROOT / cascading).exists() and cascading not in affected_files:
                    affected_files.append(cascading)
                    affected_files.sort()
                    file_count = len(affected_files)
                    print(f"   ➕ Added cascading TypeScript file to affected_files: {cascading}")
            if attempt < MAX_RETRIES:
                print(f"\n⚠️ Verification FAILED on attempt {attempt}. Looping back to Repair Agent...")
            else:
                print(f"\n❌ Max retries ({MAX_RETRIES}) reached. Setting patch_applied = False.")

    if not patch_applied:
        patch_description = f"Failed to produce a passing patch across {file_count} files after {MAX_RETRIES} attempts."

    if TMP_PROMPT_DIR.exists():
        try:
            TMP_PROMPT_DIR.rmdir()
        except OSError:
            pass

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

    out_file = pathlib.Path(output_path)
    out_file.write_text(json.dumps(final_report, indent=2), encoding="utf-8")
    print(f"🎉 Pipeline complete! Backup report written to:\n{out_file}")
    return final_report


def _reset_frontend_code_keep_mocks() -> None:
    """Resets frontend source/test files to HEAD while preserving mocks/impact-reports/*.json."""
    mocks_dir = REPO_ROOT / "frontend/src/mocks/impact-reports"
    saved_mocks: dict[pathlib.Path, str] = {}
    if mocks_dir.exists():
        for mf in mocks_dir.glob("*.json"):
            saved_mocks[mf] = mf.read_text(encoding="utf-8")

    subprocess.run(["git", "checkout", "--", "frontend/"], check=False, cwd=REPO_ROOT)

    for mf, content in saved_mocks.items():
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_text(content, encoding="utf-8")


def run_all_branches() -> None:
    """Runs all 3 demo branches sequentially, resetting frontend source files between runs."""
    branches = [
        (
            "Branch A — field_renamed",
            BASE_DIR / "reports/diff_report_field_renamed.json",
            BASE_DIR / "reports/impact_report_field_renamed.json",
        ),
        (
            "Branch B — field_type_changed",
            BASE_DIR / "reports/diff_report_type_changed.json",
            BASE_DIR / "reports/impact_report_type_changed.json",
        ),
        (
            "Branch C — endpoint_removed",
            BASE_DIR / "reports/diff_report_endpoint_removed.json",
            BASE_DIR / "reports/impact_report_endpoint_removed.json",
        ),
    ]

    results: list[tuple[str, dict]] = []
    for title, diff_path, out_path in branches:
        print("\n" + "=" * 80)
        print(f"🚀 STARTING {title}")
        print("=" * 80)
        _reset_frontend_code_keep_mocks()
        report = run_orchestrator(diff_path, out_path)
        results.append((title, report))
        print(f"\n📋 Result for {title}:")
        print(json.dumps(report, indent=2))

    _reset_frontend_code_keep_mocks()

    print("\n" + "=" * 80)
    print("🏆 CONTRACTGUARD — ALL 3 BRANCHES DEMO SUMMARY")
    print("=" * 80)
    for title, r in results:
        status_icon = "✅ PASS" if r.get("verify_status") == "pass" else "❌ FAIL"
        print(f"\n🔹 {title}")
        print(f"   • Change Type     : {r.get('change_type')} ({r.get('method')} {r.get('endpoint')})")
        print(f"   • Affected Files  : {len(r.get('affected_files', []))} files -> {r.get('affected_files')}")
        print(f"   • Patch Applied   : {r.get('patch_applied')}")
        print(f"   • Verify Status   : {status_icon}")
        print(f"   • Summary         : {r.get('summary_plain_english')}")
        print(f"   • Patch Details   : {r.get('patch_description')}")
        print(f"   • Verify Log      : {r.get('verify_log')}")
    print("\n" + "=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ContractGuard Subagent Orchestrator")
    parser.add_argument(
        "diff_file",
        help="Path to a diff_report_*.json file, OR 'all' to run all 3 branches sequentially.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Path to write impact_report.json (used when running a single diff_file).",
    )
    args = parser.parse_args()
    if args.diff_file.strip().lower() in ("all", "--all"):
        run_all_branches()
    else:
        report = run_orchestrator(args.diff_file, args.output)
        print("\n📋 Final Impact Report JSON:")
        print(json.dumps(report, indent=2))