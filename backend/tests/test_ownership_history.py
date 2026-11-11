import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace
import pandas as pd
import pytest
from sqlalchemy import Column, Date, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from test_ownership_collector import mod


def test_historical_delisted_resume_holes_and_preserve_confirmed(mod, monkeypatch):
    base = declarative_base()
    class Fund(base):
        __tablename__ = 'fund'
        id = Column(Integer, primary_key=True)
        code = Column(String); symbol = Column(String); period_end = Column(Date); pub_date = Column(Date)
    class Floats(base):
        __tablename__ = 'floats'
        id = Column(Integer, primary_key=True)
        code = Column(String); end_date = Column(Date); pub_date = Column(Date)
        change_reason_id = Column(Integer); sharesnature_id = Column(Integer)
    class Holders(base):
        __tablename__ = 'holders'
        id = Column(Integer, primary_key=True)
        code = Column(String); end_date = Column(Date); pub_date = Column(Date)
        share_holders = Column(Integer); a_share_holders = Column(Integer)
        b_share_holders = Column(Integer); h_share_holders = Column(Integer)
    source = create_engine('sqlite://'); base.metadata.create_all(source)
    source_session = sessionmaker(bind=source)()
    code = '600001.XSHG'; period = dt.date(1995, 6, 30)
    source_session.add(Holders(id=1, code=code, end_date=period, pub_date=dt.date(1995, 7, 1), share_holders=42))
    source_session.add(Holders(id=2, code=code, end_date=dt.date(1995, 9, 30), pub_date=dt.date(2099, 1, 1), share_holders=99))
    source_session.commit()
    def run_query(q):
        with source.connect() as connection:
            rows = connection.execute(q.statement)
            return pd.DataFrame(rows.fetchall(), columns=rows.keys())
    finance = SimpleNamespace(FUND_PORTFOLIO_STOCK=Fund, STK_SHAREHOLDER_FLOATING_TOP10=Floats,
                              STK_HOLDER_NUM=Holders, run_query=run_query)
    target = create_engine('sqlite://')
    @contextmanager
    def scope(Base, config):
        Base.metadata.create_all(target)
        yield sessionmaker(bind=target)
    monkeypatch.setattr(mod, 'database_scope', scope)
    n = mod._ownership_strategy({'finance': finance, 'query': source_session.query}, mod.DB_CONFIG)
    assert len(n.run(history_mode='all')) == 1
    Session = sessionmaker(bind=target)
    with Session() as session:
        row = session.query(n.OwnershipHolders).one()
        assert row.end_date == period and row.payload['share_holders'] == 42
        stock = session.query(n.OwnershipStock).one()
        assert stock.name == code and stock.industry is None
        record = session.query(n.DisclosureProbe).filter_by(key='history-scope').one()
        assert record.payload['fund']['status'] == 'verified_empty_visible_scope'
        assert record.payload['holders']['first_visible_period'] == '1995-06-30'
        row.payload = dict(row.payload, share_holders=123); session.commit()
    assert n.run(history_mode='all').empty
    with Session() as session:
        assert session.query(n.OwnershipHolders).one().payload['share_holders'] == 123
        session.delete(session.query(n.OwnershipHolders).one()); session.commit()
    assert len(n.run(history_mode='all')) == 1


def test_source_error_is_not_verified_empty(mod):
    n = mod._ownership_strategy({'finance': SimpleNamespace(), 'query': None}, mod.DB_CONFIG)
    with pytest.raises(AttributeError):
        n.run(history_mode='all')
