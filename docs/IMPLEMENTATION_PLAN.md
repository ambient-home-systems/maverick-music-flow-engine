# Implementation Plan: Security, Reliability and Cleanup

This plan turns the September 2026 code review of this fork into ordered work items, each with a
ready-to-paste AI prompt. It covers **every** finding from that review, including the lower-priority
items. Phase 0 (removing non-English content) is already done in
[PR #2](https://github.com/ambient-home-systems/maverick-music-flow-engine/pull/2).

- Reviewed commit: `0e615fe` (fork point). Line numbers below refer to commit `135d50a` and are
  approximate (`~`). Function and class names are the stable reference if lines move.
- Test status at review time: 84 unit tests pass, `scripts/validate_repo.py` passes,
  `compileall` passes.

---

## 1. How to use this plan

1. Work through the phases in order. Each prompt is one branch and one pull request.
2. Open a Claude Code session in this repository and paste the prompt. Every prompt starts by
   telling the AI to read **Section 2 (Working rules)** of this file.
3. Status labels on each finding:
   - **Verified**: confirmed by reading the code during the review.
   - **Reported**: found by a review agent and not re-checked line by line. The prompt tells the AI
     to confirm it before changing anything.
4. Optional reorder: run **Prompt 5.1** (Home Assistant test harness) first if you want every later
   fix covered by real Home Assistant tests instead of the existing function-level tests.

### Mapping to the review's suggested order

| Review step | Plan phase |
|---|---|
| 1. Lock down the artwork proxy | Phase 1 |
| 2. Narrow the command bridge and add admin checks | Phase 2 |
| 3. Fix shutdown on unload and the slider deletion | Phase 3 |
| 4. Redact diagnostics and make the token a password field | Phase 4 |
| 5. Add HA-based tests and the missing CI checks | Phase 5 |
| 6. Fix the schedule, timer, volume-window and calendar bugs | Phase 6 (remaining bugs in Phases 7 and 8) |
| 7. Add a LICENSE file | Phase 10 (Prompt 10.1; can be done any time, it is a human decision) |
| Code structure (runtime.py size, duplicate schedulers, dead code) | Phase 9 |

---

## 2. Working rules (every prompt follows these)

1. **Branching.** Start from the latest default branch (`codex/v6-engine-candidate`) after PR #2
   is merged. Use one branch and one PR per prompt, named `fix/<prompt-id>-<short-name>`
   (for example `fix/1.1-artwork-proxy`).
2. **Confirm first.** Before editing, read the files and functions named in the prompt and confirm
   the finding still applies. If the code has changed or the finding does not reproduce, stop and
   report what you found instead of changing code.
3. **Stay in scope.** Only fix what the prompt describes. List any other problems you notice in the
   PR description; do not fix them in the same PR.
4. **Card compatibility.** The HOMEii Music Flow card (github.com/r11a/homeii-music-flow) calls this
   integration's WebSocket commands and HTTP views and reads their response fields. Do not rename
   commands or remove response fields. If a change must break that contract, say so clearly in the
   PR.
5. **Tests.** Add or update tests for every behavior change. Until Prompt 5.1 lands, follow the
   existing style in `tests/` (unittest, extracting functions from the source). After it lands,
   prefer `pytest-homeassistant-custom-component` tests.
6. **Checks before committing.**
   - `python -B -m unittest discover -s tests`
   - `python -B scripts/validate_repo.py`
   - `python -m compileall -q custom_components/maverick_music_flow`
   - After Prompt 5.2: `ruff check .`, `ruff format --check .` and `pytest`
7. **validate_repo.py markers.** `scripts/validate_repo.py` requires specific strings in the source
   (for example `hashlib.blake2s`, `_queue_inflight`, every WebSocket command name). If your change
   legitimately removes one, update the script in the same PR and explain why.
8. **Docs.** Update `README.md`, `services.yaml`, `strings.json` and `translations/en.json` when
   user-visible behavior changes. English only.
9. **Security defaults.** Deny by default. Never log tokens, passwords, or full URLs with query
   strings.
10. **PR description.** List the finding IDs addressed, how you confirmed them, what changed, test
    results, and any compatibility impact.

---

## 3. Findings index

Every finding from the review, with where it is handled.

### Non-English content (done)

| ID | Finding | Status | Where |
|---|---|---|---|
| N-1 | `docs/BETA_UPGRADE_HE.md` Hebrew upgrade guide | Done | PR #2 (deleted) |
| N-2 | README header link to the Hebrew guide | Done | PR #2 |
| N-3 | `RELEASE_NOTES_1.0.0-beta.1.md` link to the Hebrew guide | Done | PR #2 |
| N-4 | Hebrew check in `_preferred_announcement_say_service` (had no effect) | Done | PR #2 |
| N-5 | Test used TTS language code `he` | Done | PR #2 (now `en`) |
| N-6 | Scan of all tracked files: no non-English text remains; logos only say "HOMEii FLOW" | Done | PR #2 |

### Security

| ID | Finding | Severity | Status | Prompt |
|---|---|---|---|---|
| S-1 | Artwork proxy: no login on item view, guessable tokens, follows redirects, no size cap, passes upstream content type, uses caller-controlled Host header, sends MA token on `/imageproxy` rewrites. Enables stored XSS on the HA site, SSRF with readable responses, and memory exhaustion | Critical | Verified | 1.1 |
| S-2 | MA command bridge allows whole command prefixes with the Engine's MA token; substring denylist for `music/` lets `music/start_sync` and `music/add_item_to_library` through; HTTP view passes internal cache flags; HTTP "read" view also exposes writes | High | Verified | 2.1 |
| S-3 | No admin checks on ~50 WebSocket commands; any HA user can change schedules, volume limits, lighting, announcements, playlists; HA entity permissions bypassed | High | Verified | 2.2 |
| S-4 | Diagnostics not redacted (image links with access tokens, login-free artwork links, error strings, names, titles) | Medium | Reported | 4.1 |
| S-5 | MA can be made to fetch any address (announcement messages starting with `http`, `play_media` with any URL) | Medium | Verified | 2.3 |
| S-6a | Library and search caches grow without limit | Medium | Reported | 7.2 |
| S-6b | Artwork content cache up to ~960 MB (192 x 5 MB) | Medium | Verified | 7.2 |
| S-6c | Saved playlists, wheel settings and profile IDs have no count limits | Medium | Reported | 7.3 |
| S-6d | MA WebSocket accepts messages of any size (`max_msg_size=0`); partial results uncapped | Medium | Verified | 7.2 |
| S-7a | MA token field shown in plain text in setup and options screens | Low | Verified | 4.2 |
| S-7b | Failed automatic setup leaves the created MA token behind | Low | Verified | 4.2 |
| S-7c | Temporary MA login session from automatic setup is never revoked | Low | Verified | 4.2 |
| S-7d | MA username/password sent over unencrypted `ws://` when the URL is `http://` (no warning) | Low | Verified | 4.2 |
| S-7e | Token stored twice (config entry data and options) | Low | Verified | 4.2 |
| S-8 | Sendspin relay: any authenticated user can connect with any `client_id` | Low | Verified | 2.4 |
| S-9 | Runtime, WebSocket commands and HTTP views (including the login-free artwork view) start in `async_setup` even with no config entry, and stay registered after removal | Low | Verified | 3.1 |

### Bugs

| ID | Finding | Severity | Status | Prompt |
|---|---|---|---|---|
| B-1 | Switch cleanup deletes the volume-rule "Max volume" sliders; they never come back | High | Verified | 3.2 |
| B-2 | Removing or disabling the integration does not stop schedules, volume limits, artwork lighting, MA connection or background tasks | High | Verified | 3.1 |
| B-3 | Schedules with `enqueue` add/next repeat the media 4x by default (up to 12x) and are logged as failed | Medium | Verified | 6.1 |
| B-4 | Queue (and library) in-flight requests stick forever after a caller is cancelled; stale data or old errors served indefinitely | Medium | Verified (queue), Reported (library) | 7.1 |
| B-5 | Calendar "next event" is storage order, not soonest; never shows "on" during an event; range queries miss in-progress events | Medium | Verified | 6.6 |
| B-6 | Overnight volume windows with chosen days apply on the wrong days; end minute inclusive | Medium | Verified | 6.3 |
| B-7a | Every MA event, including per-second progress, is fired on the HA bus and recorded | Medium | Verified | 7.4 |
| B-7b | Status sensor attributes are the whole context (large, changes every call) and are re-saved several times a minute | Medium | Reported | 7.4 |
| B-7c | ~20 sensor/binary_sensor values each walk all players and the entity registry on every update | Medium | Reported | 7.4 |
| B-7d | Playback sensors use `force_update=True` | Low | Verified | 7.4 |
| B-7e | Every activity record rewrites the whole store | Low | Reported | 7.4 |
| B-8 | Timers can be silently dropped or resurrected when two are handled at once | Medium | Reported | 6.2 |
| B-9 | Alarms fail if a speaker was off when HA started (stale MA player cache); offline players treated as ready | Medium | Reported | 6.5 |
| B-10 | `volume: 1` means 100%, not 1% | Low | Reported | 8.1 |
| B-11 | Schedules at times skipped by the daylight-saving change never fire | Low | Reported | 6.3 |
| B-12 | A schedule can play twice if HA restarts right after it runs | Low | Reported | 6.4 |
| B-13 | `after_run=disable` overwrites edits made while the schedule was retrying | Low | Reported | 6.4 |
| B-14 | Reading playback statistics changes state, so playback started/stopped activity is mostly never logged | Medium | Reported | 7.5 |
| B-15 | Library cache ignores MA's `media_item_added/updated/deleted` events; stale results up to 10 min | Medium | Reported | 7.5 |
| B-16 | `join` player command can never pass group members | Low | Reported | 8.1 |
| B-17 | Queue fetch ignores `limit_before`/`limit_after` and pages the whole queue on every open | Low | Reported | 7.6 |
| B-18 | Radio search: failed fetch reused for the next caller; bare `asyncio.create_task` | Low | Reported | 7.1 |
| B-19 | Artwork lighting drops state changes while an update is running (up to 10 s lag) | Low | Reported | 8.4 |
| B-20 | Screensaver never sends `profile_id`, so non-default profile settings are ignored | Low | Verified | 8.2 |
| B-21 | Screensaver `<img>` points at an auth-only view without credentials (may always fail; untested) | Low | Reported | 8.2 |
| B-22 | Screensaver loads raw third-party artwork URLs directly in the browser | Low | Reported | 8.2 |
| B-23 | `next_schedule`/`next_timer` sensors lack the TIMESTAMP device class | Low | Reported | 8.3 |
| B-24 | `playback_today` uses MEASUREMENT instead of TOTAL_INCREASING | Low | Reported | 8.3 |
| B-25 | Diagnostics key lists are stale (list `run_schedule`; miss `show_system_screensaver_now`, `system_screensaver_timeout`) | Low | Reported (`run_schedule` confirmed) | 4.1 |
| B-26 | `_extract_media_items` has no depth limit and is O(3^depth) | Low | Reported | 7.6 |
| B-27 | `_ha_entity_for_ma_player` scans the entity registry and rebuilds the player snapshot per MA player | Low | Reported | 7.6 |
| B-28 | Artwork token eviction is first-in-first-out, so tokens in active use are evicted first | Low | Reported | 7.2 |
| B-29 | No `CONFIG_SCHEMA` although `async_setup` exists (hassfest warning) | Low | Verified | 3.1 |

### Project housekeeping

| ID | Finding | Status | Prompt |
|---|---|---|---|
| H-1 | No LICENSE file, although `pyproject.toml` says MIT | Verified | 10.1 |
| H-2 | Fork still points at the original author (code owners, README links, HOMEii branding, names); README has stale beta/private-repo text | Verified | 10.2 |
| H-3 | Tests never load Home Assistant; no coverage of entities, HTTP views, diagnostics, config flow | Verified | 5.1 |
| H-4 | CI has no hassfest or HACS validation; ruff configured but never run; no type checking | Verified | 5.2 |
| H-5a | `runtime.py` is ~6,300 lines | Verified | 9.3 |
| H-5b | Three separate schedulers can trigger the same schedule | Reported | 9.2 |
| H-5c | Dead code: `screensaver_music_assistant_base_urls`, `players_snapshot`, `_clear_schedule_jobs` | Reported | 9.1 |
| H-5d | `runner_retry_attempts`/`runner_retry_delay` read but never stored | Reported | 9.1 |
| H-5e | Unreachable `raise` in `async_queue_action` | Reported | 9.1 |
| H-5f | `after_run=disable` logic duplicated in `runtime.py` and `switch.py` | Reported | 6.4 |
| H-6 | `validate_repo.py` enforces implementation strings (e.g. `hashlib.blake2s`) that lock in current design | Verified | 5.2 (and every prompt, rule 7) |

### Checked and fine (no action needed)

- The MA token is never logged or sent to the browser: `EngineEntry.music_assistant_token` uses
  `repr=False`, and `EngineEntry.as_dict()` and `MusicAssistantEventClient.snapshot()` only report
  booleans. Bus events and activity data carry no credentials.
- No secrets anywhere in the git history (all commits scanned; only example LAN addresses such as
  `192.168.1.10:8095`).
- The screensaver script uses `innerHTML` only for a static template; track metadata is set with
  `textContent`, and `cssImageUrl` escapes quotes and backslashes.
- Image processing (Pillow) runs off the event loop and has an 8 MB / 20-megapixel guard.
- The Sendspin relay keeps the MA token on the server (see S-8 for the client ID issue).
- Artwork lighting validates that the player is an existing `media_player` and each light is an
  existing `light` entity.
- The MA WebSocket client reconnects with backoff and fails pending commands on disconnect.

---

## 4. Phases and prompts

### Phase 0: Remove non-English content (done)

Completed in [PR #2](https://github.com/ambient-home-systems/maverick-music-flow-engine/pull/2):
N-1 to N-6. No prompt needed.

---

### Phase 1: Lock down the artwork proxy

#### Prompt 1.1: Harden both artwork proxy views (S-1)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: lock down the Engine's artwork proxy (finding S-1, Critical, verified).

Background
- custom_components/maverick_music_flow/__init__.py defines two HTTP views:
  - HomeiiFlowItemArtworkProxyView at /api/maverick_music_flow/artwork/item/{token}
    with requires_auth = False.
  - HomeiiFlowArtworkProxyView at /api/maverick_music_flow/artwork/{entity_id}
    (requires auth), which shares the same fetch logic.
- runtime.py register_artwork_source() builds the token as an unkeyed
  hashlib.blake2s(source_url, digest_size=18), so anyone who knows a source URL can
  compute its token. Tokens live 7 days and every use extends them
  (resolve_artwork_source).
- radio_directory.py station_items() registers any Radio Browser "favicon" URL.
  Anyone can submit stations to Radio Browser, so that URL is attacker-controlled.
- Both views fetch with session.get() (follows redirects), read the whole body with
  no size limit (await response.read()), and return the upstream Content-Type as-is.
- _absolute_artwork_urls() builds fetch URLs from request.scheme and request.host
  (the caller's Host header) for sources that start with "/" or are relative.
- Any http(s) URL whose path contains /imageproxy is rewritten onto every configured
  MA base URL and fetched with the Engine's MA bearer token, so an attacker-chosen
  path and query reach MA's imageproxy with credentials.
- The bearer-token check is url.startswith(base.rstrip('/') + '/').
- scripts/validate_repo.py requires the string "hashlib.blake2s" ("deterministic
  artwork tokens") and the capability "stable_artwork_urls".

Attack scenarios to close
1. Stored XSS: a station favicon returns text/html or image/svg+xml containing script.
   HA serves it from its own origin at the item URL. A victim who opens that link has
   localStorage.hassTokens stolen, which is a full HA account takeover.
2. SSRF with a readable response: a favicon points at http://192.168.x.x/... . Anyone
   who can reach HA (no login needed) requests the item URL and reads the internal page.
3. Memory exhaustion: a favicon URL returns a multi-gigabyte body.

Required changes
1. Unguessable but still deterministic tokens: on first load generate a 32-byte secret
   with secrets.token_bytes, persist it in the runtime Store, and compute tokens with
   hashlib.blake2s(source.encode(), key=secret, digest_size=18). This keeps the
   validate_repo marker and stable URLs. Existing tokens become invalid after upgrade;
   that is acceptable because the card re-requests artwork.
2. One shared, hardened fetch helper used by both views:
   - Allow only http and https.
   - Do not follow redirects automatically. Follow at most 3 manually and re-validate
     each hop.
   - Stream the body and abort above 5 MB (check Content-Length first, then enforce the
     limit while reading).
   - Accept only image/jpeg, image/png, image/webp, image/gif, image/avif, image/bmp.
     Reject SVG, HTML and everything else. Check the file's magic bytes match the type.
   - For any host that is not a configured MA base URL, block loopback, private,
     link-local, multicast, reserved and unspecified addresses (IPv4 and IPv6,
     including IPv4-mapped IPv6). Enforce this at connect time (for example a dedicated
     aiohttp connector with a resolver that rejects those addresses) so DNS rebinding
     cannot bypass it.
   - Send the MA bearer token only when scheme, host and port exactly equal a
     configured MA base URL (compare urllib.parse.urlsplit parts; no startswith).
3. Never build URLs from request.host or request.scheme. For HA-relative sources use
   homeassistant.helpers.network.get_url(hass) (prefer the internal URL).
4. Apply the /imageproxy rewrite only when the source host is a configured MA host.
5. Add these headers to every artwork response: X-Content-Type-Options: nosniff and
   Content-Security-Policy: default-src 'none'; sandbox. Set Content-Type from the
   validated image type, never from the upstream header.
6. Only cache content that passed validation.
7. Consider shortening the token lifetime (for example 24 hours, sliding) if the card
   tolerates it. State the choice in the PR.
8. Update the README section "Diagnostics, permissions and privacy" to describe how the
   artwork proxy behaves.

Tests
- Same source + same secret gives the same token; a different secret gives a different
  token; the token cannot be recomputed without the secret.
- text/html, image/svg+xml, and mismatched magic bytes are rejected.
- A body over 5 MB is rejected without reading it all.
- Private, loopback and link-local targets are rejected for non-MA hosts; the MA base
  URL still works.
- A redirect to a private address is rejected.
- A forged Host header does not change the fetched URL.
- The bearer token is not sent to http://ma-host:8095.evil.example/ or to a different
  port on the MA host.
```

---

### Phase 2: Authorization and input validation

#### Prompt 2.1: Narrow the MA command bridge (S-2)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: narrow the Music Assistant command bridge (finding S-2, High, verified).

Background
- runtime.py _music_assistant_command_allowed() allows every command starting with
  players/, player_queues/, metadata/ and audio_analysis/ by prefix. For music/ it uses
  a substring denylist ("/create", "/update", "/delete", "/remove", "/import",
  "/export", "/sync", "/add_playlist", "/remove_playlist"). Commands such as
  music/start_sync and music/add_item_to_library still pass.
- async_music_assistant_command() runs the command with the Engine's stored MA token.
- Any authenticated HA user (admin or not) can reach it through the WebSocket command
  maverick_music_flow/ma/command (websocket_api.py) and the HTTP view
  HomeiiFlowCommandView (__init__.py, POST /api/maverick_music_flow/command/{command}).
  That view's docstring says it exposes "reads", but it also exposes favorites/set and
  ma/command.
- The HTTP view passes the raw JSON body through, so callers can set the internal flags
  _homeii_cache_worker and _homeii_cache_refresh and bypass request coalescing and
  caching.

Required changes
1. Replace the prefix and denylist logic with an explicit frozenset of exact command
   names. Starting point, the commands the Engine itself sends (grep the package for
   '"players/', '"player_queues/', '"music/'):
   - info
   - players/all
   - players/cmd/{play,pause,stop,next,previous,volume_set,volume_mute,group_many,
     set_members,ungroup}
   - player_queues/{all,get,get_active_queue,items,play_media,shuffle,repeat,
     crossfade,autoplay,seek,set_playback_speed,move_item,delete_item,clear,transfer}
   - music/{search,browse,item_by_uri,recommendations,recently_played_items,
     in_progress_items}
   - music/tracks/similar_tracks, music/albums/album_tracks,
     music/artists/artist_albums, music/artists/artist_tracks
   - music/playlists/{playlist_tracks,library_items,add_playlist_tracks}
   - music/favorites/{add_item,remove_item}
   - music/<media_type>/library_items for each media type async_get_library uses
   - ai_radio/hosts/list, ai_radio/queue_dj/status, ai_radio/queue_dj/set
   Confirm each against actual use. The HOMEii Music Flow card also sends commands
   through this bridge; if its source is available, add only what it needs. Log denied
   commands at debug level (command name only) so missing entries are easy to find.
2. Deny anything that creates or removes players or groups, changes configuration,
   manages providers, syncs, imports or exports, or touches authentication (config/*,
   auth/*, players/remove, players/create_group_player, players/remove_group_player,
   music/start_sync, providers/*, and similar). Add a test asserting these are denied.
3. Strip every key starting with "_homeii_" from external payloads (WebSocket and HTTP)
   before calling runtime methods. Internal callers pass these via a separate keyword
   argument instead.
4. HomeiiFlowCommandView: give each command an explicit voluptuous schema, reject
   unknown keys, apply the same authorization rules as the WebSocket commands
   (Prompt 2.2), and fix the docstring.
5. Keep the command names and response shapes the card relies on.

Tests
- Every allowlisted command is accepted.
- Admin, config, provider, sync and unknown commands are denied.
- _homeii_* flags from external payloads are ignored.
- The HTTP view rejects unknown commands and unknown keys.
```

#### Prompt 2.2: Add authorization to WebSocket commands, HTTP views and services (S-3)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: add authorization checks (finding S-3, High, verified).

Background
- websocket_api.py registers about 50 commands. Only maverick_music_flow/queue/settings
  checks connection.user.is_admin (for writes), and wheels/set passes is_admin through.
  No command uses @websocket_api.require_admin.
- Any authenticated HA user, including non-admin and wall-tablet accounts, can
  create/delete schedules, timers and volume rules, clear all volume rules, change the
  screensaver, drive any light.* entity through artwork lighting (lighting/set checks
  that the entities exist, not who may control them), make announcements, save/delete
  playlists and run MA commands.
- None of these paths check HA entity permissions
  (connection.user.permissions.check_entity(entity_id, POLICY_CONTROL)), so users
  restricted by HA entity policies bypass them.
- __init__.py registers only set_queue_settings with async_register_admin_service.

Required changes
1. Classify every WebSocket command and HTTP view endpoint:
   - Read-only (get_context, bootstrap/get, */get, stats, activity, sendspin/status,
     and similar): any authenticated user. Decide whether diagnostics/run should be
     admin-only, because it returns schedules, rules and activity.
   - Playback control of a specific player (player/command, playback/play_media,
     queue/action, queue/transfer, group/apply, favorites/set, announce, radio/search,
     playlists action=play): any authenticated user, but check POLICY_CONTROL on every
     target entity_id.
   - Configuration writes (schedules/set|delete|run, timers/set|delete,
     volume_rules/set|delete|clear, screensaver/set, lighting/set, interface/set,
     wheels/set with scope=global, playlists action=save|delete,
     orchestration/run_once, queue/settings with values): admin-only by default.
   - ma/command: apply the allowlist from Prompt 2.1, then classify each command as
     playback control or configuration as above.
2. Add an options-flow toggle "Allow non-admin users to manage schedules, timers and
   volume rules" (default off) for households that use non-admin dashboards. Document it.
3. Return a clear unauthorized error (websocket_api.ERR_UNAUTHORIZED, or HTTP 403).
4. Services in __init__.py: keep set_queue_settings as an admin service and consider
   async_register_admin_service for clear_volume_rules, set_volume_rule, set_schedule,
   delete_schedule, set_timer, delete_timer and set_screensaver. Automations run as the
   system user, so this only affects calls a person makes.
5. Update the README section "Diagnostics, permissions and privacy" with the permission
   model.

Tests
- A non-admin user is refused each configuration write; an admin succeeds.
- A user without control permission for an entity is refused a player command on it.
- Read commands still work for non-admin users.
- The options toggle allows non-admin schedule changes when enabled.
```

#### Prompt 2.3: Validate URLs passed to Music Assistant (S-5)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop callers from making Music Assistant fetch arbitrary URLs (finding S-5,
Medium, verified).

Background
- runtime.py async_send_announcement(): if the message starts with http:// or
  https:// it is passed as "url" to music_assistant.play_announcement, and MA's server
  fetches it.
- runtime.py async_play_media(): any media_id goes straight to
  player_queues/play_media, including http(s) URLs.
- Both are reachable by any authenticated user through the announce and play_media
  services and WebSocket commands.

Required changes
1. Add a shared validator for URL media: allow only http and https; resolve the host
   and reject loopback, private, link-local, multicast and reserved addresses unless
   the host is the configured MA host or HA's own URL (get_url). Leave MA URIs
   (library://, provider schemes such as spotify://) unchanged.
2. If local media servers are a real use case, add an option "Allow announcements and
   playback from local network URLs" (default off) and document it.
3. Decide whether raw URLs are admin-only (ties in with Prompt 2.2) or open but
   validated, and state the choice in the PR.
4. Error messages must not echo the resolved internal IP address.

Tests
- A public https URL is accepted.
- http://127.0.0.1, http://192.168.1.1, http://[::1] and http://169.254.169.254 are
  rejected.
- MA provider URIs are untouched.
```

#### Prompt 2.4: Bind Sendspin sessions to the HA user (S-8)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop users from connecting as each other's Sendspin client (finding S-8, Low,
verified).

Background
- sendspin_bridge.py HomeiiFlowSendspinView accepts any client_id matching
  [A-Za-z0-9_.:-]{1,128} from any authenticated user and authenticates to MA's
  /sendspin endpoint with the Engine's MA token. One user can therefore connect with
  another user's or device's client_id.
- There is no limit on concurrent relay sessions.

Required changes
1. Record which HA user owns each client_id (persisted) and refuse other users, or
   namespace the client_id with the HA user ID. Check first how MA uses client_id for
   player identity so existing "This device" players are not duplicated; document any
   format change.
2. Cap concurrent relay sessions per user and in total.
3. Confirm the card's "This device" playback still works.

Tests
- Two different users cannot use the same client_id.
- The per-user session cap is enforced.
```

---

### Phase 3: Lifecycle and entity bugs

#### Prompt 3.1: Make unload actually stop the integration (B-2, S-9, B-29)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: make removing, disabling or reloading the integration stop all of its work
(findings B-2 High, S-9 Low, B-29 Low; all verified).

Background
- __init__.py async_unload_entry() only calls runtime.unregister_entry().
  HomeiiFlowRuntime.async_stop_orchestration() exists but nothing calls it.
- unregister_entry() calls self._music_assistant_client.configure("", "") but never
  MusicAssistantEventClient.async_stop().
- ArtworkLighting.stop() only runs on the homeassistant_stop event, never on unload.
- Background tasks created with hass.async_create_task in runtime.py (timer execution
  ~4252, media command cache refresh ~4684 and ~4705, queue ~5309, library ~5469 and
  ~5503) and in radio_directory.py (bare asyncio.create_task) are not tracked or
  cancelled.
- async_setup() calls async_prepare_runtime(), which loads storage, starts
  orchestration, and registers the WebSocket commands, the static path and all HTTP
  views (including the login-free artwork view) even when no config entry exists. HA
  cannot unregister views, so they stay after removal.
- There is no CONFIG_SCHEMA. With async_setup present, hassfest expects
  CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN).

Result today: after the entry is disabled or deleted, the 30-second and per-minute
ticks keep running stored schedules, timers (media_stop) and volume rules
(volume_set), and artwork lighting keeps driving lights, until HA restarts.

Required changes
1. Start the runtime from async_setup_entry, not async_setup. Keep async_setup only
   for what must be global, or remove it. Add
   CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN).
2. When the last entry unloads: stop orchestration (async_stop_orchestration, the
   schedule manager and the minute tick), stop artwork lighting, await
   MusicAssistantEventClient.async_stop(), cancel and await all tracked background
   tasks, and flush pending Store saves.
3. Track background tasks with entry.async_create_background_task, or a runtime-owned
   set with done-callbacks, so they can be cancelled.
4. Views and static paths cannot be unregistered, so make each view return 404 or 503
   when no entry is loaded (check a runtime "active" flag).
5. Re-adding or reloading the entry must start everything cleanly with no duplicate
   timers or listeners.

Tests
- After unload: no tick fires, no service calls are made for due schedules or volume
  rules, the MA client task is finished, artwork lighting listeners are removed, and
  the views return 404/503.
- After reload there is exactly one set of timers and listeners.
```

#### Prompt 3.2: Stop deleting the volume-rule sliders (B-1)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop the switch platform deleting the "Max volume" number entities (finding B-1,
High, verified).

Background
- switch.py _remove_stale_registry_entries() removes every entity registry entry for
  the config entry whose unique_id starts with f"{entry.entry_id}_{kind}_" and whose
  remainder is not a current key. It does not filter by platform. For kind
  "volume_rule" the prefix is "<entry_id>_volume_rule_".
- number.py HomeiiFlowVolumeRuleNumber uses unique_id
  f"{entry.entry_id}_volume_rule_max_{player}", which matches that prefix. The
  remainder "max_media_player.x" never matches a current key, so the slider is deleted
  at setup and on every SIGNAL_ENGINE_UPDATED (at least every 30 seconds).
- number.py add_missing_items() keeps the key in known_volume_rules, so the slider is
  never re-added.
- Other unique_ids checked: button.py uses "<entry>_run_schedule_<id>", which does not
  match the "_schedule_" prefix. Only the number entity collides today.

Required changes
1. Filter the cleanup by platform: only consider registry entries whose domain is
   "switch" in switch.py (and "number" in number.py's own
   _remove_stale_registry_entries).
2. Do not change existing unique_ids (that would orphan entities). If you must, add an
   entity registry migration.
3. Make number.py re-add a slider if its registry entry has gone missing.

Tests
- With one volume rule, after several engine-update signals both the switch and the
  slider still exist.
- Deleting the rule removes both.
- Schedule and timer switch cleanup still works.
```

---

### Phase 4: Secrets hygiene

#### Prompt 4.1: Redact diagnostics (S-4, B-25)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: redact diagnostics and fix their stale key lists (findings S-4 Medium, reported;
B-25 Low, reported, partly confirmed). Confirm first.

Background
- diagnostics.py never calls
  homeassistant.components.diagnostics.async_redact_data.
- The MA token and URLs are already excluded (EngineEntry.as_dict and
  MusicAssistantEventClient.snapshot report booleans only).
- But the stats and screensaver sections include active_player.artwork_candidates[:5].
  These contain entity_picture URLs such as /api/media_player_proxy/...?token=... and
  homeii_artwork_url links to the login-free artwork view. The output also includes
  last_error strings, player names and track titles. A user pasting diagnostics into a
  GitHub issue would leak working image links.
- The key lists used by diagnostics are stale: they include run_schedule (confirmed)
  and miss show_system_screensaver_now and system_screensaver_timeout.

Required changes
1. Pass all returned data through async_redact_data with a TO_REDACT set covering
   music_assistant_token, token, access_token, music_assistant_url,
   music_assistant_external_url, ma_url, url, entity_picture, media_image_url,
   artwork_candidates, homeii_artwork_url, image, image_url, plus any key containing
   "token" or "password".
2. Strip query strings from any URLs that remain. Truncate last_error and remove URLs
   and IP addresses from it.
3. Decide whether to keep media titles and player names (useful for support) and say so
   in the README.
4. Update the key lists to match the entities that actually exist.
5. Apply the same redaction to the maverick_music_flow/diagnostics/run WebSocket
   command.

Tests
- A diagnostics dump built from a runtime containing tokens and URLs contains none of
  them.
- The key lists match the entity unique_id suffixes.
```

#### Prompt 4.2: Harden setup and onboarding (S-7a to S-7e)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: harden the config and onboarding flow (finding S-7, Low, verified).

Background
- config_flow.py async_step_manual and the options flow's async_step_general declare
  the MA token as a plain str field, so it shows in clear text. The automatic step
  already uses a PASSWORD TextSelector for the password.
- async_step_automatic calls onboarding_auth.create_onboarding_token, which creates a
  long-lived MA token (auth/token/create) before validation finishes. If
  async_step_manual then fails (for example _validate_music_assistant_api returns an
  error), the token stays on MA unused, and each retry creates another.
- create_onboarding_token logs in with auth/login and never revokes that login session.
- For http:// URLs, the MA username and password travel over unencrypted ws://, with no
  warning.
- The options flow copies user_input (including the token) into entry.options while
  the original stays in entry.data, so the token is stored twice.

Required changes
1. Use selector.TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)) for
   the token in the manual and options flows.
2. Validate the MA URL and schema before creating a token. If validation fails after a
   token was created, revoke it. Confirm MA's revoke and logout command names in its
   API; do not guess.
3. Revoke or log out the temporary login session after creating the long-lived token.
4. In the automatic step, show a warning when the URL is http:// (credentials are sent
   unencrypted on the local network). Keep allowing it.
5. Store the token in one place (entry.data) and update it with
   hass.config_entries.async_update_entry instead of duplicating it in options. Migrate
   existing entries (bump the config entry version and add async_migrate_entry).
6. Update strings.json and translations/en.json.

Tests
- The token field is a password selector.
- Failed validation after token creation calls revoke.
- Migration moves the token from options to data.
```

---

### Phase 5: Tests and CI

#### Prompt 5.1: Add real Home Assistant tests (H-3)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: add tests that load Home Assistant (finding H-3, verified).

Background
- All 13 test files extract single functions or classes with ast.parse plus
  exec/runpy and stub dependencies with SimpleNamespace. homeassistant is never
  imported.
- Nothing covers the switch, number, sensor, binary_sensor, button and calendar
  platforms, diagnostics, the config flow, services or the HTTP views. That is how the
  slider deletion (B-1) and calendar bugs (B-5) went unnoticed.

Required changes
1. Add requirements_test.txt with pytest, pytest-asyncio,
   pytest-homeassistant-custom-component (pinned to a version that matches the HA floor
   in hacs.json, 2025.1 or newer) and Pillow.
2. Add tests/conftest.py with fixtures for: a fake MA server (an aiohttp test server
   implementing the WebSocket greeting, auth, and the commands the Engine uses), a stub
   music_assistant dependency, and a loaded config entry.
3. Add tests for: config flow (manual, automatic, ingress URL rejection, duplicate
   instance), setup/unload/reload, each entity platform (created, updated, removed),
   services, a representative set of WebSocket commands (including authorization once
   Prompt 2.2 lands), the HTTP views (artwork, command) and diagnostics.
4. Keep the existing unittest files running (pytest can collect them) until they are
   replaced.
5. Document how to run the tests in the README section "Reporting and contributing".
```

#### Prompt 5.2: Strengthen CI (H-4, H-6)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: strengthen CI (findings H-4 and H-6, verified).

Background
- .github/workflows/validate.yml installs Pillow and runs unittest,
  scripts/validate_repo.py and compileall on Python 3.13.
- There is no hassfest, no HACS validation, no type checking, and ruff is configured
  in pyproject.toml but never run.
- scripts/validate_repo.py enforces string markers (for example "hashlib.blake2s",
  "_queue_inflight", every command name) that lock in implementation details.

Required changes
1. Add CI jobs: home-assistant/actions/hassfest, hacs/action (category: integration),
   ruff check and ruff format --check, pytest (after Prompt 5.1), and optionally mypy
   on the package.
2. Test on the Python version current HA requires (and your supported HA floor); keep
   requires-python in pyproject.toml consistent.
3. Fix whatever ruff and hassfest report. Keep mechanical formatting and real fixes in
   separate commits.
4. Review validate_repo.py: keep the structural checks (manifest, version, domain),
   replace marker checks that encode implementation details with behavioral tests,
   and update any markers earlier prompts changed on purpose.
5. Add Dependabot for GitHub Actions.
```

---

### Phase 6: Scheduling, timers, volume windows and calendar

#### Prompt 6.1: Stop schedules re-queuing media (B-3)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop schedules adding the same media several times (finding B-3, verified).

Background
- The schedule execution code in runtime.py (~3940-3980) calls async_play_media with
  verify_playback=True, treats verified == False as a failure, and retries up to
  retry_attempts (default 4, max 12).
- async_play_media only verifies when enqueue is play, replace or shuffle:
  verify_playback = bool(payload.get("verify_playback")) and enqueue in
  {"play", "replace", "shuffle"}. For add and next it returns verified=False without
  checking anything.
- So a schedule with enqueue add/next appends the media 4 times by default (up to 12)
  and is logged as failed. The delayed snapshot check rarely rescues it, because adding
  to a queue does not change the title or state.

Required changes
1. Return a distinct result when verification does not apply (for example
   verification="skipped") and treat it as success in the schedule runner.
2. Optionally verify add/next by comparing queue item counts before and after
   (player_queues/get or player_queues/items) without re-sending the command.
3. Never retry an enqueue that MA accepted; retry only when the command itself failed.
4. Check the "play" path too: when verification fails after MA accepted the command,
   retrying replays the media. Prefer verifying for longer over re-sending.

Tests
- enqueue=add runs once and is recorded as ok.
- A failed command is retried.
- enqueue=play with slow verification does not replay.
```

#### Prompt 6.2: Fix the timer race (B-8)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop timers being dropped or resurrected (finding B-8, reported). Confirm first.

Background
- runtime.py async_run_due_timers() (~4209-4241) builds "remaining" from a snapshot of
  self._storage["timers"], awaits async_execute_timer (a blocking media_stop plus an
  activity save), then assigns self._storage["timers"] = remaining.
- A timer set during those awaits is lost; a timer deleted during them comes back.

Required changes
1. Collect the IDs of due timers, execute them, then remove only those IDs from the
   current list. Guard this with an asyncio.Lock shared with async_set_timer and
   async_delete_timer (or re-read the list after awaiting and filter by ID).
2. Save once after the change.

Tests
- Setting a new timer while a due timer is executing keeps the new timer and removes
  the executed one.
- Deleting a timer during execution keeps it deleted.
```

#### Prompt 6.3: Fix overnight windows, the end minute and daylight saving (B-6, B-11)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix overnight volume windows, the inclusive end minute and daylight-saving
handling (B-6 verified; B-11 reported, confirm first).

Background
- runtime.py _time_window_active(now, start, end): when start > end it returns
  current >= start or current <= end; otherwise start <= current <= end. The end is
  inclusive, so a window ending at 07:00 is active until 07:00:59.
- _volume_rule_active() checks "days" against the weekday of now (_homeii_weekday,
  Sunday = 0). For an overnight window, the part after midnight belongs to the previous
  day's rule. A Friday-only 22:00-06:00 limit stops at 00:00 Saturday, while
  Thursday's limit carries into 00:00-06:00 Friday even though Thursday is not chosen.
- _due_schedule_datetime() (~459-478) subtracts two datetimes that share a tzinfo,
  which Python treats as wall-clock time. A schedule inside the spring-forward gap
  (for example 02:30) is never due in the tick path; fall-back times may run twice.

Required changes
1. Make the end exclusive (start <= current < end) and document it.
2. For overnight windows, check the day the window started: after midnight, test
   whether yesterday is in "days".
3. Compute schedule due times with timezone-aware arithmetic: compare in UTC, move
   nonexistent local times to the next valid minute, and run ambiguous times once
   (fold=0).
4. Keep Sunday = 0 day numbering (it is a documented public contract).

Tests
- A Friday-only 22:00-06:00 rule is active Friday 23:00 and Saturday 05:59, and
  inactive Friday 05:00 and Saturday 22:30.
- The end minute is exclusive.
- A 02:30 schedule on spring-forward day runs once (at 03:00 or the documented time).
- A 01:30 schedule on fall-back day runs once.
```

#### Prompt 6.4: Prevent double runs and lost edits (B-12, B-13, H-5f)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: prevent duplicate schedule runs after a restart and lost edits during retries
(B-12, B-13 reported; H-5f reported). Confirm first.

Background
- Last-run times (_last_schedule_runs, set up in HomeiiFlowRuntime.__init__ ~972) live
  only in memory. If HA restarts within about 110 seconds of a run, the catch-up path
  in HomeiiScheduleRunner.reschedule() (~686-691) plays it again.
- HomeiiScheduleRunner.async_fire() (~709-739) copies the schedule at the start (~711)
  and, for after_run=disable, writes that copy back after the retry loop (~731-739).
  Edits made during the retries (up to about 7 minutes at maximum retries) are
  overwritten.
- switch.py (~431) duplicates the after_run=disable logic.

Required changes
1. Persist last-run times per schedule in the Store (bounded) and use them in catch-up.
2. For after_run=disable, re-read the current schedule by ID and only set
   enabled=False. Do not write back the old copy. Skip if the schedule was deleted or
   edited meanwhile (compare an updated_at field; add one if missing).
3. Remove the duplicated after_run logic from switch.py and call the runtime method.

Tests
- A restart right after a run does not replay it.
- An edit made during retries survives.
- A schedule deleted during its run is not recreated.
```

#### Prompt 6.5: Fix alarm readiness (B-9)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop alarms failing because of a stale Music Assistant player cache (finding B-9,
reported). Confirm first.

Background
- runtime.py _player_readiness() (~2461-2470) prefers _ma_players_by_entity, which is
  only refreshed in async_players_snapshot() (~2814). That runs from the startup
  probe, card bootstrap and players/get.
- If a speaker was off when HA started and no card is open, a 07:00 schedule sees
  "Music Assistant player is unavailable" on every retry even though HA shows the
  player as fine. Conversely, a player that has gone offline is treated as ready.

Required changes
1. Refresh the MA player map on MA player events (player_added, player_updated,
   player_removed) from the event stream, and on demand in _player_readiness when the
   data is older than a set number of seconds (single-flight).
2. Fall back to the HA entity state when the MA data is stale, and include the data's
   age in the readiness reason.

Tests
- A player unavailable at startup that later becomes available lets the schedule
  succeed.
- A player that goes offline makes readiness false.
```

#### Prompt 6.6: Fix the schedule calendar (B-5)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix the calendar entity (finding B-5, verified).

Background (calendar.py)
- The event-listing code loops over schedules in storage order and stops once
  len(events) >= limit, then sorts. With limit=1 (used for the "event" property) it
  returns the first stored schedule's next occurrence, not the soonest. Example:
  A daily at 22:00, B daily at 07:00; at 06:00 the calendar shows A.
- _schedule_events() includes an event only if start <= event_start <= end, so an
  event already in progress is excluded. HA shows "on" when start <= now < end, so the
  calendar never turns on, and range queries miss overlapping events.
- Events are built with datetime.combine(..., tzinfo=dt_util.DEFAULT_TIME_ZONE)
  (see Prompt 6.3 for daylight-saving handling).

Required changes
1. Collect candidates from all schedules, sort, then apply the limit.
2. Include events that overlap the range (event_end > start and event_start < end).
3. Give events a sensible duration (for example 1-5 minutes, or configurable) so the
   calendar can show "on".
4. Build event times with timezone-correct helpers.

Tests
- The soonest event is chosen.
- An in-progress event makes the calendar state "on".
- A range query returns overlapping events.
```

---

### Phase 7: Performance and resource limits

#### Prompt 7.1: Fix stuck in-flight requests (B-4, B-18)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix in-flight request handling (B-4 verified for queue, reported for library;
B-18 reported). Confirm the reported parts first.

Background
- runtime.py async_get_queue() (~5298-5317): the single-flight entry
  self._queue_inflight[cache_key] is removed in "finally" only if
  foreground_task.done(). If the awaiting caller is cancelled (HTTP disconnect, a
  timeout wrapper), the task is not done yet, so the entry stays. Every later call for
  that key finds existing_task and returns its old result forever (the 2-second cache
  only hides this briefly).
- async_get_library() (~5467, ~5516) uses the same pattern. Its stale branch never
  starts a refresh, so after stale_until it returns the old result, or re-raises the
  old exception, forever.
- Compare async_music_assistant_command (~4717), which removes its entry
  unconditionally.
- radio_directory.py search_stations() does the same with _radio_directory_pending
  (and uses a bare asyncio.create_task). If the only caller is cancelled and the fetch
  then fails, the failed task stays and its error is served to the next caller.

Required changes
1. Remove in-flight entries in a task done-callback, not in the caller's "finally".
2. Never serve a finished in-flight task's result as fresh: remove it and start a new
   request, or move its result into the cache with correct timestamps.
3. Make the library stale branch start a single-flight background refresh, like the
   media-command cache does.
4. Use hass.async_create_background_task (tracked, see Prompt 3.1) instead of a bare
   asyncio.create_task in radio_directory.py.

Tests
- Cancelling the first caller mid-request: the next request after completion gets
  fresh data.
- A failed fetch is not reused for the next caller.
- A stale library entry triggers a refresh.
```

#### Prompt 7.2: Bound the in-memory caches (S-6a, S-6b, S-6d, B-28)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: put limits on in-memory caches (S-6a reported, S-6b verified, S-6d verified,
B-28 reported). Confirm the reported parts first.

Background
- runtime.py _library_cache (written in async_get_library ~5552) and _search_cache
  (async_get_search ~5892) are keyed by free-text query and offset. _library_cache has
  no in-memory eviction (only the persisted copy is capped at 48 entries);
  _search_cache is only cleared on library or favorite events.
  _mark_library_cache_stale marks entries stale but never removes them. Each entry can
  hold up to 500 items plus raw data for up to 24 hours. Search-as-you-type, or a
  script looping over offsets, grows memory until HA restarts.
- The artwork content cache (cache_artwork_content) holds up to 192 entries of 5 MB,
  about 960 MB.
- The artwork token map is capped at 5000, but eviction is first-in-first-out because
  reassigning an existing key does not move it (register_artwork_source,
  resolve_artwork_source). Artwork in active use is evicted first.
- ma_client.py connects with max_msg_size=0 (unlimited) and _partial_results has no cap.

Required changes
1. Replace these dicts with a small shared LRU-with-TTL helper (for example
   collections.OrderedDict with move_to_end) limited by entry count and approximate
   total bytes. Suggested limits: library 64 entries, search 64 entries, artwork
   content 64 MB total.
2. Evict expired and stale entries on insert.
3. Artwork tokens: move_to_end on every use, so eviction is least-recently-used.
4. Set max_msg_size on the MA WebSocket (for example 64 MB; check the largest real MA
   responses such as queue and library pages) and cap partial results per command by
   count and bytes, failing the command if exceeded.
5. Report cache sizes in stats for debugging.

Tests
- Inserting past the limit evicts the least recently used entry.
- The byte cap is respected.
- A recently used artwork token survives eviction.
- An oversized MA message closes the socket cleanly and fails pending commands.
```

#### Prompt 7.3: Bound stored data (S-6c)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: limit how much data users can store (finding S-6c, reported). Confirm first.

Background
- saved_playlists.py save_playlist() allows 1-2000 URIs per playlist (each up to 4096
  bytes, about 8 MB), but there is no limit on the number of playlists or profiles, and
  profile_id is whatever the caller sends.
- interface_preferences.py save_wheel_preferences() allows unlimited contexts per user.
- Any logged-in user can bloat .storage, which is rewritten in full on every save.

Required changes
1. Add limits, for example: 100 saved playlists per profile; 500 URIs per playlist (or
   keep 2000 with a total byte cap); 1024 characters per URI; 50 wheel contexts per
   user; a byte cap on each preference dict (serialize and measure).
2. Validate profile_id against a pattern such as [a-z0-9_-]{1,32}, cap the number of
   profiles, and allow only admins to create new profiles.
3. Return clear error messages.

Tests: each limit is enforced; invalid profile IDs are rejected.
```

#### Prompt 7.4: Reduce database and CPU load (B-7a to B-7e)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: reduce Home Assistant database and CPU load (B-7a verified, B-7b reported,
B-7c reported, B-7d verified, B-7e reported). Confirm the reported parts first.

Background
- runtime.py _handle_music_assistant_message() (~2013-2077) fires
  EVENT_MUSIC_ASSISTANT ("maverick_music_flow_music_assistant_event") on the HA bus
  for every MA event, including per-second queue_time_updated progress events.
  progress_only is computed but not used to skip the fire. Each event has a unique
  sequence and occurred_at, so the recorder stores 100,000+ rows per player per day.
  Nothing in this repo listens; check whether the card subscribes to it.
- sensor.py: the Status sensor's extra_state_attributes are the whole context()
  (including the ~80-key capabilities dict and a new generated_at on every call), and
  they are written on every SIGNAL_ENGINE_UPDATED (at least 4 times a minute).
- About 20 sensor and binary_sensor value functions each call
  runtime.required_connections_snapshot(), which walks all media players and the
  entity registry, on every signal.
- Playback sensors set force_update=True (sensor.py ~260-281).
- runtime.py async_record_activity() (~5974) does a full Store.async_save every time,
  so a volume rule fighting a user writes the whole store every 30 seconds.

Required changes
1. Do not fire bus events for progress-only MA events. If the card needs progress,
   deliver it through a WebSocket subscription instead. Consider debouncing the rest.
2. Exclude noisy attributes from the recorder (_unrecorded_attributes /
   _entity_component_unrecorded_attributes). Drop generated_at and capabilities from
   sensor attributes; keep them in the WebSocket context.
3. Compute required_connections_snapshot() once per update and share it across
   entities (cache invalidated by the signal).
4. Remove force_update unless a sensor truly needs it.
5. Use Store.async_delay_save for activity records.

Tests
- Progress events do not reach the bus.
- The snapshot is computed once per signal.
- Status sensor attributes stay under 16 KB.
```

#### Prompt 7.5: Fix statistics side effects and library invalidation (B-14, B-15)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: make statistics reads side-effect free and invalidate the library cache on MA
item events (B-14, B-15, reported). Confirm first.

Background
- runtime.py playback_statistics() (~3092-3100) calls _sync_playback_statistics()
  (~2997-3014), which records player start and stop transitions. Sensors call
  playback_statistics() several times on every SIGNAL_ENGINE_UPDATED (sensor.py
  ~258-280 and ~326), and each call builds media_players_snapshot(). Because a sensor
  read records the start first, the orchestration tick (~3071-3083) sees nothing new,
  so playback_started/stopped activity is mostly never logged.
- _handle_music_assistant_message() decides library invalidation from a token list
  (~2033-2052) that does not match MA's media_item_added, media_item_updated and
  media_item_deleted events. Changes made in the MA UI stay in cached library and
  search results for up to 10 minutes, and then once more via stale-while-revalidate.

Required changes
1. Make playback_statistics() a pure read. Do the sync only in the orchestration tick
   (or on player state-change events), and log activity for the transitions the tick
   detects.
2. Handle media_item_added, media_item_updated and media_item_deleted (confirm MA's
   exact event names for schema 63+): map the item's media type to
   _mark_library_cache_stale and clear affected search entries.

Tests
- Reading statistics repeatedly does not suppress playback_started activity.
- A media_item_deleted event invalidates the library cache for that media type.
```

#### Prompt 7.6: Remove hot-path inefficiencies (B-17, B-26, B-27)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: remove expensive work from hot paths (B-17, B-26, B-27, reported). Confirm first.

Background
- runtime.py _try_music_queue_command_bridge() (~4933-4977) ignores limit_before and
  limit_after and pages the entire queue on every queue open that misses the 2-second
  cache.
- _extract_media_items() (~263-309) has no depth limit and walks wrapper children up to
  3 times per level, so a deeply nested response costs O(3^depth).
- _ha_entity_for_ma_player() (~2667-2690) scans the whole entity registry and rebuilds
  media_players_snapshot() for each MA player.

Required changes
1. Fetch only the requested window around the current queue item (respect
   limit_before/limit_after) and page further only when the card asks.
2. Add a depth limit (for example 6) and visit each child once in
   _extract_media_items.
3. Build an MA-player-ID to entity_id index once per snapshot (from the entity
   registry, filtered to the music_assistant platform) and reuse it.

Tests
- A queue fetch requests only the window.
- A deeply nested payload is handled quickly.
- The index is built once per snapshot.
```

---

### Phase 8: Smaller functional bugs

#### Prompt 8.1: Fix player command edge cases (B-10, B-16)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix volume units and the join command (B-10, B-16, reported). Confirm first.

Background (runtime.py async_player_command, ~3360-3430)
- Volume: "numeric if numeric <= 1 else numeric / 100" makes volume: 1 (meaning 1%)
  set the player to 100%.
- join: reads payload.get("group_members"), but neither the WebSocket schema
  (websocket_api.py, maverick_music_flow/player/command) nor
  SERVICE_PLAYER_COMMAND_SCHEMA (__init__.py) accepts group_members, so
  child_player_ids is always empty.

Required changes
1. Make volume units explicit: "volume" is always 0-100 percent and "volume_level" is
   always 0.0-1.0. Reject out-of-range values. Update services.yaml and the README.
2. Add group_members: [str] (validated as media_player entity IDs) to both schemas, or
   remove the join command if the card uses group/apply instead (check the card).

Tests
- volume 1 sets 0.01; volume_level 1 sets 1.0.
- join passes its members.
```

#### Prompt 8.2: Fix the system screensaver script (B-20, B-21, B-22)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix the system screensaver (B-20 verified; B-21 and B-22 reported, confirm
first).

Background (custom_components/maverick_music_flow/frontend/
maverick-music-flow-system-screensaver.js)
- ~line 507: hass.callWS({ type: "maverick_music_flow/screensaver/get", source: ... })
  sends no profile_id, so settings for a non-default profile are ignored.
- ~line 341: the <img> points at /api/maverick_music_flow/artwork/{entity_id}, which
  requires authentication (HomeiiFlowArtworkProxyView). A plain <img> request sends no
  bearer header, so it may always fail (not tested).
- artworkUrl() (~lines 164-170) returns raw external http(s) URLs, so the browser loads
  third-party artwork directly. That leaks viewers' IP addresses and causes
  mixed-content problems on HTTPS dashboards.

Required changes
1. Pass the configured profile_id to screensaver/get and any other calls that take it.
2. For authenticated artwork, use an HA signed path (the auth/sign_path WebSocket
   command) or the item proxy URLs the Engine returns (after Prompt 1.1).
3. Route external artwork through the Engine proxy; never load arbitrary third-party
   URLs directly.
4. Bump HOMEII_SYSTEM_SCREENSAVER_VERSION and mention browser cache refresh in the
   README.

Tests: if you add a JavaScript test setup, cover URL selection; otherwise list manual
test steps in the PR.
```

#### Prompt 8.3: Fix sensor metadata (B-23, B-24)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: fix sensor device and state classes (B-23, B-24, reported). Confirm first.

Background (sensor.py)
- next_schedule and next_timer return ISO strings without
  SensorDeviceClass.TIMESTAMP.
- playback_today resets daily but uses SensorStateClass.MEASUREMENT instead of
  TOTAL_INCREASING (or TOTAL with last_reset).

Required changes
1. next_schedule / next_timer: device_class TIMESTAMP, returning timezone-aware
   datetime objects (None when nothing is scheduled).
2. playback_today: state_class TOTAL_INCREASING with a proper UnitOfTime unit. Check the
   other counters for the same issue.
3. Note the long-term statistics impact in the PR (HA may ask users to fix statistics
   after a state_class change).

Tests: device classes, state classes and value types are as expected.
```

#### Prompt 8.4: Stop dropping artwork-lighting updates (B-19)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: stop artwork lighting ignoring track changes (finding B-19, reported). Confirm
first.

Background: artwork_lighting.py ArtworkLighting._schedule() returns early if an update
task for that player is still running, so a track change during an update is ignored
until the next 10-second _tick.

Required changes: when a task is running, mark the player as pending and run once more
when the task finishes (coalesce; do not queue without limit). Keep the cooldown and
the stale-download protection.

Tests: two quick state changes end with the lights showing the final track's colors
without waiting for the 10-second tick.
```

---

### Phase 9: Code structure

#### Prompt 9.1: Remove dead code and small inconsistencies (H-5c, H-5d, H-5e)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: remove dead code and small inconsistencies (H-5c, H-5d, H-5e, reported). Confirm
each first.

- Dead code in runtime.py: screensaver_music_assistant_base_urls() (~2423),
  players_snapshot() (~2912), _clear_schedule_jobs() (~1868). Remove them after
  confirming there are no callers (grep this package; check the card if it could call
  them indirectly).
- runner_retry_attempts and runner_retry_delay are read (~589-590) but
  async_set_schedule never stores them. Either store them (from retry_attempts and
  retry_delay) or remove the reads.
- The final "raise" in async_queue_action() (~3585) cannot be reached because
  clean_queue_id always falls back to player_id. Simplify the logic.

Tests: the existing suite passes; add a test for retry settings if you keep them.
```

#### Prompt 9.2: Use one scheduler for schedules (H-5b)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: use a single scheduler for schedules (finding H-5b, reported). Confirm first.

Background: the same schedule can be triggered by three mechanisms:
HomeiiScheduleRunner's timer (runtime.py ~706), the 30-second interval and per-minute
ticks in async_start_orchestration (~1828-1829), and a per-switch timer in switch.py
(~390-437). Run claims prevent double execution today, but this makes timing and
debugging fragile.

Required changes: keep HomeiiScheduleManager/HomeiiScheduleRunner as the single source
of truth (async_track_point_in_time per schedule). Limit the ticks to catch-up and
volume-policy enforcement. Remove the switch-level timer (the switch only toggles
"enabled"). Keep the existing claim and dedupe tests passing.
```

#### Prompt 9.3: Split runtime.py into modules (H-5a)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: split runtime.py (about 6,300 lines) into focused modules (finding H-5a,
verified).

Proposed modules: scheduling.py (runner, manager, timers), volume_policy.py,
library_cache.py, queue.py, artwork.py (token registry plus the fetch helper from
Prompt 1.1), announcements.py, statistics.py and ma_bridge.py (allowlist, commands,
caching). Keep HomeiiFlowRuntime as a thin facade so websocket_api.py and the entity
platforms keep working unchanged.

Rules
- Do pure moves first, in their own PR; change behavior only in later PRs.
- Update scripts/validate_repo.py markers and the file paths used by the AST-based
  tests in the same PR.
- Run the full test suite after each move.
```

---

### Phase 10: Housekeeping

#### Prompt 10.1: Add a license (H-1)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: add a LICENSE file (finding H-1, verified). This needs a human decision first.

Background
- The repository has no LICENSE file, but pyproject.toml declares
  license = { text = "MIT" } and authors [{ name = "HOMEii" }].
- This fork comes from Ronen Atsil's (GitHub r11a) HOMEii Flow Engine.

Steps
1. Check the upstream repository for a LICENSE file and report what you find. Do not
   write a license until the owner confirms which one applies.
2. If upstream has no license, all rights are reserved by default: tell the owner they
   need the author's permission before redistributing, and stop.
3. If upstream is MIT (or the owner confirms MIT with the author's permission), add
   LICENSE with the original copyright line plus the fork owner's line, and keep
   pyproject.toml consistent.
```

#### Prompt 10.2: Make the fork's identity consistent (H-2)

```text
Read docs/IMPLEMENTATION_PLAN.md Section 2 (Working rules) and follow them.

Task: update the fork's identity and README (finding H-2, verified). Ask the owner for
the new name, branding and GitHub handle before changing names or logos.

Background
- manifest.json codeowners is ["@r11a"] (the upstream author). documentation and
  issue_tracker already point at this fork.
- Names: manifest "name" and hacs.json "name" are "HOMEii Flow Engine"; pyproject name
  is "homeii-flow-engine" with authors "HOMEii". Logos and docs/BRAND.md describe the
  HOMEii Flow brand.
- README links to r11a/homeii-music-flow (the card) in many places, including
  branch-specific links (codex/v6-release-candidate) and v6.0.0 docs.
- README has stale or contradictory text: "Before a beta is published", "HACS
  installation after public availability is arranged", "The Engine tracker is visible
  only to permitted users while the repository is private", a "Status STABLE" badge
  next to BETA warnings, "The preceding 0.7.21 source passed 52 regression tests" (now
  84), and duplicated install sections.
- RELEASE_NOTES_1.0.0-beta.1.md, RELEASE_NOTES_1.0.0.md and docs/BRAND.md describe
  upstream's release process.
- The integration depends on the upstream card (HOMEii Music Flow 6.0.0). The owner
  should decide whether to fork the card too.

Required changes (after the owner's decisions)
1. Set codeowners to the owner's GitHub handle; update the manifest name, hacs.json
   name and pyproject name/authors. Keep the domain maverick_music_flow.
2. Rewrite the README for the fork: one install section, an accurate status badge, no
   private-repo or beta-preparation text, the current test count, and clear credit to
   upstream.
3. Replace or keep logos according to the owner's branding decision, and update
   docs/BRAND.md to match.
4. Keep the issue templates but update their wording for the fork.
```
