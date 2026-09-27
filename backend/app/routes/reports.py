"""
Serves generated diff/impact reports to the frontend.
Mount path: backend/app/routes/reports.py
"""

import importlib.util
import json
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/reports", tags=["reports"])

# Paths are relative to backend/ (the project CWD when the server runs).
REPORTS_DIR = Path("contractguard/reports")
SNAPSHOTS_DIR = Path("openapi_snapshots")

# Load diff_agent from backend/contractguard/diff_agent.py without requiring
# an __init__.py (contractguard is not a package).
_DIFF_AGENT_PATH = Path(__file__).resolve().parent.parent.parent / "contractguard" / "diff_agent.py"
_spec = importlib.util.spec_from_file_location("contractguard.diff_agent", _DIFF_AGENT_PATH)
_diff_agent = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_diff_agent)  # type: ignore[union-attr]
sys.modules.setdefault("contractguard.diff_agent", _diff_agent)


@router.get("/")
def list_reports():
    """Summary list for the report viewer's landing page."""
    reports = []
    for file in sorted(REPORTS_DIR.glob("impact_report_*.json")):
        data = json.loads(file.read_text())
        reports.append(
            {
                "change_id": data.get("change_id"),
                "severity": data.get("severity"),
                "verify_status": data.get("verify_status"),
            }
        )
    return reports


@router.post("/run/{change_id}")
def run_diff(change_id: str):
    """Run diff_agent against the stored snapshots for *change_id*.

    Reads openapi_snapshots/main.json (baseline) and
    openapi_snapshots/drift_{change_id}.json (drifted snapshot), writes the
    result to contractguard/reports/diff_report_{change_id}.json, and returns
    the list of detected changes as JSON.
    """
    baseline = SNAPSHOTS_DIR / "main.json"
    drift = SNAPSHOTS_DIR / f"drift_{change_id}.json"

    missing = [str(p) for p in (baseline, drift) if not p.exists()]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"Snapshot file(s) not found: {', '.join(missing)}",
        )

    old_spec = _diff_agent.load_spec(str(baseline))
    new_spec = _diff_agent.load_spec(str(drift))
    changes = _diff_agent.diff_specs(old_spec, new_spec)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REPORTS_DIR / f"diff_report_{change_id}.json"
    out_file.write_text(json.dumps(changes, indent=2))

    return changes


@router.get("/{change_id}")
def get_report(change_id: str):
    """Full detail for one scenario, e.g. GET /api/reports/field_renamed"""
    file = REPORTS_DIR / f"impact_report_{change_id}.json"
    if not file.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return json.loads(file.read_text())
