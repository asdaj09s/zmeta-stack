import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict

import pytest

from scripts.rf_forwarder import RFMeasurement, send_http, send_udp


class _CaptureHandler(BaseHTTPRequestHandler):
    received: Dict[str, str] = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        _CaptureHandler.received = {
            "path": self.path,
            "body": body,
            "headers": dict(self.headers),
        }
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return


def _start_http_server(server):
    with server:
        server.serve_forever()


def test_rf_measurement_to_payload_includes_optionals():
    measurement = RFMeasurement(
        sensor_id="blade_rf_pi",
        frequency_hz=915_000_000,
        rssi_dbm=-40.5,
        bandwidth_hz=20_000,
        dwell_s=0.8,
        latitude=35.0,
        longitude=-78.0,
        confidence=0.9,
    )

    payload = measurement.to_payload()

    assert payload["sensor_id"] == "blade_rf_pi"
    assert payload["modality"] == "rf"
    assert payload["data"]["type"] == "rf_detection"
    value = payload["data"]["value"]
    assert value["frequency_hz"] == 915_000_000
    assert value["rssi_dbm"] == -40.5
    assert value["bandwidth_hz"] == 20_000
    assert value["dwell_s"] == 0.8
    assert payload["location"] == {"lat": 35.0, "lon": -78.0}
    assert payload["confidence"] == 0.9
    assert payload["timestamp"].endswith("Z")


def test_send_udp_delivers_payload_to_socket():
    received = {}

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server_sock:
        server_sock.bind(("127.0.0.1", 0))
        host, port = server_sock.getsockname()

        def read_once():
            data, _ = server_sock.recvfrom(4096)
            received["body"] = data.decode("utf-8")

        thread = threading.Thread(target=read_once, daemon=True)
        thread.start()

        payload = {"hello": "world"}
        send_udp(payload, host, port)

        thread.join(timeout=2)

    assert json.loads(received["body"]) == payload


def test_send_http_posts_payload(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=_start_http_server, args=(server,), daemon=True)
    thread.start()

    host, port = server.server_address
    endpoint = f"http://{host}:{port}/api/v1/ingest"
    payload = {"example": True}

    response = send_http(payload, endpoint=endpoint, secret="secret123", timeout=2)

    server.shutdown()
    thread.join(timeout=2)

    assert response.status_code == 200
    assert _CaptureHandler.received["path"] == "/api/v1/ingest"
    assert json.loads(_CaptureHandler.received["body"]) == payload
    headers = {k.lower(): v for k, v in _CaptureHandler.received["headers"].items()}
    assert headers["x-zmeta-secret"] == "secret123"
    assert headers["content-type"] == "application/json"
