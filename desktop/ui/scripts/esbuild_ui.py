from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "dist"
ASSET_DIR = DIST_DIR / "assets"
SOURCE_HTML = ROOT / "index.html"
ENTRYPOINT = ROOT / "src" / "main.tsx"


def resolve_esbuild_binary() -> Path:
    candidates = sorted(ROOT.glob("node_modules/@esbuild/*/esbuild.exe"))
    if not candidates:
        candidates = sorted(ROOT.glob("node_modules/@esbuild/*/bin/esbuild"))
    if not candidates:
        raise FileNotFoundError(
            "Unable to locate an esbuild binary under node_modules/@esbuild/. "
            "Run `npm install` inside desktop/ui first."
        )
    return candidates[0]


def write_dist_html() -> None:
    source = SOURCE_HTML.read_text(encoding="utf-8")
    source_tag = '<script type="module" src="/src/main.tsx"></script>'
    replacement = (
        '    <link rel="stylesheet" href="./assets/app.css" />\n'
        '    <script type="module" src="./assets/app.js"></script>'
    )
    if source_tag not in source:
        raise RuntimeError(f"Expected to find {source_tag!r} in {SOURCE_HTML}.")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    (DIST_DIR / "index.html").write_text(source.replace(source_tag, replacement), encoding="utf-8")


def base_build_args() -> list[str]:
    return [
        str(resolve_esbuild_binary()),
        str(ENTRYPOINT.relative_to(ROOT)),
        "--bundle",
        "--format=esm",
        "--platform=browser",
        "--target=es2020",
        "--jsx=automatic",
        "--entry-names=app",
        "--outdir=dist/assets",
        "--loader:.ts=ts",
        "--loader:.tsx=tsx",
        "--loader:.css=css",
    ]


def run_build() -> int:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    write_dist_html()
    command = base_build_args() + [
        "--minify",
        '--define:process.env.NODE_ENV="production"',
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def run_dev() -> int:
    write_dist_html()
    command = base_build_args() + [
        "--sourcemap",
        '--define:process.env.NODE_ENV="development"',
        "--serve=127.0.0.1:1420",
        "--servedir=dist",
        "--watch=forever",
    ]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or serve the desktop UI with the native esbuild binary.")
    parser.add_argument("command", choices=("build", "dev"))
    args = parser.parse_args()

    try:
        if args.command == "build":
            return run_build()
        return run_dev()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
