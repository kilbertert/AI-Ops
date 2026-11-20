"""固定转发器更新保留在飞请求及原脚本恢复路径。"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["install", "busy", "unknown", "failed"])
def test_forwarder_install_keeps_connections_and_rolls_back(tmp_path, mode):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "REVISION").write_text("a" * 40 + "\n")
    (bundle / "aiops-inner-api-forward.py").write_text('print("new")\n')
    target = tmp_path / "forwarder"
    target.write_text('print("old")\n')
    old = target.read_bytes()
    script = bundle / "deploy.sh"
    script.write_text(
        (ROOT / "ops/dify-inner-api/deploy.sh")
        .read_text()
        .replace("target=/usr/local/bin/aiops-inner-api-forward", f"target={target}")
        .replace("/run/lock/aiops-inner-api-forward.lock", str(tmp_path / "lock"))
    )
    sums = subprocess.check_output(
        ["sha256sum", "REVISION", "aiops-inner-api-forward.py", "deploy.sh"], cwd=bundle
    )
    (bundle / "SHA256SUMS").write_bytes(sums)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "restarts"
    stubs = {
        "ss": (
            '#!/bin/sh\nif [ "$MODE" = unknown ]; then echo unavailable >&2; exit 1; fi\n'
            '[ "$MODE" != busy ] || echo existing-connection\nexit 0\n'
        ),
        "systemctl": f'#!/bin/sh\n[ "$1" != restart ] || echo restart >> {log}\nexit 0\n',
        "curl": (
            f'#!/bin/sh\nif [ "$MODE" = failed ] && grep -q new {target}; then\n'
            " echo 503\nelse\n echo 401\nfi\n"
        ),
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        p = bin_dir / name
        p.write_text(body)
        p.chmod(0o750)
    env = {**os.environ, "PATH": str(bin_dir) + ":" + os.environ["PATH"], "MODE": mode}
    backup = tmp_path / "backup"
    result = subprocess.run(
        ["bash", str(script), "install", str(bundle), str(backup)], env=env, capture_output=True
    )
    if mode == "install":
        assert result.returncode == 0, result.stderr.decode()
        assert target.read_text() == 'print("new")\n'
        restored = subprocess.run(
            ["bash", str(script), "restore", str(bundle), str(backup)], env=env, capture_output=True
        )
        assert restored.returncode == 0, restored.stderr.decode()
        assert target.read_bytes() == old
    else:
        assert result.returncode != 0
        assert target.read_bytes() == old
    if mode in {"busy", "unknown"}:
        assert not log.exists() and not backup.exists()
    else:
        assert log.read_text().count("restart") == 2
        assert (backup / "forwarder").read_bytes() == old
