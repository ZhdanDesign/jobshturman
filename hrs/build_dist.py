"""Build the hrs release: one zip with the wheels of core, local-app, collector and hrs, and the skill.

    uv run --no-project python hrs/build_dist.py [--sign KEY.pem]   ->  dist/hrshturman-<version>-wheels.zip (+ .sig)

The bootstrap scripts install hrs from this zip with `uv tool install --find-links`; third-party dependencies
(mcp, markitdown, httpx, ...) still come from PyPI. The `skill/` folder inside is plugin/skills/hrshturman:
the self-update takes the skill from here, so the whole release is one signed file. `--sign` writes the
Ed25519 signature next to the zip (the methodology signing key of the key server; `hrs-keys sign-release` does
the same on the server). Both files go to the GitHub release as jobshturman-wheels.zip and .sig.
"""
import argparse
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["core", "local-app", "collector", "hrs"]
SKILL = ROOT / "plugin" / "skills" / "hrshturman"
RELEASE_TAG = b"jobshturman-release\n"  # the same prefix as hrs_cli.selfupdate.RELEASE_TAG


def sign(data: bytes, key_file: Path) -> str:
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    import base64
    return base64.b64encode(key.sign(RELEASE_TAG + data)).decode()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sign", metavar="KEY.pem", help="ключ подписи Ed25519 (PEM): рядом с zip появится .sig")
    args = ap.parse_args(argv)
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
            for path in sorted(p for p in SKILL.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                zf.write(path, f"skill/{path.relative_to(SKILL).as_posix()}")
    print(out)
    for whl in wheels:
        print("  " + whl.name)
    if args.sign:
        sig = out.with_name(out.name + ".sig")
        sig.write_text(sign(out.read_bytes(), Path(args.sign)) + "\n", encoding="utf-8")
        print(sig)
    return 0


if __name__ == "__main__":
    sys.exit(main())
