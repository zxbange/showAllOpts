"""Compute derived tables inside an unpublished SQLite snapshot."""
from collections import defaultdict
from decimal import Decimal
import json
import logging
from types import SimpleNamespace
from sqlalchemy import select

from app.core.raw_inputs import decode_input, RAW_TABLES
from app.models.raw import RAW_MODELS
from app.services import raw_rules as rules
from app.models.momentum import IndustryMomentum
from app.models.crowding import MarketCrowding
from app.models.microcap import MicrocapTemperature
from app.models.breadth import MicrocapBreadth
from app.models.dividend import DividendSnapshot, DividendStock
from app.models.growth import GrowthSnapshot, GrowthStock
from app.models.earnings import EarningsRun, EarningsStock

logger=logging.getLogger(__name__)
RAW_CALC_VERSION='local-strategies-v1'
RESULT_MODELS={
 'industry-momentum':(IndustryMomentum,), 'market-crowding':(MarketCrowding,),
 'microcap-temperature':(MicrocapTemperature,), 'microcap-breadth':(MicrocapBreadth,),
 'dividend-selection':(DividendSnapshot,DividendStock), 'high-growth':(GrowthSnapshot,GrowthStock),
 'earnings-gap':(EarningsRun,EarningsStock),
}


def insert_rows(connection, model, records):
    for i in range(0,len(records),500):
        connection.execute(model.__table__.insert(),records[i:i+500])


def calculate_input(payload,stamp,state):
    name,day,settings,data=payload['strategy'],payload['day'],payload['settings'],payload['inputs']
    scope=payload['scope']
    if name=='行业动量':
        ns=rules._momentum_rules(settings,data,stamp)
        return [(IndustryMomentum,[dict(r,updated_at=stamp) for r in ns.calculate_momentum(day)])]
    if name=='抱团拥挤度':
        ns=rules._crowding_rules(settings,data,stamp)
        return [(MarketCrowding,[dict(ns.summarize_money(data['pairs']),trade_date=day,updated_at=stamp)])]
    if name=='微盘新高新低':
        ns=rules._breadth_rules(settings,data,stamp)
        counts=ns.count_prices(data['prices'],data['codes'],data['calendar'])
        return [(MicrocapBreadth,[dict(counts,series_id=scope,trade_date=day,sample_count=len(data['codes']),member_codes=json.dumps(data['codes']),updated_at=stamp)])]
    if name=='微盘温度计':
        ns=rules._microcap_rules(settings,data,stamp);size=settings['micro_num']
        metrics,members=ns.summarize_universe(data['rows'],data['turnovers'],data['amounts'],size)
        growth=[ns.number(data['growth'].get(c,{}).get('inc_net_profit_to_shareholders_year_on_year')) for c in members]
        growth=[v for v in growth if v is not None]
        previous=state.get('previous'); index=Decimal('1000.00000000'); ret=Decimal(0)
        if previous:
            returns=[]
            for code in json.loads(previous['member_codes']):
                before,after=data['endpoints'].get((code,previous['trade_date'])),data['endpoints'].get((code,day))
                if before is None or after is None or before<=0 or after<=0:
                    raise ValueError('微盘指数上一期成分的端点价格缺失')
                returns.append(after/before-1)
            ret=sum(returns)/size
            index=(previous['micro_index']*(1+ret)).quantize(Decimal('0.00000001'))
        record=dict(metrics,trade_date=day,micro_num=size,series_id=scope,sample_step=settings['sample_step'],
            member_codes=json.dumps(members),avg_np_growth=ns.rounded(rules.median(growth)) if growth else None,
            profit_growth_count=len(growth),updated_at=stamp,micro_index=index,period_return=ns.rounded(ret))
        histories=state.setdefault('histories',{f:[] for f in ns.RAW_FIELDS})
        ns.append_percentiles(record,histories);state['previous']=record
        return [(MicrocapTemperature,[record])]
    if name=='高股息选股':
        ns=rules._dividend_rules(settings,data,stamp);meta,records=ns.calculate(day)
        return [(DividendSnapshot,[dict(meta,id=1,updated_at=stamp)]),(DividendStock,records)]
    if name=='高增长':
        ns=rules._growth_rules(settings,data,stamp);meta,records=ns.calculate(data.get('as_of',day),day)
        return [(GrowthSnapshot,[dict(meta,id=1,updated_at=stamp)]),(GrowthStock,records)]
    if name=='净利润断层':
        ns=rules._earnings_rules(settings,data,stamp)
        records=ns.screen(sorted(data['names']),data['names'],data['events'],data['period'],day)
        return [(EarningsRun,[dict(report_period=data['period'],as_of_date=day,updated_at=stamp,
                   universe_count=len(data['names']),matched_count=len(records),rule_version=ns.RULE_VERSION)]),
                (EarningsStock,[ns.database_record(r) for r in records])]
    raise ValueError('未知原始数据策略')


def rebuild_raw(connection,module):
    table=RAW_MODELS[module].__table__
    snapshots=connection.execute(select(table.c.scope,table.c.trade_date).distinct().order_by(table.c.scope,table.c.trade_date)).all()
    models=RESULT_MODELS[module]
    # Earnings legacy rows are frozen first-hit baselines imported by cache sync.
    if module!='earnings-gap':
        for model in reversed(models):connection.execute(model.__table__.delete())
    latest={scope:day for scope,day in snapshots}
    states=defaultdict(dict); count=0
    for scope,day in snapshots:
        pieces=[SimpleNamespace(**dict(r)) for r in connection.execute(select(table).where(table.c.scope==scope,table.c.trade_date==day)).mappings()]
        payload=decode_input(pieces)
        if payload['scope']!=scope or payload['day']!=day or RAW_TABLES.get(payload['strategy'], (None,))[0]!=module:
            raise ValueError('原始快照日期或序列不一致')
        if day==latest[scope] and payload.get('expected_latest_day',day)!=day:
            raise ValueError('原始采集批次尚未完成，保留上一完整快照')
        output=calculate_input(payload,pieces[0].updated_at,states[scope])
        if module=='earnings-gap':
            meta=output[0][1][0];period=meta['report_period']
            existing=set(connection.scalars(select(EarningsStock.code).where(EarningsStock.report_period==period)))
            records=[r for r in output[1][1] if r['code'] not in existing]
            insert_rows(connection,EarningsStock,records)
            meta['matched_count']=len(existing)+len(records)
            connection.execute(EarningsRun.__table__.delete().where(EarningsRun.report_period==period))
            insert_rows(connection,EarningsRun,[meta]);count+=len(records)
        else:
            for model,records in output:
                insert_rows(connection,model,records);count+=len(records)
        logger.info('Local calculation module=%s date=%s scope=%s',module,day,scope)
    return count
