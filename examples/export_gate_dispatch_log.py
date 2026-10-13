"""Export a gate journal beside an existing result without recalculating fields.

From the repository root:
    python examples/export_gate_dispatch_log.py --result path/to/result.nc
    python examples/export_gate_dispatch_log.py --result path/to/result.nc --input dispatch.json
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hydrodynamics.gate_dispatch import GateDispatchInput, export_gate_dispatch_for_netcdf


def main() -> None:
    parser = argparse.ArgumentParser(description="输出结果文件对应的调闸记录")
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="站点调度快照及实际调闸记录 JSON；省略时记录判断缺项")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    inputs = GateDispatchInput.model_validate_json(args.input.read_text(encoding="utf-8-sig")) if args.input else None
    for path in export_gate_dispatch_for_netcdf(args.result, inputs, overwrite=args.overwrite):
        print(path)


if __name__ == "__main__":
    main()
