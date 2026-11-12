# Wolf524 — بوت اكتشاف وتحليل توكنات Solana

يبحث البوت تلقائيًا عن توكنات جديدة من عدة مصادر عامة، يجمع مؤشرات السوق والسلسلة، ثم يرسل قائمة بحث مرتبة مع مؤشرات المخاطر.

## الأدوات والتحليلات

- DexScreener: ملفات التوكنات الحديثة، boosts، السعر والسيولة والحجم وعدد المعاملات وتغير السعر.
- GeckoTerminal: مجمعات السيولة الحديثة على Solana.
- Solana RPC: صلاحيات Mint/Freeze وأكبر حسابات التوكن.
- RugCheck: إثراء إضافي بالمخاطر عند توفر الخدمة.
- Birdeye (اختياري): قوائم التوكنات الجديدة عند توفر مفتاح API.
- تحليل محلي قابل للتفسير: السيولة، نسبة الحجم للسيولة، عدد المعاملات، التوزيع، ومؤشرات المخاطر.

## أوامر Telegram

- /discover — بحث وفحص الآن.
- /autodiscover on — تشغيل تنبيهات دورية.
- /autodiscover off — إيقاف التنبيهات.
- /scan <mint> — فحص عقد محدد.
- /watch <mint> و /unwatch <mint> — إدارة قائمة المراقبة.
- /watchlist و /top — عرض وترتيب قائمة المراقبة.
- /settings — عرض حدود التنبيهات الحالية.
- /setalert <key> <value> — ضبط عتبات التنبيهات مثل liquidity، price، minliq، minvol، risk.

## إعداد Railway

اربط مستودع `wwhrksb5t4-svg/Wolf524` بخدمة Docker. في Service → Variables أضف `BOT_TOKEN` (توكن @BotFather) ثم طبّق التغييرات وأعد النشر. لـ Birdeye اختياري أضف `BIRDEYE_API_KEY` كـ Secret.

## إحصائيات التنبيه

- لا يتم إرسال نفس تنبيه لنفس المستخدم/العملة/نوع التنبيه إلا كل 6 ساعات.
- يدعم التنبيهات عند انحدار السيولة والسعر مع إعدادات قابلة للتعديل لكل مستخدم.
- هذا النظام يهدف إلى تقليل الضجيج دون إلغاء التنبيهات المهمة.

## حدود التقييم والسلامة

درجة النشاط البحثية مقياس heuristic للترتيب وليست احتمال نجاح أو توقع عائد. درجة المخاطر المحلية وRugCheck معروضة فقط كأدوات مساعدة وليست ضمانًا ضد الاحتيال.

## مصادر API

- DexScreener: https://docs.dexscreener.com/api/reference
- GeckoTerminal: https://api.geckoterminal.com/docs/
- RugCheck: https://api.rugcheck.xyz/swagger/index.html
- Birdeye: https://docs.birdeye.so/
