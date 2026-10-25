"""Tests for the config-flow tools (add an integration)."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from custom_components.mcp_server_http_transport.const import EVENT_TOOL_CALLED
from custom_components.mcp_server_http_transport.http import (
    AUTH_METHOD_NATIVE,
    Caller,
    MCPEndpointView,
    ValidatedToken,
)
from custom_components.mcp_server_http_transport.tools import TOOLS, call_tool

SECRET = "hunter2-very-secret"

# The view resolves every request to an authorized caller before dispatch.
CALLER = Caller(user_id="user123", auth_method="native", user=Mock(is_admin=True))


class _StubFlow(config_entries.ConfigFlow):
    """A minimal config flow: password-selector field, an error path, a create-entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input["host"] == "bad":
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title=user_input["host"], data=user_input)
        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Required("pin", default="1234"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


def _out(result):
    return json.loads(result["content"][0]["text"])


async def test_real_flow_manager_end_to_end(hass):
    """Drive Home Assistant's real FlowManager through the tools."""
    from pytest_homeassistant_custom_component.common import (
        MockModule,
        mock_config_flow,
        mock_integration,
        mock_platform,
    )

    mock_integration(
        hass,
        MockModule(
            "stubdomain",
            async_setup_entry=AsyncMock(return_value=True),
            partial_manifest={"config_flow": True},
        ),
    )
    mock_platform(hass, "stubdomain.config_flow", None)
    with mock_config_flow("stubdomain", _StubFlow):
        started = _out(await call_tool(hass, "start_config_flow", {"domain": "stubdomain"}))
        assert started["type"] == "form"
        fields = {f["name"]: f for f in started["data_schema"]}
        assert fields["pin"]["default"] == "***"
        flow_id = started["flow_id"]

        listed = _out(await call_tool(hass, "list_config_flows_in_progress", {}))
        assert listed[0]["flow_id"] == flow_id and listed[0]["source"] == "user"

        # The real manager rejects input that does not match the schema (extra key).
        rejected = _out(
            await call_tool(
                hass,
                "configure_config_flow",
                {"flow_id": flow_id, "user_input": {"host": "h", "pin": SECRET, "x": 1}},
            )
        )
        assert "error" in rejected and SECRET not in json.dumps(rejected)

        failed = _out(
            await call_tool(
                hass,
                "configure_config_flow",
                {"flow_id": flow_id, "user_input": {"host": "bad", "pin": SECRET}},
            )
        )
        assert failed["errors"] == {"base": "cannot_connect"}
        assert SECRET not in json.dumps(failed)

        created = _out(
            await call_tool(
                hass,
                "configure_config_flow",
                {"flow_id": flow_id, "user_input": {"host": "good", "pin": SECRET}},
            )
        )
        assert created["type"] == "create_entry"
        assert created["entry"]["domain"] == "stubdomain"
        assert created["entry"]["title"] == "good"
        assert created["entry"]["entry_id"]
        assert SECRET not in json.dumps(created)


class TestToolsConfigFlows:
    """Test the config-flow tools through the MCP endpoint."""

    @pytest.fixture
    def mock_hass(self):
        """Create a mock Home Assistant instance."""
        hass = Mock()
        hass.config_entries = Mock()
        hass.config_entries.flow = Mock()
        hass.config_entries.flow.async_progress.return_value = []
        return hass

    @pytest.fixture
    def view(self, mock_hass):
        """Create an MCPEndpointView instance."""
        return MCPEndpointView(mock_hass)

    async def _call(self, view, name, arguments):
        request = Mock()
        request.headers = {"Authorization": "Bearer valid_token"}
        request.json = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
                "id": 1,
            }
        )
        with patch.object(view, "_authenticate", return_value=CALLER):
            response = await view.post(request)
        assert response.status == 200
        text = json.loads(response.body)["result"]["content"][0]["text"]
        return text, json.loads(text)

    @staticmethod
    def _form(schema, **extra):
        return {
            "type": FlowResultType.FORM,
            "flow_id": "flow1",
            "handler": "moonraker",
            "step_id": "user",
            "data_schema": schema,
            "errors": None,
            "description_placeholders": None,
            "last_step": None,
            **extra,
        }

    def test_tools_registered_with_required_properties(self):
        for name in ("start_config_flow", "configure_config_flow", "abort_config_flow"):
            assert name in TOOLS
        assert TOOLS["start_config_flow"]["schema"]["inputSchema"]["required"] == ["domain"]
        assert TOOLS["configure_config_flow"]["schema"]["inputSchema"]["required"] == ["flow_id"]
        assert TOOLS["abort_config_flow"]["schema"]["inputSchema"]["required"] == ["flow_id"]
        assert TOOLS["list_config_flows_in_progress"]["schema"]["annotations"]["readOnlyHint"]

    async def test_start_returns_schema(self, view, mock_hass):
        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Optional("port", default=7125): int,
                vol.Optional("api_key"): str,
            }
        )
        mock_hass.config_entries.flow.async_init = AsyncMock(return_value=self._form(schema))

        _, data = await self._call(view, "start_config_flow", {"domain": "moonraker"})

        mock_hass.config_entries.flow.async_init.assert_awaited_once_with(
            "moonraker", context={"source": "user"}
        )
        assert data["type"] == "form"
        assert data["flow_id"] == "flow1"
        assert data["step_id"] == "user"
        fields = {f["name"]: f for f in data["data_schema"]}
        assert fields["host"]["required"] is True
        assert fields["host"]["type"] == "string"
        assert fields["port"]["default"] == 7125
        assert fields["port"]["type"] == "integer"
        assert fields["api_key"]["required"] is False

    async def test_start_always_uses_user_source(self, view, mock_hass):
        mock_hass.config_entries.flow.async_init = AsyncMock(
            return_value={"type": FlowResultType.ABORT, "reason": "single_instance_allowed"}
        )

        _, data = await self._call(
            view, "start_config_flow", {"domain": "moonraker", "source": "import"}
        )

        mock_hass.config_entries.flow.async_init.assert_awaited_once_with(
            "moonraker", context={"source": "user"}
        )
        assert data == {"type": "abort", "reason": "single_instance_allowed", "source": "user"}
        assert "source" not in TOOLS["start_config_flow"]["schema"]["inputSchema"]["properties"]

    async def test_start_unexpected_error_reports_only_type(self, view, mock_hass):
        mock_hass.config_entries.flow.async_init = AsyncMock(
            side_effect=RuntimeError("https://x/?token=" + SECRET)
        )

        text, data = await self._call(view, "start_config_flow", {"domain": "x"})

        assert data == {"error": "RuntimeError"}
        assert SECRET not in text

    async def test_configure_unexpected_error_reports_only_type(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            side_effect=RuntimeError("https://x/?token=" + SECRET)
        )

        text, data = await self._call(view, "configure_config_flow", {"flow_id": "f"})

        assert data == {"error": "RuntimeError"}
        assert SECRET not in text

    async def test_start_masks_password_selector_field(self, view, mock_hass):
        schema = vol.Schema(
            {
                vol.Required("pin", default="1234"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional("name", default="visible"): str,
            }
        )
        mock_hass.config_entries.flow.async_init = AsyncMock(return_value=self._form(schema))

        text, data = await self._call(view, "start_config_flow", {"domain": "x"})

        fields = {f["name"]: f for f in data["data_schema"]}
        assert fields["pin"]["default"] == "***"
        assert fields["name"]["default"] == "visible"
        assert "1234" not in text

    async def test_placeholder_urls_lose_userinfo_and_query(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            return_value={
                "type": FlowResultType.ABORT,
                "reason": "x",
                "description_placeholders": {"url": f"http://u:{SECRET}@host:80/p?t={SECRET}#f"},
            }
        )

        text, data = await self._call(view, "configure_config_flow", {"flow_id": "f"})

        assert data["description_placeholders"] == {"url": "http://<redacted>@host:80/p"}
        assert SECRET not in text

    @pytest.mark.parametrize("source", ["reauth", "reconfigure"])
    async def test_configure_and_abort_refuse_existing_entry_flows(self, view, mock_hass, source):
        mock_hass.config_entries.flow.async_progress.return_value = [
            {"flow_id": "f1", "handler": "x", "context": {"source": source}}
        ]
        mock_hass.config_entries.flow.async_configure = AsyncMock()

        _, configured = await self._call(view, "configure_config_flow", {"flow_id": "f1"})
        _, aborted = await self._call(view, "abort_config_flow", {"flow_id": "f1"})

        assert source in configured["error"]
        assert source in aborted["error"]
        mock_hass.config_entries.flow.async_configure.assert_not_awaited()
        mock_hass.config_entries.flow.async_abort.assert_not_called()

    async def test_non_admin_is_refused_before_any_flow_runs(self, mock_hass):
        """The new tools sit behind the same administrator gate as every other tool."""
        mock_hass.data = {"mcp_server_http_transport": {"entry_id": Mock()}}
        mock_hass.auth.async_get_user = AsyncMock(
            return_value=Mock(id="user123", is_admin=False, is_active=True)
        )
        mock_hass.config_entries.flow.async_init = AsyncMock()
        view = MCPEndpointView(mock_hass)
        request = Mock(path="/api/mcp_http")
        request.headers = {"Authorization": "Bearer t"}
        request.json = AsyncMock(
            return_value={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "start_config_flow", "arguments": {"domain": "x"}},
                "id": 1,
            }
        )

        with patch.object(
            view, "_validate_token", return_value=ValidatedToken("user123", AUTH_METHOD_NATIVE)
        ):
            response = await view.post(request)

        assert response.status == 403
        mock_hass.config_entries.flow.async_init.assert_not_awaited()

    async def test_configure_is_audited_with_masked_input(self, view, mock_hass):
        """A mutating call is fired as an audit event; secret-named input is masked."""
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            return_value={"type": FlowResultType.ABORT, "reason": "x"}
        )

        await self._call(
            view,
            "configure_config_flow",
            {"flow_id": "f1", "user_input": {"host": "h", "password": SECRET}},
        )

        event, payload = mock_hass.bus.async_fire.call_args.args
        assert event == EVENT_TOOL_CALLED
        assert payload["tool"] == "configure_config_flow"
        assert payload["user_id"] == "user123"
        assert payload["arguments"]["user_input"] == {"host": "h", "password": "***"}
        assert SECRET not in json.dumps(payload)

    def test_abort_is_marked_destructive(self):
        assert TOOLS["abort_config_flow"]["schema"]["annotations"]["destructiveHint"] is True

    async def test_start_unknown_domain(self, view, mock_hass):
        mock_hass.config_entries.flow.async_init = AsyncMock(
            side_effect=data_entry_flow.UnknownHandler
        )

        _, data = await self._call(view, "start_config_flow", {"domain": "nope"})

        assert "No setup flow exists for integration 'nope'" in data["error"]

    async def test_start_redacts_secret_defaults_and_suggestions(self, view, mock_hass):
        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Required("password", default=SECRET): str,
                vol.Optional("access_token", description={"suggested_value": SECRET}): str,
            }
        )
        mock_hass.config_entries.flow.async_init = AsyncMock(return_value=self._form(schema))

        text, data = await self._call(view, "start_config_flow", {"domain": "x"})

        assert SECRET not in text
        fields = {f["name"]: f for f in data["data_schema"]}
        assert fields["password"]["default"] == "***"
        assert fields["access_token"]["description"]["suggested_value"] == "***"

    async def test_configure_success_creates_entry_without_data(self, view, mock_hass):
        entry = Mock()
        entry.entry_id = "entry123"
        entry.domain = "moonraker"
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            return_value={
                "type": FlowResultType.CREATE_ENTRY,
                "flow_id": "flow1",
                "handler": "moonraker",
                "title": "Printer",
                "data": {"host": "10.0.0.5", "api_key": SECRET},
                "result": entry,
            }
        )

        text, data = await self._call(
            view,
            "configure_config_flow",
            {"flow_id": "flow1", "user_input": {"host": "10.0.0.5", "api_key": SECRET}},
        )

        mock_hass.config_entries.flow.async_configure.assert_awaited_once_with(
            "flow1", {"host": "10.0.0.5", "api_key": SECRET}
        )
        assert data["type"] == "create_entry"
        assert data["entry"] == {"entry_id": "entry123", "title": "Printer", "domain": "moonraker"}
        assert SECRET not in text
        assert "10.0.0.5" not in text

    async def test_configure_error_returns_errors_without_secret(self, view, mock_hass):
        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Required("password", description={"suggested_value": SECRET}): str,
            }
        )
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            return_value=self._form(
                schema,
                errors={"base": "cannot_connect"},
                description_placeholders={"host": "10.0.0.5", "api_key": SECRET},
            )
        )

        text, data = await self._call(
            view,
            "configure_config_flow",
            {"flow_id": "flow1", "user_input": {"host": "10.0.0.5", "password": SECRET}},
        )

        assert data["type"] == "form"
        assert data["errors"] == {"base": "cannot_connect"}
        assert data["description_placeholders"] == {"host": "10.0.0.5", "api_key": "***"}
        assert SECRET not in text

    async def test_configure_without_user_input_sends_empty_dict(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            return_value={"type": FlowResultType.ABORT, "reason": "already_configured"}
        )

        await self._call(view, "configure_config_flow", {"flow_id": "flow1"})

        mock_hass.config_entries.flow.async_configure.assert_awaited_once_with("flow1", {})

    async def test_configure_unknown_flow(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            side_effect=data_entry_flow.UnknownFlow
        )

        _, data = await self._call(view, "configure_config_flow", {"flow_id": "gone"})

        assert "No setup flow in progress with flow_id 'gone'" in data["error"]

    async def test_configure_invalid_input_does_not_echo_values(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock(
            side_effect=vol.Invalid("extra keys not allowed @ data['bogus']")
        )

        text, data = await self._call(
            view, "configure_config_flow", {"flow_id": "flow1", "user_input": {"bogus": SECRET}}
        )

        assert "does not match" in data["error"]
        assert SECRET not in text

    async def test_configure_rejects_non_object_user_input(self, view, mock_hass):
        mock_hass.config_entries.flow.async_configure = AsyncMock()

        _, data = await self._call(
            view, "configure_config_flow", {"flow_id": "flow1", "user_input": "nope"}
        )

        assert "must be an object" in data["error"]
        mock_hass.config_entries.flow.async_configure.assert_not_awaited()

    async def test_abort(self, view, mock_hass):
        _, data = await self._call(view, "abort_config_flow", {"flow_id": "flow1"})

        mock_hass.config_entries.flow.async_abort.assert_called_once_with("flow1")
        assert data == {"aborted": True, "flow_id": "flow1"}

    async def test_abort_unknown_flow(self, view, mock_hass):
        mock_hass.config_entries.flow.async_abort.side_effect = data_entry_flow.UnknownFlow

        _, data = await self._call(view, "abort_config_flow", {"flow_id": "gone"})

        assert "No setup flow in progress with flow_id 'gone'" in data["error"]

    async def test_list_in_progress(self, view, mock_hass):
        mock_hass.config_entries.flow.async_progress.return_value = [
            {
                "flow_id": "f1",
                "handler": "hue",
                "step_id": "link",
                "context": {"source": "zeroconf"},
            }
        ]

        _, data = await self._call(view, "list_config_flows_in_progress", {})

        assert data == [{"flow_id": "f1", "domain": "hue", "step_id": "link", "source": "zeroconf"}]
