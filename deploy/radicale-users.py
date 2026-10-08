"""Writes the Radicale user file at container start. No plaintext in the output."""

import os
import sys
from pathlib import Path

import bcrypt


def require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"{name} is required")
    if ":" in value or "\n" in value or "\r" in value:
        sys.exit(f"{name} contains a character that cannot appear in the user file")
    return value


def line(user: str, password: str) -> str:
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
    return f"{user}:{hashed.decode('ascii')}"


def main() -> None:
    owner = require("HUB_USER")
    owner_password = require("HUB_PASSWORD")
    internal = require("HUB_INTERNAL_USER")
    internal_password = require("HUB_INTERNAL_PASSWORD")
    if owner == internal:
        sys.exit("HUB_INTERNAL_USER must differ from HUB_USER")

    path = Path("/data/users")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        line(owner, owner_password)
        + "\n"
        + line(internal, internal_password)
        + "\n"
    )
    path.write_text(content, encoding="ascii")
    os.chmod(path, 0o600)


if __name__ == "__main__":
    main()
