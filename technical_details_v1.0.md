# OmniNet Multi-WAN Engine - Technical Details (v1.0)

This document provides an in-depth technical analysis of the OmniNet Multi-WAN Engine for software engineers and LLM assistants. It details the architecture, key algorithms, and the underlying code mechanisms used to achieve multi-WAN aggregation, process binding, and sub-500ms failover on Windows.

## 1. System Architecture

OmniNet operates as a local Windows service (with an optional PyQt6 GUI) that circumvents the Windows network stack's default limitations regarding multi-interface utilization. The application comprises a Core Engine and a UI layer. The Core handles all low-level networking, adapter state management, and packet routing via a localized SOCKS5/HTTP proxy.

### Core Modules:
- **`router_engine.py`**: Monitors uplink health, manages active routes, and executes failover policies.
- **`load_balancer.py`**: A multi-protocol proxy (SOCKS5, HTTP CONNECT, Transparent) that intercepts outbound traffic and pins sockets to specific physical adapters.
- **`adapter_manager.py`**: Interfaces with the Windows networking stack via batched PowerShell sessions to manipulate interface metrics without triggering long PnP device resets.
- **`app_binder.py`**: Maps originating connections to local PIDs to enforce app-specific routing rules.

---

## 2. Deep Dive: Scenarios and Implementation

### Scenario A: True Health Probing via Pinned TCP Sockets
**Problem**: Traditional ICMP (`ping -S`) can falsely report a dead link as healthy because Windows routing (Weak Host Send model) may route the ICMP echo out of a different, healthy NIC while keeping the dead link's source IP.
**Solution**: OmniNet uses TCP handshakes to public DNS over port 443, pinning the socket's egress path to the hardware interface using `IP_UNICAST_IF`.

**Code Snippet (`core/router_engine.py` & `netutil.py`)**:
```python
# From netutil.py / router_engine.py
def tcp_probe(host: str, port: int, src_ip: str, if_index: int, timeout: float = 1.5) -> Optional[float]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        # CRITICAL: Forces Windows to route the packet exclusively out of this physical interface
        s.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", if_index))
        s.bind((src_ip, 0)) 
        
        t0 = time.perf_counter()
        s.connect((host, port)) # TCP handshake to 1.1.1.1:443
        return (time.perf_counter() - t0) * 1000.0
    except OSError:
        return None
```
*Why this matters*: It proves end-to-end TCP reachability. A successful handshake guarantees that the specific ISP link is alive and traversing NAT correctly.

### Scenario B: Sub-500ms Interface Metric Failover
**Problem**: Disabling an adapter (`Disable-PnpDevice`) to force a failover takes too long (often >1s) and leaves devices in a broken state (CM_PROB_DISABLED).
**Solution**: OmniNet dynamically alters the Windows `InterfaceMetric` via a persistent PowerShell session. 

**Code Snippet (`core/adapter_manager.py`)**:
```powershell
# Executed as a batched PowerShell script via a persistent child process
foreach ($af in @('IPv4','IPv6')) { 
    Set-NetIPInterface -InterfaceAlias 'Ethernet' -AddressFamily $af -InterfaceMetric 10 -AutomaticMetric Disabled -ErrorAction SilentlyContinue 
}
```
*Why this matters*: Changing the interface metric alters the Windows routing table in ~40ms. New connections immediately drop down to the lowest metric path, achieving seamless failover for stateless or reconnecting protocols.

### Scenario C: Connection Aggregation (Multi-WAN Bonding)
**Problem**: A single TCP flow cannot be split locally. 
**Solution**: For workload aggregation (e.g., a browser opening 40 connections or a torrent client), OmniNet proxies the traffic and distributes new connections across healthy links using a Smooth Weighted Round Robin (SWRR) algorithm, similar to Nginx.

**Code Snippet (`core/router_engine.py`)**:
```python
def _swrr(self, pool: List[Uplink]) -> Uplink:
    """Smooth weighted round robin algorithm"""
    total = 0
    best = None
    best_val = None
    for u in pool:
        w = max(1, u.weight)
        cur = self._rr_state.get(u.name, 0) + w
        self._rr_state[u.name] = cur
        total += w
        if best_val is None or cur > best_val:
            best, best_val = u, cur
            
    self._rr_state[best.name] -= total
    return best
```
*Why this matters*: Unlike modulo-based round-robin which causes traffic bursts, SWRR evenly spaces out socket assignments according to the assigned adapter weights, preventing secondary links from being overwhelmed.

### Scenario D: Fast Proxy Retry (Zero-Drop Illusion)
**Problem**: When a line dies, established sockets bound to that line's public IP will drop.
**Solution**: If an application routes through the OmniNet SOCKS5 proxy, and a connection fails *during the dial phase*, the Load Balancer instantly catches the exception and retries the connection on the next available healthy link.

**Code Snippet (`core/load_balancer.py`)**:
```python
async def _dial(self, host: str, port: int, target_name: str, exe: str):
    # order contains the selected link, followed by fallback links
    for i, uplink in enumerate(order):
        try:
            # Attempts to open TCP stream via specific uplink
            r, w = await self._open_upstream(uplink, host, port)
            return r, w, uplink
        except Exception as e:
            # Failover triggered locally before the client application drops
            log.debug("dial %s:%s via %s failed: %s", host, port, uplink.display, e)
            
    raise OSError("no healthy uplink")
```
*Why this matters*: To the end-user application, this appears as a slightly prolonged TCP handshake rather than a hard connection timeout/reset.

---

## 3. Configuration and Extensibility
The engine reads from `%APPDATA%\OmniNet\config.json`. The `Config` dataclass in `config.py` acts as the source of truth for runtime behavior.

### Important Configuration Knobs for LLM Tuning:
- `rtt_switch_threshold_ms` (float): Hysteresis threshold to prevent route flapping between two connections of similar latency.
- `fail_threshold` / `recover_threshold` (int): Number of consecutive probe failures required to mark a link DOWN, and successes to mark a link UP.
- `mode`: Can be `auto` (failover/lowest RTT), `force` (pinned), or `aggregate` (load balanced).

## 4. Known Limitations for Architectural Planning
- UDP/QUIC traffic is generally not intercepted by the local SOCKS proxy and will follow the system's default route (the lowest metric adapter).
- Transparent redirect mode utilizes WinDivert, which operates at the kernel level and may trigger false positives in strict EDR (Endpoint Detection and Response) environments.
