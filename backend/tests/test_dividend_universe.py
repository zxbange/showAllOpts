from copy import deepcopy
from datetime import date

import pytest

from app.services.dividend_public_source import DividendSourceError, reconcile_universe

DAY = date(2026, 11, 17)


def pools(side='asharehub', listed='2026-11-17'):
    current = {'600900.SH': dict(symbol='600900.SH', list_date='20000101')}
    independent = [dict(code='600900', exchange='SH', list_date='2000-01-01')]
    if side == 'asharehub':
        current['920309.BJ'] = dict(symbol='920309.BJ', list_date=listed.replace('-', '') if isinstance(listed, str) else listed)
    else:
        independent.append(dict(code='920309', exchange='BJ', list_date=listed))
    return current, independent


@pytest.mark.parametrize('side', ['asharehub', 'eastmoney_selector'])
@pytest.mark.parametrize('listed', ['2026-11-17', '2023-11-18'])
def test_provably_under_three_years_differences_are_excluded(side, listed, caplog):
    current, independent = pools(side, listed)
    before = deepcopy((current, independent))
    with caplog.at_level('INFO'):
        assert set(reconcile_universe(current, independent, DAY)) == {'600900.SH'}
    assert (current, independent) == before
    assert 'listed_under_three_years' in caplog.text and '920309.BJ' in caplog.text


@pytest.mark.parametrize('side', ['asharehub', 'eastmoney_selector'])
@pytest.mark.parametrize('listed', ['2023-11-17', '2020-01-01'])
def test_eligible_missing_security_blocks_and_logs_differences(side, listed, caplog):
    with pytest.raises(DividendSourceError, match='覆盖不一致'):
        reconcile_universe(*pools(side, listed), DAY)
    assert 'eligible_unverified' in caplog.text and '920309.BJ' in caplog.text


@pytest.mark.parametrize('side', ['asharehub', 'eastmoney_selector'])
@pytest.mark.parametrize('listed', [None, '', 'invalid', '2026-02-30', '2026-11-18'])
def test_unverified_dates_never_become_exclusions(side, listed):
    with pytest.raises(DividendSourceError):
        reconcile_universe(*pools(side, listed), DAY)


def test_shared_date_conflict_checked_before_young_exclusion():
    current, independent = pools()
    independent.append(dict(code='920309', exchange='BJ', list_date='2026-11-16'))
    with pytest.raises(DividendSourceError, match='上市日期跨来源不一致'):
        reconcile_universe(current, independent, DAY)


def test_duplicate_independent_codes_remain_errors():
    current, independent = pools()
    independent.append(dict(independent[0]))
    with pytest.raises(DividendSourceError, match='重复'):
        reconcile_universe(current, independent, DAY)


def test_leap_year_third_anniversary_is_not_excluded():
    with pytest.raises(DividendSourceError, match='覆盖不一致'):
        reconcile_universe(*pools(listed='2020-02-29'), date(2023, 2, 28))
