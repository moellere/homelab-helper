"""network.* probes — cold discovery against a CIDR.

No nmap, no raw sockets, no root: pure asyncio TCP connect-scan. The trade-off
versus an ICMP/SYN scan is that we miss hosts whose firewalls drop everything
we try, but in homelabs that's a vanishingly small set. Without raw-socket
privileges the connect-scan is the only portable + cross-platform option, and
it also gives us "what's listening" as a side benefit.
"""
