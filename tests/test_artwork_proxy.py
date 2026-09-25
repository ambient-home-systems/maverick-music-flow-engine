"""Exercise the hardened artwork proxy: keyed tokens, fetch validation and trust boundaries.

The real helper, runtime methods and HTTP views run against fake aiohttp sessions at the
network boundary, without installing Home Assistant or aiohttp.
"""
from __future__ import annotations

import ast
import hashlib
import ipaddress
import logging
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import IsolatedAsyncioTestCase, TestCase, main
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/maverick_music_flow"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 10
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8
AVIF = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf" + b"\x00" * 8
BMP = b"BM" + b"\x00" * 14
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
HTML = b"<html><script>fetch('/x?'+localStorage.hassTokens)</script></html>"
MIB = 1024 * 1024


class HTTPError(Exception):
    def __init__(self, text=""):
        super().__init__(text)
        self.text = text


class Response:
    """Stand-in for aiohttp.web.Response."""

    def __init__(self, *, body=b"", status=200, content_type="", headers=None):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.headers = dict(headers or {})


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.read_bytes = 0

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            self.read_bytes += len(chunk)
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunks=None):
        self.status = status
        self.headers = dict(headers or {})
        self.content = FakeStream(chunks if chunks is not None else ([body] if body else []))
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True


class FakeSession:
    """Records every request and routes URLs to canned responses."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, *, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"url": url, "headers": dict(headers or {}), "allow_redirects": allow_redirects})
        response = self.routes.get(url, FakeResponse(status=404))
        return response() if callable(response) else response


def image(body, content_type, **headers):
    return FakeResponse(headers={"Content-Type": content_type, **headers}, body=body)


def load_artwork_proxy():
    source = COMPONENT / "artwork_proxy.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    body = [node for node in tree.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *body],
        type_ignores=[],
    )
    ns = dict(
        hashlib=hashlib, ipaddress=ipaddress, logging=logging, socket=socket, dataclass=dataclass, Any=Any,
        parse_qs=parse_qs, quote=quote, unquote=unquote, urljoin=urljoin, urlsplit=urlsplit,
        aiohttp=SimpleNamespace(), ClientError=type("ClientError", (Exception,), {}),
        ClientTimeout=lambda **kwargs: kwargs, AbstractResolver=object,
        EVENT_HOMEASSISTANT_CLOSE="homeassistant_close", HomeAssistant=object,
        NoURLAvailableError=type("NoURLAvailableError", (Exception,), {}), get_url=None, client_context=None,
        DOMAIN="maverick_music_flow",
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), ns)
    return ns


PROXY = load_artwork_proxy()


def load_views():
    source = COMPONENT / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {
        "_looks_like_artwork_url", "_append_artwork_candidate", "_collect_artwork_candidates",
        "HomeiiFlowArtworkProxyView", "HomeiiFlowItemArtworkProxyView",
    }
    body = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in wanted]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *body],
        type_ignores=[],
    )
    ns = dict(
        hashlib=hashlib, Any=Any, HomeAssistantView=object, HomeAssistant=object, HomeiiFlowRuntime=object,
        web=SimpleNamespace(Response=Response, HTTPNotFound=HTTPError, Request=object),
        ArtworkFetcher=PROXY["ArtworkFetcher"], ArtworkPayload=PROXY["ArtworkPayload"],
        artwork_fetch_urls=PROXY["artwork_fetch_urls"], ARTWORK_SECURITY_HEADERS=PROXY["ARTWORK_SECURITY_HEADERS"],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), ns)
    return ns


def load_runtime_token_methods():
    source = COMPONENT / "runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    methods = {
        "_restore_artwork_token_secret", "register_artwork_source", "resolve_artwork_source",
        "cached_artwork_content", "cache_artwork_content",
    }
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HomeiiFlowRuntime")
    cls.decorator_list = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    constants = [
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ARTWORK_TOKEN_LIFETIME" for t in n.targets)
    ]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *constants, cls],
        type_ignores=[],
    )
    ns = dict(hashlib=hashlib, time=time, Any=Any, normalize_image_type=PROXY["normalize_image_type"])
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), ns)
    return ns


RUNTIME_NS = load_runtime_token_methods()
Runtime = RUNTIME_NS["HomeiiFlowRuntime"]


def make_runtime(secret=None):
    runtime = Runtime.__new__(Runtime)
    runtime._artwork_sources = {}
    runtime._artwork_source_tokens = {}
    runtime._artwork_content_cache = {}
    runtime._artwork_token_secret = secret or secrets.token_bytes(32)
    runtime._storage = {}
    return runtime


class TokenTests(TestCase):
    SOURCE = "http://radio.test/logo.png"

    def test_same_secret_gives_same_token_and_other_secrets_differ(self):
        secret = secrets.token_bytes(32)
        first = make_runtime(secret).register_artwork_source(self.SOURCE)
        second = make_runtime(secret).register_artwork_source(self.SOURCE)
        other = make_runtime(secrets.token_bytes(32)).register_artwork_source(self.SOURCE)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("/api/maverick_music_flow/artwork/item/"))
        self.assertNotEqual(first, other)
        self.assertEqual(len(first.rsplit("/", 1)[-1]), 36)

    def test_token_cannot_be_recomputed_without_the_secret(self):
        token = make_runtime().register_artwork_source(self.SOURCE).rsplit("/", 1)[-1]
        unkeyed = hashlib.blake2s(self.SOURCE.encode("utf-8"), digest_size=18).hexdigest()
        self.assertNotEqual(token, unkeyed)
        self.assertNotEqual(token, hashlib.blake2s(self.SOURCE.encode("utf-8")).hexdigest()[:36])
        self.assertNotEqual(token, hashlib.sha256(self.SOURCE.encode("utf-8")).hexdigest()[:36])

    def test_restored_secret_is_persisted_and_invalid_values_are_ignored(self):
        runtime = make_runtime()
        generated = runtime._artwork_token_secret
        for bad in ("", None, "nope", "zz" * 32, "ab" * 31, 12345):
            self.assertFalse(runtime._restore_artwork_token_secret(bad))
        self.assertEqual(runtime._artwork_token_secret, generated)
        self.assertNotIn("artwork_token_secret", runtime._storage)
        stale_url = runtime.register_artwork_source(self.SOURCE)
        persisted = secrets.token_bytes(32)
        self.assertTrue(runtime._restore_artwork_token_secret(persisted.hex().upper()))
        self.assertEqual(runtime._artwork_token_secret, persisted)
        self.assertEqual(runtime._storage["artwork_token_secret"], persisted.hex())
        self.assertEqual(runtime.resolve_artwork_source(stale_url.rsplit("/", 1)[-1]), "")
        self.assertNotEqual(runtime.register_artwork_source(self.SOURCE), stale_url)

    def test_tokens_expire_and_slide_on_use(self):
        runtime = make_runtime()
        lifetime = RUNTIME_NS["ARTWORK_TOKEN_LIFETIME"]
        self.assertEqual(lifetime, 48 * 60 * 60)
        token = runtime.register_artwork_source(self.SOURCE).rsplit("/", 1)[-1]
        self.assertAlmostEqual(runtime._artwork_sources[token][1], time.monotonic() + lifetime, delta=5)
        runtime._artwork_sources[token] = (self.SOURCE, time.monotonic() + 10)
        self.assertEqual(runtime.resolve_artwork_source(token), self.SOURCE)
        self.assertAlmostEqual(runtime._artwork_sources[token][1], time.monotonic() + lifetime, delta=5)
        runtime._artwork_sources[token] = (self.SOURCE, time.monotonic() - 1)
        self.assertEqual(runtime.resolve_artwork_source(token), "")
        self.assertNotIn(token, runtime._artwork_sources)

    def test_content_cache_only_keeps_allowed_image_types(self):
        runtime = make_runtime()
        runtime.cache_artwork_content(self.SOURCE, HTML, "text/html")
        runtime.cache_artwork_content(self.SOURCE, SVG, "image/svg+xml")
        runtime.cache_artwork_content(self.SOURCE, PNG, "")
        self.assertIsNone(runtime.cached_artwork_content(self.SOURCE))
        runtime.cache_artwork_content(self.SOURCE, JPEG, "image/jpg; charset=binary")
        self.assertEqual(runtime.cached_artwork_content(self.SOURCE), (JPEG, "image/jpeg"))


class ValidationTests(TestCase):
    def test_magic_bytes_identify_only_allowed_raster_types(self):
        detect = PROXY["detect_image_type"]
        for body, expected in ((PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp"),
                               (AVIF, "image/avif"), (BMP, "image/bmp")):
            self.assertEqual(detect(body), expected)
        for body in (SVG, HTML, b"", b"\x00\x00\x00\x18ftypmp42", b"RIFF\x00\x00\x00\x00WAVE"):
            self.assertEqual(detect(body), "")

    def test_content_types_are_normalized_and_restricted(self):
        normalize = PROXY["normalize_image_type"]
        self.assertEqual(normalize("image/jpg; charset=binary"), "image/jpeg")
        self.assertEqual(normalize(" IMAGE/PNG "), "image/png")
        for value in ("text/html", "image/svg+xml", "application/octet-stream", "image/x-icon", "", None):
            self.assertEqual(normalize(value), "")

    def test_public_address_check_covers_every_reserved_range(self):
        public = PROXY["is_public_address"]
        for value in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "224.0.0.1",
                      "240.0.0.1", "100.64.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:10.0.0.1",
                      "::ffff:127.0.0.1", "2002:c0a8:0101::1", "2001:0:c0a8:101::1", "not-an-ip", ""):
            self.assertFalse(public(value), value)
        for value in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(public(value), value)

    def test_base_matching_is_exact_on_scheme_host_and_port(self):
        match = PROXY["url_matches_base"]
        base = "http://ma-host:8095"
        self.assertTrue(match("http://ma-host:8095/imageproxy/x", base))
        self.assertTrue(match("http://MA-HOST:8095/x", base))
        self.assertTrue(match("http://ma-host/x", "http://ma-host:80/"))
        self.assertTrue(match("https://ma-host/x", "https://ma-host:443"))
        self.assertFalse(match("http://ma-host:8095.evil.example/x", base))
        self.assertFalse(match("http://ma-host:8096/x", base))
        self.assertFalse(match("https://ma-host:8095/x", base))
        self.assertFalse(match("http://ma-host:8095@evil.example/x", base))
        self.assertFalse(match("http://ma-host:99999/x", base))

    def test_fetch_urls_come_only_from_configured_bases(self):
        urls = PROXY["artwork_fetch_urls"]
        ma = ["http://ma-host:8095", "https://ma.external.example"]
        ha = "http://ha.internal:8123"
        self.assertEqual(
            urls("/api/media_player_proxy/media_player.x?token=t", ma, ha),
            ["http://ha.internal:8123/api/media_player_proxy/media_player.x?token=t"],
        )
        self.assertEqual(urls("/api/media_player_proxy/media_player.x", ma, ""), [])
        self.assertEqual(urls("/imageproxy/abc?size=512", ma, ha), [
            "http://ma-host:8095/imageproxy/abc?size=512", "https://ma.external.example/imageproxy/abc?size=512",
        ])
        self.assertEqual(urls("cover art.jpg", ma, ha), [
            "http://ma-host:8095/imageproxy?path=cover%20art.jpg&size=512",
            "https://ma.external.example/imageproxy?path=cover%20art.jpg&size=512",
            "http://ha.internal:8123/cover%20art.jpg",
        ])
        self.assertEqual(urls("//cdn.example/x.png", ma, ha), ["https://cdn.example/x.png", "http://cdn.example/x.png"])
        self.assertEqual(urls("data:image/png;base64,AAAA", ma, ha), [])
        self.assertEqual(urls("", ma, ha), [])

    def test_imageproxy_rewrite_only_for_music_assistant_hosts(self):
        urls = PROXY["artwork_fetch_urls"]
        ma = ["http://ma-host:8095", "https://ma.external.example"]
        attacker = "https://attacker.example/a/imageproxy?path=http://169.254.169.254/latest&provider=builtin"
        self.assertEqual(urls(attacker, ma, "http://ha.internal:8123"), [attacker])
        own = "http://ma-host:8095/imageproxy/abc?size=256"
        self.assertEqual(urls(own, ma, ""), [own, "https://ma.external.example/imageproxy/abc?size=256"])
        query = "https://ma.external.example/imageproxy?path=cover.jpg&provider=spotify"
        image_id = hashlib.sha256(b"spotify/cover.jpg").hexdigest()
        self.assertEqual(urls(query, ma, ""), [
            query,
            "http://ma-host:8095/imageproxy?path=cover.jpg&provider=spotify",
            f"https://ma.external.example/imageproxy/{image_id}?size=512",
            f"http://ma-host:8095/imageproxy/{image_id}?size=512",
        ])


class FetchTests(IsolatedAsyncioTestCase):
    MA = "http://ma-host:8095"

    def fetcher(self, ma_base_urls=(MA,), ha_base_url="", trusted=None, strict=None):
        self.trusted = trusted or FakeSession()
        self.strict = strict or FakeSession()
        return PROXY["ArtworkFetcher"](
            trusted_session=self.trusted, strict_session=self.strict,
            ma_base_urls=list(ma_base_urls), ma_tokens=["ma-secret"], ha_base_url=ha_base_url,
        )

    async def test_html_svg_and_mismatched_magic_bytes_are_rejected(self):
        url = "https://radio.example/logo"
        for body, content_type in ((HTML, "text/html"), (SVG, "image/svg+xml"), (JPEG, "image/png"),
                                   (PNG, "image/jpeg"), (HTML, "image/png"), (PNG, "text/html"),
                                   (PNG, "application/octet-stream"), (PNG, "")):
            fetcher = self.fetcher(strict=FakeSession({url: image(body, content_type)}))
            self.assertIsNone(await fetcher.async_fetch(url), (body[:8], content_type))

    async def test_matching_images_are_served_with_the_validated_type(self):
        url = "https://radio.example/logo"
        for body, declared, expected in ((PNG, "image/png", "image/png"), (JPEG, "image/jpg; charset=binary", "image/jpeg"),
                                         (GIF, "image/gif", "image/gif"), (WEBP, "image/webp", "image/webp"),
                                         (AVIF, "image/avif", "image/avif"), (BMP, "image/x-ms-bmp", "image/bmp")):
            fetcher = self.fetcher(strict=FakeSession({url: image(body, declared)}))
            payload = await fetcher.async_fetch(url)
            self.assertEqual((payload.body, payload.content_type), (body, expected))
        self.assertEqual(self.strict.calls[-1]["allow_redirects"], False)
        self.assertNotIn("Authorization", self.strict.calls[-1]["headers"])

    async def test_oversized_bodies_are_rejected_without_reading_everything(self):
        url = "https://radio.example/huge.png"
        declared = image(PNG, "image/png", **{"Content-Length": str(6 * MIB)})
        fetcher = self.fetcher(strict=FakeSession({url: declared}))
        self.assertIsNone(await fetcher.async_fetch(url))
        self.assertEqual(declared.content.read_bytes, 0)
        chunks = [PNG + b"\x00" * (MIB - len(PNG))] + [b"\x00" * MIB] * 99
        streamed = FakeResponse(headers={"Content-Type": "image/png"}, chunks=chunks)
        fetcher = self.fetcher(strict=FakeSession({url: streamed}))
        self.assertIsNone(await fetcher.async_fetch(url))
        self.assertLessEqual(streamed.content.read_bytes, PROXY["MAX_ARTWORK_BYTES"] + MIB)
        self.assertTrue(streamed.closed)
        exact = FakeResponse(headers={"Content-Type": "image/png"}, chunks=[PNG + b"\x00" * (5 * MIB - len(PNG))])
        fetcher = self.fetcher(strict=FakeSession({url: exact}))
        self.assertEqual(len((await fetcher.async_fetch(url)).body), 5 * MIB)

    async def test_private_loopback_and_link_local_targets_are_refused(self):
        fetcher = self.fetcher()
        for url in ("http://192.168.1.20/a.png", "http://10.0.0.1/a.png", "http://172.16.5.5/a.png",
                    "http://127.0.0.1/a.png", "http://[::1]/a.png", "http://169.254.169.254/latest/meta-data",
                    "http://[::ffff:10.0.0.1]/a.png", "http://[fe80::1]/a.png", "http://localhost/a.png",
                    "http://app.localhost/a.png", "http://0.0.0.0/a.png", "http://100.64.0.1/a.png",
                    "http://[fc00::1]/a.png", "ftp://public.example/a.png", "file:///etc/passwd",
                    "javascript:alert(1)", "http:///a.png", ""):
            self.assertIsNone(await fetcher.async_fetch(url), url)
        self.assertEqual(self.trusted.calls, [])
        self.assertEqual(self.strict.calls, [])

    async def test_public_hosts_use_the_strict_session(self):
        for url in ("https://cdn.example/logo.png", "http://93.184.216.34/logo.png"):
            fetcher = self.fetcher(strict=FakeSession({url: image(PNG, "image/png")}))
            self.assertIsNotNone(await fetcher.async_fetch(url))
            self.assertEqual([call["url"] for call in self.strict.calls], [url])
            self.assertEqual(self.trusted.calls, [])

    async def test_private_music_assistant_base_url_still_works(self):
        base = "http://192.168.1.10:8095"
        url = f"{base}/imageproxy/abc?size=512"
        fetcher = self.fetcher(ma_base_urls=[base], trusted=FakeSession({url: image(JPEG, "image/jpeg")}))
        payload = await fetcher.async_fetch(url)
        self.assertEqual(payload.content_type, "image/jpeg")
        self.assertEqual(self.trusted.calls[0]["headers"]["Authorization"], "Bearer ma-secret")
        self.assertEqual(self.strict.calls, [])

    async def test_home_assistant_url_is_trusted_without_bearer_token(self):
        url = "http://192.168.1.5:8123/api/media_player_proxy/media_player.x?token=t"
        fetcher = self.fetcher(ha_base_url="http://192.168.1.5:8123", trusted=FakeSession({url: image(PNG, "image/png")}))
        self.assertIsNotNone(await fetcher.async_fetch(url))
        self.assertNotIn("Authorization", self.trusted.calls[0]["headers"])
        self.assertEqual(self.strict.calls, [])

    async def test_resolver_drops_private_addresses_so_rebinding_fails(self):
        answers = [
            [{"hostname": "cdn.example", "host": "93.184.216.34", "port": 443}, {"hostname": "cdn.example", "host": "10.0.0.9", "port": 443}],
            [{"hostname": "cdn.example", "host": "10.0.0.9", "port": 443}],
            [{"hostname": "cdn.example", "host": "::ffff:192.168.1.1", "port": 443}],
        ]
        closed = []

        class Inner:
            async def resolve(self, host, port=0, family=socket.AF_INET):
                return answers.pop(0)

            async def close(self):
                closed.append(True)

        resolver = PROXY["PublicAddressResolver"](Inner())
        first = await resolver.resolve("cdn.example", 443, family=socket.AF_INET)
        self.assertEqual([item["host"] for item in first], ["93.184.216.34"])
        with self.assertRaises(OSError):
            await resolver.resolve("cdn.example", 443, family=socket.AF_INET)
        with self.assertRaises(OSError):
            await resolver.resolve("cdn.example", 443, family=socket.AF_INET6)
        await resolver.close()
        self.assertEqual(closed, [True])

    async def test_redirect_to_private_address_is_rejected(self):
        url = "https://cdn.example/logo.png"
        for target in ("http://10.0.0.5/secret.png", "http://127.0.0.1:8123/api/", "http://[::1]/x", "/../../..",
                       "file:///etc/passwd", ""):
            headers = {"Location": target} if target else {}
            fetcher = self.fetcher(strict=FakeSession({url: FakeResponse(status=302, headers=headers)}))
            self.assertIsNone(await fetcher.async_fetch(url), target)
            self.assertEqual([call["url"] for call in self.strict.calls], [url] if target != "/../../.." else [url, "https://cdn.example/"])
            self.assertEqual(self.trusted.calls, [])

    async def test_redirects_are_limited_and_each_hop_is_revalidated(self):
        hops = [f"https://cdn.example/{index}.png" for index in range(5)]
        routes = {hops[i]: FakeResponse(status=301, headers={"Location": hops[i + 1]}) for i in range(4)}
        routes[hops[4]] = image(PNG, "image/png")
        fetcher = self.fetcher(strict=FakeSession(routes))
        self.assertIsNone(await fetcher.async_fetch(hops[0]))
        self.assertEqual(len(self.strict.calls), 4)
        fetcher = self.fetcher(strict=FakeSession(routes))
        self.assertIsNotNone(await fetcher.async_fetch(hops[1]))
        self.assertEqual([call["url"] for call in self.strict.calls], hops[1:])
        relative = FakeSession({
            "https://cdn.example/a/logo": FakeResponse(status=302, headers={"Location": "../b/logo.png"}),
            "https://cdn.example/b/logo.png": image(PNG, "image/png"),
        })
        fetcher = self.fetcher(strict=relative)
        self.assertIsNotNone(await fetcher.async_fetch("https://cdn.example/a/logo"))

    async def test_bearer_token_is_dropped_when_music_assistant_redirects_elsewhere(self):
        start = f"{self.MA}/imageproxy/abc"
        trusted = FakeSession({start: FakeResponse(status=302, headers={"Location": "https://cdn.example/x.png"})})
        strict = FakeSession({"https://cdn.example/x.png": image(PNG, "image/png")})
        fetcher = self.fetcher(trusted=trusted, strict=strict)
        self.assertIsNotNone(await fetcher.async_fetch(start))
        self.assertEqual(trusted.calls[0]["headers"]["Authorization"], "Bearer ma-secret")
        self.assertNotIn("Authorization", strict.calls[0]["headers"])

    async def test_bearer_token_only_for_exact_music_assistant_base(self):
        # A malformed port such as "8095.evil.example" cannot be parsed, so the URL is
        # refused before any request is made: no token can leak.
        fetcher = self.fetcher()
        self.assertIsNone(await fetcher.async_fetch("http://ma-host:8095.evil.example/a.png"))
        self.assertEqual(self.trusted.calls + self.strict.calls, [])
        for url in ("http://ma-host.evil.example:8095/a.png", "http://ma-host:8096/a.png", "https://ma-host:8095/a.png",
                    "http://ma-host/a.png", "http://evil.example/http://ma-host:8095/a.png"):
            fetcher = self.fetcher(strict=FakeSession({url: image(PNG, "image/png")}))
            self.assertIsNotNone(await fetcher.async_fetch(url), url)
            self.assertEqual(len(self.strict.calls), 1, url)
            self.assertNotIn("Authorization", self.strict.calls[0]["headers"], url)
            self.assertEqual(self.trusted.calls, [], url)
        for url in (f"{self.MA}/a.png", "http://MA-HOST:8095/imageproxy/x"):
            fetcher = self.fetcher(trusted=FakeSession({url: image(PNG, "image/png")}))
            self.assertIsNotNone(await fetcher.async_fetch(url), url)
            self.assertEqual(self.trusted.calls[0]["headers"]["Authorization"], "Bearer ma-secret")
            self.assertEqual(self.strict.calls, [])

    async def test_network_errors_and_bad_statuses_return_nothing(self):
        url = "https://cdn.example/logo.png"

        def boom():
            raise PROXY["ClientError"]("connection reset")

        for response in (boom, FakeResponse(status=404), FakeResponse(status=500), FakeResponse(status=204)):
            fetcher = self.fetcher(strict=FakeSession({url: response}))
            self.assertIsNone(await fetcher.async_fetch(url))


class ViewTests(IsolatedAsyncioTestCase):
    MA = "http://ma-host:8095"
    HA = "http://ha.internal:8123"

    def setUp(self):
        self.views = load_views()
        self.trusted = FakeSession()
        self.strict = FakeSession()
        self.views["async_get_clientsession"] = lambda _hass: self.trusted
        self.views["async_get_strict_artwork_session"] = lambda _hass: self.strict
        self.views["home_assistant_base_url"] = lambda _hass: self.HA
        self.request = SimpleNamespace(host="evil.example", scheme="http", headers={}, url="http://evil.example/x")

    def runtime(self, sources=None, cached=None, players=()):
        cache_calls = []
        runtime = SimpleNamespace(
            resolve_artwork_source=lambda token: (sources or {}).get(token, ""),
            cached_artwork_content=lambda source: (cached or {}).get(source),
            cache_artwork_content=lambda source, body, content_type: cache_calls.append((source, body, content_type)),
            music_assistant_base_urls=lambda: [self.MA],
            music_assistant_tokens=lambda: ["ma-secret"],
            media_players_snapshot=lambda **_: list(players),
            async_get_queue=AsyncMock(return_value={"data": {}}),
            cache_calls=cache_calls,
        )
        self.views["async_get_runtime"] = lambda _hass: runtime
        return runtime

    def item_view(self):
        return self.views["HomeiiFlowItemArtworkProxyView"](SimpleNamespace(data={}))

    def entity_view(self):
        return self.views["HomeiiFlowArtworkProxyView"](SimpleNamespace(data={}))

    def test_item_route_is_public_and_entity_route_is_authenticated(self):
        self.assertFalse(self.item_view().requires_auth)
        self.assertTrue(self.entity_view().requires_auth)

    async def test_forged_host_header_does_not_change_the_fetched_url(self):
        source = "/api/media_player_proxy/media_player.kitchen?token=abc"
        runtime = self.runtime(sources={"tok": source})
        self.trusted.routes[f"{self.HA}{source}"] = image(PNG, "image/png")
        response = await self.item_view().get(self.request, "tok")
        self.assertEqual((response.body, response.content_type), (PNG, "image/png"))
        self.assertEqual([call["url"] for call in self.trusted.calls], [f"{self.HA}{source}"])
        self.assertEqual(self.strict.calls, [])
        self.assertNotIn("evil.example", "".join(call["url"] for call in self.trusted.calls))
        self.assertEqual(runtime.cache_calls, [(source, PNG, "image/png")])
        self.views["home_assistant_base_url"] = lambda _hass: ""
        self.runtime(sources={"tok": source})
        with self.assertRaises(HTTPError):
            await self.item_view().get(self.request, "tok")
        self.assertEqual(len(self.trusted.calls), 1)

    async def test_item_view_serves_only_validated_images_with_security_headers(self):
        source = "https://radio.example/logo"
        for body, content_type in ((HTML, "text/html"), (SVG, "image/svg+xml"), (JPEG, "image/png")):
            runtime = self.runtime(sources={"tok": source})
            self.strict.routes[source] = image(body, content_type)
            with self.assertRaises(HTTPError):
                await self.item_view().get(self.request, "tok")
            self.assertEqual(runtime.cache_calls, [])
        runtime = self.runtime(sources={"tok": source})
        self.strict.routes[source] = image(PNG, "image/png; charset=utf-8")
        response = await self.item_view().get(self.request, "tok")
        self.assertEqual(response.content_type, "image/png")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertIn("ETag", response.headers)
        self.assertEqual(runtime.cache_calls, [(source, PNG, "image/png")])
        self.assertNotIn("Authorization", self.strict.calls[-1]["headers"])

    async def test_cached_and_conditional_responses_keep_security_headers(self):
        source = "https://radio.example/logo"
        self.runtime(sources={"tok": source}, cached={source: (PNG, "image/png")})
        response = await self.item_view().get(self.request, "tok")
        self.assertEqual(response.headers["X-HOMEii-Flow-Artwork-Source"], "memory-cache")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.request.headers["If-None-Match"] = response.headers["ETag"]
        not_modified = await self.item_view().get(self.request, "tok")
        self.assertEqual(not_modified.status, 304)
        self.assertEqual(not_modified.headers["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual(self.trusted.calls + self.strict.calls, [])

    async def test_unknown_token_and_private_sources_are_not_found(self):
        self.runtime(sources={"tok": "http://192.168.1.20/internal.png"})
        with self.assertRaises(HTTPError):
            await self.item_view().get(self.request, "missing")
        with self.assertRaises(HTTPError):
            await self.item_view().get(self.request, "tok")
        self.assertEqual(self.trusted.calls + self.strict.calls, [])

    async def test_entity_view_resolves_its_own_item_urls_locally(self):
        source = f"{self.MA}/imageproxy/abc?size=512"
        player = {"entity_id": "media_player.kitchen", "entity_picture": "/api/maverick_music_flow/artwork/item/tok",
                  "media_image_url": "/api/maverick_music_flow/artwork/item/tok?k=1", "artwork_candidates": []}
        self.runtime(sources={"tok": source}, players=[player])
        self.trusted.routes[source] = image(JPEG, "image/jpeg")
        response = await self.entity_view().get(self.request, "media_player.kitchen")
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["Content-Security-Policy"], "default-src 'none'; sandbox")
        self.assertEqual([call["url"] for call in self.trusted.calls], [source])
        self.assertEqual(self.trusted.calls[0]["headers"]["Authorization"], "Bearer ma-secret")
        self.assertEqual(self.strict.calls, [])

    async def test_entity_view_skips_unsafe_candidates_and_never_uses_request_host(self):
        player = {"entity_id": "media_player.kitchen", "entity_picture": "http://10.0.0.7/private.png",
                  "media_image_url": "/local/cover.png", "artwork_candidates": ["https://cdn.example/x.svg"]}
        self.runtime(players=[player])
        self.strict.routes["https://cdn.example/x.svg"] = image(SVG, "image/svg+xml")
        self.trusted.routes[f"{self.HA}/local/cover.png"] = image(PNG, "image/png")
        response = await self.entity_view().get(self.request, "media_player.kitchen")
        self.assertEqual(response.body, PNG)
        self.assertEqual([call["url"] for call in self.strict.calls], ["https://cdn.example/x.svg"])
        self.assertEqual([call["url"] for call in self.trusted.calls], [f"{self.HA}/local/cover.png"])
        with self.assertRaises(HTTPError):
            await self.entity_view().get(self.request, "media_player.unknown")


if __name__ == "__main__":
    main()
