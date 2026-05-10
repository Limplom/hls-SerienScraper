"""
Surfshark VPN integration via wireproxy (userspace WireGuard → SOCKS5).

Pattern learned from gluetun's internal/provider/surfshark/:
  - Server discovery uses Surfshark's public, auth-free API at
    https://api.surfshark.com/v4/server/clusters/<type>
  - Each cluster entry contains connectionName + pubKey, which together
    with the user's static PrivateKey + Address form a complete WG config.
  - WireGuard always speaks UDP/51820 on Surfshark.

Flow:
  1. fetch_servers() once per hour → cached SurfsharkServer list
  2. pick_server(country) → low-load, non-burned server
  3. _generate_config() builds wireproxy-flavored .conf in /tmp
  4. wireproxy is spawned as subprocess; exposes SOCKS5 on 127.0.0.1:<port>
  5. Playwright connects via proxy={"server": "socks5://127.0.0.1:<port>"}
  6. On Turnstile detection: rotate() marks current server burned (2h cooldown)
     and switches to a fresh one — wireproxy is restarted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SURFSHARK_API = "https://api.surfshark.com/v4/server/clusters/generic"
WG_PORT = 51820
SURFSHARK_DNS = ("162.252.172.57", "149.154.159.92")
SERVER_CACHE_TTL_SECONDS = 3600
BURN_COOLDOWN_SECONDS = 2 * 3600
WIREPROXY_STARTUP_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class SurfsharkServer:
    connection_name: str
    pub_key: str
    country: str
    country_code: str
    location: str
    load: int

    def matches(self, country: Optional[str]) -> bool:
        if not country:
            return True
        c = country.strip()
        return c == self.country_code or c.lower() == self.country.lower()


@dataclass
class SurfsharkVPN:
    private_key: str
    address: str
    socks_port: int = 1080
    config_path: Path = field(default_factory=lambda: Path("/tmp/wg-surfshark.conf"))
    wireproxy_binary: str = "wireproxy"

    _servers: list[SurfsharkServer] = field(default_factory=list, init=False)
    _servers_fetched_at: float = field(default=0.0, init=False)
    _proc: Optional[subprocess.Popen] = field(default=None, init=False)
    _current: Optional[SurfsharkServer] = field(default=None, init=False)
    _burned: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self):
        if not self.private_key:
            raise ValueError("SurfsharkVPN requires private_key")
        if not self.address:
            raise ValueError("SurfsharkVPN requires address (e.g. 10.14.0.2/16)")
        if shutil.which(self.wireproxy_binary) is None:
            raise RuntimeError(
                f"{self.wireproxy_binary} not found in PATH — install from "
                "https://github.com/whyvl/wireproxy/releases"
            )

    @property
    def proxy_url(self) -> str:
        return f"socks5://127.0.0.1:{self.socks_port}"

    @property
    def current_server(self) -> Optional[SurfsharkServer]:
        return self._current

    async def fetch_servers(self, force: bool = False) -> list[SurfsharkServer]:
        if not force and self._servers and time.time() - self._servers_fetched_at < SERVER_CACHE_TTL_SECONDS:
            return self._servers
        data = await asyncio.to_thread(self._http_get_json, SURFSHARK_API)
        self._servers = [
            SurfsharkServer(
                connection_name=item["connectionName"],
                pub_key=item["pubKey"],
                country=item["country"],
                country_code=item["countryCode"],
                location=item.get("location", ""),
                load=int(item.get("load", 50)),
            )
            for item in data
            if item.get("pubKey") and item.get("connectionName")
        ]
        self._servers_fetched_at = time.time()
        logger.info(f"Surfshark: fetched {len(self._servers)} WG servers")
        return self._servers

    def pick_server(self, country: Optional[str] = None) -> SurfsharkServer:
        now = time.time()
        candidates = [
            s for s in self._servers
            if s.matches(country) and self._burned.get(s.connection_name, 0) < now
        ]
        if not candidates:
            raise RuntimeError(f"No fresh Surfshark server available for country={country!r}")
        candidates.sort(key=lambda s: s.load)
        top_quartile = candidates[: max(3, len(candidates) // 4)]
        return random.choice(top_quartile)

    async def connect(self, country: Optional[str] = None) -> SurfsharkServer:
        await self.fetch_servers()
        server = self.pick_server(country)
        self._write_config(server)
        await self._stop_wireproxy()
        self._proc = subprocess.Popen(
            [self.wireproxy_binary, "-c", str(self.config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if not await self._wait_socks_ready(WIREPROXY_STARTUP_TIMEOUT_SECONDS):
            stderr = self._read_stderr_nowait()
            await self._stop_wireproxy()
            raise RuntimeError(
                f"wireproxy did not expose SOCKS5 on :{self.socks_port} within "
                f"{WIREPROXY_STARTUP_TIMEOUT_SECONDS}s. stderr: {stderr[:300]!r}"
            )
        self._current = server
        logger.info(f"Surfshark: connected via {server.connection_name} ({server.country}, load={server.load})")
        return server

    async def rotate(self, country: Optional[str] = None) -> SurfsharkServer:
        if self._current is not None:
            self._burned[self._current.connection_name] = time.time() + BURN_COOLDOWN_SECONDS
            logger.info(f"Surfshark: marking {self._current.connection_name} burned for "
                        f"{BURN_COOLDOWN_SECONDS // 60}min")
        return await self.connect(country)

    async def disconnect(self):
        await self._stop_wireproxy()
        self._current = None

    async def public_ip(self) -> str:
        """Resolve current public IP through the proxy. Useful for sanity checks."""
        return await asyncio.to_thread(self._http_get_text_via_proxy, "https://ifconfig.io")

    def _write_config(self, server: SurfsharkServer):
        # wireproxy config: WireGuard [Interface] + [Peer], plus its own [Socks5] section
        config = (
            "[Interface]\n"
            f"PrivateKey = {self.private_key}\n"
            f"Address = {self.address}\n"
            f"DNS = {', '.join(SURFSHARK_DNS)}\n"
            "\n"
            "[Peer]\n"
            f"PublicKey = {server.pub_key}\n"
            f"Endpoint = {server.connection_name}:{WG_PORT}\n"
            "AllowedIPs = 0.0.0.0/0, ::/0\n"
            "PersistentKeepalive = 25\n"
            "\n"
            "[Socks5]\n"
            f"BindAddress = 127.0.0.1:{self.socks_port}\n"
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(config)
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass

    async def _wait_socks_ready(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.socks_port)
                writer.close()
                await writer.wait_closed()
                return True
            except OSError:
                await asyncio.sleep(0.15)
        return False

    async def _stop_wireproxy(self):
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.to_thread(self._proc.wait, 3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                await asyncio.to_thread(self._proc.wait, 2)
        self._proc = None

    def _read_stderr_nowait(self) -> str:
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            os.set_blocking(self._proc.stderr.fileno(), False)
            return self._proc.stderr.read(2000) or b""  # type: ignore[return-value]
        except (OSError, ValueError):
            return ""

    @staticmethod
    def _http_get_json(url: str) -> list:
        req = urllib.request.Request(url, headers={"User-Agent": "hls-scraper/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _http_get_text_via_proxy(self, url: str) -> str:
        # urllib doesn't speak SOCKS5 natively. Use HTTP CONNECT through wireproxy?
        # wireproxy's [Socks5] is SOCKS5-only, no HTTP. So we use a tiny socks client via socket.
        # For simplicity here: skip — caller can use Playwright with proxy, or curl --socks5.
        # Returning a hint instead of failing silently keeps the API honest.
        raise NotImplementedError(
            "Use Playwright with proxy=self.proxy_url, or shell out to "
            f"curl --socks5 127.0.0.1:{self.socks_port} {url}"
        )


def from_config() -> Optional[SurfsharkVPN]:
    """Build SurfsharkVPN from app.config.Config, or return None if disabled."""
    from app.config import Config
    if not Config.vpn_enabled():
        return None
    return SurfsharkVPN(
        private_key=Config.SURFSHARK_PRIVATE_KEY,
        address=Config.SURFSHARK_ADDRESS,
        socks_port=Config.SURFSHARK_SOCKS_PORT,
    )


# Manual smoke test:
#   python -m app.surfshark_vpn DE
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    country = sys.argv[1] if len(sys.argv) > 1 else None

    async def _smoke():
        vpn = from_config()
        if vpn is None:
            print("VPN disabled (SURFSHARK_PRIVATE_KEY empty in env). Set it in .env.")
            return
        try:
            server = await vpn.connect(country)
            print(f"Connected: {server.connection_name} ({server.country}, load={server.load})")
            print(f"Proxy URL for Playwright: {vpn.proxy_url}")
            print(f"Verify: curl --socks5 127.0.0.1:{vpn.socks_port} https://ifconfig.io")
            input("Press Enter to disconnect …")
        finally:
            await vpn.disconnect()

    asyncio.run(_smoke())
