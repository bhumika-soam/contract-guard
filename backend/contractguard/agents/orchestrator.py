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
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DEFAULT_DIFF_FILE = AGENTS_DIR / "sample_diff_report.json"
IMPACT_PROMPT_FILE = AGENTS_DIR / "impact_agent_prompt.md"
REPAIR_PROMPT_FILE = AGENTS_DIR / "repair_agent_prompt.md"
VERIFY_PROMPT_FILE = AGENTS_DIR / "verify_agent_prompt.md"
DEFAULT_OUTPUT_FILE = AGENTS_DIR / "impact_report.json"

MAX_RETRIES = 3
MAX_PARALLEL_SUBAGENTS = 4  # Bounds concurrent Bob CLI processes


def run_bob(prompt: str, mode: str, task_label: str = "main") -> str:
    """Thread-safe Bob CLI runner using a unique prompt file per subagent call."""
    bob_path = shutil.which("bob")
    if not bob_path:
        print("❌ Error: 'bob' CLI not found in PATH.")
        sys.exit(1)

    unique_id = f"{task_label}_{uuid.uuid4().hex[:6]}"
    prompt_file = AGENTS_DIR / f"_current_prompt_{unique_id}.md"
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
        # Clean up temporary prompt file so the agents folder stays clean
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


def find_candidate_frontend_files(old_field: str, endpoint: str) -> list[str]:
    """Finds candidate frontend files referencing the changed field or endpoint to scan in parallel."""
    frontend_src = REPO_ROOT / "frontend" / "src"
    if not frontend_src.exists():
        return []

    candidates = []
    search_token = old_field if old_field and old_field != "unknown" else endpoint.strip("/").split("/")[0]

    for file_path in sorted(frontend_src.rglob("*")):
        if not file_path.is_file() or file_path.suffix not in (".ts", ".tsx", ".js", ".jsx"):
            continue
        # Skip mock impact reports so the agent doesn't scan its own output files
        rel_posix = file_path.relative_to(REPO_ROOT).as_posix()
        if "mocks/impact_reports" in rel_posix:
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
            if search_token and search_token in content:
                candidates.append(rel_posix)
        except OSError:
            continue

    return candidates


def analyze_single_file_impact(
    rel_file: str, diff_data: dict, impact_base_prompt: str
) -> tuple[str, bool, str]:
    """Subagent worker: runs Impact Agent on a single candidate file concurrently."""
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

    # Default to True if Bob found usages in the candidate file
    is_affected = bool(parsed.get("is_affected", True))
    line_summary = parsed.get("line_summary", "")
    return rel_file, is_affected, f"### File: {rel_file}\n{line_summary}\n{output}"


def run_parallel_impact_agents(
    diff_data: dict, old_field: str, new_field: str
) -> tuple[list[str], str, str]:
    """
    Dispatches parallel Impact Agent subagents across candidate files,
    with an automatic fallback to the single bulk Impact Agent if needed.
    Returns (affected_files, summary_plain_english, combined_impact_output).
    """
    impact_base_prompt = IMPACT_PROMPT_FILE.read_text(encoding="utf-8")
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
                    file_path, is_affected, section_output = future.result()
                    if is_affected:
                        affected_files.append(file_path)
                        combined_sections.append(section_output)
                        print(f"   ✅ Subagent confirmed impact in: {file_path}")
                    else:
                        print(f"   ➖ Subagent cleared (not affected): {file_path}")
                except Exception as exc:
                    print(f"   ⚠️ Subagent error on {rel_file}: {exc}")

        # Keep affected_files in deterministic sorted order
        affected_files.sort()

        if affected_files:
            file_count = len(affected_files)
            summary_plain_english = (
                f"The '{old_field}' field was renamed to '{new_field}' on {endpoint} "
                f"across {file_count} files. Any frontend code reading .{old_field} will now get undefined."
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


def run_orchestrator(
    diff_input: str | pathlib.Path | dict = DEFAULT_DIFF_FILE,
    output_path: str | pathlib.Path = DEFAULT_OUTPUT_FILE,
) -> dict:
    """
    Runs Parallel Impact Subagents -> Repair Agent -> Verify Agent (with bounded retries)
    and writes impact_report.json + frontend mock files.
    """
    if isinstance(diff_input, dict):
        raw_data = diff_input
    else:
        diff_path = pathlib.Path(diff_input)
        if not diff_path.exists():
            print(f"❌ Missing diff report file: {diff_path}")
            sys.exit(1)
        raw_data = json.loads(diff_path.read_text(encoding="utf-8"))

    if isinstance(raw_data, list):
        raw_data = raw_data[0] if raw_data else {}

    diff_data: dict = raw_data if isinstance(raw_data, dict) else {}

    old_field = next(iter(diff_data.get("old_schema_fragment", {})), "unknown")
    new_field = next(iter(diff_data.get("new_schema_fragment", {})), "unknown")
    change_id = diff_data.get("change_id", f"{diff_data.get('change_type', 'change')}_{old_field}")

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

    # 2 & 3. Run Repair Agent + Verify Agent Loop (max 3 attempts)
    repair_base_prompt = REPAIR_PROMPT_FILE.read_text(encoding="utf-8")
    verify_base_prompt = VERIFY_PROMPT_FILE.read_text(encoding="utf-8")

    verify_status = "fail"
    verify_log = "Verification did not run."
    patch_applied = False
    patch_description = "No patch applied."
    last_error_feedback = ""

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n🛠️ Step 2 (Attempt {attempt}/{MAX_RETRIES}): Running Repair Agent (Agent mode)...")
        repair_prompt = (
            f"{repair_base_prompt}\n\n"
            f"Diff Report:\n{json.dumps(diff_data, indent=2)}\n\n"
            f"Affected files ({file_count} files total): {json.dumps(affected_files)}\n"
            f"Detailed findings from parallel Impact Subagents:\n{impact_output}\n\n"
            f"Update ONLY the {file_count} files listed above from '{old_field}' to '{new_field}'. "
            f"Do not touch any unrelated variables, formatting, or other lines in those files. "
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
            f"Updated {file_count} affected files to use '{new_field}' instead of '{old_field}'.",
        )

        # Run Verify Agent
        print(f"\n🧪 Step 3 (Attempt {attempt}/{MAX_RETRIES}): Running Verify Agent (Agent mode)...")
        verify_output = run_bob(verify_base_prompt, mode="agent", task_label=f"verify_{attempt}")
        print("\n--- Verify Agent Output ---")
        print(verify_output)

        verify_json = extract_last_json(verify_output)
        verify_status = str(verify_json.get("verify_status", "")).lower()
        verify_log = verify_json.get("verify_log", verify_output)

        if verify_status not in ("pass", "fail"):
            verify_status = "fail" if "error ts" in verify_output.lower() else "pass"

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
    }

    # Route directly to the matching frontend mock file based on change_type
    output_map = {
        "field_renamed": REPO_ROOT / "frontend/src/mocks/impact-reports/drift-field-renamed.json",
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
        nargs="?",
        default=str(DEFAULT_DIFF_FILE),
        help="Path to diff_report.json (defaults to sample_diff_report.json)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Path to write impact_report.json",
    )
    args = parser.parse_args()
    run_orchestrator(args.diff_file, args.output)
