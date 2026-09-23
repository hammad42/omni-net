# OmniNet Multi-WAN Engine

A Windows 10/11 desktop app that manages several Internet lines at once: per-app
routing, weighted load balancing across links, fast failover, and a segmented
downloader that really does hit the combined speed of both lines.

Built for a three-NIC setup:

| Adapter | Typical name | Role |
|---|---|---|
| Realtek Gaming GbE (onboard) | `Ethernet` | primary 1 Gbps line |
| Realtek USB GbE (dongle) | `Ethernet 2` | secondary line |
| Intel Wi-Fi 6 AX201 | `Wi-Fi` | backup |

---

## 1. What is actually possible on Windows — read this first

Three of the requested features are commonly oversold. Here is the honest
picture, because it determines how the code is built.

### "Bind an app to a NIC"
Windows has no native per-process interface binding. Three mechanisms exist:

1. **Proxy + PID attribution** (default here). The app talks to a local
   SOCKS5/HTTP proxy. The proxy looks up the client's source port in the TCP
   table, resolves it to a PID and an `.exe` name, applies your rule, and dials
   out on the chosen NIC. No driver, no injection, zero risk. Covers everything
   that honours a proxy: browsers, Discord, Steam downloads, updaters, curl.
2. **Transparent capture (WinDivert)**. A kernel filter redirects the app's
   outbound TCP into the same proxy, so proxy-unaware apps are covered too.
   TCP only — UDP and QUIC are not captured. Needs a signed driver that some
   EDR/AV products dislike.
3. **ForceBindIP**. Injects `BindIP.dll` and hooks `WSASocket`/`bind` so the
   process is born bound to one source IP. Works on old apps.
   **Do not point this at anti-cheat games.** Vanguard (Valorant), EAC,
   BattlEye and Ricochet treat a foreign DLL as an attack; best case the game
   refuses to start, worst case you get a hardware ban. `valorant.exe` was on
   the original wish list — give it a dedicated link or a proxy rule instead.

### "Combine 1 Gbps + 100 Mbps into 1.1 Gbps"
**Per connection: impossible locally.** A TCP flow has one source IP. The far
end answers that IP, and those packets can only come back over the link that
owns it. Splitting a single flow needs cooperation at the other end — MPTCP with
a multipath-aware server, or a bonding VPN where a remote box reassembles your
two streams (Speedify, OpenMPTCProuter). Anything claiming local-only single-flow
bonding is doing per-flow balancing and calling it bonding.

**Per workload: entirely possible**, and that is what people actually feel:

* Many connections (browser with 40 sockets, torrent with 200 peers, several
  downloads) → the dispatcher spreads them and the totals add up.
* One large file → the **Turbo Download** tab splits it into HTTP Range
  requests and pulls chunks over both links at once. Single file, combined
  speed. This is the real answer to the aggregation requirement.

### "Zero-drop failover in 500 ms"
New connections: yes. OmniNet rewrites interface metrics through a warm
PowerShell session in roughly 40–120 ms, so within one probe cycle new sockets
use the healthy link.

Existing connections: no, and nothing running only on your PC can fix it. Your
active TLS session is bound to the dead line's public IP; when that line dies the
peer has nowhere to send. What OmniNet does instead: connections that go through
the proxy and fail at connect time are immediately retried on the next healthy
link, so the user-visible effect is a retry rather than an error page.

---

## 2. Architecture

```
                       ┌──────────────────────────────┐
                       │            ui/               │
                       │  main_window · tray · widgets│
                       └───────────────┬──────────────┘
                              QTimer 1 Hz pull + signal bridge
                       ┌───────────────┴──────────────┐
                       │        core.Services         │
                       └──┬────────┬────────┬─────────┘
          ┌───────────────┘        │        └───────────────┐
┌─────────▼─────────┐   ┌──────────▼────────┐   ┌───────────▼──────────┐
│  router_engine    │   │  load_balancer    │   │   app_binder         │
│ health probes     │   │ SOCKS5 + HTTP     │   │ PID→exe attribution  │
│ metric control    │◄──┤ uplink selection  │──►│ rule table           │
│ failover policy   │   │ connect failover  │   │ WinDivert redirect   │
└─────────┬─────────┘   └──────────┬────────┘   │ ForceBindIP launcher │
          │                        │            └──────────────────────┘
┌─────────▼─────────┐   ┌──────────▼────────┐
│ adapter_manager   │   │   downloader      │
│ Get-NetAdapter    │   │ range-split fetch │
│ Set-NetIPInterface│   └───────────────────┘
│ PnP repair        │
└─────────┬─────────┘
          │
┌─────────▼──────────────────────────────────────────────────────────┐
│ netutil:  persistent PowerShell session · IP_UNICAST_IF binding    │
└────────────────────────────────────────────────────────────────────┘
```

### The one API that makes all of this work

```python
sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", if_index))
sock.bind((src_ip, 0))
```

Binding the source address alone is **not** enough. Windows picks the egress
interface from the routing table (destination first), and with the default
*weak host send* model a packet carrying NIC A's address happily leaves through
NIC B. Your ISP then drops it (uRPF / BCP38) and the connection dies with no
useful error. `IP_UNICAST_IF` overrides the route lookup and pins the wire.

Gotcha: for IPv4 the index goes in **network** byte order (`"!I"`); for IPv6
(`IPPROTO_IPV6`, option 31) it goes in **host** byte order. Yes, really.

### Why metrics instead of disabling adapters

Your current script uses `Disable-NetAdapter` / `Disable-PnpDevice`. That works,
but it is exactly the path that leaves USB NICs stuck in `CM_PROB_DISABLED` (22)
after a few hundred cycles, and it hard-kills every socket on the link.

Setting `InterfaceMetric` reaches the same routing outcome — lowest metric wins
the default route — in ~40 ms, is fully reversible, and never touches the device
stack. PnP disable is still available but is off by default
(`allow_adapter_disable`), and there is a **Repair adapter** button that
re-enables a stuck device node.

### Why a persistent PowerShell child process

Spawning `powershell.exe` costs 300–900 ms. The failover budget is 500 ms, so
per-command spawning cannot meet it. `netutil.PowerShellSession` keeps one
child alive, ships each script as a base64 UTF-16LE script block (so multi-line
scripts survive the stdin pipe) and terminates results with a GUID sentinel.
All metric changes for all adapters go out in **one** round trip.

### Selection policy

`RouterEngine.select_for_new_connection()` decides per connection:

1. An explicit app rule wins, if that link is healthy.
2. Aggregate mode → smooth weighted round robin (the nginx algorithm: spreads
   3:1 as `A A B A A A B …` rather than bursting).
3. Otherwise the preferred link: wired before wireless, then weight, then RTT.

Health is a TCP handshake to `1.1.1.1:443` / `8.8.8.8:443` pinned to each NIC —
not ICMP, because `ping -S` sets the source address but not the egress
interface and will cheerfully report a dead link as alive.

---

## 3. Running it

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py                # UAC prompt, then the GUI
python main.py --cli          # headless engine, no Qt
python main.py --force "Ethernet 2"
```

Build the standalone exe:

```powershell
build\build.bat
# -> dist\OmniNet.exe   single file, no console, admin manifest embedded
```

### Pointing apps at the proxy

| App | Where |
|---|---|
| Chrome / Edge | launch with `--proxy-server="socks5://127.0.0.1:1080"` |
| Firefox | Settings ▸ Network ▸ Manual, SOCKS5 `127.0.0.1:1080`, tick remote DNS |
| Discord | uses the Windows system proxy |
| Steam | `-noverifyfiles` not needed; set the system proxy or use transparent mode |
| Anything else | Settings ▸ Transparent capture |

Chrome also speaks QUIC over UDP 443, which no local proxy can intercept. Add
`--disable-quic` or the rule will only bite on TCP fallback.

### Turbo Download

Paste a URL, pick connections per link, hit start. The file is split into
1 MiB+ chunks on a shared queue; each worker is pinned to one uplink and pulls
the next chunk when it finishes one, so a congested link simply takes fewer
chunks. The per-link split is shown live — that panel is the proof that both
lines are carrying the transfer.

---

## 4. Configuration

`%APPDATA%\OmniNet\config.json`, hot-saved from the UI. Log:
`%APPDATA%\OmniNet\omninet.log`.

Useful knobs: `probe_interval_s`, `fail_threshold`, per-uplink `weight` and
`base_metric`, `upstream_port_low/high` (the source-port range the WinDivert
filter excludes so the proxy never captures its own traffic).

---

## 5. Known limitations

* UDP is never redirected — QUIC and most game traffic follow the system route.
  Force-bind those apps by giving them a dedicated link instead.
* DNS resolution in the proxy uses the system resolver, so lookups follow the
  default route even when the connection does not. Harmless for correctness,
  visible if your two ISPs return different CDN answers.
* IPv4 only in the transparent redirector.
* Transparent mode needs the WinDivert driver; some EDR products block it.
* Captive portals on the secondary line will mark it healthy (the TCP handshake
  to :443 succeeds against the portal). Use a probe target that requires a real
  TLS session if this matters to you.

---

## 6. A bug in the existing `Switch-Ethernet.ps1`

Lines like:

```powershell
Write-Host "Strong Signal (Ethernet)" -ForegroundColor (if ($isOpt1) { "White" } else { "Gray" })
```

`if` is a statement, not an expression, so a bare `(...)` group around it is a
parse error in Windows PowerShell 5.1 (`Unexpected token 'if'`). Use a
subexpression:

```powershell
Write-Host "Strong Signal (Ethernet)" -ForegroundColor $(if ($isOpt1) { "White" } else { "Gray" })
```
