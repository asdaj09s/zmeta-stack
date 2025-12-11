"""Simple RSSI threshold classifier example.

The classifier consumes IQ samples, computes average power, and tags the
payload with a label and confidence. This is intentionally lightweight so it
can run on a Raspberry Pi alongside the forwarder. Replace the logic with a
learned model if you have one; the forwarder will call ``classify`` the same
way.
"""
import math
from typing import Sequence, Dict, Any


def classify(samples: Sequence[complex], sample_rate: float, frequency_hz: float) -> Dict[str, Any]:
    power_linear = sum(abs(s) ** 2 for s in samples) / max(len(samples), 1)
    rssi_dbm = 10 * math.log10(power_linear) + 30 if power_linear > 0 else -200

    label = "quiet" if rssi_dbm < -80 else "active"
    confidence = 0.9 if label == "active" else 0.7

    return {
        "label": label,
        "confidence": confidence,
        "features": {
            "rssi_dbm": rssi_dbm,
            "frequency_hz": frequency_hz,
            "sample_rate": sample_rate,
        },
    }
