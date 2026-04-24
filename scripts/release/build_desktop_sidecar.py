from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
DEFAULT_ENTRY_SCRIPT = REPO_ROOT / "desktop" / "backend" / "sidecar_entry.py"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "desktop" / "ui" / "src-tauri" / "binaries"
DEFAULT_BINARY_BASENAME = "somnia-sidecar"


def normalize_target_triple(target_triple: str) -> str:
    value = str(target_triple).strip()
    if not value:
        raise ValueError("target triple is required")
    return value


def target_platform(target_triple: str) -> str:
    triple = normalize_target_triple(target_triple).lower()
    if "windows" in triple:
        return "windows"
    if "darwin" in triple or "apple" in triple:
        return "macos"
    if "linux" in triple:
        return "linux"
    raise ValueError(f"Unsupported target triple: {target_triple}")


def binary_stem_for_target(target_triple: str, *, basename: str = DEFAULT_BINARY_BASENAME) -> str:
    triple = normalize_target_triple(target_triple)
    return f"{basename}-{triple}"


def binary_filename_for_target(target_triple: str, *, basename: str = DEFAULT_BINARY_BASENAME) -> str:
    stem = binary_stem_for_target(target_triple, basename=basename)
    suffix = ".exe" if target_platform(target_triple) == "windows" else ""
    return f"{stem}{suffix}"


def host_platform() -> str:
    system_name = platform.system().lower()
    if system_name.startswith("win"):
        return "windows"
    if system_name == "darwin":
        return "macos"
    if system_name == "linux":
        return "linux"
    raise RuntimeError(f"Unsupported build host: {platform.system()}")


def ensure_host_matches_target(target_triple: str) -> None:
    expected = target_platform(target_triple)
    actual = host_platform()
    if expected != actual:
        raise RuntimeError(
            f"Target '{target_triple}' is for {expected}, but this host is {actual}. "
            "PyInstaller bundles must be built on the target platform."
        )


def detect_default_target_triple() -> str:
    env_value = os.environ.get("CARGO_BUILD_TARGET", "").strip()
    if env_value:
        return env_value

    try:
        result = subprocess.run(
            ["rustc", "-vV"],
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        result = None

    if result is not None:
        for line in result.stdout.splitlines():
            if line.startswith("host:"):
                host = line.split(":", 1)[1].strip()
                if host:
                    return host

    system_name = platform.system().lower()
    machine = platform.machine().lower()
    if system_name.startswith("win"):
        return "aarch64-pc-windows-msvc" if "arm" in machine else "x86_64-pc-windows-msvc"
    if system_name == "darwin":
        return "aarch64-apple-darwin" if machine in {"arm64", "aarch64"} else "x86_64-apple-darwin"
    if system_name == "linux":
        return "aarch64-unknown-linux-gnu" if machine in {"arm64", "aarch64"} else "x86_64-unknown-linux-gnu"
    raise RuntimeError(f"Unable to infer a target triple for host '{platform.system()}'.")


def pyinstaller_module_available(python_executable: str) -> bool:
    probe = subprocess.run(
        [python_executable, "-c", "import PyInstaller"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0


def pip_module_available(python_executable: str) -> bool:
    probe = subprocess.run(
        [python_executable, "-m", "pip", "--version"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return probe.returncode == 0


def ensure_pip_available(python_executable: str) -> None:
    if pip_module_available(python_executable):
        return
    subprocess.run(
        [python_executable, "-m", "ensurepip", "--upgrade"],
        cwd=REPO_ROOT,
        check=True,
    )


def ensure_pyinstaller_available(python_executable: str) -> None:
    if pyinstaller_module_available(python_executable):
        return
    ensure_pip_available(python_executable)
    subprocess.run(
        [python_executable, "-m", "pip", "install", "pyinstaller"],
        cwd=REPO_ROOT,
        check=True,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the bundled Somnia desktop sidecar executable.")
    parser.add_argument(
        "--target-triple",
        default=detect_default_target_triple(),
        help="Rust target triple used to name the bundled sidecar artifact.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run PyInstaller.",
    )
    parser.add_argument(
        "--entry-script",
        default=str(DEFAULT_ENTRY_SCRIPT),
        help="Python entry script for the sidecar executable.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory that will receive the generated sidecar binary.",
    )
    parser.add_argument(
        "--basename",
        default=DEFAULT_BINARY_BASENAME,
        help="Base binary name before the target triple suffix is applied.",
    )
    return parser.parse_args(argv)


def build_sidecar(
    *,
    python_executable: str,
    target_triple: str,
    entry_script: Path,
    output_dir: Path,
    basename: str = DEFAULT_BINARY_BASENAME,
) -> Path:
    ensure_host_matches_target(target_triple)
    if not entry_script.is_file():
        raise FileNotFoundError(f"Sidecar entry script was not found: {entry_script}")

    ensure_pyinstaller_available(python_executable)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = output_dir / binary_filename_for_target(target_triple, basename=basename)
    if artifact_path.exists():
        artifact_path.unlink()

    with tempfile.TemporaryDirectory(prefix="somnia-sidecar-build-") as temp_dir:
        temp_root = Path(temp_dir)
        work_path = temp_root / "work"
        spec_path = temp_root / "spec"
        dist_path = temp_root / "dist"
        command = [
            python_executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--name",
            binary_stem_for_target(target_triple, basename=basename),
            "--distpath",
            str(dist_path),
            "--workpath",
            str(work_path),
            "--specpath",
            str(spec_path),
            "--paths",
            str(REPO_ROOT),
            str(entry_script),
        ]
        subprocess.run(command, cwd=REPO_ROOT, check=True)

        built_artifact = dist_path / binary_filename_for_target(target_triple, basename=basename)
        if not built_artifact.is_file():
            raise FileNotFoundError(f"PyInstaller did not produce the expected artifact: {built_artifact}")
        shutil.copy2(built_artifact, artifact_path)

    return artifact_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    artifact_path = build_sidecar(
        python_executable=str(args.python),
        target_triple=normalize_target_triple(args.target_triple),
        entry_script=Path(args.entry_script).resolve(),
        output_dir=Path(args.output_dir).resolve(),
        basename=str(args.basename).strip() or DEFAULT_BINARY_BASENAME,
    )
    print(artifact_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
