# HOMEii Flow Engine 1.0.0

> [!CAUTION]
> **BREAKING CHANGE FOR HOMEii Music Flow 5.9.3 USERS:** HOMEii Music Flow 6.0.0 requires this Engine. Install and configure Engine 1.0.0 first, restart Home Assistant, verify the integration is healthy, and only then upgrade the card to 6.0.0. Do not install the card first or mix beta and stable component versions.

HOMEii Flow Engine is the Home Assistant custom integration behind HOMEii Music Flow 6. It provides one persistent, local connection to Music Assistant for playback, queue, library, artwork, groups, schedules, timers, diagnostics, preferences, statistics and Home Assistant automations.

## Install

1. Back up Home Assistant and the dashboard configuration.
2. In HACS, add `https://github.com/r11a/homeii-flow-engine` as a custom **Integration** repository.
3. Install HOMEii Flow Engine 1.0.0 and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → HOMEii Flow Engine**.
5. Enter the direct Music Assistant server URL, normally on port 8095. Do not use the HA ingress page.
6. Sign in with Music Assistant credentials or paste a Music Assistant profile API token.
7. Leave Instance ID and Default profile ID as `default` for a normal single-instance setup.
8. Verify the integration loads successfully, then install HOMEii Music Flow card 6.0.0.

## Highlights

- persistent Music Assistant connection and capability discovery
- playback, seek, queue, favorites, playlists, groups and player control
- cached library, artwork and media details with fast stale-while-revalidate responses
- schedules, timers, announcements, listening statistics and diagnostics
- persistent wheel/interface preferences and saved playlists
- artwork lighting that continues while the dashboard card is closed
- Home Assistant entities and services for automations
- onboarding validation for direct MA URLs, schema compatibility and tokens

## Compatibility

- Home Assistant 2025.1 or newer
- Music Assistant API schema 63 or newer
- HOMEii Music Flow card 6.0.0

Full installation and rollback instructions: https://github.com/r11a/homeii-music-flow/blob/v6.0.0/docs/INSTALL_STEP_BY_STEP.md
