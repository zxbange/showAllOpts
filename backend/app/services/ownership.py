from fastapi import HTTPException
from app.services.momentum import china_today
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

def stock_item(stock):
    return {'code': stock.code, 'name': stock.name, 'industry': stock.industry}

def latest_quarter(today):
    return date(today.year, ((today.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)

def social_total(payload):
    if not payload or not payload.get('float_complete') or not isinstance(payload.get('float_members'), list):
        return None
    total = Decimal(0)
    for member in payload['float_members']:
        if member.get('class_id') is None:
            return None
        if member['class_id'] != 307004:
            continue
        try:
            shares = Decimal(str(member.get('shares')))
        except InvalidOperation:
            return None
        if not shares.is_finite() or shares < 0:
            return None
        total += shares
    return total


def social_increase(current, previous):
    current_total, previous_total = social_total(current), social_total(previous)
    if current_total is None or previous_total is None:
        return {'status': 'unknown'}
    change = current_total - previous_total
    return {'status': 'comparable', 'previous_shares': float(previous_total),
            'current_shares': float(current_total), 'change_shares': float(change),
            'change_pct': float(change / previous_total * 100) if previous_total > 0 else None}


class OwnershipService:
    def __init__(self, repository):
        self.repository = repository

    def detail(self, code):
        stock = self.repository.stock(code)
        if stock is None:
            raise HTTPException(404, '未找到已采集的股票，请先完成采集并同步。')
        today = china_today()
        reports = self.repository.reports(stock.code, today)
        holders = self.repository.holders(stock.code, today)
        change = pct = None
        if len(holders) >= 2:
            previous, current = [h.payload.get('a_share_holders') for h in holders[-2:]]
            if current is not None and previous is not None:
                change = current - previous
                pct = change / previous * 100 if previous > 0 else None
        return {'stock': stock_item(stock),
                'reports': [{'date': r.report_date, 'payload': r.payload} for r in reports],
                'holders': [{'date': h.end_date, 'pub_date': h.pub_date, 'payload': h.payload} for h in holders],
                'holder_change': change, 'holder_change_pct': pct}

    def social(self, period, mode, q):
        today = china_today()
        period = period or latest_quarter(today)
        if period > latest_quarter(today) or (period.month, period.day) not in ((3,31),(6,30),(9,30),(12,31)):
            raise HTTPException(422, '请选择已结束的季度末日期。')
        rows = self.repository.period_rows(period)
        previous = {r.code: r.payload for r, _ in self.repository.period_rows(latest_quarter(period))} if mode == 'increase' else {}
        items, unknown, eligible = [], 0, 0
        for report, stock in rows:
            social = social_increase(report.payload, previous.get(report.code)) if mode == 'increase' else report.payload.get('social_entry', {})
            if social.get('status') != 'comparable':
                unknown += 1
                continue
            eligible += 1
            if mode == 'increase':
                if social['change_shares'] <= 0:
                    continue
            elif not social.get('new_combinations') or (mode == 'first' and not social.get('first_appearance')):
                continue
            if q and q not in stock.code and q not in stock.name:
                continue
            items.append(dict(stock_item(stock), **social, report_date=period,
                              pub_date=report.payload.get('float_pub_date')))
        sort_key = 'change_shares' if mode == 'increase' else 'new_market_value'
        items.sort(key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0), r['code']))
        periods = sorted(set(self.repository.periods(today) + [latest_quarter(today)]), reverse=True)
        return {'period': period, 'periods': periods, 'items': items,
                'comparable_count': eligible, 'unknown_count': unknown,
                'collected_count': len(rows)}
