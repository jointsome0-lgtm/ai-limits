"""Fail-closed network access: a credentialed request leaves only through the VPN interface.

Order matters:
1. the interface must exist, checked before any DNS lookup;
2. the resolved IP must route via the interface (`ip route get` honours policy routing,
   e.g. sing-box/xray TUN tables);
3. an anonymous probe to the same host and IP must get the exact answer an allowed region gets;
4. only then the real request, to exactly that IP.
Every socket is also bound to the interface (SO_BINDTODEVICE, unprivileged since Linux 5.7), so if
the VPN drops between the checks and the request, the kernel refuses to send it elsewhere.
Environment proxies are ignored and redirects are not followed.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time

PROBE_ATTEMPTS = 2
TIMEOUT = 5  # per connection; worst case (route + 2 probes + request) stays under the extension's 30 s
SUBPROCESS_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}  # no inherited secrets

# Result of the last check in this process: None = no check ran, True/False otherwise.
last_check: bool | None = None


class VpnCheckFailed(Exception):
    pass


def require_supported_platform() -> None:
    """Do not substitute an unguarded transport on non-Linux platforms."""
    if sys.platform != "linux" or not hasattr(socket, "SO_BINDTODEVICE"):
        raise VpnCheckFailed("network mode requires Linux with SO_BINDTODEVICE; use offline mode on this platform")


def check_route(host: str, interface: str) -> str:
    """Return the IPv4 address to use for `host`, or raise VpnCheckFailed."""
    require_supported_platform()
    if not interface or not os.path.exists(f"/sys/class/net/{interface}"):
        raise VpnCheckFailed(f"interface {interface or '?'} is down")
    try:
        ip = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (OSError, IndexError) as e:
        raise VpnCheckFailed(f"cannot resolve {host}: {e}") from e
    try:
        out = subprocess.run(["ip", "route", "get", ip], capture_output=True, text=True, timeout=5,
                             encoding="ascii", errors="replace", check=True, env=SUBPROCESS_ENV).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise VpnCheckFailed(f"cannot check route: {e}") from e
    tokens = out.split()
    dev = tokens[tokens.index("dev") + 1] if "dev" in tokens[:-1] else None
    if dev != interface:
        raise VpnCheckFailed(f"{host} would go via {dev or '?'}, not {interface}")
    return ip


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a fixed IP through a fixed interface, verifying TLS for the real host name."""

    def __init__(self, host: str, ip: str, interface: str, **kw):
        super().__init__(host, **kw)
        self._ip = ip
        self._interface = interface

    def connect(self):
        require_supported_platform()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self._interface.encode())
            sock.settimeout(self.timeout)
            sock.connect((self._ip, self.port))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except OSError:
            sock.close()
            raise


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.keylog_filename = None  # ignore an inherited SSLKEYLOGFILE: it would write session keys to disk
    return ctx


def _get(host: str, ip: str, interface: str, path: str, headers: dict, timeout: float) -> tuple[int, bytes]:
    conn = _PinnedHTTPSConnection(host, ip, interface, timeout=timeout, context=_tls_context())
    try:
        conn.request("GET", path, headers={"Accept": "application/json", **headers})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def probe(host: str, ip: str, interface: str, path: str, expect_status: int, expect_error: str,
          timeout: float = TIMEOUT) -> None:
    """Anonymous request (no token, no cookies) to the same host and IP the real request will use.

    Being the same domain, it takes the same VPN routing rule. Only the exact answer an allowed
    region gets for a missing token counts; a region block (403) or anything unexpected raises.
    """
    for attempt in range(PROBE_ATTEMPTS):  # anonymous, so retrying a flaky tunnel is harmless
        try:
            status, body = _get(host, ip, interface, path, {}, timeout)
            break
        except (OSError, http.client.HTTPException) as e:
            if attempt == PROBE_ATTEMPTS - 1:
                raise VpnCheckFailed(f"probe to {host} failed: {e}") from e
            time.sleep(1)
    try:
        error_type = json.loads(body)["error"]["type"]
    except (ValueError, KeyError, TypeError):
        error_type = None
    if status != expect_status or error_type != expect_error:
        raise VpnCheckFailed(f"probe to {host} got HTTP {status} {error_type or ''}".strip())


def get_json_authenticated(host: str, path: str, headers: dict, interface: str,
                           probe_expect: tuple[str, int, str], timeout: float = TIMEOUT) -> dict:
    """GET https://host/path with credentials in `headers`, only after all checks pass.

    probe_expect = (probe path, status, error type) and is mandatory.
    Raises VpnCheckFailed (nothing with credentials was sent) or OSError/ValueError.
    """
    global last_check
    if not probe_expect:
        raise ValueError("an anonymous probe is required before sending credentials")
    try:
        ip = check_route(host, interface)
        probe(host, ip, interface, *probe_expect, timeout=timeout)
    except VpnCheckFailed:
        last_check = False
        raise
    except Exception as e:  # any surprise during the checks counts as a failed check
        last_check = False
        raise VpnCheckFailed(f"check failed: {type(e).__name__}") from e
    last_check = True
    status, body = _get(host, ip, interface, path, headers, timeout)
    if status != 200:
        raise OSError(f"HTTP {status} from {host}")
    return json.loads(body)
