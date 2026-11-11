from copy import deepcopy
from app.services.market_history import audit_historical_membership, category
from app.services.market_sina import SinaDataError
import pytest

DAY = '2026-11-06'


def inputs():
    members = [
        {'symbol': 'sh600001', 'board': '1'}, {'symbol': 'sh689123', 'board': '8'},
        {'symbol': 'sz001234'}, {'symbol': 'sz301234'},
    ]
    for member in members:
        member.update(listed_from='2020-01-01', listed_until=None)
    rows = [{'symbol': member['symbol'], 'date': DAY, 'close': '10', 'volume': '100',
             'amount': '1000', 'paused': 0} for member in members]
    raw = {row['symbol']: deepcopy(row) for row in rows}
    master = {'date': DAY, 'rows': members, 'expected_count': 4,
              'source_archive_evidence': {'date': DAY, 'sha256': 'a' * 64}, 'evidence': {
        'source_counts_verified': True, 'listing_dates_verified': True,
        'current_catalogue_verified': True, 'membership_verified': False,
        'snapshot_date': '2026-11-11'}}
    official = {'date': DAY, 'verified': True, 'counts': {
        'sse_main_a': 1, 'sse_star': 1, 'szse_main_a': 1, 'szse_chinext': 1}}
    return master, rows, raw, {'intervals': {}}, official


def test_complete_identity_and_each_category_count_allow_labeled_reconstruction():
    result = audit_historical_membership(*inputs())
    assert result['membership_verified'] is True
    assert result['historical_reconstruction_verified'] is True
    assert result['historical_membership_verified'] is False
    assert result['joinquant_universe_directly_verified'] is False
    assert result['traded_identity_count'] == 4


def test_explicit_full_day_pause_is_an_identity_but_stays_in_count():
    master, rows, raw, pauses, official = inputs()
    interval = {'start': DAY, 'end': DAY, 'full_day': True}
    rows[1].update(volume='0', amount='0', paused=1, pause_evidence=interval)
    raw.pop(rows[1]['symbol'])
    pauses['intervals'][rows[1]['symbol']] = [interval]
    result = audit_historical_membership(master, rows, raw, pauses, official)
    assert result['traded_identity_count'] == 3 and result['paused_identity_count'] == 1
    assert sum(result['official_counts'].values()) == 4


@pytest.mark.parametrize('change', ['missing_trade', 'undated_trade', 'extra_unknown_code',
                                  'excluded_relisting', 'extra_pause', 'wrong_same_total',
                                  'out_of_listing', 'unproved_pause', 'pause_has_volume', 'wrong_date'])
def test_counts_alone_never_approve_ambiguous_or_mismatched_identities(change):
    master, rows, raw, pauses, official = inputs()
    if change == 'missing_trade':
        raw.pop('sh600001')
    elif change == 'undated_trade':
        raw['sh600001']['date'] = '2026-11-05'
    elif change in ('extra_unknown_code', 'excluded_relisting'):
        raw['sh600999'] = dict(raw['sh600001'], symbol='sh600999')
        if change == 'excluded_relisting':
            master['catalogue_rows'] = [{'symbol': 'sh600999', 'listed_from': '2026-11-10'}]
    elif change == 'extra_pause':
        pauses['intervals']['sz001999'] = [{'start': DAY, 'end': DAY, 'full_day': True}]
    elif change == 'wrong_same_total':
        official['counts'].update(sse_main_a=2, sse_star=0)
    elif change == 'out_of_listing':
        master['rows'][0]['listed_from'] = '2026-11-10'
    elif change == 'unproved_pause':
        rows[0].update(volume='0', amount='0', paused=1)
        raw.pop('sh600001')
    elif change == 'pause_has_volume':
        rows[0].update(volume='0', amount='0', paused=1,
                       pause_evidence={'start': DAY, 'end': DAY, 'full_day': True})
    elif change == 'wrong_date':
        official['date'] = '2026-11-05'
    with pytest.raises(SinaDataError):
        audit_historical_membership(master, rows, raw, pauses, official)


def test_exclusive_delist_day_cannot_be_counted():
    master, *rest = inputs()
    master['rows'][0].update(listed_until=DAY, listed_until_exclusive=True)
    with pytest.raises(SinaDataError, match='上市区间'):
        audit_historical_membership(master, *rest)


def test_no_fixed_year_or_recent_twenty_day_cutoff():
    master, rows, raw, pauses, official = inputs()
    old = '2018-06-01'
    master['date'] = official['date'] = old
    master['source_archive_evidence']['date'] = old
    for member in master['rows']:
        member['listed_from'] = '2010-01-01'
    for row in rows + list(raw.values()):
        row['date'] = old
    assert audit_historical_membership(master, rows, raw, pauses, official)['target_date'] == old


def test_future_cdr_code_is_classified_by_official_sse_board_not_specific_symbol():
    assert category({'symbol': 'sh699999', 'board': '8'}) == 'sse_star'
    with pytest.raises(SinaDataError, match='分类'):
        category({'symbol': 'sz499999'})


def test_extra_equity_with_amount_but_no_volume_still_blocks():
    master, rows, raw, pauses, official = inputs()
    raw['sh600999'] = dict(raw['sh600001'], symbol='sh600999', volume='0')
    with pytest.raises(SinaDataError, match='之外'):
        audit_historical_membership(master, rows, raw, pauses, official)


def test_evidence_retains_archive_and_complete_suspension_scan():
    master, rows, raw, pauses, official = inputs()
    pauses['raw'] = {'requests': [{'sha256': 'b' * 64}], 'rows': []}
    result = audit_historical_membership(master, rows, raw, pauses, official)
    assert result['source_archive_evidence']['sha256'] == 'a' * 64
    assert result['suspension_scan_evidence']['raw']['requests'][0]['sha256'] == 'b' * 64
