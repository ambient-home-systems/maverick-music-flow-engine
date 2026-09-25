"""Hardened artwork fetching shared by the Engine's HTTP artwork proxy views.

Artwork sources come from Music Assistant metadata and from public directories such as
Radio Browser, so every source URL is treated as untrusted input:

- only ``http`` and ``https`` URLs are fetched,
- redirects are never followed automatically; at most ``MAX_REDIRECTS`` hops are followed
  by hand and every hop is validated again,
- bodies are streamed and abandoned once they exceed ``MAX_ARTWORK_BYTES``,
- only the raster image types in ``ALLOWED_IMAGE_TYPES`` are accepted, and the file's magic
  bytes must match the declared type (SVG, HTML and everything else is rejected),
- hosts that are not a configured Music Assistant base URL (or the Home Assistant URL used
  for HA-relative sources) may only resolve to public addresses; this is enforced by the
  connector's resolver, so DNS rebinding cannot bypass it,
- the Music Assistant bearer token is only sent when scheme, host and port exactly equal
  a configured Music Assistant base URL.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import aiohttp
from aiohttp import ClientError, ClientTimeout
from aiohttp.abc import AbstractResolver
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util.ssl import client_context

from .const import DOMAIN

MAX_ARTWORK_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
READ_CHUNK_SIZE = 64 * 1024
ARTWORK_TIMEOUT = ClientTimeout(total=8, connect=4)
ALLOWED_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif", "image/bmp"}
)
ARTWORK_ACCEPT_HEADER = "image/jpeg, image/png, image/webp, image/gif, image/avif, image/bmp"
ARTWORK_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; sandbox",
}
_IMAGE_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/x-bmp": "image/bmp",
    "image/x-ms-bmp": "image/bmp",
}
_AVIF_BRANDS = {b"avif", b"avis"}
_STRICT_SESSION_KEY = "artwork_strict_session"


def normalize_image_type(value: Any) -> str:
    """Return the canonical MIME type for an allowed image type, or an empty string."""
    clean = str(value or "").split(";", 1)[0].strip().lower()
    clean = _IMAGE_TYPE_ALIASES.get(clean, clean)
    return clean if clean in ALLOWED_IMAGE_TYPES else ""


def detect_image_type(body: bytes) -> str:
    """Return the image type identified by the file's magic bytes, or an empty string."""
    head = bytes(body[:16])
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[4:8] == b"ftyp":
        box_size = int.from_bytes(head[:4], "big")
        box_end = box_size if 16 <= box_size <= 256 else 32
        ftyp = bytes(body[8:box_end])
        brands = [ftyp[0:4], *[ftyp[index : index + 4] for index in range(8, len(ftyp), 4)]]
        if any(brand in _AVIF_BRANDS for brand in brands):
            return "image/avif"
    return ""


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
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


def is_public_address(value: Any) -> bool:
    """Return whether a textual IP address is public; anything unparsable is not."""
    try:
        address = ipaddress.ip_address(str(value or "").strip().split("%", 1)[0])
    except ValueError:
        return False
    return _is_public(address)


def is_blocked_hostname(host: Any) -> bool:
    """Return whether a host name must never be fetched from an untrusted source."""
    clean = str(host or "").strip().lower().rstrip(".")
    return not clean or clean == "localhost" or clean.endswith(".localhost")


class PublicAddressResolver(AbstractResolver):
    """Resolve names with the wrapped resolver but drop every non-public address.

    aiohttp resolves the host of every new connection through the connector's resolver,
    so a name that is rebound to a private address after a first lookup is rejected as
    well.
    """

    def __init__(self, resolver: AbstractResolver) -> None:
        """Wrap an aiohttp resolver."""
        self._resolver = resolver

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[Any]:
        """Return only public addresses for ``host``."""
        results = await self._resolver.resolve(host, port, family=family)
        allowed = [result for result in results if is_public_address(result.get("host"))]
        if not allowed:
            raise OSError(f"{host} does not resolve to a public address")
        return allowed

    async def close(self) -> None:
        """Close the wrapped resolver."""
        await self._resolver.close()


def async_get_strict_artwork_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Return the shared session that only connects to public addresses."""
    data = hass.data.setdefault(DOMAIN, {})
    session = data.get(_STRICT_SESSION_KEY)
    if isinstance(session, aiohttp.ClientSession) and not session.closed:
        return session
    connector = aiohttp.TCPConnector(
        ssl=client_context(),
        resolver=PublicAddressResolver(aiohttp.DefaultResolver()),
        limit=8,
        ttl_dns_cache=10,
    )
    session = aiohttp.ClientSession(connector=connector, timeout=ARTWORK_TIMEOUT)
    data[_STRICT_SESSION_KEY] = session

    async def _async_close_session(_event: Any) -> None:
        await session.close()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _async_close_session)
    return session


def home_assistant_base_url(hass: HomeAssistant) -> str:
    """Return this Home Assistant instance's own URL, preferring the internal one."""
    try:
        return get_url(hass, prefer_external=False).rstrip("/")
    except NoURLAvailableError:
        return ""


def _split_base(url: Any) -> tuple[str, str, int] | None:
    """Return (scheme, host, port) for an HTTP(S) URL, with the default port filled in."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.scheme, parts.hostname.lower().rstrip("."), port or (443 if parts.scheme == "https" else 80)


def url_matches_base(url: Any, base: Any) -> bool:
    """Return whether scheme, host and port of ``url`` exactly equal those of ``base``."""
    url_parts = _split_base(url)
    return url_parts is not None and url_parts == _split_base(base)


def is_music_assistant_host(host: Any, ma_base_urls: list[str]) -> bool:
    """Return whether a host name equals the host of a configured Music Assistant URL."""
    clean = str(host or "").strip().lower().rstrip(".")
    if not clean:
        return False
    hosts = {parts[1] for parts in (_split_base(base) for base in ma_base_urls) if parts}
    return clean in hosts


def artwork_fetch_urls(source: str, ma_base_urls: list[str], ha_base_url: str = "") -> list[str]:
    """Return the absolute URLs to try for an artwork source.

    URLs are only ever built from configured Music Assistant base URLs and the Home
    Assistant URL passed in, never from the incoming request. The ``/imageproxy`` rewrite
    onto every Music Assistant base URL is only applied when the source already points
    at a configured Music Assistant host.
    """
    clean = str(source or "").strip()
    ha_base = str(ha_base_url or "").rstrip("/")
    bases = [base.rstrip("/") for base in ma_base_urls if _split_base(base)]
    if not clean or clean.startswith(("data:", "blob:")):
        return []
    if clean.startswith("//"):
        return [f"https:{clean}", f"http:{clean}"]
    candidates: list[str] = []
    if clean.startswith(("http://", "https://")):
        candidates.append(clean)
        parsed = urlsplit(clean)
        if is_music_assistant_host(parsed.hostname, bases):
            path_index = parsed.path.find("/imageproxy")
            imageproxy_path = parsed.path[path_index:] if path_index >= 0 else ""
            if imageproxy_path:
                suffix = f"{imageproxy_path}{f'?{parsed.query}' if parsed.query else ''}"
                candidates.extend(f"{base}{suffix}" for base in bases)
            if parsed.path.rstrip("/").endswith("/imageproxy"):
                query = parse_qs(parsed.query)
                provider = str((query.get("provider") or ["builtin"])[0] or "builtin")
                image_path = unquote(unquote(str((query.get("path") or [""])[0] or "")))
                if image_path:
                    image_id = hashlib.sha256(
                        f"{provider}/{image_path}".encode(),
                        usedforsecurity=False,
                    ).hexdigest()
                    candidates.append(f"{parsed.scheme}://{parsed.netloc}/imageproxy/{image_id}?size=512")
                    candidates.extend(f"{base}/imageproxy/{image_id}?size=512" for base in bases)
    else:
        if clean.startswith(("/imageproxy", "imageproxy")):
            path = clean if clean.startswith("/") else f"/{clean}"
            candidates.extend(f"{base}{path}" for base in bases)
        elif clean.startswith("/"):
            if ha_base:
                candidates.append(f"{ha_base}{clean}")
        else:
            candidates.extend(f"{base}/imageproxy?path={quote(clean)}&size=512" for base in bases)
            if ha_base:
                candidates.append(f"{ha_base}/{quote(clean.lstrip('/'))}")
    return [candidate for index, candidate in enumerate(candidates) if candidate and candidate not in candidates[:index]]


async def _async_read_limited(stream: Any) -> bytes | None:
    """Read a response body in chunks and give up once it exceeds the size limit."""
    body = bytearray()
    async for chunk in stream.iter_chunked(READ_CHUNK_SIZE):
        body.extend(chunk)
        if len(body) > MAX_ARTWORK_BYTES:
            return None
    return bytes(body)


@dataclass(slots=True)
class ArtworkPayload:
    """Validated artwork bytes and the image type proven by their magic bytes."""

    body: bytes
    content_type: str


class ArtworkFetcher:
    """Fetch artwork through explicit trust boundaries.

    ``trusted_session`` is used for configured Music Assistant base URLs and the Home
    Assistant URL (which are usually private addresses). ``strict_session`` must refuse
    to connect to non-public addresses and is used for everything else.
    """

    def __init__(
        self,
        *,
        trusted_session: Any,
        strict_session: Any,
        ma_base_urls: list[str],
        ma_tokens: list[str],
        ha_base_url: str = "",
    ) -> None:
        """Bind sessions and the configured base URLs."""
        self._trusted_session = trusted_session
        self._strict_session = strict_session
        self._ma_base_urls = [str(base) for base in ma_base_urls if _split_base(base)]
        self._ma_token = next((str(token) for token in ma_tokens if str(token or "").strip()), "")
        self._ha_base_url = str(ha_base_url or "").strip()

    def classify(self, url: str) -> tuple[bool, bool] | None:
        """Return ``(trusted, send_bearer)`` for a URL, or None when it must not be fetched."""
        if _split_base(url) is None:
            return None
        host = urlsplit(str(url or "").strip()).hostname or ""
        if is_blocked_hostname(host):
            return None
        send_bearer = any(url_matches_base(url, base) for base in self._ma_base_urls)
        trusted = send_bearer or (bool(self._ha_base_url) and url_matches_base(url, self._ha_base_url))
        if not trusted:
            try:
                literal = ipaddress.ip_address(host.split("%", 1)[0])
            except ValueError:
                literal = None
            if literal is not None and not _is_public(literal):
                return None
        return trusted, send_bearer

    async def async_fetch(self, url: str) -> ArtworkPayload | None:
        """Return validated artwork for ``url`` or None when it cannot be served safely."""
        current = str(url or "").strip()
        try:
            for _hop in range(MAX_REDIRECTS + 1):
                verdict = self.classify(current)
                if verdict is None:
                    return None
                trusted, send_bearer = verdict
                headers = {"Accept": ARTWORK_ACCEPT_HEADER}
                if send_bearer and self._ma_token:
                    headers["Authorization"] = f"Bearer {self._ma_token}"
                session = self._trusted_session if trusted else self._strict_session
                async with session.get(
                    current, headers=headers, timeout=ARTWORK_TIMEOUT, allow_redirects=False
                ) as response:
                    if 300 <= response.status < 400:
                        location = str(response.headers.get("Location") or "").strip()
                        if not location:
                            return None
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status < 300:
                        return None
                    content_type = normalize_image_type(response.headers.get("Content-Type"))
                    if not content_type:
                        return None
                    length = str(response.headers.get("Content-Length") or "").strip()
                    if length and (not length.isdigit() or int(length) > MAX_ARTWORK_BYTES):
                        return None
                    body = await _async_read_limited(response.content)
                    if not body or detect_image_type(body) != content_type:
                        return None
                    return ArtworkPayload(body, content_type)
        except (ClientError, OSError, ValueError):
            return None
        return None
