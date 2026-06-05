"""
Generate a JWT bearer token for the OpenShell gateway.

The token is RS256-signed with the swarmer-oidc key stored in the
'swarmer-oidc-signing-key' Kubernetes Secret (or read from a local file).

Usage:
  python3 scripts/openshell_gen_token.py [--days 30]
"""
import argparse
import sys
import time

ISSUER = "http://swarmer-oidc.openshell.svc.cluster.local"
AUDIENCE = "openshell"
KID = "swarmer-oidc-key-1"

# Embedded private key (generated during cluster setup — rotate via make openshell-gen-token)
_PRIVATE_KEY = """***PRIVATE KEY REDACTED***"""


def main():
    parser = argparse.ArgumentParser(description="Generate OpenShell JWT bearer token")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    try:
        import jwt
    except ImportError:
        sys.exit("Missing dependency: pyjwt\nRun: pip install pyjwt")

    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "swarmer",
        "iat": now,
        "exp": now + 86400 * args.days,
        # OpenShell reads roles from realm_access.roles (Keycloak format, default roles_claim)
        "realm_access": {"roles": ["openshell-admin"]},
    }
    token = jwt.encode(payload, _PRIVATE_KEY, algorithm="RS256", headers={"kid": KID})
    print(token)


if __name__ == "__main__":
    main()
