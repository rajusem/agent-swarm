#!/usr/bin/env python3
"""Set up the dashboard password hash. Called by: make setup-auth"""
import getpass
import sys
from pathlib import Path

try:
    from argon2 import PasswordHasher
except ImportError:
    sys.exit("argon2-cffi is not installed. Run: pip install argon2-cffi")

pw = getpass.getpass("Password: ")
pw2 = getpass.getpass("Confirm password: ")
if pw != pw2:
    sys.exit("Passwords do not match.")
if not pw:
    sys.exit("Password cannot be empty.")

Path("auth").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)
Path("auth/password.hash").write_text(PasswordHasher().hash(pw))
print("Hash written to auth/password.hash")
