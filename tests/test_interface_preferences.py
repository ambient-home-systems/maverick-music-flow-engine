import ast
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

source = (
    Path(__file__).resolve().parents[1]
    / "custom_components/maverick_music_flow/interface_preferences.py"
)
tree = ast.parse(source.read_text(encoding="utf-8"))

_limits_src = (
    Path(__file__).resolve().parents[1] / "custom_components/maverick_music_flow/storage_limits.py"
)
_limits_ns = {"__name__": "storage_limits", "json": json, "re": re}
exec(compile(_limits_src.read_text(encoding="utf-8"), str(_limits_src), "exec"), _limits_ns)
ns = {
    "copy": copy,
    "re": re,
    "DEFAULT_PROFILE_ID": "default",
    **{k: v for k, v in _limits_ns.items() if not k.startswith("__")},
}
exec(
    compile(
        ast.Module(
            body=[
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ],
            type_ignores=[],
        ),
        str(source),
        "exec",
    ),
    ns,
)


class InterfacePreferencesTests(IsolatedAsyncioTestCase):
    async def test_profile_isolation_partial_updates_and_persistence(self):
        runtime = SimpleNamespace(_storage={}, async_save=AsyncMock())
        await ns["save_preferences"](
            runtime,
            {
                "profile_id": "tablet",
                "night_mode": "auto",
                "night_start": "22:30",
                "night_days": [0, 1],
            },
            True,
        )
        await ns["save_preferences"](runtime, {"profile_id": "tablet", "night_end": "06:00"})
        self.assertEqual(ns["read_preferences"](runtime, "tablet")["night_start"], "22:30")
        self.assertEqual(ns["read_preferences"](runtime, "phone"), {})
        self.assertEqual(runtime.async_save.await_count, 2)

    async def test_validation_and_failed_save_never_replace_working_settings(self):
        runtime = SimpleNamespace(_storage={}, async_save=AsyncMock())
        await ns["save_preferences"](runtime, {"night_mode": "off"})
        for payload in (
            {"night_mode": "bad"},
            {"night_start": "25:00"},
            {"night_days": [True]},
            {"night_days": [7]},
        ):
            with self.assertRaises(ValueError):
                await ns["save_preferences"](runtime, payload)
        runtime.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await ns["save_preferences"](runtime, {"night_mode": "on"})
        self.assertEqual(ns["read_preferences"](runtime), {"night_mode": "off"})

    async def test_wheel_user_isolation_and_global_permissions(self):
        runtime = SimpleNamespace(_storage={}, async_save=AsyncMock())
        data = {
            "scope": "user",
            "context": "main",
            "preference": {"hidden": ["queue"], "order": ["play", "queue"]},
        }
        await ns["save_wheel_preferences"](runtime, data, "alice")
        self.assertEqual(ns["read_wheel_preferences"](runtime, None, "bob")["user"], {})
        self.assertEqual(
            ns["read_wheel_preferences"](runtime, None, "alice")["user"]["main"]["hidden"],
            ["queue"],
        )
        with self.assertRaises(ValueError):
            await ns["save_wheel_preferences"](runtime, {**data, "scope": "global"}, "alice")
        await ns["save_wheel_preferences"](runtime, {**data, "scope": "global"}, "admin", True)
        self.assertIn("main", ns["read_wheel_preferences"](runtime, None, "bob")["global"])
        before = copy.deepcopy(runtime._storage)
        runtime.async_save.side_effect = RuntimeError("disk")
        with self.assertRaises(RuntimeError):
            await ns["save_wheel_preferences"](runtime, {**data, "context": "queue"}, "alice")
        self.assertEqual(runtime._storage, before)


class StorageLimitTests(IsolatedAsyncioTestCase):
    def runtime(self):
        return SimpleNamespace(_storage={}, async_save=AsyncMock())

    async def test_wheel_context_limit_per_user(self):
        runtime = self.runtime()
        for i in range(ns["MAX_WHEEL_CONTEXTS"]):
            await ns["save_wheel_preferences"](
                runtime, {"context": f"c{i}", "preference": {"order": ["a"]}}, "alice"
            )
        with self.assertRaisesRegex(ValueError, "context limit"):
            await ns["save_wheel_preferences"](
                runtime, {"context": "extra", "preference": {"order": ["a"]}}, "alice"
            )
        # updating an existing context and another user are still fine
        await ns["save_wheel_preferences"](
            runtime, {"context": "c0", "preference": {"order": ["b"]}}, "alice"
        )
        await ns["save_wheel_preferences"](
            runtime, {"context": "extra", "preference": {"order": ["a"]}}, "bob"
        )

    async def test_wheel_preference_byte_cap(self):
        runtime = self.runtime()
        big = [
            f"{i:03d}" + "x" * 253 for i in range(200)
        ]  # within per-list limits, over the byte cap
        with self.assertRaisesRegex(ValueError, "too large"):
            await ns["save_wheel_preferences"](
                runtime, {"context": "main", "preference": {"order": big}}, "alice"
            )
        self.assertEqual(runtime._storage, {})

    def test_interface_preference_byte_cap(self):
        ns["MAX_INTERFACE_PREFERENCE_BYTES"] = 20
        try:
            with self.assertRaisesRegex(ValueError, "too large"):
                ns["validate_preferences"]({"night_start": "22:00", "night_end": "07:00"}, {})
        finally:
            ns["MAX_INTERFACE_PREFERENCE_BYTES"] = 4 * 1024

    async def test_invalid_and_new_profiles(self):
        runtime = self.runtime()
        for bad in ["UPPER", "has space", "a" * 33, "../x"]:
            for call in (
                ns["save_preferences"](runtime, {"profile_id": bad, "night_mode": "on"}, True),
                ns["save_wheel_preferences"](
                    runtime,
                    {"profile_id": bad, "context": "m", "preference": {}},
                    "alice",
                    True,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "Invalid profile ID"):
                    await call
        with self.assertRaisesRegex(ValueError, "administrator"):
            await ns["save_preferences"](runtime, {"profile_id": "den", "night_mode": "on"})
        await ns["save_preferences"](runtime, {"profile_id": "den", "night_mode": "on"}, True)
        await ns["save_preferences"](runtime, {"profile_id": "den", "night_mode": "off"})
        await ns["save_preferences"](runtime, {"night_mode": "on"})  # default profile
        self.assertEqual(
            runtime._storage,
            {
                "interface_preferences": {
                    "den": {"night_mode": "off"},
                    "default": {"night_mode": "on"},
                }
            },
        )
