"""
Smoke test for the OpenShell gateway.

Reads connection config from .env (same as swarmer), then:
  1. Creates a sandbox
  2. Runs `echo hello` inside it
  3. Deletes the sandbox

Usage:
  python3 scripts/openshell_smoke_test.py [--gateway localhost:17670]
"""
import argparse
import os
import pathlib
import sys


def _load_env():
    env_file = pathlib.Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def _wait_ready(client, sandbox_name: str, timeout_seconds: int = 300):
    """Wait for sandbox to be ready, checking Kubernetes-style conditions."""
    import time
    from openshell._proto import openshell_pb2

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        ref = client.get(sandbox_name)
        # SDK phase field (numeric) may not be set by 0.0.51 gateway — check conditions too
        if ref.phase == openshell_pb2.SANDBOX_PHASE_READY:
            return ref
        if ref.phase == openshell_pb2.SANDBOX_PHASE_ERROR:
            raise RuntimeError(f"sandbox {sandbox_name} entered error phase")
        # Fallback: read raw proto and check conditions
        resp = client._stub.GetSandbox(
            openshell_pb2.GetSandboxRequest(name=sandbox_name), timeout=10
        )
        for cond in resp.sandbox.status.conditions:
            if cond.type == "Ready" and cond.status == "True":
                return ref
        time.sleep(2)
    raise RuntimeError(f"sandbox {sandbox_name} not ready within {timeout_seconds}s")


def main():
    _load_env()

    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default=os.environ.get("OPENSHELL_GATEWAY_URL", "localhost:17670"))
    parser.add_argument("--tls-ca", default=os.environ.get("OPENSHELL_TLS_CA", ""))
    parser.add_argument("--tls-cert", default=os.environ.get("OPENSHELL_TLS_CERT", ""))
    parser.add_argument("--tls-key", default=os.environ.get("OPENSHELL_TLS_KEY", ""))
    parser.add_argument("--token", default=os.environ.get("OPENSHELL_BEARER_TOKEN", ""))
    args = parser.parse_args()

    try:
        from openshell import SandboxClient, TlsConfig
    except ImportError:
        sys.exit("Missing dependency: pip install openshell")

    print(f"Connecting to {args.gateway} ...")

    tls = None
    if args.tls_ca:
        tls = TlsConfig(
            ca_path=pathlib.Path(args.tls_ca),
            cert_path=pathlib.Path(args.tls_cert),
            key_path=pathlib.Path(args.tls_key),
        )

    client = SandboxClient(
        args.gateway,
        tls=tls,
        bearer_token=args.token or None,
    )

    print("Creating sandbox ...")
    ref = client.create()
    print(f"Created sandbox: {ref.name}")

    print("Waiting for ready (first run may take ~3 min for image pull) ...")
    ref = _wait_ready(client, ref.name, timeout_seconds=300)

    print("Running 'echo hello' ...")
    result = client.exec(ref.id, ["echo", "hello"])
    print(f"Exec result: {result}")
    if result.exit_code != 0 or "hello" not in result.stdout:
        print("FAIL: unexpected exec result")
        client.delete(ref.name)
        sys.exit(1)

    print("Deleting sandbox ...")
    deleted = client.delete(ref.name)
    print(f"Deleted: {deleted}")

    client.close()
    print("OK")


if __name__ == "__main__":
    main()
