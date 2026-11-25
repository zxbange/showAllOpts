from check_quality import additions


def test_legacy_debt_can_only_shrink():
    baseline = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 2}},
        'format': ['backend/app/example.py'],
    }
    cleaner = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 1}},
        'format': [],
    }
    assert additions(cleaner, baseline) == []


def test_new_diagnostic_fails_even_when_total_debt_decreases():
    baseline = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 3}},
        'format': [],
    }
    changed = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 1, 'F401:new': 1}},
        'format': ['backend/app/new.py'],
    }
    assert additions(changed, baseline) == [
        'backend/app/example.py: new F401 diagnostic (1)',
        'backend/app/new.py: new formatting debt; run ruff format',
    ]


def test_same_diagnostic_added_again_is_rejected():
    baseline = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 1}},
        'format': [],
    }
    changed = {
        'version': 1,
        'lint': {'backend/app/example.py': {'F401:old': 2}},
        'format': [],
    }
    assert additions(changed, baseline) == ['backend/app/example.py: new F401 diagnostic (1)']
