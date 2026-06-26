"""Start a local Phoenix server for trace inspection.

Thin wrapper around `python -m phoenix.server.main serve` that prints
the direct URL to the finance-tracker project traces before handing
control to the Phoenix process.

Usage:
    uv run --group evals python scripts/start_phoenix.py
"""
import os
import socket
import subprocess
import sys

HTTP_PORT = int(os.environ.get("PHOENIX_PORT", "6006"))
GRPC_PORT = 4317
PROJECT = "finance-tracker"


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def _check_ports() -> None:
    busy = [p for p in (HTTP_PORT, GRPC_PORT) if _port_in_use(p)]
    if not busy:
        return
    ports_str = " and ".join(str(p) for p in busy)
    print(f"ERROR: port {ports_str} is already in use.")
    print("Another Phoenix instance may still be running.")
    print("Stop it first, or kill the process holding the port:")
    for p in busy:
        print(f"  lsof -ti tcp:{p} | xargs kill -9")
    sys.exit(1)


_check_ports()

print()
print("Starting Phoenix...")
print(f"  UI     →  http://localhost:{HTTP_PORT}")
print(f"  Traces →  http://localhost:{HTTP_PORT}/projects/{PROJECT}/traces")
print()
print("To send traces here, set in your .env:")
print(f"  PHOENIX_TRACING=1")
print(f"  PHOENIX_COLLECTOR_ENDPOINT=http://localhost:{HTTP_PORT}/v1/traces")
print()
print("Press Ctrl+C to stop.\n")

os.environ.setdefault("PHOENIX_PORT", str(HTTP_PORT))

proc = subprocess.run(
    [sys.executable, "-m", "phoenix.server.main", "serve"],
)
sys.exit(proc.returncode)
