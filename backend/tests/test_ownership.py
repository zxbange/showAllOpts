from test_cache import cached
from datetime import date, datetime
from app.models.ownership import OwnershipStock, OwnershipReport, OwnershipHolders
from app.services.ownership import latest_quarter

def test_api_search_detail_and_social(client,session):
    stamp=datetime(2026,10,21)
    session.add(OwnershipStock(code='600001.XSHG',name='示例公司',industry='行业',updated_at=stamp))
    session.add(OwnershipReport(code='600001.XSHG',report_date=date(2026,9,30),payload={'categories':{},'float_pub_date':'2026-10-20','social_entry':{'status':'comparable','first_appearance':True,'new_combinations':[{'name':'社保一零一','shares':100}],'new_market_value':200}},updated_at=stamp))
    for end,pub,a,total in [(date(2026,6,30),None,100,200),(date(2026,9,30),date(2026,10,20),80,180),(date(2099,9,30),date(2099,10,20),1,1)]:
        session.add(OwnershipHolders(code='600001.XSHG',end_date=end,pub_date=pub,payload={'a_share_holders':a,'share_holders':total},updated_at=stamp))
    session.commit()
    assert client.get('/api/v1/institution-ownership/stocks?q=示例').json()['stocks'][0]['code']=='600001.XSHG'
    assert client.get('/api/v1/institution-ownership/stocks?q=%25').json()['stocks']==[]
    detail=client.get('/api/v1/institution-ownership/stocks/600001').json()
    assert detail['holder_change']==-20 and detail['holder_change_pct']==-20
    assert len(detail['holders'])==2
    result=client.get('/api/v1/institution-ownership/social-entries?period=2026-09-30').json()
    assert len(result['items'])==1 and result['comparable_count']==1
    assert client.get('/api/v1/institution-ownership/stocks/invalid').status_code==422
    assert client.get('/api/v1/institution-ownership/stocks/999999').status_code==404
    assert client.get('/api/v1/institution-ownership/social-entries?period=2026-09-29').status_code==422
    assert client.get('/api/v1/institution-ownership/social-entries?mode=wrong').status_code==422

def test_latest_quarter_does_not_fall_back():
    assert latest_quarter(date(2026,10,21))==date(2026,9,30)
    assert latest_quarter(date(2026,1,1))==date(2025,12,31)

def test_json_incremental_cache_updates_and_preserves_null(cached,session):
    from test_cache_incremental import assert_matches
    _,cache=cached
    stamp=datetime(2026,10,21)
    session.add(OwnershipStock(code='600001.XSHG',name='示例',industry=None,updated_at=stamp))
    report=OwnershipReport(code='600001.XSHG',report_date=date(2026,9,30),payload={'categories':{'social':None}},updated_at=stamp)
    session.add(report);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')
    report.payload={'categories':{'social':{'shares':100}},'social_entry':{'status':'unknown'}}
    report.updated_at=datetime(2026,10,21,1);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')
    session.delete(report);session.commit()
    cache.refresh('institution-ownership',background=False)
    assert_matches(cache,session,'institution-ownership')


def test_social_increase_uses_total_not_new_combinations(client, session):
    stamp = datetime(2026, 10, 21)
    def payload(shares):
        return {'float_complete': True, 'float_members': [
            {'class_id': 307004, 'name': str(i), 'shares': value} for i, value in enumerate(shares)]}
    cases = [([100, 200], [150, 200]), ([100, 200], [100, 150]),
             ([100], [100]), ([], [80]), ([None], [100]), (None, [100])]
    for i, (before, after) in enumerate(cases):
        code = '60000{}.XSHG'.format(i)
        session.add(OwnershipStock(code=code, name='公司{}'.format(i), updated_at=stamp))
        if before is not None:
            session.add(OwnershipReport(code=code, report_date=date(2026, 6, 30), payload=payload(before), updated_at=stamp))
        session.add(OwnershipReport(code=code, report_date=date(2026, 9, 30), payload=payload(after), updated_at=stamp))
    session.commit()
    response = client.get('/api/v1/institution-ownership/social-entries?period=2026-09-30&mode=increase')
    assert response.status_code == 200
    data = response.json()
    assert data['comparable_count'] == 4 and data['unknown_count'] == 2
    assert [r['code'] for r in data['items']] == ['600003.XSHG', '600000.XSHG']
    first, second = data['items']
    assert first['previous_shares'] == 0 and first['change_pct'] is None
    assert second['previous_shares'] == 300 and second['current_shares'] == 350
    assert second['change_shares'] == 50
    assert abs(second['change_pct'] - 100/6) < 1e-8


def test_social_increase_rejects_incomplete_and_invalid_quantities():
    from app.services.ownership import social_increase
    valid = {'float_complete': True, 'float_members': [{'class_id': 307004, 'shares': 100}]}
    for value in (None, -1, 'NaN', 'Infinity'):
        bad = {'float_complete': True, 'float_members': [{'class_id': 307004, 'shares': value}]}
        assert social_increase(valid, bad)['status'] == 'unknown'
    assert social_increase(valid, {'float_complete': False, 'float_members': []})['status'] == 'unknown'
    assert social_increase(valid, {'float_complete': True})['status'] == 'unknown'


def test_social_increase_sums_current_social_ratios_without_affecting_selection():
    from app.services.ownership import social_increase
    previous = {'float_complete': True, 'float_members': [{'class_id': 307004, 'shares': 10}]}
    current = {'float_complete': True, 'float_members': [
        {'class_id': 307004, 'shares': 20, 'ratio': 1.25},
        {'class_id': 307004, 'shares': 30, 'ratio': 0.65},
        {'class_id': 1, 'shares': 1000, 'ratio': 20}]}
    result = social_increase(current, previous)
    assert result['current_ratio'] == 1.9
    assert result['change_shares'] == 40
    for invalid in (None, -1, 'NaN', 'Infinity', 101):
        current['float_members'][1]['ratio'] = invalid
        result = social_increase(current, previous)
        assert result['current_ratio'] is None
        assert result['status'] == 'comparable' and result['change_shares'] == 40


def test_social_increase_accepts_resolved_social_flags_with_other_classes_missing(client, session):
    # Actual regression: the new source report has ten names but no class IDs.
    # The collector can identify social/non-social without classifying every fund.
    stamp = datetime(2026, 10, 26)
    code = '002281.XSHE'
    previous = {'float_complete': True, 'float_members': [
        {'class_id': 307004, 'shares': 1872908}]}
    current = {'float_complete': False, 'social_complete': True, 'float_members': [
        {'class_id': None, 'is_social': False, 'shares': 100, 'ratio': 1}
        for _ in range(9)] + [
        {'class_id': 307004, 'raw_class_id': None, 'is_social': True,
         'shares': 3348837, 'ratio': 0.4045}]}
    session.add(OwnershipStock(code=code, name='regression', updated_at=stamp))
    for period, payload in [(date(2026, 6, 30), previous), (date(2026, 9, 30), current)]:
        session.add(OwnershipReport(code=code, report_date=period, payload=payload, updated_at=stamp))
    session.commit()
    response = client.get('/api/v1/institution-ownership/social-entries?period=2026-09-30&mode=increase')
    assert response.status_code == 200
    result = response.json()
    assert result['comparable_count'] == 1 and result['unknown_count'] == 0
    assert len(result['items']) == 1
    item = result['items'][0]
    assert item['code'] == code
    assert item['previous_shares'] == 1872908
    assert item['current_shares'] == 3348837
    assert item['change_shares'] == 1475929
    assert item['current_ratio'] == 0.4045


def test_social_total_does_not_override_explicit_unknown_flags():
    from app.services.ownership import social_total
    from decimal import Decimal
    known = {'class_id': 307004, 'is_social': True, 'shares': 100}
    valid = {'float_complete': False, 'social_complete': True, 'float_members': [known]}
    assert social_total(valid) == Decimal(100)
    for incomplete in (False, None, 1, 'true'):
        assert social_total(dict(valid, float_complete=True, social_complete=incomplete)) is None
    for unknown in (None, 0, 1, 'false', 'true'):
        # Even a populated class ID cannot override an explicit unresolved flag.
        assert social_total(dict(valid, float_members=[dict(known, is_social=unknown)])) is None
    assert social_total(dict(valid, float_members=[known, {'class_id': None}])) is None
    assert social_total(dict(valid, float_members=[known, None])) is None


def test_resolved_social_flags_preserve_quantity_and_ratio_validation():
    from app.services.ownership import social_increase, social_total
    previous = {'social_complete': True, 'float_members': [
        {'is_social': True, 'class_id': 307004, 'shares': 100}]}
    current = {'social_complete': True, 'float_members': [
        {'is_social': True, 'class_id': 307004, 'shares': 200, 'ratio': 0.5},
        {'is_social': False, 'class_id': None, 'shares': None, 'ratio': None}]}
    assert social_increase(current, previous)['change_shares'] == 100
    for invalid in (None, -1, 'NaN', 'Infinity'):
        current['float_members'][0]['shares'] = invalid
        assert social_increase(current, previous)['status'] == 'unknown'
    current['float_members'][0]['shares'] = 200
    for invalid in (None, -1, 'NaN', 'Infinity', 101):
        current['float_members'][0]['ratio'] = invalid
        result = social_increase(current, previous)
        assert result['status'] == 'comparable' and result['change_shares'] == 100
        assert result['current_ratio'] is None
    assert social_total({'float_complete': True, 'float_members': [
        {'class_id': 307004, 'shares': 100}, {'class_id': 307099, 'shares': None}]}) == 100
