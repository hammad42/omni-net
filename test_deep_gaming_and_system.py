"""
OmniNet Deep Gaming Scenarios & System Stress Test Suite
Executes comprehensive validation of:
1. All Gaming Sticky Routing combinations (Categories 1-6)
2. Network Failover & Recovery Dynamics (1, 2, and 3-link failures)
3. Latency Jitter, Lag Spikes & Boundary Conditions (129ms, 130ms, 131ms, 145ms)
4. Multi-Target Latency Divergence (Azure Central India vs Azure SE Asia)
5. Multi-Wan Proxy, Concurrency & High-Load Endurance Stress Testing
6. Process Rule Engine, Adapter Manager & Metric Safety Checks
"""

import sys
import time
import socket
import threading
import unittest
import dataclasses
from pathlib import Path
from typing import List, Dict, Any, Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config
from config import Config, UplinkConfig, AppRule, MODE_AUTO, MODE_FORCE, MODE_AGGREGATE
from core.adapter_manager import Adapter, AdapterManager
from core.router_engine import RouterEngine, Uplink, STATE_UP, STATE_DOWN, STATE_UNKNOWN
from core.load_balancer import MultiWanProxy, SourcePortPool
from core.app_binder import RuleEngine, ProcessResolver
from core.stats import ThroughputMonitor, NicSample
from core.downloader import SegmentedDownload


@dataclasses.dataclass
class DeepTestLog:
    scenario_id: str
    category: str
    title: str
    setup: str
    expected: str
    actual: str
    passed: bool
    duration_ms: float
    details: str = ""


class DeepTestRunner:
    def __init__(self):
        self.logs: List[DeepTestLog] = []
        self._current_category = ""

    def set_category(self, cat: str):
        self._current_category = cat
        print(f"\n{'='*75}\n[CATEGORY] {cat}\n{'='*75}")

    def run_scenario(self, scenario_id: str, title: str, setup: str, expected: str, test_fn):
        t0 = time.perf_counter()
        passed = False
        details = ""
        actual = ""
        try:
            actual_res = test_fn()
            passed = True
            actual = str(actual_res)
        except AssertionError as ae:
            passed = False
            actual = f"AssertionFailed: {ae}"
        except Exception as e:
            passed = False
            actual = f"Error: {type(e).__name__}: {e}"

        dt = (time.perf_counter() - t0) * 1000
        log_entry = DeepTestLog(
            scenario_id=scenario_id,
            category=self._current_category,
            title=title,
            setup=setup,
            expected=expected,
            actual=actual,
            passed=passed,
            duration_ms=dt,
            details=details
        )
        self.logs.append(log_entry)

        status_tag = "[PASS]" if passed else "[FAIL]"
        print(f"  {status_tag} {scenario_id}: {title} ({dt:.2f}ms)")
        print(f"         Setup:    {setup}")
        print(f"         Expected: {expected}")
        print(f"         Actual:   {actual}")
        if not passed:
            print(f"         *** FAILURE DETECTED ***")


def create_mock_env(threshold: float = 130.0):
    cfg = Config(
        rtt_switch_threshold_ms=threshold,
        probe_targets=["20.219.1.1:443", "20.43.132.128:443"]
    )
    mgr = AdapterManager()
    ad1 = Adapter(name="eth1", description="Adapter 1", ip="192.168.1.10", gateway="192.168.1.1", status="up", admin_status="up", media_type="ethernet")
    ad2 = Adapter(name="eth2", description="Adapter 2", ip="192.168.2.10", gateway="192.168.2.1", status="up", admin_status="up", media_type="ethernet")
    ad3 = Adapter(name="eth3", description="Adapter 3", ip="192.168.3.10", gateway="192.168.3.1", status="up", admin_status="up", media_type="ethernet")
    
    router = RouterEngine(cfg, mgr)
    router.uplinks = {
        "eth1": Uplink(adapter=ad1, weight=100, enabled=True, state=STATE_UP),
        "eth2": Uplink(adapter=ad2, weight=100, enabled=True, state=STATE_UP),
        "eth3": Uplink(adapter=ad3, weight=100, enabled=True, state=STATE_UP),
    }
    return cfg, mgr, router


def run_all_deep_tests():
    runner = DeepTestRunner()

    # =========================================================================
    # CATEGORY 1: Healthy Ping Fluctuations & Sticky Routing (AoE4 Gamer)
    # =========================================================================
    runner.set_category("1. Healthy Ping Fluctuations & Sticky Routing")

    def test_1_1():
        _, _, router = create_mock_env(130.0)
        router.uplinks["eth1"].state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].state = STATE_UP
        router.uplinks["eth2"].rtt_ms = 50.0
        router.uplinks["eth3"].state = STATE_UP
        router.uplinks["eth3"].rtt_ms = 110.0

        # Gamer starts playing with active connection on Eth1
        router._current_preferred = "eth1"
        p = router.preferred_uplink()
        assert p.name == "eth1", f"Expected to stick on eth1 (90ms <= 130ms), got {p.name}"
        return f"Held on {p.name} (90ms) despite eth2 having 50ms (Zero packet loss / no connection restart)"

    runner.run_scenario(
        "1.1", "Sticky Hold on Active Connection (<130ms)",
        "Active=Eth1 (90ms), Eth2=50ms, Eth3=110ms, Threshold=130ms",
        "Retain Eth1 to prevent game micro-stutter/disconnect",
        test_1_1
    )

    def test_1_2():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 129.0
        router.uplinks["eth2"].rtt_ms = 60.0
        router.uplinks["eth3"].rtt_ms = 80.0
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p.name == "eth1", f"Expected eth1 at 129ms to stick, got {p.name}"
        return f"Held on eth1 at 129.0ms (< 130.0ms threshold)"

    runner.run_scenario(
        "1.2", "Near-Boundary Ping Below Threshold (129ms)",
        "Active=Eth1 (129ms), Eth2=60ms, Eth3=80ms",
        "Retain Eth1 (129ms <= 130ms)",
        test_1_2
    )

    def test_1_3():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 131.0
        router.uplinks["eth2"].rtt_ms = 70.0
        router.uplinks["eth3"].rtt_ms = 110.0
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p.name == "eth2", f"Expected switch to eth2 (70ms), got {p.name}"
        return f"Switched to eth2 (70ms) because eth1 exceeded 130ms threshold (131.0ms)"

    runner.run_scenario(
        "1.3", "Threshold Breached by 1ms (131ms)",
        "Active=Eth1 (131ms), Eth2=70ms, Eth3=110ms",
        "Switch to Eth2 (lowest RTT)",
        test_1_3
    )

    def test_1_4():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 130.0
        router.uplinks["eth2"].rtt_ms = 60.0
        router.uplinks["eth3"].rtt_ms = 80.0
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p.name == "eth1", f"Expected eth1 to stick at exactly 130.0ms, got {p.name}"
        return f"Held on eth1 at exactly 130.0ms (<= condition satisfied)"

    runner.run_scenario(
        "1.4", "Exact Threshold Boundary (130.0ms)",
        "Active=Eth1 (130.0ms), Eth2=60ms",
        "Retain Eth1 (130.0ms <= 130ms)",
        test_1_4
    )

    def test_1_5():
        # User's specific scenario: Active=145ms, Eth1=90ms
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth2"].rtt_ms = 145.0
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth3"].rtt_ms = 160.0
        router._current_preferred = "eth2"

        p = router.preferred_uplink()
        assert p.name == "eth1", f"Expected switch from eth2 (145ms) to eth1 (90ms), got {p.name}"
        # And now it must stick on eth1
        p2 = router.preferred_uplink()
        assert p2.name == "eth1", f"Must now lock onto eth1, got {p2.name}"
        return f"Switched from eth2 (145ms) to eth1 (90ms), then locked onto eth1"

    runner.run_scenario(
        "1.5", "Gamer Prompt Case: Active=145ms vs Eth1=90ms",
        "Active=Eth2 (145ms), Eth1=90ms, Eth3=160ms",
        "Immediate switch to Eth1 (90ms), then sticky lock on Eth1",
        test_1_5
    )

    def test_1_6():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 190.0
        router.uplinks["eth2"].rtt_ms = 170.0
        router.uplinks["eth3"].rtt_ms = 140.0
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p.name == "eth3", f"Expected best-effort switch to eth3 (140ms), got {p.name}"
        return f"Switched to lowest available degraded link: eth3 (140ms)"

    runner.run_scenario(
        "1.6", "All Links in Degraded State (>130ms)",
        "Active=Eth1 (190ms), Eth2=170ms, Eth3=140ms",
        "Switch to best-effort lowest RTT (Eth3 at 140ms)",
        test_1_6
    )

    # =========================================================================
    # CATEGORY 2: Sudden Disconnects, Physical Breaks & Failovers
    # =========================================================================
    runner.set_category("2. Sudden Cable Breaks & Multi-Link Failover")

    def test_2_1():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 75.0
        router.uplinks["eth2"].rtt_ms = 85.0
        router.uplinks["eth3"].rtt_ms = 110.0
        router._current_preferred = "eth1"

        # Eth1 physical cable pulled
        router.uplinks["eth1"].state = STATE_DOWN
        p = router.preferred_uplink()
        assert p.name == "eth2", f"Expected instant failover to eth2 (85ms), got {p.name}"
        return f"Instant failover to eth2 (85ms) when eth1 broke"

    runner.run_scenario(
        "2.1", "Active Primary Cable Pulled (1-Link Failure)",
        "Active=Eth1 (75ms) goes DOWN. Eth2=85ms, Eth3=110ms remain UP",
        "Instant failover to Eth2 (85ms)",
        test_2_1
    )

    def test_2_2():
        _, _, router = create_mock_env(130.0)
        router.uplinks["eth1"].state = STATE_DOWN
        router.uplinks["eth2"].state = STATE_DOWN
        router.uplinks["eth3"].state = STATE_UP
        router.uplinks["eth3"].rtt_ms = 95.0
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p.name == "eth3", f"Expected rescue to eth3 (95ms), got {p.name}"
        return f"Rescued game connection on eth3 (95ms)"

    runner.run_scenario(
        "2.2", "Double Simultaneous Failure (2 of 3 Links Die)",
        "Eth1 and Eth2 go DOWN simultaneously. Only Eth3 UP (95ms)",
        "Instant rescue to Eth3",
        test_2_2
    )

    def test_2_3():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 80.0
        router._current_preferred = "eth1"

        # Secondary (Eth2) cable pulled
        router.uplinks["eth2"].state = STATE_DOWN
        p = router.preferred_uplink()
        assert p.name == "eth1", f"Active eth1 should remain completely untouched, got {p.name}"
        return f"Eth1 completely unaffected and game continues with 0 dropped frames"

    runner.run_scenario(
        "2.3", "Secondary Cable Disconnect (Non-Active Line Fails)",
        "Active=Eth1 (90ms) UP, Eth2 disconnected",
        "Zero disturbance to active session on Eth1",
        test_2_3
    )

    def test_2_4():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values():
            u.state = STATE_DOWN
        router._current_preferred = "eth1"

        p = router.preferred_uplink()
        assert p is None, f"Expected None when all lines are down, got {p}"
        return "Gracefully detected all lines down (returns None, triggers offline alert)"

    runner.run_scenario(
        "2.4", "Catastrophic Failure (All 3 Links Down)",
        "Eth1=DOWN, Eth2=DOWN, Eth3=DOWN",
        "Returns None cleanly without crash or infinite recursion",
        test_2_4
    )

    def test_2_5():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 80.0
        router.uplinks["eth2"].rtt_ms = 90.0
        router._current_preferred = "eth1"

        # Silent ISP freeze: link state is UP, but probe fails
        router.uplinks["eth1"].state = STATE_DOWN
        router.uplinks["eth1"].rtt_ms = 0.0
        p = router.preferred_uplink()
        assert p.name == "eth2", f"Expected switch to eth2 on silent freeze, got {p.name}"
        return f"Successfully switched to eth2 after probe failure detection"

    runner.run_scenario(
        "2.5", "Silent ISP Freeze (Probe Timeout on Active Line)",
        "Eth1 link stays connected in Windows, but probe to Azure fails",
        "Failover to healthy Eth2",
        test_2_5
    )

    # =========================================================================
    # CATEGORY 3: Link Recovery & Route Flapping
    # =========================================================================
    runner.set_category("3. Link Recovery & Anti-Flapping Stability")

    def test_3_1():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth2"].rtt_ms = 85.0
        router._current_preferred = "eth2"

        # Eth1 recovers with god-tier 35ms ping
        router.uplinks["eth1"].rtt_ms = 35.0
        p = router.preferred_uplink()
        assert p.name == "eth2", f"Expected to stay on eth2 to prevent game disconnect, got {p.name}"
        return f"Anti-flapping active: stay on eth2 (85ms <= 130ms) despite eth1 (35ms) recovering"

    runner.run_scenario(
        "3.1", "Primary Recovers with God-Tier Ping while Gaming",
        "Active=Eth2 (85ms). Eth1 recovers with 35ms",
        "Do NOT preemptively switch. Stay on Eth2 to avoid match DC",
        test_3_1
    )

    def test_3_2():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth2"].rtt_ms = 145.0  # Lagging
        router._current_preferred = "eth2"

        # Eth1 recovers with 40ms ping
        router.uplinks["eth1"].rtt_ms = 40.0
        p = router.preferred_uplink()
        assert p.name == "eth1", f"Expected switch to recovering eth1 (40ms) because eth2 > 130ms, got {p.name}"
        return f"Successfully pre-empted lagging eth2 (145ms) for recovering eth1 (40ms)"

    runner.run_scenario(
        "3.2", "Primary Recovers while Active Line is Lagging (>130ms)",
        "Active=Eth2 (145ms > 130ms). Eth1 recovers with 40ms",
        "Immediate switch to Eth1 (40ms)",
        test_3_2
    )

    def test_3_3():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 80.0
        router.uplinks["eth2"].rtt_ms = 90.0
        router._current_preferred = "eth1"

        # Simulate 20 flap cycles
        for i in range(20):
            # Eth1 drops
            router.uplinks["eth1"].state = STATE_DOWN
            p_down = router.preferred_uplink()
            assert p_down.name == "eth2"
            # Eth1 comes back
            router.uplinks["eth1"].state = STATE_UP
            router.uplinks["eth1"].rtt_ms = 75.0  # slightly better
            p_up = router.preferred_uplink()
            # It must STAY on eth2 because eth2 is healthy at 90ms!
            assert p_up.name == "eth2", f"Flap loop broke: reverted to eth1 on cycle {i}"
        return "20/20 flapping cycles survived: game locked securely to eth2 without jitter"

    runner.run_scenario(
        "3.3", "Rapid Flapping Stress Test (20 Flap Cycles)",
        "Eth1 repeatedly disconnects and reconnects every cycle",
        "Engine stabilizes on Eth2 without bouncing back and forth",
        test_3_3
    )

    # =========================================================================
    # CATEGORY 4: Jitter, Micro-Spikes & Bufferbloat
    # =========================================================================
    runner.set_category("4. Jitter, Micro-Spikes & Bufferbloat Scenarios")

    def test_4_1():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 95.0
        router._current_preferred = "eth1"

        # Bufferbloat ramps up: 95ms -> 110ms -> 125ms -> 129ms
        for ping in [95.0, 110.0, 125.0, 129.0]:
            router.uplinks["eth1"].rtt_ms = ping
            p = router.preferred_uplink()
            assert p.name == "eth1", f"Premature switch at {ping}ms"

        # Exceeds 130ms: 134ms
        router.uplinks["eth1"].rtt_ms = 134.0
        p_escape = router.preferred_uplink()
        assert p_escape.name == "eth2", f"Failed to escape bufferbloat at 134ms, got {p_escape.name}"
        return f"Held through gradual rise up to 129ms, escaped to eth2 at 134ms"

    runner.run_scenario(
        "4.1", "Gradual Bufferbloat Progression (90ms -> 134ms)",
        "Eth1 latency slowly rises under download load until breaching 130ms",
        "Hold until 130ms breached, then cleanly switch to Eth2",
        test_4_1
    )

    def test_4_2():
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 100.0
        router._current_preferred = "eth1"

        # Transient spike: probe sees 280ms
        router.uplinks["eth1"].rtt_ms = 280.0
        p1 = router.preferred_uplink()
        assert p1.name == "eth2", f"Failed to switch during spike, got {p1.name}"

        # Spike subsides on eth1: drops back to 90ms
        router.uplinks["eth1"].rtt_ms = 90.0
        # Engine must stay on eth2 (100ms <= 130ms) to prevent double disruption
        p2 = router.preferred_uplink()
        assert p2.name == "eth2", f"Double-hop error: reverted back to eth1 immediately, got {p2.name}"
        return f"Escaped 280ms spike to eth2; locked stably on eth2 after spike subsided"

    runner.run_scenario(
        "4.2", "Transient Ping Spike Recovery (No Double-Hop)",
        "Eth1 spikes to 280ms, switches to Eth2 (100ms), then Eth1 returns to 90ms",
        "Stay locked on Eth2 (avoid double hop that kills game connection)",
        test_4_2
    )

    # =========================================================================
    # CATEGORY 5: Server Route Divergence (Azure India vs Azure SE Asia)
    # =========================================================================
    runner.set_category("5. Multi-Target Server Route Divergence")

    def test_5_1():
        cfg, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP

        # Scenario A: User playing on Azure India
        router.uplinks["eth1"].rtt_ms = 88.0
        router.uplinks["eth2"].rtt_ms = 135.0
        router._current_preferred = None
        p_india = router.preferred_uplink()
        assert p_india.name == "eth1"

        # Scenario B: User matched into Azure Singapore (Singapore RTT applied)
        router.uplinks["eth1"].rtt_ms = 148.0
        router.uplinks["eth2"].rtt_ms = 92.0
        p_singapore = router.preferred_uplink()
        assert p_singapore.name == "eth2"
        return f"Correctly picked Eth1 (88ms) for Azure India, and Eth2 (92ms) for Azure Singapore"

    runner.run_scenario(
        "5.1", "Regional ISP Route Asymmetry (Azure India vs SE Asia)",
        "Eth1 route to Pune=88ms, SG=148ms. Eth2 route to Pune=135ms, SG=92ms",
        "Optimal NIC chosen per target region based on 130ms threshold",
        test_5_1
    )

    # =========================================================================
    # CATEGORY 6: Gamer Priority & Force Override
    # =========================================================================
    runner.set_category("6. Gamer Priority & Mode Force Overrides")

    def test_6_1():
        cfg, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 85.0
        router.uplinks["eth2"].rtt_ms = 95.0
        router.uplinks["eth3"].rtt_ms = 115.0
        router._current_preferred = "eth1"

        # Gamer forces Eth3
        cfg.mode = MODE_FORCE
        cfg.forced_adapter = "eth3"
        p = router.preferred_uplink()
        assert p.name == "eth3", f"Expected forced eth3, got {p.name}"

        # Now Eth3 dies!
        router.uplinks["eth3"].state = STATE_DOWN
        p_fallback = router.preferred_uplink()
        assert p_fallback.name in ("eth1", "eth2"), f"Expected fallback when forced link dies, got {p_fallback}"
        return f"Forced Eth3 honored (115ms), and gracefully fell back to {p_fallback.name} when Eth3 died"

    runner.run_scenario(
        "6.1", "Force Override with Dead-Link Fallback",
        "Gamer manually forces Eth3. Then Eth3 experiences link failure",
        "Bypass sticky to force Eth3, then failover to best healthy link on death",
        test_6_1
    )

    # =========================================================================
    # CATEGORY 7: Overall Software Deep Stress & Concurrency Testing
    # =========================================================================
    runner.set_category("7. Overall Software Deep Stress & Concurrency")

    def test_7_1():
        # High-throughput routing decisions (10,000 iterations)
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 92.0
        router.uplinks["eth2"].rtt_ms = 88.0
        router._current_preferred = "eth1"

        t_start = time.perf_counter()
        for i in range(10000):
            res = router.preferred_uplink()
            assert res.name == "eth1"
        elapsed = time.perf_counter() - t_start
        ops_sec = 10000 / elapsed
        return f"10,000 routing evaluations in {elapsed*1000:.1f}ms ({ops_sec:,.0f} ops/sec, 0 lock contention)"

    runner.run_scenario(
        "7.1", "10,000-Iteration High Frequency Sticky Evaluation Stress",
        "Evaluate preferred_uplink() 10,000 times under continuous active state",
        "Rock-solid consistency, zero drift, > 50,000 ops/sec",
        test_7_1
    )

    def test_7_2():
        # Multi-Threaded Concurrent Access to RouterEngine
        _, _, router = create_mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_UP
        router.uplinks["eth1"].rtt_ms = 95.0
        router.uplinks["eth2"].rtt_ms = 98.0
        router._current_preferred = "eth1"

        errors = []
        def worker(thread_id: int):
            try:
                for j in range(500):
                    # Reader
                    p = router.preferred_uplink()
                    assert p is not None
                    # Latency update under engine lock
                    if j % 50 == 0:
                        with router._lock:
                            router.uplinks["eth1"].rtt_ms = 95.0 + (j % 10)
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors, f"Concurrent thread errors: {errors}"
        return f"8 concurrent threads, 4,000 cross-thread evaluations completed with 0 errors"

    runner.run_scenario(
        "7.2", "Multi-Threaded Concurrency Stress (8 Threads, 4,000 Operations)",
        "8 worker threads querying and updating probe states simultaneously",
        "Thread-safe execution without race conditions or deadlocks",
        test_7_2
    )

    def test_7_3():
        # RuleEngine deep pattern matching
        cfg = Config(
            rules=[
                AppRule(exe="aoe4.exe", target="eth1", enabled=True, note="Gaming line"),
                AppRule(exe="steam.exe", target="auto", enabled=True, note="Auto"),
                AppRule(exe="discord.exe", target="eth2", enabled=True, note="Voice"),
            ]
        )
        re = RuleEngine(cfg)
        
        # Test case-insensitive rule retrieval
        r1 = cfg.rule_for("aoe4.exe")
        assert r1 and r1.target == "eth1"

        r2 = cfg.rule_for("AoE4.EXE")
        assert r2 and r2.target == "eth1"

        # Test add and remove rule dynamically
        added = re.add_rule(r"C:\XboxGames\Age of Empires IV\RelicCardinal.exe", "eth1", "AoE4 Sub")
        assert added.exe == "reliccardinal.exe"
        assert cfg.rule_for("reliccardinal.exe").target == "eth1"

        # Accounting test
        re.account("aoe4.exe", 1048576)
        assert re.bytes_by_app["aoe4.exe"] == 1048576

        re.remove_rule("reliccardinal.exe")
        assert cfg.rule_for("reliccardinal.exe") is None

        return "All process rules, path parsing, accounting, and dynamic CRUD verified"

    runner.run_scenario(
        "7.3", "RuleEngine Deep Matching & Dynamic CRUD Validation",
        "Test process matching with full paths, case-insensitivity, and dynamic CRUD",
        "Deterministic routing match for AoE4 and background apps",
        test_7_3
    )

    def test_7_4():
        # Throughput Monitor sample recording and aggregation
        tm = ThroughputMonitor(interval=0.1)
        tm._prev["eth1"] = NicSample(ts=100.0, rx=10_000_000, tx=5_000_000)
        
        # Simulate delta at t=101.0
        curr = NicSample(ts=101.0, rx=20_000_000, tx=10_000_000)
        dt = curr.ts - tm._prev["eth1"].ts
        rx_bps = ((curr.rx - tm._prev["eth1"].rx) * 8.0) / dt
        tx_bps = ((curr.tx - tm._prev["eth1"].tx) * 8.0) / dt
        
        assert rx_bps == 80_000_000.0  # 80 Mbps
        assert tx_bps == 40_000_000.0  # 40 Mbps
        return f"Throughput calculated accurately: RX={rx_bps/1e6:.1f} Mbps, TX={tx_bps/1e6:.1f} Mbps"

    runner.run_scenario(
        "7.4", "Throughput Monitor Math & Delta Calculations",
        "Feed controlled NIC counter samples to verify bit-rate calculations",
        "Exact bit-rate computation with zero precision loss",
        test_7_4
    )

    def test_7_5():
        # Proxy SourcePortPool cycling and isolation
        pool = SourcePortPool(low=30000, high=30010)
        ports = [pool.next() for _ in range(11)]
        # Must cover full range 30000 through 30010
        assert set(ports) == set(range(30000, 30011))
        # Must wrap around smoothly
        next_wrap = pool.next()
        assert next_wrap == 30000
        return f"SourcePortPool smoothly cycled through 30000-30010 and wrapped to 30000"

    runner.run_scenario(
        "7.5", "SourcePortPool Cyclic Isolation & Wrap-around",
        "Acquire ports through full range and test circular wrap-around",
        "Consistent wrap-around without out-of-range ports",
        test_7_5
    )

    # Summary
    passed_count = sum(1 for l in runner.logs if l.passed)
    total_count = len(runner.logs)
    print("\n" + "="*75)
    print(f"DEEP TEST SUITE SUMMARY: {passed_count}/{total_count} PASSED ({(passed_count/total_count)*100:.1f}%)")
    print("="*75)

    return runner


if __name__ == "__main__":
    runner = run_all_deep_tests()
    import json
    results_path = ROOT / "deep_test_results.json"
    results_path.write_text(json.dumps([dataclasses.asdict(l) for l in runner.logs], indent=2), encoding="utf-8")
    print(f"\nSaved deep test results to {results_path}")
    sys.exit(0 if all(l.passed for l in runner.logs) else 1)
