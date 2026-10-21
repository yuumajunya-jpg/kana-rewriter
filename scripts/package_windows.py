"""Package the app and a prebuilt TSF DLL for an existing Windows Python environment."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def tsf_artifact(directory, source, build_script):
    manifest = ROOT / "build" / directory / "latest.json"
    build_info = json.loads(manifest.read_text(encoding="utf-8-sig"))
    dll_name = build_info["dll"]
    if not isinstance(dll_name, str) or Path(dll_name).name != dll_name or not dll_name.endswith(".dll"):
        raise RuntimeError("Invalid TSF build manifest")
    dll = manifest.parent / dll_name
    if not dll.is_file():
        raise RuntimeError(f"Build the TSF DLL first with scripts/{build_script}")
    source_hash = hashlib.sha256((ROOT / "native" / source).read_bytes()).hexdigest()
    if build_info.get("source_sha256") != source_hash:
        raise RuntimeError(f"TSF DLL is stale or has no source hash; run scripts/{build_script} again")
    return manifest, dll, dll_name


def package(output, model):
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.implementation.name != "cpython":
        raise RuntimeError("Run with 64-bit Windows CPython from the working environment")
    artifacts = [tsf_artifact("tsf-probe", "tsf_probe.cpp", "build_tsf_probe.ps1"),
                 tsf_artifact("tsf-edit-probe", "tsf_edit_probe.cpp", "build_tsf_edit_probe.ps1")]
    if model and not model.is_file():
        raise FileNotFoundError(model)
    staging = (ROOT / "build").resolve()
    staging.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kana-package-", dir=staging) as temporary:
        if not Path(temporary).resolve().is_relative_to(staging):
            raise RuntimeError("Temporary bundle must remain inside build/")
        bundle = Path(temporary) / "kana-rewriter"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages")
        shutil.copytree(ROOT / "kana_rewriter", bundle / "kana_rewriter", ignore=ignore)
        shutil.copy2(ROOT / "config.example.toml", bundle / "config.toml")
        shutil.copy2(ROOT / "docs/WINDOWS_DISTRIBUTION.md", bundle / "README.md")
        for manifest, dll, dll_name in artifacts:
            target = bundle / "build" / manifest.parent.name
            target.mkdir(parents=True)
            shutil.copy2(dll, target / dll_name)
            shutil.copy2(manifest, target / "latest.json")
        (bundle / "models").mkdir()
        if model:
            shutil.copy2(model, bundle / "models" / model.name)
            config = (bundle / "config.toml").read_text(encoding="utf-8")
            config = re.sub(r'^model_path\s*=.*$',
                            lambda _: 'model_path = ' + json.dumps("models/" + model.name, ensure_ascii=False),
                            config, flags=re.MULTILINE)
            # Match the reference model used by the normal setup instructions.
            if model.name.startswith("jinen-v2-"):
                config = re.sub(r'^model_format\s*=.*$', 'model_format = "jinen_v2"',
                                config, flags=re.MULTILINE)
            (bundle / "config.toml").write_text(config, encoding="utf-8")
        (bundle / "launch.py").write_text(
            "from pathlib import Path\nimport os\nimport sys\n"
            "root = Path(__file__).resolve().parent\nos.chdir(root)\n"
            "sys.path.insert(0, str(root))\n"
            "if __name__ == '__main__':\n"
            "    import struct\n"
            "    if sys.version_info < (3, 11) or sys.platform != 'win32' or struct.calcsize('P') != 8:\n"
            "        raise SystemExit('Requires 64-bit Windows Python 3.11 or later')\n"
            "    from kana_rewriter.__main__ import main\n"
            "    raise SystemExit(main())\n", encoding="utf-8")
        (bundle / "start.cmd").write_text(
            '@echo off\r\ncd /d "%~dp0"\r\n'
            'if defined KANA_REWRITER_PYTHON goto custom\r\n'
            'if exist "%~dp0.venv\\Scripts\\python.exe" goto venv\r\n'
            'py -3 "%~dp0launch.py" --config "%~dp0config.toml" %*\r\ngoto done\r\n'
            ':venv\r\n"%~dp0.venv\\Scripts\\python.exe" "%~dp0launch.py" --config "%~dp0config.toml" %*\r\ngoto done\r\n'
            ':custom\r\n"%KANA_REWRITER_PYTHON%" "%~dp0launch.py" --config "%~dp0config.toml" %*\r\n'
            ':done\r\n'
            'if errorlevel 1 pause\r\n', encoding="ascii", newline="")
        for manifest, dll, dll_name in artifacts:
            packaged_dll = bundle / "build" / manifest.parent.name / dll_name
            subprocess.run([sys.executable, "-I", "-c", "import ctypes; "
                            f"ctypes.WinDLL({str(packaged_dll)!r})"], check=True, cwd=bundle, timeout=60)
        subprocess.run([sys.executable, "-I", str(bundle / "launch.py"), "--help"],
                       check=True, cwd=bundle, stdout=subprocess.DEVNULL, timeout=30)
        hashes = {str(file.relative_to(bundle)).replace("\\", "/"):
                  hashlib.sha256(file.read_bytes()).hexdigest()
                  for file in bundle.rglob("*") if file.is_file() and "__pycache__" not in file.parts}
        (bundle / "bundle-manifest.json").write_text(json.dumps({
            "requires_python": ">=3.11 (64-bit)", "architecture": "win_amd64",
            "sha256": hashes}, indent=2), encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for file in bundle.rglob("*"):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, file.relative_to(bundle.parent))
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/kana-rewriter-windows-x64.zip")
    parser.add_argument("--model", type=Path, help="Optional GGUF; confirm redistribution rights before sharing")
    args = parser.parse_args()
    package(args.output.resolve(), args.model.resolve() if args.model else None)
