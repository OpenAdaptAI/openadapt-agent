"""``openadapt-agent learn``: an AI agent does run 1, OpenAdapt compiles it.

A Claude computer-use agent performs the task once. Every input action it
takes goes through Flow's :class:`~openadapt_flow.authoring.AuthoringSession`,
so it is recorded exactly like a person's demonstration (before/after frames
and ``events.jsonl``). Observation actions (screenshot, zoom, wait, cursor
position, pointer moves) are not recorded: they are not part of the program.

When the agent stops, the session distills the recording (steps with no
visible effect are dropped) and compiles it. The result is a draft bundle
with status ``needs_human_admit``. Later runs replay that bundle with no
model calls.

A person must accept the draft before it is used. :func:`learn` asks the
``confirm`` callback (the CLI asks on the terminal) and only then calls
``admit``. Without a confirmation the draft stays unaccepted, and nothing in
this module accepts it on the person's behalf.

Typed text that equals one of the task inputs (``params``) is recorded as
that parameter, so replays can enter different values. A type-ahead prefix of
exactly one input (``Fem`` for ``Female``) is typed in full and recorded as
that input. Text that matches no input is recorded literally.
"""

from __future__ import annotations

import base64
import io
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "LEARN_TOOL_VERSION",
    "PRICES_USD_PER_MTOK",
    "AgentRun",
    "LearnResult",
    "SessionExecutor",
    "learn",
    "run_agent",
    "usage_cost",
]

LEARN_TOOL_VERSION = "openadapt.learn/v1"
TOOLSET_TYPE = "computer_toolset_20260801"
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 8192
EFFORT = "medium"
IMAGE_TOKENS_BOUND = 1_800
MAX_CONTEXT_TOKENS = 180_000
WHEEL_PX_PER_CLICK = 100
NOT_EXECUTED = "Not executed: an earlier computer action in this turn failed."

# USD per million tokens, list price (checked 2026-10-14).
PRICES_USD_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {
        "input": 2.00,
        "output": 10.00,
        "cache_write_5m": 2.50,
        "cache_write_1h": 4.00,
        "cache_read": 0.20,
    },
    "claude-opus-5-5": {
        "input": 4.00,
        "output": 20.00,
        "cache_write_5m": 5.00,
        "cache_write_1h": 8.00,
        "cache_read": 0.20,
    },
}

_MODS = {
    "ctrl": "Control",
    "control": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "option": "Alt",
    "super": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "win": "Meta",
}
_NAMED = {
    "return": "Enter",
    "enter": "Enter",
    "kp_enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "pageup": "PageUp",
    "page_down": "PageDown",
    "pagedown": "PageDown",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
}


def usage_cost(model: str, usage: Mapping[str, Any]) -> dict[str, Any]:
    """List-price cost of one response's ``usage`` block."""

    p = PRICES_USD_PER_MTOK[model]
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    detail = usage.get("cache_creation") or {}
    cw1h = int(detail.get("ephemeral_1h_input_tokens") or 0)
    cost = (
        inp * p["input"]
        + out * p["output"]
        + (cw - cw1h) * p["cache_write_5m"]
        + cw1h * p["cache_write_1h"]
        + cr * p["cache_read"]
    ) / 1_000_000
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "cost_usd": cost,
    }


def _split_chord(text: str) -> tuple[list[str], str]:
    if text == "+":
        return [], "+"
    parts = [p for p in text.split("+") if p]
    out = []
    for p in parts:
        low = p.lower()
        out.append(_MODS.get(low) or _NAMED.get(low) or p)
    return out[:-1], out[-1]


class SessionExecutor:
    """Runs computer-toolset member calls through an authoring session.

    Input members are recorded by the session. Observation members are
    answered from ``capture`` and not recorded.
    """

    INPUT_MEMBERS = {
        "left_click",
        "right_click",
        "double_click",
        "left_click_drag",
        "scroll",
        "type",
        "key",
    }
    OBSERVE_MEMBERS = {"screenshot", "zoom", "cursor_position", "wait", "mouse_move"}

    def __init__(
        self,
        session: Any,
        capture: Callable[[], bytes],
        viewport: tuple[int, int],
        *,
        params: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.session = session
        self.capture = capture
        self.w, self.h = int(viewport[0]), int(viewport[1])
        self.pos = (self.w // 2, self.h // 2)
        self._param_by_value = {
            str(v).strip(): k for k, v in (params or {}).items() if str(v).strip()
        }
        self.bound_params: list[str] = []
        self.literal_types = 0
        self.expanded_prefixes = 0

    def _expand_prefix(self, text: str) -> Optional[tuple[str, str]]:
        """Bind a short type-ahead (``Fem`` for ``Female``) to its input.

        Agents pick list options by typing the first letters. Replaying the
        literal prefix would pick the same option for every later run, so
        when the text is a prefix of exactly one input value, the full value
        is typed instead and recorded as that input.
        """

        low = text.strip().lower()
        if len(low) < 2:
            return None
        hits = [(k, v) for v, k in self._param_by_value.items() if v.lower().startswith(low)]
        if len(hits) != 1 or hits[0][1].lower() == low:
            return None
        return hits[0]

    def _xy(self, v: Any) -> tuple[int, int]:
        x, y = int(round(float(v[0]))), int(round(float(v[1])))
        if not (0 <= x < self.w and 0 <= y < self.h):
            raise ValueError(f"coordinate {[x, y]} is outside the {self.w}x{self.h} screen")
        return x, y

    def _zoom(self, region: Any) -> bytes:
        from PIL import Image

        x0, y0, x1, y1 = (int(round(float(v))) for v in region)
        x0, x1 = sorted((max(0, x0), min(self.w, x1)))
        y0, y1 = sorted((max(0, y0), min(self.h, y1)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise ValueError(f"zoom region {region} is empty")
        img = Image.open(io.BytesIO(self.capture())).crop((x0, y0, x1, y1))
        scale = min(self.w / img.width, self.h / img.height)
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def run(self, name: str, inp: Mapping[str, Any]) -> tuple[str, Optional[bytes]]:
        """Execute one member call. Returns (text, image or None)."""

        s = self.session
        if name == "screenshot":
            return "", self.capture()
        if name == "zoom":
            return "", self._zoom(inp["region"])
        if name == "cursor_position":
            return f"[{self.pos[0]}, {self.pos[1]}]", None
        if name == "wait":
            time.sleep(min(max(float(inp.get("duration", 1.0)), 0.0), 10.0))
            return "OK", None
        if name == "mouse_move":
            self.pos = self._xy(inp["coordinate"])
            return "OK", None
        if name in ("left_click", "right_click", "double_click"):
            if inp.get("text"):
                raise ValueError("clicks with held modifier keys are not recordable here")
            if inp.get("coordinate") is not None:
                self.pos = self._xy(inp["coordinate"])
            method = {"left_click": "click", "right_click": "right_click"}.get(name, "double_click")
            getattr(s, method)(*self.pos)
            return "OK", None
        if name == "left_click_drag":
            start = self._xy(inp["start_coordinate"])
            end = self._xy(inp["coordinate"])
            s.drag(*start, *end)
            self.pos = end
            return "OK", None
        if name == "scroll":
            if inp.get("text"):
                raise ValueError("scroll with held modifier keys is not recordable here")
            if inp.get("coordinate") is not None:
                self.pos = self._xy(inp["coordinate"])
            amt = int(inp.get("scroll_amount", 3)) * WHEEL_PX_PER_CLICK
            dx, dy = {
                "down": (0, amt),
                "up": (0, -amt),
                "right": (amt, 0),
                "left": (-amt, 0),
            }[str(inp.get("scroll_direction", "down"))]
            # The wheel acts under the pointer; a recorded click is the only
            # pointer placement replay reproduces, so scroll where the
            # pointer already is.
            s.scroll(dx, dy)
            return "OK", None
        if name == "type":
            text = str(inp["text"])
            param = self._param_by_value.get(text.strip())
            if param is None:
                expanded = self._expand_prefix(text)
                if expanded is not None:
                    param, text = expanded
                    self.expanded_prefixes += 1
            if param is not None:
                self.bound_params.append(param)
            else:
                self.literal_types += 1
            s.type_text(text, param=param)
            return "OK", None
        if name == "key":
            mods, key = _split_chord(str(inp["text"]))
            for _ in range(max(1, min(int(inp.get("repeat", 1) or 1), 20))):
                if mods:
                    s.hotkey(mods, key)
                else:
                    s.press(key)
            return "OK", None
        raise ValueError(f"member {name!r} is not available while learning")


@dataclass
class AgentRun:
    """What the agent did on run 1, with its model usage."""

    model: str
    stopped: str = "not started"
    actions: int = 0
    input_actions: int = 0
    observe_actions: int = 0
    model_calls: int = 0
    screenshots_sent: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    model_wait_s: float = 0.0
    action_s: float = 0.0
    final_report: str = ""
    error: Optional[str] = None
    action_log: list[dict[str, Any]] = field(default_factory=list)


def _img_block(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(png).decode("ascii"),
        },
    }


def _set_cache_breakpoint(messages: list[dict[str, Any]]) -> None:
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if isinstance(b, dict):
                    b.pop("cache_control", None)
    messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}


def run_agent(
    executor: SessionExecutor,
    task: str,
    *,
    client: Any,
    model: str = DEFAULT_MODEL,
    max_actions: int = 80,
    max_cost_usd: float = 1.0,
    max_wall_s: float = 900.0,
    on_usage: Optional[Callable[[dict[str, Any]], None]] = None,
    may_spend: Optional[Callable[[float], bool]] = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> AgentRun:
    """Drive the app until the model stops or a cap is reached.

    The cost cap is checked before every paid call against a worst-case
    estimate of that call, so a run never passes ``max_cost_usd``.
    ``may_spend(worst_usd)`` lets a caller enforce a ledger across runs.
    """

    run = AgentRun(model=model)
    price = PRICES_USD_PER_MTOK[model]
    tools = [{"type": TOOLSET_TYPE, "cache_control": {"type": "ephemeral"}}]
    first = executor.capture()
    run.screenshots_sent += 1
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": task}, _img_block(first)]}
    ]
    t0 = time.monotonic()
    last_prompt = 0
    pending = 2_000 + IMAGE_TOKENS_BOUND
    last_out = 0
    try:
        while True:
            if time.monotonic() - t0 > max_wall_s:
                run.stopped = "wall_cap"
                break
            est_in = last_prompt + pending + last_out + 6_000 * (run.model_calls == 0)
            if est_in > MAX_CONTEXT_TOKENS:
                run.stopped = "context_cap"
                break
            worst = (est_in * price["cache_write_5m"] + MAX_TOKENS * price["output"]) / 1e6
            if run.cost_usd + worst > max_cost_usd:
                run.stopped = "cost_cap"
                break
            if may_spend is not None and not may_spend(worst):
                run.stopped = "ledger_cap"
                break
            _set_cache_breakpoint(messages)
            t_send = time.time()
            raw = client.messages.with_raw_response.create(
                model=model,
                max_tokens=MAX_TOKENS,
                tools=tools,
                messages=messages,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": EFFORT},
            )
            run.model_wait_s += time.time() - t_send
            data = json.loads(raw.text)
            priced = usage_cost(model, data.get("usage") or {})
            run.model_calls += 1
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                setattr(run, k, getattr(run, k) + priced[k])
            run.cost_usd += priced["cost_usd"]
            if on_usage is not None:
                on_usage(priced)
            last_prompt = (
                priced["input_tokens"]
                + priced["cache_creation_input_tokens"]
                + priced["cache_read_input_tokens"]
            )
            last_out = priced["output_tokens"]
            content = data.get("content") or []
            stop = data.get("stop_reason")
            uses = [b for b in content if b.get("type") == "tool_use"]
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            log(f"call {run.model_calls}: {stop} uses={len(uses)} run=${run.cost_usd:.4f}")
            if stop in ("refusal", "max_tokens") or stop != "tool_use" or not uses:
                run.stopped = stop if stop in ("refusal", "max_tokens") else "model_done"
                run.final_report = "\n".join(texts)
                break
            messages.append({"role": "assistant", "content": content})
            results: list[dict[str, Any]] = []
            failed = budget_hit = False
            last_idx: Optional[int] = None
            last_was_image = False
            new_images = 0
            for u in uses:
                base: dict[str, Any] = {"type": "tool_result", "tool_use_id": u["id"]}
                if u.get("toolset_name"):
                    base["toolset_name"] = u["toolset_name"]
                if failed or budget_hit:
                    results.append(
                        {
                            **base,
                            "is_error": True,
                            "content": NOT_EXECUTED if failed else "Action budget exhausted.",
                        }
                    )
                    continue
                if u.get("toolset_name") != "computer":
                    results.append(
                        {
                            **base,
                            "is_error": True,
                            "content": f"There is no tool named {u.get('name')!r} here.",
                        }
                    )
                    failed = True
                    last_idx = len(results) - 1
                    last_was_image = False
                    continue
                if run.actions >= max_actions:
                    budget_hit = True
                    results.append(
                        {**base, "is_error": True, "content": "Action budget exhausted."}
                    )
                    continue
                name = str(u.get("name"))
                inp = u.get("input") or {}
                run.actions += 1
                if name in SessionExecutor.INPUT_MEMBERS:
                    run.input_actions += 1
                else:
                    run.observe_actions += 1
                a: dict[str, Any] = {"n": run.actions, "name": name, "t_start": time.time()}
                try:
                    text, png = executor.run(name, inp)
                    a["ok"] = True
                    if png is not None:
                        run.screenshots_sent += 1
                        new_images += 1
                        results.append({**base, "content": [_img_block(png)]})
                        last_was_image = True
                    else:
                        results.append(
                            {**base, "content": [{"type": "text", "text": text or "OK"}]}
                        )
                        last_was_image = False
                except Exception as exc:  # noqa: BLE001 - reported to the model
                    a["ok"] = False
                    a["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    results.append(
                        {
                            **base,
                            "is_error": True,
                            "content": f"{name} failed: {type(exc).__name__}: {exc}"[:500],
                        }
                    )
                    failed = True
                    last_was_image = False
                a["t_end"] = time.time()
                run.action_s += a["t_end"] - a["t_start"]
                run.action_log.append(a)
                last_idx = len(results) - 1
            trailing: list[dict[str, Any]] = []
            if last_idx is not None and not last_was_image:
                blk = _img_block(executor.capture())
                run.screenshots_sent += 1
                new_images += 1
                r = results[last_idx]
                if r.get("is_error"):
                    trailing = [{"type": "text", "text": "Screen after the failed action:"}, blk]
                else:
                    body = (
                        r["content"]
                        if isinstance(r["content"], list)
                        else [{"type": "text", "text": r["content"]}]
                    )
                    r["content"] = body + [blk]
            messages.append({"role": "user", "content": results + trailing})
            pending = new_images * IMAGE_TOKENS_BOUND + 300 * len(uses) + 500
            if budget_hit or run.actions >= max_actions:
                run.stopped = "action_cap"
                break
    except Exception as exc:  # noqa: BLE001 - usage already recorded per call
        run.stopped = "error"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
    run.wall_s = time.monotonic() - t0
    return run


@dataclass
class LearnResult:
    """Outcome of one ``learn``: the agent run, the draft, and its acceptance."""

    status: str
    agent: dict[str, Any]
    recording_dir: Optional[str] = None
    bundle_dir: Optional[str] = None
    workflow_id: Optional[str] = None
    distilled: Optional[dict[str, Any]] = None
    bound_params: list[str] = field(default_factory=list)
    literal_types: int = 0
    expanded_prefixes: int = 0
    accepted: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_COMPILABLE_STOPS = {"model_done"}


def learn(
    session: Any,
    task: str,
    *,
    client: Any,
    capture: Callable[[], bytes],
    viewport: tuple[int, int],
    bundle_dir: Path | str,
    params: Optional[Mapping[str, str]] = None,
    model: str = DEFAULT_MODEL,
    max_actions: int = 80,
    max_cost_usd: float = 1.0,
    confirm: Optional[Callable[[dict[str, Any]], bool]] = None,
    on_usage: Optional[Callable[[dict[str, Any]], None]] = None,
    may_spend: Optional[Callable[[float], bool]] = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> LearnResult:
    """Run the agent once through ``session``, then distill and compile.

    Args:
        session: A Flow ``AuthoringSession`` over the pinned app.
        task: The instruction given to the agent.
        client: An ``anthropic.Anthropic`` client.
        capture: Returns a settled screenshot (PNG bytes).
        viewport: Screen size in pixels.
        bundle_dir: Where the compiled draft is written.
        params: Task inputs; typed text equal to a value is bound to its name.
        confirm: Asked with the draft summary. Only a True answer admits it.

    Returns:
        A :class:`LearnResult`. ``status`` is ``accepted`` only after
        ``confirm`` returned True; otherwise ``needs_human_admit`` (draft
        compiled), or ``agent_stopped`` / ``compile_failed``.
    """

    executor = SessionExecutor(session, capture, viewport, params=params)
    session.start_record()
    run = run_agent(
        executor,
        task,
        client=client,
        model=model,
        max_actions=max_actions,
        max_cost_usd=max_cost_usd,
        on_usage=on_usage,
        may_spend=may_spend,
        log=log,
    )
    agent = asdict(run)
    agent.pop("action_log", None)
    agent["action_log"] = run.action_log
    result = LearnResult(
        status="agent_stopped",
        agent=agent,
        bound_params=sorted(set(executor.bound_params)),
        literal_types=executor.literal_types,
        expanded_prefixes=executor.expanded_prefixes,
    )
    if run.stopped not in _COMPILABLE_STOPS:
        rec = session.halt()
        result.recording_dir = str(rec) if rec else None
        result.error = f"agent stopped: {run.stopped}"
        return result
    try:
        compiled = session.compile(Path(bundle_dir), name="learned")
    except Exception as exc:  # noqa: BLE001 - reported, never admitted
        result.status = "compile_failed"
        result.error = f"{type(exc).__name__}: {exc}"[:1000]
        return result
    rec_dir = getattr(session, "recording_dir", None)
    result.recording_dir = str(rec_dir) if rec_dir else None
    result.bundle_dir = str(bundle_dir)
    result.workflow_id = compiled.get("workflow_id")
    result.distilled = compiled.get("distilled")
    result.status = str(compiled.get("status", "needs_human_admit"))
    if confirm is not None and confirm(result.to_dict()) is True:
        session.admit("yes")
        result.accepted = True
        result.status = "accepted"
    return result
