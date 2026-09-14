"""Консольные команды: пользователи, разовый тик, демо-данные.

    python -m app.cli createuser sasha --role buyer
    python -m app.cli passwd sasha
    python -m app.cli users
    python -m app.cli doctor
    python -m app.cli import-adsets --account 1790847419029975
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


def cmd_doctor(_: argparse.Namespace) -> None:
    """Проверяет всё, что нужно движку, и показывает, что реально видит Keitaro."""
    from datetime import datetime, timezone

    from app.clients.facebook import FacebookClient, FacebookError
    from app.clients.keitaro import KeitaroClient, KeitaroError
    from app.config import get_settings
    from app.engine import AutocontrolEngine
    from app.models import AdAccount, ControlledAdset, KeitaroProfile, SocialAccount
    from app.tzwindow import UnknownTimezone, day_window, describe_offset, to_tracker_range

    settings = get_settings()
    engine = AutocontrolEngine()
    now = datetime.now(timezone.utc)
    problems = 0

    with session_scope() as session:
        print("== Keitaro ==")
        profiles = session.scalars(select(KeitaroProfile)).all()
        if not profiles:
            print("  нет ни одного трекера — добавьте на странице «Интеграции»")
            problems += 1
        for profile in profiles:
            try:
                KeitaroClient(
                    base_url=profile.base_url,
                    api_key=profile.api_key,
                    timezone_name=profile.timezone_name,
                    adset_field=profile.adset_field,
                    timeout=settings.keitaro_timeout,
                ).ping()
                print(f"  [ок]     {profile.title}: отвечает, поле адсета {profile.adset_field}")
            except KeitaroError as exc:
                print(f"  [ОШИБКА] {profile.title}: {exc}")
                problems += 1

        print("\n== Токены Facebook ==")
        socials = session.scalars(select(SocialAccount)).all()
        if not socials:
            print("  нет ни одного токена — добавьте на странице «Интеграции»")
            problems += 1
        for social in socials:
            try:
                who = FacebookClient(
                    social.access_token, settings.fb_api_version, settings.fb_timeout
                ).check_token()
                print(f"  [ок]     {social.title}: живой, {who.get('name')} ({who.get('id')})")
            except FacebookError as exc:
                print(f"  [ОШИБКА] {social.title}: {exc}")
                problems += 1

        print("\n== Кабинеты и сутки ==")
        accounts = session.scalars(select(AdAccount)).all()
        if not accounts:
            print("  кабинетов нет")
            problems += 1
        for account in accounts:
            label = account.title or account.account_id
            try:
                window = day_window(account.timezone_name, now=now)
            except UnknownTimezone as exc:
                print(f"  [ОШИБКА] {label}: {exc}")
                problems += 1
                continue
            tracker_tz = account.keitaro.timezone_name if account.keitaro else settings.keitaro_timezone
            start, end = to_tracker_range(window, tracker_tz)
            print(f"  [ок]     {label}: {describe_offset(account.timezone_name, now)}")
            print(f"           сутки {window.account_day} -> в {tracker_tz}: {start} .. {end}")

        print("\n== Что видит Keitaro по адсетам ==")
        for account in accounts:
            adsets = session.scalars(
                select(ControlledAdset).where(ControlledAdset.account_pk == account.id).limit(5)
            ).all()
            if not adsets:
                continue
            label = account.title or account.account_id
            try:
                window = day_window(account.timezone_name, now=now)
                metrics = engine.keitaro_for(account).fetch_adset_metrics(
                    window, [a.adset_id for a in adsets]
                )
            except (KeitaroError, UnknownTimezone) as exc:
                print(f"  [ОШИБКА] {label}: {exc}")
                problems += 1
                continue

            print(f"  {label}:")
            for adset in adsets:
                row = metrics.get(adset.adset_id)
                flags = []
                if row.spend == 0:
                    flags.append("нет расхода")
                if row.unique_clicks == 0:
                    flags.append("нет уников")
                mark = "  <-- " + ", ".join(flags) if flags else ""
                print(
                    f"    {adset.name or adset.adset_id:26} расход ${row.spend:>8.2f} "
                    f"конв. {row.conversions:>3} уники {row.unique_clicks:>5}{mark}"
                )

    print()
    if problems:
        print(f"Проблем: {problems}. Движок не сможет работать, пока они есть.")
        sys.exit(1)
    print("Всё на месте.")
    print(
        "Если расход и уники везде нулевые, а трафик идёт — значит, Keitaro не получает\n"
        "расход или ID адсета. Проверьте макрос в ссылке и передачу расхода."
    )


def cmd_fb_accounts(args: argparse.Namespace) -> None:
    """Кабинеты, доступные токену: откуда взять ID и таймзону."""
    from app.clients.facebook import FacebookClient, FacebookError
    from app.config import get_settings
    from app.models import SocialAccount

    settings = get_settings()
    with session_scope() as session:
        socials = session.scalars(select(SocialAccount)).all()
        if args.social:
            socials = [s for s in socials if s.title == args.social or str(s.id) == args.social]
        if not socials:
            sys.exit("нет подходящего токена")

        for social in socials:
            print(f"== {social.title} ==")
            try:
                accounts = FacebookClient(
                    social.access_token, settings.fb_api_version, settings.fb_timeout
                ).list_accounts()
            except FacebookError as exc:
                print(f"  ошибка: {exc}")
                continue
            for account in accounts:
                print(
                    f"  {account.get('account_id', ''):20} "
                    f"{(account.get('timezone_name') or '—'):26} {account.get('name', '')}"
                )


def cmd_import_adsets(args: argparse.Namespace) -> None:
    """Массовая постановка адсетов кабинета под контроль."""
    from app.clients.facebook import FacebookError
    from app.engine import AutocontrolEngine
    from app.models import AdAccount, GeoThreshold, UserGeoThreshold
    from app.naming import detect_geo
    from app.services import ServiceError, attach_adset

    engine = AutocontrolEngine()
    with session_scope() as session:
        account = session.scalar(
            select(AdAccount).where(AdAccount.account_id == args.account.removeprefix("act_"))
        )
        if account is None:
            sys.exit(f"кабинет {args.account} не заведён — добавьте его на «Интеграциях»")

        owner = None
        if args.user:
            owner = session.scalar(select(User).where(User.login == args.user.strip().lower()))
            if owner is None:
                sys.exit(f"нет такого пользователя: {args.user}")

        known = {g for (g,) in session.execute(select(GeoThreshold.geo)).all()}
        if owner is not None:
            known |= {
                g for (g,) in session.execute(
                    select(UserGeoThreshold.geo).where(UserGeoThreshold.user_id == owner.id)
                ).all()
            }
        if not known:
            sys.exit("не задано ни одного порога — сначала заполните пороги по гео")

        try:
            adsets = engine.facebook_for(account).list_adsets(
                account.account_id, statuses=None if args.all else ["ACTIVE"]
            )
        except FacebookError as exc:
            sys.exit(f"Facebook: {exc}")

        added = skipped = 0
        for item in adsets:
            campaign = item.get("campaign") or {}
            geo = args.geo.upper() if args.geo else detect_geo(
                item.get("name"), campaign.get("name"), known
            )
            if geo is None:
                print(f"  пропуск  {item.get('name')}: не понял гео")
                skipped += 1
                continue
            if args.dry_run:
                print(f"  поставил {item.get('name'):34} {geo}")
                added += 1
                continue
            try:
                attach_adset(
                    session, adset_id=item["id"], account=account, geo=geo,
                    name=item.get("name", ""), campaign_id=campaign.get("id", ""),
                    campaign_name=campaign.get("name", ""), owner=owner,
                )
            except ServiceError as exc:
                print(f"  пропуск  {item.get('name')}: {exc}")
                skipped += 1
                continue
            print(f"  поставил {item.get('name'):34} {geo}")
            added += 1

        word = "поставил бы" if args.dry_run else "поставил"
        print(f"\n{word} под контроль: {added}, пропустил: {skipped}")
        if args.dry_run:
            print("это была примерка — повторите без --dry-run")


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
    sub.add_parser("doctor", help="проверить интеграции и что видит Keitaro").set_defaults(
        func=cmd_doctor
    )

    p = sub.add_parser("fb-accounts", help="кабинеты, доступные токену")
    p.add_argument("--social", help="название или id социального аккаунта")
    p.set_defaults(func=cmd_fb_accounts)

    p = sub.add_parser("import-adsets", help="поставить адсеты кабинета под контроль")
    p.add_argument("--account", required=True, help="ID рекламного кабинета")
    p.add_argument("--geo", help="задать гео всем вместо определения по имени")
    p.add_argument("--user", help="чьи личные пороги применять")
    p.add_argument("--all", action="store_true", help="не только активные адсеты")
    p.add_argument("--dry-run", action="store_true", help="показать, но не сохранять")
    p.set_defaults(func=cmd_import_adsets)

    args = parser.parse_args()
    init_db()
    args.func(args)


if __name__ == "__main__":
    main()
