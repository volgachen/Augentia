import getpass

from app.auth import hash_password


def main() -> None:
    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if not password:
        raise SystemExit("password must not be empty")
    if password != confirm:
        raise SystemExit("passwords do not match")
    print(hash_password(password))


if __name__ == "__main__":
    main()
