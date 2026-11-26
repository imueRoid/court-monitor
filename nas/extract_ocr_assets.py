"""Extract the tested model and native library from verified build-time wheels."""
from pathlib import Path
from shutil import copyfileobj
from sys import argv
from zipfile import ZipFile

wheel_dir, output = map(Path, argv[1:])
output.mkdir(parents=True, exist_ok=True)

for name, entries in (
    ("ddddocr", {
        "ddddocr/common_old.onnx": "common_old.onnx",
        "ddddocr-1.6.1.dist-info/licenses/LICENSE": "ddddocr-LICENSE",
    }),
    ("onnxruntime", {
        "onnxruntime/capi/libonnxruntime.so.1.32.0": "libonnxruntime.so",
        "onnxruntime/capi/libonnxruntime_providers_shared.so": "libonnxruntime_providers_shared.so",
        "onnxruntime/LICENSE": "onnxruntime-LICENSE",
        "onnxruntime/ThirdPartyNotices.txt": "onnxruntime-ThirdPartyNotices.txt",
    }),
):
    (wheel,) = wheel_dir.glob(f"{name}-*.whl")
    with ZipFile(wheel) as archive:
        for source, target in entries.items():
            with archive.open(source) as src, (output / target).open("wb") as dest:
                copyfileobj(src, dest)
