"""
Validate & repair Seed3D-generated USD assets via static rule pipelines.

Pipeline:
    1. Unzip the generated_3d_*.zip into a per-request workspace
    2. Locate the USD entry file (default: pbr/mesh_textured_pbr.usd)
    3. Static validation via omni-asset-cli
    4. Static repair via usd_simready_cli.py
    5. Merge both reports into pipeline.json

The validator and repairer CLIs are required dependencies. If either
binary or the reference json is missing the pipeline fails fast — there
is no fallback stub mode.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# 路径常量（相对当前文件，便于本地/线上同套代码）
# ---------------------------------------------------------------------------
LOCAL_BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = LOCAL_BASE_DIR.parent

DEFAULT_OMNI_ASSET_CLI = REPO_ROOT / "omni-asset-cli" / "omni_asset_cli.py"
DEFAULT_SIMREADY_CLI = REPO_ROOT / "usd-simready-inspector" / "usd_simready_cli.py"
DEFAULT_REF_JSON = (
    REPO_ROOT
    / "usd-simready-inspector"
    / "simready_furniture_reference_with_wikidata.json"
)
DEFAULT_WORK_DIR = LOCAL_BASE_DIR / "workspace"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class StageResult:
    name: str
    status: str  # "ok" | "warn" | "skipped" | "failed"
    duration_s: float = 0.0
    artifact: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineReport:
    request_id: str
    input_zip: str
    work_dir: str
    usd_entry: str | None = None
    output_usdc: str | None = None
    overall_status: str = "ok"
    stages: list[StageResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "input_zip": self.input_zip,
            "work_dir": self.work_dir,
            "usd_entry": self.usd_entry,
            "output_usdc": self.output_usdc,
            "overall_status": self.overall_status,
            "stages": [asdict(s) for s in self.stages],
            "summary": self.summary,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _find_usd_entry(extracted_dir: Path) -> Path | None:
    """
    Seed3D 解压后的典型布局是 pbr/mesh_textured_pbr.usd，
    这里做一个稳健的回退：优先找 mesh_textured_pbr.usd，再退到第一个 .usd[ac]。
    """
    preferred = list(extracted_dir.rglob("mesh_textured_pbr.usd"))
    if preferred:
        return preferred[0]
    for ext in ("*.usdc", "*.usd", "*.usda"):
        hits = list(extracted_dir.rglob(ext))
        if hits:
            return hits[0]
    return None


def _run_subprocess(cmd: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(cmd, check=False, text=True, capture_output=True)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


# ---------------------------------------------------------------------------
# Stage 1: unzip
# ---------------------------------------------------------------------------
def stage_unzip(zip_path: Path, dest_dir: Path) -> StageResult:
    started = time.time()
    try:
        if not zip_path.exists():
            return StageResult(
                name="unzip",
                status="failed",
                duration_s=time.time() - started,
                error=f"zip not found: {zip_path}",
            )
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest_dir)
        return StageResult(
            name="unzip",
            status="ok",
            duration_s=time.time() - started,
            artifact=str(dest_dir),
        )
    except Exception as e:
        return StageResult(
            name="unzip",
            status="failed",
            duration_s=time.time() - started,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Stage 2: omni-asset-cli validate
# ---------------------------------------------------------------------------
def stage_validate(
    usd_entry: Path,
    out_json: Path,
    omni_asset_cli: Path,
    profile: str = "stage1-furniture",
) -> StageResult:
    started = time.time()
    out_json.parent.mkdir(parents=True, exist_ok=True)

    if not omni_asset_cli.exists():
        return StageResult(
            name="validate",
            status="failed",
            duration_s=time.time() - started,
            error=f"omni-asset-cli not found at {omni_asset_cli}",
        )

    cmd = [
        sys.executable,
        str(omni_asset_cli),
        "validate",
        str(usd_entry),
        "--profile",
        profile,
        "--output-json",
        str(out_json),
    ]
    rc, stdout, stderr = _run_subprocess(cmd)
    if rc != 0:
        return StageResult(
            name="validate",
            status="failed",
            duration_s=time.time() - started,
            artifact=str(out_json) if out_json.exists() else None,
            error=(stderr or stdout).strip()[:500],
        )

    try:
        report = json.loads(out_json.read_text(encoding="utf-8"))
        issues = report.get("issues") or []
        is_valid = (report.get("summary") or {}).get("is_valid", True)
        return StageResult(
            name="validate",
            status="ok" if is_valid else "warn",
            duration_s=time.time() - started,
            artifact=str(out_json),
            extra={"issue_count": len(issues), "is_valid": is_valid},
        )
    except Exception as e:
        return StageResult(
            name="validate",
            status="warn",
            duration_s=time.time() - started,
            artifact=str(out_json),
            error=f"failed to parse report: {e}",
        )


# ---------------------------------------------------------------------------
# Stage 3: usd_simready_cli process
# ---------------------------------------------------------------------------
def stage_simready_process(
    usd_entry: Path,
    output_usdc: Path,
    report_json: Path,
    simready_cli: Path,
    ref_json: Path,
    omni_asset_cli: Path | None = None,
) -> StageResult:
    started = time.time()
    output_usdc.parent.mkdir(parents=True, exist_ok=True)
    report_json.parent.mkdir(parents=True, exist_ok=True)

    if not simready_cli.exists():
        return StageResult(
            name="simready_process",
            status="failed",
            duration_s=time.time() - started,
            error=f"usd_simready_cli not found at {simready_cli}",
        )
    if not ref_json.exists():
        return StageResult(
            name="simready_process",
            status="failed",
            duration_s=time.time() - started,
            error=f"reference json not found at {ref_json}",
        )

    cmd = [
        sys.executable,
        str(simready_cli),
        "process",
        str(ref_json),
        str(usd_entry),
        "--output",
        str(output_usdc),
        "--output-format",
        "usdc",
        "--report-output",
        str(report_json),
        "--emit-report",
        "--allow-mesh-defects",
        "--allow-missing-assets",
        "--author-rigid-body",
        "--rigid-body-mode",
        "dynamic",
    ]
    if omni_asset_cli and Path(omni_asset_cli).exists():
        cmd.extend(["--omni-asset-cli", str(omni_asset_cli)])
    else:
        cmd.append("--skip-mesh-preflight")
    rc, stdout, stderr = _run_subprocess(cmd)
    if rc != 0:
        return StageResult(
            name="simready_process",
            status="failed",
            duration_s=time.time() - started,
            artifact=str(output_usdc) if output_usdc.exists() else None,
            error=(stderr or stdout).strip()[:500],
        )

    extra: dict[str, Any] = {"report": str(report_json)}
    try:
        if report_json.exists():
            r = json.loads(report_json.read_text(encoding="utf-8"))
            extra["up_axis"] = r.get("stage", {}).get("up_axis")
            extra["bbox_size"] = (
                r.get("geometry", {}).get("bbox", {}).get("world", {}).get("size")
            )
            extra["missing_relative_count"] = r.get("asset_dependencies", {}).get(
                "missing_relative_count"
            )
            extra["review_required"] = r.get("review_required", False)
    except Exception as e:
        extra["report_parse_error"] = str(e)

    return StageResult(
        name="simready_process",
        status="ok",
        duration_s=time.time() - started,
        artifact=str(output_usdc),
        extra=extra,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def validate_and_repair(
    zip_path: Path,
    request_id: str,
    work_dir: Path = DEFAULT_WORK_DIR,
    omni_asset_cli: Path = DEFAULT_OMNI_ASSET_CLI,
    simready_cli: Path = DEFAULT_SIMREADY_CLI,
    ref_json: Path = DEFAULT_REF_JSON,
) -> PipelineReport:
    """
    Run the full validate+repair pipeline against a Seed3D zip.
    Returns a PipelineReport that the caller can serialize.
    """
    request_dir = work_dir / request_id
    extracted_dir = request_dir / "extracted"
    output_usdc = request_dir / f"{request_id}.simready_static.usdc"
    validate_json = request_dir / "validate_report.json"
    simready_report_json = request_dir / f"{request_id}.simready_static.report.json"
    pipeline_json = request_dir / "pipeline.json"

    report = PipelineReport(
        request_id=request_id,
        input_zip=str(zip_path),
        work_dir=str(request_dir),
    )

    # Stage 1: unzip
    s1 = stage_unzip(zip_path, extracted_dir)
    report.stages.append(s1)
    if s1.status == "failed":
        report.overall_status = "failed"
        report.finished_at = time.time()
        request_dir.mkdir(parents=True, exist_ok=True)
        pipeline_json.write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        return report

    # Stage 2: locate entry
    usd_entry = _find_usd_entry(extracted_dir)
    if usd_entry is None:
        report.stages.append(
            StageResult(
                name="locate_entry",
                status="failed",
                error="No USD file found in extracted archive",
            )
        )
        report.overall_status = "failed"
        report.finished_at = time.time()
        pipeline_json.write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8"
        )
        return report
    report.usd_entry = str(usd_entry)
    report.stages.append(
        StageResult(name="locate_entry", status="ok", artifact=str(usd_entry))
    )

    # Stage 3: validate
    s_validate = stage_validate(
        usd_entry,
        validate_json,
        omni_asset_cli=omni_asset_cli,
    )
    report.stages.append(s_validate)

    # Stage 4: simready process
    s_repair = stage_simready_process(
        usd_entry,
        output_usdc,
        simready_report_json,
        simready_cli=simready_cli,
        ref_json=ref_json,
        omni_asset_cli=omni_asset_cli,
    )
    report.stages.append(s_repair)
    if s_repair.status in ("ok", "skipped") and output_usdc.exists():
        report.output_usdc = str(output_usdc)

    # 汇总
    statuses = [s.status for s in report.stages]
    if "failed" in statuses:
        report.overall_status = "failed"
    elif "warn" in statuses:
        report.overall_status = "warn"
    else:
        report.overall_status = "ok"

    report.summary = {
        "validate_issue_count": s_validate.extra.get("issue_count"),
        "validate_is_valid": s_validate.extra.get("is_valid"),
        "up_axis": s_repair.extra.get("up_axis"),
        "bbox_size": s_repair.extra.get("bbox_size"),
        "missing_relative_count": s_repair.extra.get("missing_relative_count"),
        "review_required": s_repair.extra.get("review_required"),
    }
    report.finished_at = time.time()

    pipeline_json.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate & repair a Seed3D-generated USD zip via static rule pipelines."
    )
    parser.add_argument("zip_path", help="Path to generated_3d_*.zip (or any USD zip).")
    parser.add_argument(
        "--request-id",
        default=None,
        help="Request id used as workspace subdir & output basename. "
        "Defaults to the zip stem.",
    )
    parser.add_argument(
        "--work-dir",
        default=str(DEFAULT_WORK_DIR),
        help=f"Workspace root for extracted/processed assets. Default: {DEFAULT_WORK_DIR}",
    )
    parser.add_argument(
        "--omni-asset-cli",
        default=str(DEFAULT_OMNI_ASSET_CLI),
        help="Path to omni-asset-cli/omni_asset_cli.py.",
    )
    parser.add_argument(
        "--simready-cli",
        default=str(DEFAULT_SIMREADY_CLI),
        help="Path to usd-simready-inspector/usd_simready_cli.py.",
    )
    parser.add_argument(
        "--ref-json",
        default=str(DEFAULT_REF_JSON),
        help="Path to simready_furniture_reference_with_wikidata.json.",
    )
    args = parser.parse_args()

    zip_path = Path(args.zip_path).resolve()
    request_id = args.request_id or zip_path.stem
    work_dir = Path(args.work_dir).resolve()

    report = validate_and_repair(
        zip_path=zip_path,
        request_id=request_id,
        work_dir=work_dir,
        omni_asset_cli=Path(args.omni_asset_cli),
        simready_cli=Path(args.simready_cli),
        ref_json=Path(args.ref_json),
    )

    print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))

    if report.overall_status == "failed":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
