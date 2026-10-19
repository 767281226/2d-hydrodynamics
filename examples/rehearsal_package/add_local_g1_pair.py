"""Create a local G1 pair for the tiny example result; no database is used."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hydrodynamics.gate_dispatch import GateDispatchInput, export_gate_dispatch_for_netcdf
from hydrodynamics.triangular_solver_contract import validate_triangular_run_request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-config", required=True)
    parser.add_argument("--assignment", required=True)
    args = parser.parse_args()
    run_config = Path(args.run_config).resolve()
    assignment = json.loads(Path(args.assignment).read_text(encoding="utf-8"))
    capability = validate_triangular_run_request(run_config, allow_existing_result=True)
    csv_path, json_path = export_gate_dispatch_for_netcdf(
        capability.result_path, GateDispatchInput(), script_id=assignment["script_id"],
    )
    print(json.dumps({"status": "local_g1_pair_created", "csv": str(csv_path), "json": str(json_path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
