# Numerical method interface (reserved)

This phase does not select governing equations, spatial discretization,
numerical flux, limiter, dry/wet-cell algorithm, or time integrator.

`numerics.scheme`, `numerics.flux`, and `numerics.time_integrator` are optional
non-empty strings. They are placeholders that allow a future engine to expose
method selection without changing the case shape. `null` in the example means
“not selected yet”. No value is interpreted by the current Python package.

The general controls have schema-level sanity checks only:

- `cfl`: dimensionless number in `(0, 1]`;
- `min_depth`: non-negative metres;
- `dry_depth`: non-negative metres and no larger than `min_depth`.

These checks do not define a physical dry-state algorithm. The later numerical
design must document which methods accept these controls and whether their
legal ranges differ.

## Terrain mapping boundary

DEM-to-grid mapping is a data-preparation concern, not a numerical Solver choice.
Round 5 implements pure in-memory `DEMDataset` → `TerrainField` mapping via
`area_weighted_mean`, `direct`, `bilinear`, and `auto`. The mapper supplies
`terrain_elevation[j, i]` to a future solver but does not choose or implement any
equation, flux, limiter, or time integrator. CRS reprojection and NoData fill
strategies remain outside the current scope.

## DEM data contract boundary

DEM 元数据校验和 TerrainField 质量信息属于数据准备前置层，不是数值离散或
Solver 选择。`DEMMetadata.vertical_datum` 未确认时不得猜测或转换；模型可用
`vertical_datum_required` 表达运行前置要求。
