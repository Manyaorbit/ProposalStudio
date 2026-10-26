"""One-time setup: choose your login and (optionally) store your OpenAI API key.

Your password is never saved. Only a bcrypt hash of it goes into the .env file,
which is created readable by you alone.
"""
import getpass
import os
import sys

import bcrypt

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".env")


def read_env():
    values = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip("'").strip('"')
    return values


def write_env(values):
    lines = ["# Created by setup_credentials.py. Keep this file private; never share or upload it."]
    for k, v in values.items():
        lines.append(f"{k}='{v}'")
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV, 0o600)
    except OSError:
        pass


def main():
    values = read_env()
    print("Proposal Studio setup\n")
    user = input(f"Username [{values.get('APP_USER') or 'owner'}]: ").strip() or values.get("APP_USER") or "owner"
    while True:
        pw = getpass.getpass("Choose a password (12-72 characters): ")
        if len(pw) < 12:
            print("  Too short. Use at least 12 characters (a few random words works well).")
            continue
        if len(pw.encode()) > 72:
            print("  Too long. bcrypt only uses the first 72 bytes; please shorten it.")
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("  They didn't match. Try again.")
            continue
        break
    values["APP_USER"] = user
    values["APP_PASSWORD_HASH"] = bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()

    key = getpass.getpass("OpenAI API key (press Enter to skip or keep the current one): ").strip()
    if key:
        values["OPENAI_API_KEY"] = key
    values.setdefault("OPENAI_MODEL", "gpt-4.1")
    write_env(values)
    print("\nDone. Saved to .env. Start the app with:  python app.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
