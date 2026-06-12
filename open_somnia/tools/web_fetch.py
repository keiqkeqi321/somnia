from __future__ import annotations

import html
import http.client
import ipaddress
import re
import socket
import ssl
from typing import Any
from urllib.parse import urljoin, urlparse

from open_somnia.tools.registry import ToolDefinition


MAX_RESPONSE_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 15
MAX_REDIRECTS = 5
USER_AGENT = "somnia-web-fetch/1.0"
ACCEPT_HEADER = "text/html,text/plain,text/markdown,application/json,*/*;q=0.5"
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class WebFetchError(ValueError):
    pass


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _blocked_fetch_ip(ip: IPAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
        or (ip.version == CGNAT_NETWORK.version and ip in CGNAT_NETWORK)
    )


def _validate_fetch_url(url: str) -> tuple[str, str, int, str]:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise WebFetchError("web_fetch only supports http and https URLs.")
    if not parsed.hostname:
        raise WebFetchError("web_fetch URL must include a hostname.")
    if parsed.username or parsed.password:
        raise WebFetchError("web_fetch does not support URLs with embedded credentials.")
    host = parsed.hostname.rstrip(".")
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return parsed.scheme, host, port, target


def _resolve_public_ip(host: str, port: int) -> str:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked_fetch_ip(literal):
            raise WebFetchError(f"web_fetch blocked private or local address: {host}")
        return str(literal)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebFetchError(f"web_fetch could not resolve host '{host}': {exc}") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            address = ipaddress.ip_address(str(sockaddr[0]))
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise WebFetchError(f"web_fetch could not resolve host '{host}'.")
    blocked = [str(address) for address in addresses if _blocked_fetch_ip(address)]
    if blocked:
        raise WebFetchError(f"web_fetch blocked private or local resolved address: {', '.join(blocked)}")
    return str(addresses[0])


class _VettedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, vetted_ip: str, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._vetted_ip = vetted_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._vetted_ip, self.port), self.timeout, self.source_address)


class _VettedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, vetted_ip: str, timeout: int) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._vetted_ip = vetted_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._vetted_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


def _read_limited(response: http.client.HTTPResponse) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise WebFetchError("web_fetch response exceeded 1 MiB limit.")
    return body


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    if match:
        charset = match.group(1).strip("\"'")
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _looks_like_html(text: str) -> bool:
    prefix = text.lstrip()[:200].lower()
    return prefix.startswith("<!doctype html") or prefix.startswith("<html") or "<body" in prefix


def _html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|template|svg|canvas)\b.*?</\1>", " ", text)
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|section|article|header|footer|main|aside|nav|li|tr|h[1-6])\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _fetch_once(url: str) -> tuple[int, str, str, bytes, str | None]:
    scheme, host, port, target = _validate_fetch_url(url)
    vetted_ip = _resolve_public_ip(host, port)
    connection: http.client.HTTPConnection
    if scheme == "https":
        connection = _VettedHTTPSConnection(host, port, vetted_ip, REQUEST_TIMEOUT_SECONDS)
    else:
        connection = _VettedHTTPConnection(host, port, vetted_ip, REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Host": host if port in {80, 443} else f"{host}:{port}",
                "User-Agent": USER_AGENT,
                "Accept": ACCEPT_HEADER,
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = _read_limited(response)
        location = response.getheader("Location") if response.status in {301, 302, 303, 307, 308} else None
        content_type = response.getheader("Content-Type") or "application/octet-stream"
        reason = response.reason or ""
        return int(response.status), reason, content_type, body, location
    finally:
        connection.close()


def _fetch_following_redirects(url: str) -> tuple[str, int, str, str, bytes]:
    current_url = str(url or "").strip()
    for _ in range(MAX_REDIRECTS + 1):
        status, reason, content_type, body, location = _fetch_once(current_url)
        if not location:
            return current_url, status, reason, content_type, body
        current_url = urljoin(current_url, location)
    raise WebFetchError("web_fetch exceeded redirect limit.")


def web_fetch(ctx: Any, payload: dict[str, Any]) -> str:
    del ctx
    url = str(payload.get("url", "")).strip()
    final_url, status, reason, content_type, body = _fetch_following_redirects(url)
    decoded = _decode_body(body, content_type)
    is_html = "text/html" in content_type.lower() or _looks_like_html(decoded)
    rendered = _html_to_text(decoded) if is_html else decoded
    rendered = rendered.strip()
    status_line = f"status {status} {reason}".strip()
    header = f"{status_line} · {content_type} · {len(body)} bytes"
    if final_url != url:
        header = f"{header} · final_url {final_url}"
    return f"{header}\n\n{rendered}"


def register_web_fetch_tool(registry) -> None:
    registry.register(
        ToolDefinition(
            name="web_fetch",
            description=(
                "Fetch one HTTP/HTTPS URL and return text content. HTML pages are reduced to readable text; "
                "JSON, plain text, and Markdown bodies are returned verbatim. Does not execute JavaScript or crawl links."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
            handler=web_fetch,
        )
    )
