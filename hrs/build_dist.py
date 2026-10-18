"""Build the hrs release: one zip with the wheels of core, local-app, collector and hrs.

    uv run --no-project python hrs/build_dist.py      ->  dist/hrshturman-<version>-wheels.zip

The bootstrap scripts install hrs from this zip with `uv tool install --find-links`;
third-party dependencies (mcp, markitdown, httpx, ...) still come from PyPI.
"""
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["core", "local-app", "collector", "hrs"]


def main() -> int:
    version = tomllib.loads((ROOT / "hrs" / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    out = ROOT / "dist" / f"hrshturman-{version}-wheels.zip"
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for pkg in PACKAGES:
            subprocess.run(["uv", "build", "--wheel", "--out-dir", tmp, str(ROOT / pkg)], check=True)
        wheels = sorted(Path(tmp).glob("*.whl"))
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for whl in wheels:
                zf.write(whl, whl.name)
    print(out)
    for whl in wheels:
        print("  " + whl.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
