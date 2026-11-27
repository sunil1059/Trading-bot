"""Create a dashboard password hash for TB_DASHBOARD_PASSWORD_HASH. Prompts; nothing is echoed or stored."""

import getpass

from backend.api.security import hash_password

if __name__ == "__main__":
    pw = getpass.getpass("Dashboard password (min 12 chars): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        raise SystemExit("passwords differ or too short")
    print(hash_password(pw))
