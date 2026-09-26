import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

AGENTS_DIR = pathlib.Path(__file__).resolve().parent
DIFF_REPORT_FILE = AGENTS_DIR / "sample_diff_report.json"
IMPACT_PROMPT_FILE = AGENTS_DIR / "impact_agent_prompt.md"
REPAIR_PROMPT_FILE = AGENTS_DIR / "repair_agent_prompt.md"
VERIFY_PROMPT_FILE = AGENTS_DIR / "verify_agent_prompt.md"
OUTPUT_REPORT_FILE = AGENTS_DIR / "impact_report.json"

MAX_RETRIES = 3


os.environ["BOB_API_KEY"] = os.environ.get("BOB_API_KEY", "")
def run_bob(prompt: str, mode: str) -> str:
    """Writes prompt to a file to bypass Windows CLI length limit, then runs Bob."""
    bob_path = shutil.which("bob")
    if not bob_path:
        print("❌ Error: 'bob' CLI not found in PATH.")
        sys.exit(1)

    prompt_file = AGENTS_DIR / "_current_prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    short_instruction = (
        f"Read the instructions in '{prompt_file.as_posix()}' and execute them completely."
    )

    cmd = [bob_path, "run", "--accept-license", "--mode", mode, short_instruction]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ.copy(),
    )

    if result.returncode != 0:
        print(f"\n❌ Bob CLI exited with code {result.returncode}")
        print("--- STDOUT ---")
        print(result.stdout)
        print("--- STDERR ---")
        print(result.stderr)
        sys.exit(1)

    return result.stdout.strip()


def extract_last_json(text: str) -> dict:
    """Extracts the last valid JSON object from Bob's CLI output, fixing 120-char line wraps."""
    # Only look at Assistant responses so we don't match example JSON in the User prompt
    if "Assistant (" in text:
        text = text.split("Assistant (")[-1]

    matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    for candidate in reversed(matches):
        # Collapse Bob CLI's 120-column terminal line-wrapping and extra spaces
        cleaned = re.sub(r"\s+", " ", candidate).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            continue
    return {}


def main():
    if not DIFF_REPORT_FILE.exists():
        print(f"❌ Missing {DIFF_REPORT_FILE}")
        sys.exit(1)

    diff_data = json.loads(DIFF_REPORT_FILE.read_text(encoding="utf-8"))
    old_field = next(iter(diff_data.get("old_schema_fragment", {})), "unknown")
    new_field = next(iter(diff_data.get("new_schema_fragment", {})), "unknown")
    change_id = diff_data.get("change_id", f"{diff_data.get('change_type', 'change')}_{old_field}")

    # 1. Run Impact Agent (Ask mode)
    print("🔍 Step 1: Running Impact Agent (Ask mode)...")
    impact_base_prompt = IMPACT_PROMPT_FILE.read_text(encoding="utf-8")
    impact_prompt = (
        f"{impact_base_prompt}\n\n"
        f"Diff Report JSON:\n{json.dumps(diff_data, indent=2)}\n\n"
        f"At the very end of your response, include a valid JSON block in this exact format:\n"
        f'{{"summary_plain_english": "...", "affected_files": ["frontend/src/..."]}}'
    )
    impact_output = run_bob(impact_prompt, mode="ask")
    print("\n--- Impact Agent Findings ---")
    print(impact_output)

    # Save findings to a file so Repair Agent can read them cleanly
    findings_file = AGENTS_DIR / "latest_impact_findings.txt"
    findings_file.write_text(impact_output, encoding="utf-8")

    impact_json = extract_last_json(impact_output)
    affected_files = impact_json.get("affected_files", [])
    summary_plain_english = impact_json.get(
        "summary_plain_english",
        f"The '{old_field}' field was renamed to '{new_field}' on {diff_data.get('endpoint')}. "
        f"Any frontend code reading .{old_field} will now get undefined.",
    )

    proceed = input("\nProceed to Repair Agent with these findings? (y/n): ")
    if proceed.lower() != "y":
        print("Aborted before applying repairs.")
        return

    # 2 & 3. Run Repair Agent + Verify Agent Loop (max 3 attempts)
    repair_base_prompt = REPAIR_PROMPT_FILE.read_text(encoding="utf-8")
    verify_base_prompt = (
        VERIFY_PROMPT_FILE.read_text(encoding="utf-8")
        if VERIFY_PROMPT_FILE.exists()
        else "Run `npx tsc --noEmit` in the frontend folder and return JSON: {\"verify_status\": \"pass\"|\"fail\", \"verify_log\": \"...\"}"
    )

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
            f"Affected files and lines found by Impact Agent:\n{impact_output}\n\n"
            f"Update each of those specific occurrences from '{old_field}' to '{new_field}'. "
            f"Do not touch any unrelated variables, formatting, or other lines in those files.\n"
            f'At the very end of your response, include a JSON block: {{"patch_description": "Brief summary of changes made"}}'
        )
        if last_error_feedback:
            repair_prompt += (
                f"\n\nWARNING: Previous repair attempt failed verification with these errors. "
                f"Fix them carefully:\n{last_error_feedback}"
            )

        repair_output = run_bob(repair_prompt, mode="agent")
        print("\n--- Repair Agent Output ---")
        print(repair_output)

        repair_json = extract_last_json(repair_output)
        patch_description = repair_json.get(
            "patch_description",
            f"Updated affected files to use '{new_field}' instead of '{old_field}'.",
        )

        # Run Verify Agent
        print(f"\n🧪 Step 3 (Attempt {attempt}/{MAX_RETRIES}): Running Verify Agent (Agent mode)...")
        verify_output = run_bob(verify_base_prompt, mode="agent")
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
        patch_description = f"Failed to produce a passing patch after {MAX_RETRIES} attempts."

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

    OUTPUT_REPORT_FILE.write_text(json.dumps(final_report, indent=2), encoding="utf-8")
    print(f"\n🎉 Pipeline complete! Final report written to:\n{OUTPUT_REPORT_FILE}")


if __name__ == "__main__":
    main()