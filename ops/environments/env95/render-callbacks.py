"""从现役回调片段渲染95既有API入口配置；凭据仅透传，业务身份头清空。"""

import argparse
from pathlib import Path


def render(root: Path) -> str:
    text = (root / "ops/nginx/dify-consumer-callback.conf").read_text()
    return text.replace("http://172.18.0.1:8788", "http://127.0.0.1:8788")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(render(Path(__file__).resolve().parents[3]))
