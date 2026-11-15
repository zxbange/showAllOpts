"""净利润断层的只读查询映射；由本地工具内嵌，聚宽无需上传此文件。"""
# BEGIN SHARED EARNINGS SOURCES


_EarningsSourceBase = declarative_base()  # 与入库 Base 隔离，绝不在目标库创建源表。


class _EarningsExpress(_EarningsSourceBase):
    __tablename__ = 'LC_FSPerformedLetters'
    ID = Column(BigInteger, primary_key=True)
    CompanyCode = Column(Integer)
    InfoPublDate = Column(DateTime)
    EndDate = Column(Date)
    NPParentCompanyOwners = Column(Numeric)
    NPCOwnersYOY = Column(Numeric)


class _EarningsCompany(_EarningsSourceBase):
    __tablename__ = 'SecuMain'
    ID = Column(BigInteger, primary_key=True)
    CompanyCode = Column(Integer)
    SecuCode = Column(String)
    SecuCategory = Column(Integer)


def _earnings_source_table(database, name):
    """只复用已加载对象；否则使用已核实字段，避免 getattr 触发远程表结构加载。"""
    cached = builtins.vars(database).get(name)
    if cached is not None:
        return cached
    return {'LC_FSPerformedLetters': _EarningsExpress, 'SecuMain': _EarningsCompany}[name]

# END SHARED EARNINGS SOURCES
