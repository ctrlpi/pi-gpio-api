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
import threading
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        # A .py sensor, run with the agent's own Python (no shebang, no executable bit)
        cls.test_py_script_name = "test_ci_sensor.py"
        cls.test_py_script_path = os.path.join(cls.sensor_dir, cls.test_py_script_name)
        with open(cls.test_py_script_path, "w") as f:
            f.write("import sys, json\nprint(json.dumps({'value': int(sys.argv[1]) * 2}))\n")

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

        # Clean up test sensor scripts
        for attr in ("test_script_path", "test_py_script_path"):
            if hasattr(cls, attr) and os.path.exists(getattr(cls, attr)):
                try:
                    os.remove(getattr(cls, attr))
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

    def test_bad_api_key_is_ignored(self):
        """An empty, null or non-text api_key on /config/load or /config/update counts as no key:
        the current key keeps working, and no empty, missing or default key gets in"""
        url = f"{self.base_url}/config/read"
        try:
            for bad in ("", None, [], {}, True):
                requests.post(f"{self.base_url}/config/load", headers=self.headers, json={"api_key": bad}, timeout=3)
                requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"api_key": bad}, timeout=3)
                self.assertEqual(requests.get(url, headers=self.headers, timeout=3).status_code, 200, repr(bad))
                for other in ({"Api-Key": ""}, {}, {"Api-Key": "your-secret-key"}):
                    self.assertEqual(requests.get(url, headers=other, timeout=3).status_code, 403, f"{bad!r} {other}")
        finally:
            requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"api_key": self.api_key}, timeout=3)

    def test_bad_api_key_in_config_file_means_default(self):
        """A config.json whose api_key is missing, empty, null or not text starts on the
        default key, never with an open or locked agent"""
        for stored in (None, "", "missing", ["x"]):
            temp = tempfile.mkdtemp(prefix="pi_gpio_key_test_")
            port = find_free_port()
            config = {"name": "key-test"} if stored == "missing" else {"name": "key-test", "api_key": stored}
            with open(os.path.join(temp, "config.json"), "w") as f:
                json.dump(config, f)
            proc = subprocess.Popen([sys.executable, self.script_path, "--port", str(port), "--local"],
                                    cwd=temp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"http://127.0.0.1:{port}"
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        if requests.get(f"{base}/hello", timeout=0.5).status_code == 200:
                            break
                    except requests.RequestException:
                        time.sleep(0.1)
                url = f"{base}/config/read"
                self.assertEqual(requests.get(url, headers={"Api-Key": "your-secret-key"}, timeout=3).status_code, 200, repr(stored))
                for other in ({"Api-Key": ""}, {}):
                    self.assertEqual(requests.get(url, headers=other, timeout=3).status_code, 403, f"{stored!r} {other}")
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                shutil.rmtree(temp, ignore_errors=True)

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

    def test_sensor_python_script(self):
        """A .py sensor script runs with Python and gets its args"""
        r_cfg = requests.post(
            f"{self.base_url}/sensor/config/test_py_sens",
            headers=self.headers,
            json={"script": f"{self.test_py_script_name} 21"},
            timeout=3
        )
        self.assertEqual(r_cfg.status_code, 200)
        r_read = requests.get(f"{self.base_url}/sensor/read/test_py_sens", headers=self.headers, timeout=3)
        self.assertEqual(r_read.status_code, 200)
        self.assertEqual(r_read.json().get("test_py_sens"), 42)
        requests.post(f"{self.base_url}/sensor/config/test_py_sens", headers=self.headers, json={"remove": True}, timeout=3)

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

    def _script(self, action, name, body=None, headers=None):
        url = f"{self.base_url}/script/{action}/{name}"
        if action == "read":
            return requests.get(url, headers=headers or self.headers, timeout=5)
        return requests.post(url, headers=headers or self.headers, json=body, timeout=5)

    def test_script_write_list_read_run_remove(self):
        """/script/* writes, lists, reads, runs (.py with Python, others with sh) and removes a script"""
        py_name, sh_name = "test_ci_script.py", "test_ci_script.sh"
        py_src = "import sys\nprint('hi ' + ' '.join(sys.argv[1:]))\n"
        try:
            r = self._script("write", py_name, {"content": py_src})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": py_name, "size": len(py_src)})

            r = requests.get(f"{self.base_url}/script/list", headers=self.headers, timeout=3)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["write"], "enabled")
            entry = next(e for e in r.json()["scripts"] if e["name"] == py_name)
            self.assertEqual(entry["size"], len(py_src))
            self.assertRegex(entry["modified"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

            r = self._script("read", py_name)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": py_name, "content": py_src})

            r = self._script("run", py_name, {"args": ["a1", 22]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": py_name, "exit_code": 0, "output": "hi a1 22"})

            # Windows line endings and non-ASCII text come back exactly; size counts bytes
            crlf = "# 21.5 \u00b0C\r\nline two\r\nno final newline"
            r = self._script("write", py_name, {"content": crlf})
            self.assertEqual(r.json()["size"], len(crlf.encode("utf-8")))
            self.assertEqual(self._script("read", py_name).json()["content"], crlf)

            # Overwrite, then a shell script with a non-zero exit code and no body
            self.assertEqual(self._script("write", py_name, {"content": "print('v2')\n"}).status_code, 200)
            self.assertEqual(self._script("run", py_name).json()["output"], "v2")
            self.assertEqual(self._script("write", sh_name, {"content": "echo out\nexit 3\n"}).status_code, 200)
            r = requests.post(f"{self.base_url}/script/run/{sh_name}", headers=self.headers, timeout=5)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": sh_name, "exit_code": 3, "output": "out"})

            r = self._script("write", py_name, {"remove": True})
            self.assertEqual(r.json(), {"name": py_name, "status": "removed"})
            self.assertEqual(self._script("read", py_name).status_code, 404)
            self.assertEqual(self._script("run", py_name).status_code, 404)
            self.assertEqual(self._script("write", py_name, {"remove": True}).json()["status"], "not found")
        finally:
            for n in (py_name, sh_name):
                p = os.path.join(self.sensor_dir, n)
                if os.path.exists(p):
                    os.remove(p)

    def test_script_write_refused_with_default_key(self):
        """/script/write answers 403 while api_key is the shipped default"""
        default = {"Api-Key": "your-secret-key"}
        self.assertEqual(requests.post(f"{self.base_url}/config/update", headers=self.headers,
                                       json={"api_key": "your-secret-key"}, timeout=3).status_code, 200)
        try:
            self.assertEqual(self._script("write", "test_ci_default.sh", {"content": "echo x"}, default).status_code, 403)
            r = requests.get(f"{self.base_url}/script/list", headers=default, timeout=3)
            self.assertEqual(r.json()["write"], "default_key")
            self.assertFalse(os.path.exists(os.path.join(self.sensor_dir, "test_ci_default.sh")))
        finally:
            requests.post(f"{self.base_url}/config/update", headers=default, json={"api_key": self.api_key}, timeout=3)

    def test_script_write_lock(self):
        """/script/write can create write.lock, which then blocks every write and can't be changed or removed"""
        lock = os.path.join(self.sensor_dir, "write.lock")
        for name in ("write.lock", "WRITE.LOCK", "Write.Lock"):
            self.assertEqual(self._script("run", name).status_code, 400, name)
        self.assertFalse(os.path.exists(lock))
        try:
            r = self._script("write", "write.lock", {"content": "locked by the API"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(os.path.exists(lock))
            r = requests.get(f"{self.base_url}/script/list", headers=self.headers, timeout=3)
            self.assertEqual(r.json()["write"], "locked")
            self.assertIn("write.lock", [e["name"] for e in r.json()["scripts"]])
            self.assertEqual(self._script("write", "test_ci_locked.sh", {"content": "echo x"}).status_code, 403)
            self.assertEqual(self._script("write", self.test_script_name, {"remove": True}).status_code, 403)
            self.assertEqual(self._script("write", "write.lock", {"remove": True}).status_code, 403)
            self.assertEqual(self._script("write", "write.lock", {"content": ""}).status_code, 403)
            self.assertEqual(self._script("run", "write.lock").status_code, 400)
            self.assertTrue(os.path.exists(lock))
            self.assertTrue(os.path.exists(self.test_script_path))
            self.assertFalse(os.path.exists(os.path.join(self.sensor_dir, "test_ci_locked.sh")))
            # Reads and runs still work while locked
            self.assertEqual(self._script("read", "write.lock").json()["content"], "locked by the API")
            self.assertEqual(self._script("read", self.test_script_name).status_code, 200)
            self.assertEqual(self._script("run", self.test_script_name).json()["exit_code"], 0)
        finally:
            if os.path.exists(lock):
                os.remove(lock)

    def test_script_bad_names_and_args(self):
        """/script/* rejects paths, hidden names and non-alphanumeric args"""
        # main.py's router decodes %2F first: a 404 there, a 400 in lite.py.
        for action in ("read", "write", "run"):
            self.assertIn(self._script(action, "..%2Fmain.py", {"content": "x"}).status_code, (400, 404))
        for bad in [".hidden", "a%20b", "a..%5Cb"]:
            self.assertEqual(self._script("read", bad).status_code, 400, bad)
            self.assertEqual(self._script("write", bad, {"content": "x"}).status_code, 400, bad)
            self.assertEqual(self._script("run", bad).status_code, 400, bad)
        for bad in ["a%5Cb.sh", "-rf", "x%00.sh", "x" * 33]:
            self.assertEqual(self._script("write", bad, {"content": "x"}).status_code, 400, bad)
        # A subfolder is never listed, read, written or run
        sub = os.path.join(self.sensor_dir, "test_ci_dir")
        os.makedirs(sub, exist_ok=True)
        try:
            for action in ("read", "write", "run"):
                self.assertEqual(self._script(action, "test_ci_dir", {"content": "x"}).status_code, 400, action)
            r = requests.get(f"{self.base_url}/script/list", headers=self.headers, timeout=3)
            self.assertNotIn("test_ci_dir", [e["name"] for e in r.json()["scripts"]])
            self.assertEqual(os.listdir(sub), [])
        finally:
            os.rmdir(sub)
        self.assertEqual(self._script("run", self.test_script_name, {"args": ["a;b"]}).status_code, 400)
        self.assertEqual(self._script("run", self.test_script_name, {"args": "a"}).status_code, 422)
        self.assertEqual(self._script("write", "test_ci_x.sh", {}).status_code, 400)
        self.assertEqual(self._script("read", "test_ci_missing.sh").status_code, 404)
        # 32 characters is the longest name accepted
        long_name = "test_ci_" + "x" * 21 + "." + "sh"
        self.assertEqual(len(long_name), 32)
        try:
            self.assertEqual(self._script("write", long_name, {"content": "echo x"}).status_code, 200)
            self.assertEqual(self._script("read", long_name).status_code, 200)
        finally:
            p = os.path.join(self.sensor_dir, long_name)
            if os.path.exists(p):
                os.remove(p)
        self.assertEqual(requests.get(f"{self.base_url}/script/list", timeout=3).status_code, 403)

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

    def test_body_near_cap_accepted(self):
        """A body just under the 16KB cap is read whole and accepted"""
        r = requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"padding": "x" * 16000}, timeout=3)
        self.assertEqual(r.status_code, 200)

    def _chunked_update(self, chunks):
        """POST /config/update as a chunked body (headers first, then the chunks); returns the raw reply.
        Connection: close makes the server close once it has answered, which ends the read. No
        half-close (shutdown SHUT_WR) before reading: uvicorn takes it as the client leaving and
        drops its reply, and no real client does it."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall((f"POST /config/update HTTP/1.1\r\nHost: 127.0.0.1\r\nApi-Key: {self.api_key}\r\n"
                       "Content-Type: application/json\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
            time.sleep(0.2)
            reply = b""
            try:
                s.sendall(b"".join(b"%x\r\n" % len(c) + c + b"\r\n" for c in chunks) + b"0\r\n\r\n")
                while True:
                    part = s.recv(4096)
                    if not part:
                        break
                    reply += part
            except ConnectionError:
                pass    # a refused body may be answered and closed before it is all sent
            return reply

    def test_chunked_body_decoded(self):
        """A chunked body is decoded and applied; one over the 16KB cap is refused (413) and applied nowhere"""
        name = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json().get("name")
        try:
            body = json.dumps({"name": "chunked-ok"}).encode()
            reply = self._chunked_update([body[:7], body[7:]])
            self.assertTrue(reply.startswith(b"HTTP/1.1 200"), reply[:60])
            cfg = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json()
            self.assertEqual(cfg.get("name"), "chunked-ok")

            body = json.dumps({"name": "chunked-big", "padding": "x" * 20000}).encode()
            reply = self._chunked_update([body[i:i + 4000] for i in range(0, len(body), 4000)])
            if reply:
                self.assertTrue(reply.startswith(b"HTTP/1.1 413"), reply[:60])
            cfg = requests.get(f"{self.base_url}/config/read", headers=self.headers, timeout=3).json()
            self.assertEqual(cfg.get("name"), "chunked-ok")
        finally:
            requests.post(f"{self.base_url}/config/update", headers=self.headers, json={"name": name}, timeout=3)


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


class _WebhookSink(BaseHTTPRequestHandler):
    """Keep-alive HTTP/1.1 sink for WebhookSender tests. Records (path, Api-Key, body, client
    port) per delivery. /flaky answers 500 once; /drop closes the connection, without a reply,
    on the second request it gets over one connection (a kept-alive socket the target dropped)."""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.requests_on_conn = getattr(self, "requests_on_conn", 0) + 1
        if self.path == "/drop" and self.requests_on_conn == 2:
            self.close_connection = True
            return
        status = 200
        if self.path == "/flaky" and not self.server.flaky_done:
            self.server.flaky_done = True
            status = 500
        else:
            self.server.received.append((self.path, self.headers.get("Api-Key"), body, self.client_address[1]))
        self.send_response(status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class BaseWebhookSenderTests:
    """WebhookSender: one worker and one kept-alive session per target URL."""
    SERVER_FILE = None

    @classmethod
    def setUpClass(cls):
        # Importing a server creates its GPIOManager, which writes config.json and agent.log
        # into the current folder: keep that in a temporary one for the whole class.
        cls.old_cwd = os.getcwd()
        cls.temp_dir = tempfile.mkdtemp()
        os.chdir(cls.temp_dir)
        path = os.path.join(_script_dir, cls.SERVER_FILE)
        spec = importlib.util.spec_from_file_location("srv_" + cls.SERVER_FILE.replace(".", "_"), path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.sink = ThreadingHTTPServer(("127.0.0.1", 0), _WebhookSink)
        cls.sink.received, cls.sink.flaky_done = [], False
        threading.Thread(target=cls.sink.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.sink.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.sink.shutdown()
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def setUp(self):
        self.sink.received.clear()

    def wait_for(self, path, count, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = [r for r in self.sink.received if r[0] == path]
            if len(got) >= count:
                return got
            time.sleep(0.05)
        return [r for r in self.sink.received if r[0] == path]

    def test_in_order_over_one_connection(self):
        sender = self.mod.WebhookSender(2, 30)
        for i in range(5):
            sender.send(self.base + "/order", {"n": i}, {})
        got = self.wait_for("/order", 5)
        self.assertEqual([r[2]["n"] for r in got], list(range(5)))
        self.assertEqual(len({r[3] for r in got}), 1, "expected one kept-alive connection")

    def test_each_target_gets_only_its_own_key(self):
        sender = self.mod.WebhookSender(2, 30)
        sender.send(self.base + "/a", {"t": "a"}, {"Api-Key": "key-a"})
        sender.send(self.base + "/b", {"t": "b"}, {"Api-Key": "key-b"})
        sender.send(self.base + "/c", {"t": "c"}, {})
        self.assertEqual(self.wait_for("/a", 1)[0][1], "key-a")
        self.assertEqual(self.wait_for("/b", 1)[0][1], "key-b")
        self.assertIsNone(self.wait_for("/c", 1)[0][1])
        sender.send(self.base + "/a", {"t": "a2"}, {"Api-Key": "key-a"})
        self.assertEqual([r[1] for r in self.wait_for("/a", 2)], ["key-a", "key-a"])

    def test_failure_is_retried_after_the_delay(self):
        self.sink.flaky_done = False
        sender = self.mod.WebhookSender(2, 0.5)
        sender.send(self.base + "/flaky", {"n": 1}, {})
        self.assertEqual(self.wait_for("/flaky", 1, timeout=0.3), [])
        got = self.wait_for("/flaky", 1, timeout=5)
        self.assertEqual(len(got), 1)

    def test_dropped_keepalive_connection_is_resent_at_once(self):
        sender = self.mod.WebhookSender(2, 30)   # a queued retry would take 30 s
        sender.send(self.base + "/drop", {"n": 1}, {})
        sender.send(self.base + "/drop", {"n": 2}, {})
        got = self.wait_for("/drop", 2, timeout=5)
        self.assertEqual([r[2]["n"] for r in got], [1, 2])

    def test_idle_target_closes_and_leaves(self):
        cls = self.mod.WebhookSender
        old, cls.IDLE_SECS = cls.IDLE_SECS, 0.3
        try:
            sender = cls(2, 30)
            sender.send(self.base + "/idle", {"n": 1}, {})
            self.wait_for("/idle", 1)
            deadline = time.time() + 5
            while time.time() < deadline and sender.targets:
                time.sleep(0.05)
            self.assertEqual(sender.targets, {})
            sender.send(self.base + "/idle", {"n": 2}, {})   # comes back on demand
            self.assertEqual(len(self.wait_for("/idle", 2)), 2)
        finally:
            cls.IDLE_SECS = old


class TestWebhookSenderLite(BaseWebhookSenderTests, unittest.TestCase):
    SERVER_FILE = "lite.py"


@unittest.skipIf(not HAS_FASTAPI_AND_PYDANTIC, "fastapi and pydantic not installed; skipping main.py")
class TestWebhookSenderMain(BaseWebhookSenderTests, unittest.TestCase):
    SERVER_FILE = "main.py"


class BaseScanShapeTests:
    """/gpio/scan entries: `value` and `reversed` only on a reversed pin. The route answers
    422 off-Pi, so scan_all() is called directly with get_pin_info() faked."""
    SERVER_FILE = None

    @classmethod
    def setUpClass(cls):
        cls.old_cwd = os.getcwd()
        cls.temp_dir = tempfile.mkdtemp()
        os.chdir(cls.temp_dir)
        path = os.path.join(_script_dir, cls.SERVER_FILE)
        spec = importlib.util.spec_from_file_location("scan_" + cls.SERVER_FILE.replace(".", "_"), path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def test_value_and_reversed_only_on_a_reversed_pin(self):
        pins = {"5":  {"gpio": 5,  "value": 1, "level": 1, "type": "input", "pullup": "up"},
                "26": {"gpio": 26, "value": 1, "level": 0, "type": "output", "name": "relay", "reversed": True}}
        def fake_info(gpio, show_level=False):
            return dict(pins[gpio])   # any other pin raises, and scan_all() skips it
        manager = self.mod.manager
        manager.get_pin_info = fake_info
        try:
            entries = {e["gpio"]: e for e in manager.scan_all()}
        finally:
            del manager.get_pin_info
        self.assertEqual(entries[5], {"gpio": 5, "level": 1, "type": "input", "pullup": "up"})
        self.assertEqual(entries[26], {"gpio": 26, "level": 0, "type": "output", "name": "relay",
                                       "value": 1, "reversed": True})


class TestScanShapeLite(BaseScanShapeTests, unittest.TestCase):
    SERVER_FILE = "lite.py"


@unittest.skipIf(not HAS_FASTAPI_AND_PYDANTIC, "fastapi and pydantic not installed; skipping main.py")
class TestScanShapeMain(BaseScanShapeTests, unittest.TestCase):
    SERVER_FILE = "main.py"


if __name__ == "__main__":
    suite = unittest.TestSuite()

    # 1. First run tests on lite.py
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPiGpioApiLite))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestWebhookSenderLite))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestScanShapeLite))

    # 2. Then check fastapi and pydantic, if both available, run against main.py
    if HAS_FASTAPI_AND_PYDANTIC:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestPiGpioApiMain))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestWebhookSenderMain))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestScanShapeMain))
    else:
        print("Note: fastapi and/or pydantic not installed; skipping main.py tests.")

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
