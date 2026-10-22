"""Tools that drive Home Assistant's config-flow API (add an integration)."""

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import voluptuous as vol
import voluptuous_serialize
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from . import (
    ANNOTATION_DESTRUCTIVE,
    ANNOTATION_NON_IDEMPOTENT,
    ANNOTATION_READ_ONLY,
    dumps,
    register_tool,
)

_LOGGER = logging.getLogger(__name__)

# These flows talk to devices and cloud accounts, so the call is open-world.
_ANNOTATION_OPEN_WORLD = {**ANNOTATION_NON_IDEMPOTENT, "openWorldHint": True}

REDACTED = "***"

# Field names whose values must never be echoed back to the caller or logged.
_SECRET_NAME = re.compile(r"pass(word|wd|phrase)?|secret|token|key|credential|auth", re.IGNORECASE)


def _is_secret(name: Any) -> bool:
    """Return True if a field name looks like it holds a credential."""
    return isinstance(name, str) and _SECRET_NAME.search(name) is not None


def _is_secret_field(field: dict[str, Any]) -> bool:
    """Return True if a serialized form field is secret by name or by password selector."""
    selector = field.get("selector")
    text = selector.get("text") if isinstance(selector, dict) else None
    if isinstance(text, dict) and str(getattr(text.get("type"), "value", text.get("type"))) == (
        "password"
    ):
        return True
    return _is_secret(field.get("name"))


def _strip_url(value: Any) -> Any:
    """Drop userinfo, query string and fragment from a URL-like string."""
    if not isinstance(value, str) or "://" not in value:
        return value
    try:
        parts = urlsplit(value)
        host = parts.netloc.rpartition("@")[2]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return REDACTED


def _redact(value: Any) -> Any:
    """Return value with the content of any secret-looking key replaced."""
    if isinstance(value, dict):
        return {k: (REDACTED if _is_secret(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return _strip_url(value)


def _serialize_schema(schema: Any) -> list[dict[str, Any]]:
    """Describe a form's voluptuous schema as a list of fields, without secret values."""
    try:
        fields = voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)
    except (ValueError, TypeError, vol.Invalid) as err:
        _LOGGER.debug("Could not serialize config flow schema: %s", err)
        return [{"error": "form schema could not be described"}]

    for field in fields:
        if not _is_secret_field(field):
            continue
        if "default" in field:
            field["default"] = REDACTED
        description = field.get("description")
        if isinstance(description, dict) and "suggested_value" in description:
            description["suggested_value"] = REDACTED
    return fields


def _flow_result(result: dict[str, Any], source: str | None = None) -> dict[str, Any]:
    """Reduce a config-flow result to a JSON-safe summary with no secret values."""
    out: dict[str, Any] = {"type": str(getattr(result["type"], "value", result["type"]))}
    for key in ("flow_id", "handler", "step_id", "reason", "last_step", "url", "progress_action"):
        if result.get(key) is not None:
            out[key] = result[key]
    if source:
        out["source"] = source
    if result.get("errors"):
        out["errors"] = result["errors"]
    if result.get("description_placeholders"):
        out["description_placeholders"] = _redact(result["description_placeholders"])
    if result.get("menu_options"):
        out["menu_options"] = list(result["menu_options"])
    if result.get("data_schema") is not None:
        out["data_schema"] = _serialize_schema(result["data_schema"])
    if result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY:
        # Entry data holds the credentials, so only identify the entry.
        entry = result.get("result")
        out["entry"] = {
            "entry_id": getattr(entry, "entry_id", None),
            "title": result.get("title"),
            "domain": getattr(entry, "domain", None) or result.get("handler"),
        }
    return out


# Reauth and reconfigure flows change an existing entry's credentials or settings.
_PROTECTED_SOURCES = {config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE}


def _flow_source(hass: HomeAssistant, flow_id: str) -> str | None:
    """Return the source of an in-progress flow, or None if it is not in progress."""
    for flow in hass.config_entries.flow.async_progress():
        if flow.get("flow_id") == flow_id:
            return (flow.get("context") or {}).get("source")
    return None


def _protected_message(source: str) -> str:
    return f"Flows with source '{source}' change an existing entry and are not driven by this tool"


def _text(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": dumps(payload)}]}


def _error(message: str) -> dict[str, Any]:
    return _text({"error": message})


@register_tool(
    name="start_config_flow",
    description=(
        "Start the setup flow for an integration (add an integration). Returns the flow_id, "
        "the first step and, for a form, its fields (name, type, required, default). Continue "
        "with configure_config_flow, or discard with abort_config_flow. Secret-looking fields "
        "are never echoed back"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "description": "Integration domain to set up, e.g. 'moonraker'",
            },
        },
        "required": ["domain"],
    },
    annotations=_ANNOTATION_OPEN_WORLD,
)
async def start_config_flow(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    """Start a config flow for an integration."""
    domain = arguments["domain"]
    try:
        result = await hass.config_entries.flow.async_init(
            domain, context={"source": config_entries.SOURCE_USER}
        )
    except data_entry_flow.UnknownHandler:
        return _error(f"No setup flow exists for integration '{domain}'")
    except data_entry_flow.FlowError as err:
        return _error(f"Could not start setup flow for '{domain}': {err}")
    except Exception as err:  # noqa: BLE001 - library errors can carry tokenised URLs
        _LOGGER.debug("Config flow start failed", exc_info=True)
        return _error(type(err).__name__)
    return _text(_flow_result(result, config_entries.SOURCE_USER))


@register_tool(
    name="configure_config_flow",
    description=(
        "Submit the next step of a setup flow started with start_config_flow. Returns the next "
        "step or form, field errors such as 'cannot_connect', an abort reason, or the created "
        "entry (entry_id, title, domain). Pass an empty user_input for steps with no fields "
        "(e.g. a confirmation). Secret-looking values are never echoed back"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "flow_id": {"type": "string", "description": "flow_id from start_config_flow"},
            "user_input": {
                "type": "object",
                "description": "Values for the current step's fields, keyed by field name",
            },
        },
        "required": ["flow_id"],
    },
    annotations=_ANNOTATION_OPEN_WORLD,
)
async def configure_config_flow(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    """Submit user input to an in-progress config flow."""
    flow_id = arguments["flow_id"]
    user_input = arguments.get("user_input")
    if user_input is not None and not isinstance(user_input, dict):
        return _error("user_input must be an object")
    source = _flow_source(hass, flow_id)
    if source in _PROTECTED_SOURCES:
        return _error(_protected_message(source))
    try:
        result = await hass.config_entries.flow.async_configure(flow_id, user_input or {})
    except data_entry_flow.UnknownFlow:
        return _error(f"No setup flow in progress with flow_id '{flow_id}'")
    except data_entry_flow.UnknownStep:
        return _error("The setup flow has no handler for this step")
    except vol.Invalid as err:
        # Schema rejection; the message names fields, not the submitted values.
        return _error(f"user_input does not match the current step's fields: {err}")
    except data_entry_flow.FlowError as err:
        return _error(f"Setup flow failed: {err}")
    except Exception as err:  # noqa: BLE001 - library errors can carry tokenised URLs
        _LOGGER.debug("Config flow step failed", exc_info=True)
        return _error(type(err).__name__)
    return _text(_flow_result(result, source))


@register_tool(
    name="abort_config_flow",
    description="Abort an unfinished setup flow and discard it",
    input_schema={
        "type": "object",
        "properties": {
            "flow_id": {"type": "string", "description": "flow_id of the flow to abort"},
        },
        "required": ["flow_id"],
    },
    annotations=ANNOTATION_DESTRUCTIVE,
)
async def abort_config_flow(hass: HomeAssistant, arguments: dict[str, Any]) -> dict[str, Any]:
    """Abort an in-progress config flow."""
    flow_id = arguments["flow_id"]
    source = _flow_source(hass, flow_id)
    if source in _PROTECTED_SOURCES:
        return _error(_protected_message(source))
    try:
        hass.config_entries.flow.async_abort(flow_id)
    except data_entry_flow.UnknownFlow:
        return _error(f"No setup flow in progress with flow_id '{flow_id}'")
    return _text({"aborted": True, "flow_id": flow_id})


@register_tool(
    name="list_config_flows_in_progress",
    description="List setup flows that are started but not finished (including discovered ones)",
    input_schema={"type": "object", "properties": {}},
    annotations=ANNOTATION_READ_ONLY,
)
async def list_config_flows_in_progress(
    hass: HomeAssistant, arguments: dict[str, Any]
) -> dict[str, Any]:
    """List config flows in progress."""
    flows = [
        {
            "flow_id": flow["flow_id"],
            "domain": flow["handler"],
            "step_id": flow.get("step_id"),
            "source": (flow.get("context") or {}).get("source"),
        }
        for flow in hass.config_entries.flow.async_progress()
    ]
    return _text(flows)
