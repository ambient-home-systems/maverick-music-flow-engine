"""Lightweight repository validation for HOMEii Flow Engine."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "maverick_music_flow"
COMMANDS = {
    "maverick_music_flow/bootstrap/get",
    "maverick_music_flow/get_context",
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
        component / "frontend" / "homeii-flow-icon.png",
        component / "frontend" / "homeii-flow-logo.png",
        component / "translations" / "en.json",
        ROOT / "icon.png",
        ROOT / "logo.png",
        ROOT / "hacs.json",
        ROOT / "README.md",
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
    if DOMAIN not in hacs.get("domains", []):
        raise SystemExit("hacs.json must list the integration domain")

    ws_text = (component / "websocket_api.py").read_text(encoding="utf-8")
    missing_commands = sorted(command for command in COMMANDS if command not in ws_text)
    if missing_commands:
        raise SystemExit(f"Missing websocket commands: {', '.join(missing_commands)}")

    const_text = (component / "const.py").read_text(encoding="utf-8")
    runtime_text = (component / "runtime.py").read_text(encoding="utf-8")
    if f'VERSION = "{manifest["version"]}"' not in const_text:
        raise SystemExit("const.py and manifest.json versions must match")
    forbidden_runtime_paths = {
        "singular radio library command": '"radio": ["radio"]',
        "Home Assistant library fallback": 'async_call_service_response("music_assistant", "get_library"',
        "Home Assistant queue fallback": 'async_call_service_response("music_assistant", "get_queue"',
        "legacy mass_queue fallback": '"domain": "mass_queue"',
        "schedule media_play fallback": 'fallback_action',
    }
    for label, marker in forbidden_runtime_paths.items():
        if marker in runtime_text:
            raise SystemExit(f"Forbidden HOMEii Flow 6 runtime path remains: {label}")
    required_performance_contract = {
        "persistent media detail capability": '"persistent_media_detail_cache": True',
        "stable artwork capability": '"stable_artwork_urls": True',
        "artwork ETag capability": '"artwork_etag": True',
        "compatible shelf capability": '"compatible_library_shelves": True',
        "compact library capability": '"compact_library_responses": True',
        "revisioned snapshot capability": '"revisioned_snapshots": True',
        "active source capability": '"active_source_contract": True',
        "favorite mutation capability": '"favorite_mutation": True',
        "Music Assistant 2.10 capability": '"music_assistant_2_10": True',
        "Music Assistant schema 63 capability": '"music_assistant_schema_63": True',
        "Music Assistant WebSocket command capability": '"music_assistant_websocket_commands": True',
        "Music Assistant radio library path": '"radio": ["radios"]',
        "direct catalog capability": '"direct_library_catalog": True',
        "direct player capability": '"direct_player_catalog": True',
        "full queue capability": '"full_queue_snapshots": True',
        "queue autoplay capability": '"queue_autoplay": True',
        "detail request coalescing": "_media_command_inflight",
        "queue request coalescing": "_queue_inflight",
        "detail stale-while-revalidate cache": "_media_command_cache",
        "deterministic artwork tokens": "hashlib.blake2s",
        "compatible larger shelf reuse": "_library_cache_entry",
        "compact library responses": "_library_response",
        "snapshot epoch": "_snapshot_epoch",
        "stale response ordering": "_snapshot_meta",
        "server info handshake": "_async_music_assistant_server_info",
        "required contract probes": "_async_music_assistant_contract_probe",
        "native player identity map": "_ma_players_by_entity",
        "preferred API endpoint": "_ma_preferred_base_url",
        "provider discovery cache": "_provider_ids_cache",
    }
    for label, marker in required_performance_contract.items():
        source = const_text if "capability" in label else runtime_text
        if marker not in source:
            raise SystemExit(f"Missing 0.7.17 state/performance contract: {label}")

    print("HOMEii Flow Engine repo validation passed.")


if __name__ == "__main__":
    main()
