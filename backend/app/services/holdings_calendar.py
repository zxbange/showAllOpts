"""Versioned exchange calendar; unknown coverage fails closed, never weekday fallback."""
from datetime import date

# Verified against the SSE annual exchange closure notice, not the civil work calendar.
# Add a year only after its official full-year notice has been published and reviewed.
CALENDARS = {
    2026: {
        'source': 'https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml',
        'holidays': [('01-01', '01-03'), ('02-15', '02-23'), ('04-04', '04-06'),
                     ('05-01', '05-05'), ('06-19', '06-21'), ('09-25', '09-27'), ('10-01', '10-07')],
    },
}


def trading_day(day):
    calendar = CALENDARS.get(day.year)
    if calendar is None:
        return None
    return day.weekday() < 5 and not any(
        date.fromisoformat(f'{day.year}-{start}') <= day <= date.fromisoformat(f'{day.year}-{end}')
        for start, end in calendar['holidays'])
