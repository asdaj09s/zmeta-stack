import builtins
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict
import sys

import pytest

from scripts.rf_forwarder import (
    RFMeasurement,
    SignalLibrary,
    SignalDefinition,
    _parse_frequency,
    classify_envelope,
    compute_rssi_dbm,
    load_classifier,
    measure_rssi_dbm,
    send_http,
    send_udp,
)


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


def test_rf_measurement_includes_signal_match_when_present():
    match = {"name": "adsb", "band_hz": [1089e6, 1091e6], "confidence": 0.9}
    measurement = RFMeasurement(
        sensor_id="blade_rf_pi",
        frequency_hz=1_090_000_000,
        signal_match=match,
    )

    payload = measurement.to_payload()

    assert payload["data"]["value"]["signal_of_interest"] == match


def test_signal_definition_validates_bounds():
    with pytest.raises(ValueError):
        SignalDefinition(name="invalid", min_hz=200, max_hz=100)
    with pytest.raises(ValueError):
        SignalDefinition(name="negative", min_hz=-1, max_hz=10)


def test_signal_library_rejects_bad_csv(tmp_path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text("name,min_hz,max_hz\ninvalid,915MHz,902MHz\n")

    with pytest.raises(ValueError):
        SignalLibrary.from_csv(csv_path)


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


def test_measure_rssi_dbm_with_fake_soapysdr(monkeypatch):
    calls = {}

    class FakeStreamResult:
        def __init__(self, ret):
            self.ret = ret

    class FakeDevice:
        def __init__(self, args):
            calls["device_args"] = args

        def setSampleRate(self, direction, channel, rate):
            calls["sample_rate"] = rate

        def setGain(self, direction, channel, gain):
            calls["gain"] = gain

        def setFrequency(self, direction, channel, freq):
            calls["freq"] = freq

        def setupStream(self, direction, fmt):
            calls["setup"] = (direction, fmt)
            return "stream"

        def activateStream(self, stream):
            calls["active"] = stream

        def readStream(self, stream, buffers, count):
            buffer = buffers[0]
            for i in range(count):
                buffer[i] = 0.001 + 0.001j
            return FakeStreamResult(ret=count)

        def deactivateStream(self, stream):
            calls["deactivated"] = stream

        def closeStream(self, stream):
            calls["closed"] = stream

    class FakeSoapySDR:
        SOAPY_SDR_RX = 0
        SOAPY_SDR_CF32 = 1

        @staticmethod
        def Device(args):
            return FakeDevice(args)

    class FakeNumpy:
        complex64 = complex

        def empty(self, count, dtype=None):
            return [0j for _ in range(count)]

        def abs(self, arr):
            return [abs(x) for x in arr]

        def log10(self, value):
            import math

            return math.log10(value)

        def mean(self, arr):
            return sum(arr) / len(arr)

        def std(self, arr):
            mu = self.mean(arr)
            return (sum((x - mu) ** 2 for x in arr) / len(arr)) ** 0.5

        def max(self, arr):
            return builtins.max(arr)

    monkeypatch.setitem(sys.modules, "SoapySDR", FakeSoapySDR())
    monkeypatch.setitem(sys.modules, "numpy", FakeNumpy())

    rssi = measure_rssi_dbm(driver="bladerf", frequency_hz=100_000_000, sample_rate=1_000_000, gain=10.0)

    assert calls["device_args"] == {"driver": "bladerf"}
    assert calls["sample_rate"] == 1_000_000
    assert calls["gain"] == 10.0
    assert calls["freq"] == 100_000_000
    assert isinstance(rssi, float)


def test_signal_library_from_csv_and_match(tmp_path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        """name,min_hz,max_hz,confidence,description
adsb,1.089GHz,1.091GHz,0.9,ADS-B replies
lora_915,902MHz,915MHz,0.7,LoRa US915
""",
        encoding="utf-8",
    )

    library = SignalLibrary.from_csv(csv_path)

    match = library.match(1_090_000_000)
    assert match is not None
    assert isinstance(match, SignalDefinition)
    assert match.name == "adsb"
    assert match.confidence == 0.9
    assert match.to_match_payload()["band_hz"] == [1_089_000_000.0, 1_091_000_000.0]
    assert library.match(905_000_000).name == "lora_915"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("915MHz", 915_000_000.0),
        ("1.09GHz", 1_090_000_000.0),
        ("433000000", 433_000_000.0),
        ("162.025mhz", 162_025_000.0),
    ],
)
def test_parse_frequency_supports_suffixes(value, expected):
    assert _parse_frequency(value) == pytest.approx(expected)


def test_rf_measurement_includes_classification():
    measurement = RFMeasurement(
        sensor_id="blade_rf_pi",
        frequency_hz=433_920_000,
        classification={"label": "lora_433", "confidence": 0.76},
    )

    payload = measurement.to_payload()

    assert payload["data"]["value"]["classification"] == {
        "label": "lora_433",
        "confidence": 0.76,
    }


def test_load_classifier_executes_custom_module(tmp_path):
    module_path = tmp_path / "classifier.py"
    module_path.write_text(
        """
def classify(samples, sample_rate, frequency_hz):
    return {
        "label": "custom",
        "confidence": 0.66,
        "details": {
            "sample_rate": sample_rate,
            "frequency": frequency_hz,
            "count": len(samples),
        },
    }
""",
        encoding="utf-8",
    )

    classifier = load_classifier(module_path)
    result = classifier([0j, 1j], 1_000_000, 915_000_000)

    assert result["label"] == "custom"
    assert result["confidence"] == 0.66
    assert result["details"]["count"] == 2


def test_classify_envelope_uses_variability(monkeypatch):
    class FakeNumpy:
        def abs(self, arr):
            return arr

        def mean(self, arr):
            return sum(arr) / len(arr)

        def std(self, arr):
            mu = self.mean(arr)
            return (sum((x - mu) ** 2 for x in arr) / len(arr)) ** 0.5

        def max(self, arr):
            return builtins.max(arr)

    # Quiet/continuous stream
    continuous = classify_envelope([1, 1.1, 0.9, 1.05], 1_000_000, 433_920_000, np_module=FakeNumpy())
    assert continuous["label"].startswith("continuous")
    assert 0.55 <= continuous["confidence"] <= 0.98

    # Bursty stream with higher crest factor
    bursty = classify_envelope([0.1, 0.2, 2.0, 0.1, 0.05], 1_000_000, 915_000_000, np_module=FakeNumpy())
    assert bursty["label"].startswith("bursty")
    assert bursty["confidence"] >= continuous["confidence"]
