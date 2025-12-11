"""
Lightweight RF forwarder for Raspberry Pi or similar edge nodes.

The script builds a minimal ZMeta RF JSON packet and delivers it over
HTTP or UDP to the ingest endpoint. It intentionally keeps dependencies
small (requests + standard library) so it can run on constrained hosts.

The SoapyBladeRF module on the bladeRF GitHub registers the driver key
"bladerf", which is the value expected by the ``--soapysdr-driver`` flag.

You can also tag detections with a signal-of-interest CSV (see
``examples/signals_of_interest.csv``) so the forwarder emits
``signal_of_interest`` metadata and a confidence score alongside the RF
payload. For richer detection labeling, load a custom classifier module (see
``examples/rf_classifier_threshold.py``) that consumes the captured IQ samples
and returns a label + confidence for inclusion in the payload. A built-in
envelope classifier is also available via ``--builtin-classifier envelope``; it
uses amplitude variability and crest factor to distinguish bursty links from
continuous emitters without heavy dependencies.

Example:
    python scripts/rf_forwarder.py \
        --sensor-id blade_rf_pi \
        --frequency-hz 915000000 \
        --rssi-dbm -35 \
        --http-endpoint http://backend.example.com:8000/api/v1/ingest \
        --secret my_shared_secret
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import socket
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple

import requests


@dataclass
class RFMeasurement:
    """Container for minimal RF measurement fields."""

    sensor_id: str
    frequency_hz: float
    rssi_dbm: Optional[float] = None
    bandwidth_hz: Optional[float] = None
    dwell_s: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    confidence: Optional[float] = None
    source_format: str = "zmeta"

    signal_match: Optional[Dict[str, Any]] = None
    classification: Optional[Dict[str, Any]] = None

    def to_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload: Dict[str, Any] = {
            "sensor_id": self.sensor_id,
            "modality": "rf",
            "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "source_format": self.source_format,
            "data": {
                "type": "rf_detection",
                "value": {
                    "frequency_hz": self.frequency_hz,
                },
            },
        }

        if self.rssi_dbm is not None:
            payload["data"]["value"]["rssi_dbm"] = self.rssi_dbm
        if self.bandwidth_hz is not None:
            payload["data"]["value"]["bandwidth_hz"] = self.bandwidth_hz
        if self.dwell_s is not None:
            payload["data"]["value"]["dwell_s"] = self.dwell_s
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.latitude is not None and self.longitude is not None:
            payload["location"] = {"lat": self.latitude, "lon": self.longitude}
        if self.signal_match:
            payload["data"]["value"]["signal_of_interest"] = self.signal_match
        if self.classification:
            payload["data"]["value"]["classification"] = self.classification

        return payload


@dataclass(frozen=True)
class SignalDefinition:
    """Represents a single signal of interest described in a CSV row."""

    name: str
    min_hz: float
    max_hz: float
    confidence: float = 0.8
    description: Optional[str] = None

    def __post_init__(self) -> None:
        if self.min_hz <= 0 or self.max_hz <= 0:
            raise ValueError("Frequencies must be positive")
        if self.min_hz > self.max_hz:
            raise ValueError("min_hz must be less than or equal to max_hz")

    def matches(self, frequency_hz: float) -> bool:
        return self.min_hz <= frequency_hz <= self.max_hz

    def to_match_payload(self) -> Dict[str, Any]:
        match = {
            "name": self.name,
            "band_hz": [self.min_hz, self.max_hz],
            "confidence": self.confidence,
        }
        if self.description:
            match["description"] = self.description
        return match


def _parse_frequency(value: str) -> float:
    """Parse a frequency string supporting Hz, MHz, and GHz suffixes."""

    text = str(value).strip().replace(" ", "").lower()
    multiplier = 1.0
    for suffix, factor in ("ghz", 1e9), ("mhz", 1e6), ("khz", 1e3):
        if text.endswith(suffix):
            multiplier = factor
            text = text[: -len(suffix)]
            break

    try:
        numeric = float(text)
    except ValueError as exc:
        raise ValueError(f"Invalid frequency value: {value}") from exc

    return numeric * multiplier


class SignalLibrary:
    """Signal-of-interest library backed by a CSV definition file.

    Columns:
        name,min_hz,max_hz,confidence,description
    Frequencies may be provided in Hz, MHz (e.g., "915MHz"), or GHz (e.g.,
    "1.09GHz"). Confidence defaults to 0.8 if omitted.
    """

    def __init__(self, signals: Iterable[SignalDefinition]):
        self._signals = list(signals)

    @classmethod
    def from_csv(cls, path: Path) -> "SignalLibrary":
        with path.open("r", newline="") as handle:
            reader = csv.DictReader(handle)
            definitions = []
            for row in reader:
                if "min_hz" not in row or "max_hz" not in row or "name" not in row:
                    raise ValueError("CSV is missing required columns: name,min_hz,max_hz")
                try:
                    min_hz = _parse_frequency(row["min_hz"])
                    max_hz = _parse_frequency(row["max_hz"])
                except ValueError as exc:
                    raise ValueError(f"Invalid frequency in row for signal '{row.get('name', '')}': {exc}") from exc
                definitions.append(
                    SignalDefinition(
                        name=row["name"],
                        min_hz=min_hz,
                        max_hz=max_hz,
                        confidence=float(row.get("confidence", 0.8) or 0.8),
                        description=row.get("description") or None,
                    )
                )
        return cls(definitions)

    def match(self, frequency_hz: float) -> Optional[SignalDefinition]:
        for signal in self._signals:
            if signal.matches(frequency_hz):
                return signal
        return None


def send_http(payload: Dict[str, Any], endpoint: str, secret: Optional[str] = None, timeout: float = 5.0) -> requests.Response:
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-ZMeta-Secret"] = secret

    response = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=timeout)
    response.raise_for_status()
    return response


def send_udp(payload: Dict[str, Any], host: str, port: int) -> None:
    message = json.dumps(payload).encode("utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(message, (host, port))


def measure_rssi_dbm(
    *,
    driver: str,
    frequency_hz: float,
    sample_rate: float = 2_000_000,
    gain: float = 0.0,
    sample_count: int = 4096,
    channel: int = 0,
) -> float:
    """Approximate RSSI using a SoapySDR-compatible device (e.g., bladeRF).

    The function measures average power across ``sample_count`` IQ samples and
    converts it to dBm. It requires ``SoapySDR`` and ``numpy`` at runtime but
    imports them lazily so that the forwarder can still run without SDR
    hardware or those dependencies installed.
    """

    samples, np = collect_iq_samples(
        driver=driver,
        frequency_hz=frequency_hz,
        sample_rate=sample_rate,
        gain=gain,
        sample_count=sample_count,
        channel=channel,
    )
    return compute_rssi_dbm(samples, np_module=np)


def collect_iq_samples(
    *,
    driver: str,
    frequency_hz: float,
    sample_rate: float,
    gain: float,
    sample_count: int,
    channel: int = 0,
) -> Tuple[Sequence[complex], Any]:
    """Capture IQ samples from a SoapySDR device and return them with numpy."""

    def _module_available(name: str) -> bool:
        if name in sys.modules:
            return True
        return importlib.util.find_spec(name) is not None

    if not (_module_available("SoapySDR") and _module_available("numpy")):
        raise RuntimeError("SoapySDR and numpy are required for SDR measurements")

    SoapySDR = importlib.import_module("SoapySDR")  # type: ignore
    np = importlib.import_module("numpy")  # type: ignore

    sdr = SoapySDR.Device({"driver": driver})
    sdr.setSampleRate(SoapySDR.SOAPY_SDR_RX, channel, sample_rate)
    sdr.setGain(SoapySDR.SOAPY_SDR_RX, channel, gain)
    sdr.setFrequency(SoapySDR.SOAPY_SDR_RX, channel, frequency_hz)

    rx_stream = sdr.setupStream(SoapySDR.SOAPY_SDR_RX, SoapySDR.SOAPY_SDR_CF32)
    sdr.activateStream(rx_stream)

    buffer = np.empty(sample_count, dtype=np.complex64)
    result = sdr.readStream(rx_stream, [buffer], sample_count)

    if isinstance(result, tuple):
        num_samples = result[0]
    else:
        num_samples = getattr(result, "ret", sample_count)

    sdr.deactivateStream(rx_stream)
    sdr.closeStream(rx_stream)

    if num_samples <= 0:
        raise RuntimeError("No samples captured from SDR")

    return buffer[:num_samples], np


def compute_rssi_dbm(samples: Sequence[complex], *, np_module: Any) -> float:
    magnitude = np_module.abs(samples)
    if hasattr(magnitude, "__mul__") and not isinstance(magnitude, list):
        power_linear = magnitude * magnitude
    else:
        power_linear = [val * val for val in magnitude]

    if hasattr(power_linear, "mean"):
        power_watts = float(power_linear.mean())
    else:
        power_watts = float(sum(power_linear) / len(power_linear))
    if power_watts <= 0:
        raise RuntimeError("Invalid power measurement from SDR")

    return 10 * float(np_module.log10(power_watts)) + 30


def load_classifier(module_path: Path) -> Callable[[Sequence[complex], float, float], Dict[str, Any]]:
    """Load a classifier callable from a python module at ``module_path``."""

    spec = spec_from_loader(module_path.stem, SourceFileLoader(module_path.stem, str(module_path)))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load classifier module from {module_path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)

    classify_fn = getattr(module, "classify", None)
    if classify_fn is None or not callable(classify_fn):
        raise RuntimeError("Classifier module must expose a callable 'classify(samples, sample_rate, frequency_hz)'")

    return classify_fn


def classify_envelope(
    samples: Sequence[complex],
    sample_rate: float,
    frequency_hz: float,
    *,
    np_module: Any,
) -> Dict[str, Any]:
    """Lightweight classifier that looks at amplitude envelope statistics.

    It inspects magnitude variance and crest factor (peak-to-average ratio)
    to separate steady "continuous" emitters from bursty links such as many
    drone control channels. The computation relies only on basic numpy ops
    and reuses IQ samples already captured for RSSI.
    """

    magnitudes = np_module.abs(samples)
    mean_mag = float(np_module.mean(magnitudes)) if len(magnitudes) else 0.0
    std_mag = float(np_module.std(magnitudes)) if len(magnitudes) else 0.0
    peak_mag = float(np_module.max(magnitudes)) if len(magnitudes) else 0.0

    variability = std_mag / (mean_mag + 1e-9)
    crest_factor = peak_mag / (mean_mag + 1e-9)

    label = "continuous" if variability < 0.35 and crest_factor < 3 else "bursty"
    confidence = max(0.55, min(0.98, 0.5 + 0.2 * variability + 0.1 * crest_factor))

    return {
        "label": f"{label}_envelope",
        "confidence": round(confidence, 3),
        "features": {
            "variability": variability,
            "crest_factor": crest_factor,
            "sample_rate": sample_rate,
            "frequency_hz": frequency_hz,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward a minimal RF detection as ZMeta JSON")
    parser.add_argument("--sensor-id", required=True, help="Unique sensor identifier (e.g., blade_rf_pi)")
    parser.add_argument("--frequency-hz", type=float, required=True, help="Center frequency in Hz")
    parser.add_argument("--rssi-dbm", type=float, help="Optional RSSI in dBm")
    parser.add_argument("--bandwidth-hz", type=float, help="Optional bandwidth in Hz")
    parser.add_argument("--dwell-s", type=float, help="Optional dwell time in seconds")
    parser.add_argument("--latitude", type=float, help="Latitude if GPS is available")
    parser.add_argument("--longitude", type=float, help="Longitude if GPS is available")
    parser.add_argument("--confidence", type=float, help="Optional confidence (0-1)")
    parser.add_argument("--secret", help="Shared secret for HTTP ingest")

    parser.add_argument(
        "--signals-csv",
        type=Path,
        help="CSV file describing signals of interest to tag detections",
    )

    parser.add_argument(
        "--classifier-module",
        type=Path,
        help=(
            "Python module exposing classify(samples, sample_rate, frequency_hz) -> {label, confidence} "
            "to enrich RF payloads"
        ),
    )

    parser.add_argument(
        "--builtin-classifier",
        choices=["envelope"],
        help="Use a lightweight built-in classifier that inspects amplitude variability",
    )

    parser.add_argument("--soapysdr-driver", help="Use SoapySDR to measure RSSI (e.g., bladerf)")
    parser.add_argument("--sample-rate", type=float, default=2_000_000, help="SDR sample rate in samples per second")
    parser.add_argument("--sdr-gain", type=float, default=0.0, help="RX gain to apply on the SDR")
    parser.add_argument(
        "--sample-count",
        type=int,
        default=4096,
        help="Number of IQ samples to average for RSSI",
    )

    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument(
        "--http-endpoint",
        help="HTTP ingest endpoint (e.g., http://localhost:8000/api/v1/ingest)",
    )
    destination.add_argument(
        "--udp",
        nargs=2,
        metavar=("HOST", "PORT"),
        help="Send via UDP to host/port (e.g., --udp 127.0.0.1 5005)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    signal_match = None
    signal_library: Optional[SignalLibrary] = None
    if args.signals_csv:
        signal_library = SignalLibrary.from_csv(args.signals_csv)

    classification: Optional[Dict[str, Any]] = None
    classifier_fn: Optional[Callable[[Sequence[complex], float, float], Dict[str, Any]]] = None
    if args.classifier_module and args.builtin_classifier:
        print("Choose either --classifier-module or --builtin-classifier, not both", file=sys.stderr)
        sys.exit(1)

    if args.builtin_classifier and not args.soapysdr_driver:
        print("Built-in classifier requires SDR sampling; set --soapysdr-driver", file=sys.stderr)
        sys.exit(1)

    if args.classifier_module:
        classifier_fn = load_classifier(args.classifier_module)
    elif args.builtin_classifier == "envelope":
        classifier_fn = None  # assigned once numpy is available

    rssi_dbm = args.rssi_dbm
    samples: Optional[Sequence[complex]] = None
    np_module: Any = None
    if args.soapysdr_driver:
        try:
            samples, np_module = collect_iq_samples(
                driver=args.soapysdr_driver,
                frequency_hz=args.frequency_hz,
                sample_rate=args.sample_rate,
                gain=args.sdr_gain,
                sample_count=args.sample_count,
            )
            rssi_dbm = compute_rssi_dbm(samples, np_module=np_module)
            print(f"Measured RSSI via SoapySDR: {rssi_dbm:.2f} dBm")
            if args.builtin_classifier == "envelope":
                classifier_fn = lambda iq, rate, freq: classify_envelope(iq, rate, freq, np_module=np_module)
        except Exception as exc:
            print(f"Failed to sample SDR RSSI: {exc}", file=sys.stderr)
            sys.exit(1)
    elif classifier_fn:
        print("Classifier requires SDR sampling; set --soapysdr-driver", file=sys.stderr)
        sys.exit(1)

    if classifier_fn and samples is not None and np_module is not None:
        try:
            classification = classifier_fn(samples, args.sample_rate, args.frequency_hz)
        except Exception as exc:
            print(f"Classifier failed: {exc}", file=sys.stderr)
            sys.exit(1)

    if signal_library:
        match = signal_library.match(args.frequency_hz)
        if match:
            signal_match = match.to_match_payload()
            if args.confidence is None:
                args.confidence = match.confidence

    measurement = RFMeasurement(
        sensor_id=args.sensor_id,
        frequency_hz=args.frequency_hz,
        rssi_dbm=rssi_dbm,
        bandwidth_hz=args.bandwidth_hz,
        dwell_s=args.dwell_s,
        latitude=args.latitude,
        longitude=args.longitude,
        confidence=args.confidence,
        signal_match=signal_match,
        classification=classification,
    )
    payload = measurement.to_payload()

    if args.http_endpoint:
        response = send_http(payload, endpoint=args.http_endpoint, secret=args.secret)
        print(f"HTTP {response.status_code}: {response.text}")
    else:
        host, port_str = args.udp
        send_udp(payload, host=host, port=int(port_str))
        print(f"UDP packet sent to {host}:{port_str}")


if __name__ == "__main__":
    main()
