"""Validation for URL media that the Engine hands to Music Assistant.

Music Assistant's server fetches plain ``http(s)`` URLs passed as announcement or queue
media, and URLs embedded as the item id of a Music Assistant URI (for example
``builtin://track/https://...``). Callers must not be able to point it at the local
network, so every URL media reference is checked before it is sent:

- only ``http`` and ``https`` URLs are accepted; other network or file schemes are refused,
- URLs with credentials, whitespace, backslashes or control characters are refused so that
  Music Assistant cannot parse a different host than the one checked here,
- the host is resolved and every address it resolves to must be public (loopback, private,
  link-local, multicast, reserved and unspecified addresses are refused),
- URLs whose scheme, host and port equal a configured Music Assistant base URL or Home
  Assistant's own URL are always accepted,
- the "Allow announcements and playback from local network URLs" option additionally
  accepts private LAN addresses (RFC 1918, shared address space and IPv6 unique local),
  never loopback or link-local ones.

Music Assistant URIs that do not embed a URL (``library://``, ``spotify://``, ...) are
left unchanged. Error messages never include the URL, host or resolved address.

This module has no Home Assistant imports so the policy can be tested directly.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import unquote, urlsplit

Address = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

RESOLVE_TIMEOUT_SECONDS = 5.0

# Schemes that name a network or file location rather than a Music Assistant provider.
NON_HTTP_URL_SCHEMES = frozenset(
    {
        "dict",
        "file",
        "ftp",
        "ftps",
        "gopher",
        "ldap",
        "ldaps",
        "nfs",
        "rtmp",
        "rtmps",
        "rtp",
        "rtsp",
        "rtsps",
        "sftp",
        "smb",
        "srtp",
        "tcp",
        "telnet",
        "tls",
        "udp",
        "ws",
        "wss",
    }
)

# Private LAN ranges the local network option allows. Loopback and link-local stay refused.
LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)

# Music Assistant commands on the card bridge whose arguments are media references
# that Music Assistant may fetch, with the argument keys holding them.
MEDIA_REFERENCE_ARGS: dict[str, tuple[str, ...]] = {
    "player_queues/play_media": ("media",),
    "music/item_by_uri": ("uri",),
    "music/library/add_item": ("item",),
    "music/favorites/add_item": ("item",),
    "music/playlists/add_playlist_tracks": ("uris",),
}

_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.IGNORECASE)
_EMBEDDED_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_UNSAFE_URL_RE = re.compile(r"[\s\\\x00-\x1f\x7f]")

_NOT_ALLOWED = "Media URL is not allowed"
_LOCAL_HINT = "An administrator can allow local network URLs in the HOMEii Flow Engine options."


class MediaUrlNotAllowed(ValueError):
    """Raised when URL media must not be passed to Music Assistant."""


def _origin(url: str) -> tuple[str, str, int] | None:
    """Return (scheme, host, port) for an HTTP(S) URL with the default port filled in."""
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().rstrip(".")
    return parts.scheme, host, port or (443 if parts.scheme == "https" else 80)


def _is_public(address: Address) -> bool:
    """Return whether an address is a routable public address."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour, *(address.teredo or ())]
        if any(item is not None and not _is_public(item) for item in embedded):
            return False
        if address.is_site_local:
            return False
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


def _is_local_network(address: Address) -> bool:
    """Return whether an address is on a private LAN (never loopback or link-local)."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in LOCAL_NETWORKS)


def address_allowed(address: Address, *, allow_local: bool = False) -> bool:
    """Return whether Music Assistant may fetch media from ``address``."""
    return _is_public(address) or (allow_local and _is_local_network(address))


def _parse_address(value: Any) -> Address | None:
    try:
        return ipaddress.ip_address(str(value or "").strip().split("%", 1)[0])
    except ValueError:
        return None


async def async_resolve_host(host: str, port: int) -> list[str]:
    """Resolve a host name to the textual addresses a TCP client would connect to."""
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(RESOLVE_TIMEOUT_SECONDS):
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def embedded_media_urls(reference: Any) -> list[str]:
    """Return the URLs in a media reference that Music Assistant would fetch.

    A plain ``http(s)`` URL is returned as is. For a Music Assistant URI, every URL that
    appears in it (also percent-encoded) is returned from where it starts. A reference
    with a non-HTTP network or file scheme is returned as is so it is refused.
    """
    if not isinstance(reference, str):
        return []
    clean = reference.strip()
    match = _SCHEME_RE.match(clean)
    scheme = match.group(1).lower() if match else ""
    if scheme in ("http", "https") or scheme in NON_HTTP_URL_SCHEMES:
        return [clean]
    decoded = clean
    for _ in range(3):
        unquoted = unquote(decoded)
        if unquoted == decoded:
            break
        decoded = unquoted
    return [decoded[found.start() :] for found in _EMBEDDED_URL_RE.finditer(decoded)]


def media_reference_values(value: Any) -> list[str]:
    """Return the string references in a media argument (string, item dict or list)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [value["uri"]] if isinstance(value.get("uri"), str) else []
    if isinstance(value, (list, tuple)):
        return [reference for item in value for reference in media_reference_values(item)]
    return []


def command_media_references(command: str, args: Any) -> list[str]:
    """Return the media references in the arguments of a Music Assistant command."""
    if not isinstance(args, dict):
        return []
    keys = MEDIA_REFERENCE_ARGS.get(str(command or "").strip(), ())
    return [reference for key in keys for reference in media_reference_values(args.get(key))]


async def async_validate_media_url(
    url: str,
    *,
    trusted_bases: Iterable[str] = (),
    allow_local: bool = False,
    resolve: Resolver | None = None,
) -> None:
    """Raise MediaUrlNotAllowed unless Music Assistant may fetch ``url``."""
    clean = str(url or "").strip()
    if _UNSAFE_URL_RE.search(clean):
        raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: it contains invalid characters.")
    try:
        parts = urlsplit(clean)
    except ValueError as err:
        raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: it is not a valid URL.") from err
    if parts.scheme.lower() not in ("http", "https"):
        raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: only http and https URLs are supported.")
    if "@" in parts.netloc:
        raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: URLs with credentials are not supported.")
    origin = _origin(clean)
    if origin is None:
        raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: it is not a valid URL.")
    if any(origin == _origin(base) for base in trusted_bases):
        return
    _scheme, host, port = origin
    local_refusal = f"{_NOT_ALLOWED}: it points to a local, private or reserved network address."
    if host == "localhost" or host.endswith(".localhost"):
        raise MediaUrlNotAllowed(local_refusal)
    literal = _parse_address(host)
    if literal is not None:
        addresses = [literal]
    else:
        try:
            resolved = await (resolve or async_resolve_host)(host, port)
        except (OSError, TimeoutError, UnicodeError, ValueError) as err:
            raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: its host could not be resolved.") from err
        addresses = [_parse_address(value) for value in resolved]
        if not addresses:
            raise MediaUrlNotAllowed(f"{_NOT_ALLOWED}: its host could not be resolved.")
    if any(
        address is None or not address_allowed(address, allow_local=allow_local)
        for address in addresses
    ):
        raise MediaUrlNotAllowed(local_refusal if allow_local else f"{local_refusal} {_LOCAL_HINT}")


async def async_validate_media_reference(
    reference: Any,
    *,
    trusted_bases: Iterable[str] = (),
    allow_local: bool = False,
    resolve: Resolver | None = None,
) -> None:
    """Validate every URL a media reference would make Music Assistant fetch."""
    bases = list(trusted_bases)
    for url in embedded_media_urls(reference):
        await async_validate_media_url(
            url, trusted_bases=bases, allow_local=allow_local, resolve=resolve
        )
