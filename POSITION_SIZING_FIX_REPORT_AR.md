# تقرير تدقيق وإصلاح Position Sizing — GGP (Release 9)

**التاريخ:** 2026-11-10
**الهدف:** كل صفقة جديدة تستهدف 20% من Available Balance الحقيقي كهامش، برافعة 20x، BUY وSELL.
**الحالة:** إصلاح مُثبت بالأرقام محليًا (Mock/PAPER). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. السبب الجذري (مُثبت من الكود والأرقام)

ليست المشكلة في نسبة 20% — فقد كانت مضبوطة أصلًا في `ggp_position_sizing.py`
(`allocation_for_classification("TREND") → 0.20`). المشكلة في **عامل ثانٍ يقيّد الحجم**:

```python
final_notional = min(target_notional, risk_limited_notional)
```

حيث:
```python
risk_budget             = balance * (MAX_TRADE_RISK_PCT / 100)     # 1.5% افتراضيًا
effective_stop_distance = |entry - stop| + entry * 0.0005
risk_limited_notional   = risk_budget * entry / effective_stop_distance
```

عندما يكون الوقف الهيكلي واسعًا (ATR × 1.6 ≈ 2.4% لـDOGE)، فإن:

```text
risk_limited_notional = (balance × 1.5%) × 20  ≈ 0.30 × balance
```

وهذا **أصغر بكثير** من الهامش المستهدف 20% × 20 = 4.0 × balance. لذلك يقيّد
`risk_limited_notional` الحجم، فينهار الهامش الفعلي.

### الإثبات الرقمي (من `calculate_position_size` الفعلية)

| Balance | Entry | Stop % | Risk % | target_margin | target_notional | final_notional | margin الفعلي | Binding |
|---|---|---|---|---|---|---|---|---|
| 100 | 0.06 | 0.375% | 1.5 | 20.00 | 400.00 | 352.94 | 17.65 | RISK_GATE |
| 100 | 0.06 | 2.4% | 1.5 | 20.00 | 400.00 | 61.22 | 3.06 | RISK_GATE |
| **33.35** | **0.06** | **2.4%** | **1.5** | **6.67** | **133.40** | **20.42** | **1.02** | **RISK_GATE** |
| 33.35 | 0.06 | 1.6% | 1.5 | 6.67 | 133.40 | 30.32 | 1.52 | RISK_GATE |
| 33.35 | 0.06 | 3.0% | 1.0 | 6.67 | 133.40 | 10.93 | 0.55 | RISK_GATE |

**الخلاصة:** الحالة المُبلَّغة (رصيد 33.35، هامش ≈ 1.04 USDT) = `risk_limited_notional`
لبوقف بعرض ≈ 2.4% مع سقف مخاطرة 1.5%. ليس خطأ في الرصيد ولا في الرافعة ولا قسمة مزدوجة —
بل **بوابة المخاطرة المستقلة تقيّد الحجم عن الهامش المستهدف**.

### المشكلة الهندسية الحقيقية
كان هذا التقييد يحدث **بصمت**: لا حقل في النتيجة يبيّن أن المخاطرة هي القيد، ولا سجل
`[POSITION_SIZING_AUDIT]` يوضح الفرق بين الهامش المستهدف والفعلي. كذلك لم يكن الرصيد يُقرأ
مع إثبات المصدر والطابع الزمني (قد يُستخدم Cache قديم).

---

## 2. الملفات والدوال المعدّلة

| الملف | الدالة/الحقل | التعديل |
|---|---|---|
| `ggp_position_sizing.py` | ثوابت الوحدة | `TRADE_ALLOCATION_PCT = 0.20`، `STRONG_TRADE_ALLOCATION_PCT = 0.30`، `POLICY_LEVERAGE = 20` |
| `ggp_position_sizing.py` | `allocation_for_classification` | تستخدم الثوابت المسمّاة |
| `ggp_position_sizing.py` | `PositionSize` | حقول جديدة: `binding_constraint`، `margin_achieved_ratio` |
| `ggp_position_sizing.py` | `calculate_position_size` | يحسب القيد المُقيِّد ونسبة تحقيق الهامش صراحةً |
| `ggpbot.py` | `get_available_balance_snapshot` (جديدة) | يعيد (balance, source, timestamp) مع إعادة الجلب عند انتهاء الصلاحية |
| `ggpbot.py` | `execute_entry` | قراءة الرصيد عبر الـsnapshot؛ سجل `[POSITION_SIZING_AUDIT]`؛ سجل `[SIZING_RISK_GATE]` |

**لم تُلمس**: Scanner، Strategy، Entry Logic، Institutional Models، Dashboard، Telegram،
Flask Routes، Threading، Exchange Sync، Profit Brain، Native SL، TP1/TP2.

---

## 3. الحساب قبل الإصلاح وبعده (DOGE، رصيد 33.35)

| | قبل | بعد |
|---|---|---|
| الهامش المستهدف (20%) | 6.67 USDT (محسوب لكن مُقيَّد) | 6.67 USDT (مسجّل) |
| القيمة الاسمية المستهدفة | 133.40 USDT | 133.40 USDT |
| القيد المُقيِّد | غير مُعلن (صامت) | **`RISK_GATE` مُعلن + سجل WARN** |
| الهامش الفعلي عند وقف 2.4% | ≈1.02 USDT | ≈1.02 USDT + **تفسير صريح** |
| الرصيد | قد يكون Cache قديمًا | مصدر + طابع زمني، fail-closed إن تعذّر |

> عند التعارض بين هامش 20% وحد المخاطرة، **يُحترم حد المخاطرة** ولا يُرفع. لذلك يبقى الهامش
> الفعلي أقل من 20% عند الوقوف الواسعة، وهذا **مقصود ومسجَّل**، لا خطأ.

---

## 4. مثال DOGE BUY بسعر 0.06

```text
Available Balance = 100 USDT
Target Margin     = 100 × 0.20 = 20 USDT
Leverage          = 20x
Target Notional   = 20 × 20  = 400 USDT
DOGE Price        = 0.06
DOGE Quantity     = 400 / 0.06 = 6666.666666 DOGE
```

**سجل التنفيذ الفعلي (تشغيل حقيقي لـ`execute_entry` في PAPER):**

```text
[POSITION_SIZING_AUDIT]
  Symbol: DOGE/USDT:USDT
  Direction: BUY
  BingX Available Balance: 100.0000 USDT
  Balance Source: PAPER
  Balance Timestamp: 1794346847.402
  Allocation Percentage: 20%
  Target Margin: 20.0000 USDT
  Leverage: 20x
  Target Notional: 400.0000 USDT
  Entry Price: 0.0600000000
  Raw Quantity: 6666.6666666667
  Final Exchange Quantity: 6666.6666666600
  Expected Actual Notional: 400.0000 USDT
  Expected Margin: 20.0000 USDT
  Sizing Difference: margin_target_minus_actual=+0.0000 USDT (achieved=100.0%)
  Binding Constraint: ALLOCATION
  Risk Check: estimated_stop_loss=0.4000 USDT (0.400%) cap=1.50% => PASS
  Final Status: PASS
```

`STATE.qty` بعد التنفيذ = **6666.66666666 DOGE** — الكمية وصلت إلى أمر التنفيذ فعلًا.

---

## 5. مثال DOGE SELL بسعر 0.06

نفس المعادلة تمامًا (الاتجاه لا يغيّر الحجم):

```text
Balance 100   → Target Margin 20   → Notional 400   → Qty 6666.666666 DOGE
Balance 33.35 → Target Margin 6.67 → Notional 133.40 → Qty 2223.333333 DOGE
```

`test_execute_entry_buy_and_sell_same_quantity` يؤكد تساوي `STATE.qty` بين BUY وSELL.

---

## 6. إثبات أن 20% وصلت إلى أمر التنفيذ

1. `[POSITION_SIZING_AUDIT]` يطبع الرصيد والنسبة والهامش المستهدف والقيمة الاسمية والكمية النهائية.
2. `Sizing Difference (achieved=100.0%)` يثبت أن الهامش الفعلي = المستهدف عند عدم تقييد المخاطرة.
3. `test_execute_entry_audit_proves_20pct_reached_the_order` يتحقق من هذه السطور نصًّا.
4. عند تقييد المخاطرة: `Binding Constraint: RISK_GATE` + سجل `[SIZING_RISK_GATE]` WARN.

---

## 7. مقارنة Target Quantity مع Actual Exchange Quantity

| Balance | Stop | Target Qty | Actual Qty | القيد |
|---|---|---|---|---|
| 100 | 0.05% | 6666.666666 | 6666.666666 | ALLOCATION |
| 33.35 | 0.05% | 2223.333333 | 2223.333333 | ALLOCATION |
| 33.35 | 2.4% | 2223.333333 | 340.306122 | RISK_GATE (مُعلن) |

> في PAPER لا يوجد `contractSize`/دقة حقيقية، فـ`Final Exchange Quantity` تطابق الخام.
> على LIVE تُطبَّق `amount_to_precision` وقواعد العقد، ويُسجَّل الفرق.

---

## 8. نتائج اختبارات Regression

- **`python -m pytest -q` → 249 passed, 0 failed** (كان 229؛ +20 في `test_position_sizing_policy.py`).
- `compileall` → exit 0. `preflight` → PREFLIGHT OK.

التغطية (المطلوب 16): Margin=20 من 100؛ Notional=400 عند 20x؛ DOGE=6666.666666؛
Balance 33.35 → Margin 6.67؛ DOGE=2223.333333؛ BUY/SELL نفس المعادلة؛ لا مضاعفة/قسمة مزدوجة للرافعة؛
لا نسبة منافسة؛ التخفيض ليس صامتًا؛ رصيد صفري/غير مقروء يُرفض؛ Cache قديم لا يُستخدم؛
Contract Size/Precision؛ Risk Gate يمنع التجاوز؛ القيد يُبلَّغ دائمًا؛ تناسق الكمية. +3 اختبارات تكامل.

---

## 9. تأكيد عدم تغيير الوحدات المحمية

`git diff --name-only` → `ggp_position_sizing.py` و`ggpbot.py` فقط + ملف اختبار جديد.
لم يُلمس أي ملف Scanner/Strategy/Dashboard/Telegram/Flask/Profit Brain/Native SL/TP.

---

## 10. القيود وما لم يُختبر

- كل الاختبارات محلية (PAPER/Mock). **لم** يُختبر التنفيذ على BingX Demo.
- لم تُقرأ `contractSize` و`amount_to_precision` و`tradeMinQuantity/tradeMinUSDT` الحقيقية.
- لم تُقارن الأرقام الفعلية التي تعرضها المنصة مع حسابات البوت.

**لا أدّعي نجاح التحقق LIVE.** يجب اختبار BingX Demo أولًا ومقارنة الأرقام.

---

## 11. ZIP النهائي

- الاسم: `GGP_RELEASE_9_FINAL_HARDENED.zip`
- SHA-256: `e262fd75664e28050d012e942f27e5a4a9d08cbbe63bd2aafe967888163c03b3`
