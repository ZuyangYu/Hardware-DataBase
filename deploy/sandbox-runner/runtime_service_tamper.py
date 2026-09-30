#!/usr/bin/env python3
"""Check the live Runner rejects a pre-existing, unreviewed OCI service.

Run only after the reviewed Gateway deployment has been disabled. The test
occupies its container name briefly and always removes the injected instance.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
SERVICE = "hdb-svc-deploy-sandbox-info"
BASE = os.getenv("SANDBOX_RUNNER_URL", "http://127.0.0.1:8780").rstrip("/")


def main() -> None:
    if subprocess.run(["docker", "inspect", SERVICE], capture_output=True).returncode == 0:
        raise RuntimeError("disable the reviewed Gateway service before the tamper test")

    with (HERE / "service-profiles.json").open(encoding="utf-8") as handle:
        catalog = json.load(handle)["deploy/sandbox-info"]
    token = (HERE / ".." / "mcp-gateway" / "secrets" / "runner-token").read_text(encoding="utf-8").strip()
    wrong_image = json.loads((HERE / "bundles" / "report" / "bundle.json").read_text(encoding="utf-8"))["image_digest"]
    assert catalog["image_digest"] != wrong_image

    for image, label, override_entrypoint, extra_network in (
        (wrong_image, "wrong image ID", True, False),
        (catalog["image_digest"], "pinned image with overridden entrypoint", True, False),
        (catalog["image_digest"], "pinned image connected to an egress network", False, True),
    ):
        created_here = False
        network = f"hdb-sandbox-egress-test-{os.getpid()}" if extra_network else ""
        try:
            args = [
                "docker", "run", "-d", "--rm", "--pull", "never", "--name", SERVICE,
                "--label", "hdb.sandbox.runner=1", "--network", "hdb-sandbox-internal",
                "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", f"{os.geteuid()}:{os.getegid()}",
                "--pids-limit", "128", "--memory", "256m", "--cpus", "1",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            ]
            if override_entrypoint:
                args.extend(["--entrypoint", "/usr/local/bin/python3"])
            args.append(image)
            if override_entrypoint:
                args.extend(["-c", "import time; time.sleep(45)"])
            created = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if created.returncode:
                raise RuntimeError("test container could not be started")
            created_here = True
            if extra_network:
                established = subprocess.run(
                    ["docker", "network", "create", network], capture_output=True, timeout=30
                )
                if established.returncode:
                    raise RuntimeError("test egress network could not be created")
                attached = subprocess.run(
                    ["docker", "network", "connect", network, SERVICE], capture_output=True, timeout=30
                )
                if attached.returncode:
                    raise RuntimeError("test egress network could not be attached")

            request = urllib.request.Request(
                BASE + "/v1/services",
                method="POST",
                data=json.dumps({
                    "deployment_ref": "deploy/sandbox-info",
                    "profile_ref": catalog["profile_ref"],
                    "image_digest": catalog["image_digest"],
                }).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Audience": "sandbox-gateway",
                    "Content-Type": "application/json",
                },
            )
            try:
                urllib.request.urlopen(request, timeout=15).close()
            except urllib.error.HTTPError as exc:
                payload = json.load(exc)
                assert exc.code == 503 and payload["detail"]["error"]["code"] == "service_unverified"
            else:
                raise AssertionError(f"Runner accepted a service with {label}")
            print(f"[ok] live Runner rejects same-name OCI service with {label}")
        finally:
            if created_here:
                subprocess.run(["docker", "rm", "-f", SERVICE], capture_output=True, timeout=30)
            if network:
                subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)


if __name__ == "__main__":
    main()
