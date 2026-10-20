# -*- coding: utf-8 -*-
"""聚宽 Notebook 独立研究脚本：按跟踪指数去重，选同日规模最大的 ETF。
不下单、不连接数据库。分类是关键词规则，须审阅；未识别项目单列。
"""
import builtins
import datetime as dt
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
import jqdata as jq
from jqdata import finance, query

# ---------------- 可配置参数 ----------------
AS_OF_DATE = None  # 默认北京时间昨天及之前最后一个交易日
LOOKBACK_DAYS = 15
MAX_STALE_TRADE_DAYS = 3
BATCH_SIZE = 50
PAGE_SIZE = 2000
EXPORT_CSV = True
CATEGORIES = ['宽基ETF', '行业ETF', '主题ETF', '策略/风格ETF', '跨境ETF', '商品ETF', '债券ETF']
# 支持按基金代码或指数代码配置，基金代码优先。
# OVERRIDES = {'510300.XSHG': {'category': '宽基ETF'}}
# 合并不同指数到同一方向：为相关基金/指数设置相同的 group 和 category。
# 缺失标的时可人工设置 target_code、target_name；请核实后再填写。
OVERRIDES = {}
# 关键词分类为研究用途，不是官方分类。优先：货币排除、债券、跨境、策略、宽基、主题、行业。
RULES = [
    ('排除：货币ETF', '货币 现金 日利 添益 快线'),
    ('债券ETF', '债 国开 政金 政策性金融'),
    ('跨境ETF', '恒生 港股 香港 沪港深 中概 纳斯达克 纳指 标普 日经 德国 DAX 法国 东证 沙特 亚太 海外 全球 美国 日本 韩国 MSCI中国'),
    ('策略/风格ETF', '红利 低波 价值 质量 基本面 等权 高股息 自由现金流 成长 动量'),
    ('宽基ETF', '沪深300 中证A50 中证A100 中证A500 中证500 中证800 中证1000 中证2000 上证50 上证180 上证380 上证综 深证100 深证成 深证50 深证300 创业板指 创业板50 创业板综 科创50 科创100 科创200 科创综 科创创业50 国证2000 国证A'),
    ('主题ETF', '人工智能 机器人 新能源 光伏 电池 储能 智能 数字 云计算 大数据 物联网 互联网 网络安全 信息安全 创新药 疫苗 养老 环保 碳中和 绿色 一带一路 国企 央企 国资 改革 科技 创新 新材料 稀土 稀有金属 资源 5G 低空 卫星 航天'),
    ('行业ETF', '银行 证券 券商 保险 金融 医药 中药 医疗 生物 半导体 芯片 军工 国防 煤炭 钢铁 有色 化工 地产 房地产 食品 饮料 酒 农业 养殖 畜牧 家电 汽车 机械 电力 公用事业 建筑 建材 交通 运输 物流 航空 通信 传媒 计算机 软件 电子 消费 石油 油气'),
]


def api(name):
    value = globals().get(name) or getattr(jq, name, None) or getattr(builtins, name, None)
    if value is None:
        raise RuntimeError('研究环境缺少 API：' + name)
    return value


def text(value):
    return '' if value is None or pd.isna(value) else str(value).strip()


def positive(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError):
        return None


def read_pages(table, filters=()):
    frames, cursor = [], -1
    while True:
        frame = finance.run_query(query(table).filter(*filters).filter(table.id > cursor)
                                  .order_by(table.id.asc()).limit(PAGE_SIZE))
        if frame is None:
            raise ValueError('查询返回 None')
        if frame.empty:
            break
        ids = frame['id'].tolist()
        if ids != sorted(set(ids)) or ids[0] <= cursor:
            raise ValueError('分页主键重复或乱序')
        frames.append(frame)
        cursor = ids[-1]
        if len(frame) < PAGE_SIZE:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def classify(target, override):
    if override.get('category'):
        category = override['category']
        if category not in CATEGORIES and category != '排除：货币ETF':
            raise ValueError('无效人工分类：' + category)
        return category, '人工指定'
    label = re.sub(r'\s+', '', target).upper()
    if ('黄金' in label and not builtins.any(w in label for w in ('股票', '黄金股', '产业'))) or \
            builtins.any(w in label for w in ('豆粕期货', '有色金属期货', '能源化工期货')):
        return '商品ETF', '商品标的关键词'
    for category, words in RULES:
        hit = next((word for word in words.split() if word.upper() in label), None)
        if hit and category == '宽基ETF':
            # 避免把‘中证500医药’等行业子指数误识别为宽基。
            suffix = label.split(hit.upper(), 1)[1]
            suffix = re.sub(r'[（(][^）)]*[）)]', '', suffix)
            for word in ('全收益', '净收益', '价格', '指数', '收益'):
                suffix = suffix.replace(word, '')
            if suffix:
                continue
        if hit:
            return category, '关键词：' + hit
    return '待核对', '请通过 OVERRIDES 指定分类'


def target_catalog(universe, day):
    table = finance.FUND_INVEST_TARGET
    frame = read_pages(table, (table.pub_date <= day, table.start_date <= day))
    if frame.empty:
        raise ValueError('有效跟踪标的表为空，请核对数据权限')
    required = {'code', 'end_date', 'traced_index_code', 'traced_index_name'}
    if not required.issubset(frame.columns):
        raise ValueError('跟踪标的字段不符：' + str(list(frame.columns)))
    mapping = {}
    for row in frame.to_dict('records'):
        if text(row['end_date']) and pd.Timestamp(row['end_date']).date() < day:
            continue
        if row['code'] in universe.index:
            mapping.setdefault(row['code'], set()).add((text(row['traced_index_code']), text(row['traced_index_name'])))
    records, audit = [], []
    for code, info in universe.iterrows():
        override = OVERRIDES.get(code, {})
        targets = mapping.get(code, set())
        if override.get('target_name'):
            targets = {(override.get('target_code', ''), override['target_name'])}
        if len(targets) != 1:
            audit.append(dict(code=code, name=info['display_name'], reason='跟踪标的缺失或有效版本冲突，请人工核对', details=str(sorted(targets))))
            continue
        target_code, target_name = next(iter(targets))
        if not target_name:
            audit.append(dict(code=code, reason='标的名称为空'))
            continue
        override = dict(OVERRIDES.get(target_code, {}), **override)
        category, reason = classify(target_name, override)
        # 同一标的全局分组，不因分类差异重复入选。没有指数代码时只能按完整名称匹配。
        group = override.get('group') or ('code:' + target_code if target_code else 'name:' + re.sub(r'\s+', '', target_name).upper())
        records.append(dict(code=code, name=info['display_name'], category=category,
                            target_code=target_code, target_name=target_name, group=group,
                            classification_reason=reason))
    return records, audit


def load_sizes(codes, days):
    table, output = finance.FUND_SHARE_DAILY, {}
    for offset in range(0, len(codes), BATCH_SIZE):
        batch = codes[offset:offset + BATCH_SIZE]
        print('规模采集：{}/{} ETF'.format(builtins.min(offset + len(batch), len(codes)), len(codes)), flush=True)
        shares = read_pages(table, (table.code.in_(batch), table.date >= days[0], table.date <= days[-1]))
        navs = api('get_extras')('unit_net_value', batch, start_date=days[0], end_date=days[-1], df=True)
        if navs is None:
            raise ValueError('单位净值接口返回 None')
        navs = navs.copy()
        navs.index = pd.Index(pd.to_datetime(navs.index).date)
        if navs.index.has_duplicates:
            raise ValueError('单位净值日期重复')
        for row in shares.to_dict('records'):
            code, day = row['code'], pd.Timestamp(row['date']).date()
            if code not in batch or day not in days:
                raise ValueError('份额数据超出查询范围')
            amount = positive(row['shares'])  # 聚宽该字段单位：份
            nav = positive(navs.at[day, code]) if day in navs.index and code in navs else None
            if amount is None or nav is None:
                continue
            value = (amount * nav, amount, nav)
            previous = output.setdefault(code, {}).get(day)
            if previous is not None and previous != value:
                raise ValueError('{} {} 份额记录冲突'.format(code, day))
            output[code][day] = value
    return output


def select_largest(records, sizes, days):
    groups, selected, audit, candidates = {}, [], [], []
    for row in records:
        groups.setdefault(row['group'], []).append(row)
    for group, members in groups.items():
        categories = {row['category'] for row in members}
        if len(categories) != 1 or next(iter(categories)) not in CATEGORIES:
            audit.append(dict(group=group, reason='分类待核对、冲突或不在所选七类', details='; '.join('{} {} [{}] {}'.format(row['code'], row['name'], row['target_name'], row['classification_reason']) for row in members)))
            continue
        common = set(days)
        for row in members:
            common.intersection_update(sizes.get(row['code'], {}))
        if not common:
            audit.append(dict(group=group, reason='所有候选无共同有效规模日期，整组暂不选', details='; '.join('{} {} [{}] {}'.format(row['code'], row['name'], row['target_name'], row['classification_reason']) for row in members)))
            continue
        day = builtins.max(common)
        lag = len(days) - 1 - days.index(day)
        if lag > MAX_STALE_TRADE_DAYS:
            audit.append(dict(group=group, reason='共同规模日期过旧', details=str(day)))
            continue
        ranked = []
        for row in members:
            size, shares, nav = sizes[row['code']][day]
            ranked.append(dict(row, size_yuan=size, size_yi=size / Decimal('100000000'), shares=shares,
                               unit_nav=nav, size_date=str(day), stale_trade_days=lag, group_count=len(members)))
        ranked.sort(key=lambda row: (-row['size_yuan'], row['code']))
        for rank, row in enumerate(ranked, 1):
            row['group_rank'] = rank
        candidates.extend(ranked)
        selected.append(ranked[0])
    selected.sort(key=lambda row: (CATEGORIES.index(row['category']), -row['size_yuan'], row['code']))
    return selected, audit, candidates


def run():
    if not 1 <= PAGE_SIZE <= 3000 or not 1 <= BATCH_SIZE <= 100:
        raise ValueError('PAGE_SIZE 必须为 1~3000；BATCH_SIZE 必须为 1~100')
    if LOOKBACK_DAYS <= MAX_STALE_TRADE_DAYS or MAX_STALE_TRADE_DAYS < 0:
        raise ValueError('回看天数必须大于允许滞后天数')
    today = (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()
    end = pd.Timestamp(AS_OF_DATE).date() if AS_OF_DATE else today - dt.timedelta(days=1)
    if end >= today:
        raise ValueError('请指定已结束的日期，不能使用当日或未来日期')
    days = [pd.Timestamp(day).date() for day in api('get_trade_days')(end_date=end, count=LOOKBACK_DAYS)]
    if not days or days != sorted(set(days)):
        raise ValueError('交易日历为空或乱序')
    universe = api('get_all_securities')(types=['etf'], date=days[-1])
    if universe is None or universe.empty:
        raise ValueError('ETF 列表为空')
    print('截止日期：{}；聚宽 ETF 数量：{}'.format(days[-1], len(universe)), flush=True)
    print('规模 = 同日份额 × 单位净值；只比较同组共同日期；分类为可调整的研究规则。', flush=True)
    records, audit = target_catalog(universe, days[-1])
    # 保留全部已识别跟踪关系参与分组，不把分类未知的竞争者悄悄漏掉。
    sizes = load_sizes([row['code'] for row in records], days)
    selected, issues, candidates = select_largest(records, sizes, days)
    audit.extend(issues)
    result, review, all_candidates = pd.DataFrame(selected), pd.DataFrame(audit), pd.DataFrame(candidates)
    if not result.empty:
        cols = ['category', 'target_name', 'code', 'name', 'size_yi', 'size_date', 'group_count']
        for category in CATEGORIES:
            section = result[result.category == category]
            print('\n{}：{} 只'.format(category, len(section)))
            print(section[cols].to_string(index=False, formatters={'size_yi': lambda v: '{:.2f}'.format(v)}))
    print('\n选出 {} 只；待核对/排除 {} 条，完整明细见 audit_df。'.format(len(result), len(review)))
    print('结果仅覆盖聚宽已识别、数据完整的候选，不代表已经核验全市场覆盖。')
    if not review.empty:
        print(review.head(30).to_string(index=False))
    if EXPORT_CSV:
        for label, frame in [('结果', result), ('待核对', review), ('候选明细', all_candidates)]:
            frame.to_csv('ETF分类去重_{}_{}.csv'.format(label, days[-1]), index=False, encoding='utf-8-sig')
    return result, review, all_candidates


if __name__ == '__main__':
    selected_df, audit_df, candidate_df = run()
    ETF_POOL = selected_df['code'].tolist() if not selected_df.empty else []
    print('\nETF_POOL = {!r}'.format(ETF_POOL))
