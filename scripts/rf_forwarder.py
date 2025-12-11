"""
Lightweight RF forwarder for Raspberry Pi or similar edge nodes.

The script builds a minimal ZMeta RF JSON packet and delivers it over
HTTP or UDP to the ingest endpoint. It intentionally keeps dependencies
small (requests + standard library) so it can run on constrained hosts.

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
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

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

        return payload


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
    measurement = RFMeasurement(
        sensor_id=args.sensor_id,
        frequency_hz=args.frequency_hz,
        rssi_dbm=args.rssi_dbm,
        bandwidth_hz=args.bandwidth_hz,
        dwell_s=args.dwell_s,
        latitude=args.latitude,
        longitude=args.longitude,
        confidence=args.confidence,
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
