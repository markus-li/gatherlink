from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_socks5_connect_reaches_status_http_helper_over_gatherlink(tmp_path: Path) -> None:
    """Prove SOCKS5 CONNECT traffic crosses the Gatherlink UDP service transport."""
    out_dir = tmp_path / "socks5-acceptance"

    result = subprocess.run(
        [
            sys.executable,
            "tools/socks5_gatherlink_acceptance.py",
            "--out",
            str(out_dir),
            "--timeout",
            "15",
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    report = json.loads(result.stdout)

    assert report["passed"] is True
    assert "socks5-http-over-gatherlink-ok" in report["steps"]
    assert "Gatherlink local status (EXPERIMENTAL)" in report["http_status_payload"]["body_preview"]
