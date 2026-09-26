"""Run the real config and options flow code against small Home Assistant stand-ins.

Home Assistant is not installed for these tests. config_flow.py is imported as part of a
stand-in package (its __init__.py is not run), with stand-ins for the Home Assistant and
aiohttp names it uses. Music Assistant calls are replaced per test.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"
PACKAGE = "_mmf_config_flow_under_test"
TOKEN = "music_assistant_token"
URL = "music_assistant_url"


class AbortFlow(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ClientError(Exception):
    pass


class TextSelectorType:
    TEXT = "text"
    PASSWORD = "password"


class TextSelectorConfig(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class TextSelector:
    def __init__(self, config: TextSelectorConfig | None = None) -> None:
        self.config = config or TextSelectorConfig()

    def __call__(self, value: Any) -> Any:
        return value


class FlowBase:
    def async_show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def async_create_entry(self, *, title, data, options=None):
        return {"type": "create_entry", "title": title, "data": data, "options": options}


class ConfigFlow(FlowBase):
    def __init_subclass__(cls, domain: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def async_show_menu(self, *, step_id, menu_options):
        return {"type": "menu", "step_id": step_id, "menu_options": menu_options}

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        if self.unique_id in self.hass.configured_ids:
            raise AbortFlow("already_configured")


class OptionsFlow(FlowBase):
    pass


def _module(name: str, **attrs: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


_STUBS = {
    "aiohttp": _module("aiohttp", ClientError=ClientError, ClientTimeout=lambda **kwargs: kwargs),
    "homeassistant": _module("homeassistant"),
    "homeassistant.config_entries": _module(
        "homeassistant.config_entries",
        ConfigFlow=ConfigFlow,
        OptionsFlow=OptionsFlow,
        ConfigEntry=object,
        ConfigFlowResult=dict,
    ),
    "homeassistant.core": _module("homeassistant.core", callback=lambda func: func),
    "homeassistant.data_entry_flow": _module("homeassistant.data_entry_flow", AbortFlow=AbortFlow),
    "homeassistant.helpers": _module("homeassistant.helpers"),
    "homeassistant.helpers.aiohttp_client": _module(
        "homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: hass.session
    ),
    "homeassistant.helpers.selector": _module(
        "homeassistant.helpers.selector",
        TextSelector=TextSelector,
        TextSelectorConfig=TextSelectorConfig,
        TextSelectorType=TextSelectorType,
    ),
}


def load_config_flow() -> types.ModuleType:
    """Import config_flow.py with Home Assistant and aiohttp stand-ins."""
    name = f"{PACKAGE}.config_flow"
    if name in sys.modules:
        return sys.modules[name]
    installed = [stub for stub in _STUBS if stub not in sys.modules]
    sys.modules.update({stub: _STUBS[stub] for stub in installed})
    try:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(COMPONENT)]
        sys.modules[PACKAGE] = package
        return importlib.import_module(name)
    finally:
        for stub in installed:
            sys.modules.pop(stub, None)


CF = load_config_flow()


def schema_field(schema, key):
    """Return the validator of a voluptuous schema field by its key name."""
    return next(value for marker, value in schema.schema.items() if str(marker) == key)


def assert_password(test: IsolatedAsyncioTestCase, validator: Any) -> None:
    test.assertIsInstance(validator, TextSelector)
    test.assertEqual(validator.config.get("type"), TextSelectorType.PASSWORD)


class FlowTestCase(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.hass = SimpleNamespace(
            session=object(),
            configured_ids=set(),
            data={},
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
        )
        self.create_token = AsyncMock(return_value="dedicated-token")
        self.revoke_token = AsyncMock(return_value=True)
        self.check_server = AsyncMock(return_value=None)
        self.check_api = AsyncMock(return_value=None)
        patches = {
            "create_onboarding_token": self.create_token,
            "revoke_onboarding_token": self.revoke_token,
            "_validate_music_assistant_server": self.check_server,
            "_validate_music_assistant_api": self.check_api,
        }
        originals = {name: getattr(CF, name) for name in patches}
        for name, value in patches.items():
            setattr(CF, name, value)
        self.addCleanup(lambda: [setattr(CF, name, value) for name, value in originals.items()])

    def config_flow(self):
        flow = CF.HomeiiFlowConfigFlow()
        flow.hass = self.hass
        return flow

    async def automatic_login(self, url="http://ma.test:8095"):
        flow = self.config_flow()
        result = await flow.async_step_automatic({URL: url, "instance_id": "default"})
        return flow, result


class PasswordFieldTests(FlowTestCase):
    async def test_manual_token_field_is_a_password_selector(self):
        result = await self.config_flow().async_step_manual()
        assert_password(self, schema_field(result["data_schema"], TOKEN))

    async def test_options_token_field_is_a_password_selector(self):
        entry = SimpleNamespace(data={TOKEN: "stored", URL: "http://ma.test:8095"}, options={})
        flow = CF.HomeiiFlowOptionsFlow(entry)
        flow.hass = self.hass
        result = await flow.async_step_general()
        validator = schema_field(result["data_schema"], TOKEN)
        assert_password(self, validator)
        # The stored token is never offered back as the field's default.
        marker = next(marker for marker in result["data_schema"].schema if str(marker) == TOKEN)
        self.assertNotIn("stored", repr(getattr(marker, "default", None)))

    async def test_automatic_password_field_is_a_password_selector(self):
        _flow, result = await self.automatic_login()
        assert_password(self, schema_field(result["data_schema"], "password"))


class AutomaticSetupTests(FlowTestCase):
    async def test_server_is_validated_before_credentials_are_requested(self):
        self.check_server.return_value = "unsupported_ma_version"
        flow, result = await self.automatic_login()
        self.assertEqual(result["step_id"], "automatic")
        self.assertEqual(result["errors"], {"base": "unsupported_ma_version"})
        self.check_server.assert_awaited_once_with(self.hass, "http://ma.test:8095")
        self.create_token.assert_not_awaited()

    async def test_invalid_url_is_rejected_before_contacting_ma(self):
        for url, error in (("http://user:pw@ma.test:8095", "invalid_url"), ("http://ha:8123/api/hassio_ingress/x/", "ma_ingress_url")):
            _flow, result = await self.automatic_login(url)
            self.assertEqual(result["errors"], {URL: error})
        self.check_server.assert_not_awaited()
        self.create_token.assert_not_awaited()

    async def test_http_url_shows_the_unencrypted_warning_form(self):
        _flow, result = await self.automatic_login("http://ma.test:8095")
        self.assertEqual(result["step_id"], "automatic_login_http")
        _flow, result = await self.automatic_login("https://ma.test:8095")
        self.assertEqual(result["step_id"], "automatic_login")

    async def test_http_setup_is_still_allowed(self):
        flow, _result = await self.automatic_login("http://ma.test:8095")
        result = await flow.async_step_automatic_login_http({"username": "user", "password": "secret"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"][TOKEN], "dedicated-token")
        self.assertEqual(result["data"][URL], "http://ma.test:8095")
        self.assertNotIn(TOKEN, result["options"])
        self.assertNotIn("password", str(result))
        self.create_token.assert_awaited_once_with(self.hass.session, "http://ma.test:8095", "user", "secret")
        self.revoke_token.assert_not_awaited()

    async def test_failed_validation_after_token_creation_revokes_the_token(self):
        self.check_api.return_value = "invalid_auth"
        flow, _result = await self.automatic_login("https://ma.test:8095")
        result = await flow.async_step_automatic_login({"username": "user", "password": "secret"})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.check_api.assert_awaited_once_with(self.hass, "https://ma.test:8095", "dedicated-token")
        self.revoke_token.assert_awaited_once_with(self.hass.session, "https://ma.test:8095", "dedicated-token")

    async def test_each_failed_retry_revokes_its_own_token(self):
        self.check_api.return_value = "cannot_connect"
        flow, _result = await self.automatic_login()
        for token in ("first", "second"):
            self.create_token.return_value = token
            await flow.async_step_automatic_login_http({"username": "user", "password": "secret"})
        self.assertEqual([call.args[2] for call in self.revoke_token.await_args_list], ["first", "second"])

    async def test_unrevokable_token_still_reports_the_setup_error(self):
        self.check_api.return_value = "invalid_response"
        self.revoke_token.return_value = False
        flow, _result = await self.automatic_login()
        with self.assertLogs(CF._LOGGER, "WARNING") as logs:
            result = await flow.async_step_automatic_login_http({"username": "user", "password": "secret"})
        self.assertEqual(result["errors"], {"base": "invalid_response"})
        self.assertNotIn("dedicated-token", "".join(logs.output))

    async def test_duplicate_found_after_token_creation_revokes_the_token(self):
        flow, _result = await self.automatic_login()
        self.hass.configured_ids.add("default")
        with self.assertRaises(AbortFlow):
            await flow.async_step_automatic_login_http({"username": "user", "password": "secret"})
        self.revoke_token.assert_awaited_once_with(self.hass.session, "http://ma.test:8095", "dedicated-token")

    async def test_failed_sign_in_creates_nothing_to_revoke(self):
        self.create_token.side_effect = ValueError("automatic_login_failed")
        flow, _result = await self.automatic_login()
        result = await flow.async_step_automatic_login_http({"username": "user", "password": "bad"})
        self.assertEqual(result["errors"], {"base": "automatic_login_failed"})
        self.check_api.assert_not_awaited()
        self.revoke_token.assert_not_awaited()


class ManualSetupTests(FlowTestCase):
    async def test_token_is_stored_in_data_only(self):
        result = await self.config_flow().async_step_manual(
            {"instance_id": "default", URL: "http://ma.test:8095", TOKEN: " manual-token "}
        )
        self.assertEqual(result["data"][TOKEN], "manual-token")
        self.assertNotIn(TOKEN, result["options"])


class OptionsTokenTests(FlowTestCase):
    def options_flow(self, options=None):
        self.entry = SimpleNamespace(
            data={"instance_id": "default", URL: "http://ma.test:8095", TOKEN: "stored-token"},
            options=dict(options or {}),
        )
        flow = CF.HomeiiFlowOptionsFlow(self.entry)
        flow.hass = self.hass
        return flow

    async def test_new_token_updates_entry_data_not_options(self):
        result = await self.options_flow({"profile_id": "default"}).async_step_general(
            {URL: "http://ma.test:8095", TOKEN: "new-token"}
        )
        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn(TOKEN, result["data"])
        self.check_api.assert_awaited_once_with(self.hass, "http://ma.test:8095", "new-token")
        update = self.hass.config_entries.async_update_entry
        update.assert_called_once()
        self.assertIs(update.call_args.args[0], self.entry)
        self.assertEqual(update.call_args.kwargs["data"][TOKEN], "new-token")
        self.assertEqual(update.call_args.kwargs["data"]["instance_id"], "default")
        # Options are saved in the same update, so the entry reloads only once.
        self.assertEqual(update.call_args.kwargs["options"], result["data"])

    async def test_blank_token_keeps_the_stored_token(self):
        result = await self.options_flow().async_step_general({URL: "http://ma.test:8095", TOKEN: ""})
        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn(TOKEN, result["data"])
        self.check_api.assert_awaited_once_with(self.hass, "http://ma.test:8095", "stored-token")
        self.hass.config_entries.async_update_entry.assert_not_called()

    async def test_invalid_new_token_is_not_saved(self):
        self.check_api.return_value = "invalid_auth"
        result = await self.options_flow().async_step_general({URL: "http://ma.test:8095", TOKEN: "bad"})
        self.assertEqual(result["errors"], {"base": "invalid_auth"})
        self.hass.config_entries.async_update_entry.assert_not_called()


if __name__ == "__main__":
    main()
