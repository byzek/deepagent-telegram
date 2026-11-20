"""Tool that lets the agent audit the network connections it and its sandbox
have made (from the `net_conn` table filled by `app.netconns`)."""
from __future__ import annotations

from langchain.tools import tool


def build_network_tools(pool) -> list:
    @tool
    async def network_connections(hours: int = 24, external_only: bool = False) -> str:
        """List every network peer the agent and sandbox containers talked to in
        the last `hours`, grouped by service (source, direction, peer, port),
        with first/last seen, sample count, peak concurrency and whether it's
        open now. Use it to audit where data goes, spot unexpected outbound
        connections, or debug connectivity. `external_only` hides docker-internal
        and host traffic."""
        if pool is None:
            return "UNAVAILABLE: no database pool"
        sql = (
            "SELECT source, direction, proto, ip, port, name, kind, first_seen, last_seen,"
            " samples, peak, open_now FROM net_conn"
            " WHERE last_seen > now() - make_interval(hours => %s)"
            + (" AND kind = 'external'" if external_only else "")
            + " ORDER BY kind DESC, samples DESC LIMIT 200"
        )
        async with pool.connection() as conn:
            rows = await (await conn.execute(sql, (hours,))).fetchall()
        if not rows:
            return f"NO_CONNECTIONS seen in the last {hours}h."
        lines = ["source dir proto peer:port (name) kind first..last samples peak open"]
        for s, d, pr, ip, port, name, kind, fs, ls, n, peak, op in rows:
            lines.append(
                f"{s} {d} {pr} {ip}:{port} ({name or '?'}) {kind} "
                f"{fs:%m-%d %H:%M}..{ls:%m-%d %H:%M} {n} {peak} {op}"
            )
        return "\n".join(lines)

    return [network_connections]
