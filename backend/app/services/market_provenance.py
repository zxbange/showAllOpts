"""Attach source/method only when a displayed row matches our durable write."""
from pathlib import Path

from app.services.market_writer import row_hash
from app.services.market_methodology import JOINQUANT_HEAT_METHOD


def history_provenance(request, kind):
    collector = getattr(request.app.state, 'market_collection', None)
    hashes = {}
    methods = {}
    sources = {}
    if collector is not None and (Path(collector.root) / 'state.sqlite').exists():
        try:
            with collector.connect() as db:
                for row in db.execute('SELECT trade_date,heat_hash,crowding_hash FROM days'):
                    value = row[kind + '_hash']
                    if value:
                        hashes.setdefault(row['trade_date'], set()).add(value)
                # The source commit can succeed before the worker acknowledges
                # it locally. Exact-value intent matching also covers that crash
                # window; an uncommitted intent never matches a displayed row.
                for row in db.execute("SELECT key,value FROM metadata WHERE key LIKE ?", (f'write-intent:%:{kind}',)):
                    _, day, stored_kind = row['key'].split(':', 2)
                    value = row['value']
                    if stored_kind == kind and len(value) == 64:
                        hashes.setdefault(day, set()).add(value)
                for row in db.execute("SELECT key,value FROM metadata WHERE key LIKE ?", (f'write-intent-history:%:{kind}:%',)):
                    _, day, stored_kind, value_hash = row['key'].split(':', 3)
                    if stored_kind == kind and row['value'] == value_hash and len(value_hash) == 64:
                        hashes.setdefault(day, set()).add(value_hash)
                for row in db.execute("SELECT key,value FROM metadata WHERE key LIKE ?", (f'source:{kind}:%',)):
                    _, stored_kind, value_hash = row['key'].split(':', 2)
                    if stored_kind == kind and row['value'] in ('sina', 'public_market'):
                        sources[value_hash] = row['value']
                for row in db.execute("SELECT key,value FROM metadata WHERE key LIKE ?", (f'methodology:{kind}:%',)):
                    _, stored_kind, value_hash = row['key'].split(':', 2)
                    if stored_kind == kind and len(value_hash) == 64:
                        methods[value_hash] = row['value']
        except Exception:
            pass  # Reading existing history must work before the first collection.

    def metadata(row):
        expected = hashes.get(row.trade_date.isoformat(), set())
        actual = row_hash(kind, row)
        owned = bool(expected) and actual in expected
        result = {'source': sources.get(actual, 'sina') if owned else 'joinquant'}
        if kind == 'heat':
            # Missing metadata belongs to the previous Sina contract. It must
            # not be relabelled merely because the running software changed.
            jq_method = not owned or methods.get(actual) == JOINQUANT_HEAT_METHOD
            result.update(price_basis='qfq' if jq_method else 'bfq',
                          denominator='eligible' if jq_method else 'universe')
        return result
    return metadata
