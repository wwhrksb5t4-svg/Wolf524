# Wolf524 Telegram Bot

بوت Telegram بسيط جاهز للتشغيل باستخدام Python.

## المتطلبات

- Python 3.10+
- توكن بوت Telegram من @BotFather

## التثبيت

```bash
python -m venv .venv
source .venv/bin/activate  # على Linux/macOS
# أو
# .venv\Scripts\activate  # على Windows

pip install -r requirements.txt
cp .env.example .env
```

## إعداد التوكن

عدّل ملف `.env` وأضف التوكن الخاص بك:

```env
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
```

## التشغيل

```bash
python bot.py
```

## الأوامر المتاحة

- `/start` — بداية البوت
- `/help` — عرض قائمة الأوامر
- `/status` — التحقق من حالة البوت
- `/about` — نبذة عن البوت

## ملاحظات

- هذا الإصدار يدعم استقبال الرسائل النصية وإعادة إيصالها كـ echo.
- يمكنك توسيع البوت بإضافة أوامر جديدة ومع��لجات إضافية.
