import json
from pathlib import Path
import subprocess
import sys
import zipfile

from setuptools.build_meta import build_wheel

from hydrodynamics.triangular_shp_export import package_schema, source_package_schema


def test_v2_shp_schema_is_packaged_and_wheel_installs(tmp_path):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel_path = wheelhouse / build_wheel(str(wheelhouse))

    with zipfile.ZipFile(wheel_path) as archive:
        names = set(archive.namelist())
        assert "hydrodynamics/triangular_shp_export.py" in names
        v1_path = "hydrodynamics/shapefile_schemas/triangular_result_shp.schema.json"
        v2_path = "hydrodynamics/shapefile_schemas/triangular_source_result_shp_v2.schema.json"
        assert v1_path in names and v2_path in names
        assert json.loads(archive.read(v1_path)) == package_schema()
        assert json.loads(archive.read(v2_path)) == source_package_schema()
        entry_points = next(name for name in names if name.endswith("entry_points.txt"))
        assert "hydrodynamics-triangle-shp = hydrodynamics.triangular_shp_export:main" in archive.read(entry_points).decode("utf-8")

    install_target = tmp_path / "installed"
    install_target.mkdir()
    subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-m", "pip", "install", "--no-deps", "--target", str(install_target), str(wheel_path)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    assert (install_target / "hydrodynamics" / "shapefile_schemas" / "triangular_result_shp.schema.json").is_file()
    assert (install_target / "hydrodynamics" / "shapefile_schemas" / "triangular_source_result_shp_v2.schema.json").is_file()
