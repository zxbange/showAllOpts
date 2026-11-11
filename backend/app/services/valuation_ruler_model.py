"""Exact CSI300 equal-positive-PE arithmetic and immutable daily validation."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import re

START = date(2010, 1, 1)
INDEX = '000300.XSHG'
METHOD = 'csi300_equal_positive_pe_ttm_v1'
PUBLIC_SOURCE = 'CSI_EM.PE_TTM.V1'
JQ_SOURCE = 'JQ_PLATFORM.PE_TTM.V1'
SOURCES = frozenset((PUBLIC_SOURCE, JQ_SOURCE))
CODE = re.compile(r'[036]\d{5}\.(?:XSHG|XSHE)\Z')


class ValuationError(ValueError):
    """Only fixed safe descriptions, never upstream messages or connection details."""


class ValuationConflict(ValuationError):
    pass


def decimal_value(value, precision, scale, *, positive=False):
    if value is None or isinstance(value, bool):
        raise ValuationError('数值缺失或格式无效。')
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation):
        raise ValuationError('数值格式无效。') from None
    if not result.is_finite() or result == 0 or (positive and result < 0):
        raise ValuationError('数值为零、非有限值或不符合正值要求。')
    if result.as_tuple().exponent < -scale or result.copy_abs() >= Decimal(10) ** (precision-scale):
        raise ValuationError('数值超出字段精度，未执行隐式舍入。')
    return result


def valid_code(code):
    return (isinstance(code, str) and CODE.fullmatch(code) is not None and
            ((code.startswith('6') and code.endswith('.XSHG')) or
             (code.startswith(('0', '3')) and code.endswith('.XSHE'))))


def validate_day(day, index_close, members, source, expected=None):
    if type(day) is not date or day < START or day.weekday() >= 5 or source not in SOURCES:
        raise ValuationError('交易日期或数据来源不符合估值标尺口径。')
    if not isinstance(members, dict) or len(members) != 300 or any(not valid_code(code) for code in members):
        raise ValuationError('须有当日完整的 300 只唯一沪深成分股。')
    close = decimal_value(index_close, 24, 8, positive=True)
    values = [decimal_value(value, 30, 12) for value in members.values()]
    positives = [value for value in values if value > 0]
    if not positives:
        raise ValuationError('当日没有可计算平均值的正 PE 成分股。')
    with localcontext() as context:
        context.prec = 60
        total = sum(positives, Decimal(0)).quantize(Decimal('1e-12'), rounding=ROUND_HALF_EVEN)
        mean = (total/len(positives)).quantize(Decimal('1e-12'), rounding=ROUND_HALF_EVEN)
        result = dict(trade_date=str(day), index_code=INDEX, method_version=METHOD,
            index_close=format(close, '.8f'), mean_pe_ttm=format(mean, '.12f'), pe_sum=format(total, '.12f'),
            member_count=300, positive_count=len(positives), loss_count=300-len(positives),
            members_sha256=hashlib.sha256('\n'.join(sorted(members)).encode()).hexdigest(), source=source)
    if expected is not None:
        for key in ('index_code', 'method_version', 'members_sha256', 'source'):
            if expected.get(key) != result[key]:
                raise ValuationError('日汇总来源、口径或成分股指纹不一致。')
        for key in ('member_count', 'positive_count', 'loss_count'):
            if type(expected.get(key)) is not int or expected[key] != result[key]:
                raise ValuationError('日汇总成分股数量与明细不一致。')
        for key, precision, scale in [('index_close',24,8),('mean_pe_ttm',30,12),('pe_sum',38,12)]:
            if decimal_value(expected.get(key),precision,scale,positive=True) != Decimal(result[key]):
                raise ValuationError('日汇总数值与成分股明细不一致。')
    return result
