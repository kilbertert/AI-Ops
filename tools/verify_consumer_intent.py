#!/usr/bin/env python3
"""用显式配置的真实模型执行固定语料；消耗额度，报告仅写私有目录。"""

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.model_request import resolve_provider_api_key
from aiops_diagnostics.consumer import consumer_intent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    settings = Settings.from_config(Path(args.config))
    provider = settings.agent.select_provider(None)
    key = resolve_provider_api_key(settings.agent, provider=provider)
    root = Path(__file__).resolve().parents[1]
    out = Path(args.out).resolve()
    if out.is_relative_to(root) or out.exists():
        raise ValueError("报告必须在仓库外新建")
    corpus = root / "docs/agents/consumer-wf01/semantic-cases.jsonl"
    results = []
    for line in corpus.read_text().splitlines():
        case = json.loads(line)
        start = time.monotonic()
        try:
            value = consumer_intent.classify(case["question"], language="zh", provider=provider, api_key=key)
            row = {"result": value, "passed": all(value[k] == v for k, v in case["expected"].items())}
        except (ValueError, RuntimeError, OSError) as exc:
            row = {"passed": False, "error": type(exc).__name__}
        results.append({**case, **row, "elapsed_ms": round((time.monotonic() - start) * 1000)})
    report = {
        "provider": provider.name,
        "model": provider.model,
        "results": results,
        "source_sha256": hashlib.sha256(Path(consumer_intent.__file__).read_bytes()).hexdigest(),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    with out.open("x") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print(f"{sum(row['passed'] for row in results)}/{len(results)}")
    return 0 if all(row["passed"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
