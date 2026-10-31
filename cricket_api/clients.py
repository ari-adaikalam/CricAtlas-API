"""Who a request comes from, for the rate limit. Written so that nothing an outside caller sends can pick its own limiter key unless the connection came from a proxy the operator named.

Two modes (CRICKET_API_CLIENT_IP):

  peer   the default. The client is the address of the connection. Forwarding headers are ignored, whatever they say. A connection with no address (a unix socket) is refused, because
         every visitor would otherwise share one limit. A unix socket is supported only in proxy mode.
  proxy  the server sits behind a reverse proxy or a CDN. CRICKET_API_TRUSTED_PROXIES lists the peers that are such proxies, as addresses or networks (and `unix` for connections that
         arrive over a unix socket). The header named by CRICKET_API_CLIENT_IP_HEADER (X-Forwarded-For by default) is read only when the connection comes from a listed peer.

In proxy mode the header is a chain. The proxy that sees the real client appends its address, and every further proxy appends the one before. Read from the right, each entry that is a
listed proxy is skipped and the first entry that is not is the client. An entry to the left of that one was written by the client or by something before the first proxy, and is ignored.
Because the walk starts at the right, a client cannot change its own address by sending a header of its own. A header from a listed peer that is not a clean list of addresses (an invalid
entry, a port, a name, an empty entry, too long, too many entries) is refused with 400, not guessed at.

An address is made canonical before it is used: an IPv4-mapped IPv6 address becomes its IPv4 address, a scope id is removed, and an IPv6 address is reduced to its network prefix
(CRICKET_API_IPV6_PREFIX, 64 by default). A key therefore has a fixed small size and one client has one key.

The server must be started with the launcher (python -m cricket_api), which turns uvicorn's own proxy header handling off. That handling would rewrite the connection address before this module
sees it, using a list of trusted addresses of its own.
"""
import ipaddress
import logging
import os
import re
from dataclasses import dataclass

from fastapi import Request

from cricket_api.errors import ApiError
from cricket_api.settings import ConfigError, Settings

log = logging.getLogger("cricket_api.clients")
MAX_HEADER_CHARS = 1024
MAX_HOPS = 16
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PEER_KEY_CHARS = 64
MODES = ("peer", "proxy")


class ClientAddressError(ApiError):
    pass


@dataclass(frozen=True)
class Policy:
    mode: str
    networks: tuple
    unix: bool
    header: str
    ipv6_prefix: int
    warned: set

    @classmethod
    def from_settings(cls, settings: Settings) -> "Policy":
        if os.environ.get("CRICKET_API_BEHIND_CLOUDFLARE"):
            raise ConfigError("CRICKET_API_BEHIND_CLOUDFLARE is no longer read. Use CRICKET_API_CLIENT_IP=proxy with CRICKET_API_TRUSTED_PROXIES and CRICKET_API_CLIENT_IP_HEADER=CF-Connecting-IP, "
                              "and CRICKET_API_SHARED_CACHE=1 for the cache headers")
        if settings.client_ip not in MODES:
            raise ConfigError(f"CRICKET_API_CLIENT_IP must be one of {', '.join(MODES)}")
        if settings.max_clients < 1:
            raise ConfigError("CRICKET_API_MAX_CLIENTS must be at least 1")
        if not 32 <= settings.ipv6_prefix <= 128:
            raise ConfigError("CRICKET_API_IPV6_PREFIX must be between 32 and 128")
        if not HEADER_NAME.match(settings.client_ip_header):
            raise ConfigError("CRICKET_API_CLIENT_IP_HEADER is not a header name")
        networks, unix = [], False
        for token in (t.strip() for t in settings.trusted_proxies.split(",") if t.strip()):
            if token.lower() == "unix":
                unix = True
                continue
            try:
                network = ipaddress.ip_network(token, strict=True)
            except ValueError:
                raise ConfigError(f"CRICKET_API_TRUSTED_PROXIES has an entry that is not an address or a network: {token[:40]!r}") from None
            if network.prefixlen == 0:
                raise ConfigError("CRICKET_API_TRUSTED_PROXIES must not trust every address")
            networks.append(network)
        if settings.client_ip == "proxy" and not networks and not unix:
            raise ConfigError("CRICKET_API_CLIENT_IP=proxy needs CRICKET_API_TRUSTED_PROXIES: the addresses or networks of the proxies, or unix")
        if settings.client_ip == "peer" and (networks or unix):
            raise ConfigError("CRICKET_API_TRUSTED_PROXIES is set but CRICKET_API_CLIENT_IP is peer, so it would be ignored. Set CRICKET_API_CLIENT_IP=proxy")
        return cls(settings.client_ip, tuple(networks), unix, settings.client_ip_header.lower(), settings.ipv6_prefix, set())

    def parse(self, text: str):
        """The address in the text, or None. Mapped IPv4 becomes IPv4. A scope id or any other decoration is not accepted from a header."""
        if "%" in text:                                         # Python reads a scope id (fe80::1%eth0) as part of the address
            return None
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return None
        if address.version == 6 and address.ipv4_mapped:
            return address.ipv4_mapped
        return address

    def key(self, address) -> str:
        """The limiter key of an address: the address for IPv4, the network prefix for IPv6."""
        if address.version == 4:
            return str(address)
        return str(ipaddress.ip_network((address, self.ipv6_prefix), strict=False))

    def trusted(self, address) -> bool:
        return any(address.version == network.version and address in network for network in self.networks)

    def peer(self, request: Request):
        """(address or None, an opaque key for a peer that is not an address). The peer comes from the connection, never from a header."""
        client = request.scope.get("client")
        if not client or not client[0]:
            return None, None
        text = str(client[0]).split("%")[0]
        address = self.parse(text)
        return address, (None if address else "opaque:" + str(client[0])[:PEER_KEY_CHARS])

    def identify(self, request: Request) -> str:
        address, opaque = self.peer(request)
        if address is None and opaque is None:                  # no connection address: a unix socket
            if self.mode == "proxy" and self.unix:
                return self.from_header(request, None)
            self.warn("no-peer", "a request arrived with no client address (a unix socket) and unix is not a trusted proxy, so it was refused. See CRICKET_API_CLIENT_IP")
            raise ClientAddressError(500, "Something went wrong")
        if self.mode == "peer" or not (address is not None and self.trusted(address)):
            return self.key(address) if address is not None else opaque
        return self.from_header(request, address)

    def from_header(self, request: Request, peer) -> str:
        """The client behind a trusted peer. A request with no header at all came from the peer itself."""
        lines = request.headers.getlist(self.header)
        if not lines:
            return self.key(peer) if peer is not None else "local"
        value = ", ".join(lines)
        if len(value) > MAX_HEADER_CHARS:
            raise ClientAddressError(400, "The client address header is too long")
        entries = [part.strip() for part in value.split(",")]
        if len(entries) > MAX_HOPS:
            raise ClientAddressError(400, "The client address header has too many entries")
        addresses = [self.parse(entry) for entry in entries]
        if any(a is None for a in addresses):
            raise ClientAddressError(400, "The client address header is not a list of addresses")
        for address in reversed(addresses):
            if not self.trusted(address):
                return self.key(address)
        return self.key(addresses[0])                           # every hop is a listed proxy, so the leftmost is the origin of the chain

    def warn(self, reason: str, message: str) -> None:
        if reason not in self.warned:                           # once for each reason, so a flood of such requests cannot flood the log
            self.warned.add(reason)
            log.warning(message)
