"""Local stdio authoring tools: first demo, not governed run.

``openadapt-agent serve --authoring`` registers the same probe names hosted
MCP will use: ``observe``, ``start_record``, ``click``, ``halt``. Local
stdio may also include ``type`` for agent-driven typing through Flow's
Recorder. Hosted remains pause-only. Human type during a pause is
``record_observed`` everywhere; never ``type_text`` on the pause target.

This module is a transport-independent bridge. It does not open a network
listener and does not implement a remote mailbox. Window titles, field
values, screenshots, and backend pixels never cross the MCP wire.
``--authoring`` does not imply ``--allow-run``.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from openadapt_agent.bridge import BridgeError, ToolSpec

__all__ = [
    "AUTHORING_LOCAL_TOOLS",
    "AUTHORING_PROBE_TOOLS",
    "AuthoringBridge",
    "AuthoringError",
    "OBSERVE_SCHEMA_VERSION",
    "open_authoring_session",
    "project_observe",
]

OBSERVE_SCHEMA_VERSION = "openadapt.authoring.observe/v1"
try:
    from openadapt_types.authoring import OBSERVE_SCHEMA_VERSION as _TYPES_OBSERVE
except ImportError:
    pass
else:
    if isinstance(_TYPES_OBSERVE, str) and _TYPES_OBSERVE:
        OBSERVE_SCHEMA_VERSION = _TYPES_OBSERVE

AUTHORING_PROBE_TOOLS = ("observe", "start_record", "click", "halt")
AUTHORING_LOCAL_TOOLS = (
    "type",
    "stop_record",
    "pause_for_input",
    "compile",
    "get_command_result",
)
AUTHORING_TOOLS = AUTHORING_PROBE_TOOLS + AUTHORING_LOCAL_TOOLS

_EMPTY_OBJECT = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}
_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_MUTATING = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
_LOCAL_HALT = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

_FORBIDDEN_WIRE_KEYS = frozenset(
    {
        "value",
        "text",
        "title",
        "window_title",
        "screenshot",
        "ocr",
        "url",
        "urls",
        "backend_pixels",
        "raw",
        "path",
        "file_path",
        "pixels",
    }
)
_BOUNDS_KEYS = frozenset({"x", "y", "w", "h"})
_PROCESS_NAME = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_SIX_DIGITS = re.compile(r"\d{6,}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"\b(?:\+?\d[\d\-\s().]{7,}\d)\b")
_RESULT_DROP = _FORBIDDEN_WIRE_KEYS | frozenset(
    {"execution_outcome", "success", "events", "frames", "before_png", "after_png"}
)
_CLOSED_BACKENDS = frozenset(
    {"web", "macos", "linux", "windows", "rdp", "citrix", "unknown"}
)
_COACH_ONLY_BACKENDS = frozenset({"windows", "rdp", "citrix"})

_CLICK_FIELDS = frozenset({"node_id", "x", "y"})
_TYPE_FIELDS = frozenset({"text", "param", "node_id"})
_PAUSE_FIELDS = frozenset({"node_id", "param", "secret"})
_RESULT_FIELDS = frozenset({"command_id"})

_PROBE_HELP = {
    "observe": (
        "Return a PHI-safe authoring observation of the pinned local window "
        "(openadapt.authoring.observe/v1). No screenshots, OCR, field values, "
        "window titles, URLs, or backend pixels. Use node_id values from this "
        "tree for click."
    ),
    "start_record": (
        "Start a Flow Recorder session over the locally pinned backend. "
        "Refuses Windows native, Citrix, and RDP (coach-only). Does not "
        "compile and does not enable run tools."
    ),
    "click": (
        "Click one observed node through Flow Recorder. Prefer node_id from "
        "the last observe. Local stdio also accepts integer x,y pixels. "
        "Unknown or stale node_id returns error stale_node."
    ),
    "halt": (
        "Stop the authoring session without compiling. Recording evidence "
        "stays local. This is not a governed run halt."
    ),
}


class AuthoringError(BridgeError):
    """Authoring tool refusal or missing Flow session."""


def open_authoring_session(**kwargs: Any) -> object:
    """Construct Flow's public authoring session when that module exists."""
    try:
        from openadapt_flow import authoring as flow_authoring
    except ImportError as exc:
        raise AuthoringError(
            "openadapt_flow.authoring is not available in this environment; "
            "stdio --authoring depends on the Flow authoring session that wraps "
            "Recorder (compile returns needs_human_admit; Continue uses "
            "record_observed, never type_text on the pause target)"
        ) from exc
    opener = getattr(flow_authoring, "open_session", None)
    if callable(opener):
        return opener(**kwargs)
    session_cls = getattr(flow_authoring, "AuthoringSession", None)
    if callable(session_cls):
        return session_cls(**kwargs)
    raise AuthoringError(
        "openadapt_flow.authoring is importable but exposes neither "
        "open_session nor AuthoringSession"
    )


def _safe_label(value: Any, *, process_name: bool = False) -> Optional[str]:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > 80:
        return None
    if process_name and not _PROCESS_NAME.fullmatch(collapsed):
        return None
    if "://" in collapsed or "@" in collapsed or _SIX_DIGITS.search(collapsed):
        return None
    if _EMAIL.search(collapsed) or _SSN.search(collapsed) or _PHONE.search(collapsed):
        return None
    return collapsed


def _bounds(value: Any) -> Optional[dict[str, float]]:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, float] = {}
    for key in ("x", "y", "w", "h"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        out[key] = float(raw)
    extra = set(value) - _BOUNDS_KEYS
    if extra:
        return out
    return out


def _project_window(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"role": "window"}
    window: dict[str, Any] = {}
    process_name = _safe_label(value.get("process_name"), process_name=True)
    if process_name:
        window["process_name"] = process_name
    role = value.get("role")
    window["role"] = role if isinstance(role, str) and role else "window"
    bounds = _bounds(value.get("bounds"))
    if bounds is not None:
        window["bounds"] = bounds
    return window


def _project_node(value: Any) -> Optional[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    node_id = value.get("node_id")
    if not isinstance(node_id, str) or not node_id:
        return None
    node: dict[str, Any] = {"node_id": node_id}
    role = value.get("role")
    if isinstance(role, str) and role:
        node["role"] = role
    control_type = value.get("control_type")
    if isinstance(control_type, str) and control_type:
        node["control_type"] = control_type
    class_name = _safe_label(value.get("class_name"))
    if class_name:
        node["class_name"] = class_name[:64]
    automation_id = _safe_label(value.get("automation_id"))
    if automation_id:
        node["automation_id"] = automation_id
    name = _safe_label(value.get("name"))
    if name:
        node["name"] = name
    if isinstance(value.get("enabled"), bool):
        node["enabled"] = value["enabled"]
    if isinstance(value.get("focused"), bool):
        node["focused"] = value["focused"]
    bounds = _bounds(value.get("bounds"))
    if bounds is not None:
        node["bounds"] = bounds
    return node


def project_observe(payload: Any) -> dict[str, Any]:
    """Fail-closed PHI projection for ``openadapt.authoring.observe/v1``."""
    source = payload if isinstance(payload, Mapping) else {}
    backend = source.get("backend")
    if backend not in _CLOSED_BACKENDS:
        backend = "unknown"
    coach_only = backend in _COACH_ONLY_BACKENDS or source.get("coach_only") is True
    agent_drive = (not coach_only) and source.get("agent_drive") is not False
    if coach_only:
        agent_drive = False
    tree_in = source.get("tree")
    nodes: list[dict[str, Any]] = []
    if isinstance(tree_in, list) and not coach_only:
        for item in tree_in:
            node = _project_node(item)
            if node is not None:
                nodes.append(node)
            if len(nodes) >= 200:
                break
    projected: dict[str, Any] = {
        "schema_version": OBSERVE_SCHEMA_VERSION,
        "backend": backend,
        "provider": (
            source.get("provider") if isinstance(source.get("provider"), str) else "unknown"
        ),
        "mode": "authoring",
        "agent_drive": agent_drive,
        "coach_only": coach_only,
        "recording": source.get("recording") is True,
        "window": _project_window(source.get("window")),
        "tree": nodes,
        "truncated": source.get("truncated") is True or (
            isinstance(tree_in, list) and len(tree_in) > 200
        ),
        "node_count": len(nodes),
    }
    if not nodes:
        projected["reason"] = (
            source.get("reason")
            if isinstance(source.get("reason"), str) and source.get("reason")
            else "empty_projection"
        )
    return projected


def _public_result(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {"status": "ok"}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _RESULT_DROP or key in _FORBIDDEN_WIRE_KEYS:
            continue
        if key == "execution_outcome":
            continue
        if isinstance(value, Mapping):
            nested = _public_result(value)
            if nested:
                out[key] = nested
            continue
        if isinstance(value, list):
            continue
        if key == "success":
            continue
        out[key] = value
    return out


def _require_object(arguments: Optional[dict[str, Any]], allowed: set[str]) -> dict[str, Any]:
    payload = dict(arguments or {})
    unknown = set(payload) - allowed
    if unknown:
        raise AuthoringError("arguments do not match the declared authoring schema")
    return payload


def _invoke(session: object, method: str, **kwargs: Any) -> Any:
    func = getattr(session, method, None)
    aliases = {
        "type_agent": ("type_text", "type"),
        "pause_for_input": ("pause",),
        "stop_record": ("finish", "stop"),
        "start_record": ("start",),
    }
    if not callable(func):
        for alias in aliases.get(method, ()):
            candidate = getattr(session, alias, None)
            if callable(candidate):
                func = candidate
                break
    if not callable(func):
        raise AuthoringError(f"authoring session does not implement {method}")
    try:
        return func(**kwargs) if kwargs else func()
    except TypeError:
        return func(kwargs) if kwargs else func()


class AuthoringBridge:
    """Stdio authoring tool specs and dispatch over a session object."""

    def __init__(self, session: object):
        self.session = session
        self._last_tool: Optional[str] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._coach_only = False

    def handles(self, name: str) -> bool:
        return name in AUTHORING_TOOLS

    def list_tool_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                name="observe",
                description=_PROBE_HELP["observe"],
                input_schema=_EMPTY_OBJECT,
                annotations=_READ_ONLY,
            ),
            ToolSpec(
                name="start_record",
                description=_PROBE_HELP["start_record"],
                input_schema=_EMPTY_OBJECT,
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="click",
                description=_PROBE_HELP["click"],
                input_schema={
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Opaque node id from the last observe tree.",
                        },
                        "x": {
                            "type": "integer",
                            "description": "Local pixel X. Stdio only; hosted click is node_id.",
                        },
                        "y": {
                            "type": "integer",
                            "description": "Local pixel Y. Stdio only; hosted click is node_id.",
                        },
                    },
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="halt",
                description=_PROBE_HELP["halt"],
                input_schema=_EMPTY_OBJECT,
                annotations=_LOCAL_HALT,
            ),
            ToolSpec(
                name="type",
                description=(
                    "Agent-driven typing through Flow Recorder.type_text. "
                    "Local stdio only; hosted MCP has no type tool. Do not use "
                    "this for secrets or for text a person already typed. Human "
                    "input uses pause_for_input, which persists with "
                    "record_observed and never type_text on the pause target."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Literal text the agent types through the backend.",
                        },
                        "param": {
                            "type": "string",
                            "description": "Optional workflow parameter name for this type.",
                        },
                        "node_id": {
                            "type": "string",
                            "description": "Optional observed node to focus before typing.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="pause_for_input",
                description=(
                    "Pause so a person can type in the application. On Continue, "
                    "persist with Recorder.record_observed on the pause-target "
                    "node. Never call type_text for that human input. Secret "
                    "pauses store no text. The MCP result has no value."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "node_id": {
                            "type": "string",
                            "description": "Pause-target node id captured at pause start.",
                        },
                        "param": {
                            "type": "string",
                            "description": "Parameter name to bind on the observed type.",
                        },
                        "secret": {
                            "type": "boolean",
                            "description": "If true, persist secret=True and no text.",
                        },
                    },
                    "additionalProperties": False,
                },
                annotations=_MUTATING,
            ),
            ToolSpec(
                name="stop_record",
                description=(
                    "Finish the Flow Recorder session without compiling. "
                    "Evidence stays local."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations=_LOCAL_HALT,
            ),
            ToolSpec(
                name="compile",
                description=(
                    "Wrap Flow compile_recording and return needs_human_admit. "
                    "An agent click never paints VERIFIED. Refuses a session "
                    "that had a secret pause and no TYPE/param event."
                ),
                input_schema=_EMPTY_OBJECT,
                annotations={
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolSpec(
                name="get_command_result",
                description=(
                    "Return the last in-process authoring result. Stdio executes "
                    "tools synchronously; call this after a probe if the client "
                    "expects the hosted pending/command_id shape."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "command_id": {
                            "type": "string",
                            "description": "Optional id; stdio has one in-process result.",
                        }
                    },
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY,
            ),
        ]
        return specs

    def dispatch(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if name not in AUTHORING_TOOLS:
            raise AuthoringError("unknown tool name")
        if name == "get_command_result":
            _require_object(arguments, _RESULT_FIELDS)
            return self._last_command_result()
        handlers = {
            "observe": self._observe,
            "start_record": self._start_record,
            "click": self._click,
            "halt": self._halt,
            "type": self._type_agent,
            "pause_for_input": self._pause_for_input,
            "stop_record": self._stop_record,
            "compile": self._compile,
        }
        result = handlers[name](arguments)
        self._last_tool = name
        self._last_result = result
        return result

    def _last_command_result(self) -> dict[str, Any]:
        if self._last_result is None:
            return {
                "command_id": None,
                "status": "idle",
                "retry_after_ms": 0,
                "result": None,
            }
        return {
            "command_id": self._last_tool,
            "status": "done",
            "retry_after_ms": 0,
            "result": dict(self._last_result),
        }

    def _observe(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        projected = project_observe(_invoke(self.session, "observe"))
        self._coach_only = projected.get("coach_only") is True
        return projected

    def _refuse_coach_only(self, tool: str) -> None:
        if self._coach_only:
            raise AuthoringError(
                f"{tool} refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)"
            )

    def _start_record(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        self._refuse_coach_only("start_record")
        raw = _invoke(self.session, "start_record")
        result = _public_result(raw)
        if result.get("error") == "COACH_ONLY" or result.get("coach_only") is True:
            self._coach_only = True
            raise AuthoringError(
                "start_record refused: COACH_ONLY (person actuates; this backend "
                "is not agent-drive in v1)"
            )
        result.setdefault("status", "recording")
        return result

    def _click(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _CLICK_FIELDS)
        self._refuse_coach_only("click")
        node_id = payload.get("node_id")
        x = payload.get("x")
        y = payload.get("y")
        has_node = isinstance(node_id, str) and bool(node_id)
        has_point = x is not None or y is not None
        if has_point and (not isinstance(x, int) or isinstance(x, bool) or
                          not isinstance(y, int) or isinstance(y, bool)):
            raise AuthoringError("click x and y must both be integers")
        if not has_node and not has_point:
            raise AuthoringError("click requires node_id or local x and y")
        kwargs: dict[str, Any] = {}
        if has_node:
            kwargs["node_id"] = node_id
        if has_point:
            kwargs["x"] = x
            kwargs["y"] = y
        raw = _invoke(self.session, "click", **kwargs)
        result = _public_result(raw)
        if result.get("error") == "stale_node" or result.get("status") == "stale_node":
            return {"status": "error", "error": "stale_node"}
        if result.get("error") == "COACH_ONLY":
            raise AuthoringError(
                "click refused: COACH_ONLY (person actuates; this backend is "
                "not agent-drive in v1)"
            )
        result.setdefault("status", "ok")
        if has_node:
            result.setdefault("node_id", node_id)
        return result

    def _halt(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = _invoke(self.session, "halt")
        result = _public_result(raw)
        result.setdefault("status", "halted")
        result["compiled"] = False
        return result

    def _type_agent(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _TYPE_FIELDS)
        self._refuse_coach_only("type")
        text = payload.get("text")
        if not isinstance(text, str) or text == "":
            raise AuthoringError("type requires a non-empty text string")
        kwargs: dict[str, Any] = {"text": text}
        param = payload.get("param")
        if param is not None:
            if not isinstance(param, str) or not param:
                raise AuthoringError("param must be a string")
            kwargs["param"] = param
        node_id = payload.get("node_id")
        if node_id is not None:
            if not isinstance(node_id, str) or not node_id:
                raise AuthoringError("node_id must be a string")
            kwargs["node_id"] = node_id
        raw = _invoke(self.session, "type_agent", **kwargs)
        result = _public_result(raw)
        result.pop("text", None)
        result.setdefault("status", "ok")
        result["recorded"] = True
        if param:
            result["param"] = param
        return result

    def _pause_for_input(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        payload = _require_object(arguments, _PAUSE_FIELDS)
        kwargs: dict[str, Any] = {}
        node_id = payload.get("node_id")
        if node_id is not None:
            if not isinstance(node_id, str) or not node_id:
                raise AuthoringError("node_id must be a string")
            kwargs["node_id"] = node_id
        param = payload.get("param")
        if param is not None:
            if not isinstance(param, str) or not param:
                raise AuthoringError("param must be a string")
            kwargs["param"] = param
        secret = payload.get("secret")
        if secret is not None:
            if not isinstance(secret, bool):
                raise AuthoringError("secret must be a boolean")
            kwargs["secret"] = secret
        raw = _invoke(self.session, "pause_for_input", **kwargs)
        result = _public_result(raw)
        result.pop("text", None)
        result.pop("value", None)
        result.setdefault("recorded", True)
        if param:
            result["param"] = param
        if secret is True:
            result["secret"] = True
        return result

    def _stop_record(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = _invoke(self.session, "stop_record")
        result = _public_result(raw)
        result.setdefault("status", "stopped")
        result["compiled"] = False
        return result

    def _compile(self, arguments: Optional[dict[str, Any]]) -> dict[str, Any]:
        _require_object(arguments, set())
        raw = _invoke(self.session, "compile")
        if isinstance(raw, Mapping) and raw.get("error") == "missing_secret_type":
            return {"status": "error", "error": "missing_secret_type"}
        result = _public_result(raw)
        if result.get("status") == "error":
            return result
        workflow_id = result.get("workflow_id")
        public = {
            "status": "needs_human_admit",
            "recording_retained": True,
        }
        if isinstance(workflow_id, str) and workflow_id:
            public["workflow_id"] = workflow_id
        return public
