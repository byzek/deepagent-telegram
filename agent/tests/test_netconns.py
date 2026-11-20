"""Unit tests for /proc/net connection parsing (stdlib-only, no heavy deps).

Run without pytest:   python3 tests/test_netconns.py   (from agent/)
Run with pytest:      pytest tests/test_netconns.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.netconns import aggregate, classify_ip, parse_proc_net, split_dump  # noqa: E402

HDR = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
TCP = HDR + (
    # listener on :8000 (0x1F40)
    "   0: 00000000:1F40 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 1\n"
    # inbound to our listener from 172.18.0.5:51000
    "   1: 030012AC:1F40 050012AC:C738 01 00000000:00000000 00:00000000 00000000  1000 0 2\n"
    # outbound 172.18.0.3:40000 -> 1.1.1.1:443, plus a TIME_WAIT to the same
    "   2: 030012AC:9C40 01010101:01BB 01 00000000:00000000 00:00000000 00000000  1000 0 3\n"
    "   3: 030012AC:9C41 01010101:01BB 06 00000000:00000000 00:00000000 00000000  1000 0 4\n"
)


def test_parse_ipv4_skips_listeners_and_tags_direction():
    rows = parse_proc_net(TCP, "tcp")
    assert ("tcp", "in", "172.18.0.5", 8000) in [(r["proto"], r["direction"], r["ip"], r["port"]) for r in rows]
    out = [r for r in rows if r["direction"] == "out"]
    assert [(r["ip"], r["port"], r["state"]) for r in out] == [
        ("1.1.1.1", 443, "ESTABLISHED"), ("1.1.1.1", 443, "TIME_WAIT")]


def test_inbound_is_keyed_by_local_service_port():
    rows = parse_proc_net(TCP, "tcp")
    assert [r for r in rows if r["direction"] == "in"][0]["port"] == 8000


def test_parse_ipv6_v4_mapped():
    line = HDR + "   0: 0000000000000000FFFF0000030012AC:9C42 0000000000000000FFFF000008080808:0035 01 0 0 0 0 0 5\n"
    (r,) = parse_proc_net(line, "tcp6")
    assert (r["proto"], r["ip"], r["port"]) == ("tcp", "8.8.8.8", 53)


def test_aggregate_counts_concurrent_and_open():
    agg = aggregate("agent", parse_proc_net(TCP, "tcp"))
    assert agg[("agent", "tcp", "out", "1.1.1.1", 443)] == {"count": 2, "open": 1}


def test_classify_ip():
    assert classify_ip("172.18.0.5") == "internal"
    assert classify_ip("127.0.0.1") == "internal"
    assert classify_ip("1.1.1.1") == "external"


def test_split_dump():
    d = split_dump("==tcp==\nA\n==udp==\nB\n")
    assert d == {"tcp": "A\n", "udp": "B\n"}


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
    print("ok")
