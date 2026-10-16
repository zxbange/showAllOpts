"""Read-only connection and table check; prints aggregate counts, never credentials."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import Settings
from app.core.database import build_engine
from app.models.momentum import IndustryMomentum as Row


def main():
    engine = build_engine(Settings())
    try:
        with engine.connect() as connection:
            result = connection.execute(select(
                Row.industry_type, func.count(), func.min(Row.trade_date), func.max(Row.trade_date),
            ).group_by(Row.industry_type)).all()
            for industry_type, count, first, last in result:
                print(f'{industry_type}: rows={count}, first={first}, latest={last}')
            if not result:
                print('Connection successful; industry_momentum has no records.')
    except SQLAlchemyError as exc:
        print(f'Database check failed ({type(exc).__name__}). Check .env and the database network whitelist.')
        raise SystemExit(1) from None
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
