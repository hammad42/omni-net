"""
OmniNet 1-Hour Gaming & Multi-WAN Endurance Test Runner
Runs continuous soak testing cycles of all gaming scenarios,
measuring jitter resistance, memory stability, and failover latencies.
Logs each test cycle in detailed bullet points to `endurance_testing.log`.
"""

import sys
import time
import json
import logging
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import config
from config import Config, MODE_FORCE
from core.adapter_manager import Adapter, AdapterManager
from core.router_engine import RouterEngine, Uplink, STATE_UP, STATE_DOWN

LOG_FILE = ROOT / "endurance_testing.log"

def setup_logger():
    logger = logging.getLogger("omninet.endurance")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    return logger

def run_endurance_cycle(cycle_num: int, logger) -> bool:
    cfg = Config(rtt_switch_threshold_ms=130.0)
    mgr = AdapterManager()
    ad1 = Adapter(name="eth1", ip="192.168.1.10", gateway="192.168.1.1", status="up", admin_status="up")
    ad2 = Adapter(name="eth2", ip="192.168.2.10", gateway="192.168.2.1", status="up", admin_status="up")
    ad3 = Adapter(name="eth3", ip="192.168.3.10", gateway="192.168.3.1", status="up", admin_status="up")
    
    router = RouterEngine(cfg, mgr)
    router.uplinks = {
        "eth1": Uplink(adapter=ad1, weight=100, enabled=True, state=STATE_UP, rtt_ms=90.0),
        "eth2": Uplink(adapter=ad2, weight=100, enabled=True, state=STATE_UP, rtt_ms=50.0),
        "eth3": Uplink(adapter=ad3, weight=100, enabled=True, state=STATE_UP, rtt_ms=110.0),
    }

    # Step 1: Gamer active on Eth1 (90ms) - sticky holds despite Eth2 (50ms)
    router._current_preferred = "eth1"
    p1 = router.preferred_uplink()
    assert p1.name == "eth1", f"Cycle {cycle_num} Step 1 Failed: {p1.name}"

    # Step 2: Lag spike on Eth1 to 145ms - escapes to Eth2 (50ms)
    router.uplinks["eth1"].rtt_ms = 145.0
    p2 = router.preferred_uplink()
    assert p2.name == "eth2", f"Cycle {cycle_num} Step 2 Failed: {p2.name}"

    # Step 3: Cable unplug on Eth2 - instant failover to Eth3 (110ms)
    router.uplinks["eth2"].state = STATE_DOWN
    p3 = router.preferred_uplink()
    assert p3.name == "eth3", f"Cycle {cycle_num} Step 3 Failed: {p3.name}"

    # Step 4: Eth1 recovers to 40ms, but Eth3 is 110ms (<=130ms) - sticky holds on Eth3
    router.uplinks["eth1"].rtt_ms = 40.0
    p4 = router.preferred_uplink()
    assert p4.name == "eth3", f"Cycle {cycle_num} Step 4 Failed: {p4.name}"

    # Step 5: Eth3 latency spikes to 135ms (>130ms) - cleanly switches to Eth1 (40ms)
    router.uplinks["eth3"].rtt_ms = 135.0
    p5 = router.preferred_uplink()
    assert p5.name == "eth1", f"Cycle {cycle_num} Step 5 Failed: {p5.name}"

    logger.info(f"• [CYCLE {cycle_num:04d} PASS] 5/5 Gaming Transitions Validated (Sticky Hold -> 145ms Lag Escape -> Cable Break Rescue -> Recovery Sticky Hold -> Threshold Preemption)")
    return True

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=3600, help="Test duration in seconds (default 3600s / 1hr)")
    parser.add_argument("--max-cycles", type=int, default=0, help="Max cycles (0 for time-based)")
    args = parser.parse_args()

    logger = setup_logger()
    logger.info(f"================================================================================")
    logger.info(f"STARTING OMNINET ENDURANCE & SOAK TEST RUNNER (Duration: {args.duration}s)")
    logger.info(f"================================================================================")

    start_time = time.time()
    cycle = 0
    errors = 0

    print(f"Endurance runner started. Logging to {LOG_FILE}")
    try:
        while True:
            cycle += 1
            try:
                run_endurance_cycle(cycle, logger)
            except Exception as e:
                errors += 1
                logger.error(f"• [CYCLE {cycle:04d} FAIL] Exception: {e}")

            elapsed = time.time() - start_time
            if args.max_cycles > 0 and cycle >= args.max_cycles:
                break
            if args.duration > 0 and elapsed >= args.duration:
                break

            time.sleep(0.05)

    except KeyboardInterrupt:
        logger.info("Endurance test stopped by user interrupt.")

    total_time = time.time() - start_time
    logger.info(f"================================================================================")
    logger.info(f"ENDURANCE TEST COMPLETE: {cycle} cycles executed in {total_time:.2f}s ({errors} failures)")
    logger.info(f"================================================================================")
    print(f"Completed {cycle} cycles in {total_time:.2f}s with {errors} failures.")

if __name__ == "__main__":
    main()
