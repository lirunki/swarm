"""Repair the known platform-tag bug in Cygwin's packaged maturin 1.15.

Only use on wheels built locally with this interpreter. Maturin emits
`cygwin_86_64` for x86_64 Cygwin, while Python/pip expect the sysconfig platform
including the Cygwin version. The extension itself builds/imports correctly.
`wheel tags` updates both the filename and WHEEL/RECORD metadata, not the binary.
Never retag Windows CPython, Linux, other ABIs or unknown platform tags.
"""

from __future__ import annotations

import subprocess
import sys
import sysconfig
from pathlib import Path


def repair(folder: Path) -> None:
    if sys.platform != "cygwin":
        raise RuntimeError("Wheel repair requires the Cygwin interpreter that built the wheels")
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    interpreter = f"cp{sys.version_info.major}{sys.version_info.minor}"
    if not platform.endswith("_x86_64"):
        raise RuntimeError("Only x86_64 Cygwin is supported")
    for wheel in sorted(folder.glob("pydantic_core-*.whl")):
        python_tag, abi_tag, platform_tag = wheel.stem.rsplit("-", 3)[-3:]
        if platform_tag == platform:
            continue
        if (python_tag, abi_tag, platform_tag) != (interpreter, interpreter, "cygwin_86_64"):
            raise RuntimeError(f"Refusing to retag an unknown ABI/platform: {wheel.name}")
        print(f"Repairing local maturin wheel platform: {wheel.name} -> {platform}", flush=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "wheel",
                "tags",
                "--platform-tag",
                platform,
                "--remove",
                str(wheel),
            ],
            check=True,
        )


if __name__ == "__main__":
    repair(Path(sys.argv[1]))
