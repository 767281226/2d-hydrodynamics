# Development guide

## Environment

Python 3.11 or newer is supported. Install the package and its small runtime
dependency from the repository root:

```bash
python -m pip install -e .
```

Install the optional test dependency when using pytest:

```bash
python -m pip install -e ".[test]"
```

## Load a case from Python

```python
from hydrodynamics.config import load_config

config = load_config("examples/case_001/config.yaml")
print(config.model.name)
print(config.output.variables)
```

The returned `SimulationConfig` is a typed Pydantic object. It can be passed
to a future engine without coupling that engine to YAML parsing. Referenced
files are not opened yet.

## Checks

```bash
python -m compileall hydrodynamics
python -m pytest
```

Tests cover valid loading, missing fields, invalid enum values, numeric ranges,
and type-specific terrain/boundary rules. Numerical results are out of scope.
