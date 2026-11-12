def candidate_summary(report: core.TokenReport) -> str:
    links = getattr(report, "community_links", [])
    coverage = getattr(report, "community_coverage", 0)
    link_lines = "\n".join(f"• {label}: {url}" for label, url in links) or "• لا توجد روابط مجتمع/موقع منشورة في بيانات DexScreener."
    phantom_link = core.phantom_token_url(report.mint)
    return (
        f"🆕 مرشح للمراجعة — {report.name} ({report.symbol})\n"
        f"مؤشر المخاطر الأولي: {report.risk_score}/100 · السيولة: {core.usd(report.liquidity_usd)}\n"
        f"حجم 24س: {core.usd(report.volume_24h)} · معاملات شراء/بيع: {report.buys_24h}/{report.sells_24h}\n"
        f"بصمة الروابط العامة: {coverage}/4 فئات (X/Telegram/Discord/موقع أو غيره)\n"
        f"روابط المشروع المدرجة:\n{link_lines}\n"
        f"عنوان التوكن: {report.mint}\n"
        f"DexScreener: https://dexscreener.com/solana/{report.mint}\n"
        f"Solscan: https://solscan.io/token/{report.mint}\n"
        f"Phantom: {phantom_link}\n\n"
        "تنبيه: هذا ترتيب آلي لمعلومات السوق وروابط عامة فقط. لا يفحص حجم/تفاعل المجتمع أو صدق الحسابات أو مشاعر المنشورات؛ وجود الروابط لا يثبت شرعية المشروع. ليست توصية شراء وقد تخسر كامل المبلغ."
    )
