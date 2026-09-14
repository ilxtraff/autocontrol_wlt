"""Пороги: свой перебивает общий, снимок копируется в адсет при заливе."""
import pytest

from app.models import (
    SOURCE_GLOBAL, SOURCE_MANUAL, SOURCE_PERSONAL, STATE_OFF, STATE_WATCHING,
    AdAccount, KeitaroProfile, ROLE_BUYER, ROLE_ADMIN, ROLE_CEO, SocialAccount,
)
from app.rules import Thresholds
from app.services import (
    ServiceError, attach_adset, authenticate, controlled_count_by_geo,
    create_user, detach_adset, normalize_geo, resolve_thresholds,
    upsert_geo_threshold, upsert_user_threshold, delete_user_threshold,
)


@pytest.fixture()
def admin(session):
    return create_user(session, "admin", "secret1", role=ROLE_ADMIN)


@pytest.fixture()
def buyer(session):
    return create_user(session, "sasha", "secret1", role=ROLE_BUYER)


@pytest.fixture()
def account(session):
    kt = KeitaroProfile(title="kt", base_url="https://kt", api_key="k")
    fb = SocialAccount(title="fb", access_token="tok")
    session.add_all([kt, fb])
    session.flush()
    acc = AdAccount(account_id="1", timezone_name="America/Los_Angeles",
                    social_id=fb.id, keitaro_id=kt.id)
    session.add(acc)
    session.flush()
    return acc


# ------------------------------------------------------------------- права

def test_only_admin_and_ceo_edit_global_thresholds(session, admin, buyer):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    ceo = create_user(session, "boss", "secret1", role=ROLE_CEO)
    upsert_geo_threshold(session, "RO", 8.0, 3.0, 0.18, user=ceo)
    with pytest.raises(ServiceError, match="admin и group CEO"):
        upsert_geo_threshold(session, "ES", 10.0, 4.0, 0.17, user=buyer)


# ------------------------------------------------------------- выбор порога

def test_personal_threshold_wins_over_global(session, admin, buyer):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    upsert_user_threshold(session, buyer.id, "IT", 6.0, 3.0, 0.09)

    mine = resolve_thresholds(session, "IT", buyer.id)
    assert mine.source == SOURCE_PERSONAL
    assert mine.thresholds == Thresholds(6.0, 3.0, 0.09)

    theirs = resolve_thresholds(session, "IT", admin.id)
    assert theirs.source == SOURCE_GLOBAL
    assert theirs.thresholds == Thresholds(11.0, 5.0, 0.2)


def test_global_default_applies_when_no_personal_row(session, admin, buyer):
    upsert_geo_threshold(session, "ES", 10.0, 4.0, 0.17, user=admin)
    resolved = resolve_thresholds(session, "ES", buyer.id)
    assert resolved.source == SOURCE_GLOBAL
    assert resolved.thresholds.max_cpa == 10.0


def test_removing_my_geo_falls_back_to_global(session, admin, buyer):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    upsert_user_threshold(session, buyer.id, "IT", 6.0, 3.0, 0.09)
    delete_user_threshold(session, buyer.id, "IT")
    assert resolve_thresholds(session, "IT", buyer.id).source == SOURCE_GLOBAL


def test_geo_must_be_two_letters(session):
    assert normalize_geo(" it ") == "IT"
    with pytest.raises(ServiceError):
        normalize_geo("ITA")
    with pytest.raises(ServiceError):
        normalize_geo("")


# ------------------------------------------------------- снимок при заливе

def test_thresholds_are_copied_into_the_adset(session, admin, buyer, account):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    adset = attach_adset(
        session, adset_id="1001", account=account, geo="IT", name="eblo2_it_ero-B4", owner=buyer
    )
    assert adset.thresholds() == Thresholds(11.0, 5.0, 0.2)
    assert adset.threshold_source == SOURCE_GLOBAL
    assert adset.state == STATE_WATCHING


def test_running_adsets_keep_their_own_numbers(session, admin, buyer, account):
    """Правка справочника влияет на новые заливы, идущие доживают по своим числам."""
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    old = attach_adset(session, adset_id="1001", account=account, geo="IT", owner=buyer)

    upsert_geo_threshold(session, "IT", 4.0, 2.0, 0.05, user=admin)
    new = attach_adset(session, adset_id="1002", account=account, geo="IT", owner=buyer)

    assert old.thresholds() == Thresholds(11.0, 5.0, 0.2)
    assert new.thresholds() == Thresholds(4.0, 2.0, 0.05)


def test_manual_thresholds_are_marked_as_such(session, buyer, account):
    adset = attach_adset(
        session, adset_id="1003", account=account, geo="IT", owner=buyer,
        thresholds=Thresholds(7.0, 2.0, 0.1),
    )
    assert adset.threshold_source == SOURCE_MANUAL


def test_attach_without_any_threshold_is_refused(session, buyer, account):
    with pytest.raises(ServiceError, match="не задано ни одного порога"):
        attach_adset(session, adset_id="1004", account=account, geo="MX", owner=buyer)


# ------------------------------------------------------------------- прочее

def test_detach_keeps_facebook_status_untouched(session, admin, buyer, account):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    adset = attach_adset(session, adset_id="1001", account=account, geo="IT", owner=buyer)
    detach_adset(session, adset, actor="sasha")
    assert adset.state == STATE_OFF
    assert adset.decisions[-1].note.startswith("снят с автоконтроля вручную")


def test_controlled_count_by_geo(session, admin, buyer, account):
    upsert_geo_threshold(session, "IT", 11.0, 5.0, 0.2, user=admin)
    upsert_geo_threshold(session, "RO", 8.0, 3.0, 0.18, user=admin)
    attach_adset(session, adset_id="1", account=account, geo="IT", owner=buyer)
    attach_adset(session, adset_id="2", account=account, geo="IT", owner=buyer)
    attach_adset(session, adset_id="3", account=account, geo="RO", owner=buyer)
    assert controlled_count_by_geo(session) == {"IT": 2, "RO": 1}


def test_authenticate(session):
    create_user(session, "sasha", "secret1")
    assert authenticate(session, "SASHA", "secret1") is not None
    assert authenticate(session, "sasha", "wrong") is None
    assert authenticate(session, "ghost", "secret1") is None


def test_duplicate_login_is_refused(session):
    create_user(session, "sasha", "secret1")
    with pytest.raises(ServiceError, match="уже есть"):
        create_user(session, "sasha", "secret2")


def test_short_password_is_refused(session):
    with pytest.raises(ServiceError, match="короче"):
        create_user(session, "bob", "123")
