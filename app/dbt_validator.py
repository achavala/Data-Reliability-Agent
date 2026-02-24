from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings


def _run_dbt_command(cmd: list[str], project_dir: str, profiles_dir: str) -> dict[str, Any]:
    """Run a dbt CLI command in subprocess and capture output."""
    env = os.environ.copy()
    env["DBT_PROFILES_DIR"] = profiles_dir

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "dbt command timed out after 120 seconds",
        }
    except FileNotFoundError:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "dbt CLI not found. Ensure dbt is installed and on PATH.",
        }


def validate_patch_with_dbt(
    patch_sql: str,
    model_path: str,
    model_id: str,
) -> dict[str, Any]:
    """Validate a patch by running dbt compile and dbt test in a sandboxed copy.

    1. Copy the dbt project to a temp directory
    2. Apply the patch to the target model file
    3. Run dbt compile --select <model>
    4. Run dbt test --select <model> (only if compile passes)
    5. Return results and clean up
    """
    if not settings.dbt_project_dir:
        return {
            "dbt_compile": "skip",
            "dbt_test": "skip",
            "dbt_compile_output": "DBT_PROJECT_DIR not configured",
            "dbt_test_output": "",
        }

    profiles_dir = settings.dbt_profiles_dir or settings.dbt_project_dir

    with tempfile.TemporaryDirectory(prefix="dra_dbt_") as tmpdir:
        sandbox_dir = Path(tmpdir) / "project"
        shutil.copytree(settings.dbt_project_dir, sandbox_dir, dirs_exist_ok=True)

        # Apply the patch
        target_file = sandbox_dir / model_path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(patch_sql)

        # Extract the short model name from unique_id for --select
        # e.g. "model.analytics.orders" -> "orders"
        select_name = model_id.split(".")[-1] if "." in model_id else model_id

        # dbt compile
        compile_result = _run_dbt_command(
            ["dbt", "compile", "--select", select_name, "--no-version-check"],
            str(sandbox_dir),
            profiles_dir,
        )
        compile_ok = compile_result["returncode"] == 0

        # dbt test (only if compile passed)
        test_ok = False
        test_result: dict[str, Any] = {"stdout": "", "stderr": ""}
        if compile_ok:
            test_result = _run_dbt_command(
                ["dbt", "test", "--select", select_name, "--no-version-check"],
                str(sandbox_dir),
                profiles_dir,
            )
            test_ok = test_result["returncode"] == 0

    return {
        "dbt_compile": "pass" if compile_ok else "fail",
        "dbt_test": "pass" if test_ok else ("fail" if compile_ok else "skip"),
        "dbt_compile_output": compile_result.get("stderr", "")[:2000],
        "dbt_test_output": test_result.get("stderr", "")[:2000],
    }
