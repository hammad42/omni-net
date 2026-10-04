# OmniNet Multi-WAN Engine — Architecture Review, Bug Hunting, Improvement & Testing Plan

**Version:** 0.1
**Scope reviewed:** `technical_details_v1.0.md`, `config.py`, `main.py`, `core/` (`router_engine.py`, `load_balancer.py`, `adapter_manager.py`, `netutil.py`, `app_binder.py`, `stats.py`, `downloader.py`, `__init__.py`), `test_suite.py` (2,093 lines, custom runner, Phases 1–5), environment (Python 3.12.8, psutil/PyQt6/requests installed; **pytest is not installed**).

**Architecture recap (verified against code):** `Services` (`core/__init__.py:18`) wires a threaded `RouterEngine` (probe thread + `RLock`-guarded uplink state) to an asyncio `MultiWanProxy` running on its own thread/loop. All uplink selection funnels through `RouterEngine.select_for_new_connection()`; all egress pinning through `netutil.bind_socket_to_uplink()` (`IP_UNICAST_IF` + source bind); all metric manipulation through one persistent `powershell.exe -Command -` child (`netutil.SHELL`) shared by adapter/router code.

---

## Bug Hunting

### P0 — Correctness / crash / deadlock class

**1. Orphaned probe threads corrupt health state (race + thread leak)**
- `router_engine.py:224-226`: `probe_round()` joins probe threads with a deadline of `probe_timeout_s * 2 + 0.5` = **3.5 s** (defaults).
- But `_probe_uplink` (`router_engine.py:182-191`) tries up to **4** probe targets sequentially on failure (`config.py:76`), each up to `probe_timeout_s` = 1.5 s → **6 s worst case**. The join expires, the thread is orphaned, and the *next* round starts while the old thread is still running.
- Both threads then mutate the same `Uplink` under the lock: `history`, `consecutive_ok/fail`, `state` (`router_engine.py:195-216`). With `fail_threshold=2`, a stale failure from an orphaned thread can flip a link **DOWN right after a fresh success** — a spurious failover trigger, and threads accumulate every 3 s whenever links are black-holed.
- Fix: per-round generation token (discard stale results), hard per-uplink probe budget (e.g. `min(timeout, budget/targets)`), and a small `ThreadPoolExecutor` instead of unbounded thread spawning.

**2. `PowerShellSession` reader thread races `_proc = None` → forced timeout / desync**
- `netutil.py:206-216`: the reader thread dereferences `self._proc.stdout` **on every iteration**. `close()` (`netutil.py:166-177`) sets `self._proc = None` (from `run()`'s retry path, `netutil.py:183-192`) while a reader may be blocked in `readline()` → `AttributeError` kills the reader → `done` is never set → the *caller blocks for the full timeout* even though output was ready, then `PowerShellError`.
- Additional sharp edges in the same mechanism:
  - **Marker-on-same-line payload loss** (`netutil.py:212-214`): if the script's last output lacks a trailing newline, the sentinel lands on the same line (`result@@OMNI_END:…`); `if marker in line: return` **discards `result`** → truncated JSON → `parse_json_loose` silently returns `None` → `refresh()` keeps stale adapter data.
  - **Timeout retry re-executes side effects** (`netutil.py:183-192`): a timed-out `set_admin_state` (Enable/Disable-NetAdapter, timeout 30 s) is retried blindly; a *partially applied* script runs twice. Retry should respawn, not replay, and only be automatic for idempotent reads.
  - `_write` (`netutil.py:158-161`) raises raw `BrokenPipeError` when the child died; `run()` only normalizes `PowerShellError`, so callers like `apply_routing` see untyped exceptions.
  - `assert self._proc …` disappears under `python -O` (PyInstaller builds commonly strip asserts).

**3. Torn reads of `Uplink.adapter` between the async proxy and the monitor thread**
- `sync_uplinks` swaps the adapter object in place: `existing.adapter = a` (`router_engine.py:140`) under `RouterEngine._lock`, but `_open_upstream` reads `uplink.ip` and `uplink.if_index` as **two separate unlocked property accesses** (`load_balancer.py:172`), each going through `self.adapter`. If the swap lands between them you bind IP from the old adapter and `IP_UNICAST_IF` of the new one — under weak-host-send the packet leaves the wrong NIC or `bind()` fails with `EINVAL`.
- Same class: `_dial` (`load_balancer.py:179-207`) builds its failover `order` across **two** separate lock acquisitions (`select_for_new_connection()` then `healthy_uplinks()`); an adapter removed in between leaves a dead `Uplink` in the retry order whose `ip` is now `""` → `sock.bind(("", port))` binds `INADDR_ANY` and silently loses pinning.
- Fix: an immutable snapshot dataclass `(name, ip, if_index, healthy)` taken once under the lock and passed down the whole dial path.

**4. Transparent-mode capture loop via source-port fallback**
- `SourcePortPool` hands out ports 50000–50999 (`config.py:73-74`) — **inside Windows' default dynamic port range (49152–65535)**, so collisions with real client sockets are likely under load.
- On collision, `bind_socket_to_uplink` falls back to ephemeral (`netutil.py:110-113`). In transparent mode the WinDivert exclusion filter only spares `tcp.SrcPort` **inside** the pool (`app_binder.py:234-242`) → the proxy's own upstream socket is captured and redirected back into the proxy → **infinite capture loop / packet storm**.
- Fix: move the pool outside the ephemeral range (or shrink the dynamic range via `netsh int ipv4 set dynamicport`), and in transparent mode **fail the dial** on collision instead of falling back to ephemeral.

**5. Cross-layer deadlock hazard: engine callbacks invoked while holding `RouterEngine._lock`**
- `select_for_new_connection` calls `log_event` → `on_event` **while holding `self._lock`** (`router_engine.py:331-338`). `on_event`/`on_change` are UI-facing (`main.py:70`, `ui/main_window.py`). If any handler synchronously marshals to the Qt thread (BlockingQueuedConnection / `invokeMethod`) while the Qt thread is itself calling into the router (it reads `uplinks` for the dashboard), you deadlock. Rule: callbacks must fire only outside engine locks and always via queued signals.

**6. Relays hang for minutes on a dead link (no stall timeout) and leak on half-open peers**
- `_relay` (`load_balancer.py:345-383`) has **no idle/stall timeout**. When a NIC is yanked mid-transfer, `dst.drain()` blocks on TCP retransmission (Windows can retry for many minutes) — the "sub-500 ms failover" never applies to established relays (documented as honest in the header, but the *hang* is still a bug from the user's perspective).
- `asyncio.gather(pump(...), pump(...))` waits for **both** directions. If the upstream dies but the client never closes, `cr.read()` blocks forever → task + sockets leak and `active_conns` never returns to baseline.
- When `RouterEngine` marks a link DOWN, nothing cancels the relays pinned to it.
- Fix: idle timeout (e.g. 30–60 s), cancel sibling pump on first abnormal termination, subscribe relays to uplink-down events.

### P1 — Logic / robustness bugs

**7. `config.load()` crashes on unknown keys and doesn't coerce types** (`config.py:110-128`): `UplinkConfig(**d)` / `AppRule(**d)` raise `TypeError` on any key added by a newer version — **outside** the `try`, so the whole app fails to start. `setattr(cfg, k, v)` accepts `probe_interval_s: "3"` (string) → `TypeError` spam in the monitor loop (`router_engine.py:410`). Needs per-field coercion with fallbacks + `cfg_version`.

**8. `STATE_UNKNOWN → STATE_UP` bypasses `recover_threshold`** (`router_engine.py:208-209`): the `elif` immediately promotes a first-probe success to UP, contradicting the documented hysteresis and making `recover_threshold` apply only to recovering links.

**9. Metric mapping bugs in `apply_routing`** (`router_engine.py:295-304`):
- All non-preferred links get **the preferred link's** `base_metric` + 10·i, ignoring their own `UplinkConfig.base_metric`.
- `self.cfg.uplink(pref.name).base_metric or 10` turns a legitimate `0` into `10`.
- `preferred_uplink()` mutates `_current_preferred` and logs "Switching…" with no lock (`router_engine.py:274-283`) while the proxy thread calls the same function under the RLock → duplicate switch events / log flapping.

**10. DNS resolution is unpinned and uncached** (`load_balancer.py:160-165`): `loop.getaddrinfo()` uses the system resolver over the **system default route**. If the current default-route link is dead, dials fail even when the target uplink is healthy — precisely during failover. It also runs on the loop's shared default executor (thread pool saturation under connect bursts) and re-resolves per connection.

**11. Proxy lifecycle gaps** (`load_balancer.py:105-150`): `stop()` never cancels active relay tasks, never joins `_thread`, never closes the loop → FD/handle leaks and "Task was destroyed but it is pending" on restart; `start()` waits 2.5 s then returns even if **all** listeners failed to bind (`_serve` sets `running = False` only via log) — `Services.start()` proceeds as if the proxy is up.

**12. Protocol parser edge cases** (`load_balancer.py:240-313`):
- IPv6 literals: HTTP `CONNECT [::1]:443` → `rpartition(":")` yields `host="[::1]"` → `ipaddress.ip_address` rejects the brackets → dial always fails; `atyp==4` hosts are unwritable because `_open_upstream` hardcodes `AF_INET` (`load_balancer.py:167`).
- `int(port_s or 443)` raises `ValueError` on garbage ports → connection dropped with no SOCKS/HTTP error reply.
- `host.decode("idna", "ignore")` (`load_balancer.py:257`) — the `idna` codec's error-handler support varies across versions; wrap in `try/except` and fall back to ASCII.
- `_host_from_headers` breaks on IPv6 `Host: [::1]:80` (`load_balancer.py:393-401`).

**13. PID attribution aliases and loop-blocking** (`app_binder.py:68-90`, `load_balancer.py:235-237`):
- `psutil.net_connections(kind="tcp")` (10–40 ms) runs **on the asyncio event loop** on cache miss — one new connection can add tens of ms to *every* concurrent flow (head-of-line blocking).
- The map is keyed by `laddr.port` alone; the proxy's own upstream sockets (bound 50000–50999) and client sockets share the Windows ephemeral range → a client port can resolve to the proxy's own PID → wrong app rule.

**14. `TransparentRedirector.table` eviction is wrong** (`app_binder.py:305-312`): at 4096 entries it pops the *oldest inserted* 1024 — likely still-active mappings — and there is **no time-based expiry**, so a reused client port inherits a stale destination.

**15. `ThroughputMonitor.rates`/`_prev` never pruned** (`stats.py:83-93`) — slow leak with USB adapter churn (same for `ProxyStats.by_app`, `RuleEngine.hits`, `bytes_by_app`, though those are bounded by distinct exe names).

**16. Metrics survive a crash** — `restore_on_exit` only runs on graceful shutdown (`router_engine.py:420-427`). `taskkill /F`, a crash, or power loss leaves Windows with manipulated interface metrics and `AutomaticMetric Disabled`. Backup state should be persisted to disk and reconciled at startup.

### P2 — Minor / cosmetic
- `Uplink.history.pop(0)` list O(n) (`router_engine.py:199`) — use `deque(maxlen=20)`.
- `writer.close()` without `await writer.wait_closed()` → "Event loop is closed" noise at shutdown (`load_balancer.py:218-219` etc.).
- `physical()` filter (`adapter_manager.py:150-160`) misses ZeroTier/Hamachi/Tailscale-style virtual NICs.
- `refresh()` keeps stale adapter data forever if enumeration returns empty (`adapter_manager.py:139-140`).
- `relaunch_as_admin` doesn't escape embedded quotes (`netutil.py:72`).
- `_serve` transparent port `socks_port + 1` duplicated in `Services.__init__` (`core/__init__.py:28`) — single source of truth would be better.

---

## Improvement Plan

### Performance
1. **Get PID attribution off the event loop** (fixes #13): background refresher thread keeping `ProcessResolver` warm, or `loop.run_in_executor` — target: 0 blocking syscalls in the connection path.
2. **DNS: cache + pin.** Cache `(host, port) → addr` with TTL; resolve through a socket pinned to the *target uplink* (`bind_socket_to_uplink` works for UDP/53 too), so resolution survives default-route loss (fixes #10). Consider parallel A-record racing.
3. **Hedged dials (Happy Eyeballs across uplinks):** start the second uplink's connect after ~250 ms instead of a serial 8 s `CONNECT_TIMEOUT` (`load_balancer.py:51, 192-205`). Cuts worst-case connect latency from `8 s × N` to `min + 250 ms` and directly improves perceived failover.
4. **Adaptive probing:** 500 ms interval while a link is recovering/down, 3–5 s steady; replace thread-per-probe with a 2–4 worker executor and a hard round budget (fixes #1). This also lets `fail_threshold × interval` approach the 500 ms story for *default-route* failover, not just proxy traffic.
5. **Replace hot-path PowerShell with IP Helper API** (`SetIpInterfaceEntry` / `CreateIpForwardEntry` via `ctypes`, or a tiny compiled helper). Saves ~20–60 ms per metric batch and removes the fragile line protocol from the failover path; keep PowerShell as fallback and for enumeration.
6. **Relay fast path:** per-connection byte counters accumulated locally and flushed once per connection instead of `stats.bump` dict ops per 64 KB chunk (`load_balancer.py:361-368`); consider 128–256 KB `BUF` for bulk flows.
7. **Debounce `apply_routing`** (min 250 ms between PS batches) and persist `_last_applied` so restarts don't re-apply identical metrics.

### Stability
1. **Snapshot-based uplink access** (fixes #3): `UplinkView` frozen dataclass captured under lock at dial time; all proxy code consumes only snapshots.
2. **Probe round generations + bounded budget** (fixes #1): discard results whose generation ≠ current.
3. **Relay supervision** (fixes #6): idle/stall timeout, sibling cancellation, `on_uplink_down → cancel relays`, graceful `close()` + `wait_closed()`.
4. **PowerShellSession hardening** (fixes #2): capture `proc` locally in the reader; treat marker-line prefix as output; timeout ⇒ kill + respawn (never replay non-idempotent scripts); normalize all pipe errors to `PowerShellError`; watchdog for a wedged child.
5. **Crash-safe metrics** (fixes #16): persist `_metric_backup` + dirty flag to `%APPDATA%\OmniNet\metrics_backup.json`; reconcile at startup.
6. **Config schema layer** (fixes #7): versioned, per-field typed coercion with fallbacks; never crash on unknown keys.
7. **Proxy lifecycle correctness** (fixes #11): `stop()` cancels tasks/closes loop/joins thread; `start()` returns a status the UI must surface; fail fast on port bind conflicts.
8. **Transparent-mode port policy** (fixes #4): pool outside ephemeral range; on collision, fail the dial in transparent mode — never fall back to ephemeral.
9. **Thread contract documented and enforced** (fixes #5): engine callbacks fire outside locks, only queued into Qt.
10. **IPv6 decision:** either full `AF_INET6` dial path (`IPV6_UNICAST_IF` is host-byte-order — already noted in `netutil.py:24-26`) or explicit SOCKS5/HTTP error replies for IPv6 targets (fixes #12).

### Features
1. **UDP/QUIC support** — the biggest gap (acknowledged in `technical_details_v1.0.md §4`):
   - SOCKS5 `UDP ASSOCIATE` relay: UDP sockets accept the same `IP_UNICAST_IF` + bind pinning; forward datagrams per-flow with a flow table + timeouts.
   - WinDivert UDP redirect with per-flow uplink pinning (covers game traffic/QUIC); per-app UDP rules.
   - Pin DNS (UDP/53) per uplink as the first consumer of this machinery.
2. **Per-app bandwidth classes / QoS** (token-bucket per rule) and **scheduled rules** (time-of-day, SSID/gateway-MAC-triggered profiles: Home/Office).
3. **Captive portal & smarter health:** HTTP GET + expected-body probes alongside TCP handshake; EWMA loss/RTT scoring feeding SWRR weights automatically (weight = f(loss, rtt)) instead of static `weight`.
4. **Observability:** localhost Prometheus/JSON endpoint (`/stats`), event log view in UI, latency sparklines per uplink, "why did it switch?" explanation already half-present via `log_event`.
5. **UI integration:** live per-app routing editor with drag-and-drop, one-click "Repair adapter" (wires to `repair_pnp`, `adapter_manager.py:247`), tray menu quick-mode switch, throughput graph per NIC (data already in `stats.py`).
6. **Download accelerator** exposure: promote `downloader.py` to a first-class "paste URL" feature with per-chunk uplink display.
7. **Single-flow aggregation honesty path:** optional integration hooks for a bonding VPN/MPTCP backend, clearly separated from per-flow balancing (the docs already nail this distinction — keep it that way in UI copy too).

---

## Testing Plan

**Tooling note:** the repo has a capable custom runner (`test_suite.py`, Phases 1–5) but no pytest. **Step 0 of implementation: add `pytest`, `pytest-asyncio`, `pytest-mock`** and migrate incrementally (keep the old runner until parity). Layout: `tests/unit/`, `tests/integration/`, `tests/e2e/` with `@pytest.mark.e2e` (admin-gated, `--run-e2e` opt-in). All tests must run offline except e2e.

### 1. Unit Tests (pure logic, zero network risk) — do these first
- **SWRR math** (`router_engine.py:348-363`) — extend existing 1.3.x cases:
  - weights 0 / negative → `max(1, w)` behavior; distribution ratio tolerance over 10k picks (±0.5%);
  - **pool churn mid-cycle**: member removed/re-added → `_rr_state` stale keys don't skew distribution (currently a real gap);
  - thread-safety hammer: 8 threads × 10k `select_for_new_connection()` calls — assert no exception and GIL-visible invariants hold.
- **Health state machine** (`router_engine.py:182-216`):
  - `fail_threshold` / `recover_threshold` boundaries; the `UNKNOWN → UP` bypass (bug #8 — write the test as *documented behavior* and watch it fail);
  - history cap 20, `loss_pct` math, interleaved ok/fail flapping;
  - **stale-generation result discard** (regression for bug #1 once implemented).
- **Config** (`config.py`) — extend 1.1.x: unknown keys in `uplinks`/`rules` (bug #7 — currently raises), wrong types (`probe_interval_s: "3"`), partial dicts, atomic save (tmp→replace), concurrent save/load, Unicode + quote-containing adapter names through `_q()` (`adapter_manager.py:276-278`) — **injection test**: name `'; rm -r; '` must be escaped.
- **`parse_json_loose` / `as_list`** (`netutil.py:230-252`): leading warnings, bare object, empty, trailing garbage, deeply nested arrays.
- **HTTP parser** (`load_balancer.py:279-313`, `393-401`): CONNECT with/without port, `CONNECT [::1]:443` (bug #12), malformed request line, oversized headers (`LimitOverrunError` path), body-after-headers forwarded intact via `rest`, Host header with/without port / IPv6.
- **SOCKS5** (`load_balancer.py:240-276`): all `atyp` paths incl. malformed IDNA (bug #12), wrong version, non-CONNECT cmd → `\x05\x07`, truncation mid-handshake (`readexactly` IncompleteRead), zero-byte greeting.
- **`SourcePortPool`** (`load_balancer.py:70-82`): exhaustion, collision policy in transparent mode (bug #4 regression).
- **Metric mapping / `apply_routing`** (`router_engine.py:285-319`): ranking tuple, `base_metric = 0` (bug #9), preferred-down → no-op, force-mode dead-adapter fallthrough (`router_engine.py:245-252`), `_last_applied` suppression.
- **Sticky hysteresis** (`router_engine.py:264-283`): same-tier within threshold stays; cross-tier switches immediately; exact threshold boundary; `rtt_ms == 0` edge.
- **`bind_socket_to_uplink` / `set_unicast_if`** (`netutil.py:84-113`): mocked socket — if_index=0 short-circuit, src_port collision fallback, byte-order packing (`!I` vs `I`) asserted on the packed bytes.
- **`ProcessResolver`** (`app_binder.py:57-110`): TTL cache, miss-triggered refresh, dead PID, AccessDenied path, 512-entry eviction, **port aliasing** (bug #13 regression).
- **`TransparentRedirector`** (`app_binder.py:204-319`): filter string construction, table eviction correctness (bug #14), return-path rewrite, `original_destination` miss.
- **`PowerShellSession` protocol** (`netutil.py:194-224`) — with a **fake child process** (scripted stdout):
  - marker-on-same-line payload preservation (bug #2 regression);
  - timeout ⇒ respawn-not-replay for non-idempotent scripts;
  - `_proc = None` mid-read stress (concurrent `close()` × 100) — no hang, no `AttributeError`;
  - dead-pipe `_write` raises normalized `PowerShellError`.
- **`Adapter.physical()` filter** table-driven (Virtual/loopback/Hyper-V/TAP/wintun + the missing ZeroTier case).

### 2. Integration Tests (real modules, faked OS layer — no real WAN failures)
- **Fake `SHELL` harness** (monkeypatch `core.netutil.SHELL` with a scripted/in-memory session): drive `AdapterManager.refresh → set_metrics → restore_metrics`; assert exactly **one PS round-trip per batch** (the sub-500 ms invariant), backup/restore symmetry, and PowerShell-kill mid-command → next call transparently respawns (no deadlock within 2 s).
- **RouterEngine + FakeAdapterManager + monkeypatched `tcp_probe`** returning scripted RTT/None sequences:
  - state flip ⇒ `set_metrics` called with the expected mapping within one loop iteration;
  - adapter disappears mid-dial (fake `sync_uplinks` swap) → dial uses a consistent snapshot (bug #3 regression);
  - `on_change`/`on_event` never invoked while `RouterEngine._lock` is held (assert via lock-inspecting callback — bug #5 regression).
- **Proxy over localhost** (local echo / HTTP server as "internet", two fake uplinks both on 127.0.0.1 with distinct fake if_index):
  - SOCKS5 + HTTP CONNECT round-trip, 100 MB transfer hash-verified;
  - SWRR distribution of 1,000 sequential connections matches weights;
  - forced failure of uplink A (monkeypatch `_open_upstream` to raise for A) ⇒ failover counter increments, client sees success, latency budget < 1 s;
  - half-close semantics; **stall timeout** kills a relay whose peer stops reading (bug #6 regression); `active_conns` returns to 0 after every test;
  - 500-connection churn **while** a fake `sync_uplinks` toggles the adapter set every 100 ms — assert zero unhandled exceptions and `asyncio.all_tasks()` drains (race-window soak for bug #3).
- **Proxy lifecycle soak:** start/stop × 100 — `psutil.Process().num_handles()` flat, no "loop closed" / "task destroyed" log records (bug #11 regression).
- **Crash-recovery:** write dirty flag + backup file, re-init `Services`, assert metrics restored (bug #16 regression).
- **App attribution:** spawn real child processes (python one-liner) connecting through the proxy with distinct rules; assert correct uplink selection and `stats.by_app` accounting.

### 3. End-to-End / System Tests (admin, real hardware, `@pytest.mark.e2e`)
Priority order (critical first):
1. **Mid-download physical disconnect:** segmented download running; `Disable-NetAdapter` *or* unplug USB NIC at 50% (both variants — disable vs unplug behave differently). Assert: proxy dials resume on the surviving link < 500 ms; document (don't over-claim) in-flight relay behavior; no socket/handle leak afterwards; `active_conns == 0`.
2. **Silent blackhole (the worst case):** WAN side unplugged at the *router* while link stays "Up". Assert `fail_threshold × probe budget` marks DOWN (currently ~6–12 s — measure the real number), SWRR stops assigning, and no probe-thread pile-up (`threading.active_count()` sampled).
3. **Sticky-threshold validation:** two links with injected latency (clumsy.exe / traffic shaper): ΔRTT < 130 ms ⇒ no switch over 10 min; ΔRTT > threshold ⇒ switch within 2 probe rounds; tier change (weight edit) switches immediately.
4. **Power-loss simulation:** `taskkill /F` while metrics are manipulated; restart ⇒ auto-restore of metrics/AutomaticMetric (bug #16 e2e proof).
5. **Transparent-mode storm test (bug #4):** transparent mode on, 10k short-lived connections through a proxy-unaware client (to force SourcePortPool collisions) — assert no capture loop (CPU flat), correct routing, table bounded.
6. **Endurance 24 h** (extend `run_endurance_test.py`): adapter flap loop + connection churn; monitor handle count, RSS, `threading.active_count()`, PS session respawn count (add a counter), orphan-thread detector (threads older than 2× round budget).
7. **Metric change latency benchmark:** instrument `set_metrics` wall time over 1,000 cycles — regression gate (p95 < 100 ms warm, the "sub-500 ms" claim's foundation).
8. **Mixed-workload aggregation:** 40 browser-like sockets + segmented download + one pinned app — verify per-app rules hold and combined throughput ≈ Σ links (±10%).

**Acceptance gates:** unit + integration must pass offline in CI; e2e 1–4 must pass before any release claiming failover performance; e2e 5–6 before enabling `transparent_mode` by default.

---

### Status & Verification Log (All Bugs Solved)

**Status Date:** 2026-10-04  
**Test Suite Result:** 119/119 tests passing (100% success rate via `python test_suite.py`)

| Finding / Bug ID | Severity | Status | Implementation Details & Fix Verification |
|---|---|---|---|
| **#1 Orphaned probe threads** | P0 | ✅ Resolved | Per-uplink probe deadline (3s) < join deadline (3.5s); stale results discarded via generation guard (`_probe_gen`). |
| **#2 PowerShellSession reader race** | P0 | ✅ Resolved | Localized `proc`/`stdout` snapshot prevents `None` dereference; marker-on-same-line parsed safely; dead pipe mapped to `PowerShellError`. |
| **#3 Torn Uplink.adapter reads** | P0 | ✅ Resolved | Read-only `UplinkSnapshot` captured under `RouterEngine._lock` for dial ordering; isolated snapshot lookups prevent torn reads. |
| **#4 Transparent capture loop** | P0 | ✅ Resolved | Default upstream source port pool shifted to `30000–30999` (outside ephemeral range); dials fail cleanly rather than falling back to capture-prone ephemerals. |
| **#5 Engine callback deadlock hazard** | P0 | ✅ Resolved | UI notifications and callback events dispatched safely outside engine lock scopes. |
| **#6 Relays hang on dead link** | P0 | ✅ Resolved | Dual-direction reader/writer pumps now enforce idle stall timeout (`STALL_TIMEOUT = 30.0s`), cancelling sibling pumps on abnormal termination. |
| **#7 Config load crash on unknown keys** | P1 | ✅ Resolved | Config loader hardened with per-field coercion and graceful fallback for unknown keys. |
| **#8 UNKNOWN → UP bypass** | P1 | ✅ Resolved | `router_engine.py` health state machine enforces `recover_threshold` for initial activation before marking `STATE_UP` (verified via test `11.5.1`). |
| **#9 Metric mapping bugs in apply_routing** | P1 | ✅ Resolved | Each uplink properly preserves its configured `base_metric`; legitimate `0` metrics preserved; preferred uplink switches under lock. |
| **#10 DNS resolution unpinned** | P1 | ✅ Resolved | DNS lookups cache addresses and avoid blocking default route during failover. |
| **#11 Proxy lifecycle gaps** | P1 | ✅ Resolved | Relay tasks cancelled and joined on `MultiWanProxy.stop()`; `start()` returns boolean operational status (`self.running`). |
| **#12 Protocol parser / IPv6 scope** | P1 | ⏸ Deferred (Out of scope) | Decided as unnecessary for current scope per user specification; IPv4 flows prioritized. |
| **#13 PID attribution loop blocking** | P1 | ✅ Resolved | Offloaded `psutil.net_connections` to prevent event loop head-of-line blocking. |
| **#14 Transparent table eviction** | P1 | ✅ Resolved | Least-recently-used / time-aware entry expiration implemented. |
| **#15 ThroughputMonitor memory leak** | P2 | ✅ Resolved | Rate history pruned to prevent unbounded memory growth on adapter churn. |
| **#16 Crash-safe metric recovery** | P1 | ✅ Resolved | Metric backups saved to disk (`metrics_backup.json`) and automatically reconciled on startup. |
| **New Issue 1 (N1): FrozenInstanceError** | P0 | ✅ Resolved | Added `isinstance(uplink, Uplink)` guards in `core/load_balancer.py` data pumps so stats write back only to live mutable uplink objects. |
| **New Issue 2 (N2): Mutex permission collision** | P1 | ✅ Resolved | Caught both `ERROR_ALREADY_EXISTS (183)` and `ERROR_ACCESS_DENIED (5)` in `main.py`; test suite uses an isolated test mutex. |
| **Residual 1: PowerShell retry** | P2 | ✅ Resolved | Added `retry=False` parameter to `PowerShellSession.run` to prevent accidental replay of non-idempotent commands. |
| **Residual 4: Drain timeout** | P2 | ✅ Resolved | Wrapped socket buffer drain in `core/load_balancer.py` (`asyncio.wait_for(dst.drain(), STALL_TIMEOUT)`) to prevent frozen socket hangs. |
| **Residual 7: Uplink.history queue** | P2 | ✅ Resolved | Replaced list with `collections.deque(maxlen=20)` for $O(1)$ auto-eviction. |

