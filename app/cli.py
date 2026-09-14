"""Консольные команды: пользователи, разовый тик, демо-данные.

    python -m app.cli createuser sasha --role buyer
    python -m app.cli passwd sasha
    python -m app.cli users
    python -m app.cli tick
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import ROLE_ADMIN, ROLE_BUYER, ROLE_CEO, User
from app.security import hash_password
from app.services import ServiceError, create_user

ROLES = [ROLE_BUYER, ROLE_ADMIN, ROLE_CEO]


def _ask_password(login: str) -> str:
    first = getpass.getpass(f"Пароль для {login}: ")
    second = getpass.getpass("Повторите: ")
    if first != second:
        sys.exit("Пароли не совпали")
    return first


def cmd_createuser(args: argparse.Namespace) -> None:
    password = args.password or _ask_password(args.login)
    with session_scope() as session:
        try:
            user = create_user(
                session, args.login, password, role=args.role,
                display_name=args.name or "", team=args.team or "",
            )
        except ServiceError as exc:
            sys.exit(str(exc))
        print(f"создан {user.login} · {user.role_title}")


def cmd_passwd(args: argparse.Namespace) -> None:
    password = args.password or _ask_password(args.login)
    if len(password) < 6:
        sys.exit("пароль короче 6 символов")
    with session_scope() as session:
        user = session.scalar(select(User).where(User.login == args.login.strip().lower()))
        if user is None:
            sys.exit(f"нет такого пользователя: {args.login}")
        user.password_hash = hash_password(password)
        print(f"пароль обновлён: {user.login}")


def cmd_users(_: argparse.Namespace) -> None:
    with session_scope() as session:
        users = session.scalars(select(User).order_by(User.login)).all()
        if not users:
            print("пользователей нет")
            return
        for user in users:
            flag = "" if user.is_active else " · отключён"
            print(f"{user.login:16} {user.role_title:10}{flag}")


def cmd_tick(_: argparse.Namespace) -> None:
    from app.engine import AutocontrolEngine

    with session_scope() as session:
        report = AutocontrolEngine().tick(session)
    print(report.as_dict())


def cmd_seed(_: argparse.Namespace) -> None:
    """Демо-данные: пороги из CRM и пара адсетов, чтобы посмотреть интерфейс."""
    from app.models import AdAccount, KeitaroProfile, SocialAccount
    from app.services import attach_adset, upsert_geo_threshold, upsert_user_threshold

    with session_scope() as session:
        admin = session.scalar(select(User).where(User.role.in_([ROLE_ADMIN, ROLE_CEO])))
        if admin is None:
            sys.exit("сначала создайте admin: python -m app.cli createuser admin --role admin")

        for geo, cpa, spend, ucpc in [
            ("ES", 10.00, 4.00, 0.1700),
            ("HU", 10.00, 5.00, 0.1900),
            ("MX", 6.00, 3.00, 0.1200),
            ("RO", 8.00, 3.00, 0.1800),
            ("IT", 11.00, 5.00, 0.2000),
        ]:
            upsert_geo_threshold(session, geo, cpa, spend, ucpc, user=admin)
        upsert_user_threshold(session, admin.id, "IT", 9.00, 4.00, 0.1500)

        keitaro = session.scalar(select(KeitaroProfile))
        if keitaro is None:
            keitaro = KeitaroProfile(
                title="Демо-трекер", base_url="https://tracker.example.com",
                api_key="demo", timezone_name="Europe/Moscow",
            )
            session.add(keitaro)
        social = session.scalar(select(SocialAccount))
        if social is None:
            social = SocialAccount(title="Демо-профиль", access_token="demo-token")
            session.add(social)
        session.flush()

        account = session.scalar(select(AdAccount))
        if account is None:
            account = AdAccount(
                account_id="1790847419029975", title="乐启智抖-2 (10:00)",
                timezone_name="America/Los_Angeles",
                social_id=social.id, keitaro_id=keitaro.id,
            )
            session.add(account)
            session.flush()

        for adset_id, name in [
            ("1001", "eblo2_it_ero-B4"),
            ("1002", "eblo4_it_ero-B5"),
            ("1003", "eblo2_it_ero-B3"),
        ]:
            attach_adset(
                session, adset_id=adset_id, account=account, geo="IT", name=name,
                campaign_name="[AK47] [PO] [it] [eblo2_it_ero]", owner=admin,
            )
        print("демо-данные записаны")


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli", description="Автоконтроль адсетов")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("createuser", help="создать пользователя")
    p.add_argument("login")
    p.add_argument("--password")
    p.add_argument("--role", choices=ROLES, default=ROLE_BUYER)
    p.add_argument("--name")
    p.add_argument("--team")
    p.set_defaults(func=cmd_createuser)

    p = sub.add_parser("passwd", help="сменить пароль")
    p.add_argument("login")
    p.add_argument("--password")
    p.set_defaults(func=cmd_passwd)

    sub.add_parser("users", help="список пользователей").set_defaults(func=cmd_users)
    sub.add_parser("tick", help="прогнать один тик автоконтроля").set_defaults(func=cmd_tick)
    sub.add_parser("seed", help="демо-данные для просмотра интерфейса").set_defaults(func=cmd_seed)

    args = parser.parse_args()
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
