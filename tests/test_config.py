from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from hydrodynamics.config import (
    ConfigValidationError,
    DomainConfig,
    NoDataStrategy,
    SimulationConfig,
    load_config,
)
from pydantic import ValidationError


EXAMPLE = Path(__file__).parents[1] / "examples" / "case_001" / "config.yaml"


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(EXAMPLE)
        self.raw = self.config.model_dump(mode="python")

    def test_complete_config_loads(self) -> None:
        self.assertEqual(self.config.model.name, "case_001")
        self.assertEqual(self.config.domain.dx, 50.0)
        self.assertEqual(self.config.domain.nx, 100)
        self.assertEqual(self.config.domain.ny, 100)
        self.assertEqual(len(self.config.boundary), 4)
        self.assertEqual(self.config.terrain.nodata_strategy, NoDataStrategy.ERROR)

    def test_domain_rejects_user_nx_ny(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["domain"]["nx"] = 100
        with self.assertRaises(ValidationError) as raised:
            SimulationConfig.model_validate(raw)
        self.assertTrue(any(error["loc"][-1] == "nx" for error in raised.exception.errors()))

    def test_domain_extent_must_be_divisible(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["domain"]["xmax"] = 4999
        with self.assertRaises(ValidationError) as raised:
            SimulationConfig.model_validate(raw)
        self.assertIn("evenly divisible", str(raised.exception))

    def test_domain_allows_different_dx_and_dy(self) -> None:
        config = DomainConfig(xmin=0, xmax=3000, ymin=0, ymax=4000, dx=50, dy=100)
        self.assertEqual(config.nx, 60)
        self.assertEqual(config.ny, 40)

    def test_nodata_strategy_error_is_valid(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["terrain"]["nodata_strategy"] = "error"
        config = SimulationConfig.model_validate(raw)
        self.assertEqual(config.terrain.nodata_strategy, NoDataStrategy.ERROR)

    def test_unknown_nodata_strategy_fails(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["terrain"]["nodata_strategy"] = "fill"
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)
    def test_missing_required_parameter_fails(self) -> None:
        raw = copy.deepcopy(self.raw)
        del raw["model"]["name"]
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)

    def test_unknown_enum_value_fails(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["terrain"]["type"] = "mesh"
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)

    def test_numeric_range_fails(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["numerics"]["cfl"] = 0
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)

    def test_unsupported_boundary_type_fails(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["boundary"][0]["type"] = "inlet"
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)

    def test_raster_terrain_requires_file(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["terrain"]["file"] = None
        with self.assertRaises(ValidationError):
            SimulationConfig.model_validate(raw)

    def test_loader_reports_readable_validation_error(self) -> None:
        raw = copy.deepcopy(self.raw)
        raw["terrain"]["file"] = None
        yaml_text = "\n".join(
            [
                "model:",
                "  name: case_001",
                "  end_time: 1",
                "  coordinate_system: EPSG:xxxx",
                "domain:",
                "  dx: 1",
                "  dy: 1",
                "terrain:",
                "  type: raster",
                "  file: null",
                "initial_condition:",
                "  type: constant_depth",
                "  depth: 0",
                "roughness:",
                "  type: constant",
                "  manning_n: 0.03",
                "boundary:",
                "  - id: wall",
                "    location: west",
                "    type: wall",
                "source:",
                "  rainfall:",
                "    enabled: false",
                "numerics: {}",
                "output: {}",
                "validation: {}",
            ]
        )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as stream:
            stream.write(yaml_text)
            path = Path(stream.name)
        try:
            with self.assertRaises(ConfigValidationError) as raised:
                load_config(path)
            self.assertIn("terrain", str(raised.exception))
            self.assertIn("file is required", str(raised.exception))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
