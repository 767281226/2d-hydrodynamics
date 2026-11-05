"""Build the offline Linux bundle on a machine WITH network (Windows or Linux).

    python build_offline_bundle.py [--arch x86_64|aarch64] [--python 3.12] [--out dist]

Produces dist/hydro-pipeline-offline-linux-<arch>.tar.gz containing:
  python/      portable CPython (python-build-standalone, install_only)
  wheels/      every dependency wheel for manylinux <arch>, cp312 (no sdists: nothing compiles on the target)
  project/     hydro-pipeline source (hydro_pipeline/, tests/, docs/, examples/, pyproject.toml)
  install.sh, run_case.sh, start_viewer.sh, DEPLOY_LINUX_OFFLINE.md, requirements-linux.txt, SHA256SUMS
"""
import argparse, hashlib, json, re, shutil, subprocess, sys, tarfile, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent.parent
PBS_API = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"


def sh(*cmd):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", choices=["x86_64", "aarch64"], default="x86_64")
    ap.add_argument("--python", default="3.12")
    ap.add_argument("--out", default=str(HERE / "dist"))
    a = ap.parse_args()
    name = f"hydro-pipeline-offline-linux-{a.arch}"
    stage = Path(a.out) / name
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "wheels").mkdir(parents=True)

    # 1) portable CPython
    release = json.load(urllib.request.urlopen(PBS_API, timeout=60))
    want = re.compile(rf"cpython-{re.escape(a.python)}\.\d+\+\d+-{a.arch}-unknown-linux-gnu-install_only\.tar\.gz$")
    asset = next(x for x in release["assets"] if want.match(x["name"]))
    cache = Path(a.out) / "_cache"; cache.mkdir(parents=True, exist_ok=True)
    py_tgz = cache / asset["name"]
    if not py_tgz.is_file() or py_tgz.stat().st_size != asset["size"]:
        print("download", asset["name"], flush=True)
        urllib.request.urlretrieve(asset["browser_download_url"], py_tgz)
    shutil.copy2(py_tgz, stage / "python.tar.gz")

    # 2) wheels for the target platform (binary only)
    pyver = a.python.replace(".", "")
    plats = [f"manylinux_2_{v}_{a.arch}" for v in (17, 24, 27, 28)] + [f"manylinux2014_{a.arch}"]
    cmd = [sys.executable, "-m", "pip", "download", "-r", HERE / "requirements-linux.in", "-d", stage / "wheels",
           "--only-binary=:all:", "--python-version", a.python, "--implementation", "cp", "--abi", f"cp{pyver}"]
    for p in plats:
        cmd += ["--platform", p]
    sh(*cmd)
    wheels = sorted(p.name for p in (stage / "wheels").glob("*.whl"))
    pins = []
    for w in wheels:
        dist, ver = w.split("-")[:2]
        pins.append(f"{dist.replace('_', '-')}=={ver}")
    (stage / "requirements-linux.txt").write_bytes(("\n".join(pins) + "\n").encode("utf-8"))  # LF for Linux
    shutil.copy2(stage / "requirements-linux.txt", HERE / "requirements-linux.txt")

    # 3) project source
    proj = stage / "project"
    for item in ("hydro_pipeline", "tests", "docs", "examples"):
        shutil.copytree(PROJECT / item, proj / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for item in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(PROJECT / item, proj / item)

    # 4) scripts + doc (LF line endings, executable bits set in the tar)
    for item in ("install.sh", "run_case.sh", "start_viewer.sh", "DEPLOY_LINUX_OFFLINE.md"):
        text = (HERE / item).read_text(encoding="utf-8").replace("\r\n", "\n")
        (stage / item).write_bytes(text.encode("utf-8"))

    # 5) checksums
    lines = []
    for p in sorted(stage.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(stage).as_posix()}")
    (stage / "SHA256SUMS").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))  # LF: sha256sum -c rejects CRLF names

    # 6) tarball
    tgz = Path(a.out) / f"{name}.tar.gz"
    def mode(ti):
        if ti.name.endswith(".sh"):
            ti.mode = 0o755
        return ti
    with tarfile.open(tgz, "w:gz") as tar:
        tar.add(stage, arcname=name, filter=mode)
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    (Path(a.out) / f"{name}.tar.gz.sha256").write_bytes(f"{digest}  {tgz.name}\n".encode("utf-8"))
    print(f"\nbundle: {tgz} ({tgz.stat().st_size/1e6:.1f} MB)\nsha256: {digest}\nwheels: {len(wheels)}")


if __name__ == "__main__":
    main()
