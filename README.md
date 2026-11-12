import importlib

import bot


def test_validate_mint_and_alert_dedupe(monkeypatch, tmp_path):
    db_path = tmp_path / "wolf524.db"
    monkeypatch.setattr(bot, "DATABASE_PATH", str(db_path))
    bot.init_db()

    mint = "So11111111111111111111111111111111111111112"
    assert bot.validate_mint(mint) is True
    assert bot.validate_mint("bad-mint") is False

    assert bot.should_send_alert(100, mint, "price_drop") is True
    bot.mark_alert_sent(100, mint, "price_drop")
    assert bot.should_send_alert(100, mint, "price_drop") is False

    bot.set_user_setting(100, "min_liquidity_usd", 15000)
    assert bot.get_user_setting(100, "min_liquidity_usd", 0) == 15000


def test_threshold_aliases_and_defaults(monkeypatch, tmp_path):
    db_path = tmp_path / "wolf524.db"
    monkeypatch.setattr(bot, "DATABASE_PATH", str(db_path))
    bot.init_db()

    thresholds = bot.get_alert_thresholds(42)
    assert thresholds["min_liquidity_usd"] == bot.DEFAULT_ALERT_SETTINGS["min_liquidity_usd"]
    assert thresholds["liquidity_drop_pct"] == bot.DEFAULT_ALERT_SETTINGS["liquidity_drop_pct"]

    bot.set_user_setting(42, "liquidity_drop_pct", 25)
    thresholds = bot.get_alert_thresholds(42)
    assert thresholds["liquidity_drop_pct"] == 25
