"""learn: the agent's input actions are recorded; acceptance needs a person."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from openadapt_agent.learn import SessionExecutor, learn


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 80), "white").save(buf, format="PNG")
    return buf.getvalue()


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.admitted = False
        self.recording_dir = Path("/tmp/rec")

    def start_record(self) -> None:
        self.calls.append(("start_record",))

    def __getattr__(self, name: str) -> Any:
        if name in {"click", "double_click", "right_click", "drag", "scroll", "hotkey", "press"}:
            return lambda *a, **k: self.calls.append((name, *a))
        raise AttributeError(name)

    def type_text(self, text: str, param: str | None = None) -> None:
        self.calls.append(("type", text, param))

    def halt(self) -> Path:
        self.calls.append(("halt",))
        return self.recording_dir

    def compile(self, bundle_dir: Path, *, name: str) -> dict:
        self.calls.append(("compile", name))
        return {
            "status": "needs_human_admit",
            "workflow_id": "wf_x",
            "recording_retained": True,
            "distilled": {"steps_in": 3, "steps_out": 2, "dropped": [{"index": 0}]},
        }

    def admit(self, confirm: object = None) -> dict:
        self.admitted = True
        return {"status": "accepted"}


class _Raw:
    def __init__(self, data: dict) -> None:
        self.text = json.dumps(data)


class FakeClient:
    """Returns a scripted list of responses."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.messages = self
        self.with_raw_response = self

    def create(self, **_kwargs: Any) -> _Raw:
        return _Raw(self._responses.pop(0))


def _use(i: int, name: str, **inp: Any) -> dict:
    return {
        "type": "tool_use",
        "id": f"t{i}",
        "name": name,
        "toolset_name": "computer",
        "input": inp,
    }


USAGE = {"input_tokens": 10, "output_tokens": 10}
SCRIPT = [
    {
        "stop_reason": "tool_use",
        "usage": USAGE,
        "content": [
            _use(1, "screenshot"),
            _use(2, "left_click", coordinate=[10, 20]),
            _use(3, "type", text="Nadia"),
            _use(4, "type", text="free text"),
            _use(7, "type", text="Fem"),
            _use(5, "key", text="ctrl+a"),
            _use(6, "mouse_move", coordinate=[5, 5]),
        ],
    },
    {"stop_reason": "end_turn", "usage": USAGE, "content": [{"type": "text", "text": "Saved."}]},
]


def _learn(session: FakeSession, tmp_path: Path, confirm=None):
    return learn(
        session,
        "do it",
        client=FakeClient(SCRIPT),
        capture=_png,
        viewport=(100, 80),
        bundle_dir=tmp_path / "bundle",
        params={"fname": "Nadia", "sex": "Female", "state": "Maine", "s2": "Massachusetts"},
        confirm=confirm,
    )


def test_records_inputs_binds_params_and_skips_observations(tmp_path: Path) -> None:
    session = FakeSession()
    result = _learn(session, tmp_path)
    assert session.calls == [
        ("start_record",),
        ("click", 10, 20),
        ("type", "Nadia", "fname"),
        ("type", "free text", None),
        ("type", "Female", "sex"),
        ("hotkey", ["Control"], "a"),
        ("compile", "learned"),
    ]
    assert result.bound_params == ["fname", "sex"]
    assert result.expanded_prefixes == 1
    assert result.literal_types == 1
    assert result.agent["model_calls"] == 2


def test_draft_is_not_accepted_without_a_person(tmp_path: Path) -> None:
    session = FakeSession()
    result = _learn(session, tmp_path)
    assert result.status == "needs_human_admit"
    assert result.accepted is False
    assert session.admitted is False


def test_draft_is_not_accepted_when_the_person_declines(tmp_path: Path) -> None:
    session = FakeSession()
    result = _learn(session, tmp_path, confirm=lambda _s: False)
    assert result.status == "needs_human_admit" and not session.admitted


def test_person_acceptance_admits(tmp_path: Path) -> None:
    session = FakeSession()
    seen: list[dict] = []
    result = _learn(session, tmp_path, confirm=lambda s: seen.append(s) or True)
    assert result.status == "accepted" and session.admitted
    assert seen[0]["distilled"]["steps_out"] == 2


def test_agent_that_did_not_finish_is_not_compiled(tmp_path: Path) -> None:
    session = FakeSession()
    result = learn(
        session,
        "do it",
        client=FakeClient(SCRIPT),
        capture=_png,
        viewport=(100, 80),
        bundle_dir=tmp_path / "bundle",
        max_actions=2,
    )
    assert result.status == "agent_stopped"
    assert ("halt",) in session.calls
    assert not any(c[0] == "compile" for c in session.calls)


def test_out_of_screen_click_is_an_error_for_the_model() -> None:
    ex = SessionExecutor(FakeSession(), _png, (100, 80))
    try:
        ex.run("left_click", {"coordinate": [500, 5]})
    except ValueError as exc:
        assert "outside" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_ambiguous_or_short_prefix_stays_literal() -> None:
    session = FakeSession()
    ex = SessionExecutor(session, _png, (100, 80), params={"a": "Maine", "b": "Massachusetts"})
    ex.run("type", {"text": "Ma"})
    ex.run("type", {"text": "M"})
    assert session.calls == [("type", "Ma", None), ("type", "M", None)]
