"""Acceptance of Windows BAT and Windows Unicode path compatibility."""
import os
from pathlib import Path


def pytest_ignore_collect(collection_path, config):
    if os.name != "nt" and Path(collection_path).is_relative_to(Path(__file__).parent):
        return True
    return None
