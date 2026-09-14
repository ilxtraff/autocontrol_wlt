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


def cmd_social_add(args: argparse.Namespace) -> None:
    """Заводит социальный аккаунт из мультитокена."""
    from app.models import SocialAccount
    from app.multitoken import MultitokenError, normalize_proxy, parse_multitoken

    raw = args.multitoken
    if not raw:
        # Просим ввод, а не аргумент: так секрет не осядет в истории команд.
        raw = getpass.getpass("Мультитокен (ввод скрыт): ")
    try:
        parsed = parse_multitoken(raw)
        if args.proxy:
            parsed.proxy = normalize_proxy(args.proxy)
    except MultitokenError as exc:
        sys.exit(str(exc))

    print(f"разобрано: {parsed.describe()}")
    if not parsed.proxy:
        print("  прокси нет — запросы пойдут с IP сервера; это заметно повышает риск чекпоинта")
    if not parsed.has_session:
        print("  кук сессии нет — токен не получится перевыпустить автоматически")

    with session_scope() as session:
        social = session.scalar(select(SocialAccount).where(SocialAccount.title == args.title))
        if social is None:
            social = SocialAccount(title=args.title)
            session.add(social)
        if parsed.access_token:
            social.access_token = parsed.access_token
        if parsed.cookies:
            social.cookies = parsed.cookies
        if parsed.user_agent:
            social.user_agent = parsed.user_agent
        if parsed.proxy:
            social.proxy = parsed.proxy
        if parsed.uid:
            social.fb_user_id = parsed.uid
        social.token_status = "unknown"
        social.proxy_status = "unknown"
    print(f"аккаунт «{args.title}» сохранён. Проверьте: python -m app.cli fb-accounts")


def cmd_doctor(_: argparse.Namespace) -> None:
    """Проверяет всё, что нужно движку, и показывает, что реально видит Keitaro."""
    from datetime import datetime, timezone

    from app.clients.facebook import FacebookError, client_for_social
    from app.clients.keitaro import KeitaroClient, KeitaroError
    from app.config import get_settings
    from app.engine import AutocontrolEngine
    from app.models import (
        AdAccount, ControlledAdset, KeitaroProfile, SocialAccount, utcnow,
    )
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
            client = client_for_social(social, settings)

            if social.proxy:
                try:
                    social.proxy_ip = client.check_proxy()
                    social.proxy_status = "ok" if social.proxy_ip else "unknown"
                    print(
                        f"  [ок]     {social.title}: прокси {social.proxy_label}"
                        f" · выход {social.proxy_ip or '—'}"
                    )
                except FacebookError as exc:
                    social.proxy_status = "invalid"
                    print(f"  [ОШИБКА] {social.title}: {exc}")
                    problems += 1
                    continue
            else:
                print(
                    f"  [!]      {social.title}: прокси не задан — запросы идут с IP сервера"
                )

            token = client.describe_token()
            if token["error"] and social.has_session:
                print("           токен не отвечает, пробую перевыпустить из кук…")
                if engine.refresh_token(social):
                    print("           токен перевыпущен")
                    client = client_for_social(social, settings)
                    token = client.describe_token()
            if token["error"]:
                print(f"  [ОШИБКА] {social.title}: {token['error']}")
                if token["hint"]:
                    print(f"           {token['hint']}")
                if "недоступен" not in token["error"]:
                    social.token_status = "invalid"
                    social.token_error = token["error"]
                    social.token_checked_at = utcnow()
                problems += 1
                continue

            print(f"  [ок]     {social.title}: {token['kind']}, {token['name']}")
            social.token_checked_at = utcnow()

            missing = {"ads_management", "ads_read"} - set(token["permissions"])
            if token["permissions"] and missing:
                print(f"           [ВАЖНО] не хватает прав: {', '.join(sorted(missing))}")
                social.token_status = "no_perms"
                social.token_error = f"нет прав: {', '.join(sorted(missing))}"
                problems += 1
                continue

            accounts_seen, notes = client.list_accounts_verbose()
            if not accounts_seen:
                print(f"           [ВАЖНО] токен не видит ни одного кабинета")
                for note in notes:
                    print(f"           {note}")
                if token["kind"] == "токен системного пользователя":
                    print(
                        "           Нужен токен самого профиля, а не системного "
                        "пользователя — шаг 8 в НАСТРОЙКА.md."
                    )
                social.token_status = "no_accounts"
                social.token_error = "; ".join(notes)[:500]
                problems += 1
                continue

            social.token_status = "ok"
            social.token_error = ""
            print(f"           кабинетов доступно: {len(accounts_seen)}")

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

            field = (account.keitaro.adset_field if account.keitaro
                     else settings.keitaro_adset_field)
            if all(m.unique_clicks == 0 and m.spend == 0 for m in metrics.values()):
                print(f"  {label}: по полю {field} трекер не отдал ничего — ищу верное поле…")
                try:
                    candidates = engine.keitaro_for(account).probe_adset_field(
                        window, [a.adset_id for a in adsets]
                    )
                except KeitaroError as exc:
                    candidates = []
                    print(f"           не смог проверить: {exc}")
                if candidates:
                    print(
                        f"           [ВАЖНО] ID адсетов лежат в {', '.join(candidates)}, "
                        f"а не в {field}."
                    )
                    print(
                        "           Поправьте «Поле с ID адсета» на «Интеграциях» "
                        "и повторите doctor."
                    )
                    problems += 1
                else:
                    print(
                        "           ни в одном sub_id трекер не знает эти ID: проверьте,\n"
                        "           что в ссылке есть adset_id={{adset.id}} и что трафик уже шёл."
                    )
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


def cmd_find_field(args: argparse.Namespace) -> None:
    """Подбирает sub_id, в котором Keitaro хранит ID адсетов."""
    from datetime import datetime, timezone

    from app.clients.keitaro import KeitaroError
    from app.engine import AutocontrolEngine
    from app.models import AdAccount, ControlledAdset
    from app.tzwindow import day_window

    engine = AutocontrolEngine()
    with session_scope() as session:
        account = session.scalar(
            select(AdAccount).where(AdAccount.account_id == args.account.removeprefix("act_"))
        )
        if account is None:
            sys.exit(f"кабинет {args.account} не заведён")

        ids = args.adset or [
            a.adset_id for a in session.scalars(
                select(ControlledAdset).where(ControlledAdset.account_pk == account.id).limit(10)
            ).all()
        ]
        if not ids:
            sys.exit(
                "нечего искать: укажите --adset <ID> или сначала поставьте адсеты под контроль"
            )

        window = day_window(account.timezone_name, now=datetime.now(timezone.utc))
        print(f"ищу ID адсетов {', '.join(ids[:5])} за сутки {window.account_day}…")
        try:
            found = engine.keitaro_for(account).probe_adset_field(window, ids)
        except KeitaroError as exc:
            sys.exit(f"Keitaro: {exc}")

    if not found:
        print(
            "ни в одном sub_id этих ID нет.\n"
            "Проверьте, что в ссылке есть adset_id={{adset.id}}, что параметр\n"
            "замаплен на sub_id в настройках трекера и что по этим адсетам уже был трафик."
        )
        sys.exit(1)
    print(f"нашёл в: {', '.join(found)}")
    print("Впишите это в «Поле с ID адсета» на странице «Интеграции».")


def cmd_fb_accounts(args: argparse.Namespace) -> None:
    """Кабинеты, доступные токену: откуда взять ID и таймзону."""
    from app.clients.facebook import FacebookError, client_for_social
    from app.config import get_settings
    from app.models import SocialAccount, utcnow

    settings = get_settings()
    with session_scope() as session:
        socials = session.scalars(select(SocialAccount)).all()
        if args.social:
            socials = [s for s in socials if s.title == args.social or str(s.id) == args.social]
        if not socials:
            sys.exit("нет подходящего токена")

        for social in socials:
            print(f"== {social.title} ==")
            client = client_for_social(social, settings)

            if social.proxy:
                try:
                    ip = client.check_proxy()
                    social.proxy_ip = ip
                    social.proxy_status = "ok" if ip else "unknown"
                    print(f"  прокси {social.proxy_label} · выход {ip or '—'}")
                except FacebookError as exc:
                    social.proxy_status = "invalid"
                    print(f"  прокси не работает: {exc}")
                    continue
            else:
                print("  прокси не задан — запросы идут с IP сервера")

            token = client.describe_token()
            if token["error"] and social.has_session:
                from app.engine import AutocontrolEngine

                print("  токен не отвечает, пробую перевыпустить из кук…")
                if AutocontrolEngine().refresh_token(social):
                    print("  токен перевыпущен из кук")
                    client = client_for_social(social, settings)
                    token = client.describe_token()
            if token["error"]:
                print(f"  не удалось проверить: {token['error']}")
                if token["hint"]:
                    print(f"  {token['hint']}")
                # Сетевой сбой — не повод объявлять токен мёртвым.
                if "недоступен" not in token["error"]:
                    social.token_status = "invalid"
                    social.token_error = token["error"]
                    social.token_checked_at = utcnow()
                    print("  Возьмите новый токен профиля — шаг 8 в НАСТРОЙКА.md.")
                continue

            print(f"  {token['kind']}: {token['name']} ({token['id']})")
            if token["permissions"]:
                need = {"ads_management", "ads_read"}
                missing = need - set(token["permissions"])
                if missing:
                    print(f"  [ВАЖНО] не хватает прав: {', '.join(sorted(missing))}")
                else:
                    print("  права ads_management и ads_read на месте")

            try:
                accounts, notes = client.list_accounts_verbose()
            except FacebookError as exc:
                print(f"  ошибка: {exc}")
                if exc.hint:
                    print(f"  {exc.hint}")
                continue

            for note in notes:
                print(f"  {note}")

            if not accounts:
                social.token_status = "no_accounts"
                social.token_error = "; ".join(notes)[:500]
                social.token_checked_at = utcnow()
                print("  кабинетов не нашлось.")
                if token["kind"] == "токен системного пользователя":
                    print(
                        "  Это токен системного пользователя. Чтобы работать от имени\n"
                        "  социального аккаунта, выпустите токен самого профиля —\n"
                        "  шаг 8 в НАСТРОЙКА.md."
                    )
                else:
                    print("  Проверьте, что у профиля есть доступ к рекламным кабинетам.")
                continue

            social.token_status = "ok"
            social.token_error = ""
            social.token_checked_at = utcnow()
            print(f"  кабинетов: {len(accounts)}")
            for account in accounts:
                print(
                    f"    {str(account.get('account_id', '')):20} "
                    f"{(account.get('timezone_name') or '—'):26} {account.get('name', '')}"
                )


def _pick_one(session, model, arg, what):
    """Находит запись по названию/id, либо берёт единственную."""
    from sqlalchemy import select as _select

    rows = list(session.scalars(_select(model)))
    if not rows:
        sys.exit(f"нет ни одного {what} — заведите на «Интеграциях»")
    if arg:
        for row in rows:
            if row.title == arg or str(row.id) == arg:
                return row
        sys.exit(f"{what} «{arg}» не найден")
    if len(rows) > 1:
        names = ", ".join(r.title for r in rows)
        sys.exit(f"{what}ов несколько ({names}) — укажите нужный явно")
    return rows[0]


def cmd_import_accounts(args: argparse.Namespace) -> None:
    """Заводит рекламные кабинеты, доступные токену, пачкой."""
    from app.clients.facebook import FacebookError, client_for_social
    from app.config import get_settings
    from app.models import AdAccount, KeitaroProfile, SocialAccount
    from app.tzwindow import UnknownTimezone, resolve_tz

    settings = get_settings()
    with session_scope() as session:
        social = _pick_one(session, SocialAccount, args.social, "аккаунт")
        keitaro = _pick_one(session, KeitaroProfile, args.keitaro, "трекер")

        try:
            accounts = client_for_social(social, settings).list_accounts()
        except FacebookError as exc:
            sys.exit(f"Facebook: {exc}")
        if not accounts:
            sys.exit("токен не вернул ни одного кабинета")

        added = updated = skipped = 0
        for item in accounts:
            acc_id = str(item.get("account_id") or item.get("id", "")).removeprefix("act_")
            if not acc_id:
                continue
            if args.filter and args.filter.lower() not in (item.get("name", "").lower() + acc_id):
                skipped += 1
                continue

            tz = (item.get("timezone_name") or "").strip()
            if tz:
                try:
                    resolve_tz(tz)
                except UnknownTimezone:
                    tz = ""  # незнакомую таймзону не пишем — иначе движок споткнётся

            row = session.scalar(select(AdAccount).where(AdAccount.account_id == acc_id))
            if row is None:
                row = AdAccount(account_id=acc_id)
                session.add(row)
                added += 1
                mark = "завёл  "
            else:
                updated += 1
                mark = "обновил"
            row.title = item.get("name", "") or row.title
            row.timezone_name = tz or row.timezone_name
            row.currency = item.get("currency", "") or row.currency
            row.social_id = social.id
            row.keitaro_id = keitaro.id
            if not args.quiet:
                warn = "" if row.timezone_name else "  <-- без таймзоны, задайте вручную"
                print(f"  {mark}  {acc_id:20} {(row.timezone_name or '—'):24} {row.title}{warn}")

        print(f"\nзавёл: {added}, обновил: {updated}" + (f", пропустил: {skipped}" if skipped else ""))
        session.flush()  # без этого запрос ниже не увидит только что заведённые кабинеты
        no_tz = session.scalars(
            select(AdAccount).where(AdAccount.timezone_name == "")
        ).all()
        if no_tz:
            print(
                f"внимание: у {len(no_tz)} кабинетов нет таймзоны — по ним автоконтроль "
                "не пойдёт, пока не проставите её на «Интеграциях»"
            )


def cmd_keitaro_probe(args: argparse.Namespace) -> None:
    """Показывает сырой ответ Keitaro по адсетам и выносит вердикт по расходу."""
    import json
    from collections import defaultdict
    from datetime import datetime, timezone

    from app.engine import AutocontrolEngine
    from app.models import AdAccount, ControlledAdset
    from app.tzwindow import day_window, to_tracker_range

    def cost_like(row: dict) -> dict:
        """Числовые поля строки, похожие на деньги и не равные нулю."""
        out = {}
        for key, val in row.items():
            if key in ("clicks", "conversions", "leads", "sales") or "click" in key:
                continue
            try:
                num = float(val)
            except (TypeError, ValueError):
                continue
            if num > 0:
                out[key] = num
        return out

    engine = AutocontrolEngine()
    with session_scope() as session:
        account = session.scalar(
            select(AdAccount).where(AdAccount.account_id == args.account.removeprefix("act_"))
        )
        if account is None:
            sys.exit(f"кабинет {args.account} не заведён")
        adsets = session.scalars(
            select(ControlledAdset).where(ControlledAdset.account_pk == account.id).limit(args.limit)
        ).all()
        if not adsets:
            sys.exit("под контролем нет адсетов этого кабинета — сначала import-adsets")

        ids = [a.adset_id for a in adsets]
        keitaro = engine.keitaro_for(account)
        window = day_window(account.timezone_name, now=datetime.now(timezone.utc))
        date_from, date_to = to_tracker_range(window, keitaro.timezone_name)
        field = keitaro.adset_field

        print(f"кабинет {account.account_id}  поле адсета: {field}")
        print(f"окно (пояс трекера {keitaro.timezone_name}): {date_from} .. {date_to}")
        print(f"адсеты: {', '.join(ids)}\n")

        # 1. Как движок: группировка по полю адсета.
        _p, adset_rows = keitaro.build_report(window, ids)
        print("=== 1. группировка по адсету (как движок) ===")
        if not adset_rows:
            print("  0 строк — по этим ID за окно ничего нет")
        for row in adset_rows:
            print(" ", json.dumps(row, ensure_ascii=False))
        adset_cost_fields = set()
        for row in adset_rows:
            adset_cost_fields |= set(cost_like(row))

        # 2. Группировка по адсету + кампании: видно связь и есть ли там расход.
        print("\n=== 2. по адсету + кампании ===")
        _p2, pair_rows = keitaro.build_report(window, ids, grouping=[field, "campaign"])
        adsets_per_campaign = defaultdict(set)
        pair_cost_fields = set()
        for row in pair_rows:
            print(" ", json.dumps(row, ensure_ascii=False))
            adsets_per_campaign[row.get("campaign", "?")].add(row.get(field))
            pair_cost_fields |= set(cost_like(row))

        # 3. Группировка по кампании: там расход обычно и лежит.
        print("\n=== 3. группировка по кампании ===")
        _p3, camp_rows = keitaro.build_report(window, ids, grouping=["campaign"])
        camp_cost_fields = set()
        for row in camp_rows:
            print(" ", json.dumps(row, ensure_ascii=False))
            camp_cost_fields |= set(cost_like(row))

        many = any(len(v) > 1 for v in adsets_per_campaign.values())

        print("\n=== ВЕРДИКТ ===")
        if adset_cost_fields:
            names = ", ".join(sorted(adset_cost_fields))
            if "cost" in adset_cost_fields:
                print("  расход есть на уровне адсета в поле cost — движок должен его видеть.")
                print("  Если doctor всё равно показывает 0 — пришлите вывод, посмотрим окно.")
            else:
                print(f"  расход есть на уровне адсета, но в поле: {names}")
                print(f"  добавлю это имя в разбор — движок начнёт его читать.")
        elif pair_cost_fields:
            names = ", ".join(sorted(pair_cost_fields))
            print(f"  расход виден при группировке адсет+кампания (поле: {names}).")
            print("  подстрою движок на такую группировку.")
        elif camp_cost_fields:
            names = ", ".join(sorted(camp_cost_fields))
            if many:
                print(f"  расход только по кампаниям (поле: {names}), и в кампании НЕСКОЛЬКО адсетов.")
                print("  разнесу расход кампании по адсетам пропорционально кликам.")
            else:
                print(f"  расход только по кампаниям (поле: {names}), 1 кампания = 1 адсет.")
                print("  переключу движок на контроль по кампании — расход сойдётся точно.")
        else:
            print("  расхода нет ни на одном уровне за это окно.")
            print("  Возможно, сейчас просто нет трафика — прогоните команду, когда открутка идёт.")
        print("\nПришлите этот вывод целиком — по нему подстрою движок точно.")


def cmd_import_adsets(args: argparse.Namespace) -> None:
    """Массовая постановка адсетов кабинета под контроль."""
    from app.clients.facebook import FacebookError
    from app.engine import AutocontrolEngine
    from app.models import AdAccount, GeoThreshold, UserGeoThreshold
    from app.naming import detect_geo, geo_candidates
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

        fb_client = engine.facebook_for(account)
        try:
            adsets = fb_client.list_adsets(
                account.account_id, statuses=None if args.all else ["ACTIVE"]
            )
        except FacebookError as exc:
            sys.exit(f"Facebook: {exc}")

        if not adsets and not args.all:
            # Пусто по активным — посмотрим, есть ли вообще адсеты в кабинете,
            # чтобы отличить «нет адсетов» от «нет активных».
            try:
                everything = fb_client.list_adsets(account.account_id, statuses=None)
            except FacebookError:
                everything = []
            if everything:
                from collections import Counter

                by_status = Counter(
                    (a.get("effective_status") or a.get("status") or "?") for a in everything
                )
                breakdown = ", ".join(f"{k}: {v}" for k, v in by_status.most_common())
                print(f"активных адсетов нет. Всего в кабинете {len(everything)} — {breakdown}")
                print("Добавьте флаг --all, чтобы взять не только активные.")
            else:
                print("в кабинете нет ни одного адсета — проверьте, тот ли это кабинет")
            return

        from collections import Counter

        added = skipped = geo_fail = 0
        candidate_counts: Counter = Counter()
        sample_names: list[str] = []
        for item in adsets:
            campaign = item.get("campaign") or {}
            geo = args.geo.upper() if args.geo else detect_geo(
                item.get("name"), campaign.get("name"), known
            )
            if geo is None:
                print(f"  пропуск  {item.get('name')}: не понял гео")
                skipped += 1
                geo_fail += 1
                # Копим, что в этих именах вообще похоже на гео.
                for code in geo_candidates(item.get("name"), campaign.get("name")):
                    candidate_counts[code] += 1
                if len(sample_names) < 5:
                    camp = (item.get("campaign") or {}).get("name", "")
                    sample_names.append(
                        f"{item.get('name', '')}"
                        + (f"  ←  кампания: {camp}" if camp else "")
                    )
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
        if geo_fail and not args.geo:
            print(
                f"у {geo_fail} адсетов гео не распозналось по имени. Задайте всем одно "
                "через --geo XX либо заведите нужные гео в порогах"
            )
            known_codes = {c for c in candidate_counts if c in known}
            new_codes = [(c, n) for c, n in candidate_counts.most_common() if c not in known]
            if new_codes:
                shown = ", ".join(f"{c} (×{n})" for c, n in new_codes[:8])
                print(f"  в именах похоже на гео, но нет в порогах: {shown}")
                print("  если это гео — заведите их в порогах, и они распознаются сами")
            elif not candidate_counts:
                print("  в именах вообще нет двухбуквенных кодов гео — только --geo XX")
                print(f"  примеры имён: {', '.join(n for n in sample_names if n)}")
            elif known_codes:
                # Кандидаты есть и они в порогах — значит имена нестандартные.
                print(f"  примеры имён: {', '.join(n for n in sample_names if n)}")
        if args.dry_run and added:
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

    p = sub.add_parser("social-add", help="завести аккаунт из мультитокена")
    p.add_argument("title", help="название аккаунта, например Камилла")
    p.add_argument("--multitoken", help="лучше не передавать — спросим скрытым вводом")
    p.add_argument("--proxy", help="прокси, если его нет в мультитокене")
    p.set_defaults(func=cmd_social_add)

    p = sub.add_parser("find-field", help="подобрать sub_id с ID адсетов")
    p.add_argument("--account", required=True, help="ID рекламного кабинета")
    p.add_argument("--adset", action="append", help="ID адсета для поиска, можно несколько")
    p.set_defaults(func=cmd_find_field)

    p = sub.add_parser("fb-accounts", help="кабинеты, доступные токену")
    p.add_argument("--social", help="название или id социального аккаунта")
    p.set_defaults(func=cmd_fb_accounts)

    p = sub.add_parser("keitaro-probe", help="сырой ответ Keitaro по адсетам (отладка расхода)")
    p.add_argument("--account", required=True, help="ID рекламного кабинета")
    p.add_argument("--limit", type=int, default=5, help="сколько адсетов показать")
    p.set_defaults(func=cmd_keitaro_probe)

    p = sub.add_parser("import-accounts", help="завести рекламные кабинеты пачкой")
    p.add_argument("--social", help="название или id социального аккаунта")
    p.add_argument("--keitaro", help="название или id трекера")
    p.add_argument("--filter", help="только кабинеты, где имя/ID содержит эту строку")
    p.add_argument("--quiet", action="store_true", help="без построчного вывода")
    p.set_defaults(func=cmd_import_accounts)

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
