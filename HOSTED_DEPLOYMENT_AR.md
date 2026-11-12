# تشغيل GGP على الاستضافة — دليل النشر

## 1) Build / Start

- Build command: `python -m pip install --upgrade pip && python -m pip install -r requirements.txt`
- Start command: `python -u ggpbot.py` (موجود كذلك في `Procfile`).
- Python runtime: 3.11.
- شغّل **Web Service / Worker واحد فقط** بنسخة واحدة. لا تستخدم Gunicorn بعدة workers ولا أكثر من replica؛ البوت يحتفظ بقفل تشغيل واحد ويعتمد على SQLite محليًا.
- لا تستخدم أمر تشغيل `flask run` أو `gunicorn ggpbot:app`؛ هذا المشروع يبدأ لوحة Flask ومحرك التداول داخل العملية نفسها.

## 2) متغيرات الاستضافة

أضف القيم من لوحة Environment / Variables لدى مزود الاستضافة، ولا ترفع ملف `.env` أو مفاتيح API إلى GitHub.

| Variable | القيمة / الاستخدام |
|---|---|
| `BINGX_API_KEY` | مفتاح BingX بصلاحية قراءة الحساب والتداول المطلوب |
| `BINGX_API_SECRET` | السر المقابل للمفتاح |
| `PAPER_MODE` | `true` للتشغيل الورقي أولًا |
| `ENABLE_LIVE_TRADING` | `false` أثناء الاختبار؛ لا يصبح LIVE إلا مع `PAPER_MODE=false` والمفتاحين |
| `TRADE_JOURNAL_PATH` | مسار SQLite داخل Volume دائم، مثل `/var/data/ggp_trade_journal.sqlite3` إذا كان هذا هو مسار الـ Volume لدى مزودك |
| `HOSTED_PREFLIGHT_INTERVAL_SEC` | اختياري؛ الافتراضي 60 ثانية، والحدود 20–300 |
| `EXCHANGE_HTTP_TIMEOUT_MS` | اختياري؛ الافتراضي 12000ms |
| `PORT` | يحدده مزود الاستضافة تلقائيًا؛ لا تثبته يدويًا عادةً |

عند وجود `PORT` أو متغير استضافة معروف، يكتشف التطبيق أنه يعمل على استضافة ويُلزم فحوصات الجاهزية قبل السماح بإرسال أوامر دخول جديدة.

## 3) فحوصات التشغيل

- `/health`: فحص حياة العملية HTTP؛ يظل 200 عند تعطل BingX مؤقتًا حتى لا يتسبب فحص الحياة في إعادة تشغيل عملية قد تدير مركزًا مفتوحًا.
- `/ready`: فحص جاهزية التداول؛ يرجع 200 بعد تحميل أسواق BingX، ونجاح قراءات الحساب/المراكز في LIVE، واكتمال استرداد Trade Journal. يرجع 503 قبل ذلك.
- في LIVE، أي فشل في قراءة الرصيد أو حالة المراكز أو الاسترداد يبقي بوابة الدخول مغلقة. لا يعيد التطبيق إرسال أوامر MARKET لمجرد Timeout.
- فحص الجاهزية لا يغيّر الاستراتيجية أو الرافعة أو الحجم أو SL/TP أو TP1/TP2.

يفضّل ضبط Health Check لدى مزود الاستضافة على `/health`، ويمكن استخدام `/ready` كـ Readiness Check منفصل إذا كان المزود يدعم ذلك.

## 4) تخزين SQLite

ملف `data/ggp_trade_journal.sqlite3` هو سجل استرداد الصفقة بعد إعادة تشغيل الخدمة. أنشئ Persistent Volume لدى مزود الاستضافة واضبط `TRADE_JOURNAL_PATH` إلى مسار داخل نقطة تركيب هذا الـ Volume. لا تعتمد على نظام ملفات مؤقت؛ إعادة نشر الخدمة على قرص مؤقت قد تفقد سجل الصفقات وتضع الاسترداد في حالة غير معروفة.

## 5) بوابة التشغيل الحي

ابدأ بـ `PAPER_MODE=true`. بعد التأكد من ثبات الخدمة وظهور `ready=true`، اختبر حساب BingX Demo/VST وتحقق من صلاحيات API ووضع المركز وقراءة Native SL. لا تحول إلى LIVE لمجرد أن GitHub Actions نجح؛ الاختبارات الآلية لا تتصل بحسابك ولا تثبت صلاحية مفاتيحك الفعلية أو إعدادات مزود الاستضافة.

للتداول الحقيقي فقط بعد اختبار Demo ومراجعة سجل الاسترداد:
- `PAPER_MODE=false`
- `ENABLE_LIVE_TRADING=true`
- `BINGX_API_KEY` و`BINGX_API_SECRET` صحيحان، مع صلاحيات BingX اللازمة.

لا تضع صلاحية السحب على مفتاح API. فعّل IP whitelist لدى BingX عندما يدعم مزود الاستضافة عنوان خروج ثابتًا.
