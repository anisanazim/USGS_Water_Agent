#!/usr/bin/env python
"""Automated E2E Test Runner and Allure Report Generator.

Executes all natural language queries defined in tests/test_queries.json
against the USGS Water Data Agent, captures all traces, logs, and artifacts,
and automatically compiles a rich interactive Allure HTML report.

Usage:
    python run_allure_suite.py
    python run_allure_suite.py --open
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
ALLURE_RESULTS = ROOT_DIR / "allure-results"
ALLURE_REPORT = ROOT_DIR / "allure-report"
PYTHON_EXE = sys.executable


def run_command(cmd, cwd=ROOT_DIR):
    print(f"\n[RUN] {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Run USGS Water Agent E2E suite and generate Allure report.")
    parser.add_argument("--open", action="store_true", help="Automatically open Allure report in default browser after generation.")
    parser.add_argument("--query-id", type=str, default=None, help="Optionally run only a specific query ID (e.g. spatial_chlorophyll_wake_nc).")
    args = parser.parse_args()

    # 1. Clean previous results if desired
    if ALLURE_RESULTS.exists():
        shutil.rmtree(ALLURE_RESULTS, ignore_errors=True)
    ALLURE_RESULTS.mkdir(parents=True, exist_ok=True)

    # 2. Run Pytest with Allure listener
    pytest_cmd = [
        PYTHON_EXE,
        "-m",
        "pytest",
        "tests/test_e2e_queries.py",
        f"--alluredir={ALLURE_RESULTS}",
        "-v",
        "-s",
    ]
    if args.query_id:
        pytest_cmd.extend(["-k", args.query_id])

    pytest_code = run_command(pytest_cmd)

    # 3. Locate Allure CLI
    allure_bin = shutil.which("allure")
    if not allure_bin:
        # Check standard installation path on Windows
        candidate = Path("C:/allure-2.38.1/bin/allure.bat")
        if candidate.exists():
            allure_bin = str(candidate)

    if not allure_bin:
        print("\n[WARNING] Allure CLI binary ('allure') not found on PATH.")
        print("Raw test results are saved in: ./allure-results")
        print("Install allure or add it to PATH to compile HTML report.")
        sys.exit(pytest_code)

    # 4. Generate Allure HTML Report
    print("\n[INFO] Compiling interactive Allure HTML report...")
    gen_cmd = [allure_bin, "generate", str(ALLURE_RESULTS), "-o", str(ALLURE_REPORT), "--clean"]
    gen_code = run_command(gen_cmd)

    if gen_code == 0:
        print("\n==================================================================")
        print(" SUCCESS: Allure Report generated successfully!")
        print(f" Report Location: {ALLURE_REPORT.resolve()}")
        print(" To view the report in your browser, run:")
        print(f"     allure open {ALLURE_REPORT}")
        print("==================================================================")
        if args.open:
            print("\n[INFO] Opening Allure report in browser...")
            run_command([allure_bin, "open", str(ALLURE_REPORT)])
    else:
        print(f"\n[ERROR] Failed to compile Allure report (code: {gen_code}).")

    sys.exit(pytest_code)


if __name__ == "__main__":
    main()
