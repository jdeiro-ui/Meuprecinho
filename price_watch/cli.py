"""Uso: python -m price_watch.cli set-password [usuario]"""

import getpass
import sys

from sqlalchemy import delete

from .db import SessionLocal, init_db
from .models import User
from .security import hash_password


def set_password(username: str) -> None:
    pw = getpass.getpass("Nova senha: ")
    if len(pw) < 8:
        sys.exit("A senha precisa ter pelo menos 8 caracteres")
    if pw != getpass.getpass("Repita a senha: "):
        sys.exit("As senhas não conferem")
    init_db()
    with SessionLocal() as db:
        db.execute(delete(User))  # app de usuário único
        db.add(User(username=username, password_hash=hash_password(pw)))
        db.commit()
    print(f"Usuário '{username}' salvo.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "set-password":
        sys.exit(__doc__)
    set_password(args[1] if len(args) > 1 else input("Usuário: ").strip() or "admin")


if __name__ == "__main__":
    main()
