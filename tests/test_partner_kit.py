"""Day-1 partner kit: invoke Flow and keep the MockMed oracle honest.

Unit tests in this package stub ``openadapt-flow run``. These tests call the
installed engine. The weekly Flow tutorial e2e still owns the Playwright
``VERIFIED`` / ``HALTED`` path.
"""

from __future__ import annotations

import json
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from openadapt_agent.runner import default_flow_cli


def test_default_flow_cli_invokes_the_installed_tutorial_help() -> None:
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    result = subprocess.run(
        [*default_flow_cli(), "tutorial", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout + result.stderr
    assert result.returncode == 0, text
    assert "--break-it" in text


def test_mockmed_optimistic_fault_leaves_the_store_unchanged() -> None:
    pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    try:
        before = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert before["records"] == []

        request = Request(
            url + "api/encounter?fault=optimistic",
            data=json.dumps(
                {"patient_id": "p1", "type": "Triage", "note": "synthetic"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as raised:
            urlopen(request, timeout=2)
        assert raised.value.code == 409

        after = json.loads(urlopen(url + "api/db", timeout=2).read().decode("utf-8"))
        assert after["records"] == []
        assert db.rejected_writes == 1
    finally:
        stop()
