"""Audited current proposal versions, not general rules about 'small shareholders'.

Amounts are evidence assertions only: parsing must still read the current source,
match its exact publication version, and reject conflicting or future versions.
"""
ORDINARY_A_SHARE = {
    ('600989.SH', '2023-12-31', '2024-07-18'): {
        'label': '中小股东', 'pretax_per_ten': '3.158',
        'record_date': '2024-07-23', 'ex_date': '2024-07-24',
        'document': 'https://file.finance.sina.com.cn/211.154.219.97:9494/MRGG/CNSESH_STOCK/2024/2024-7/2024-07-18/10334213.PDF',
    },
    ('600989.SH', '2024-12-31', '2025-05-07'): {
        'label': '中小股东', 'pretax_per_ten': '4.598',
        'record_date': '2025-05-12', 'ex_date': '2025-05-13',
        'document': 'https://static.cninfo.com.cn/finalpage/2025-05-07/1223476469.PDF',
    },
    ('600989.SH', '2025-12-31', '2026-04-22'): {
        'label': '中小股东', 'pretax_per_ten': '4.921',
        'record_date': '2026-04-27', 'ex_date': '2026-04-28',
        'document': 'https://static.cninfo.com.cn/finalpage/2026-04-22/1225136155.PDF',
    },
}

# Original 601211 holders did not exchange or multiply their existing shares.
# The 0.62 exchange ratio applies only to former 600837 holders, not this DPS.
UNCHANGED_SHARE_UNIT = {
    ('601211.SH', '2025-03-14', '吸收合并'): {
        'total_shares': '17629708696',
        'document': 'https://static.cninfo.com.cn/finalpage/2025-03-17/1222816248.PDF',
    },
    ('601211.SH', '2025-03-13', '其他变动原因,增发A股上市'): {
        'total_shares': '15515776028',
        'document': 'https://www1.hkexnews.hk/listedco/listconews/sehk/2025/0313/2025031301549_c.pdf',
        'supplement': 'https://static.cninfo.com.cn/finalpage/2025-03-14/1222789137.PDF',
    },
}
