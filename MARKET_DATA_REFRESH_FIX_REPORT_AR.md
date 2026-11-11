# تقرير إصلاح تحديث بيانات السوق والمؤشرات — GGP

**التاريخ:** 2026-11-10
**الهدف:** إصلاح تدفق بيانات السوق والمؤشرات من BingX إلى Profit Brain أثناء صفقة مفتوحة.
**الحالة:** إصلاح مُثبت محليًا (Mock). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. السبب الجذري (مُثبت بالكود والتنفيذ)

في `LiveTradeManager._apply_management` كان المسار يطلب:

```python
raw_frame = get_ohlcv_safe(symbol, 80)
```

بينما `fetch_ohlcv` يشترط حدًّا أدنى صارمًا:

```python
data = safe_api_call(ex.fetch_ohlcv, sym, INTERVAL, limit=limit)
if not data or len(data) < 100:
    return None          # <-- أي طلب أقل من 100 شمعة يعود None
```

النتيجة: `get_ohlcv_safe(symbol, 80)` تُعيد **None دائمًا**، فيسقط التنفيذ فورًا إلى:

```text
[PROFIT_BRAIN] <symbol>: insufficient completed candles; keep current protection
```

ويعود قبل أي حساب، فلا يتحدّث `adx_live` / `di_plus_live` / `di_minus_live` /
`continuation_probability` / `trade_state` أبدًا بعد الدخول — تبقى مجمّدة على قيم لحظة الدخول.
وهذا يفسّر تمامًا ثبات ADX=28.9 وDI+=22.5 وDI-=21.8 وContinuation=0.50 وState=RANGE_CHOP.

**الإثبات المباشر:**
```text
fetch_ohlcv(limit=80)  -> None      (80 < 100)
fetch_ohlcv(limit=120) -> rows: 120
```

**لم تكن المشكلة**: Cache، ولا Thread منفصل، ولا Timezone، ولا فريم خاطئ، ولا قفل — بل
**عدم تطابق الحد الأدنى لعدد الشموع المطلوبة مع حد الجالب**.

---

## 2. لماذا ظلت ADX وDI وContinuation ثابتة
لأن كل دورة إدارة صفقة كانت تخرج عند السطر الأول بعد جلب الشموع، قبل تنفيذ كتلة الحساب
الثقيل (`if now - self.last_heavy_calc_ts >= 5:`). لذلك لم تُكتب أي قيمة جديدة في `STATE`.

## 3. سبب رسالة "insufficient completed candles"
هي فرع الحماية بعد `df_live is None` — وكان يُطلق دائمًا لأن الجلب يعود `None` بحد 80 شمعة.

---

## 4. الملفات والدوال التي تم تعديلها

| الملف | الدالة | التعديل |
|---|---|---|
| `ggpbot.py` | `_apply_management` | حد OHLCV صحيح (120 ≥ الحد الأدنى 100)؛ حماية من استثناء الجلب؛ فصل الشمعة المغلقة عن قيد التكوين؛ حساب freshness؛ سجلات `[MARKET_DATA_REFRESH]`/`[INDICATOR_REFRESH]`/`[PROFIT_BRAIN_DATA_VALIDATION]`؛ عدّاد `indicator_snapshot_version` |
| `ggpbot.py` | كتلة الحساب الثقيل | كتابة `indicator_snapshot_version`/`indicator_snapshot_type=CONFIRMED`/`last_rsi`/`last_vwap`؛ تحصين `prev_di_spread` |
| `ggp_market_data.py` (جديد) | `MarketDataRefresh` | محرّك جلب+تحقق مستقل: فصل CONFIRMED/LIVE، كشف STALE/INSUFFICIENT، تخزين provenance |
| `ggpbot.py` | الاستيرادات | `REQUIRED_CLOSED_CANDLES`, `MANAGEMENT_OHLCV_LIMIT`, `FETCH_MIN_CANDLES`, `_timeframe_seconds` |

**لم تُلمس**: الاستراتيجية، RF Engine، Scanner، Dashboard، Telegram، Flask، Position Sizing،
Leverage، Native SL، TP1/TP2، Trade Execution، Recovery Hold، قواعد قرار Profit Brain.

---

## 5. مصدر بيانات كل مؤشر

| المؤشر | المصدر | الفريم | الفترة | نوع اللقطة |
|---|---|---|---|---|
| ADX / DI± | BingX OHLCV (شموع مغلقة) | 15m | 14 | CONFIRMED |
| ATR | BingX OHLCV (شموع مغلقة) | 15m | 14 | CONFIRMED |
| RSI | BingX OHLCV (شموع مغلقة) | 15m | 14 | CONFIRMED |
| EMA state | BingX OHLCV (شموع مغلقة) | 15m | 20/50 | CONFIRMED |
| VWAP | BingX OHLCV (شموع مغلقة) | 15m | جلسة | CONFIRMED |
| Volume ratio | BingX OHLCV (شموع مغلقة) | 15m | 10 | CONFIRMED |
| Continuation | محرك داخلي من الشموع المغلقة | 15m | — | CONFIRMED |
| Market Regime | مصنّف داخلي من الشموع المغلقة | 15m | — | CONFIRMED |
| Mark/Last Price, PnL, ROE | BingX Ticker | — | — | LIVE (كل دورة) |

---

## 6. آلية التحديث كل 5 ثوانٍ

- السعر الحي (Mark/Last/PnL/ROE) يُحدَّث كل دورة إدارة (2–5 ثوانٍ حسب `target_interval`).
- OHLCV يُجلب كل دورة عبر `get_ohlcv_safe` مع Cache TTL = 10 ثوانٍ أثناء وجود صفقة (يحمي Rate Limits).
- الحساب الثقيل للمؤشرات كل 5 ثوانٍ (`last_heavy_calc_ts`).
- المؤشرات **المؤكدة** لا تتغير إلا عند وصول شمعة مغلقة جديدة؛ بين الشموع تُعلَن `UNCHANGED_BUT_VALID`.

---

## 7. الفرق بين Confirmed وLive Snapshots

- **CONFIRMED:** من الشموع المغلقة فقط، `snapshot_type=CONFIRMED`، يزداد `snapshot_version` فقط
  عند تغيّر آخر شمعة مغلقة. يُستخدم لكل قرارات الإدارة.
- **LIVE / PROVISIONAL:** اختياري من الشمعة قيد التكوين، `snapshot_type=LIVE_PROVISIONAL`،
  ولا يستبدل القيم المؤكدة ولا يطلق قرارات تتطلب إغلاق شمعة (مطبَّق في `ggp_market_data`).

Freshness: `FRESH` / `UNCHANGED_BUT_VALID` / `STALE` / `INVALID` / `INSUFFICIENT`.

---

## 8. نتائج اختبارات Regression

- **`python -m pytest -q` → 267 passed, 0 failed** (+18 في `test_market_data_refresh.py`).
- `compileall` → exit 0. `preflight` → PREFLIGHT OK.

التغطية (المطلوب 17): حد OHLCV ≥ الحد الأدنى؛ رفض <100؛ الجلب بحد الإدارة؛ فصل الشمعة قيد
التكوين؛ تحديث CONFIRMED عند شمعة جديدة فقط؛ عدم التغيير عند مرور 5 ثوانٍ؛ عدم استبدال المؤكد
بالمؤقت؛ كشف STALE؛ كشف INSUFFICIENT؛ تحقق الفريم؛ الحد الأدنى للشموع؛ عدم تمرير STALE كـFRESH؛
عدم استخدام ADX/DI قديمة؛ استمرار Native SL عند فشل البيانات؛ عدم لمس TP1/TP2؛ لا Race (عدّاد
version متسق)؛ Rate Limits (إعادة استخدام Cache)؛ + اختبار دورة متعددة وتكامل حقيقي.

---

## 9. إثبات أن Profit Brain يستقبل أحدث البيانات (تشغيل حقيقي)

```text
[MARKET_DATA_REFRESH]
  Symbol: SOL/USDT:USDT | Timeframe: 15m
  Last Closed Candle: 1794346200.0 | Current Candle: 1794348000.0
  Received Candle Count: 121 | Required Candle Count: 60
  OHLCV Freshness: FRESH | Cache Status: HIT

[INDICATOR_REFRESH]
  ADX: 99.99 | DI_PLUS: 3.00 | DI_MINUS: 0.00 | RSI: 100.00 | ATR: 0.33627571
  Continuation: 0.96 | Market Regime: COMPRESSION
  Snapshot Type: CONFIRMED | Snapshot Version: 1 | Freshness: FRESH | Update Status: UPDATED

[PROFIT_BRAIN_DATA_VALIDATION]
  Indicator Snapshot Version: 1 | Data Freshness: FRESH
  Required Indicators Available: ['adx','di_plus','di_minus','continuation','regime']
  Missing Indicators: [] | Decision Allowed: True
```

المؤشرات تُحسب الآن فعليًا وتصل إلى `STATE` (لم تعد مجمّدة على قيم الدخول).

---

## 10. تأكيد عدم تغيير الاستراتيجية أو الحمايات

`git diff --name-only` → `ggpbot.py` فقط + ملفان جديدان. لم تُلمس قواعد الدخول/الخروج، ولا
عتبات ADX/RSI/DI، ولا Native SL، ولا TP1/TP2، ولا Position Sizing.

---

## 11. القيود وما لم يُختبر

- كل الاختبارات محلية (Mock). **لم** يُختبر على BingX Demo.
- لم تُتحقق أعداد الشموع/الفريم/التزامن الحقيقي من المنصة.
- لم يُقارن توقيت `timestamp` الفعلي من BingX.

**لا أدّعي نجاح التحقق LIVE.** يجب اختبار BingX Demo أولًا.

---

## 12. ZIP النهائي

- الاسم: `GGP_RELEASE_9_FINAL_HARDENED.zip`
- SHA-256: `5949debe724c9470edabdc264024757164060322065d207e5acc51f0ad770f89`
