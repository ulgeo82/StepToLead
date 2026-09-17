"""Локальная команда, не HTTP-регистрация: python -m app.setup_admin."""
import asyncio
import getpass
import argparse
import os
import secrets
from pathlib import Path

from sqlalchemy import select

from app.core.access import hash_password
from app.db import Base, SessionLocal, engine
from app.models.access import AdminUser


async def setup():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-file", help="Создать admin со случайным паролем; сохранить в приватный файл. Только если пользователей ещё нет.")
    args = parser.parse_args()
    if args.bootstrap_file:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with SessionLocal() as db:
            if await db.scalar(select(AdminUser.id).limit(1)):
                print("Администратор уже настроен. Существующие данные не изменены.")
                return
            password = secrets.token_urlsafe(24)
            destination = Path(args.bootstrap_file)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL исключает перезапись существующего файла/симлинка.
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write("StepToLead — первоначальный доступ\nАдрес: http://localhost:3000/login\nЛогин: admin\nПароль: " + password + "\n\nХраните пароль в менеджере паролей. Не отправляйте этот файл другим людям.\n")
            db.add(AdminUser(username="admin", password_hash=hash_password(password), role="admin"))
            await db.commit()
        await engine.dispose()
        print("Администратор создан. Пароль сохранён только в указанном приватном файле.")
        return
    username = input("Логин администратора [admin]: ").strip().lower() or "admin"
    password = getpass.getpass("Новый пароль (не менее 12 символов): ")
    if len(password) < 12 or len(password) > 256 or password != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают или длина вне диапазона 12–256 символов")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        user = await db.scalar(select(AdminUser).where(AdminUser.username == username))
        if user:
            raise SystemExit("Такой пользователь уже есть. Пароль не изменён.")
        db.add(AdminUser(username=username, password_hash=hash_password(password), role="admin"))
        await db.commit()
    await engine.dispose()
    print("Администратор создан. Войдите через /login.")


if __name__ == "__main__":
    asyncio.run(setup())
