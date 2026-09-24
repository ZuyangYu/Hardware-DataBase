from __future__ import annotations

import re


_POWER_PATTERNS = (
    re.compile(r"^(?:VCC|VDD|VIN|VBAT|VEXT|VSYS|PWR|PVDD|AVDD|DVDD)", re.IGNORECASE),
    re.compile(r"(?:^|[_-])(?:[0-9]+V[0-9]*|[0-9]+P[0-9]+V)(?:$|[_-])", re.IGNORECASE),
)
_GROUND_PATTERNS = (
    re.compile(r"^(?:GND|PGND|AGND|DGND|VSS|0V)$", re.IGNORECASE),
    re.compile(r"(?:^|[_-])GND(?:$|[_-])", re.IGNORECASE),
)
_CLOCK_PATTERNS = (
    re.compile(r"(?:CLK|CLOCK|XTAL|OSC|MCLK|SCLK)", re.IGNORECASE),
)
_POWER_PIN_EXACT = {
    "BAT",
    "BATT",
    "VBAT",
    "VIN",
    "VOUT",
    "VSYS",
    "VEXT",
    "VBUS",
    "PWR",
    "PVDD",
    "AVDD",
    "DVDD",
    "VDDA",
    "VDDD",
    "VIO",
    "VI_O",
}
_GROUND_PIN_EXACT = {"GND", "PGND", "AGND", "DGND", "VSS", "0V"}
_POWER_PIN_PREFIXES = ("VCC", "VDD")


def classify_net_name(name: str | None) -> str:
    # Defensive: EDIF escapes identifiers that start with a digit using a
    # leading ``&`` (e.g. ``&0V75_ACQ``). The lite parser strips this at
    # extraction time, but classification may be called against names from
    # other sources (SpyDrNet, JSON round-trips), so strip again here.
    net_name = (name or "").strip().lstrip("&")
    if not net_name:
        return "signal"
    if any(pattern.search(net_name) for pattern in _GROUND_PATTERNS):
        return "ground"
    if any(pattern.search(net_name) for pattern in _POWER_PATTERNS):
        return "power"
    if any(pattern.search(net_name) for pattern in _CLOCK_PATTERNS):
        return "clock"
    return "signal"


def classify_power_pin_name(name: str | None) -> str | None:
    """Classify pin names that imply a supply or reference-ground role.

    Net-name classification intentionally stays conservative; this helper is
    used when a non-obvious net (for example ``UBD_PR``) is connected to a
    clearly supply-like pin such as ``VBAT``.
    """
    pin_name = (name or "").strip().lstrip("&").upper()
    if not pin_name:
        return None
    tokens = [token for token in re.split(r"[^A-Z0-9]+", pin_name) if token]
    candidates = [pin_name, *tokens]
    if any(candidate in _GROUND_PIN_EXACT for candidate in candidates):
        return "ground"
    if any(candidate in _POWER_PIN_EXACT for candidate in candidates):
        return "power"
    if any(candidate.startswith(_POWER_PIN_PREFIXES) for candidate in candidates):
        return "power"
    return None
