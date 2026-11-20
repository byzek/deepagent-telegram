"""Connection tracking for the agent and sandbox containers.

Every sample reads the kernel's socket tables (`/proc/net/{tcp,tcp6,udp,udp6}`)
locally for the agent and via the exec API for the sandbox, so no Docker socket
or extra privileges are needed. Like ntopng's service map, raw 5-tuples are
folded into "services" (source, proto, direction, peer, port), and each one is
upserted with first/last seen, how many samples it appeared in, and its peak
concurrency. Storage grows with distinct services, not with traffic.

Sampling can miss very short connections, but closed TCP sockets sit in
TIME_WAIT for ~60s, so a 5s interval still catches almost all of them.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from pathlib import Path

log = logging.getLogger(__name__)

FILES = ("tcp", "tcp6", "udp", "udp6")
_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
           "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
           "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}
_OPEN = {"ESTABLISHED", "SYN_SENT", "SYN_RECV"}
# Compose service names resolved to label internal peers.
SERVICES = ("postgres", "searxng", "valkey", "sandbox", "tor", "qdrant", "dashboard",
            "agent", "host.docker.internal")

DDL = """
CREATE TABLE IF NOT EXISTS net_conn (
    source     TEXT NOT NULL,
    proto      TEXT NOT NULL,
    direction  TEXT NOT NULL,
    ip         TEXT NOT NULL,
    port       INTEGER NOT NULL,
    name       TEXT,
    kind       TEXT NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    samples    BIGINT NOT NULL DEFAULT 1,
    peak       INTEGER NOT NULL DEFAULT 1,
    open_now   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, proto, direction, ip, port)
);
CREATE INDEX IF NOT EXISTS net_conn_last_seen_idx ON net_conn (last_seen);
"""


def _addr(hexaddr: str) -> tuple[str, int]:
    h, p = hexaddr.split(":")
    raw = bytes.fromhex(h)
    # The kernel prints each 32-bit word in host (little-endian) byte order.
    raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    ip = ipaddress.ip_address(raw)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(p, 16)


def _listen_ports(text: str) -> set[int]:
    ports = set()
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) > 3 and f[3] == "0A":
            ports.add(_addr(f[1])[1])
    return ports


def parse_proc_net(text: str, kind: str, listen_ports: set[int] | None = None) -> list[dict]:
    """Parse one /proc/net table into peer rows. Inbound rows (to one of our
    listening ports) are keyed by that local service port; outbound by the
    remote port."""
    if listen_ports is None:
        listen_ports = _listen_ports(text)
    proto = kind.rstrip("6")
    rows = []
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4:
            continue
        try:
            (lip, lport), (rip, rport) = _addr(f[1]), _addr(f[2])
        except ValueError:
            continue
        state = _STATES.get(f[3], f[3])
        if state == "LISTEN" or rport == 0 or ipaddress.ip_address(rip).is_unspecified:
            continue
        if proto == "udp":
            state = "ESTABLISHED"  # connected UDP socket
        inbound = lport in listen_ports
        rows.append({"proto": proto, "direction": "in" if inbound else "out",
                     "ip": rip, "port": lport if inbound else rport, "state": state})
    return rows


def split_dump(text: str) -> dict[str, str]:
    """Split `==tcp==\\n...` sections produced by `dump_command()`."""
    out, cur = {}, None
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s.startswith("==") and s.endswith("==") and len(s) > 4:
            cur = s.strip("=")
            out[cur] = ""
        elif cur:
            out[cur] += line
    return out


def parse_dump(sections: dict[str, str]) -> list[dict]:
    listen = set().union(*(_listen_ports(t) for k, t in sections.items() if k.startswith("tcp")))
    return [r for k, t in sections.items() for r in parse_proc_net(t, k, listen)]


def dump_command() -> str:
    return "; ".join(f"echo =={f}==; cat /proc/net/{f} 2>/dev/null" for f in FILES)


def aggregate(source: str, rows: list[dict]) -> dict[tuple, dict]:
    agg: dict[tuple, dict] = {}
    for r in rows:
        a = agg.setdefault((source, r["proto"], r["direction"], r["ip"], r["port"]),
                           {"count": 0, "open": 0})
        a["count"] += 1
        a["open"] += r["state"] in _OPEN
    return agg


def classify_ip(ip: str) -> str:
    a = ipaddress.ip_address(ip)
    return "internal" if (a.is_private or a.is_loopback or a.is_link_local) else "external"


class Namer:
    """IP -> label: compose service names first, then cached reverse DNS."""

    def __init__(self, ttl: float = 300):
        self.ttl, self._services, self._at, self._rdns = ttl, {}, 0.0, {}

    async def _refresh(self):
        loop = asyncio.get_running_loop()
        found = {}
        for svc in SERVICES:
            try:
                for info in await loop.getaddrinfo(svc, None):
                    found[info[4][0]] = "host" if svc == "host.docker.internal" else svc
            except OSError:
                pass
        self._services, self._at = found, time.monotonic()

    async def name(self, ip: str) -> str | None:
        if time.monotonic() - self._at > self.ttl:
            await self._refresh()
        if ip in self._services:
            return self._services[ip]
        if ipaddress.ip_address(ip).is_loopback:
            return "localhost"
        if ip not in self._rdns:
            try:
                host = await asyncio.wait_for(
                    asyncio.to_thread(socket.gethostbyaddr, ip), timeout=2)
                self._rdns[ip] = host[0]
            except (OSError, asyncio.TimeoutError):
                self._rdns[ip] = None
        return self._rdns[ip]


def _local_dump() -> dict[str, str]:
    out = {}
    for f in FILES:
        p = Path("/proc/net") / f
        if p.exists():
            out[f] = p.read_text()
    return out


async def ensure_table(pool) -> None:
    async with pool.connection() as conn:
        await conn.execute(DDL, prepare=False)


async def _record(pool, namer: Namer, agg: dict[tuple, dict]) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE net_conn SET open_now = 0 WHERE open_now > 0")
        for (source, proto, direction, ip, port), a in agg.items():
            name = await namer.name(ip)
            kind = "host" if name == "host" else classify_ip(ip)
            await conn.execute(
                "INSERT INTO net_conn (source, proto, direction, ip, port, name, kind, peak, open_now)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (source, proto, direction, ip, port) DO UPDATE SET"
                " last_seen = now(), samples = net_conn.samples + 1,"
                " peak = GREATEST(net_conn.peak, EXCLUDED.peak), open_now = EXCLUDED.open_now,"
                " name = COALESCE(EXCLUDED.name, net_conn.name)",
                (source, proto, direction, ip, port, name, kind, a["count"], a["open"]),
            )


async def connection_sampler(pool, run_cmd, interval: float, retention_hours: int) -> None:
    """Background loop: fold current agent + sandbox sockets into net_conn."""
    namer = Namer()
    tick, prune_every = 0, max(1, int(300 / interval))
    while True:
        try:
            agg = aggregate("agent", parse_dump(_local_dump()))
            try:
                res = await run_cmd(dump_command())
                agg.update(aggregate("sandbox", parse_dump(split_dump(res.get("stdout") or ""))))
            except Exception as exc:  # noqa: BLE001 - sandbox down shouldn't stop agent tracking
                log.debug("sandbox connection dump failed: %s", exc)
            await _record(pool, namer, agg)
            tick += 1
            if tick % prune_every == 0:
                async with pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM net_conn WHERE last_seen < now() - make_interval(hours => %s)",
                        (retention_hours,),
                    )
        except Exception:  # noqa: BLE001 - never let the sampler kill the app
            log.exception("connection sampler error")
        await asyncio.sleep(interval)
