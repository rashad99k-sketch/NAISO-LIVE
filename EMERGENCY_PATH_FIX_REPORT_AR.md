# تقرير إصلاح مسار الطوارئ المستقل — GGP

**التاريخ:** 2026-11-10
**النطاق:** مسار `DECISION_EMERGENCY_BYPASS` فقط في `LiveTradeManager._apply_management`.
**الحالة:** مُثبت محليًا (Mock). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. السبب الجذري

في الإصلاح السابق أضفت بوابة صلاحية البيانات، لكن مسار الطوارئ كان:

```python
if _emergency and not _decision_allowed:
    log_execution("[DECISION_EMERGENCY_BYPASS] ...")
# ثم يستمر إلى:
decision = self.board.evaluate(context)   # <-- بمؤشرات قديمة (STALE)
```

أي أن علم الطوارئ **كان يمرّ عبر `board.evaluate`** باستخدام Snapshot غير صالحة. هذا يناقض المبدأ:
الطوارئ الحقيقية يجب ألا تعتمد على المؤشرات إطلاقًا، ولا أن تمرّ عبر سلطة القرار المؤشّري.

**النتيجة المطلوبة:** مسار طوارئ **مستقل تمامًا** لا يستدعي `board.evaluate` ولا يقرأ أي مؤشر.

---

## 2. الملفات المعدّلة

| الملف | الدالة | التعديل |
|---|---|---|
| `ggpbot.py` | `LiveTradeManager._emergency_exit_if_valid` (جديدة) | مسار طوارئ مستقل: يتحقق من مصدر الإشارة وحداثتها وحالة المركز الفعلية، ثم ينفذ إغلاقًا مُتحقَّقًا عبر `safe_close_position` (لا `board.evaluate`) |
| `ggpbot.py` | `LiveTradeManager._apply_management` | عند وجود علم طوارئ ⇒ يُستدعى المعالج المستقل ويُعاد فورًا (لا يمر أبدًا إلى `board.evaluate`)؛ وإلا تُطبَّق بوابة البيانات كما هي |
| `ggpbot.py` | `STATE` الافتراضي | `emergency_exit=False`, `emergency_reason=None`, `emergency_source=None`, `emergency_ts=0.0` |
| `test_market_data_refresh.py` | — | استبدال اختبار الـbypass القديم بـ6 اختبارات مسار مستقل + اختبار `force_close_local` |

**لم تُلمس:** استراتيجية الدخول، Profit Brain Rules، TP1/TP2، Position Sizing، Leverage،
Native SL Pipeline، Scanner، Dashboard، Telegram، Flask، Recovery Hold.

---

## 3. سلوك مسار الطوارئ الجديد

يُقبل الطوارئ ويُنفَّذ **فقط** إذا تحققت كل الشروط:
1. `STATE["emergency_exit"] == True`.
2. `emergency_source` موجود (غير فارغ) — لا طوارئ بلا مصدر.
3. `emergency_ts` حديث (عمره ≤ `EMERGENCY_SIGNAL_MAX_AGE_SEC`، افتراضيًا 30 ثانية) — لا طوارئ قديمة.
4. `fetch_position_tri_state(symbol) == "OPEN"` — المركز مؤكد مفتوح على المنصة.

عندها فقط: `safe_close_position(..., close_reason=reason)` ويجب أن يُثبت `close_verified`، ثم
`finalize_trade_with_reality`، ويُصفَّر العلم. أي شرط غير متحقق ⇒ `[EMERGENCY_EXIT_REJECTED]` **بلا أي إغلاق**.

**المسار لا يستدعي `board.evaluate` ولا يقرأ أي مؤشر** (ADX/DI/Continuation/Regime).

---

## 4. دليل من مسار التنفيذ الفعلي (تشغيل حقيقي لـ`_apply_management`)

```text
STALE, no emergency                board.evaluate=0 tags=['[DECISION_BLOCKED]']
STALE + valid emergency            board.evaluate=0 tags=['[EMERGENCY_EXIT_EXECUTED]']
STALE + emergency no source        board.evaluate=0 tags=['[EMERGENCY_EXIT_REJECTED]']
STALE + emergency stale signal     board.evaluate=0 tags=['[EMERGENCY_EXIT_REJECTED]']
STALE + emergency but FLAT         board.evaluate=0 tags=['[EMERGENCY_EXIT_REJECTED]']
FRESH, no emergency                board.evaluate=1 tags=[]
```

**الخلاصة:** `board.evaluate` = 0 في **كل** حالات الطوارئ (سواء نُفِّذ أو رُفض)، و= 1 فقط عند
FRESH بدون طوارئ. لا تمرّ Snapshot غير صالحة إلى Profit Brain في أي حال.

---

## 5. التحقق من صحة إشارة الطوارئ (البند 3 و4)

- **المصدر:** إشارة بلا `emergency_source` ⇒ رفض.
- **التوقيت:** إشارة عمرها 999 ثانية ⇒ رفض (غير حديثة).
- **حالة المركز الفعلية:** المنصة FLAT ⇒ رفض (لا إغلاق أعمى).
- **إشارة قديمة/غير مؤكدة لا تتجاوز الحماية.**

---

## 6. الحماية الأصلية والمزامنة

- Native SL / TP1 / TP2 على المنصة **لا تُلمس** بمسار الطوارئ (يستخدم نفس `safe_close_position` المُتحقَّق فقط).
- `_force_close` (حدث `force_close_local` المستقل عند اختفاء المركز) **يستمر بالعمل** حتى مع بيانات STALE
  (`test_force_close_event_is_independent_of_market_data`).
- مراقبة السعر والمزامنة تعمل قبل البوابة وتستمر.

---

## 7. الاختبارات ونتائجها

| الاختبار | النتيجة |
|---|---|
| STALE + no emergency → board=0 ولا قرار | PASS |
| STALE + emergency → board=0 (مسار مستقل فقط) | PASS |
| FRESH + normal → board=1 | PASS |
| طوارئ بلا مصدر → رفض | PASS |
| طوارئ بإشارة قديمة → رفض | PASS |
| طوارئ والمنصة FLAT → رفض | PASS |
| طوارئ صحيحة + OPEN → إغلاق مُتحقَّق بلا board | PASS |
| `force_close_local` مستقل عن البيانات | PASS |

- **`python -m pytest -q` → 287 passed, 0 failed** (+6 عن الجولة السابقة).
- `compileall` → exit 0. `preflight` → PREFLIGHT OK.

---

## 8. القيود
كل الأدلة من Mock. **لم** يُختبر على BingX Demo. **لا أعلن جاهزية LIVE.**

---

## 9. ZIP النهائي
- الاسم: `GGP_RELEASE_9_FINAL_HARDENED.zip`
- SHA-256: مذكور في رسالة التسليم وفي صفحة التنزيل (index.html)، لأن تضمين بصمة الملف داخل الملف نفسه يغيّر البصمة.
