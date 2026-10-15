"""End to end: halt -> needs_repair -> teach_repair -> person approves -> replay passes.

Drives the real Flow replayer, teach loop, and repair lifecycle on Flow's
``modal-once`` fixture (a survey dialog covers the confirm banner). The
fixture's fake backend and vision live in Flow's test suite, so this test runs
when ``OPENADAPT_FLOW_SRC`` points at an openadapt-flow checkout whose
installed package has ``openadapt_flow.repair.agent_teach``. It is skipped
otherwise.

Campaign verdicts are recorded as fixed passing results. Flow's own tests
cover the campaign batteries, and on this synthetic dismiss button the
production fault campaign refuses (no identity evidence), which is the
correct behavior for a real deployment.
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

import pytest

FLOW_SRC = os.environ.get("OPENADAPT_FLOW_SRC")
pytest.importorskip("openadapt_flow.repair.agent_teach")
if not FLOW_SRC or not (Path(FLOW_SRC) / "tests" / "test_halt_learn_loop.py").is_file():
    pytest.skip("set OPENADAPT_FLOW_SRC to an openadapt-flow checkout", allow_module_level=True)
sys.path.insert(0, FLOW_SRC)

import numpy as np  # noqa: E402
from openadapt_flow.ir import Workflow  # noqa: E402
from openadapt_flow.repair import Actor, CanaryRunObservation, RepairStore  # noqa: E402
from openadapt_flow.repair.candidate import CampaignResult  # noqa: E402
from openadapt_flow.repair.lifecycle import RepairLifecycleError  # noqa: E402
from PIL import Image  # noqa: E402
from tests.test_halt_learn_loop import MODAL_FACT, _replay  # noqa: E402
from tests.test_teach_cli import _base_bundle  # noqa: E402

from openadapt_agent.bridge import AgentBridge  # noqa: E402
from openadapt_agent.cards import WorkflowCard  # noqa: E402
from openadapt_agent.repair import RepairAccessPolicy, RepairDesk, set_access  # noqa: E402
from openadapt_agent.runner import RunnerConfig  # noqa: E402
from openadapt_agent.service import CatalogEntry, EngineResult  # noqa: E402

WORKFLOW = "save_encounter"
AGENT = "calling-agent"
PERSON = Actor(kind="human", id="reviewer")
AUTOMATION = Actor(kind="automation", id="ci")


def _frame(with_dialog: bool) -> bytes:
    import cv2

    image = np.full((800, 1280, 3), 255, np.uint8)
    if with_dialog:
        cv2.rectangle(image, (60, 84), (220, 132), (40, 40, 40), -1)
        cv2.putText(image, "Dismiss", (74, 116), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    return buf.tobytes()


class DialogScreen:
    """The halted app: the dialog is up until something clicks Dismiss."""

    def __init__(self) -> None:
        self.dialog = True
        self.clicks: list[tuple[int, int]] = []

    def screenshot(self) -> bytes:
        return _frame(self.dialog)

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        if 60 <= x <= 220 and 84 <= y <= 132:
            self.dialog = False

    def viewport(self) -> tuple[int, int]:
        return (1280, 800)


class Scrubber:
    def validate_ready(self, modalities):
        pass

    def scrub_image(self, image):
        return image.convert("L").convert("RGB")

    def evidence(self, modalities):
        return {"provider": "test", "package_version": "0", "status": "completed"}


class ReplayEngine:
    """Runs the active bundle through Flow's real replayer on modal-once."""

    def __init__(self, tmp: Path, desk: RepairDesk, bundle: Path) -> None:
        self.tmp, self.desk, self.bundle = tmp, desk, bundle
        self.model_calls = 0

    def execute(self, entry, run_id, inputs, raw) -> EngineResult:
        bundle = self.desk.active_bundle(entry.name, self.bundle)
        run_dir = self.tmp / "runs" / run_id
        report, _ = _replay(
            Workflow.load(bundle), modal_text=MODAL_FACT, bundle=bundle, run_dir=run_dir
        )
        if report.success:
            return EngineResult(reason="saved_and_checked", model_calls=0)
        return EngineResult(
            reason="unexpected_screen",
            halt_step_id=report.halt.state_id,
            run_dir=str(run_dir),
            bundle_dir=str(bundle),
            model_calls=0,
        )

    def refresh(self, entry, run_id):
        return None


def _passed(kind: str) -> CampaignResult:
    return CampaignResult(campaign=kind, passed=True, total=5, failed=0)


def test_halt_teach_approve_replay(tmp_path: Path) -> None:
    bundle = _base_bundle(tmp_path)
    access = tmp_path / "access.json"
    set_access(access, workflow=WORKFLOW, mode="scrubbed_only", enabled_by="admin")
    screen = DialogScreen()
    desk = RepairDesk(
        tmp_path / ".repair",
        RepairAccessPolicy(access),
        demonstrator=screen,
        scrubber_factory=Scrubber,
        agent_id=AGENT,
    )
    bridge = AgentBridge(
        None, RunnerConfig(runs_dir=tmp_path / "runs"), allow_run=True, repair_desk=desk
    )
    card = WorkflowCard(name=WORKFLOW, purpose="Save an encounter.", done_means="Saved.")
    bridge.catalog[WORKFLOW] = CatalogEntry(card=card, engine=ReplayEngine(tmp_path, desk, bundle))

    def run(request_id: str) -> dict:
        return bridge.dispatch(
            "run_workflow",
            {"workflow": WORKFLOW, "inputs": {}, "request_id": request_id},
        )

    # 1. The run halts and returns needs_repair, with no screen in it.
    halted = run("req-0001")
    assert halted["outcome"] == "needs_review"
    block = halted["needs_repair"]
    assert block["step_id"] == "s_verify"
    assert block["teach_repair_available"] is True

    # 2. The agent looks at the scrubbed screen and teaches one click.
    shown = bridge.dispatch("get_repair_screen", {"repair_token": block["repair_token"]})
    assert shown["scrubbed"] is True
    Image.open(io.BytesIO(base64.b64decode(shown["screen_png_base64"])))
    taught = bridge.dispatch(
        "teach_repair",
        {
            "repair_token": block["repair_token"],
            "demonstration": {"action": "click", "x": 140, "y": 108},
        },
    )
    assert taught["status"] == "pending_approval"
    assert screen.clicks == [(140, 108)]

    # The token works once.
    with pytest.raises(Exception, match="already used"):
        bridge.dispatch(
            "teach_repair",
            {
                "repair_token": block["repair_token"],
                "demonstration": {"action": "click", "x": 140, "y": 108},
            },
        )

    # 3. Until a person approves, the workflow still halts.
    assert run("req-0002")["outcome"] == "needs_review"

    store = RepairStore(desk.store_for(WORKFLOW))
    candidate = store.load_candidate(taught["candidate_id"])
    assert candidate.state == "candidate"
    provenance = candidate.provenance
    assert provenance.repaired_by == "calling_agent"
    assert provenance.agent_id == AGENT
    assert provenance.screen_share_mode == "scrubbed_only"
    assert provenance.screens_shared == 1
    assert provenance.screens_scrubbed is True
    assert provenance.scrub_evidence.provider == "test"

    # 4. The agent can't review or approve its own repair, even as a "human".
    with pytest.raises(RepairLifecycleError):
        store.review(candidate.candidate_id, Actor(kind="human", id=AGENT))
    store.review(candidate.candidate_id, PERSON)
    store.record_campaign(candidate.candidate_id, _passed("replay"), AUTOMATION)
    store.record_campaign(candidate.candidate_id, _passed("fault"), AUTOMATION)
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, Actor(kind="human", id=AGENT))
    store.approve(candidate.candidate_id, PERSON, non_interactive=True)
    store.stage(candidate.candidate_id, AUTOMATION)
    store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=1)

    # 5. The approved repair replays without halting, with no model calls.
    passed = run("req-0003")
    assert passed["outcome"] == "done"
    assert passed["model_calls"] == 0
    assert "needs_repair" not in passed
    store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id=passed["run_id"], verified=True, silent_incorrect=False),
    )
    assert store.load_candidate(candidate.candidate_id).state == "active"
