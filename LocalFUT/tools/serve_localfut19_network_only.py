#!/usr/bin/env python3
"""Run only the build-independent LocalFUT19 network services."""

from __future__ import annotations

import os
import sys
import threading
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(ROOT, "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import local_fut19_server as server


def main() -> int:
    try:
        runtime_profile = server.configure_runtime_adapter("network-only")
    except RuntimeError as exc:
        server.log("compat", "REFUSED network-only startup: %s" % exc)
        return 5
    log_path = os.environ.get("LOCALFUT19_NETWORK_LOG", "").strip()
    if log_path:
        server.LOGFILE = os.path.abspath(log_path)
        os.makedirs(os.path.dirname(server.LOGFILE), exist_ok=True)
    occupied = server.occupied_service_ports()
    if occupied:
        server.log(
            "main",
            "network-only startup refused; occupied ports: %s"
            % ", ".join(str(port) for port in occupied),
        )
        return 4
    server.reset_server_log()
    server.ensure_cert()
    for function in (
        server.start_redirector,
        server.start_blaze,
        server.start_fut,
    ):
        threading.Thread(target=function, daemon=True).start()
        time.sleep(0.3)
    server.log(
        "main",
        "network-only services started; profile=%s native=%s routes=%s dto=%s"
        % (runtime_profile.profile_id, runtime_profile.native_adapter_id,
           runtime_profile.route_adapter_id, runtime_profile.dto_adapter_id),
    )
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.log("main", "network-only services stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
