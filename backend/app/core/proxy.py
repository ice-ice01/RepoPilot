"""受信反向代理的 X-Forwarded-For 解析。

反向代理部署时 `request.client.host` 是代理地址（通常是 127.0.0.1），登录 IP 限流会把
所有真实用户合并成同一个键。只有当直连对端本身在受信列表内时才信任 X-Forwarded-For，
取链路中最后一跳（由受信代理追加，客户端无法伪造更靠右的条目），其余情况保持原值。
"""

import ipaddress
from typing import Any, Awaitable, Callable

_LOOPBACK = ipaddress.ip_network("127.0.0.0/8")
_IPV6_LOOPBACK = ipaddress.ip_network("::1/128")
_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_trusted(raw: str) -> list[_IPNetwork] | str | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value == "loopback":
        return "loopback"
    networks: list[_IPNetwork] = []
    for item in value.split(","):
        entry = item.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return networks or None


class TrustedProxyMiddleware:
    """纯 ASGI 中间件：受信代理直连时用 X-Forwarded-For 最后一跳重写 scope["client"]。"""

    def __init__(self, app: Callable[..., Awaitable[Any]], trusted_proxies: str = "") -> None:
        self.app = app
        self._trusted = _parse_trusted(trusted_proxies)

    def _peer_trusted(self, peer: str | None) -> bool:
        if self._trusted is None or not peer:
            return False
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        if self._trusted == "loopback":
            return address in _LOOPBACK or address in _IPV6_LOOPBACK
        return any(address in network for network in self._trusted)

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]], send: Callable[..., Awaitable[Any]]) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            peer = client[0] if isinstance(client, (tuple, list)) and client else None
            if self._peer_trusted(peer):
                forwarded_for: str | None = None
                for name, value in scope.get("headers") or []:
                    if name == b"x-forwarded-for":
                        forwarded_for = value.decode("latin-1")
                        break
                if forwarded_for:
                    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
                    if hops:
                        candidate = hops[-1].strip("[]")
                        try:
                            ipaddress.ip_address(candidate)
                        except ValueError:
                            candidate = ""
                        if candidate:
                            scope["client"] = (candidate, client[1] if isinstance(client, (tuple, list)) and len(client) > 1 else 0)
        await self.app(scope, receive, send)


__all__ = ["TrustedProxyMiddleware"]
