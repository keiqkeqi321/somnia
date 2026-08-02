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


def write_dist_html(*, dev: bool = False) -> None:
    source = SOURCE_HTML.read_text(encoding="utf-8")
    source_tag = '<script type="module" src="/src/main.tsx"></script>'
    if source_tag not in source:
        raise RuntimeError(f"Expected to find {source_tag!r} in {SOURCE_HTML}.")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    replacement = _asset_tags(dev=dev)
    shutil.copy2(ROOT / "src-tauri" / "icons" / "32x32.png", ASSET_DIR / "favicon.png")
    (DIST_DIR / "index.html").write_text(source.replace(source_tag, replacement), encoding="utf-8")


def _asset_tags(*, dev: bool = False) -> str:
    """Reference the hashed production bundles when they exist (build mode).

    Dev mode writes the HTML before the watch build produces any assets, so it
    always uses the fixed un-hashed names. A stale hashed bundle left behind by
    a previous production build must never be referenced here: the dev server
    serves its fresh in-memory build under the un-hashed names.
    """
    if dev:
        return (
            '    <link rel="stylesheet" href="./assets/app.css" />\n'
            '    <script type="module" src="./assets/app.js"></script>'
        )
    js_assets = sorted(ASSET_DIR.glob("app-*.js"))
    css_assets = sorted(ASSET_DIR.glob("app-*.css"))
    if not js_assets and not css_assets:
        return (
            '    <link rel="stylesheet" href="./assets/app.css" />\n'
            '    <script type="module" src="./assets/app.js"></script>'
        )
    if len(js_assets) != 1 or len(css_assets) != 1:
        raise RuntimeError(f"Expected exactly one app js/css bundle in {ASSET_DIR}, found {js_assets + css_assets}.")
    return (
        f'    <link rel="stylesheet" href="./assets/{css_assets[0].name}" />\n'
        f'    <script type="module" src="./assets/{js_assets[0].name}"></script>'
    )


def base_build_args(*, hashed: bool) -> list[str]:
    return [
        str(resolve_esbuild_binary()),
        str(ENTRYPOINT.relative_to(ROOT)),
        "--bundle",
        "--format=esm",
        "--platform=browser",
        "--target=es2020",
        "--jsx=automatic",
        f"--entry-names=app{'-[hash]' if hashed else ''}",
        "--outdir=dist/assets",
        "--public-path=./assets",
        "--loader:.ts=ts",
        "--loader:.tsx=tsx",
        "--loader:.css=css",
        "--loader:.png=file",
    ]


def run_build() -> int:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    command = base_build_args(hashed=True) + [
        "--minify",
        '--define:process.env.NODE_ENV="production"',
    ]
    result = subprocess.run(command, cwd=ROOT, check=False).returncode
    if result != 0:
        return result
    # The HTML references the hashed bundle names, so it is written after the
    # assets exist.
    write_dist_html()
    return 0


def run_dev() -> int:
    write_dist_html(dev=True)
    command = base_build_args(hashed=False) + [
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
