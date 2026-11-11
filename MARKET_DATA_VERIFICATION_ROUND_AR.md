# تقرير جولة التحقق النهائية — تحديث بيانات السوق والمؤشرات (GGP)

**التاريخ:** 2026-11-10
**النطاق:** تحقق محدود + إصلاح دقيق واحد (تقرير Cache). لا تغيير في الاستراتيجية أو المعمارية.
**الحالة:** أدلة فعلية من تشغيل الكود (Mock). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. إثبات تغيّر Indicator Snapshot عند إغلاق شمعة 15m جديدة (البند 1 و5)

سجل فعلي من تشغيل متعدد الدورات (`_apply_management` الحقيقية):

```text
--- CYCLE 1 (candle A)        -> version=1 closed_ts=1794347100.0
--- CYCLE 2 (candle A again)  -> version=1 closed_ts=1794347100.0
--- CYCLE 3 (candle A again)  -> version=1 closed_ts=1794347100.0
--- CYCLE 4 (NEW candle B)    -> version=2 closed_ts=1794348000.0
```

- `snapshot_version` ثابت (1) خلال ثلاث دورات على نفس الشمعة المغلقة.
- يزيد إلى 2 **مرة واحدة فقط** عند وصول شمعة مغلقة جديدة، ويتقدم `closed_ts`.
- يُسجَّل Version وTimestamp في كل تحديث داخل `[INDICATOR_REFRESH]`.

**وصول النسخة الجديدة إلى Profit Brain** (سجل فعلي):
```text
[PROFIT_BRAIN_DATA_VALIDATION]
  Indicator Snapshot Version: 2
  Snapshot Timestamp: 1794348000.0
  Data Freshness: FRESH
  Required Indicators Available: ['adx','di_plus','di_minus','continuation','regime']
  Missing Indicators: []
  Decision Allowed: True
```

---

## 2. TTL=10 ثوانٍ لا يجعل البيانات القديمة تظهر FRESH (البند 2)

- `get_ohlcv_safe_meta` يستخدم TTL = 10 ثوانٍ أثناء وجود صفقة، ويعيد `CACHE_HIT` عند الخدمة من الذاكرة.
- **دليل انتهاء الصلاحية (سجل فعلي):**
```text
call1 source=FRESH_REQUEST exchange_calls=1
call2 source=CACHE_HIT      exchange_calls=1     (خلال TTL)
call3 (expired cache) source=FRESH_REQUEST exchange_calls=2   (مدخل قديم -> إعادة جلب)
```
- Freshness لا يعتمد على Cache بل على **آخر شمعة مغلقة**: إذا لم تتغير ⇒ `UNCHANGED_BUT_VALID`،
  وإذا رجعت للخلف ⇒ `STALE`. لذلك لا يظهر مدخل Cache قديم أبدًا كـ`FRESH` جديد.

---

## 3. الفرق بين طلب OHLCV جديد وCache HIT (البند 3) — إصلاح

**العيب:** كان السجل `Cache Status: {'HIT' if received else 'MISS'}` — أي يُسجّل HIT لمجرد وجود شموع،
ولا يميّز إعادة استخدام الذاكرة عن طلب جديد إلى BingX.

**الإصلاح:** أضفت `get_ohlcv_safe_meta()` التي تعيد `(frame, source)` حيث `source ∈ {FRESH_REQUEST, CACHE_HIT, NONE}`،
وسجل `[MARKET_DATA_REFRESH]` يعرض الآن:
```text
Data Source: FRESH_REQUEST | CACHE_HIT
Cache Status: MISS          | HIT
```
فلا يُسجَّل Cache HIT على أنه طلب جديد، والعكس.

---

## 4. مراجعة ADX=99.99 وDI+=3 وDI-=0 وRSI=100 (البند 4)

هذه القيم **صحيحة رياضيًا** وليست خطأ. عند سلسلة **رتيبة صاعدة تمامًا** (اختبار فعلي):
```text
MONOTONIC UP -> ADX=99.99 DI+=70.96 DI-=0.00 RSI=100.00
OSCILLATING  -> ADX=21.00 DI+=29.78 DI-=31.58 RSI=45.09
```
- ADX=100: لأن `DX=100` في كل شمعة عند حركة اتجاهية أحادية (لا يوجد حركة هابطة إطلاقًا).
- DI-=0: لا توجد حركات هابطة ⇒ `minus_dm=0`.
- RSI=100: كل التغيرات موجبة ⇒ متوسط الخسارة = 0.
- مع بيانات متذبذبة واقعية تعود القيم معتدلة (ADX=21، DI متقارب، RSI=45).

**لم أعدّل أي عتبة ADX/RSI/DI ولم أفرض أي قيمة.** ملاحظة: القيم القصوى في التشغيل السابق كانت بسبب
بيانات اختبار رتيبة (خطي) وليست من BingX الحقيقي — يجب تأكيدها على بيانات Demo فعلية.

---

## 5. اختبار متعدد الدورات (البند 5)
مغطّى في `test_version_increments_only_on_new_closed_candle` و`test_multi_cycle_simulation_refreshes_then_holds`:
ثبات Version بين الشموع المغلقة ثم زيادته عند وصول شمعة جديدة.

---

## 6. فشل BingX API وانتهاء Cache (البند 6)

**فشل API (سجل فعلي):** عند رفع استثناء من الجالب:
```text
[MARKET_DATA_REFRESH] SOL/USDT:USDT: fetch error RuntimeError: BingX 503;
  keep current protection (Native SL unchanged)
```
- `last_market_data_freshness = INVALID`، والتحقق يعرض `Decision Allowed: False` و`Reject Reason: stale_or_missing_market_data`.
- **لا تُستخدم بيانات STALE في قرارات Profit Brain**، ولا يُصنع أي قرار.

**انتهاء Cache:** مدخل قديم (> TTL) يُهمَل ويُعاد جلبه (دليل البند 2).

---

## 7. حفظ Native SL وTP1/TP2 (البند 7)
اختبار `test_data_failure_never_touches_protection` و`test_api_failure_marks_invalid_and_blocks_decision`:
عند فشل البيانات تبقى `synthetic_sl` و`tp1_price` و`tp2_price` و`native_sl_order_id` و`native_sl_verified`
**دون تغيير**. لم تُلمس قواعد الإدارة.

---

## 8. نتائج الاختبارات الفعلية

- **`python -m pytest -q` → 272 passed, 0 failed** (مستقرة عبر تشغيلين؛ +5 اختبارات تحقق جديدة).
- `compileall` → exit 0. `preflight` → PREFLIGHT OK.
- `test_market_data_refresh.py` → 23 passed.

---

## 9. الملفات المعدّلة
- `ggpbot.py`: `get_ohlcv_safe_meta` جديدة (تمييز CACHE_HIT/FRESH_REQUEST)؛ سجل `Data Source` في `[MARKET_DATA_REFRESH]`.
- `test_market_data_refresh.py`: +5 اختبارات تحقق.
- **لم تُلمس**: الاستراتيجية، RF، Scanner، Dashboard، Telegram، Flask، Position Sizing، Leverage،
  Native SL، TP1/TP2، Execution، Recovery، قواعد Profit Brain (مُثبت بـ`git diff --name-only`).

---

## 10. القيود
كل الأدلة من Mock (بيانات مُصنّعة). **لم** يُختبر على BingX Demo: أعداد الشموع الحقيقية، توقيت
`timestamp`، وسلوك الفريم الفعلي. **لا أدّعي جاهزية LIVE قبل اختبار BingX Demo.**

---

## 11. ZIP النهائي
- الاسم: `GGP_RELEASE_9_FINAL_HARDENED.zip`
- SHA-256: `0fe4730b9a229124dde371cb5124342871f4b23d11080d25e697e4e0e853c824`
