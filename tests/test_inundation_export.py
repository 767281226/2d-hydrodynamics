import numpy as np
import pytest

from hydrodynamics.inundation_export import write_grid_shapefile, write_wet_grid_shapefile
from hydrodynamics.vector_mask import VectorMaskError


@pytest.mark.parametrize("writer_name", ["grid", "wet"])
def test_structured_inundation_writers_refuse_before_creating_target(tmp_path, writer_name):
    target = tmp_path / "new-output" / "layer.shp"
    if writer_name == "grid":
        call = lambda: write_grid_shapefile(
            target, mask=np.ones((2, 2), dtype=bool), xmin=0.0, ymin=0.0,
            dx=1.0, dy=1.0, crs="EPSG:32650",
        )
    else:
        call = lambda: write_wet_grid_shapefile(
            target, wet_mask=np.ones((2, 2), dtype=bool), max_depth=np.ones((2, 2)),
            xmin=0.0, ymin=0.0, dx=1.0, dy=1.0, crs="EPSG:32650",
        )
    with pytest.raises(VectorMaskError, match="native P4 result"):
        call()
    assert not target.parent.exists()
    assert not list(tmp_path.rglob("*.shp"))
    assert not list(tmp_path.rglob("*.shx"))
    assert not list(tmp_path.rglob("*.dbf"))


def test_structured_inundation_writer_preserves_preexisting_target(tmp_path):
    target = tmp_path / "existing.shp"
    target.write_bytes(b"user-owned historical bytes")
    with pytest.raises(VectorMaskError, match="native P4 result"):
        write_grid_shapefile(
            target, mask=np.ones((1, 1), dtype=bool), xmin=0.0, ymin=0.0,
            dx=1.0, dy=1.0, crs="EPSG:32650",
        )
    assert target.read_bytes() == b"user-owned historical bytes"
    assert not target.with_suffix(".shx").exists()
    assert not target.with_suffix(".dbf").exists()
    assert not target.with_suffix(".prj").exists()
    assert not target.with_suffix(".cpg").exists()
