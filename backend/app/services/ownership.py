from fastapi import HTTPException
from app.services.momentum import china_today
from datetime import date, timedelta

def stock_item(stock):
    return {'code': stock.code, 'name': stock.name, 'industry': stock.industry}

def latest_quarter(today):
    return date(today.year, ((today.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)

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
        items, unknown, eligible = [], 0, 0
        for report, stock in rows:
            social = report.payload.get('social_entry', {})
            if social.get('status') != 'comparable':
                unknown += 1
                continue
            eligible += 1
            if not social.get('new_combinations') or (mode == 'first' and not social.get('first_appearance')):
                continue
            if q and q not in stock.code and q not in stock.name:
                continue
            items.append(dict(stock_item(stock), **social, report_date=period,
                              pub_date=report.payload.get('float_pub_date')))
        items.sort(key=lambda r: (r.get('new_market_value') is None, -(r.get('new_market_value') or 0), r['code']))
        periods = sorted(set(self.repository.periods(today) + [latest_quarter(today)]), reverse=True)
        return {'period': period, 'periods': periods, 'items': items,
                'comparable_count': eligible, 'unknown_count': unknown,
                'collected_count': len(rows)}
