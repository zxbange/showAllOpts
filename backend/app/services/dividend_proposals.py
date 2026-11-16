"""Canonical currently-public A-share pre-tax DPS proposals.

No total-share multiplication, no inferred zero and no historical point-in-time
claim: providers must supply their latest effective publicly disclosed proposal.
"""
from datetime import date
from decimal import Decimal, InvalidOperation


class ProposalError(ValueError):
    pass


def proposal_totals(events, today):
    """Normalize verified events: symbol/period/announced/dps/status/event_id.

    An event ID identifies the fiscal payout (not a publication stage). Revisions
    replace the event; different dates cannot silently turn a revision additive.
    """
    selected = {}
    for event in events:
        try:
            period = date.fromisoformat(event['period'])
            published = date.fromisoformat(event['announced'])
            symbol = event['symbol']
            identity = (symbol, event['event_id'])
            if published > today or period > today or not event['event_id']:
                raise ValueError()
            dps = Decimal(str(event['dps']))
            if not dps.is_finite() or dps < 0 or event['status'] not in ('active', 'zero', 'cancelled'):
                raise ValueError()
            if event['status'] != 'active' and dps != 0:
                raise ValueError()
            event = dict(event, dps=dps)
            previous = selected.get(identity)
            if previous and previous['period'] != event['period']:
                raise ValueError()
            if previous and previous['announced'] == event['announced'] and previous != event:
                raise ValueError()
            if not previous or previous['announced'] < event['announced']:
                selected[identity] = dict(event, dps=dps)
        except (ValueError, TypeError, KeyError, InvalidOperation):
            raise ProposalError('公开分红方案冲突或字段缺失。') from None
    result, annual_years = {}, {}
    for (symbol, _), event in selected.items():
        fiscal = int(event['period'][:4])
        if event.get('annual_final') is True:
            annual_years.setdefault(symbol, set()).add(fiscal)
        result.setdefault(symbol, {})[fiscal] = result.setdefault(symbol, {}).get(fiscal, Decimal(0)) + event['dps']
    return {symbol: {'fiscal_year': max(annual_years[symbol]),
                     'annual_dps': totals} for symbol, totals in result.items()
            if annual_years.get(symbol)}


def eastmoney_proposals(rows, today):
    """MAIN latest row per fiscal payout, never TOTAL_DIVIDEND or PUBLISH_DATE."""
    import re
    normalized, seen, selected_years = [], set(), {}
    for row in rows:
        label = str(row.get('REPORT_DATE') or '')
        if not label.endswith('年报') or label.endswith('半年报'):
            continue
        if row.get('ASSIGN_PROGRESS') in ('预披露', '公司预案'):
            continue
        try:
            period = date.fromisoformat(str(row.get('REPORT_TIME') or '')[:10])
            # Current-only proof: a published effective stage proves the annual
            # proposal is public now; never claim this is its original date.
            first = date.fromisoformat(str(row.get('DAT_YAGGR') or row.get('NOTICE_DATE') or '')[:10])
            if (period.month, period.day) != (12, 31):
                raise ValueError()
            if first <= today:
                symbol = row.get('SECUCODE')
                selected_years[symbol] = max(selected_years.get(symbol, 0), period.year)
        except (ValueError, TypeError):
            raise ProposalError('年报预案公告日期无法核验：{} {}'.format(row.get('SECUCODE'), row.get('REPORT_TIME'))) from None
    for row in rows:
        period = str(row.get('REPORT_TIME') or '')[:10]
        first = str(row.get('DAT_YAGGR') or '')[:10]
        announced = str(row.get('NOTICE_DATE') or '')[:10]
        symbol = row.get('SECUCODE')
        label = str(row.get('REPORT_DATE') or '')
        try:
            period_day = date.fromisoformat(period)
            selected_year = selected_years.get(symbol)
            if selected_year is None or not selected_year - 2 <= period_day.year <= selected_year:
                continue
            announced_day = date.fromisoformat(announced)
            if not isinstance(symbol, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', symbol):
                raise ValueError()
            annual = period.endswith('-12-31') and label.endswith('年报') and not label.endswith('半年报')
            ordinary = annual or (period.endswith('-06-30') and label.endswith('半年报')) or (
                period.endswith(('-03-31', '-09-30')) and '季报' in label)
            if not ordinary:
                raise ValueError()
            if (symbol, period) in seen:
                raise ValueError()
            seen.add((symbol, period))
            first_day = date.fromisoformat(first) if first else announced_day
            if first_day > today or announced_day > today:
                continue
            if announced_day < first_day or period_day > today:
                raise ValueError()
            if row.get('ASSIGN_PROGRESS') in ('预披露', '公司预案'):
                continue
            if row.get('ASSIGN_PROGRESS') not in ('董事会预案', '股东大会预案', '实施方案'):
                raise ValueError()
            text = str(row.get('NEW_PROFILE') or row.get('IMPL_PLAN_PROFILE') or '').strip()
            stock_only = re.fullmatch(r'10(?:送([0-9]+(?:\.[0-9]+)?)(?:股)?)?(?:转(?:增)?([0-9]+(?:\.[0-9]+)?)(?:股)?)?(?:[（(]含税[^()（）]*[)）])?', text)
            zero = text in ('不分配不转增', '不分配不转赠', '不分配', '不派现不送股不转增') or (
                stock_only is not None and (stock_only[1] is not None or stock_only[2] is not None))
            if zero:
                dps, status = Decimal(0), 'zero'
            else:
                if row.get('ASSIGN_OBJECT') not in ('全体股东', '全体A股股东', 'A股股东'):
                    raise ValueError()
                # Exact per-10-share RMB cash leg; never parse percentages or total cash.
                match = re.fullmatch(r'10(?:送[0-9.]+(?:股)?)*(?:转(?:增)?[0-9.]+(?:股)?)*派([0-9]+(?:\.[0-9]+)?)元(?:[（(]含税[^()（）]*[)）])?', text)
                if match is None:
                    from app.services.dividend_announcement_evidence import ORDINARY_A_SHARE
                    evidence = ORDINARY_A_SHARE.get((symbol, period, announced))
                    if evidence is None:
                        raise ProposalError('非标准或差异化分红方案待公告核验：{} {}'.format(symbol, period))
                    matches = re.findall(re.escape(evidence['label']) + r'10派([0-9]+(?:\.[0-9]+)?)元', text)
                    if (len(matches) != 1 or Decimal(matches[0]) != Decimal(evidence['pretax_per_ten'])
                            or str(row.get('EX_DIVIDEND_DATE') or '')[:10] != evidence['ex_date']):
                        raise ValueError()
                    per_ten = Decimal(matches[0])
                else:
                    per_ten = Decimal(match[1])
                if str(row.get('IS_UNASSIGN')) == '1':
                    raise ValueError()
                dps, status = per_ten / 10, 'active'
            normalized.append(dict(symbol=symbol, period=period, announced=announced,
                dps=dps, status=status, event_id=period, annual_final=annual))
        except ProposalError:
            raise
        except (ValueError, TypeError, InvalidOperation):
            raise ProposalError('分红预案日期、状态或每股税前金额无法核验：{} {}'.format(symbol, period)) from None
    return proposal_totals(normalized, today)


def restate_current_share_basis(events, rows, today):
    """Restate raw MAIN cash legs using explicit *implemented* bonus factors.

    MAIN is not adjusted (605499 2025 was live-verified). Each cash leg is on
    the pre-ex shares for its own distribution, so same-date bonus also applies.
    This function never infers a factor from total capital or market prices.
    """
    import re
    actions = {}
    for row in rows:
        symbol = row.get('SECUCODE')
        selected = events.get(symbol)
        if selected is None or row.get('ASSIGN_PROGRESS') != '实施方案':
            continue
        text = str(row.get('NEW_PROFILE') or row.get('IMPL_PLAN_PROFILE') or '')
        if not re.search(r'(?:送|转(?:增)?)[0-9]', text):
            if re.search(r'拆股|合股|缩股', text):
                raise ProposalError('拆合股事件缺少可核验比例。')
            continue
        try:
            effective = date.fromisoformat(str(row.get('EX_DIVIDEND_DATE') or '')[:10])
            if effective > today or effective.year < selected['fiscal_year'] - 2:
                continue
            match = re.fullmatch(r'10(?:送([0-9]+(?:\.[0-9]+)?)(?:股)?)?(?:转(?:增)?([0-9]+(?:\.[0-9]+)?)(?:股)?)?(?:派[0-9]+(?:\.[0-9]+)?元)?(?:[（(]含税[^()（）]*[)）])?',text)
            if match is None:
                raise ValueError()
            factor = 1 + (Decimal(match[1] or '0') + Decimal(match[2] or '0')) / 10
            if factor <= 1 or effective in actions.setdefault(symbol, {}):
                raise ValueError()
            actions[symbol][effective] = factor
        except (ValueError, TypeError, InvalidOperation):
            raise ProposalError('送转比例或A股除权生效日无法核验。') from None
    for symbol, changes in actions.items():
        selected = events[symbol]
        annual = {year: Decimal(0) for year in selected['annual_dps']}
        for row in rows:
            if row.get('SECUCODE') != symbol:
                continue
            period = str(row.get('REPORT_TIME') or '')[:10]
            if not period[:4].isdigit() or int(period[:4]) not in annual:
                continue
            # Reuse exactly the raw single-event parser; no adjusted output is
            # ever fed back into it. Interim-only rows need an annual anchor.
            anchor = next((r for r in rows if r.get('SECUCODE') == symbol and
                           str(r.get('REPORT_TIME') or '')[:10] == f"{selected['fiscal_year']}-12-31"), None)
            raw = eastmoney_proposals([row] if row is anchor else [anchor, row], today).get(symbol)
            if raw is None:
                raise ProposalError('送转前每笔派息无法核验。')
            # The anchor contributes once below, not once per interim leg.
            own = raw['annual_dps'].get(int(period[:4]), Decimal(0))
            if row is not anchor and int(period[:4]) == selected['fiscal_year']:
                anchor_raw = eastmoney_proposals([anchor], today)[symbol]['annual_dps'][selected['fiscal_year']]
                own -= anchor_raw
            if own == 0:
                continue
            try:
                effective_cash = (date.fromisoformat(str(row.get('EX_DIVIDEND_DATE') or '')[:10])
                                  if row.get('ASSIGN_PROGRESS') == '实施方案' else None)
                if effective_cash is not None and effective_cash <= today:
                    basis = effective_cash
                else:
                    first = date.fromisoformat(str(row.get('DAT_YAGGR') or row.get('NOTICE_DATE') or '')[:10])
                    notice = date.fromisoformat(str(row.get('NOTICE_DATE') or '')[:10])
                    if any(first <= day <= notice for day in changes):
                        raise ValueError()
                    basis = notice
                factor = Decimal(1)
                for effective, ratio in changes.items():
                    if effective >= basis:
                        factor *= ratio
                annual[int(period[:4])] += own / factor
            except (ValueError, TypeError):
                raise ProposalError('预案每股基准与送转事件先后无法核验。') from None
        selected['annual_dps'] = annual
        selected['share_basis'] = 'current_unadjusted_share'
        selected['share_actions'] = {day.isoformat(): str(factor) for day, factor in changes.items()}
    return events


def verify_equity_events(proposals, rows, today, listed_dates=None):
    """Cross-check reported capital changes; unknown changes cannot be ignored.

    EH total capital is an audit signal only, never a bonus/split ratio. A/H
    issuance does not multiply existing A-share units; unknown causes still stop.
    """
    import re
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get('SECUCODE'), []).append(row)
    unchanged_unit = {'增发A股上市', '增发上市', '超额配售', '回购注销', '股份回购注销',
                      '股权激励股份回购注销', '首发H股上市', 'H股超额配售', '可转债转股',
                      '回购', '债转股上市', '限制性股票', '高管股份变动',
                      '自主行权', '期权行权', '配售A股上市', '增发H股上市',
                      '配售H股上市', '股份性质变更', '首发限售股份上市',
                      '股权激励限售流通股上市'}
    bonus_reasons = {'送股上市', '转增股上市'}
    for symbol, proposal in proposals.items():
        # A verified zero current-fiscal cash amount stays zero under any
        # positive share-unit factor and cannot pass the strict >3% screen.
        if proposal['annual_dps'].get(proposal['fiscal_year']) == Decimal(0):
            continue
        start = date(proposal['fiscal_year'] - 2, 1, 1)
        if (listed_dates or {}).get(symbol):
            start = max(start, date.fromisoformat(listed_dates[symbol]))
        actions = proposal.get('share_actions', {})
        matched, baseline = set(), None
        entries = grouped.get(symbol)
        if not entries:
            raise ProposalError('权益变动覆盖缺失：' + symbol)
        seen = set()
        for row in sorted(entries, key=lambda item: str(item.get('END_DATE'))):
            try:
                day = date.fromisoformat(str(row.get('END_DATE') or '')[:10])
                shares = Decimal(str(row.get('TOTAL_SHARES')))
                reason = str(row.get('CHANGE_REASON') or '')
                if day in seen or not shares.is_finite() or shares <= 0:
                    raise ValueError()
                seen.add(day)
                if day > today:
                    continue
                if day < start:
                    baseline = shares
                    continue
                if baseline is None:
                    if day == start:
                        baseline = shares
                        continue
                    raise ValueError()
                reasons = set(reason.split(','))
                unit_change = bool(re.search(r'送股|转增|拆股|合股|缩股|配股', reason))
                if unit_change:
                    if (day.isoformat() not in actions
                            or not reasons.intersection(bonus_reasons)
                            or not reasons.issubset(unchanged_unit | bonus_reasons)):
                        raise ValueError()
                    matched.add(day.isoformat())
                elif shares != baseline and not reasons.issubset(unchanged_unit):
                    from app.services.dividend_announcement_evidence import UNCHANGED_SHARE_UNIT
                    evidence = UNCHANGED_SHARE_UNIT.get((symbol, day.isoformat(), reason))
                    if evidence is None or shares != Decimal(evidence['total_shares']):
                        raise ValueError()
                baseline = shares
            except (ValueError, TypeError, InvalidOperation):
                raise ProposalError('权益变动原因或每股基准无法核验：{} {}'.format(symbol, row.get('END_DATE'))) from None
        if baseline is None or matched != set(actions):
            raise ProposalError('送转事件与权益变动清单未完整对应：' + symbol)
