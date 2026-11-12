import sqlite3
from types import SimpleNamespace

import app


def test_spam_preferences_create_and_migrate(monkeypatch, tmp_path):
    db_path = tmp_path / "prefs.sqlite"
    monkeypatch.setattr(app.core, "DATABASE_PATH", str(db_path))
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE spam_filter_preferences (user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)")
        db.execute("INSERT INTO spam_filter_preferences (user_id, enabled) VALUES (9, 0)")

    app.init_preferences_db()
    assert app.spam_filter_enabled(9) is False
    assert app.get_spam_threshold(9) == app.SPAM_FILTER_THRESHOLD

    app.set_spam_threshold(9, 35)
    assert app.get_spam_threshold(9) == 35
    app.set_spam_filter(9, True)
    assert app.spam_filter_enabled(9) is True
    assert app.get_spam_threshold(9) == 35


def test_spam_score_and_threshold_validation():
    clean = SimpleNamespace(
        name="Example Token", symbol="EXM", liquidity_usd=100_000,
        volume_24h=20_000, buys_24h=100, sells_24h=80,
        top10_percent=20, mint_authority=False, freeze_authority=False,
    )
    spam = SimpleNamespace(
        name="Free giveaway aaaaaa", symbol="CLAIM", liquidity_usd=100,
        volume_24h=10_000, buys_24h=1, sells_24h=1,
        top10_percent=90, mint_authority=True, freeze_authority=True,
    )
    assert app.spam_signals(clean)[0] < app.spam_signals(spam)[0]
    try:
        app.set_spam_threshold(1, 0)
    except ValueError:
        pass
    else:
        raise AssertionError("Threshold outside 1-100 must be rejected")
