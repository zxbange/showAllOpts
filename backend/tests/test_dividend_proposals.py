from datetime import date
from decimal import Decimal
import pytest
from app.services.dividend_proposals import ProposalError, proposal_totals

DAY=date(2026,11,16)

def event(period='2025-12-31', dps='1.003', announced='2026-03-26', **extra):
    return dict(dict(symbol='600036.SH', period=period, announced=announced, dps=dps,
                     status='active', event_id=period, annual_final=period.endswith('12-31')), **extra)


def test_interim_does_not_switch_fiscal_year_and_final_not_total_year_again():
    rows=[event(), event('2025-06-30','1.013','2025-08-29'), event('2026-06-30','1.1','2026-08-29')]
    result=proposal_totals(rows, DAY)['600036.SH']
    assert result['fiscal_year']==2025
    assert result['annual_dps'][2025]==Decimal('2.016')
    assert result['annual_dps'][2026]==Decimal('1.1')


def test_revision_replaces_not_adds_and_zero_final_still_selects_year():
    rows=[event(), event(dps='1.2', announced='2026-04-01'), event('2026-12-31','0','2027-03-20',status='zero'),
          event('2026-06-30','1.1','2026-08-29')]
    result=proposal_totals(rows,date(2027,3,20))['600036.SH']
    assert result['fiscal_year']==2026 and result['annual_dps'][2026]==Decimal('1.1')
    assert result['annual_dps'][2025]==Decimal('1.2')


@pytest.mark.parametrize('updates',[{'dps':None},{'dps':'NaN'},{'dps':-1},{'announced':'2027-01-01'}, {'status':'unknown'}])
def test_invalid_not_inferred_zero(updates):
    with pytest.raises(ProposalError):proposal_totals([event(**updates)],DAY)


def test_conflicting_same_publication_rejected():
    with pytest.raises(ProposalError):proposal_totals([event(),event(dps='2')],DAY)


def test_live_public_samples_preserve_quarterly_and_zero_rollover():
    import json
    from pathlib import Path
    from app.services.dividend_proposals import eastmoney_proposals
    rows=json.loads((Path(__file__).parent/'fixtures/dividend/eastmoney-current-proposals-20261116.json').read_text())
    results=eastmoney_proposals(rows,DAY)
    assert results['600036.SH']['fiscal_year']==2025
    assert results['600036.SH']['annual_dps'][2025]==Decimal('2.016')
    assert results['600900.SH']['annual_dps'][2025]==Decimal('1.00')
    assert results['600900.SH']['annual_dps'][2024]==Decimal('.943')
    assert results['688025.SH']['annual_dps'][2025]==Decimal('.89')
    assert results['301139.SZ']['fiscal_year']==2025
    assert results['301139.SZ']['annual_dps'][2025]==0


def test_latest_public_snapshot_not_historical_reconstruction():
    from app.services.dividend_proposals import eastmoney_proposals
    row=dict(SECUCODE='600036.SH',REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',
        DAT_YAGGR='2026-03-28',NOTICE_DATE='2026-07-04',ASSIGN_PROGRESS='实施方案',
        NEW_PROFILE='10派10.03元(含税)',IS_UNASSIGN='0',ASSIGN_OBJECT='A股股东')
    # Latest row is not necessarily the first proposal. Never backdate July's terms to March.
    assert eastmoney_proposals([row],date(2026,3,28))=={}


def test_duplicate_current_period_and_unknown_special_distribution_fail():
    from app.services.dividend_proposals import eastmoney_proposals
    row=dict(SECUCODE='600036.SH',REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',
        DAT_YAGGR='2026-03-28',NOTICE_DATE='2026-07-04',ASSIGN_PROGRESS='实施方案',
        NEW_PROFILE='10派10.03元(含税)',IS_UNASSIGN='0',ASSIGN_OBJECT='A股股东')
    with pytest.raises(ProposalError):eastmoney_proposals([row,row],DAY)
    special=dict(row,REPORT_DATE='2025特别分配',REPORT_TIME='2025-08-01')
    with pytest.raises(ProposalError):eastmoney_proposals([row,special],DAY)


def test_dongpeng_raw_dps_restatement_uses_effective_factor_once():
    from app.services.dividend_proposals import eastmoney_proposals, restate_current_share_basis
    base=dict(SECUCODE='605499.SH',ASSIGN_PROGRESS='实施方案',ASSIGN_OBJECT='全体股东',IS_UNASSIGN='0')
    rows=[dict(base,REPORT_DATE='2025半年报',REPORT_TIME='2025-06-30',DAT_YAGGR='2025-08-01',
               NOTICE_DATE='2025-08-18',EX_DIVIDEND_DATE='2025-08-25',NEW_PROFILE='10派25元(含税)'),
          dict(base,REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',DAT_YAGGR='2026-03-01',
               NOTICE_DATE='2026-05-12',EX_DIVIDEND_DATE='2026-05-18',NEW_PROFILE='10转3派25元(含税)')]
    raw=eastmoney_proposals(rows,DAY)
    assert raw['605499.SH']['annual_dps'][2025]==5
    updated=restate_current_share_basis(raw,rows,DAY)
    assert updated['605499.SH']['annual_dps'][2025]==Decimal(5)/Decimal('1.3')
    # Replaying raw events does not adjust a previously adjusted value twice.
    assert restate_current_share_basis(updated,rows,DAY)==updated
    rows[1]['EX_DIVIDEND_DATE']=None
    with pytest.raises(ProposalError):restate_current_share_basis(eastmoney_proposals(rows,DAY),rows,DAY)


def test_plain_cash_has_no_share_basis_adjustment():
    from app.services.dividend_proposals import eastmoney_proposals,restate_current_share_basis
    import json
    from pathlib import Path
    rows=json.loads((Path(__file__).parent/'fixtures/dividend/eastmoney-current-proposals-20261116.json').read_text())
    raw=eastmoney_proposals(rows,DAY)
    assert restate_current_share_basis(raw,rows,DAY)==raw


def test_current_snapshot_public_stage_can_prove_public_now_without_first_date():
    from app.services.dividend_proposals import eastmoney_proposals
    row=dict(SECUCODE='600036.SH',REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',
             DAT_YAGGR=None,NOTICE_DATE='2026-07-04',ASSIGN_PROGRESS='股东大会预案',
             NEW_PROFILE='不分配不转增',IS_UNASSIGN='1')
    assert eastmoney_proposals([row],DAY)['600036.SH']['fiscal_year']==2025
    row['NOTICE_DATE']='2026-12-01'
    assert eastmoney_proposals([row],DAY)=={}
    row['NOTICE_DATE']=None
    with pytest.raises(ProposalError):eastmoney_proposals([row],DAY)


def test_unpublished_annual_does_not_roll_year():
    from app.services.dividend_proposals import eastmoney_proposals
    old=dict(SECUCODE='600036.SH',REPORT_DATE='2024年报',REPORT_TIME='2024-12-31',
             DAT_YAGGR='2025-03-01',NOTICE_DATE='2025-07-04',ASSIGN_PROGRESS='实施方案',
             NEW_PROFILE='10派2元',IS_UNASSIGN='0',ASSIGN_OBJECT='全体股东')
    draft=dict(old,REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',DAT_YAGGR=None,
               NOTICE_DATE='2026-03-01',ASSIGN_PROGRESS='预披露',NEW_PROFILE='待定')
    assert eastmoney_proposals([old,draft],DAY)['600036.SH']['fiscal_year']==2024


def test_equity_crosscheck_requires_explicit_event_and_rejects_unknown_change():
    from app.services.dividend_proposals import verify_equity_events
    proposal={'605499.SH':dict(fiscal_year=2025,annual_dps={2025:Decimal(5)/Decimal('1.3')},
                              share_actions={'2026-05-18':'1.3'})}
    rows=[dict(SECUCODE='605499.SH',END_DATE='2022-01-01',TOTAL_SHARES=400000000,CHANGE_REASON='首发上市'),
          dict(SECUCODE='605499.SH',END_DATE='2026-05-18',TOTAL_SHARES=734199310,CHANGE_REASON='转增股上市')]
    verify_equity_events(proposal,rows,DAY)
    rows.insert(1,dict(SECUCODE='605499.SH',END_DATE='2026-05-05',TOTAL_SHARES=578195410,CHANGE_REASON='其他变动原因'))
    with pytest.raises(ProposalError,match='605499'):
        verify_equity_events(proposal,rows,DAY)


def test_equity_crosscheck_stable_and_unmatched_split():
    from app.services.dividend_proposals import verify_equity_events
    proposal={'600036.SH':dict(fiscal_year=2025,annual_dps={2025:Decimal('2.016')})}
    rows=[dict(SECUCODE='600036.SH',END_DATE='2022-01-01',TOTAL_SHARES=100,CHANGE_REASON='首发上市'),
          dict(SECUCODE='600036.SH',END_DATE='2025-01-01',TOTAL_SHARES=100,CHANGE_REASON='限售股上市')]
    verify_equity_events(proposal,rows,DAY)
    rows[1].update(TOTAL_SHARES=200,CHANGE_REASON='转增股上市')
    with pytest.raises(ProposalError):verify_equity_events(proposal,rows,DAY)
    with pytest.raises(ProposalError):verify_equity_events(proposal,[],DAY)


def test_two_bonus_events_and_pure_stock_distribution():
    from app.services.dividend_proposals import eastmoney_proposals,restate_current_share_basis
    base=dict(SECUCODE='600001.SH',ASSIGN_PROGRESS='实施方案',ASSIGN_OBJECT='全体股东',IS_UNASSIGN='0')
    rows=[dict(base,REPORT_DATE='2024年报',REPORT_TIME='2024-12-31',DAT_YAGGR='2025-03-01',
               NOTICE_DATE='2025-04-25',EX_DIVIDEND_DATE='2025-05-01',NEW_PROFILE='10转2派10元'),
          dict(base,REPORT_DATE='2025半年报',REPORT_TIME='2025-06-30',DAT_YAGGR='2025-08-01',
               NOTICE_DATE='2025-08-18',EX_DIVIDEND_DATE='2025-08-25',NEW_PROFILE='10派25元'),
          dict(base,REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',DAT_YAGGR='2026-03-01',
               NOTICE_DATE='2026-05-12',EX_DIVIDEND_DATE='2026-05-18',NEW_PROFILE='10转3')]
    raw=eastmoney_proposals(rows,DAY)
    assert raw['600001.SH']['annual_dps'][2025]==Decimal('2.5')
    adjusted=restate_current_share_basis(raw,rows,DAY)['600001.SH']['annual_dps']
    assert adjusted[2024]==Decimal(1)/Decimal('1.56')
    assert adjusted[2025]==Decimal('2.5')/Decimal('1.3')


def test_unimplemented_bonus_not_applied_and_pending_cash_ambiguous_basis_rejected():
    from app.services.dividend_proposals import eastmoney_proposals,restate_current_share_basis
    base=dict(SECUCODE='600001.SH',ASSIGN_PROGRESS='股东大会预案',ASSIGN_OBJECT='全体股东',IS_UNASSIGN='0')
    annual=dict(base,REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',DAT_YAGGR='2026-03-01',
                NOTICE_DATE='2026-05-12',EX_DIVIDEND_DATE='2026-12-18',NEW_PROFILE='10转3派25元')
    raw=eastmoney_proposals([annual],DAY)
    assert restate_current_share_basis(raw,[annual],DAY)['600001.SH']['annual_dps'][2025]==Decimal('2.5')
    annual.update(ASSIGN_PROGRESS='实施方案',EX_DIVIDEND_DATE='2026-05-18')
    interim=dict(base,REPORT_DATE='2025半年报',REPORT_TIME='2025-06-30',DAT_YAGGR='2025-08-01',
                 NOTICE_DATE='2026-06-01',EX_DIVIDEND_DATE=None,NEW_PROFILE='10派10元')
    with pytest.raises(ProposalError,match='先后'):
        restate_current_share_basis(eastmoney_proposals([annual,interim],DAY),[annual,interim],DAY)


def test_differential_ordinary_holder_requires_exact_audited_version():
    from app.services.dividend_proposals import eastmoney_proposals
    row=dict(SECUCODE='600989.SH',REPORT_DATE='2025年报',REPORT_TIME='2025-12-31',
             DAT_YAGGR='2026-03-13',NOTICE_DATE='2026-04-22',ASSIGN_PROGRESS='实施方案',
             ASSIGN_OBJECT='全体股东',IS_UNASSIGN='0',EX_DIVIDEND_DATE='2026-04-28',
             NEW_PROFILE='10派4.20元(含税,扣税后3.78元)(中小股东10派4.921元;大股东10派3.906元)')
    assert eastmoney_proposals([row],DAY)['600989.SH']['annual_dps'][2025]==Decimal('0.4921')
    for change in (dict(SECUCODE='600001.SH'),dict(NOTICE_DATE='2026-04-23'),
                   dict(EX_DIVIDEND_DATE='2026-04-29'),
                   dict(NEW_PROFILE=row['NEW_PROFILE'].replace('4.921','4.922'))):
        with pytest.raises(ProposalError):eastmoney_proposals([dict(row,**change)],DAY)


def test_equity_reason_components_are_exact_not_substring_allowlist():
    from app.services.dividend_proposals import verify_equity_events
    proposal={'600036.SH':dict(fiscal_year=2025,annual_dps={2025:Decimal(1)})}
    base=dict(SECUCODE='600036.SH',END_DATE='2022-01-01',TOTAL_SHARES=100,CHANGE_REASON='首发上市')
    current=dict(base,END_DATE='2025-01-01',TOTAL_SHARES=110,CHANGE_REASON='债转股上市,高管股份变动')
    verify_equity_events(proposal,[base,current],DAY)
    current['CHANGE_REASON']='回购,其他变动原因'
    with pytest.raises(ProposalError):verify_equity_events(proposal,[base,current],DAY)
    proposal['600036.SH']['share_actions']={'2025-01-01':'1.1'}
    current['CHANGE_REASON']='转增股上市,其他变动原因'
    with pytest.raises(ProposalError):verify_equity_events(proposal,[base,current],DAY)


def test_merger_issuance_exception_is_bound_to_original_security_and_event():
    from app.services.dividend_proposals import verify_equity_events
    proposal={'601211.SH':dict(fiscal_year=2025,annual_dps={2025:Decimal('.5')})}
    base=dict(SECUCODE='601211.SH',END_DATE='2022-01-01',TOTAL_SHARES=8903730620,CHANGE_REASON='首发上市')
    event=dict(base,END_DATE='2025-03-13',TOTAL_SHARES=15515776028,CHANGE_REASON='其他变动原因,增发A股上市')
    h_event=dict(event,END_DATE='2025-03-14',TOTAL_SHARES=17629708696,CHANGE_REASON='吸收合并')
    verify_equity_events(proposal,[base,event,h_event],DAY)
    with pytest.raises(ProposalError):
        verify_equity_events(proposal,[base,event,dict(h_event,TOTAL_SHARES=17629708697)],DAY)
    for change in (dict(END_DATE='2025-03-14'),dict(TOTAL_SHARES=15515776029),dict(CHANGE_REASON='其他变动原因')):
        with pytest.raises(ProposalError):verify_equity_events(proposal,[base,dict(event,**change)],DAY)


def test_explicit_zero_current_fiscal_cannot_enter_positive_yield_ranking():
    from app.services.dividend_proposals import verify_equity_events
    verify_equity_events({'000656.SZ':dict(fiscal_year=2025,annual_dps={2025:Decimal(0)})},[],DAY)
