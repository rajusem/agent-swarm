"""
End-to-end smoke test for the OpenShell session launch sequence.

Runs each step of _setup_openshell_sandbox individually against a real
OpenShell gateway, verifying correctness before proceeding. Designed to
be both a debugging tool and a repeatable e2e test.

Usage:
  python3 scripts/openshell_smoke_test.py [--model google/gemini-3.5-flash]

Requirements:
  - OpenShell gateway reachable (OPENSHELL_GATEWAY_URL in .env)
  - swarmer auth/secret.key exists
  - At least one OpencodeSecret in the DB with a Google AI Studio key

Exit 0 = all steps passed. Exit 1 = one or more failures.
"""
import argparse
import asyncio
import json
import re
import shlex
import sys

sys.path.insert(0, ".")

PASS = "✓"
FAIL = "✗"
_results: list[tuple[str, bool, str]] = []


def step(label: str, passed: bool, detail: str = "") -> bool:
    marker = PASS if passed else FAIL
    print(f"  {marker}  {label}", end="")
    if detail:
        print(f" — {detail}", end="")
    print()
    _results.append((label, passed, detail))
    return passed


def _mask(text: str) -> str:
    return re.sub(r'"key":"[^"]{8}[^"]*"', '"key":"****"', text)


async def run_smoke_test(model: str) -> bool:
    from swarmer.crypto import init_crypto
    from swarmer.openshell_client import _get_client, ensure_provider, create_sandbox
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    from swarmer.openshell_policy import build_session_policy
    from openshell._proto import openshell_pb2

    init_crypto("auth/secret.key")

    # ── 1. Read Google API key from DB ───────────────────────────────────────
    print("\n[1] Reading credentials from DB")
    google_key = None
    try:
        from swarmer.database import init_db, get_db
        from sqlalchemy import select
        from swarmer.models.opencode_secret import OpencodeSecret

        init_db("sqlite+aiosqlite:///data/swarmer.db")
        async for db in get_db():
            result = await db.execute(select(OpencodeSecret))
            secret = result.scalars().first()
            if secret:
                google_key = secret.google_api_key
            break
    except Exception as exc:
        step("Read OpencodeSecret from DB", False, str(exc))
        return False

    if not step("Google API key present", bool(google_key),
                f"len={len(google_key) if google_key else 0}"):
        return False

    client = _get_client()
    tool = OpenCodeStrategy()

    # ── 2. Provider setup ────────────────────────────────────────────────────
    print("\n[2] Gateway provider setup")
    provider_name = "swarmer-smoke-test-google"
    try:
        await ensure_provider(provider_name, "google-ai-studio", {},
                              credentials={"GOOGLE_API_KEY": google_key})
        step("CreateProvider/UpdateProvider", True, provider_name)
    except Exception as exc:
        step("CreateProvider/UpdateProvider", False, str(exc))
        return False

    # ── 3. Sandbox creation ──────────────────────────────────────────────────
    print("\n[3] Sandbox creation")

    class _FakeSession:
        language = "golang"
        agent_tool = "opencode"

    policy_yaml = build_session_policy(_FakeSession(), [], [], "opencode", model)
    ref = None
    try:
        ref = await create_sandbox(
            image=tool.get_image(),
            env_vars={},
            policy_yaml=policy_yaml,
            provider_names=[provider_name],
        )
        step("CreateSandbox + WaitReady", True, ref.name)
    except Exception as exc:
        step("CreateSandbox + WaitReady", False, str(exc))
        # Clean up provider and bail
        try:
            client._stub.DeleteProvider(
                openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        except Exception:
            pass
        return False

    sid = ref.id
    sandbox_name = ref.name

    def xec(cmd, timeout=20):
        """Execute a command in the sandbox and return ExecResult."""
        if isinstance(cmd, str):
            return client.exec(sid, ["sh", "-c", cmd], timeout_seconds=timeout)
        return client.exec(sid, cmd, timeout_seconds=timeout)

    all_passed = True

    # ── 4. Provider env injection ────────────────────────────────────────────
    print("\n[4] Provider environment injection")
    try:
        r = xec(["bash", "-i", "-c", "printenv GOOGLE_API_KEY"])
        val = r.stdout.strip()
        is_ref = val.startswith("openshell:resolve:")
        ok = step("GOOGLE_API_KEY is reference token", is_ref,
                  val[:50] if is_ref else f"got: {val!r}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("GOOGLE_API_KEY check", False, str(exc))
        all_passed = False

    # ── 5. Filesystem write access ───────────────────────────────────────────
    print("\n[5] Filesystem permissions")
    for path in ["/sandbox", "/home/sandbox"]:
        try:
            r = xec(f"mkdir -p {path}/.smoke-test && rmdir {path}/.smoke-test && echo ok")
            ok = step(f"{path} writable", r.exit_code == 0 and "ok" in r.stdout,
                      (r.stderr or "").strip()[:80] if r.exit_code != 0 else "")
            all_passed = all_passed and ok
        except Exception as exc:
            step(f"{path} writable", False, str(exc))
            all_passed = False

    # ── 6. model.json ────────────────────────────────────────────────────────
    print("\n[6] Model configuration")
    try:
        model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
        clean_cmd = model_setup_cmd.rstrip().rstrip("&").rstrip()
        r = xec(clean_cmd)
        r2 = xec(["cat", "/sandbox/.local/state/opencode/model.json"])
        model_id = model.split("/", 1)[-1]
        has_model = bool(r2.stdout and model_id in r2.stdout)
        ok = step("model.json written", has_model,
                  r2.stdout.strip()[:80] if has_model
                  else f"exit={r.exit_code} stderr={r.stderr.strip()!r}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("model.json", False, str(exc))
        all_passed = False

    # ── 7. auth.json ─────────────────────────────────────────────────────────
    print("\n[7] Auth configuration (auth.json)")
    try:
        share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")
        clean_share = share_cmd.rstrip().rstrip(";").rstrip()
        r = xec(clean_share)
        r2 = xec(["cat", "/sandbox/.opencode/auth.json"])
        has_auth = bool(r2.stdout and "google" in r2.stdout)
        ok = step("auth.json written with reference token", has_auth,
                  _mask(r2.stdout.strip()) if has_auth
                  else f"exit={r.exit_code} stderr={r.stderr.strip()!r}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("auth.json", False, str(exc))
        all_passed = False

    # ── 8. Container opencode.json preserved ─────────────────────────────────
    print("\n[8] opencode.json (container default)")
    try:
        r = xec(["cat", "/sandbox/opencode.json"])
        cfg = json.loads(r.stdout) if r.stdout else {}
        has_providers = "enabled_providers" in cfg
        ok = step("enabled_providers present", has_providers,
                  str(cfg.get("enabled_providers", "(missing)")))
        all_passed = all_passed and ok
    except Exception as exc:
        step("opencode.json check", False, str(exc))
        all_passed = False

    # ── 9. opencode run ──────────────────────────────────────────────────────
    print("\n[9] opencode prompt execution")
    prompt = "Write Hello World in large ASCII art text. Be brief."

    class _FakeSess:
        mode = "prompt"
        instruction_prompt = ""

    main_cmd = tool.build_main_cmd(_FakeSess(), model, resolved_prompt=prompt)
    print(f"     cmd: {main_cmd}")
    try:
        r = xec(main_cmd, timeout=120)
        output = (r.stdout or "").strip()
        err = (r.stderr or "").replace("/bin/bash: /home/sandbox/.bash_profile: Permission denied\n", "").strip()
        ok_exit = step("opencode exits 0", r.exit_code == 0, f"exit={r.exit_code}")
        ok_out = step("opencode produces output", bool(output), f"{len(output)} chars")
        all_passed = all_passed and ok_exit and ok_out
        if output:
            print(f"\n--- Output (first 800 chars) ---\n{output[:800]}\n---")
        if err:
            print(f"  stderr: {err[:300]}")
    except Exception as exc:
        step("opencode run", False, str(exc))
        all_passed = False

    # ── Cleanup ──────────────────────────────────────────────────────────────
    print("\n[cleanup]")
    try:
        client.delete(sandbox_name)
        step("Delete sandbox", True, sandbox_name)
    except Exception as exc:
        step("Delete sandbox", False, str(exc))
    try:
        client._stub.DeleteProvider(
            openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        step("Delete test provider", True)
    except Exception as exc:
        step("Delete test provider", False, str(exc))

    # ── Summary ──────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        print("\nFailures:")
        for label, ok, detail in _results:
            if not ok:
                print(f"  {FAIL}  {label}" + (f" — {detail}" if detail else ""))
    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenShell e2e smoke test")
    parser.add_argument("--model", default="google/gemini-3.5-flash",
                        help="Model to use (default: google/gemini-3.5-flash)")
    args = parser.parse_args()

    print(f"OpenShell Smoke Test — model: {args.model}")
    ok = asyncio.run(run_smoke_test(args.model))
    sys.exit(0 if ok else 1)
