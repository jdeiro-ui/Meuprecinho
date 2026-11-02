"""Uso: python -m price_watch.cli set-password [usuario]   (cria ou troca a senha; vira administrador)"""

import getpass
import sys

from sqlalchemy import select

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
        user = db.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(username=username)
            db.add(user)
        user.password_hash = hash_password(pw)
        user.is_admin = True
        db.commit()
    print(f"Administrador '{username}' salvo.")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "set-password":
        sys.exit(__doc__)
    set_password(args[1] if len(args) > 1 else input("Usuário: ").strip() or "admin")


if __name__ == "__main__":
    main()
