"""Offline Docker smoke: non-root, read-only root, readiness and durable restart."""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, text=True, capture_output=True)


def main(image):
    name = "ecom-smoke-" + uuid4().hex[:12]
    volume = name + "-state"
    token = uuid4().hex
    auth = json.dumps({token: {"tenant_id": "smoke", "user_id": "smoke",
                              "actor_id": "smoke", "expires_at": time.time() + 600}})
    docker("volume", "create", volume)

    def launch():
        docker("run", "-d", "--name", name, "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges:true", "--tmpfs", "/tmp",
               "-v", volume + ":/srv/ecom/app/sessions",
               "-p", "127.0.0.1::8000", "-e", "OPENAI_API_KEY=offline-smoke",
               "-e", "AUTH_TOKENS=" + auth, "-e", "LANGSMITH_TRACING=false", image)
        port = json.loads(docker("inspect", name).stdout)[0]["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"]
        return "http://127.0.0.1:" + port

    def request(base, path, method="GET"):
        data = b"{}" if method == "POST" else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as result:
            return json.load(result)

    def ready(base):
        for _ in range(60):
            try:
                if request(base, "/ready")["status"] == "ready":
                    return
            except (OSError, urllib.error.HTTPError):
                time.sleep(0.5)
        raise RuntimeError("Container did not become ready")

    try:
        base = launch()
        ready(base)
        assert docker("exec", name, "id", "-u").stdout.strip() == "10001"
        assert docker("exec", name, "touch", "/srv/ecom/forbidden", check=False).returncode != 0
        sid = request(base, "/sessions", "POST")["session_id"]
        docker("stop", "--time", "80", name)
        assert json.loads(docker("inspect", name).stdout)[0]["State"]["ExitCode"] == 0
        docker("rm", name)
        base = launch()
        ready(base)
        # Persisted session authorization/lookup, without constructing a model run.
        request(base, "/sessions/" + sid + "/handoffs")
        print("Container smoke passed: readiness, non-root, read-only, stop, persisted session.")
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main(sys.argv[1])
