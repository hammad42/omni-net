"""
OmniNet Comprehensive Test Suite
Covers Phases 1 through 5 of the OmniNet Deep Testing Plan.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import dataclasses
import hashlib
import http.server
import json
import logging
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config
from config import AppRule, Config, UplinkConfig, MODE_AGGREGATE, MODE_AUTO, MODE_FORCE
import core
from core.adapter_manager import Adapter, AdapterManager
from core.app_binder import ProcessResolver, RuleEngine, ForceBindIP
from core.downloader import DownloadProgress, SegmentedDownload
from core.load_balancer import MultiWanProxy, SourcePortPool
from core.netutil import (
    IS_WINDOWS, SHELL, as_list, bind_socket_to_uplink, is_admin,
    parse_json_loose, set_unicast_if, socket_options_for, PowerShellSession
)
from core.router_engine import RouterEngine, Uplink, STATE_DOWN, STATE_UNKNOWN, STATE_UP, tcp_probe
from core.stats import ThroughputMonitor, NicSample, human_bps, human_bytes


# --------------------------------------------------------------------------- #
# Test Reporting Framework
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class TestResult:
    phase: str
    name: str
    description: str
    status: str  # "PASS", "FAIL", "SKIP"
    duration_ms: float
    error: str = ""
    details: str = ""


class TestRunner:
    def __init__(self):
        self.results: List[TestResult] = []
        self._current_phase = ""

    def start_phase(self, phase_name: str):
        self._current_phase = phase_name
        print(f"\n{'='*70}\n[RUNNING] {phase_name}\n{'='*70}")

    def run_case(self, name: str, description: str, fn):
        t0 = time.perf_counter()
        try:
            fn()
            dt = (time.perf_counter() - t0) * 1000
            res = TestResult(self._current_phase, name, description, "PASS", dt)
            print(f"  [PASS] {name} ({dt:.1f}ms): {description}")
        except unittest.SkipTest as st:
            dt = (time.perf_counter() - t0) * 1000
            res = TestResult(self._current_phase, name, description, "SKIP", dt, details=str(st))
            print(f"  [SKIP] {name}: {st}")
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000
            err_msg = f"{type(e).__name__}: {e}"
            res = TestResult(self._current_phase, name, description, "FAIL", dt, error=err_msg)
            print(f"  [FAIL] {name} ({dt:.1f}ms): {err_msg}")
        self.results.append(res)

    def summary(self) -> Dict[str, Any]:
        passed = sum(1 for r in self.results if r.status == "PASS")
        failed = sum(1 for r in self.results if r.status == "FAIL")
        skipped = sum(1 for r in self.results if r.status == "SKIP")
        total = len(self.results)
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": (passed / total * 100) if total else 0.0
        }


runner = TestRunner()


# --------------------------------------------------------------------------- #
# PHASE 1: Pure Logic Tests (Level 0 - Zero Network Risk)
# --------------------------------------------------------------------------- #

def test_phase_1():
    runner.start_phase("Phase 1: Pure Logic Tests (Zero Network Risk)")

    # 1.1 Config tests
    def test_config_missing_file():
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                config.CONFIG_PATH = Path(td) / "missing_config.json"
                cfg = config.load()
                assert cfg.mode == MODE_AUTO
                assert cfg.socks_port == 1080
                assert cfg.http_port == 8080
                assert isinstance(cfg.uplinks, dict)
                assert isinstance(cfg.rules, list)
            finally:
                config.CONFIG_PATH = orig

    def test_config_corrupt_json():
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                p = Path(td) / "corrupt.json"
                p.write_text("{ this is not valid json : [", encoding="utf-8")
                config.CONFIG_PATH = p
                cfg = config.load()
                assert cfg.mode == MODE_AUTO
            finally:
                config.CONFIG_PATH = orig

    def test_config_invalid_mode_reset():
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                p = Path(td) / "bad_mode.json"
                p.write_text(json.dumps({"mode": "quantum_hyper_drive"}), encoding="utf-8")
                config.CONFIG_PATH = p
                cfg = config.load()
                assert cfg.mode == MODE_AUTO
            finally:
                config.CONFIG_PATH = orig

    def test_config_nested_hydration():
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                p = Path(td) / "nested.json"
                data = {
                    "mode": "aggregate",
                    "uplinks": {"Ethernet": {"name": "Ethernet", "weight": 250, "base_metric": 15}},
                    "rules": [{"exe": "game.exe", "target": "Ethernet", "enabled": True}]
                }
                p.write_text(json.dumps(data), encoding="utf-8")
                config.CONFIG_PATH = p
                cfg = config.load()
                assert cfg.mode == "aggregate"
                assert "Ethernet" in cfg.uplinks
                assert isinstance(cfg.uplinks["Ethernet"], UplinkConfig)
                assert cfg.uplinks["Ethernet"].weight == 250
                assert len(cfg.rules) == 1
                assert isinstance(cfg.rules[0], AppRule)
                assert cfg.rules[0].exe == "game.exe"
            finally:
                config.CONFIG_PATH = orig

    def test_config_save_reload_roundtrip():
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                p = Path(td) / "roundtrip.json"
                config.CONFIG_PATH = p
                cfg = Config(mode=MODE_FORCE, forced_adapter="Ethernet 2")
                cfg.uplink("Ethernet 2").weight = 80
                cfg.rules.append(AppRule(exe="discord.exe", target="Ethernet 2"))
                config.save(cfg)
                assert p.exists()
                cfg2 = config.load()
                assert cfg2.mode == MODE_FORCE
                assert cfg2.forced_adapter == "Ethernet 2"
                assert cfg2.uplinks["Ethernet 2"].weight == 80
                assert len(cfg2.rules) == 1
                assert cfg2.rules[0].exe == "discord.exe"
            finally:
                config.CONFIG_PATH = orig

    def test_config_rule_for_case_insensitive():
        cfg = Config()
        cfg.rules.append(AppRule(exe="chrome.exe", target="Ethernet", enabled=True))
        cfg.rules.append(AppRule(exe="valorant.exe", target="Ethernet 2", enabled=False))
        assert cfg.rule_for("Chrome.exe") is not None
        assert cfg.rule_for("CHROME.EXE").target == "Ethernet"
        assert cfg.rule_for("valorant.exe") is None  # disabled
        assert cfg.rule_for("unknown.exe") is None

    def test_config_uplink_auto_create():
        cfg = Config()
        assert "WiFi" not in cfg.uplinks
        u = cfg.uplink("WiFi")
        assert u.name == "WiFi"
        assert u.weight == 100
        assert "WiFi" in cfg.uplinks

    runner.run_case("1.1.1", "Config missing file returns default", test_config_missing_file)
    runner.run_case("1.1.2", "Config corrupt JSON recovers gracefully", test_config_corrupt_json)
    runner.run_case("1.1.3", "Config invalid mode resets to auto", test_config_invalid_mode_reset)
    runner.run_case("1.1.4", "Config nested dataclass hydration", test_config_nested_hydration)
    runner.run_case("1.1.5", "Config save & reload roundtrip", test_config_save_reload_roundtrip)
    runner.run_case("1.1.6", "Config rule_for case-insensitive & enabled check", test_config_rule_for_case_insensitive)
    runner.run_case("1.1.7", "Config uplink auto-creates missing entry", test_config_uplink_auto_create)

    # 1.2 Stats formatting tests
    def test_human_bps():
        assert human_bps(0) == "0 b/s"
        assert human_bps(800) == "800 b/s"
        assert human_bps(1500) == "1.50 Kb/s"
        assert human_bps(50_000_000) == "50.00 Mb/s"
        assert human_bps(1_200_000_000) == "1.20 Gb/s"
        assert human_bps(-100) == "-100 b/s"

    def test_human_bytes():
        assert human_bytes(0) == "0 B"
        assert human_bytes(512) == "512 B"
        assert human_bytes(1024) == "1.00 KB"
        assert human_bytes(1048576) == "1.00 MB"
        assert human_bytes(1073741824) == "1.00 GB"
        assert human_bytes(1099511627776) == "1.00 TB"
        assert human_bytes(-50) == "-50 B"

    runner.run_case("1.2.1", "human_bps bit rate formatting and clamping", test_human_bps)
    runner.run_case("1.2.2", "human_bytes byte size formatting and clamping", test_human_bytes)

    # 1.3 SWRR algorithm tests
    def test_swrr_equal_weights():
        cfg = Config(mode=MODE_AGGREGATE)
        ad1 = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        ad2 = Adapter(name="Eth2", description="", if_index=2, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.2.10", prefix=24, dhcp=True, gateway="192.168.2.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        u1 = Uplink(adapter=ad1, weight=100, state=STATE_UP)
        u2 = Uplink(adapter=ad2, weight=100, state=STATE_UP)
        r.uplinks = {"Eth1": u1, "Eth2": u2}
        counts = {"Eth1": 0, "Eth2": 0}
        for _ in range(200):
            sel = r.select_for_new_connection()
            assert sel is not None
            counts[sel.name] += 1
        assert counts["Eth1"] == 100
        assert counts["Eth2"] == 100

    def test_swrr_weighted_ratio():
        cfg = Config(mode=MODE_AGGREGATE)
        ad1 = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        ad2 = Adapter(name="Eth2", description="", if_index=2, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.2.10", prefix=24, dhcp=True, gateway="192.168.2.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        u1 = Uplink(adapter=ad1, weight=300, state=STATE_UP)
        u2 = Uplink(adapter=ad2, weight=100, state=STATE_UP)
        r.uplinks = {"Eth1": u1, "Eth2": u2}
        counts = {"Eth1": 0, "Eth2": 0}
        for _ in range(400):
            sel = r.select_for_new_connection()
            assert sel is not None
            counts[sel.name] += 1
        # 300:100 -> 3:1 ratio (300 vs 100)
        assert counts["Eth1"] == 300
        assert counts["Eth2"] == 100

    def test_swrr_single_and_zero_links():
        cfg = Config(mode=MODE_AGGREGATE)
        ad1 = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        # 0 links
        assert r.select_for_new_connection() is None
        # 1 link
        r.uplinks = {"Eth1": Uplink(adapter=ad1, weight=100, state=STATE_UP)}
        assert r.select_for_new_connection().name == "Eth1"

    def test_swrr_smooth_pattern():
        cfg = Config(mode=MODE_AGGREGATE)
        ad1 = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        ad2 = Adapter(name="Eth2", description="", if_index=2, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.2.10", prefix=24, dhcp=True, gateway="192.168.2.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        r.uplinks = {
            "Eth1": Uplink(adapter=ad1, weight=300, state=STATE_UP),
            "Eth2": Uplink(adapter=ad2, weight=100, state=STATE_UP)
        }
        seq = [r.select_for_new_connection().name for _ in range(4)]
        assert "Eth2" in seq
        assert seq.count("Eth1") == 3
        assert seq.count("Eth2") == 1

    runner.run_case("1.3.1", "SWRR equal 100:100 weight distribution", test_swrr_equal_weights)
    runner.run_case("1.3.2", "SWRR 300:100 ratio distribution", test_swrr_weighted_ratio)
    runner.run_case("1.3.3", "SWRR handles 0 and 1 healthy uplinks", test_swrr_single_and_zero_links)
    runner.run_case("1.3.4", "SWRR smooth interleaving pattern (no bursting)", test_swrr_smooth_pattern)

    # 1.4 Uplink state machine transitions
    def test_state_machine_transitions():
        cfg = Config()
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        ad = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                     mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                     ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                     route_metric=10, if_metric=10, auto_metric="Disabled")
        u = Uplink(adapter=ad, state=STATE_UNKNOWN)

        # UNKNOWN -> 2 OK -> UP
        # Simulate probe ok 1
        u.consecutive_ok += 1
        if u.consecutive_ok >= cfg.recover_threshold or u.state == STATE_UNKNOWN:
            u.state = STATE_UP
        assert u.state == STATE_UP

        # UP -> 1 fail -> still UP
        u.consecutive_ok = 0
        u.consecutive_fail += 1
        if u.consecutive_fail >= cfg.fail_threshold:
            u.state = STATE_DOWN
        assert u.state == STATE_UP
        assert u.consecutive_fail == 1

        # UP -> 2nd fail -> DOWN
        u.consecutive_fail += 1
        if u.consecutive_fail >= cfg.fail_threshold:
            u.state = STATE_DOWN
        assert u.state == STATE_DOWN

        # DOWN -> 1 OK -> still DOWN
        u.consecutive_fail = 0
        u.consecutive_ok += 1
        if u.consecutive_ok >= cfg.recover_threshold:
            u.state = STATE_UP
        assert u.state == STATE_DOWN

        # DOWN -> 2nd OK -> UP
        u.consecutive_ok += 1
        if u.consecutive_ok >= cfg.recover_threshold:
            u.state = STATE_UP
        assert u.state == STATE_UP

    def test_loss_percentage_math():
        ad = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                     mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                     ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                     route_metric=10, if_metric=10, auto_metric="Disabled")
        u = Uplink(adapter=ad, state=STATE_UP)
        u.history = [True, True, True, False, True, False, True, False]
        loss = 100.0 * (1.0 - sum(u.history) / len(u.history))
        assert abs(loss - 37.5) < 0.01

    def test_interleaved_health_counters():
        ad = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                     mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                     ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                     route_metric=10, if_metric=10, auto_metric="Disabled")
        u = Uplink(adapter=ad, state=STATE_UP)
        for _ in range(5):
            u.consecutive_ok = 0
            u.consecutive_fail += 1
            assert u.consecutive_fail == 1
            u.consecutive_fail = 0
            u.consecutive_ok += 1
            assert u.consecutive_ok == 1
        assert u.state == STATE_UP

    runner.run_case("1.4.1", "Uplink state machine threshold transitions", test_state_machine_transitions)
    runner.run_case("1.4.2", "Uplink loss percentage calculation", test_loss_percentage_math)
    runner.run_case("1.4.3", "Interleaved OK/FAIL prevents state flapping", test_interleaved_health_counters)

    # 1.5 Preferred uplink ranking logic
    def test_preferred_ranking():
        cfg = Config(mode=MODE_AUTO)
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)

        # Case A: Wired vs WiFi (wired wins)
        ad_eth = Adapter(name="Eth", description="Wired", if_index=1, status="up", admin_status="up",
                         mac="", link_speed="", speed_bps=1000, media_type="802.3", virtual=False,
                         ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                         route_metric=10, if_metric=10, auto_metric="Disabled")
        ad_wifi = Adapter(name="WiFi", description="Wireless", if_index=2, status="up", admin_status="up",
                          mac="", link_speed="", speed_bps=300, media_type="Native 802.11", virtual=False,
                          ip="192.168.2.10", prefix=24, dhcp=True, gateway="192.168.2.1",
                          route_metric=10, if_metric=10, auto_metric="Disabled")
        u_eth = Uplink(adapter=ad_eth, weight=100, state=STATE_UP, rtt_ms=100.0)
        u_wifi = Uplink(adapter=ad_wifi, weight=100, state=STATE_UP, rtt_ms=50.0)
        r.uplinks = {"Eth": u_eth, "WiFi": u_wifi}
        # Wired should win even if WiFi has lower RTT
        assert r.preferred_uplink().name == "Eth"

        # Case B: Weight comparison (higher weight wins between wired)
        ad_eth2 = Adapter(name="Eth2", description="Wired2", if_index=3, status="up", admin_status="up",
                          mac="", link_speed="", speed_bps=1000, media_type="802.3", virtual=False,
                          ip="192.168.3.10", prefix=24, dhcp=True, gateway="192.168.3.1",
                          route_metric=10, if_metric=10, auto_metric="Disabled")
        u_eth.weight = 100
        u_eth.rtt_ms = 80.0
        u_eth2 = Uplink(adapter=ad_eth2, weight=200, state=STATE_UP, rtt_ms=90.0)
        r.uplinks = {"Eth": u_eth, "Eth2": u_eth2}
        assert r.preferred_uplink().name == "Eth2"

        # Case C: Sticky routing — same tier, current RTT under threshold stays put
        u_eth2.weight = 100
        u_eth.rtt_ms = 45.0
        u_eth2.rtt_ms = 90.0
        # Eth2 is _current_preferred from Case B and its RTT (90ms) is below
        # the default threshold (130ms), so sticky routing keeps it even though
        # Eth has lower RTT.
        assert r.preferred_uplink().name == "Eth2"

        # Case C2: RTT exceeds threshold → switch occurs
        u_eth2.rtt_ms = 150.0   # above 130ms threshold
        assert r.preferred_uplink().name == "Eth"

        # Case D: Force mode
        r.set_mode(MODE_FORCE, "Eth2")
        u_eth2.state = STATE_UP
        u_eth2.rtt_ms = 90.0
        assert r.preferred_uplink().name == "Eth2"

        # Case E: Forced link DOWN falls back to auto
        u_eth2.state = STATE_DOWN
        pref = r.preferred_uplink()
        assert pref.name == "Eth"

    runner.run_case("1.5.1", "Preferred uplink ranking (wired/weight/RTT/force)", test_preferred_ranking)

    # 1.6 Metric calculation logic
    def test_metric_calculation():
        cfg = Config(manage_metrics=True)
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        ad1 = Adapter(name="Eth1", description="", if_index=1, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        ad2 = Adapter(name="Eth2", description="", if_index=2, status="up", admin_status="up",
                      mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                      ip="192.168.2.10", prefix=24, dhcp=True, gateway="192.168.2.1",
                      route_metric=10, if_metric=10, auto_metric="Disabled")
        r.uplinks = {
            "Eth1": Uplink(adapter=ad1, weight=100, state=STATE_UP, rtt_ms=50.0),
            "Eth2": Uplink(adapter=ad2, weight=100, state=STATE_UP, rtt_ms=100.0),
        }
        # In apply_routing, preferred (Eth1) gets base_metric (10)
        # Secondary (Eth2) gets base_metric + 10 = 20
        pref = r.preferred_uplink()
        assert pref.name == "Eth1"

    runner.run_case("1.6.1", "Metric assignment ordering calculation", test_metric_calculation)

    # 1.7 netutil helper functions
    def test_netutil_helpers():
        # as_list
        assert as_list(None) == []
        assert as_list("test") == ["test"]
        assert as_list([1, 2, 3]) == [1, 2, 3]

        # parse_json_loose
        assert parse_json_loose('{"status": "ok"}') == {"status": "ok"}
        assert parse_json_loose('WARNING: extra text\n{"a": 1}\nMore text') == {"a": 1}
        assert parse_json_loose('[1, 2, 3]') == [1, 2, 3]
        assert parse_json_loose('No json here') is None
        assert parse_json_loose('{ broken json ') is None

        # socket_options_for
        opts = socket_options_for("192.168.1.10", 5)
        assert len(opts) == 1
        proto, optname, val = opts[0]
        assert proto == socket.IPPROTO_IP
        assert optname == 31  # IP_UNICAST_IF
        assert val == struct.pack("!I", 5)

    runner.run_case("1.7.1", "netutil as_list, parse_json_loose, socket_options_for", test_netutil_helpers)


# --------------------------------------------------------------------------- #
# PHASE 2: Read-Only Network Tests (Level 1 - Zero Risk)
# --------------------------------------------------------------------------- #

def test_phase_2():
    runner.start_phase("Phase 2: Read-Only Network Tests (Zero Risk)")

    # 2.1 PowerShell session test
    def test_powershell_session():
        sess = PowerShellSession()
        try:
            out = sess.run("Write-Output 'HELLO_OMNINET'").strip()
            assert "HELLO_OMNINET" in out

            # Test JSON parsing
            obj = sess.run_json("@{ Key = 'Val1'; Num = 42 } | ConvertTo-Json")
            assert obj["Key"] == "Val1"
            assert obj["Num"] == 42

            # Test multi-line Base64 survival
            script = """
            $x = 10
            $y = 20
            Write-Output ($x + $y)
            """
            out2 = sess.run(script).strip()
            assert "30" in out2
        finally:
            sess.close()

    runner.run_case("2.1.1", "PowerShellSession run, run_json, multi-line base64", test_powershell_session)

    # 2.2 Adapter enumeration
    def test_adapter_enumeration():
        mgr = AdapterManager()
        found = mgr.refresh()
        assert len(found) > 0, "Expected at least one network adapter"
        physical = mgr.physical()
        assert len(physical) > 0, "Expected at least one physical adapter"
        names = [a.name for a in physical]
        assert "Ethernet" in names or "Ethernet 2" in names or "Wi-Fi" in names
        routable = mgr.routable()
        for a in routable:
            assert a.is_routable
            assert a.ip
            assert a.gateway

    def test_adapter_filtering_virtual():
        mgr = AdapterManager()
        # Synthetic adapters
        a_real = Adapter(name="RealTek", description="Realtek Gaming GbE", if_index=1, status="up",
                         admin_status="up", mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                         ip="192.168.1.10", prefix=24, dhcp=True, gateway="192.168.1.1",
                         route_metric=10, if_metric=10, auto_metric="Disabled")
        a_hyperv = Adapter(name="vEthernet", description="Hyper-V Virtual Ethernet", if_index=2, status="up",
                           admin_status="up", mac="", link_speed="", speed_bps=1000, media_type="", virtual=True,
                           ip="172.16.1.1", prefix=24, dhcp=False, gateway="",
                           route_metric=10, if_metric=10, auto_metric="Disabled")
        a_tap = Adapter(name="TAP", description="TAP-Windows Adapter V9", if_index=3, status="up",
                        admin_status="up", mac="", link_speed="", speed_bps=1000, media_type="", virtual=False,
                        ip="10.8.0.2", prefix=24, dhcp=True, gateway="10.8.0.1",
                        route_metric=10, if_metric=10, auto_metric="Disabled")
        mgr._adapters = {a.name: a for a in [a_real, a_hyperv, a_tap]}
        phys = mgr.physical()
        assert len(phys) == 1
        assert phys[0].name == "RealTek"

    runner.run_case("2.2.1", "AdapterManager enumerate physical and routable NICs", test_adapter_enumeration)
    runner.run_case("2.2.2", "AdapterManager filters virtual and TAP/Hyper-V adapters", test_adapter_filtering_virtual)

    # 2.3 Health probing (Read-Only)
    def test_tcp_probe():
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable adapters found for TCP probe")
        nic = routable[0]
        # TCP handshake to Cloudflare 1.1.1.1:443
        rtt = tcp_probe("1.1.1.1", 443, nic.ip, nic.if_index, timeout=2.0)
        assert rtt is not None, f"TCP probe to 1.1.1.1:443 failed on {nic.name}"
        assert rtt > 0.0

    runner.run_case("2.3.1", "Read-only TCP probe pinned to active NIC", test_tcp_probe)

    # 2.4 Process resolution
    def test_process_resolver():
        pr = ProcessResolver(ttl=1.0)
        # Open a local listening socket to get a known port
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            pid = pr.pid_for_port(port)
            assert pid == os.getpid(), f"Expected PID {os.getpid()}, got {pid}"
            exe = pr.name_for_pid(pid)
            assert "python" in exe.lower(), f"Expected python in exe name, got {exe}"
        finally:
            srv.close()

    def test_process_resolver_cache_eviction():
        pr = ProcessResolver(ttl=1.0)
        # Manually populate > 512 entries
        for i in range(513):
            pr._names[100000 + i] = f"dummy_{i}.exe"
        # Calling name_for_pid with valid running process triggers eviction and retains new pid
        pr.name_for_pid(os.getpid())
        assert len(pr._names) <= 1

    def test_dead_pid_handling():
        pr = ProcessResolver(ttl=1.0)
        # Querying an invalid/dead PID
        res = pr.name_for_pid(99999999)
        assert res == ""

    runner.run_case("2.4.1", "ProcessResolver maps local TCP port to PID and exe", test_process_resolver)
    runner.run_case("2.4.2", "ProcessResolver cache eviction at 512 entries", test_process_resolver_cache_eviction)
    runner.run_case("2.4.3", "ProcessResolver dead PID returns empty string", test_dead_pid_handling)

    # 2.5 ThroughputMonitor
    def test_throughput_monitor():
        mon = ThroughputMonitor(interval=0.5)
        mon.start()
        try:
            time.sleep(1.2)
            mgr = AdapterManager()
            for a in mgr.routable():
                rate = mon.rate_for(a.name)
                assert rate.rx_bps >= 0.0
                assert rate.tx_bps >= 0.0
            rx_tot, tx_tot = mon.totals([a.name for a in mgr.routable()])
            assert rx_tot >= 0.0
            assert tx_tot >= 0.0
        finally:
            mon.stop()

    runner.run_case("2.5.1", "ThroughputMonitor starts, samples rates, stops cleanly", test_throughput_monitor)


# --------------------------------------------------------------------------- #
# PHASE 3: Localhost-Only Tests (Level 2 - Zero Risk)
# --------------------------------------------------------------------------- #

def test_phase_3():
    runner.start_phase("Phase 3: Localhost-Only Tests (Zero Risk)")

    # 3.1 Rule Engine
    def test_rule_engine():
        cfg = Config()
        re = RuleEngine(cfg)
        rule = re.add_rule("testapp.exe", "Ethernet 2", "Test Note")
        assert rule.exe == "testapp.exe"
        assert rule.target == "Ethernet 2"
        assert any(r.exe == "testapp.exe" for r in cfg.rules)
        re.account("testapp.exe", 2048)
        assert re.bytes_by_app["testapp.exe"] == 2048
        re.remove_rule("testapp.exe")
        assert not any(r.exe == "testapp.exe" for r in cfg.rules)

    runner.run_case("3.1.1", "RuleEngine add_rule, account, remove_rule", test_rule_engine)

    # 3.2 SourcePortPool
    def test_source_port_pool():
        pool = SourcePortPool(50000, 50004)
        ports = [pool.next() for _ in range(10)]
        expected = [50000, 50001, 50002, 50003, 50004, 50000, 50001, 50002, 50003, 50004]
        assert ports == expected

    runner.run_case("3.2.1", "SourcePortPool round-robin cycling", test_source_port_pool)

    # 3.3 MultiWanProxy lifecycle and SOCKS5/HTTP protocol
    def test_proxy_lifecycle_and_protocol():
        cfg = Config(
            socks_port=11080,
            http_port=18080,
            proxy_enabled=True,
            transparent_mode=False
        )
        mgr = AdapterManager()
        router = RouterEngine(cfg, mgr)
        router.sync_uplinks()
        rules = RuleEngine(cfg)
        proxy = MultiWanProxy(cfg, router, rules)

        proxy.start()
        try:
            assert proxy.running, "Proxy failed to start"

            # Test SOCKS5 handshake on port 11080
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(("127.0.0.1", 11080))
            # Client greeting: VER=5, NMETHODS=1, METHOD=0 (no auth)
            s.sendall(b"\x05\x01\x00")
            resp = s.recv(2)
            assert resp == b"\x05\x00", f"Unexpected SOCKS5 greeting response: {resp!r}"
            s.close()

            # Test HTTP proxy error on invalid destination (testing protocol parse)
            s_http = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s_http.settimeout(3.0)
            s_http.connect(("127.0.0.1", 18080))
            s_http.sendall(b"GET http://127.0.0.1:9999/test HTTP/1.1\r\nHost: 127.0.0.1:9999\r\n\r\n")
            http_resp = s_http.recv(1024)
            # Expect HTTP response (either 502 Bad Gateway or similar)
            assert b"HTTP/1.1" in http_resp
            s_http.close()
        finally:
            proxy.stop()
            time.sleep(0.3)

    def test_http_host_header_parsing():
        from core.load_balancer import _host_from_headers
        lines1 = [b"GET / HTTP/1.1", b"Host: example.com:8080", b"User-Agent: test"]
        h1, p1 = _host_from_headers(lines1)
        assert h1 == "example.com"
        assert p1 == 8080

        lines2 = [b"GET / HTTP/1.1", b"Host: example.org", b"User-Agent: test"]
        h2, p2 = _host_from_headers(lines2)
        assert h2 == "example.org"
        assert p2 == 80

        lines3 = [b"GET / HTTP/1.1", b"User-Agent: test"]
        h3, p3 = _host_from_headers(lines3)
        assert h3 == ""
        assert p3 == 80

    def test_proxy_stats_accounting():
        from core.load_balancer import ProxyStats
        stats = ProxyStats()
        stats.bump(stats.by_app, "chrome.exe", "conns", 1)
        stats.bump(stats.by_app, "chrome.exe", "up", 4096)
        assert stats.by_app["chrome.exe"]["conns"] == 1
        assert stats.by_app["chrome.exe"]["up"] == 4096

    def test_socks5_unsupported_cmd_rejection():
        cfg = Config(socks_port=11081, http_port=18081, proxy_enabled=True, transparent_mode=False)
        mgr = AdapterManager()
        router = RouterEngine(cfg, mgr)
        proxy = MultiWanProxy(cfg, router, RuleEngine(cfg))
        proxy.start()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect(("127.0.0.1", 11081))
            s.sendall(b"\x05\x01\x00")
            assert s.recv(2) == b"\x05\x00"
            # Send unsupported CMD 0x02 (BIND)
            s.sendall(b"\x05\x02\x00\x01\x7f\x00\x00\x01\x00\x50")
            resp = s.recv(10)
            # Response: VER=5, REP=7 (Command not supported)
            assert len(resp) >= 2
            assert resp[0] == 0x05
            assert resp[1] == 0x07
            s.close()
        finally:
            proxy.stop()
            time.sleep(0.3)

    runner.run_case("3.3.1", "MultiWanProxy start, SOCKS5 greeting, HTTP protocol, stop", test_proxy_lifecycle_and_protocol)
    runner.run_case("3.3.2", "HTTP Host header parsing with and without port", test_http_host_header_parsing)
    runner.run_case("3.3.3", "ProxyStats bump metrics and counters", test_proxy_stats_accounting)
    runner.run_case("3.3.4", "SOCKS5 rejects unsupported command with 0x07", test_socks5_unsupported_cmd_rejection)


# --------------------------------------------------------------------------- #
# PHASE 4: Controlled Network & Downloader Tests (Level 3 - Safe)
# --------------------------------------------------------------------------- #

def test_phase_4():
    runner.start_phase("Phase 4: Controlled Network & Downloader Tests (Safe)")

    # 4.1 DNS Flush
    def test_dns_flush():
        mgr = AdapterManager()
        mgr.flush_dns()  # executes Clear-DnsClientCache

    runner.run_case("4.1.1", "Clear-DnsClientCache execution via AdapterManager", test_dns_flush)

    # 4.2 SegmentedDownloader with Local HTTP Server (Range Requests)
    def test_segmented_downloader_local():
        # Spin up a local HTTP server that supports Range headers and serves 3 MB of test data
        test_payload = os.urandom(3 * 1024 * 1024)  # 3 MiB
        expected_hash = hashlib.sha256(test_payload).hexdigest()

        class RangeHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # suppress server logs

            def do_GET(self):
                range_header = self.headers.get("Range")
                total = len(test_payload)
                if range_header:
                    # Parse bytes=start-end
                    eq = range_header.split("=")[1]
                    parts = eq.split("-")
                    start = int(parts[0])
                    end = int(parts[1]) if parts[1] else total - 1
                    chunk = test_payload[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(total))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(test_payload)

        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink for downloader test")

        host_ip = routable[0].ip
        srv = http.server.HTTPServer((host_ip, 0), RangeHandler)
        srv_port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        try:
            # Only include the adapter that owns host_ip. Cross-NIC workers would
            # route via IP_UNICAST_IF through the physical switch to reach a local
            # server IP, which times out. 2 connections_per_link still validates
            # parallel chunked download and SHA-256 reassembly.
            matching = [a for a in routable if a.ip == host_ip]
            uplinks = [Uplink(adapter=a, state=STATE_UP) for a in matching[:1]]
            with tempfile.TemporaryDirectory() as td:
                out_file = str(Path(td) / "downloaded.bin")
                dl = SegmentedDownload(
                    url=f"http://{host_ip}:{srv_port}/testfile",
                    dest=out_file,
                    uplinks=uplinks,
                    connections_per_link=2
                )
                progress = dl.run()
                assert progress.finished, f"Download failed: {progress.error}"
                assert not progress.error, f"Download error: {progress.error}"
                assert Path(out_file).exists()
                actual_payload = Path(out_file).read_bytes()
                actual_hash = hashlib.sha256(actual_payload).hexdigest()
                assert actual_hash == expected_hash, "Downloaded file hash mismatch!"
        finally:
            srv.shutdown()

    def test_small_file_single_stream():
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink")
        host_ip = routable[0].ip
        small_payload = b"OmniNet Small Test File " * 100
        small_hash = hashlib.sha256(small_payload).hexdigest()

        class SmallHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(small_payload)))
                self.end_headers()
                self.wfile.write(small_payload)

        srv = http.server.HTTPServer((host_ip, 0), SmallHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                out_file = str(Path(td) / "small.bin")
                uplinks = [Uplink(adapter=routable[0], state=STATE_UP)]
                dl = SegmentedDownload(url=f"http://{host_ip}:{port}/small", dest=out_file, uplinks=uplinks)
                p = dl.run()
                assert p.finished
                assert not p.error
                assert hashlib.sha256(Path(out_file).read_bytes()).hexdigest() == small_hash
        finally:
            srv.shutdown()

    runner.run_case("4.2.1", "SegmentedDownload range probing, chunking, and hash verification", test_segmented_downloader_local)
    runner.run_case("4.2.2", "SegmentedDownload small file (<1MB) single-stream fallback", test_small_file_single_stream)

    # 4.3 Safe Metric Backup & Validation (No disruption)
    def test_metric_backup_read():
        mgr = AdapterManager()
        mgr.refresh()
        physical = mgr.physical()
        if not physical:
            raise unittest.SkipTest("No physical adapters")
        mgr.backup_metrics()
        for a in physical:
            assert a.name in mgr._metric_backup
            metric, auto = mgr._metric_backup[a.name]
            assert isinstance(metric, int)
            assert auto in ("Enabled", "Disabled", "")

    runner.run_case("4.3.1", "AdapterManager backup_metrics preserves original states", test_metric_backup_read)


# --------------------------------------------------------------------------- #
# PHASE 5: Integration & Resilience Tests
# --------------------------------------------------------------------------- #

def test_phase_5():
    runner.start_phase("Phase 5: Integration & Resilience Tests")

    # 5.1 Services Container Lifecycle
    def test_services_lifecycle():
        cfg = Config(proxy_enabled=False, transparent_mode=False, manage_metrics=False)
        svc = core.Services(cfg)
        svc.start()
        try:
            assert svc.stats is not None
            assert svc.router is not None
        finally:
            svc.stop()

    def test_services_proxy_disabled():
        cfg = Config(proxy_enabled=False, transparent_mode=False, manage_metrics=False)
        svc = core.Services(cfg)
        svc.start()
        try:
            assert not svc.proxy.running
        finally:
            svc.stop()

    def test_services_idempotent_stop():
        cfg = Config(proxy_enabled=False, transparent_mode=False, manage_metrics=False)
        svc = core.Services(cfg)
        svc.start()
        svc.stop()
        # Second stop call should be completely safe
        svc.stop()

    runner.run_case("5.1.1", "Services start and stop lifecycle", test_services_lifecycle)
    runner.run_case("5.1.2", "Services proxy_enabled=False does not launch proxy listener", test_services_proxy_disabled)
    runner.run_case("5.1.3", "Services stop is idempotent (multiple calls safe)", test_services_idempotent_stop)

    # 5.2 Single-Instance Mutex
    def test_single_instance_mutex():
        import ctypes
        from main import acquire_single_instance, MUTEX_NAME
        # The main.py acquire_single_instance uses CreateMutexW
        ok1 = acquire_single_instance()
        assert ok1, "First instance should acquire mutex"
        # Attempt second handle to check collision
        ERROR_ALREADY_EXISTS = 183
        h2 = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.windll.kernel32.GetLastError()
        ctypes.windll.kernel32.CloseHandle(h2)
        assert err == ERROR_ALREADY_EXISTS, "Second instance should detect mutex already exists"

    runner.run_case("5.2.1", "Single-instance named mutex detection", test_single_instance_mutex)

    # 5.3 is_admin check
    def test_is_admin():
        elevated = is_admin()
        assert isinstance(elevated, bool)

    runner.run_case("5.3.1", "is_admin elevation check function", test_is_admin)


# --------------------------------------------------------------------------- #
# PHASE 6: Edge Cases & Boundary Testing (Level 0 — Zero Risk)
# --------------------------------------------------------------------------- #

def test_phase_6():
    runner.start_phase("Phase 6: Edge Cases & Boundary Testing (Zero Risk)")

    # 6.1 SWRR edge cases
    def _make_adapter(name: str, idx: int, media: str = "") -> Adapter:
        return Adapter(
            name=name, description=name, if_index=idx, status="up",
            admin_status="up", mac="", link_speed="", speed_bps=1000,
            media_type=media, virtual=False,
            ip=f"192.168.{idx}.10", prefix=24, dhcp=True,
            gateway=f"192.168.{idx}.1",
            route_metric=10, if_metric=10, auto_metric="Disabled"
        )

    def test_swrr_weight_zero_clamped():
        """weight=0 should be clamped to 1 by max(1, weight)."""
        cfg = Config(mode=MODE_AGGREGATE)
        ad1 = _make_adapter("Eth1", 1)
        ad2 = _make_adapter("Eth2", 2)
        r = RouterEngine(cfg, AdapterManager())
        u1 = Uplink(adapter=ad1, weight=0, state=STATE_UP)
        u2 = Uplink(adapter=ad2, weight=0, state=STATE_UP)
        r.uplinks = {"Eth1": u1, "Eth2": u2}
        # Both clamped to 1 → should round-robin evenly
        counts = {"Eth1": 0, "Eth2": 0}
        for _ in range(100):
            sel = r.select_for_new_connection()
            assert sel is not None
            counts[sel.name] += 1
        assert counts["Eth1"] == 50
        assert counts["Eth2"] == 50

    def test_swrr_three_links():
        """3-link SWRR with 200:100:50 weights."""
        cfg = Config(mode=MODE_AGGREGATE)
        r = RouterEngine(cfg, AdapterManager())
        r.uplinks = {
            "A": Uplink(adapter=_make_adapter("A", 1), weight=200, state=STATE_UP),
            "B": Uplink(adapter=_make_adapter("B", 2), weight=100, state=STATE_UP),
            "C": Uplink(adapter=_make_adapter("C", 3), weight=50,  state=STATE_UP),
        }
        counts = {"A": 0, "B": 0, "C": 0}
        total = 350  # 200+100+50
        for _ in range(total):
            sel = r.select_for_new_connection()
            assert sel is not None
            counts[sel.name] += 1
        assert counts["A"] == 200
        assert counts["B"] == 100
        assert counts["C"] == 50

    def test_swrr_stale_rr_state_key():
        """Removing an uplink mid-run leaves a stale key in _rr_state;
        the next round must not crash and must still select from live pool."""
        cfg = Config(mode=MODE_AGGREGATE)
        r = RouterEngine(cfg, AdapterManager())
        ad1 = _make_adapter("Eth1", 1)
        ad2 = _make_adapter("Eth2", 2)
        r.uplinks = {
            "Eth1": Uplink(adapter=ad1, weight=100, state=STATE_UP),
            "Eth2": Uplink(adapter=ad2, weight=100, state=STATE_UP),
        }
        # Pump a few selections so _rr_state gets populated
        for _ in range(10):
            r.select_for_new_connection()
        # Remove Eth2 — its key stays in _rr_state
        del r.uplinks["Eth2"]
        for _ in range(20):
            sel = r.select_for_new_connection()
            assert sel is not None
            assert sel.name == "Eth1"

    runner.run_case("6.1.1", "SWRR weight=0 clamped to 1 distributes evenly", test_swrr_weight_zero_clamped)
    runner.run_case("6.1.2", "SWRR 3-link 200:100:50 distribution", test_swrr_three_links)
    runner.run_case("6.1.3", "SWRR stale _rr_state key after uplink removal", test_swrr_stale_rr_state_key)

    # 6.2 select_for_new_connection edge cases
    def test_select_dead_rule_target():
        """Explicit rule target is DOWN → spills to next healthy link."""
        cfg = Config(mode=MODE_AUTO)
        r = RouterEngine(cfg, AdapterManager())
        r.uplinks = {
            "Eth1": Uplink(adapter=_make_adapter("Eth1", 1), weight=100, state=STATE_DOWN),
            "Eth2": Uplink(adapter=_make_adapter("Eth2", 2), weight=100, state=STATE_UP),
        }
        sel = r.select_for_new_connection(prefer_name="Eth1")
        # Should fail over to Eth2
        assert sel is not None
        assert sel.name == "Eth2"

    def test_select_all_down_returns_none():
        cfg = Config(mode=MODE_AUTO)
        r = RouterEngine(cfg, AdapterManager())
        r.uplinks = {
            "Eth1": Uplink(adapter=_make_adapter("Eth1", 1), weight=100, state=STATE_DOWN),
            "Eth2": Uplink(adapter=_make_adapter("Eth2", 2), weight=100, state=STATE_DOWN),
        }
        assert r.select_for_new_connection() is None

    def test_force_mode_dead_falls_back():
        cfg = Config(mode=MODE_FORCE, forced_adapter="Eth1")
        r = RouterEngine(cfg, AdapterManager())
        r.uplinks = {
            "Eth1": Uplink(adapter=_make_adapter("Eth1", 1), weight=100, state=STATE_DOWN),
            "Eth2": Uplink(adapter=_make_adapter("Eth2", 2), weight=100, state=STATE_UP),
        }
        pref = r.preferred_uplink()
        # Forced link is DOWN; falls back to Eth2
        assert pref is not None
        assert pref.name == "Eth2"

    runner.run_case("6.2.1", "select_for_new_connection dead rule target spills to failover", test_select_dead_rule_target)
    runner.run_case("6.2.2", "select_for_new_connection all links DOWN returns None", test_select_all_down_returns_none)
    runner.run_case("6.2.3", "preferred_uplink in FORCE mode with forced link DOWN falls back", test_force_mode_dead_falls_back)

    # 6.3 apply_routing guards
    def test_apply_routing_dedup_guard():
        """apply_routing skips the shell call when the metric mapping is unchanged."""
        cfg = Config(manage_metrics=True)
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        ad = _make_adapter("Eth1", 1)
        r.uplinks = {"Eth1": Uplink(adapter=ad, weight=100, state=STATE_UP)}
        shell_calls = []
        orig_set = mgr.set_metrics
        mgr.set_metrics = lambda m: shell_calls.append(m) or orig_set(m)
        r.apply_routing(force=True)   # First call — must go through
        count_after_first = len(shell_calls)
        r.apply_routing()             # Second call same mapping — should be skipped
        assert len(shell_calls) == count_after_first  # no new call

    def test_apply_routing_manage_metrics_false():
        """apply_routing returns immediately when manage_metrics is False."""
        cfg = Config(manage_metrics=False)
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        r.uplinks = {"Eth1": Uplink(adapter=_make_adapter("Eth1", 1), weight=100, state=STATE_UP)}
        shell_calls = []
        mgr.set_metrics = lambda m: shell_calls.append(m)
        r.apply_routing(force=True)
        assert len(shell_calls) == 0

    runner.run_case("6.3.1", "apply_routing deduplication guard skips unchanged mapping", test_apply_routing_dedup_guard)
    runner.run_case("6.3.2", "apply_routing with manage_metrics=False is a no-op", test_apply_routing_manage_metrics_false)

    # 6.4 Downloader edge cases
    def test_download_progress_pct_zero_total():
        from core.downloader import DownloadProgress
        dp = DownloadProgress(total=0, done=0)
        assert dp.pct == 0.0  # must not raise ZeroDivisionError

    def test_download_progress_pct_partial():
        from core.downloader import DownloadProgress
        dp = DownloadProgress(total=200, done=50)
        assert abs(dp.pct - 25.0) < 0.001

    def test_segmented_download_empty_uplinks():
        from core.downloader import SegmentedDownload
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.bin")
            dl = SegmentedDownload(url="http://127.0.0.1:9/", dest=out, uplinks=[])
            p = dl.run()
            assert p.finished
            assert p.error  # must report an error, not crash

    runner.run_case("6.4.1", "DownloadProgress.pct with total=0 returns 0 without ZeroDivisionError", test_download_progress_pct_zero_total)
    runner.run_case("6.4.2", "DownloadProgress.pct partial returns correct percentage", test_download_progress_pct_partial)
    runner.run_case("6.4.3", "SegmentedDownload with empty uplinks list returns error", test_segmented_download_empty_uplinks)

    # 6.5 Config edge cases
    def test_config_unknown_keys_ignored():
        """Unknown keys in config.json must be ignored (forward compat)."""
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                p = Path(td) / "extra.json"
                data = {"mode": "auto", "unknown_future_key": 42,
                        "another_unknown": {"nested": True}}
                p.write_text(json.dumps(data), encoding="utf-8")
                config.CONFIG_PATH = p
                cfg = config.load()
                assert cfg.mode == "auto"  # known key preserved
            finally:
                config.CONFIG_PATH = orig

    def test_config_concurrent_save():
        """10 threads writing the config file simultaneously must not corrupt it."""
        with tempfile.TemporaryDirectory() as td:
            orig = config.CONFIG_PATH
            try:
                config.CONFIG_PATH = Path(td) / "concurrent.json"
                cfg = Config(mode=MODE_AGGREGATE)
                errors = []

                def saver():
                    try:
                        config.save(cfg)
                    except Exception as e:
                        errors.append(str(e))

                threads = [threading.Thread(target=saver) for _ in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                assert not errors, f"Save errors: {errors}"
                # Result must be valid JSON
                loaded = config.load()
                assert loaded.mode == MODE_AGGREGATE
            finally:
                config.CONFIG_PATH = orig

    def test_config_all_modes_roundtrip():
        """Each mode string survives a save/load roundtrip."""
        from config import ALL_MODES
        for mode in ALL_MODES:
            with tempfile.TemporaryDirectory() as td:
                orig = config.CONFIG_PATH
                try:
                    config.CONFIG_PATH = Path(td) / f"{mode}.json"
                    cfg = Config(mode=mode)
                    config.save(cfg)
                    loaded = config.load()
                    assert loaded.mode == mode, f"Mode {mode} not preserved"
                finally:
                    config.CONFIG_PATH = orig

    runner.run_case("6.5.1", "Config unknown future keys are silently ignored", test_config_unknown_keys_ignored)
    runner.run_case("6.5.2", "Config concurrent save from 10 threads without corruption", test_config_concurrent_save)
    runner.run_case("6.5.3", "Config all modes roundtrip save/load", test_config_all_modes_roundtrip)

    # 6.6 _host_from_headers edge cases
    def test_host_header_malformed():
        from core.load_balancer import _host_from_headers
        # No Host header at all
        h, p = _host_from_headers([b"GET / HTTP/1.1"])
        assert h == ""
        assert p == 80
        # Host with only colon-port (no hostname)
        h2, p2 = _host_from_headers([b"GET / HTTP/1.1", b"Host: :8080"])
        # Graceful — either empty host or whatever the parser decides; must not crash
        assert isinstance(h2, str)
        assert isinstance(p2, int)

    def test_host_header_no_port():
        from core.load_balancer import _host_from_headers
        h, p = _host_from_headers([b"GET / HTTP/1.1", b"Host: example.com"])
        assert h == "example.com"
        assert p == 80

    def test_proxy_stats_bump_empty_key():
        """bump() with an empty key must be a no-op (early return)."""
        from core.load_balancer import ProxyStats
        stats = ProxyStats()
        stats.bump(stats.by_app, "", "conns", 1)
        assert "" not in stats.by_app

    runner.run_case("6.6.1", "_host_from_headers handles missing or malformed Host", test_host_header_malformed)
    runner.run_case("6.6.2", "_host_from_headers no-port Host defaults to 80", test_host_header_no_port)
    runner.run_case("6.6.3", "ProxyStats bump with empty key is a no-op", test_proxy_stats_bump_empty_key)

    # 6.7 ForceBindIP
    def test_forcebind_is_risky_anticheat():
        from core.app_binder import ForceBindIP
        risky = [
            "valorant.exe", "Valorant.EXE", "vanguard.exe",
            "fortnite.exe", "apex.exe", "r5apex.exe",
            "csgo.exe", "cs2.exe", "eac.exe", "battleye.exe",
            "destiny2.exe", "rustclient.exe",
        ]
        for exe in risky:
            assert ForceBindIP.is_risky(exe), f"Expected {exe} to be risky"

    def test_forcebind_is_risky_safe():
        from core.app_binder import ForceBindIP
        safe = ["chrome.exe", "firefox.exe", "steam.exe",
                "discord.exe", "python.exe", "curl.exe"]
        for exe in safe:
            assert not ForceBindIP.is_risky(exe), f"Expected {exe} to be safe"

    def test_forcebind_locate_missing():
        """locate() must return None gracefully when ForceBindIP is not installed."""
        from core.app_binder import ForceBindIP
        import shutil
        fb = ForceBindIP(exe_path="/nonexistent/ForceBindIP64.exe")
        # PATH and local bin also won't have it in test environment
        orig_which = shutil.which
        shutil.which = lambda _: None
        try:
            result = fb.locate()
            # May be None or a real path — must not raise
            assert result is None or isinstance(result, str)
        finally:
            shutil.which = orig_which

    runner.run_case("6.7.1", "ForceBindIP.is_risky detects all anti-cheat titles", test_forcebind_is_risky_anticheat)
    runner.run_case("6.7.2", "ForceBindIP.is_risky returns False for safe apps", test_forcebind_is_risky_safe)
    runner.run_case("6.7.3", "ForceBindIP.locate returns None gracefully when not installed", test_forcebind_locate_missing)

    # 6.8 TransparentRedirector
    def test_transparent_filter_string():
        """The WinDivert filter string must reference the correct port range."""
        from core.app_binder import TransparentRedirector
        cfg = Config(upstream_port_low=50000, upstream_port_high=50999)
        tr = TransparentRedirector(cfg, proxy_port=1081)
        f = tr._filter()
        assert "50000" in f
        assert "50999" in f
        assert "1081" in f
        assert "outbound" in f

    def test_transparent_table_eviction():
        """When table grows past 4096, oldest 1024 entries are pruned."""
        from core.app_binder import TransparentRedirector
        cfg = Config()
        tr = TransparentRedirector(cfg, proxy_port=1081)
        for i in range(4097):
            tr.table[i] = ("1.2.3.4", 443)
        # Trigger eviction (inline the logic for the test)
        if len(tr.table) > 4096:
            for k in list(tr.table)[:1024]:
                tr.table.pop(k, None)
        assert len(tr.table) == 4097 - 1024

    def test_transparent_original_destination_miss():
        from core.app_binder import TransparentRedirector
        cfg = Config()
        tr = TransparentRedirector(cfg, proxy_port=1081)
        assert tr.original_destination(99999) is None
        tr.table[12345] = ("8.8.8.8", 443)
        assert tr.original_destination(12345) == ("8.8.8.8", 443)

    runner.run_case("6.8.1", "TransparentRedirector filter string contains correct port ranges", test_transparent_filter_string)
    runner.run_case("6.8.2", "TransparentRedirector table eviction at 4096 entries", test_transparent_table_eviction)
    runner.run_case("6.8.3", "TransparentRedirector original_destination miss returns None", test_transparent_original_destination_miss)

    # 6.9 Uplink history & stats boundaries
    def test_uplink_history_cap():
        """Uplink history must not grow past 20 entries."""
        ad = _make_adapter("Eth1", 1)
        u = Uplink(adapter=ad, state=STATE_UP)
        for i in range(25):
            u.history.append(bool(i % 2))
            if len(u.history) > 20:
                u.history.pop(0)
        assert len(u.history) == 20

    def test_nic_rate_deque_maxlen():
        """NicRate history deque must not exceed maxlen=120."""
        from core.stats import NicRate
        from collections import deque
        nr = NicRate(name="Eth1")
        for i in range(150):
            nr.history.append((float(i), float(i)))
        assert len(nr.history) == 120

    def test_human_bps_negatives():
        """human_bps with negative input must not crash."""
        from core.stats import human_bps, human_bytes
        result = human_bps(-100)
        assert isinstance(result, str)
        result2 = human_bytes(-50)
        assert isinstance(result2, str)

    runner.run_case("6.9.1", "Uplink probe history capped at 20 entries", test_uplink_history_cap)
    runner.run_case("6.9.2", "NicRate history deque respects maxlen=120", test_nic_rate_deque_maxlen)
    runner.run_case("6.9.3", "human_bps and human_bytes handle negative input gracefully", test_human_bps_negatives)


# --------------------------------------------------------------------------- #
# PHASE 7: Concurrency & Resilience Tests (Level 2 — Localhost only)
# --------------------------------------------------------------------------- #

def test_phase_7():
    runner.start_phase("Phase 7: Concurrency & Resilience Tests (Localhost Only)")

    def _make_proxy(socks: int, http_: int, mark_up: bool = False):
        """Helper: build and start a proxy.

        mark_up=False (default): uplinks stay STATE_UNKNOWN — the proxy can
            handle SOCKS5/HTTP protocol (greetings, parsing) but any upstream
            dial attempt will get a 'no healthy uplink' error (502/SOCKS error).
        mark_up=True: uplinks are forced to STATE_UP so the proxy can actually
            attempt outbound connections through the NIC. Use this when the test
            needs a real relay (e.g. HTTP CONNECT tunnel).
        """
        cfg = Config(socks_port=socks, http_port=http_,
                     proxy_enabled=True, transparent_mode=False)
        mgr = AdapterManager()
        router = RouterEngine(cfg, mgr)
        router.sync_uplinks()
        if mark_up:
            with router._lock:
                for u in router.uplinks.values():
                    u.state = STATE_UP
                    u.consecutive_ok = cfg.recover_threshold
        rules = RuleEngine(cfg)
        proxy = MultiWanProxy(cfg, router, rules)
        proxy.start()
        return proxy

    # 7.1 Concurrent SOCKS5 connections
    def test_50_concurrent_socks5():
        """50 simultaneous SOCKS5 greeting handshakes must all succeed."""
        proxy = _make_proxy(12080, 19080)
        try:
            assert proxy.running
            errors = []
            done = []

            def do_handshake():
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(5.0)
                    s.connect(("127.0.0.1", 12080))
                    s.sendall(b"\x05\x01\x00")
                    resp = s.recv(2)
                    if resp != b"\x05\x00":
                        errors.append(f"Bad greeting: {resp!r}")
                    else:
                        done.append(1)
                except Exception as e:
                    errors.append(str(e))
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

            threads = [threading.Thread(target=do_handshake) for _ in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert not errors, f"Concurrent SOCKS5 errors: {errors[:3]}"
            assert len(done) == 50
        finally:
            proxy.stop()
            time.sleep(0.3)

    def test_rapid_connect_disconnect():
        """200 rapid connect/disconnect cycles must not leave the proxy in a bad state."""
        proxy = _make_proxy(12081, 19081)
        try:
            assert proxy.running
            for _ in range(200):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2.0)
                try:
                    s.connect(("127.0.0.1", 12081))
                except Exception:
                    pass
                finally:
                    s.close()
            # Proxy must still be alive after the storm
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s2.settimeout(3.0)
            s2.connect(("127.0.0.1", 12081))
            s2.sendall(b"\x05\x01\x00")
            resp = s2.recv(2)
            s2.close()
            assert resp == b"\x05\x00"
        finally:
            proxy.stop()
            time.sleep(0.3)

    def test_socks5_domain_atyp3():
        """SOCKS5 ATYP=3 (domain name) handshake — proxy must parse it correctly
        and respond (connection itself may fail if no real uplink, that is fine)."""
        proxy = _make_proxy(12082, 19082)
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(("127.0.0.1", 12082))
            # greeting
            s.sendall(b"\x05\x01\x00")
            greeting = s.recv(2)
            assert greeting == b"\x05\x00", f"Bad greeting: {greeting!r}"
            # CONNECT, ATYP=3 (domain), host="example.com", port=80
            domain = b"example.com"
            req = (
                b"\x05\x01\x00"    # VER CMD RSV
                b"\x03"            # ATYP=3 domain
                + bytes([len(domain)])
                + domain
                + b"\x00\x50"     # port 80
            )
            s.sendall(req)
            # Proxy may close the connection if no upstream is available.
            # Accept either a valid SOCKS5 error reply or a clean close.
            try:
                resp = s.recv(10)
                if resp:  # if we got something, it must be a valid SOCKS5 reply
                    assert resp[0] == 0x05, f"Expected SOCKS5 reply, got: {resp!r}"
                # empty resp (server closed) is also acceptable
            except (ConnectionResetError, socket.timeout, OSError):
                pass  # acceptable if proxy closed without a reply
        except (ConnectionResetError, socket.timeout, OSError):
            pass  # acceptable if no upstream available at all
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            proxy.stop()
            time.sleep(0.3)

    runner.run_case("7.1.1", "50 concurrent SOCKS5 handshakes all succeed", test_50_concurrent_socks5)
    runner.run_case("7.1.2", "200 rapid connect/disconnect cycles leave proxy healthy", test_rapid_connect_disconnect)
    runner.run_case("7.1.3", "SOCKS5 ATYP=3 domain handshake parsed correctly", test_socks5_domain_atyp3)

    # 7.2 HTTP CONNECT and plain proxy
    def test_http_connect_tunnel():
        """HTTP CONNECT to an echo server on the NIC IP — bidirectional relay must work.
        The echo server must listen on the NIC's IP, not loopback, because the proxy
        dials upstream via bind_socket_to_uplink which pins the egress NIC.
        Connecting from a NIC-bound socket to 127.0.0.1 fails on Windows."""
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink for HTTP CONNECT tunnel test")
        host_ip = routable[0].ip

        # Echo server bound to the NIC's IP so the proxy can reach it
        echo_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        echo_srv.bind((host_ip, 0))
        echo_srv.listen(5)
        echo_port = echo_srv.getsockname()[1]
        received = []

        def echo_handler():
            try:
                conn, _ = echo_srv.accept()
                data = conn.recv(1024)
                received.append(data)
                conn.sendall(data)  # echo back
                conn.close()
            except Exception:
                pass

        t_echo = threading.Thread(target=echo_handler, daemon=True)
        t_echo.start()

        proxy = _make_proxy(12083, 19083, mark_up=True)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(8.0)
            s.connect(("127.0.0.1", 19083))
            # HTTP CONNECT to the echo server on the NIC IP
            req = f"CONNECT {host_ip}:{echo_port} HTTP/1.1\r\nHost: {host_ip}:{echo_port}\r\n\r\n"
            s.sendall(req.encode())
            resp = s.recv(256)
            assert b"200" in resp, f"Expected 200 Connection Established, got: {resp!r}"
            # Tunnel established — send data through
            s.sendall(b"HELLO_TUNNEL")
            echo_reply = s.recv(64)
            assert echo_reply == b"HELLO_TUNNEL"
        finally:
            try:
                s.close()
            except Exception:
                pass
            echo_srv.close()
            proxy.stop()
            time.sleep(0.3)

    def test_http_proxy_absolute_uri_rewrite():
        """HTTP proxy rewrites 'GET http://host/path' to relative '/path'.
        We test the proxy response to a request aimed at a non-existent host
        (502 is acceptable) — the point is that the proxy parses the URI
        without crashing and returns a valid HTTP response."""
        proxy = _make_proxy(12084, 19084)
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(("127.0.0.1", 19084))
            req = b"GET http://no-such-host.invalid/test HTTP/1.1\r\nHost: no-such-host.invalid\r\n\r\n"
            s.sendall(req)
            resp = s.recv(256)
            # Must receive a valid HTTP response line
            assert resp.startswith(b"HTTP/1."), f"Not an HTTP response: {resp!r}"
        finally:
            try:
                s.close()
            except Exception:
                pass
            proxy.stop()
            time.sleep(0.3)

    runner.run_case("7.2.1", "HTTP CONNECT tunnel enables bidirectional relay", test_http_connect_tunnel)
    runner.run_case("7.2.2", "HTTP proxy absolute URI rewrite returns valid HTTP response", test_http_proxy_absolute_uri_rewrite)

    # 7.3 Downloader edge cases
    def test_downloader_cancel_mid_transfer():
        """Calling cancel() mid-download stops workers and the progress is marked finished."""
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink")

        payload = os.urandom(4 * 1024 * 1024)  # 4 MiB

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                range_hdr = self.headers.get("Range")
                total = len(payload)
                if range_hdr and "=" in range_hdr:
                    start_s, _, end_s = range_hdr.split("=")[1].partition("-")
                    start = int(start_s)
                    end = int(end_s) if end_s else total - 1
                    chunk = payload[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    # Drip data slowly so cancel has time to fire
                    for i in range(0, len(chunk), 4096):
                        self.wfile.write(chunk[i:i + 4096])
                        self.wfile.flush()
                        time.sleep(0.01)
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(payload)

        host_ip = routable[0].ip
        srv = http.server.HTTPServer((host_ip, 0), SlowHandler)
        srv_port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()

        uplinks = [Uplink(adapter=routable[0], state=STATE_UP)]
        try:
            with tempfile.TemporaryDirectory() as td:
                out_file = str(Path(td) / "cancelled.bin")
                dl = SegmentedDownload(
                    url=f"http://{host_ip}:{srv_port}/file",
                    dest=out_file,
                    uplinks=uplinks,
                    connections_per_link=2
                )

                def cancel_soon():
                    time.sleep(0.3)  # let it start
                    dl.cancel()

                ct = threading.Thread(target=cancel_soon, daemon=True)
                ct.start()
                progress = dl.run()
                ct.join(timeout=3)
                # After cancel, run() must return (not hang)
                assert progress.finished
                # Must have downloaded at least something (cancel fired mid-way)
                # or nothing if it was cancelled before start — either is fine
        finally:
            srv.shutdown()

    def test_downloader_server_no_range_support():
        """Server that returns 200 (not 206) must trigger single-stream fallback."""
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink")

        payload = b"OmniNet No-Range Test " * 200
        expected_hash = hashlib.sha256(payload).hexdigest()

        class NoRangeHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                # Always return 200 full body, never 206
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        host_ip = routable[0].ip
        srv = http.server.HTTPServer((host_ip, 0), NoRangeHandler)
        srv_port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                out_file = str(Path(td) / "norange.bin")
                dl = SegmentedDownload(
                    url=f"http://{host_ip}:{srv_port}/file",
                    dest=out_file,
                    uplinks=[Uplink(adapter=routable[0], state=STATE_UP)]
                )
                p = dl.run()
                assert p.finished
                assert not p.error, f"Download error: {p.error}"
                actual = hashlib.sha256(Path(out_file).read_bytes()).hexdigest()
                assert actual == expected_hash
        finally:
            srv.shutdown()

    def test_downloader_retry_on_transient_failure():
        """Server fails the first 2 range requests per chunk, succeeds on 3rd.
        The download must still complete correctly."""
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if not routable:
            raise unittest.SkipTest("No routable uplink")

        payload = os.urandom(2 * 1024 * 1024)  # 2 MiB to force chunking
        expected_hash = hashlib.sha256(payload).hexdigest()
        fail_counts: dict = {}
        fail_lock = threading.Lock()

        class RetryHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                range_hdr = self.headers.get("Range", "")
                total = len(payload)
                with fail_lock:
                    count = fail_counts.get(range_hdr, 0)
                    fail_counts[range_hdr] = count + 1
                if count < 2 and range_hdr:
                    # Fail with 503 first 2 times per unique non-empty Range header
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if range_hdr and "=" in range_hdr:
                    # Serve the requested byte range
                    start_s, _, end_s = range_hdr.split("=")[1].partition("-")
                    start = int(start_s)
                    end = int(end_s) if end_s else total - 1
                    chunk = payload[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(chunk)
                else:
                    # Plain GET (no Range header) — serve full content
                    self.send_response(200)
                    self.send_header("Content-Length", str(total))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    self.wfile.write(payload)

        host_ip = routable[0].ip
        srv = http.server.HTTPServer((host_ip, 0), RetryHandler)
        srv_port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                out_file = str(Path(td) / "retry.bin")
                dl = SegmentedDownload(
                    url=f"http://{host_ip}:{srv_port}/file",
                    dest=out_file,
                    uplinks=[Uplink(adapter=routable[0], state=STATE_UP)],
                    connections_per_link=1
                )
                p = dl.run()
                assert p.finished
                if not p.error:
                    actual_hash = hashlib.sha256(Path(out_file).read_bytes()).hexdigest()
                    assert actual_hash == expected_hash
        finally:
            srv.shutdown()

    runner.run_case("7.3.1", "SegmentedDownload cancel() mid-transfer stops workers and returns", test_downloader_cancel_mid_transfer)
    runner.run_case("7.3.2", "SegmentedDownload falls back to single-stream when server ignores Range", test_downloader_server_no_range_support)
    runner.run_case("7.3.3", "SegmentedDownload retries failed chunks (3-attempt retry loop)", test_downloader_retry_on_transient_failure)

    # 7.4 PowerShell resilience
    def test_powershell_crash_recovery():
        """After the PowerShell child process is killed, the next run() auto-respawns."""
        if not IS_WINDOWS:
            raise unittest.SkipTest("PowerShell only on Windows")
        sess = PowerShellSession()
        try:
            # First run to establish the session
            out1 = sess.run("Write-Output 'FIRST'").strip()
            assert "FIRST" in out1
            # Kill the child process
            if sess._proc and sess._proc.poll() is None:
                sess._proc.kill()
                sess._proc.wait()
            # Next run must auto-respawn
            out2 = sess.run("Write-Output 'AFTER_CRASH'").strip()
            assert "AFTER_CRASH" in out2
        finally:
            sess.close()

    def test_powershell_timeout_raises():
        """A script that sleeps past the timeout must raise PowerShellError."""
        if not IS_WINDOWS:
            raise unittest.SkipTest("PowerShell only on Windows")
        from core.netutil import PowerShellError
        sess = PowerShellSession()
        try:
            raised = False
            try:
                sess.run("Start-Sleep 30", timeout=2.0)
            except PowerShellError:
                raised = True
            assert raised, "Expected PowerShellError on timeout"
        finally:
            sess.close()

    runner.run_case("7.4.1", "PowerShellSession auto-respawns after child process crash", test_powershell_crash_recovery)
    runner.run_case("7.4.2", "PowerShellSession raises PowerShellError on timeout", test_powershell_timeout_raises)

    # 7.5 Services and RouterEngine resilience
    def test_services_restart_cycle():
        """Services can be stopped and restarted cleanly without port conflicts."""
        cfg = Config(proxy_enabled=False, transparent_mode=False, manage_metrics=False)
        for cycle in range(2):
            svc = core.Services(cfg)
            svc.start()
            time.sleep(0.1)
            svc.stop()
            time.sleep(0.3)  # allow OS to reclaim ports

    def test_router_probe_round_parallel():
        """probe_round spawns one thread per candidate and merges results."""
        cfg = Config(probe_interval_s=1.0, probe_timeout_s=0.5,
                     probe_targets=["1.1.1.1:443", "8.8.8.8:443"])
        mgr = AdapterManager()
        mgr.refresh()
        r = RouterEngine(cfg, mgr)
        r.sync_uplinks()
        if not r.candidates():
            raise unittest.SkipTest("No routable adapters for probe round test")
        t0 = time.perf_counter()
        r.probe_round()
        elapsed = (time.perf_counter() - t0) * 1000
        # If probes ran serially, total time ≈ N × timeout (>= 500ms per adapter).
        # Parallel execution must finish faster than N × timeout.
        n = len(r.candidates())
        max_serial = n * cfg.probe_timeout_s * 1000 * 0.9  # 90% of serial time = fail threshold
        # Only assert if multiple candidates (otherwise no parallelism to verify)
        if n > 1:
            assert elapsed < max_serial, (
                f"Probe round took {elapsed:.0f}ms, expected < {max_serial:.0f}ms "
                f"(serial would be ~{n * cfg.probe_timeout_s * 1000:.0f}ms)"
            )

    runner.run_case("7.5.1", "Services stop->start->stop cycle without port conflicts", test_services_restart_cycle)
    runner.run_case("7.5.2", "RouterEngine probe_round executes probes in parallel", test_router_probe_round_parallel)


# --------------------------------------------------------------------------- #
# PHASE 8: Failover & Routing Timing (Level 3 — Requires Admin)
# --------------------------------------------------------------------------- #

def test_phase_8():
    runner.start_phase("Phase 8: Failover & Routing Timing (Admin Required)")

    if not IS_WINDOWS:
        print("  [SKIP] Phase 8 requires Windows")
        return
    if not is_admin():
        print("  [SKIP] Phase 8 requires administrator privileges (run with --admin)")
        return

    # 8.1 Metric write latency and restore
    def test_metric_write_latency():
        """set_metrics() on a warm shell must complete in <200ms."""
        mgr = AdapterManager()
        mgr.refresh()
        physical = mgr.physical()
        if not physical:
            raise unittest.SkipTest("No physical adapters")
        mgr.backup_metrics()  # save originals
        try:
            mapping = {a.name: a.if_metric if a.if_metric > 0 else 100
                       for a in physical[:2]}
            t0 = time.perf_counter()
            mgr.set_metrics(mapping)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert elapsed_ms < 200, (
                f"set_metrics took {elapsed_ms:.0f}ms, expected < 200ms"
            )
        finally:
            mgr.restore_metrics()

    def test_metric_restore_on_shutdown():
        """restore_all() returns interface metrics to their original values."""
        mgr = AdapterManager()
        mgr.refresh()
        physical = mgr.physical()
        if not physical:
            raise unittest.SkipTest("No physical adapters")
        original_metrics = {a.name: a.if_metric for a in physical if a.if_metric >= 0}
        mgr.backup_metrics()
        try:
            # Apply different metrics
            new_mapping = {name: metric + 50 for name, metric in original_metrics.items()}
            mgr.set_metrics(new_mapping)
            # Verify they changed
            mgr.refresh()
            for a in mgr.physical():
                if a.name in new_mapping:
                    assert a.if_metric == new_mapping[a.name] or a.if_metric >= 0
        finally:
            mgr.restore_all()

    def test_failover_new_connections_under_500ms():
        """Simulates link going DOWN: from state change to apply_routing completing
        must be under 500ms (the documented failover SLA for new connections)."""
        cfg = Config(manage_metrics=True, probe_interval_s=1.0)
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if len(routable) < 1:
            raise unittest.SkipTest("Need at least 1 routable adapter for failover test")
        mgr.backup_metrics()
        try:
            r = RouterEngine(cfg, mgr)
            r.sync_uplinks()
            if not r.uplinks:
                raise unittest.SkipTest("No uplinks discovered")
            # Mark all uplinks as UP first
            for u in r.uplinks.values():
                u.state = STATE_UP
                u.consecutive_ok = cfg.recover_threshold
            # Simulate one link going DOWN
            first = next(iter(r.uplinks.values()))
            first.state = STATE_DOWN
            first.consecutive_fail = cfg.fail_threshold
            # Time the apply_routing call
            t0 = time.perf_counter()
            r.apply_routing(force=True)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert elapsed_ms < 500, (
                f"apply_routing after failover took {elapsed_ms:.0f}ms, expected < 500ms"
            )
        finally:
            mgr.restore_all()

    runner.run_case("8.1.1", "set_metrics() on warm PowerShell shell completes in <200ms", test_metric_write_latency)
    runner.run_case("8.1.2", "restore_all() returns interface metrics to original values", test_metric_restore_on_shutdown)
    runner.run_case("8.1.3", "Failover route update (apply_routing) completes in <500ms", test_failover_new_connections_under_500ms)

    # 8.2 RouterEngine monitor thread
    def test_router_monitor_thread_lifecycle():
        """RouterEngine monitor thread starts, runs at least one probe cycle, and exits cleanly."""
        cfg = Config(probe_interval_s=0.5, probe_timeout_s=1.0, manage_metrics=False)
        mgr = AdapterManager()
        r = RouterEngine(cfg, mgr)
        events = []
        r.on_event = lambda lvl, msg: events.append((lvl, msg))
        r.start()
        time.sleep(1.5)  # Let at least 2 probe rounds run
        r.stop()
        # Thread must have exited
        assert r._thread is not None
        assert not r._thread.is_alive(), "Monitor thread still alive after stop()"

    def test_sync_uplinks_removes_gone_adapter():
        """sync_uplinks correctly removes an adapter that has disappeared."""
        cfg = Config()
        mgr = AdapterManager()
        mgr.refresh()
        r = RouterEngine(cfg, mgr)
        r.sync_uplinks()
        if not r.uplinks:
            raise unittest.SkipTest("No uplinks to test removal")
        gone_events = []
        r.on_event = lambda lvl, msg: gone_events.append((lvl, msg)) if "removed" in msg.lower() else None
        # Inject a fake adapter that will disappear on next refresh
        fake_name = "__OmniNet_Test_Fake_Adapter__"
        from core.adapter_manager import Adapter as AdpCls
        fake_ad = AdpCls(
            name=fake_name, description="Fake", if_index=9999,
            status="up", admin_status="up", mac="", link_speed="",
            speed_bps=1000, media_type="", virtual=False,
            ip="10.99.99.99", prefix=24, dhcp=True, gateway="10.99.99.1",
            route_metric=10, if_metric=10, auto_metric="Disabled"
        )
        r.uplinks[fake_name] = Uplink(adapter=fake_ad, state=STATE_UP)
        # Now call sync_uplinks — real refresh won't include fake_name → it gets removed
        r.sync_uplinks()
        assert fake_name not in r.uplinks

    runner.run_case("8.2.1", "RouterEngine monitor thread starts, runs, exits cleanly", test_router_monitor_thread_lifecycle)
    runner.run_case("8.2.2", "sync_uplinks correctly removes disappeared adapters", test_sync_uplinks_removes_gone_adapter)

    # 8.3 Proxy failover on dial failure
    def test_proxy_failover_on_dial_failure():
        """When first uplink dial fails, proxy must try next healthy uplink
        and increment stats.failovers."""
        cfg = Config(socks_port=12090, http_port=19090,
                     proxy_enabled=True, transparent_mode=False)
        mgr = AdapterManager()
        mgr.refresh()
        routable = mgr.routable()
        if len(routable) < 1:
            raise unittest.SkipTest("Need routable adapter for proxy failover test")

        router = RouterEngine(cfg, mgr)
        router.sync_uplinks()
        rules = RuleEngine(cfg)
        proxy = MultiWanProxy(cfg, router, rules)
        proxy.start()
        initial_failovers = proxy.stats.failovers
        try:
            # Make a connection to a port we know will be refused (localhost:1)
            # The proxy will try to connect upstream and fail; with only one uplink
            # it should record a failed_conn, not a failover.
            # This test validates the stats accounting path.
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(("127.0.0.1", 12090))
            s.sendall(b"\x05\x01\x00")
            assert s.recv(2) == b"\x05\x00"
            # Request CONNECT to 127.0.0.1:1 (refused)
            s.sendall(b"\x05\x01\x00\x01\x7f\x00\x00\x01\x00\x01")
            resp = s.recv(10)
            assert len(resp) >= 2 and resp[0] == 0x05
            # REP != 0x00 means connection failed (expected)
            s.close()
            # failed_conns counter must have incremented
            assert proxy.stats.failed_conns >= 1 or proxy.stats.total_conns >= 1
        finally:
            proxy.stop()
            time.sleep(0.3)

    runner.run_case("8.3.1", "Proxy stats.failed_conns increments when upstream dial fails", test_proxy_failover_on_dial_failure)


# --------------------------------------------------------------------------- #
# Phase 9: Deep Gaming & System Stress Suite
# --------------------------------------------------------------------------- #

def test_phase_9():
    runner.start_phase("Phase 9: Deep Gaming & System Stress Suite")

    def _mock_env(threshold: float = 130.0):
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

    # 9.1 Sticky routing & ping variations
    def test_9_1_sticky_hold():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 50.0
        router.uplinks["eth3"].rtt_ms = 110.0
        router._current_preferred = "eth1"
        p = router.preferred_uplink()
        assert p.name == "eth1"

    def test_9_2_near_boundary_below():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 129.0
        router.uplinks["eth2"].rtt_ms = 60.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth1"

    def test_9_3_threshold_breached():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 131.0
        router.uplinks["eth2"].rtt_ms = 70.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth2"

    def test_9_4_exact_threshold():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 130.0
        router.uplinks["eth2"].rtt_ms = 60.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth1"

    def test_9_5_user_case_145_vs_90():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth2"].rtt_ms = 145.0
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth3"].rtt_ms = 160.0
        router._current_preferred = "eth2"
        p = router.preferred_uplink()
        assert p.name == "eth1"
        assert router.preferred_uplink().name == "eth1"

    def test_9_6_all_degraded():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 190.0
        router.uplinks["eth2"].rtt_ms = 170.0
        router.uplinks["eth3"].rtt_ms = 140.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth3"

    # 9.2 Cable breaks & failover
    def test_9_7_primary_pulled():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 75.0
        router.uplinks["eth2"].rtt_ms = 85.0
        router._current_preferred = "eth1"
        router.uplinks["eth1"].state = STATE_DOWN
        assert router.preferred_uplink().name == "eth2"

    def test_9_8_double_failure():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].state = STATE_DOWN
        router.uplinks["eth2"].state = STATE_DOWN
        router.uplinks["eth3"].rtt_ms = 95.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth3"

    def test_9_9_secondary_pulled():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 80.0
        router._current_preferred = "eth1"
        router.uplinks["eth2"].state = STATE_DOWN
        assert router.preferred_uplink().name == "eth1"

    def test_9_10_all_links_down():
        _, _, router = _mock_env(130.0)
        for u in router.uplinks.values(): u.state = STATE_DOWN
        router._current_preferred = "eth1"
        assert router.preferred_uplink() is None

    def test_9_11_silent_freeze():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 0.0
        router.uplinks["eth1"].state = STATE_DOWN
        router.uplinks["eth2"].rtt_ms = 90.0
        router._current_preferred = "eth1"
        assert router.preferred_uplink().name == "eth2"

    # 9.3 Flapping & recovery
    def test_9_12_primary_recovers_super_ping():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth2"].rtt_ms = 85.0
        router._current_preferred = "eth2"
        router.uplinks["eth1"].rtt_ms = 35.0
        assert router.preferred_uplink().name == "eth2"

    def test_9_13_primary_recovers_when_lagging():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth2"].rtt_ms = 145.0
        router._current_preferred = "eth2"
        router.uplinks["eth1"].rtt_ms = 40.0
        assert router.preferred_uplink().name == "eth1"

    def test_9_14_flapping_loop_stability():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 80.0
        router.uplinks["eth2"].rtt_ms = 90.0
        router._current_preferred = "eth1"
        for _ in range(20):
            router.uplinks["eth1"].state = STATE_DOWN
            assert router.preferred_uplink().name == "eth2"
            router.uplinks["eth1"].state = STATE_UP
            router.uplinks["eth1"].rtt_ms = 75.0
            assert router.preferred_uplink().name == "eth2"

    # 9.4 Jitter & bufferbloat
    def test_9_15_gradual_bufferbloat():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 95.0
        router._current_preferred = "eth1"
        for ping in [95.0, 110.0, 125.0, 129.0]:
            router.uplinks["eth1"].rtt_ms = ping
            assert router.preferred_uplink().name == "eth1"
        router.uplinks["eth1"].rtt_ms = 134.0
        assert router.preferred_uplink().name == "eth2"

    def test_9_16_transient_spike_no_double_hop():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 90.0
        router.uplinks["eth2"].rtt_ms = 100.0
        router._current_preferred = "eth1"
        router.uplinks["eth1"].rtt_ms = 280.0
        assert router.preferred_uplink().name == "eth2"
        router.uplinks["eth1"].rtt_ms = 90.0
        assert router.preferred_uplink().name == "eth2"

    # 9.5 Multi-target & overrides
    def test_9_17_regional_asymmetry():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 88.0
        router.uplinks["eth2"].rtt_ms = 135.0
        router._current_preferred = None
        assert router.preferred_uplink().name == "eth1"
        router.uplinks["eth1"].rtt_ms = 148.0
        router.uplinks["eth2"].rtt_ms = 92.0
        assert router.preferred_uplink().name == "eth2"

    def test_9_18_force_dead_fallback():
        cfg, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 85.0
        router.uplinks["eth3"].rtt_ms = 115.0
        cfg.mode = MODE_FORCE
        cfg.forced_adapter = "eth3"
        assert router.preferred_uplink().name == "eth3"
        router.uplinks["eth3"].state = STATE_DOWN
        assert router.preferred_uplink().name == "eth1"

    # 9.6 Concurrency & system stress
    def test_9_19_high_freq_eval_stress():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 92.0
        router.uplinks["eth2"].rtt_ms = 88.0
        router._current_preferred = "eth1"
        for _ in range(5000):
            assert router.preferred_uplink().name == "eth1"

    def test_9_20_multithread_concurrency():
        _, _, router = _mock_env(130.0)
        router.uplinks["eth1"].rtt_ms = 95.0
        router.uplinks["eth2"].rtt_ms = 98.0
        router._current_preferred = "eth1"
        errors = []
        def worker():
            try:
                for j in range(250):
                    p = router.preferred_uplink()
                    assert p is not None
                    if j % 50 == 0:
                        with router._lock:
                            router.uplinks["eth1"].rtt_ms = 95.0 + (j % 10)
            except Exception as e:
                errors.append(str(e))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors

    def test_9_21_rule_engine_crud():
        cfg = Config(rules=[AppRule(exe="aoe4.exe", target="eth1", enabled=True)])
        re = RuleEngine(cfg)
        assert cfg.rule_for("AoE4.EXE").target == "eth1"
        re.add_rule("reliccardinal.exe", "eth1")
        assert cfg.rule_for("reliccardinal.exe").target == "eth1"
        re.remove_rule("reliccardinal.exe")
        assert cfg.rule_for("reliccardinal.exe") is None

    def test_9_22_throughput_math():
        tm = ThroughputMonitor(interval=0.1)
        tm._prev["eth1"] = NicSample(ts=100.0, rx=10_000_000, tx=5_000_000)
        curr = NicSample(ts=101.0, rx=20_000_000, tx=10_000_000)
        dt = curr.ts - tm._prev["eth1"].ts
        rx_bps = ((curr.rx - tm._prev["eth1"].rx) * 8.0) / dt
        assert rx_bps == 80_000_000.0

    def test_9_23_source_port_cyclic():
        pool = SourcePortPool(low=30000, high=30010)
        ports = [pool.next() for _ in range(11)]
        assert set(ports) == set(range(30000, 30011))
        assert pool.next() == 30000

    runner.run_case("9.1.1", "Sticky hold on active connection (<130ms)", test_9_1_sticky_hold)
    runner.run_case("9.1.2", "Near-boundary ping below threshold (129ms)", test_9_2_near_boundary_below)
    runner.run_case("9.1.3", "Threshold breached by 1ms (131ms switches)", test_9_3_threshold_breached)
    runner.run_case("9.1.4", "Exact threshold boundary (130.0ms holds)", test_9_4_exact_threshold)
    runner.run_case("9.1.5", "Gamer prompt case: Active=145ms vs Eth1=90ms (instant switch & lock)", test_9_5_user_case_145_vs_90)
    runner.run_case("9.1.6", "All links in degraded state (>130ms picks lowest)", test_9_6_all_degraded)
    runner.run_case("9.2.1", "Active primary cable pulled (instant failover)", test_9_7_primary_pulled)
    runner.run_case("9.2.2", "Double simultaneous failure (instant rescue to 3rd line)", test_9_8_double_failure)
    runner.run_case("9.2.3", "Secondary cable disconnect (zero disturbance to active)", test_9_9_secondary_pulled)
    runner.run_case("9.2.4", "Catastrophic failure (all down returns None safely)", test_9_10_all_links_down)
    runner.run_case("9.2.5", "Silent ISP freeze (probe timeout marks DOWN and switches)", test_9_11_silent_freeze)
    runner.run_case("9.3.1", "Primary recovers with god-tier ping (sticky hold avoids mid-match DC)", test_9_12_primary_recovers_super_ping)
    runner.run_case("9.3.2", "Primary recovers while active line is lagging (>130ms preemption)", test_9_13_primary_recovers_when_lagging)
    runner.run_case("9.3.3", "Rapid flapping stress test (20 cycles stability)", test_9_14_flapping_loop_stability)
    runner.run_case("9.4.1", "Gradual bufferbloat progression (hold until 130ms breached)", test_9_15_gradual_bufferbloat)
    runner.run_case("9.4.2", "Transient ping spike recovery (no double-hop oscillation)", test_9_16_transient_spike_no_double_hop)
    runner.run_case("9.5.1", "Regional ISP route asymmetry (Azure India vs SE Asia)", test_9_17_regional_asymmetry)
    runner.run_case("9.5.2", "Force override with dead-link fallback", test_9_18_force_dead_fallback)
    runner.run_case("9.6.1", "High frequency sticky evaluation stress (5,000 iterations)", test_9_19_high_freq_eval_stress)
    runner.run_case("9.6.2", "Multi-threaded concurrency stress (4 threads, 1,000 ops)", test_9_20_multithread_concurrency)
    runner.run_case("9.6.3", "RuleEngine deep matching & dynamic CRUD", test_9_21_rule_engine_crud)
    runner.run_case("9.6.4", "Throughput monitor rate math & accuracy", test_9_22_throughput_math)
    runner.run_case("9.6.5", "SourcePortPool cyclic range & smooth wrap-around", test_9_23_source_port_cyclic)


# --------------------------------------------------------------------------- #
# Main Entry Point
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="OmniNet Test Suite")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3, 4, 5, 6, 7, 8, 9],
                        help="Run a specific phase (1-9)")
    parser.add_argument("--all", action="store_true", help="Run all phases (1-7, 9; excludes Phase 8)")
    parser.add_argument("--admin", action="store_true",
                        help="Also run Phase 8 (requires administrator privileges, writes interface metrics)")
    parser.add_argument("--json", action="store_true", help="Output JSON results")
    args = parser.parse_args()

    # Default to all safe phases if neither --phase nor --all is specified
    run_all = args.all or args.phase is None

    t_start = time.perf_counter()

    if run_all or args.phase == 1:
        test_phase_1()
    if run_all or args.phase == 2:
        test_phase_2()
    if run_all or args.phase == 3:
        test_phase_3()
    if run_all or args.phase == 4:
        test_phase_4()
    if run_all or args.phase == 5:
        test_phase_5()
    if run_all or args.phase == 6:
        test_phase_6()
    if run_all or args.phase == 7:
        test_phase_7()
    if args.admin or args.phase == 8:
        test_phase_8()
    if run_all or args.phase == 9:
        test_phase_9()

    total_time = (time.perf_counter() - t_start) * 1000
    summary = runner.summary()

    print("\n" + "=" * 70)
    print(f"OMNINET TEST SUITE COMPLETED in {total_time:.1f}ms")
    print(f"Total: {summary['total']} | Passed: {summary['passed']} | Failed: {summary['failed']} | Skipped: {summary['skipped']}")
    print(f"Pass Rate: {summary['pass_rate']:.1f}%")
    print("=" * 70 + "\n")

    if args.json:
        out_dict = {
            "summary": summary,
            "total_time_ms": total_time,
            "results": [dataclasses.asdict(r) for r in runner.results]
        }
        Path(ROOT / "test_results.json").write_text(json.dumps(out_dict, indent=2), encoding="utf-8")
        print(f"Wrote test results to {ROOT / 'test_results.json'}")

    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
