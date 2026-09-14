"""Удаление из интеграций: ничего не должно остаться висеть."""
import pytest
from sqlalchemy import select

from app.models import (
    STATE_WATCHING, AdAccount, ControlledAdset, Decision, KeitaroProfile,
    SocialAccount,
)
from app.services import (
    ServiceError, delete_ad_account, delete_keitaro_profile,
    delete_social_account,
)


@pytest.fixture()
def setup(session):
    keitaro = KeitaroProfile(title="Основной", base_url="https://kt", api_key="k")
    social = SocialAccount(title="Камилла")
    social.access_token = "EAAG" + "x" * 40
    social.cookies = "c_user=1; xs=2"
    social.proxy = "http://u:p@1.2.3.4:8080"
    session.add_all([keitaro, social])
    session.flush()
    account = AdAccount(
        account_id="999", title="Кабинет-1", timezone_name="America/Los_Angeles",
        social_id=social.id, keitaro_id=keitaro.id,
    )
    session.add(account)
    session.flush()
    return keitaro, social, account


def add_adset(session, account, adset_id="1001"):
    adset = ControlledAdset(
        adset_id=adset_id, name=f"adset-{adset_id}", geo="IT",
        account_pk=account.id, max_cpa=11.0, max_spend_no_conv=5.0,
        max_ucpc=0.2, state=STATE_WATCHING,
    )
    session.add(adset)
    session.flush()
    session.add(Decision(adset_pk=adset.id, adset_name=adset.name, kind="paused", rule="ucpc"))
    session.flush()
    return adset


# ------------------------------------------------------------------ отказы

def test_keitaro_in_use_is_not_deleted(session, setup):
    keitaro, _, account = setup
    with pytest.raises(ServiceError, match="Кабинет-1"):
        delete_keitaro_profile(session, keitaro)
    assert session.scalar(select(KeitaroProfile)) is not None


def test_social_in_use_is_not_deleted(session, setup):
    _, social, account = setup
    with pytest.raises(ServiceError, match="Кабинет-1"):
        delete_social_account(session, social)
    assert session.scalar(select(SocialAccount)) is not None


def test_error_names_several_accounts(session, setup):
    keitaro, social, account = setup
    session.add(AdAccount(account_id="888", title="Кабинет-2", keitaro_id=keitaro.id))
    session.flush()
    with pytest.raises(ServiceError) as exc:
        delete_keitaro_profile(session, keitaro)
    assert "Кабинет-1" in str(exc.value) and "Кабинет-2" in str(exc.value)


# ---------------------------------------------------------------- удаление

def test_free_keitaro_is_deleted(session, setup):
    keitaro, _, account = setup
    session.delete(account)
    session.flush()
    delete_keitaro_profile(session, keitaro)
    session.flush()
    assert session.scalar(select(KeitaroProfile)) is None


def test_free_social_is_deleted_with_its_secrets(session, setup):
    _, social, account = setup
    session.delete(account)
    session.flush()
    delete_social_account(session, social)
    session.commit()
    assert session.scalar(select(SocialAccount)) is None
    # Секреты не должны пережить удаление записи.
    rows = session.execute(select(SocialAccount.cookies_enc)).all()
    assert rows == []


def test_ad_account_takes_its_adsets_and_journal(session, setup):
    _, _, account = setup
    add_adset(session, account, "1001")
    add_adset(session, account, "1002")
    session.commit()

    removed = delete_ad_account(session, account)
    session.commit()

    assert removed == 2
    assert session.scalar(select(AdAccount)) is None
    assert session.scalars(select(ControlledAdset)).all() == []
    # Журнал уходит вместе с адсетами, иначе остались бы записи в никуда.
    assert session.scalars(select(Decision)).all() == []


def test_deleting_account_keeps_keitaro_and_social(session, setup):
    keitaro, social, account = setup
    add_adset(session, account)
    session.commit()

    delete_ad_account(session, account)
    session.commit()

    assert session.scalar(select(KeitaroProfile)) is not None
    assert session.scalar(select(SocialAccount)) is not None


def test_empty_account_deletes_cleanly(session, setup):
    _, _, account = setup
    assert delete_ad_account(session, account) == 0
    session.commit()
    assert session.scalar(select(AdAccount)) is None


def test_other_accounts_survive(session, setup):
    keitaro, social, account = setup
    other = AdAccount(account_id="777", title="Кабинет-3",
                      social_id=social.id, keitaro_id=keitaro.id)
    session.add(other)
    session.flush()
    add_adset(session, other, "2001")
    add_adset(session, account, "1001")
    session.commit()

    delete_ad_account(session, account)
    session.commit()

    left = session.scalars(select(ControlledAdset)).all()
    assert [a.adset_id for a in left] == ["2001"]
    assert session.scalar(select(AdAccount)).account_id == "777"
