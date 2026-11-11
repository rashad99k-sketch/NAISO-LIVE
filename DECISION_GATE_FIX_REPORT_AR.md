# تقرير إصلاح بوابة قرار بيانات السوق (GGP)

**التاريخ:** 2026-11-10
**النطاق:** فرض بوابة صلاحية البيانات كشرط تنفيذ فعلي في `LiveTradeManager._apply_management`.
**الحالة:** مُثبت محليًا (Mock). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. السبب الجذري

في `_apply_management` كان يُحسب:

```python
_decision_allowed = not _stale and not _missing
```

ويُسجَّل فقط (`Decision Allowed: False`)، ثم **يستمر التنفيذ بلا شرط** إلى:

```python
decision = self.board.evaluate(context)
self._last_decision = decision
self._execute_decision(decision, symbol)
```

أي أن نتيجة الرفض كانت **Log فقط** ولم تمنع قرار Profit Brain. بما أن `TradeManagementBoard`
هو السلطة الوحيدة للقرار، فكان يمكن تنفيذ إجراءات (SL/BE/Trail/Partial/Full Close) مبنية على
Snapshot غير صالح.

**عيب مصاحب مكتشف:** عند غياب `continuation_probability` كان الكود يسقط بـ`TypeError`
(`None >= 0.62`) — أي أن مسار البيانات الناقصة كان يرمي استثناءً بدل الحماية الآمنة.

---

## 2. الملفات المعدّلة

| الملف | الدالة | التعديل |
|---|---|---|
| `ggpbot.py` | `LiveTradeManager.__init__` | تهيئة `last_decision_blocked_reason = None` |
| `ggpbot.py` | `LiveTradeManager._apply_management` | **بوابة تنفيذ فعلية:** عند `_decision_allowed=False` (وغياب علم طوارئ مستقل) تُعاد الدالة فورًا **قبل** `board.evaluate`، مع سجل `[DECISION_BLOCKED]`. ويُسمح بمرور طوارئ مستقلة عبر `[DECISION_EMERGENCY_BYPASS]` |
| `ggpbot.py` | كتلة "cached values" | تحصين `ContinuationEvaluation` بـ`safe_float` لمنع `TypeError` عند قيم `None` |
| `test_market_data_refresh.py` | — | +9 اختبارات بوابة |

**لم تُلمس:** الاستراتيجية، Scanner، RF Engine، Dashboard، Telegram، Flask، Position Sizing،
Leverage، Native SL Pipeline، TP1/TP2، Recovery Hold، Profit Brain Rules (مُثبت بـ`git diff --name-only`).

---

## 3. سلوك البوابة

- **FRESH / UNCHANGED_BUT_VALID** ⇒ `board.evaluate` يعمل طبيعيًا.
- **STALE / INVALID / INSUFFICIENT / UNKNOWN** أو مؤشر مطلوب مفقود ⇒ **لا يُستدعى `board.evaluate`**
  ولا يُنفَّذ أي قرار إداري جديد.
- **Native SL / TP1 / TP2** المثبَّتة على المنصة **لا تُلمس** أبدًا بهذه البوابة.
- **مراقبة المركز وتحديث Mark Price والمزامنة** تعمل قبل البوابة وتستمر (البوابة بعد تحديث السعر).
- **طوارئ مستقلة** (`STATE["emergency_exit"]` المُعلَّم خارج مسار المؤشرات) تمرّ عبر البوابة.
- **UNKNOWN لا يتحول إلى PASS.**

---

## 4. إثبات أن board.evaluate لا يعمل عند Decision Allowed=False (سجل فعلي)

```text
scenario=STALE   emergency=False -> board.evaluate calls=0 blocked_reason=stale_or_missing_market_data sl=82.0 tp1=84.0 tp2=85.0 native_sl=sl-1
scenario=STALE   emergency=True  -> board.evaluate calls=1 blocked_reason=None sl=82.0 tp1=84.0 tp2=85.0 native_sl=sl-1
scenario=FRESH   emergency=False -> board.evaluate calls=1 blocked_reason=None sl=82.0 tp1=84.0 tp2=85.0 native_sl=sl-1

[DECISION_BLOCKED] SOL/USDT:USDT: market data STALE; no new management decision
(SL/BE/trail/TP/close) executed. Native SL and TP protection on the exchange are unchanged.
```

---

## 5. إثبات أن الحماية الأصلية لا تتغير

`test_gate_block_does_not_change_protection` و`test_gate_block_leaves_sync_and_monitoring_intact`:
عند الحظر تبقى `synthetic_sl=82.0` و`tp1_price=84.0` و`tp2_price=85.0` و`native_sl_order_id=sl-1`
و`native_sl_verified=True` و`open=True` دون تغيير، ويستمر تحديث `mark_price` (83.99).

---

## 6. الاختبارات الجديدة ونتائجها

| الاختبار | النتيجة |
|---|---|
| STALE → board لا يُستدعى | PASS (calls=0) |
| INVALID → board لا يُستدعى | PASS (calls=0، freshness=INVALID) |
| INSUFFICIENT → board لا يُستدعى | PASS (calls=0) |
| Missing ADX/DI/Continuation → board لا يُستدعى | PASS (calls=0) |
| UNKNOWN → لا يُعامل كـPASS | PASS (calls=0) |
| FRESH → board يعمل | PASS (calls=1) |
| UNCHANGED_BUT_VALID → يستمر | PASS (calls=1) |
| طوارئ مستقلة تتجاوز الحظر | PASS (calls=1) |
| الحظر لا يغيّر Native SL/TP1/TP2 | PASS |
| الحظر لا يعطّل المزامنة/مراقبة السعر | PASS (mark_price تحدّث) |

- **`python -m pytest -q` → 281 passed, 0 failed** (مستقرة عبر تشغيلين؛ +9 اختبارات).
- `compileall` → exit 0. `preflight` → PREFLIGHT OK.

---

## 7. مراجعة `MarketDataRefresh` مقابل مسار الإنتاج

- `MarketDataRefresh` في `ggp_market_data.py` **غير مستخدم في مسار الإنتاج** (لا يُستورد في
  `ggpbot.py`؛ الاستيراد هو للثوابت `REQUIRED_CLOSED_CANDLES`/`MANAGEMENT_OHLCV_LIMIT`/`FETCH_MIN_CANDLES`/`timeframe_seconds`).
- مسار الإنتاج في `_apply_management` يطبّق **نفس** قواعد Freshness
  (`FRESH`/`UNCHANGED_BUT_VALID`/`STALE`/`INSUFFICIENT`/`INVALID`) وعدّاد `indicator_snapshot_version`
  داخليًا، دون ازدواج فعلي. لا تناقض: القيم متطابقة دلاليًا.
- **توصية (لم تُنفَّذ الآن تجنبًا لـRefactoring واسع):** لاحقًا يمكن جعل `_apply_management` يستدعي
  `MarketDataRefresh` كمصدر وحيد لتقليل الازدواج. حاليًا أبقيتُ المسار كما هو مع فرض البوابة.

---

## 8. القيود

كل الأدلة من Mock. **لم** يُختبر على BingX Demo. **لا أعلن جاهزية LIVE**؛ الاختبارات المحلية
ليست بديلًا عن اختبار المنصة الفعلي.

---

## 9. ZIP النهائي
- الاسم: `GGP_RELEASE_9_FINAL_HARDENED.zip`
- SHA-256: `4b3ee99a7ae2283a5c7527d1010190fe922e11f08d9c4f5bb686208da265a831`
