# -*- coding: utf-8 -*-
"""聚宽研究环境：只读核对两融担保比例相关数据表，不采集全量、不写数据库。

把本文件完整粘贴到聚宽新的研究单元格运行。仅打印可见候选表、字段及
含担保/维持比例字段的一条公开样本，不需要填写或输出账号、密码。
候选表按名称发现，未命中或无权限不能据此断言聚宽全平台没有该数据。
"""
import builtins as _builtins
import inspect as _inspect


def candidate_names(database, seeds):
    """访问已知表触发表目录加载；避免 NumPy.any 覆盖 Python 内置函数。"""
    words = ('margin', 'guarantee', 'collateral', 'assure', '融资', '担保', '维持')
    names = _builtins.set(seeds)
    for name in _builtins.dir(database):
        if name.startswith('_'):
            continue
        lowered = name.lower()
        if lowered.startswith(('mt_', 'stk_mt_')) or _builtins.any(word in lowered for word in words):
            names.add(name)
    return _builtins.sorted(names)


def run():
    from jqdata import finance, jy, query

    print('两融字段核对 v2：加载表目录、排除接口方法；每张候选表最多读取一行。')
    field_words = ('maintain', 'maintenance', 'guarant', 'collateral', 'assur', 'margin', '担保', '维持')
    databases = (
        ('finance', finance, 'STK_INCOME_STATEMENT', ('STK_MT_TOTAL',)),
        ('jy', jy, 'LC_IncomeStatement', ('MT_TradingStat',)),
    )
    for label, database, warmup, seeds in databases:
        # 首次属性访问可加载懒加载目录；不查询这张无关表的业务记录。
        try:
            _builtins.getattr(database, warmup)
        except Exception as exc:
            print('{} 表目录初始化提示：{}'.format(label, _builtins.type(exc).__name__))
        candidates = candidate_names(database, seeds)
        print('\n{} 候选表：{}'.format(label, candidates))
        read_count = 0
        for name in candidates[:25]:
            try:
                table = _builtins.getattr(database, name)
                if not _inspect.isclass(table):
                    print('{} 跳过：不是数据表类。'.format(name))
                    continue
                frame = database.run_query(query(table).limit(1))
                read_count += 1
                columns = _builtins.list(frame.columns)
                print('{} 字段：{}'.format(name, columns))
                matched = [col for col in columns
                           if _builtins.any(word in _builtins.str(col).lower() for word in field_words)]
                if matched:
                    dates = [col for col in columns
                             if _builtins.any(word in _builtins.str(col).lower()
                                              for word in ('date', 'day', 'period')) and col not in matched]
                    print('候选字段样本（不保证为最新日期）：')
                    print(frame[dates + matched].to_string(index=False))
            except Exception as exc:
                print('{} 无法读取：{}'.format(name, _builtins.type(exc).__name__))
        print('{} 成功读取 {} 张候选表。'.format(label, read_count))
        if _builtins.len(candidates) > 25:
            print('仅检查前25张候选表，请先反馈以上结果，再限定需要进一步检查的表。')
    print('\n核对完成。没有命中字段仅代表本次候选范围未确认，不代表全库不存在。')


if __name__ == '__main__':
    run()
