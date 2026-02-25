from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

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


def _drop_schema(schema_name: str) -> None:
    """Drop a Postgres schema used for sandbox validation."""
    try:
        import psycopg2
        conn = psycopg2.connect(settings.database_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        cur.close()
        conn.close()
    except Exception:
        pass  # Best-effort cleanup


def validate_patch_with_dbt(
    patch_sql: str,
    model_path: str,
    model_id: str,
) -> dict[str, Any]:
    """Validate a patch by running dbt in a fully isolated sandbox.

    Uses a unique Postgres schema per validation run so that:
    - Views/tables are created fresh from the patched SQL
    - Tests run against the actual patched models, not stale views
    - No interference with the main dbt project or other runs

    Steps:
    1. Copy the dbt project to a temp directory
    2. Generate a unique schema and write a sandboxed profiles.yml
    3. Apply the patch to the target model file
    4. Run dbt seed (populate reference data)
    5. Run dbt run --select <model>+ (materialize patched model + downstream)
    6. Run dbt test --select <model> (test the patched model)
    7. Drop the sandbox schema and clean up
    """
    if not settings.dbt_project_dir:
        return {
            "dbt_compile": "skip",
            "dbt_test": "skip",
            "dbt_compile_output": "DBT_PROJECT_DIR not configured",
            "dbt_test_output": "",
        }

    profiles_dir = settings.dbt_profiles_dir or settings.dbt_project_dir
    sandbox_schema = f"dra_sandbox_{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory(prefix="dra_dbt_") as tmpdir:
        sandbox_dir = Path(tmpdir) / "project"
        shutil.copytree(settings.dbt_project_dir, sandbox_dir, dirs_exist_ok=True)

        # Read the original profiles.yml and create a sandboxed version with unique schema
        orig_profiles_path = Path(profiles_dir) / "profiles.yml"
        if orig_profiles_path.exists():
            with open(orig_profiles_path) as f:
                profiles = yaml.safe_load(f)
            # Replace schema in all targets
            for profile_name in profiles:
                if isinstance(profiles[profile_name], dict) and "outputs" in profiles[profile_name]:
                    for target_name in profiles[profile_name]["outputs"]:
                        profiles[profile_name]["outputs"][target_name]["schema"] = sandbox_schema
            sandbox_profiles_dir = str(sandbox_dir)
            with open(sandbox_dir / "profiles.yml", "w") as f:
                yaml.dump(profiles, f, default_flow_style=False)
        else:
            sandbox_profiles_dir = profiles_dir

        # Extract the short model name from unique_id for --select
        select_name = model_id.split(".")[-1] if "." in model_id else model_id

        try:
            # dbt seed — populate reference data in the sandbox schema
            seed_result = _run_dbt_command(
                ["dbt", "seed", "--no-version-check"],
                str(sandbox_dir),
                sandbox_profiles_dir,
            )
            if seed_result["returncode"] != 0:
                return {
                    "dbt_compile": "fail",
                    "dbt_test": "skip",
                    "dbt_compile_output": f"dbt seed failed: {seed_result.get('stderr', '')[:1000]}",
                    "dbt_test_output": "",
                }

            # dbt run ALL models first to establish baseline views/tables
            # (needed so downstream models can reference sibling dependencies)
            baseline_result = _run_dbt_command(
                ["dbt", "run", "--no-version-check"],
                str(sandbox_dir),
                sandbox_profiles_dir,
            )
            if baseline_result["returncode"] != 0:
                return {
                    "dbt_compile": "fail",
                    "dbt_test": "skip",
                    "dbt_compile_output": f"dbt baseline run failed: {baseline_result.get('stdout', '')[-1000:]}",
                    "dbt_test_output": "",
                }

            # Now apply the patch on top of the baseline
            target_file = sandbox_dir / model_path
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(patch_sql)

            # dbt run — re-materialize the patched model and its downstream dependents
            run_result = _run_dbt_command(
                ["dbt", "run", "--select", f"{select_name}+", "--no-version-check"],
                str(sandbox_dir),
                sandbox_profiles_dir,
            )
            compile_ok = run_result["returncode"] == 0

            # dbt test (only if run passed)
            test_ok = False
            test_result: dict[str, Any] = {"stdout": "", "stderr": ""}
            if compile_ok:
                test_result = _run_dbt_command(
                    ["dbt", "test", "--select", select_name, "--no-version-check"],
                    str(sandbox_dir),
                    sandbox_profiles_dir,
                )
                test_ok = test_result["returncode"] == 0
        finally:
            # Always clean up the sandbox schema
            _drop_schema(sandbox_schema)

    return {
        "dbt_compile": "pass" if compile_ok else "fail",
        "dbt_test": "pass" if test_ok else ("fail" if compile_ok else "skip"),
        "dbt_compile_output": run_result.get("stderr", "")[:2000],
        "dbt_test_output": test_result.get("stderr", "")[:2000],
    }
