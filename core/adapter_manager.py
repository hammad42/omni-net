"""
core/adapter_manager.py — enumerate NICs and change their priority safely.

Design decision worth stating up front: this module prefers *interface metrics*
over *PnP enable/disable*. Your existing script disables adapters with
Disable-NetAdapter / Disable-PnpDevice. That works, but it is exactly the path
that leaves USB NICs stuck in CM_PROB_DISABLED (22) or CM_PROB_FAILED_POST_START
after a few hundred cycles, and it hard-kills every socket on the adapter.

Changing the metric achieves the same routing outcome (lowest metric wins) in
~40 ms, is reversible, and never touches the device stack. Disable is still
exposed, gated behind config.allow_adapter_disable, for the cases where you
genuinely want the link gone (e.g. forcing a captive-portal re-auth).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .netutil import SHELL, as_list

log = logging.getLogger("omninet.adapters")

_ENUM_PS = r"""
$rows = @()
foreach ($a in (Get-NetAdapter -ErrorAction SilentlyContinue)) {
    $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
          Select-Object -First 1
    $rt = Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
          Sort-Object RouteMetric | Select-Object -First 1
    $ifc = Get-NetIPInterface -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Select-Object -First 1
    $rows += [pscustomobject]@{
        Name        = $a.Name
        Description = $a.InterfaceDescription
        IfIndex     = [int]$a.ifIndex
        Status      = [string]$a.Status
        AdminStatus = [string]$a.AdminStatus
        Mac         = [string]$a.MacAddress
        LinkSpeed   = [string]$a.LinkSpeed
        Speed       = [int64]$a.Speed
        MediaType   = [string]$a.MediaType
        Virtual     = [bool]$a.Virtual
        Ip          = if ($ip) { [string]$ip.IPAddress } else { '' }
        Prefix      = if ($ip) { [int]$ip.PrefixLength } else { 0 }
        Dhcp        = if ($ip) { [string]$ip.PrefixOrigin } else { '' }
        Gateway     = if ($rt) { [string]$rt.NextHop } else { '' }
        RouteMetric = if ($rt) { [int]$rt.RouteMetric } else { -1 }
        IfMetric    = if ($ifc) { [int]$ifc.InterfaceMetric } else { -1 }
        AutoMetric  = if ($ifc) { [string]$ifc.AutomaticMetric } else { '' }
    }
}
$rows | ConvertTo-Json -Depth 4 -Compress
"""


@dataclass
class Adapter:
    name: str
    description: str = ""
    if_index: int = 0
    status: str = ""
    admin_status: str = ""
    mac: str = ""
    link_speed: str = ""
    speed_bps: int = 0
    media_type: str = ""
    virtual: bool = False
    ip: str = ""
    prefix: int = 0
    dhcp: str = ""
    gateway: str = ""
    route_metric: int = -1
    if_metric: int = -1
    auto_metric: str = ""

    @property
    def is_wifi(self) -> bool:
        return "802.11" in (self.media_type or "") or "wi-fi" in self.name.lower()

    @property
    def is_up(self) -> bool:
        return self.status.lower() == "up"

    @property
    def is_routable(self) -> bool:
        """Has a link, an address and a default gateway — i.e. can reach the
        Internet on its own. This is the only kind of adapter we uplink."""
        return self.is_up and bool(self.ip) and bool(self.gateway)

    @property
    def label(self) -> str:
        return f"{self.name} — {self.description}"


class AdapterManager:
    """Thread-safe-ish wrapper. All PowerShell calls serialise on SHELL's lock."""

    def __init__(self) -> None:
        self._adapters: Dict[str, Adapter] = {}
        self._metric_backup: Dict[str, tuple[int, str]] = {}   # name -> (metric, AutomaticMetric)
        self._disabled_by_us: set[str] = set()

    # ------------------------------------------------------------------ #
    # Enumeration
    # ------------------------------------------------------------------ #
    def refresh(self) -> List[Adapter]:
        data = SHELL.run_json(_ENUM_PS, timeout=25)
        found: Dict[str, Adapter] = {}
        for row in as_list(data):
            try:
                a = Adapter(
                    name=row.get("Name", ""),
                    description=row.get("Description", "") or "",
                    if_index=int(row.get("IfIndex") or 0),
                    status=row.get("Status", "") or "",
                    admin_status=row.get("AdminStatus", "") or "",
                    mac=row.get("Mac", "") or "",
                    link_speed=row.get("LinkSpeed", "") or "",
                    speed_bps=int(row.get("Speed") or 0),
                    media_type=row.get("MediaType", "") or "",
                    virtual=bool(row.get("Virtual")),
                    ip=row.get("Ip", "") or "",
                    prefix=int(row.get("Prefix") or 0),
                    dhcp=row.get("Dhcp", "") or "",
                    gateway=row.get("Gateway", "") or "",
                    route_metric=int(row.get("RouteMetric", -1)),
                    if_metric=int(row.get("IfMetric", -1)),
                    auto_metric=row.get("AutoMetric", "") or "",
                )
            except Exception as e:  # one malformed row must not kill enumeration
                log.warning("bad adapter row %r: %s", row, e)
                continue
            if a.name:
                found[a.name] = a
        if data is not None:
            self._adapters = found
        return list(self._adapters.values())

    @property
    def adapters(self) -> List[Adapter]:
        return list(self._adapters.values())

    def get(self, name: str) -> Optional[Adapter]:
        return self._adapters.get(name)

    def physical(self) -> List[Adapter]:
        """Skip Hyper-V switches, VPN taps, loopback, Bluetooth PAN, Tailscale, ZeroTier etc."""
        out = []
        skip_keywords = (
            "loopback", "hyper-v", "tap-", "wan miniport", "bluetooth",
            "virtualbox", "vmware", "wintun", "zerotier", "tailscale",
            "hamachi", "wireguard"
        )
        for a in self._adapters.values():
            d = (a.description or "").lower()
            n = (a.name or "").lower()
            if a.virtual or any(k in d or k in n for k in skip_keywords):
                continue
            out.append(a)
        return sorted(out, key=lambda x: (not x.is_routable, x.if_index))

    def routable(self) -> List[Adapter]:
        return [a for a in self.physical() if a.is_routable]

    # ------------------------------------------------------------------ #
    # Metrics — the fast, safe way to steer the default route
    # ------------------------------------------------------------------ #
    def backup_metrics(self) -> None:
        changed = False
        for a in self.physical():
            if a.name not in self._metric_backup and a.if_metric >= 0:
                self._metric_backup[a.name] = (a.if_metric, a.auto_metric or "Enabled")
                changed = True
        if changed and self._metric_backup:
            self._save_backup_to_disk()

    def _save_backup_to_disk(self) -> None:
        try:
            import json
            import config
            path = config.METRICS_BACKUP_PATH
            data = {name: {"metric": m, "auto": a} for name, (m, a) in self._metric_backup.items()}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            log.warning("could not save metrics backup to disk: %s", e)

    def _clear_backup_from_disk(self) -> None:
        try:
            import config
            path = config.METRICS_BACKUP_PATH
            if path.exists():
                path.unlink()
        except Exception:
            pass

    def reconcile_startup_metrics(self) -> bool:
        """If a previous session crashed without restoring metrics, restore them now."""
        try:
            import json
            import config
            path = config.METRICS_BACKUP_PATH
            if not path.exists():
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data:
                self._clear_backup_from_disk()
                return False
            self._metric_backup = {
                name: (int(v.get("metric", 10)), str(v.get("auto", "Enabled")))
                for name, v in data.items() if isinstance(v, dict)
            }
            log.info("reconciling %d un-restored metrics from previous crashed session", len(self._metric_backup))
            self.restore_metrics()
            self._clear_backup_from_disk()
            return True
        except Exception as e:
            log.warning("failed to reconcile startup metrics: %s", e)
            self._clear_backup_from_disk()
            return False

    def set_metric(self, name: str, metric: int, both_families: bool = True) -> bool:
        """Set the interface metric. Lower == preferred. ~40 ms on a warm shell."""
        self.backup_metrics()
        fams = "'IPv4','IPv6'" if both_families else "'IPv4'"
        script = (
            f"foreach ($af in @({fams})) {{"
            f" Set-NetIPInterface -InterfaceAlias '{_q(name)}' -AddressFamily $af"
            f" -InterfaceMetric {int(metric)} -AutomaticMetric Disabled -ErrorAction SilentlyContinue }}"
            f"; 'ok'"
        )
        out = SHELL.run(script, timeout=10)
        ok = "ok" in out
        if ok:
            a = self._adapters.get(name)
            if a:
                a.if_metric = int(metric)
                a.auto_metric = "Disabled"
        return ok

    def set_metrics(self, mapping: Dict[str, int]) -> None:
        """Apply several metrics in ONE round-trip — this is what makes sub-500 ms
        failover possible. A per-adapter call would cost 3x the shell latency."""
        if not mapping:
            return
        self.backup_metrics()
        parts = []
        for name, metric in mapping.items():
            parts.append(
                f"foreach ($af in @('IPv4','IPv6')) {{ Set-NetIPInterface -InterfaceAlias "
                f"'{_q(name)}' -AddressFamily $af -InterfaceMetric {int(metric)} "
                f"-AutomaticMetric Disabled -ErrorAction SilentlyContinue }}"
            )
        SHELL.run("; ".join(parts) + "; 'ok'", timeout=15)
        for name, metric in mapping.items():
            a = self._adapters.get(name)
            if a:
                a.if_metric = int(metric)
                a.auto_metric = "Disabled"

    def restore_metrics(self) -> None:
        """Put Windows back the way we found it — automatic metric on by default."""
        if not self._metric_backup:
            self._clear_backup_from_disk()
            return
        parts = []
        for name, (metric, auto) in self._metric_backup.items():
            if (auto or "").lower() == "enabled":
                parts.append(
                    f"foreach ($af in @('IPv4','IPv6')) {{ Set-NetIPInterface -InterfaceAlias "
                    f"'{_q(name)}' -AddressFamily $af -AutomaticMetric Enabled "
                    f"-ErrorAction SilentlyContinue }}")
            else:
                parts.append(
                    f"foreach ($af in @('IPv4','IPv6')) {{ Set-NetIPInterface -InterfaceAlias "
                    f"'{_q(name)}' -AddressFamily $af -InterfaceMetric {int(metric)} "
                    f"-AutomaticMetric Disabled -ErrorAction SilentlyContinue }}")
        try:
            SHELL.run("; ".join(parts) + "; 'ok'", timeout=20)
        except Exception as e:
            log.error("metric restore failed: %s", e)
        self._metric_backup.clear()
        self._clear_backup_from_disk()

    # ------------------------------------------------------------------ #
    # Admin state — used sparingly
    # ------------------------------------------------------------------ #
    def set_admin_state(self, name: str, up: bool) -> bool:
        verb = "Enable-NetAdapter" if up else "Disable-NetAdapter"
        script = f"{verb} -Name '{_q(name)}' -Confirm:$false -ErrorAction SilentlyContinue; 'ok'"
        out = SHELL.run(script, timeout=30)
        if up:
            self._disabled_by_us.discard(name)
        else:
            self._disabled_by_us.add(name)
        return "ok" in out

    def repair_pnp(self, name: str) -> bool:
        """Recovery path for an adapter stuck in a bad PnP state (the
        CM_PROB_DISABLED case). Re-enables the device node, then the interface."""
        script = f"""
$a = Get-NetAdapter -Name '{_q(name)}' -ErrorAction SilentlyContinue
if ($a) {{
    $pnp = Get-PnpDevice -InstanceId $a.PnPDeviceID -ErrorAction SilentlyContinue
    if ($pnp -and $pnp.Status -ne 'OK') {{
        Enable-PnpDevice -InstanceId $a.PnPDeviceID -Confirm:$false -ErrorAction SilentlyContinue
    }}
    Enable-NetAdapter -Name '{_q(name)}' -Confirm:$false -ErrorAction SilentlyContinue
}}
'ok'
"""
        return "ok" in SHELL.run(script, timeout=45)

    def restore_all(self) -> None:
        for name in list(self._disabled_by_us):
            try:
                self.set_admin_state(name, True)
            except Exception:
                pass
        self.restore_metrics()

    # ------------------------------------------------------------------ #
    def flush_dns(self) -> None:
        SHELL.run("Clear-DnsClientCache -ErrorAction SilentlyContinue; 'ok'", timeout=10)


def _q(s: str) -> str:
    """Escape a single quote for a PowerShell single-quoted string."""
    return (s or "").replace("'", "''")
