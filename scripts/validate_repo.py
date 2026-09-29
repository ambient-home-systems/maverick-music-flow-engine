"""Lightweight repository validation for Maverick Music Flow Engine."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "maverick_music_flow"
COMMANDS = {
    "maverick_music_flow/bootstrap/get",
    "maverick_music_flow/get_context",
    "maverick_music_flow/events/subscribe",
    "maverick_music_flow/diagnostics/run",
    "maverick_music_flow/stats/get",
    "maverick_music_flow/playback_stats/get",
    "maverick_music_flow/orchestration/status",
    "maverick_music_flow/orchestration/run_once",
    "maverick_music_flow/queue/get",
    "maverick_music_flow/queue/action",
    "maverick_music_flow/queue/transfer",
    "maverick_music_flow/library/get",
    "maverick_music_flow/search/get",
    "maverick_music_flow/players/get",
    "maverick_music_flow/playback/play_media",
    "maverick_music_flow/player/command",
    "maverick_music_flow/ma/command",
    "maverick_music_flow/favorites/get",
    "maverick_music_flow/favorites/set",
    "maverick_music_flow/group/apply",
    "maverick_music_flow/schedules/get",
    "maverick_music_flow/schedules/set",
    "maverick_music_flow/schedules/delete",
    "maverick_music_flow/schedules/run",
    "maverick_music_flow/timers/get",
    "maverick_music_flow/timers/set",
    "maverick_music_flow/timers/delete",
    "maverick_music_flow/volume_rules/get",
    "maverick_music_flow/volume_rules/set",
    "maverick_music_flow/volume_rules/delete",
    "maverick_music_flow/volume_rules/clear",
    "maverick_music_flow/announce",
    "maverick_music_flow/activity/get",
    "maverick_music_flow/screensaver/get",
    "maverick_music_flow/screensaver/set",
    "maverick_music_flow/screensaver/show",
    "maverick_music_flow/sendspin/status",
}


def load_json(path: Path) -> dict:
    """Load a JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    """Validate repo structure."""
    component = ROOT / "custom_components" / DOMAIN
    required_files = [
        component / "__init__.py",
        component / "manifest.json",
        component / "config_flow.py",
        component / "runtime.py",
        component / "websocket_api.py",
        component / "diagnostics.py",
        component / "binary_sensor.py",
        component / "button.py",
        component / "calendar.py",
        component / "number.py",
        component / "sensor.py",
        component / "switch.py",
        component / "services.yaml",
        component / "icon.png",
        component / "logo.png",
        component / "frontend" / "maverick-music-flow-system-screensaver.js",
        component / "frontend" / "maverick-music-flow-icon.png",
        component / "frontend" / "maverick-music-flow-logo.png",
        component / "frontend" / "maverick-music-flow-logo-dark.png",
        component / "translations" / "en.json",
        ROOT / "icon.png",
        ROOT / "logo.png",
        ROOT / "hacs.json",
        ROOT / "README.md",
        ROOT / "LICENSE",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required_files if not path.exists()]
    if missing:
        raise SystemExit(f"Missing required files: {', '.join(missing)}")

    manifest = load_json(component / "manifest.json")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if project.get("version") != manifest.get("version"):
        raise SystemExit("pyproject.toml and manifest.json versions must match")
    if manifest.get("domain") != DOMAIN:
        raise SystemExit("manifest.json domain does not match folder name")
    if not manifest.get("version"):
        raise SystemExit("manifest.json must include version for a custom integration")
    # http: the Engine registers HTTP views and static paths on hass.http.
    if manifest.get("dependencies") != ["http", "music_assistant"]:
        raise SystemExit("manifest.json must require the http and Music Assistant integrations")
    if manifest.get("config_flow") is not True:
        raise SystemExit("manifest.json must enable config_flow")

    hacs = load_json(ROOT / "hacs.json")
    if not hacs.get("name"):
        raise SystemExit("hacs.json must set name")
    # HACS validates hacs.json against a fixed key schema and rejects unknown keys (for
    # example "domains", which was removed after hacs/action failed CI with "extra keys
    # not allowed @ data['domains']"). For an integration repository HACS finds the
    # domain itself from custom_components/, so no such key is needed here.
    allowed_hacs_keys = {
        "name",
        "render_readme",
        "content_in_root",
        "zip_release",
        "filename",
        "homeassistant",
        "country",
        "persistent_directory",
        "hide_default_branch",
    }
    unknown_hacs_keys = sorted(set(hacs) - allowed_hacs_keys)
    if unknown_hacs_keys:
        raise SystemExit(
            f"hacs.json has keys HACS does not recognize: {', '.join(unknown_hacs_keys)}"
        )

    ws_text = (component / "websocket_api.py").read_text(encoding="utf-8")
    missing_commands = sorted(command for command in COMMANDS if command not in ws_text)
    if missing_commands:
        raise SystemExit(f"Missing websocket commands: {', '.join(missing_commands)}")

    const_text = (component / "const.py").read_text(encoding="utf-8")
    runtime_text = (component / "runtime.py").read_text(encoding="utf-8")
    if f'VERSION = "{manifest["version"]}"' not in const_text:
        raise SystemExit("const.py and manifest.json versions must match")

    # These are bans on specific legacy code paths that were removed on purpose (they were
    # unreliable or incompatible with Music Assistant 2.10), not markers that lock in one
    # implementation of a still-required feature, so a string check is appropriate here.
    forbidden_runtime_paths = {
        "Home Assistant library fallback": 'async_call_service_response("music_assistant", "get_library"',
        "Home Assistant queue fallback": 'async_call_service_response("music_assistant", "get_queue"',
        "legacy mass_queue fallback": '"domain": "mass_queue"',
        "schedule media_play fallback": "fallback_action",
    }
    for label, marker in forbidden_runtime_paths.items():
        if marker in runtime_text:
            raise SystemExit(f"Forbidden Maverick Music Flow runtime path remains: {label}")

    # Behavior that used to be enforced here by grepping for private attribute names and
    # capability-dict literals (for example "hashlib.blake2s" or "_queue_inflight") is
    # covered by real tests instead, since a string match only proves the text is present
    # somewhere in the file, not that the feature works:
    #   - the card-facing capability contract: tests/ha/test_websocket_api.py
    #     (test_get_context, REQUIRED_CAPABILITIES)
    #   - deterministic, unguessable artwork tokens: tests/test_artwork_proxy.py
    #   - request coalescing and stale-while-revalidate caching: tests/test_command_bridge.py
    #   - queue in-flight request cleanup: tests/test_lifecycle.py
    #   - the Music Assistant 2.10 radio library path (plural "radios"):
    #     tests/ha/test_websocket_api.py (test_library_get_radio_uses_the_plural_ma_command_path)
    #   - the MA server-info handshake and contract probes: tests/ha/test_websocket_api.py
    #     (test_bootstrap_and_players; async_bootstrap_snapshot fails if either step fails)
    #   - snapshot ordering metadata (epoch/revision): tests/ha/test_websocket_api.py
    #     (test_queue_get, test_library_get)

    print("Maverick Music Flow Engine repo validation passed.")


if __name__ == "__main__":
    main()
