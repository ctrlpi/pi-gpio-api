#!/usr/bin/env python3
import sys
import os

# Auto-switch to local virtual environment if outer python lacks dependencies
_script_dir = os.path.dirname(os.path.abspath(__file__))
_venv_py = os.path.join(_script_dir, "venv", "bin", "python3")
if not os.path.isfile(_venv_py):
    _venv_py = os.path.join(_script_dir, "venv", "bin", "python")

if os.path.isfile(_venv_py) and os.path.abspath(sys.executable) != os.path.abspath(_venv_py):
    try:
        import requests
        import fastapi
        import pydantic
    except ImportError:
        os.execv(_venv_py, [_venv_py] + sys.argv)

try:
    import requests
except ImportError:
    print("Error: missing required package (requests).", file=sys.stderr)
    sys.exit(1)

# Check if fastapi and pydantic are available
try:
    import fastapi
    import pydantic
    HAS_FASTAPI_AND_PYDANTIC = True
except ImportError:
    HAS_FASTAPI_AND_PYDANTIC = False

import time
import unittest
import json
import tempfile
import socket
import shutil
import subprocess

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

class BasePiGpioApiTests:
    def __str__(self):
        return f"{self._testMethodName} [{getattr(self, 'server_script_name', 'server')}]"

    @classmethod
    def start_server(cls, server_script_name: str):
        cls.server_script_name = server_script_name
        cls.repo_dir = os.path.dirname(os.path.abspath(__file__))
        cls.script_path = os.path.join(cls.repo_dir, server_script_name)

        cls.port = find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.api_key = "test-secret-key"
        cls.headers = {"Api-Key": cls.api_key}

        # Run inside an isolated temporary directory so state files (config.json,
        # status.json, agent.log) do not touch or pollute the repository.
        cls.temp_dir = tempfile.mkdtemp(prefix=f"pi_gpio_{server_script_name.replace('.', '_')}_test_")

        # Set up a test sensor script in scripts/
        cls.sensor_dir = os.path.join(cls.repo_dir, "scripts")
        cls.created_sensor_dir = not os.path.exists(cls.sensor_dir)
        os.makedirs(cls.sensor_dir, exist_ok=True)
        cls.test_script_name = "test_ci_sensor.sh"
        cls.test_script_path = os.path.join(cls.sensor_dir, cls.test_script_name)
        with open(cls.test_script_path, "w") as f:
            f.write("#!/bin/sh\necho '{\"value\": 42}'\n")
        os.chmod(cls.test_script_path, 0o755)

        # Start server process
        cmd = [
            sys.executable,
            cls.script_path,
            "--port", str(cls.port),
            "--local",
            "--api-key", cls.api_key,
            "--save"
        ]
        cls.server_proc = subprocess.Popen(
            cmd,
            cwd=cls.temp_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Wait for the API server to be responsive
        deadline = time.time() + 10.0
        started = False
        while time.time() < deadline:
            if cls.server_proc.poll() is not None:
                raise RuntimeError(f"Server {server_script_name} terminated prematurely with code {cls.server_proc.returncode}")
            try:
                r = requests.get(f"{cls.base_url}/hello", timeout=0.5)
                if r.status_code == 200:
                    started = True
                    break
            except Exception:
                pass
            time.sleep(0.1)

        if not started:
            cls.server_proc.terminate()
            raise RuntimeError(f"Timed out waiting for {server_script_name} to start on localhost")

    @classmethod
    def stop_server(cls):
        # Terminate server process
        if hasattr(cls, "server_proc") and cls.server_proc:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()
                cls.server_proc.wait()

        # Clean up temporary directory
        if hasattr(cls, "temp_dir") and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

        # Clean up test sensor script
        if hasattr(cls, "test_script_path") and os.path.exists(cls.test_script_path):
            try:
                os.remove(cls.test_script_path)
            except OSError:
                pass
        if getattr(cls, "created_sensor_dir", False) and os.path.exists(cls.sensor_dir):
            try:
                os.rmdir(cls.sensor_dir)
            except OSError:
                pass

    def test_hello(self):
        """GET /hello succeeds without auth and returns device name"""
        res = requests.get(f"{self.base_url}/hello", timeout=3)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("name", data)

    def test_auth_enforced(self):
        """Protected endpoints reject requests with missing or invalid Api-Key with 403"""
        # Missing Api-Key header
        res_no_auth = requests.get(f"{self.base_url}/config/read", timeout=3)
        self.assertEqual(res_no_auth.status_code, 403)

        # Invalid Api-Key header
        res_bad_auth = requests.get(f"{self.base_url}/config/read", headers={"Api-Key": "invalid-key"}, timeout=3)
        self.assertEqual(res_bad_auth.status_code, 403)

        # Valid Api-Key header
        res_ok = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3)
        self.assertEqual(res_ok.status_code, 200)

    def test_config_read(self):
        """GET /config/read returns complete agent configuration"""
        res = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("agent", data)
        self.assertIn("settings", data)
        self.assertIn("gpios", data)
        self.assertIn("sensors", data)
        self.assertIn("platform", data["agent"])
        self.assertIn("version", data["agent"])

    def test_config_update(self):
        """POST /config/update applies runtime configuration changes"""
        res = requests.post(
            f"{self.base_url}/config/update",
            headers=self.headers,
            json={"log_days": 14},
            timeout=3
        )
        self.assertEqual(res.status_code, 200)

        # Verify update in config read
        read_res = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3)
        self.assertEqual(read_res.json().get("settings", {}).get("log_days"), 14)

        # Restore
        requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"log_days": 7}, timeout=3)

    def test_numbers_stored_as_text(self):
        """A number sent for a key, name, URL or script is stored as text"""
        name = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json().get("name")
        res = requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"name": 77, "webhook_key": 7, "matter_key": 8}, timeout=3)
        self.assertEqual(res.status_code, 200)
        cfg = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json()
        self.assertEqual(cfg["name"], "77")
        self.assertEqual(cfg["notifications"]["webhook_key"], "7")
        self.assertEqual(cfg["bridges"]["matter_key"], "8")
        # A numeric api_key becomes "4242", which then authenticates.
        self.assertEqual(requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"api_key": 4242}, timeout=3).status_code, 200)
        try:
            self.assertEqual(requests.get(f"{self.base_url}/config/read", headers={"Api-Key": "4242"}, timeout=3).status_code, 200)
        finally:
            requests.post(f"{self.base_url}/config/update", headers={"Api-Key": "4242"}, json={"api_key": self.api_key}, timeout=3)
        requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"name": name, "webhook_key": "", "matter_key": ""}, timeout=3)
        # The same on /config/load, and for a pin name and a sensor script.
        res = requests.post(f"{self.base_url}/config/load", headers=self.headers, json={"name": 9, "gpios": {"26": {"type": "output", "name": 777}}}, timeout=3)
        self.assertEqual(res.status_code, 200)
        cfg = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json()
        self.assertEqual(cfg["name"], "9")
        self.assertEqual(cfg["gpios"]["26"]["name"], "777")
        requests.post(f"{self.base_url}/config/load", headers=self.headers, json={"name": name}, timeout=3)
        self.assertEqual(requests.post(f"{self.base_url}/gpio/config/26", headers=self.headers, json={"type": "output", "name": 778}, timeout=3).status_code, 200)
        self.assertEqual(requests.get(f"{self.base_url}/gpio/read/26", headers=self.headers, timeout=3).json().get("name"), "778")
        res = requests.post(f"{self.base_url}/sensor/config/numeric", headers=self.headers, json={"script": 123}, timeout=3)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("script"), "123")
        requests.post(f"{self.base_url}/sensor/config/numeric", headers=self.headers, json={"remove": True}, timeout=3)
        requests.post(f"{self.base_url}/gpio/config/26", headers=self.headers, json={"type": "remove"}, timeout=3)

    def test_config_load(self):
        """POST /config/load loads bulk JSON configuration"""
        payload = {
            "gpios": {
                "26": {"name": "loaded_pin", "type": "output", "init": 0}
            }
        }
        res = requests.post(f"{self.base_url}/config/load", headers=self.headers, json=payload, timeout=3)
        self.assertEqual(res.status_code, 200)
        read_res = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3)
        self.assertIn("26", read_res.json().get("gpios", {}))

    def test_gpio_config_and_read(self):
        """POST /gpio/config configures a pin; GET /gpio/read reads by pin and alias"""
        res = requests.post(
            f"{self.base_url}/gpio/config/26",
            headers=self.headers,
            json={"name": "relay", "type": "output", "init": 0, "reversed": True},
            timeout=3
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("name"), "relay")
        self.assertEqual(data.get("reversed"), True)
        self.assertEqual(data.get("gpio"), 26)

        # Read by pin number
        read_pin = requests.get(f"{self.base_url}/gpio/read/26", headers=self.headers, timeout=3)
        self.assertEqual(read_pin.status_code, 200)
        pin_data = read_pin.json()
        self.assertEqual(pin_data.get("value"), 0)
        self.assertEqual(pin_data.get("name"), "relay")

        # Read by alias name
        read_alias = requests.get(f"{self.base_url}/gpio/read/relay", headers=self.headers, timeout=3)
        self.assertEqual(read_alias.status_code, 200)
        self.assertEqual(read_alias.json().get("gpio"), 26)

    def test_gpio_write_and_toggle(self):
        """POST /gpio/write supports setting 1, 0, and 'toggle'"""
        requests.post(f"{self.base_url}/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0}, timeout=3)

        # Write 1
        w1 = requests.post(f"{self.base_url}/gpio/write/26", headers=self.headers, json={"value": 1}, timeout=3)
        self.assertEqual(w1.status_code, 200)
        self.assertEqual(w1.json().get("value"), 1)

        r1 = requests.get(f"{self.base_url}/gpio/read/26", headers=self.headers, timeout=3)
        self.assertEqual(r1.json().get("value"), 1)

        # Toggle to 0
        wt = requests.post(f"{self.base_url}/gpio/write/26", headers=self.headers, json={"value": "toggle"}, timeout=3)
        self.assertEqual(wt.status_code, 200)
        self.assertEqual(wt.json().get("value"), 0)

        # Write 0
        w0 = requests.post(f"{self.base_url}/gpio/write/26", headers=self.headers, json={"value": 0}, timeout=3)
        self.assertEqual(w0.status_code, 200)
        self.assertEqual(w0.json().get("value"), 0)

    def test_gpio_pulse_duration(self):
        """POST /gpio/write with duration pulses the output and reverts"""
        requests.post(f"{self.base_url}/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0}, timeout=3)
        pulse = requests.post(
            f"{self.base_url}/gpio/write/26",
            headers=self.headers,
            json={"value": 1, "duration": 0.2},
            timeout=3
        )
        self.assertEqual(pulse.status_code, 200)
        self.assertEqual(pulse.json().get("value"), 1)

        # Reverts back after duration
        time.sleep(0.35)
        r = requests.get(f"{self.base_url}/gpio/read/26", headers=self.headers, timeout=3)
        self.assertEqual(r.json().get("value"), 0)

    def test_gpio_read_all(self):
        """GET /gpio/read returns all configured pins"""
        requests.post(f"{self.base_url}/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0}, timeout=3)
        r = requests.get(f"{self.base_url}/gpio/read", headers=self.headers, timeout=3)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, dict)
        self.assertIn("26", data)

    def test_gpio_watched(self):
        """GET /gpio/watched returns the map/list of watched pins"""
        r = requests.get(f"{self.base_url}/gpio/watched", headers=self.headers, timeout=3)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("watched", data)
        self.assertIsInstance(data["watched"], (dict, list))

    def test_gpio_remove(self):
        """POST /gpio/config with type='remove' deletes pin configuration"""
        requests.post(f"{self.base_url}/gpio/config/19", headers=self.headers, json={"type": "output"}, timeout=3)
        r_rem = requests.post(f"{self.base_url}/gpio/config/19", headers=self.headers, json={"type": "remove"}, timeout=3)
        self.assertEqual(r_rem.status_code, 200)
        self.assertEqual(r_rem.json().get("status"), "removed")

        r_read = requests.get(f"{self.base_url}/gpio/read", headers=self.headers, timeout=3)
        self.assertNotIn("19", r_read.json())

    def test_gpio_scan(self):
        """GET /gpio/scan reports platform scan or unsupported platform (422 off-Pi)"""
        r = requests.get(f"{self.base_url}/gpio/scan", headers=self.headers, timeout=3)
        if r.status_code == 422:
            self.assertIn("error", r.json())
        else:
            self.assertEqual(r.status_code, 200)
            self.assertIn("gpios", r.json())

    def test_sensor_config_and_read(self):
        """POST /sensor/config and GET /sensor/read correctly execute and manage scripts"""
        r_cfg = requests.post(
            f"{self.base_url}/sensor/config/test_sens",
            headers=self.headers,
            json={"script": self.test_script_name},
            timeout=3
        )
        self.assertEqual(r_cfg.status_code, 200)
        self.assertEqual(r_cfg.json().get("name"), "test_sens")

        # Single-field JSON unwraps value
        r_read = requests.get(f"{self.base_url}/sensor/read/test_sens", headers=self.headers, timeout=3)
        self.assertEqual(r_read.status_code, 200)
        self.assertEqual(r_read.json().get("test_sens"), 42)

        # Merged read
        r_all = requests.get(f"{self.base_url}/sensor/read", headers=self.headers, timeout=3)
        self.assertEqual(r_all.status_code, 200)
        self.assertIn("test_sens", r_all.json())

        # Remove sensor
        r_del = requests.post(
            f"{self.base_url}/sensor/config/test_sens",
            headers=self.headers,
            json={"remove": True},
            timeout=3
        )
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(r_del.json().get("status"), "removed")

        # 404 after removal
        r_gone = requests.get(f"{self.base_url}/sensor/read/test_sens", headers=self.headers, timeout=3)
        self.assertEqual(r_gone.status_code, 404)

    def test_sensor_security_path_traversal(self):
        """Sensor configuration rejects path traversal attempts with 400"""
        for bad in ["../main.py", "/bin/sh", "/etc/passwd", "scripts/test.sh", ".hidden"]:
            r = requests.post(
                f"{self.base_url}/sensor/config/pwn",
                headers=self.headers,
                json={"script": bad},
                timeout=3
            )
            self.assertEqual(r.status_code, 400, f"Expected 400 for bad path: {bad}")

    def test_sensor_security_arg_injection(self):
        """Sensor configuration rejects shell injection characters in arguments with 400"""
        for bad in [
            f"{self.test_script_name} ; rm -rf /",
            f"{self.test_script_name} && echo hi",
            f"{self.test_script_name} $(whoami)"
        ]:
            r = requests.post(
                f"{self.base_url}/sensor/config/pwn",
                headers=self.headers,
                json={"script": bad},
                timeout=3
            )
            self.assertEqual(r.status_code, 400, f"Expected 400 for injection: {bad}")

    def test_logs(self):
        """GET /logs returns log tail list"""
        r = requests.get(f"{self.base_url}/logs", headers=self.headers, timeout=3)
        self.assertEqual(r.status_code, 200)
        self.assertIn("logs", r.json())
        self.assertIsInstance(r.json()["logs"], list)

    def test_body_size_limit(self):
        """Requests exceeding 16KB payload limit are rejected with 413"""
        huge_data = {"dummy": "x" * 20000}
        r = requests.post(f"{self.base_url}/config/load", headers=self.headers, json=huge_data, timeout=3)
        self.assertEqual(r.status_code, 413)


class TestPiGpioApiLite(BasePiGpioApiTests, unittest.TestCase):
    """Test suite running against lite.py (zero fastapi/pydantic dependencies)"""

    @classmethod
    def setUpClass(cls):
        cls.start_server("lite.py")

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()


@unittest.skipIf(not HAS_FASTAPI_AND_PYDANTIC, "fastapi and pydantic not installed; skipping main.py")
class TestPiGpioApiMain(BasePiGpioApiTests, unittest.TestCase):
    """Test suite running against main.py (FastAPI / Pydantic)"""

    @classmethod
    def setUpClass(cls):
        cls.start_server("main.py")

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()


if __name__ == "__main__":
    suite = unittest.TestSuite()

    # 1. First run tests on lite.py
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPiGpioApiLite))

    # 2. Then check fastapi and pydantic, if both available, run against main.py
    if HAS_FASTAPI_AND_PYDANTIC:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPiGpioApiMain))
    else:
        print("Note: fastapi and/or pydantic not installed; skipping main.py tests.")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
