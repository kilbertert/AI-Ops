"""安装已合并的管家静态制品；旧根目录保留，通过 vhost root 切换或恢复。"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path


def verify(build, manifest, revision):
    data = json.loads(manifest.read_text())
    if data.get("dirty") is not False or data.get("source_commit") != revision:
        raise ValueError("制品不是指定的干净合并修订")
    files = data["files"]
    if "index.html" not in files or not any("aiPackage-pages-chat-chat" in p for p in files):
        raise ValueError("制品缺少首页或助手")
    actual = {str(p.relative_to(build)) for p in build.rglob("*") if p.is_file()}
    if actual != set(files) or any(p.is_symlink() for p in build.rglob("*")):
        raise ValueError("制品文件集合不符或包含符号链接")
    for name, digest in files.items():
        path = build / name
        if not path.resolve().is_relative_to(build.resolve()):
            raise ValueError("制品路径越界")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("制品摘要不符")


def install(args):
    verify(args.build, args.manifest, args.revision)
    for path in (args.release, args.vhost, args.extension, args.proxy, args.backup):
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path)):
            raise ValueError("部署路径含不支持的字符")
    original = args.vhost.read_text()
    roots = re.findall(r"(?m)^\s*root\s+([^;]+);", original)
    if len(roots) != 1 or not Path(roots[0]).is_dir():
        raise ValueError("旧静态根不可读")
    old_static = Path(roots[0]) / "static"
    if any(p.is_symlink() for p in old_static.rglob("*")):
        raise ValueError("旧静态文件包含符号链接")
    updated, count = re.subn(r"(?m)^(\s*)root\s+[^;]+;", rf"\g<1>root {args.release};", original)
    if count != 1 or not args.extension.parent.is_dir() or not args.proxy.parent.is_dir():
        raise ValueError("需要唯一静态根与已有配置目录")
    if args.release.exists() or args.release.is_symlink() or args.backup.exists():
        raise ValueError("发布目录或备份目录已存在；使用新的目录")
    args.backup.mkdir(parents=True, mode=0o700)
    paths = [args.vhost, args.extension, args.proxy]
    existed = [p.exists() for p in paths]
    for i, path in enumerate(paths):
        if existed[i]:
            shutil.copy2(path, args.backup / str(i))
    (args.backup / "restore.json").write_text(
        json.dumps({"paths": [str(p) for p in paths], "existed": existed})
    )
    shutil.copytree(args.build, args.release)
    # 只保留旧JS/CSS哈希分块；不把旧目录中的任意文件重新发布。
    for path in old_static.rglob("*"):
        target = args.release / "static" / path.relative_to(old_static)
        if (
            path.is_file()
            and re.fullmatch(r"(?:js|css)/[^/]+\.[0-9a-f]{8}\.(?:js|css)", str(path.relative_to(old_static)))
            and not target.exists()
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    # Nginx worker 需读静态文件；备份、清单及运行凭据不进入静态根。
    args.release.chmod(0o755)
    for p in args.release.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    assets = Path(__file__).resolve().parents[1] / "ops/nginx"
    snippet = (
        (assets / "operator-h5-assistant.conf")
        .read_text()
        .replace("/etc/aiops-41/operator-h5-proxy.inc", str(args.proxy))
    )
    try:
        args.extension.write_text(snippet)
        args.proxy.write_text((assets / "operator-h5-proxy.inc").read_text())
        args.vhost.write_text(updated)
        subprocess.run([args.nginx, "-t"], check=True)
        subprocess.run([args.nginx, "-s", "reload"], check=True)
    except Exception:
        restore(args.backup, args.nginx)
        raise
    print("installed; previous static root retained; backup:", args.backup)


def restore(backup, nginx):
    record = json.loads((backup / "restore.json").read_text())
    for i, (name, existed) in enumerate(zip(record["paths"], record["existed"], strict=True)):
        path = Path(name)
        if existed:
            shutil.copy2(backup / str(i), path)
        else:
            path.unlink(missing_ok=True)
    subprocess.run([nginx, "-t"], check=True)
    subprocess.run([nginx, "-s", "reload"], check=True)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--nginx", required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--restore", action="store_true")
    for key in ("build", "manifest", "release", "vhost", "extension", "proxy"):
        parser.add_argument("--" + key, type=Path)
    parser.add_argument("--revision")
    args = parser.parse_args()
    if args.restore:
        restore(args.backup, args.nginx)
    else:
        if any(
            getattr(args, k) is None
            for k in ("build", "manifest", "release", "vhost", "extension", "proxy", "revision")
        ):
            parser.error("安装需要全部制品与路径参数")
        install(args)


if __name__ == "__main__":
    main()
