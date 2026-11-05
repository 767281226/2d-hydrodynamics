#!/usr/bin/env bash
# Offline install of hydro-pipeline on Linux. No network access is used.
#   ./install.sh [PREFIX]            default PREFIX=/opt/hydro-pipeline (needs write access)
#   SKIP_TESTS=1 ./install.sh ...    skip the post-install self test
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${1:-/opt/hydro-pipeline}"

echo "== hydro-pipeline offline install -> $PREFIX"
arch="$(uname -m)"
bundle_arch="$(ls "$HERE"/wheels | grep -o -m1 -E 'x86_64|aarch64' || true)"
if [ -n "$bundle_arch" ] && [ "$arch" != "$bundle_arch" ]; then
  echo "[error] this bundle is for $bundle_arch, machine is $arch"; exit 1
fi
glibc="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo 0)"
echo "   arch=$arch glibc=$glibc"
if [ "$(printf '%s\n2.28\n' "$glibc" | sort -V | head -1)" != "2.28" ]; then
  echo "[error] glibc >= 2.28 required (found $glibc). Supported e.g. RHEL/CentOS/Rocky 8+, Ubuntu 20.04+, Debian 10+, Kylin V10, UOS 20."; exit 1
fi

echo "== verify checksums"
(cd "$HERE" && sha256sum -c --quiet SHA256SUMS)

mkdir -p "$PREFIX"
echo "== portable Python"
rm -rf "$PREFIX/python"
tar -xzf "$HERE/python.tar.gz" -C "$PREFIX"          # creates $PREFIX/python
"$PREFIX/python/bin/python3" -V

echo "== virtual environment + wheels (offline)"
rm -rf "$PREFIX/venv"
"$PREFIX/python/bin/python3" -m venv "$PREFIX/venv"
PY="$PREFIX/venv/bin/python"
"$PY" -m pip install --no-index --find-links "$HERE/wheels" -r "$HERE/requirements-linux.txt" --disable-pip-version-check -q

echo "== project"
rm -rf "$PREFIX/app"
cp -r "$HERE/project" "$PREFIX/app"
"$PY" -m pip install --no-index --no-build-isolation --no-deps --disable-pip-version-check -q "$PREFIX/app"
cp "$HERE/run_case.sh" "$HERE/start_viewer.sh" "$PREFIX/"
chmod +x "$PREFIX/run_case.sh" "$PREFIX/start_viewer.sh"

echo "== import check"
"$PY" -X utf8 - <<'PYEOF'
import importlib, sys
ok = True
for m in ["numpy", "pydantic", "yaml", "rasterio", "pyproj", "shapely", "shapefile", "netCDF4", "numba", "hydro_pipeline.case_pipeline"]:
    try:
        importlib.import_module(m); print(f"   ok   {m}")
    except Exception as e:
        ok = False; print(f"   FAIL {m}: {e}")
try:
    import gmsh; print(f"   ok   gmsh {gmsh.__version__}")
except Exception as e:
    print(f"   WARN gmsh: {e}\n        mesh stage needs X11/OpenGL client libs, see DEPLOY_LINUX_OFFLINE.md section 4")
sys.exit(0 if ok else 1)
PYEOF

missing="$(ldd "$PREFIX"/venv/lib/libgmsh.so* 2>/dev/null | grep 'not found' | awk '{print $1}' | sort -u | paste -sd' ' - || true)"
if [ -n "$missing" ]; then
  echo "[warn] gmsh system libraries missing: $missing"
  echo "       mesh stage will fail until they are installed (DEPLOY_LINUX_OFFLINE.md section 4), or mesh on Windows (section 4, plan B)"
fi

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  echo "== self test (synthetic DEM, ~30 s)"
  (cd "$PREFIX/app" && "$PY" -m pytest -q -p no:cacheprovider tests) || {
    echo "[warn] self test failed; if only gmsh tests fail, install the libs in DEPLOY_LINUX_OFFLINE.md section 4"; }
fi
echo "== done. Run:  $PREFIX/run_case.sh <case.yaml> [--check|--stage ...]"
