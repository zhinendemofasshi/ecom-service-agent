"""Verify the Compose gateway using disposable state and no model calls."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request
from uuid import uuid4


def main(image):
    root = Path(__file__).resolve().parents[1]
    project = "ecom-compose-smoke-" + uuid4().hex[:10]
    token = uuid4().hex
    with TemporaryDirectory(prefix="ecom-compose-") as temporary:
        directory = Path(temporary)
        envfile = directory / "runtime.env"
        auth = {token: {"tenant_id":"smoke", "user_id":"smoke", "actor_id":"smoke",
                        "expires_at":time.time() + 600}}
        envfile.write_text("OPENAI_API_KEY=offline-smoke\nLANGSMITH_TRACING=false\nAUTH_TOKENS="
                           + json.dumps(auth) + "\n")
        envfile.chmod(0o600)
        override = directory / "override.yaml"
        override.write_text('services:\n  gateway:\n    ports: !override ["127.0.0.1::8080"]\n')
        environment = {**os.environ, "ECOM_IMAGE":image, "ECOM_RUNTIME_ENV":str(envfile)}
        command = ["docker", "compose", "--env-file", "/dev/null", "-p", project,
                   "-f", str(root / "compose.yaml"), "-f", str(override)]

        def compose(*args, check=True):
            result = subprocess.run([*command, *args], env=environment,
                                    text=True, capture_output=True)
            if check and result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout

        try:
            compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
            port = compose("port", "gateway", "8080").strip().rsplit(":", 1)[1]
            base = "http://127.0.0.1:" + port

            def request(path, *, authorized=False, data=None):
                headers = {"Content-Type":"application/json"}
                if authorized:
                    headers["Authorization"] = "Bearer " + token
                req = urllib.request.Request(base + path, headers=headers, data=data)
                try:
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    return exc.code, None

            assert request("/health")[0] == 200
            assert request("/ready")[0] == 200
            assert request("/sessions", data=b"{}")[0] == 401
            status, session = request("/sessions", authorized=True, data=b"{}")
            assert status == 201 and session["session_id"]
            with ThreadPoolExecutor(max_workers=32) as pool:
                statuses = list(pool.map(lambda _: request("/health")[0], range(64)))
            assert 429 in statuses, statuses
            assert set(statuses) <= {200, 429}, statuses
            print("Compose smoke passed: gateway, health/readiness, Bearer auth, session creation, rate limit.")
        except Exception:
            print(compose("logs", "--tail", "40", check=False), file=sys.stderr)
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans", check=False)


if __name__ == "__main__":
    main(sys.argv[1])
