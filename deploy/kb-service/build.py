"""从固定基线构建 kb-service 补丁产物；不修改输入或运行中的服务。"""

import hashlib
import json
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path


def build(source: Path, output: Path, *, scoped_account: bool = False):
    root = Path(__file__).resolve().parent
    manifest_name = "scoped-account-manifest.json" if scoped_account else "manifest.json"
    patch_name = "scoped-account.patch" if scoped_account else "baoyun-defaults.patch"
    manifest = json.loads((root / manifest_name).read_text())
    for name, hashes in manifest.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != hashes["before"]:
            raise ValueError(f"{name} 不是已核对基线，拒绝覆盖")
    output.mkdir(parents=True, exist_ok=False)
    for name in manifest:
        shutil.copy2(source / name, output / name)
    subprocess.run(
        ["patch", "--batch", "--fuzz=0", "-p1", "-i", str(root / patch_name)],
        cwd=output,
        check=True,
    )
    for name, hashes in manifest.items():
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != hashes["after"]:
            raise ValueError(f"{name} 产物校验失败")
        py_compile.compile(str(output / name), doraise=True)
    shutil.copy2(root / manifest_name, output / "manifest.json")
    print("产物已构建并核对摘要")


if __name__ == "__main__":
    build(
        Path(sys.argv[1]).resolve(),
        Path(sys.argv[2]).resolve(),
        scoped_account="--scoped-account" in sys.argv[3:],
    )
