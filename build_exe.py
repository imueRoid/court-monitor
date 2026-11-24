"""OCR 모델을 포함하는 Windows 단일 실행파일 빌드: python build_exe.py"""
from pathlib import Path

import PyInstaller.__main__
import ddddocr


root = Path(__file__).resolve().parent
model = Path(ddddocr.__file__).parent / "common_old.onnx"
PyInstaller.__main__.run([
    "--noconfirm", "--onefile", "--windowed", "--name", "court-search",
    "--copy-metadata", "ddddocr",
    "--add-data", f"{model};ddddocr",
    str(root / "app.py"),
])
