import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hydrodynamics import restricted_worker


@pytest.mark.parametrize("encoding", ["gbk", "utf-8"])
@pytest.mark.parametrize("exit_code", [0, 53, 54])
def test_cli_summary_preserves_unicode_status_and_original_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
    exit_code: int,
) -> None:
    message = "DomainMeshOnlyError: D−S boundary failed — 保留原始诊断"
    status = "mesh_generation_diagnostic_complete" if exit_code == 0 else "worker_failed"
    result = SimpleNamespace(
        run_id="synthetic-attempt",
        status=status,
        exit_code=exit_code,
        worker_started=True,
        bundle_path=Path("out/synthetic"),
        diagnostic_path=None,
        message=message,
    )
    monkeypatch.setattr(restricted_worker, "run_restricted_preparation", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hydrodynamics.restricted_worker", "--run-config", "config.json", "--project-root", "project"],
    )
    byte_stream = io.BytesIO()
    text_stream = io.TextIOWrapper(byte_stream, encoding=encoding)
    monkeypatch.setattr(sys, "stdout", text_stream)

    returned = restricted_worker.main()
    text_stream.flush()
    output = byte_stream.getvalue()

    assert output.isascii()
    assert returned == exit_code
    payload = json.loads(output.decode(encoding))
    assert payload == {
        "bundle_path": str(Path("out/synthetic")),
        "diagnostic_path": None,
        "exit_code": exit_code,
        "message": message,
        "run_id": "synthetic-attempt",
        "status": status,
        "worker_started": True,
    }
