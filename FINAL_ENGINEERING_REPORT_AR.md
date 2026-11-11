# تقرير هندسي نهائي — GGP Institutional Trading Bot
## تدقيق وإصلاح RECOVERY_HOLD، إدارة الصفقة، Native SL، وسياسة رأس المال

**التاريخ:** 2026-11-10
**النطاق:** تدقيق كامل + إصلاح جذري + اختبارات + تسليم ZIP
**الحالة:** إصلاح مُختبَر محليًا (Mock Exchange). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

---

## 1. السبب الجذري لخطأ RECOVERY_HOLD (تقرير عربي)

الرسالة:

```text
[RECOVERY_HOLD] Exposure is tracked but full management state is incomplete; no new entries or fabricated TP/SL actions
```

تنشأ من `_recovery_hold()` في `ggpbot.py`، ويُعاد بثّها من حلقة التشغيل الرئيسية. ليست رسالة
تجميلية: هي بوابة أمان تُبقي التعرض الحقيقي مرئيًا وتمنع الدخول الجديد وإجراءات TP/SL غير المُثبتة.

**السبب الجذري الفعلي:** كانت عدة أعلام وحالات عابرة (transient) تُعامل كأنها شروط Hold **دائمة**،
فلا يستطيع النظام الخروج من الحماية حتى عندما يثبت BingX الحالة. الأسباب المحددة:

1. **فقدان كمية التعبئة الأصلية عند التعطل** بين كتابة `ENTRY_PENDING` الدائمة وبين كتابة
   checkpoint ما بعد التنفيذ. عندها يكون `actual_filled_qty` صفرًا، فيُضاف
   `entry_execution_not_fully_reconciled:ENTRY_PENDING` أو `actual_filled_quantity` كحقل مفقود،
   ويبقى المركز في Hold رغم أن التعرض الحقيقي معروف من BingX.
2. **اعتماد علم الإغلاق العابر `close_reconciliation_required` / `external_close_detected`** كسبب Hold
   دائم حتى لو كانت الكمية الحية على BingX سليمة ومطابقة.
3. **أي اختلاف في الكمية** كان يُترجم إلى `exchange_remaining_quantity_mismatch_requires_fill_reconciliation`
   وHold دائم. لذلك أي إغلاق جزئي (TP1) حدث قبل الـcheckpoint يُبقي المركز عالقًا إلى الأبد.
4. **أعلام الدخول (`ENTRY_PENDING` / `EXECUTION_UNCERTAIN`) لم تكن تُثبَّت إلى `FILLED`** في الحالة
   المحفوظة بعد دخول ناجح، فيُعاد اشتقاق Hold زائف بعد كل Restart.

### تحصين إضافي (مراجعة هندسية لاحقة)

بعد المراجعة الأولى ظهر تحفظان مشروعان عالجناهما:

- **ربط TP1 بالدليل الفعلي:** لم يعد انخفاض الكمية وحده يُسجَّل كـTP1. قد يكون الانخفاض إغلاقًا
  يدويًا أو خارجيًا. الآن لا يُرقّى TP1 إلا إذا (أ) كان السجل قد حفظ `tp1_hit=True` قبل التعطل، أو
  (ب) أثبتت المنصة وجود أمر إغلاق جزئي صادر من البوت وتعبّأ فعلًا (`_tp1_fill_verified_on_exchange`
  يطابق معرّف أمر الإغلاق المسجَّل أو أمرًا موسومًا TP/Partial بنفس الكمية والاتجاه). غير ذلك ⇒
  `unverified_position_reduction_requires_tp1_fill_evidence` وHold صريح.
- **عدم فقدان الوقف عند فشل `cancelReplace`:** عقد `replace_native_stop` الجديد هو ألا يبقى المركز
  بلا وقف متحقَّق. النتيجة صارت `MOVED` / `RETAINED` / `LOST`: عند فشل التحريك يُتحقق من أن الوقف
  القديم ما زال نشطًا أو يُعاد إنشاؤه عند مستواه الأصلي، وإن تعذّر إثبات أي وقف يصبح `LOST` فيدخل
  المركز Hold.
- **مطابقة الهوية والكمية بدقة:**
  - `RETAINED` لا تُعتمد إلا بعد قراءة BingX (`_active_stop_id_at` عبر `is_matching_native_stop_order`)
    التي تفرض تطابق الرمز وجانب الإغلاق و`positionSide` وحالة نشطة وسعر التفعيل **وتغطية الكمية**.
  - `place_verified` لا يعتمد أي وقف موجود إلا إذا طابق السعر المطلوب وغطّى الكمية المطلوبة؛ الوقف
    القديم المتباين يُلغى أولًا ثم يُنشأ وقف صحيح الحجم (يمنع التباس الكمية ومضاعفة الوقف).
  - دليل TP1 يطابق الآن معرّف أمر الإغلاق أو وسم TP/Partial **مع** جانب الإغلاق و`positionSide`
    والكمية المتوقعة (50% من الأصل) — فلا يُقبل إغلاق الساق المعاكسة أو كمية مغايرة كـTP1.

هذه الأسباب متسقة مع ما ورد في `FIX_REPORT_AR.md` (نقص في Journal، وعدم وجود إعادة محاولة، وأعلام
دخول غير مُثبتة)، لكن الإصلاح هنا **جذري** لا تجميلي: يُعاد اشتقاق الحالة المفقودة من **مصادر موثوقة
فقط** (سجل أمر الدخول على BingX والكمية الحية)، ولا تُضاف قيم افتراضية ولا بيانات تخمينية.

### قاعدة السلوك بعد الإصلاح

- مركز مفتوح مؤكد من BingX + كمية/اتجاه/دخول قابل للتحقق ⇒ استعادة أكبر قدر موثوق من حالة الإدارة.
- نقص إداري غير حرج ⇒ Reconciliation آمن من مصدر موثوق (لا تخمين).
- نقص حرج لا يمكن استعادته (غياب SL هيكلي محفوظ، أهداف TP مفقودة، وضع حساب غير معروف، **زيادة**
  كمية غير مبرّرة) ⇒ استمرار Hold + منع الدخول + عدم إرسال أي TP/SL مبني على تخمين.
- لا يُعتبر Native SL موجودًا إلا بعد التحقق منه على BingX.
- لا تُعتبر الصفقة مغلقة لمجرد قبول أمر الإغلاق؛ يُتحقق من كمية المركز المتبقية فعلًا.
- لا تكرار للإغلاق أو TP1/TP2 بعد Restart (Idempotent).

---

## 2. الملفات المعدّلة وسبب كل تعديل

| الملف | التعديل | السبب |
|---|---|---|
| `ggp_recovery.py` | إضافة `reconcile_entry_fill()` + إعادة اشتقاق TP1/partial عند انخفاض الكمية المثبت؛ تفريق انخفاض/زيادة الكمية؛ `entry_execution_state` لم يعد سبب Hold عند توفر الدليل | إصلاح جذر Hold: استعادة كمية التعبئة من دليل المنصة، واعتماد الإغلاق الجزئي المثبت، ومنع Hold الدائم |
| `bingx_execution.py` | `replace_native_stop()` + `_read_back_native_stop()`؛ إعادة استخدام الـread-back في `place_and_verify_native_stop` | تحريك Native SL فعليًا على BingX (cancelReplace/STOP_ON_FAILURE) بدل تحديث محلي فقط، مع رفض الإرسال عند غياب التأكيد |
| `ggpbot.py` | `_reconcile_journal_entry_fill()` ويُستدعى قبل قرار Hold؛ `move_native_stop_on_exchange()`؛ ربط Breakeven/Trailing/TP1 بتحريك الوقف الأصلي؛ تثبيت `entry_execution_state=FILLED`؛ معالجة انخفاض الكمية | إصلاح السبب الجذري + تنفيذ أوامر الحماية فعليًا على المنصة |
| `test_ggp_recovery.py` | تحديث اختبارين يعكسان السلوك القديم (Hold الدائم) إلى السلوك الآمن الجديد + اختبار زيادة الكمية | مطابقة الاختبارات للسلوك الصحيح |
| `test_main_engine_compatibility.py` | تحديد جذر المشروع ديناميكيًا | كان يفشل عند التشغيل من الجذر (مسار `parents[1]` خاطئ) |
| `test_dual_scanner_dashboard.py` | تحديد جذر المشروع ديناميكيًا | نفس السبب |
| `test_recovery_lifecycle.py` (جديد) | 38 سيناريو تكاملي | تغطية متطلبات الاختبار العشرين |
| `.env.example` (جديد) | قالب بيئة آمن بدون أسرار | كان مطلوبًا من `preflight.py` ومفقودًا |
| `RUN_TESTS.bat` | `pytest "%ROOT%"` بدل `"%ROOT%tests"` | مجلد `tests/` غير موجود في هذا الإصدار |
| `CHANGELOG_AR.md`, `README_AR.md` | توثيق الإصلاح | التوثيق |

**لم يُعد كتابة المشروع من الصفر، ولم تُحذف وظائف، ولم تُغيّر الاستراتيجية أو قواعد الدخول.**

---

## 3. دورة إدارة الصفقة بعد الإصلاح

```text
1) تأكيد تنفيذ أمر الدخول  (TradeExecutionService.submit_market_entry -> FILLED)
2) تسجيل المركز الحقيقي في Journal  (actual_filled_qty = filled)
3) إرسال Native STOP_MARKET إلى BingX  (place_and_verify_native_stop)
4) التحقق من أمر الوقف (الرمز + الاتجاه + Position Side + الحالة + السعر + الكمية)
5) إدارة TP1 عند بلوغ الهدف
6) إغلاق 50% من الكمية الأصلية المنفذة  (target_remaining_qty = initial*0.5)
7) التحقق من التعبئة الفعلية للكمية الجزئية  (fetch_position_tri_state -> delta)
8) تحريك الوقف إلى Breakeven+  ==> أيضًا تحريك Native SL على BingX (cancelReplace)
9) إدارة Trailing Stop  ==> تحريك Native SL على BingX (ratchet، بخطوة دنيا)
10) تنفيذ TP2 وإغلاق الكمية المتبقية
11) تحقق نهائي: المركز Flat على BingX
12) تحديث Realized PnL والإحصائيات والـJournal بعد إثبات التنفيذ فقط
```

**سياسة TP1/TP2:** 50% / 50% من الكمية **الأصلية المنفذة** حصرًا، مع مراعاة دقة الكمية والحد الأدنى.
لا يُسجَّل TP1 مكتملًا قبل التحقق من التعبئة الفعلية.

**مصدر قرار موحّد:** `TradeManagementBoard` / `LiveTradeManager` فقط. لم يُضَف Profit Manager ثانٍ.

### اكتشاف مهم: Breakeven/Trailing كانت محلية فقط

كانت فروع `MOVE_SL` / `BREAKEVEN` / `LOCK_PROFITS` / `TRAIL` تحدّث `STATE["synthetic_sl"]` /
`STATE["trail_stop"]` فقط وتُسجّل الحدث بحالة `LOCAL_STATE_UPDATED` مع `native_sl_unchanged=True`.
هذا **لا يحمي الصفقة فعليًا على BingX أثناء توقف العملية** (الإغلاق المحلي يحتاج حلقة تشغيل حيّة).

**الإصلاح:** عند كل تحسين ذي دلالة يُنفَّذ `move_native_stop_on_exchange()` الذي يحرّك أمر
`STOP_MARKET` الأصلي على المنصة عبر `cancelReplace` (STOP_ON_FAILURE) ثم يتحقق من ظهوره في الأوامر
المفتوحة. عند النجاح يُسجَّل الحدث `EXCHANGE_VERIFIED`؛ وعند الفشل يدخل المركز في `RECOVERY_HOLD`
بدل ادّعاء حماية غير موجودة.

---

## 4. التحقق من Native SL على BingX

لا يُعتبر الوقف موجودًا إلا بعد مطابقة **كل** الحقول من رد الأوامر المفتوحة على المنصة:

- النوع `STOP_MARKET` (أو `TRIGGER_MARKET` عند تطابق كل الحقول الأخرى) — يُقرأ من النوع الأصلي
  داخل `order.info.type` أيضًا، لأن CCXT يطبّع النوع الموحّد إلى `market`.
- `symbol` بعد التطبيع (`BTC/USDT:USDT` ⟷ `BTC-USDT`).
- جانب الإغلاق الصحيح (`SELL` لمركز LONG و`BUY` لمركز SHORT).
- `positionSide` الصحيح: `LONG`/`SHORT` في Hedge، و`BOTH` في One-Way.
- حالة أمر نشطة (`NEW`/`OPEN`/`PENDING`/`PARTIALLY_FILLED`). غياب الحالة = رفض.
- `stopPrice` مطابق لسعر الوقف المحفوظ ضمن تسامح.
- الكمية المغطاة ≥ الكمية المطلوبة ضمن تسامح.

عند الفشل تُطبع ملخصات `[NATIVE_SL_DIAG]` منقّحة (بدون بيانات اعتماد) ويبقى Hold.

---

## 5. نتائج الاختبارات

| الأمر | النتيجة |
|---|---|
| `python -m pytest -q` | **190 passed, 0 failed** |
| `python -m compileall -q .` | نجاح |
| `python preflight.py` | `PREFLIGHT OK` |
| `test_recovery_lifecycle.py` | 38 passed |
| `test_ggp_recovery.py` | 13 passed |
| `test_bingx_execution.py` | 30 passed |
| `test_position_sizing.py` | 18 passed |

(الباقي توزّع على باقي ملفات الاختبار الموجودة.)

### السيناريوهات العشرون المطلوبة — التغطية

| # | السيناريو | الاختبار |
|---|---|---|
| 1 | Restart + مركز LIVE بحالة كاملة | `test_restart_live_position_with_complete_state_restores` |
| 2 | Restart + بيانات إدارة ناقصة | `test_restart_live_position_with_incomplete_state_holds` |
| 3 | غياب Native SL | `test_missing_native_sl_keeps_recovery_hold` |
| 4 | فشل قراءة أوامر الحماية | `test_protection_read_failure_is_not_verified` |
| 5 | SL صحيح مع اختلاف تنسيق الرمز | `test_native_sl_verifies_across_symbol_format_difference` |
| 6 | Hedge LONG وHedge SHORT | `test_hedge_long_and_short_identity_resolution`, `test_restart_hedge_short_position_restores` |
| 7 | One-Way LONG وSHORT | `test_one_way_long_and_short_use_both` |
| 8 | Partial TP1 Fill | `test_partial_tp1_fill_reconciles_without_rerunning` |
| 9 | فشل TP1 ثم إعادة المحاولة | `test_tp1_close_failure_then_retry_is_idempotent` |
| 10 | TP2 بعد TP1 | `test_tp2_runs_only_after_tp1_is_verified` |
| 11 | تكرار Runtime Event | `test_runtime_recovery_is_idempotent_on_repeat_call` |
| 12 | Timeout بعد أمر إغلاق | `test_close_timeout_blocks_and_does_not_resubmit` |
| 13 | إغلاق مقبول والمركز مفتوح | `test_accepted_close_that_leaves_position_open_is_not_verified` |
| 14 | فشل تعديل Native SL | `test_native_sl_move_failure_enters_recovery_hold` (+نجاح: `..._success_updates_state_and_journal`) |
| 15 | استعادة Journal بعد Restart | `test_journal_survives_restart_and_reloads_state` |
| 16 | منع دخول جديد أثناء Hold | `test_no_new_entry_during_recovery_hold`, `test_no_new_entry_when_position_status_unknown` |
| 17 | Medium = 20% | `test_medium_allocation_is_twenty_percent_of_available_balance` |
| 18 | Strong = 30% | `test_strong_allocation_is_thirty_percent_only_with_authorization` |
| 19 | Available Balance + Contract Size + Precision | `test_contract_size_and_precision_are_respected` |
| 20 | عدم تجاوز Risk Cap | `test_risk_cap_is_authoritative_and_never_raised_for_margin` |

---

## 6. تقرير منفصل: Mock مقابل BingX Demo

### ما تم اختباره على Mock Exchange (محليًا، 100%)

- دورة الاستعادة بعد Restart: كاملة/ناقصة/متعددة.
- التحقق من Native SL: غياب، فشل قراءة، اختلاف تنسيق الرمز، Hedge/One-Way، LONG/SHORT.
- Partial TP1، فشل TP1، TP2 بعد TP1، تكرار الأحداث.
- Timeout، قبول إغلاق بدون تقليل كمية، رفض إعادة الإرسال الأعمى.
- تعديل/إلغاء/استبدال Native SL (نجاح وفشل).
- Reconciliation لكمية التعبئة المفقودة من سجل أمر الدخول.
- سياسة التخصيص 20%/30% والرافعة 20x وحد المخاطرة.
- منع الدخول أثناء Hold.

### ما **لم** يتم اختباره على BingX Demo (يجب تنفيذه على حسابك)

- لا اتصال فعلي بحساب BingX VST/Demo في بيئة البناء (لا مفاتيح).
- ظهور أمر `STOP_MARKET` فعليًا في `GET /openApi/swap/v2/trade/openOrders` بعد الإنشاء.
- سلوك `POST /openApi/swap/v1/trade/cancelReplace` فعليًا (STOP_ON_FAILURE) لكل نوع عقد.
- `positionSide` الفعلي في Hedge/One-Way لكل عقد، ودقة الكمية/السعر و`tradeMinQuantity`/`tradeMinUSDT`.
- تعبئة أمر الإغلاق الجزئي على المنصة الحقيقية وسلوك `Reduce Only` في One-Way.
- اختبار Restart حقيقي على مركز حي.

**لا يمكن اعتبار Native SL ناجحًا نهائيًا قبل تأكيده على حساب BingX.**

---

## 7. مصادر BingX / CCXT المعتمدة

- وثائق BingX Swap الرسمية (docs-v3، فرع `main`): `POST /openApi/swap/v2/trade/order` (بما فيها
  `STOP_MARKET`/`TRIGGER_MARKET` و`closePosition`)، `DELETE /openApi/swap/v2/trade/order`
  (إلغاء أمر)، `POST /openApi/swap/v1/trade/cancelReplace` (Cancel + Replace،
  `cancelReplaceMode=STOP_ON_FAILURE`)، `GET /openApi/swap/v2/trade/openOrders` (فلتر `type`)،
  `GET /openApi/swap/v2/user/positions`، `GET /openApi/swap/v1/positionSide/dual` (وضع الحساب).
  ملاحظة موثّقة: في Hedge Mode **لا يُرسل `reduceOnly`**، و`positionSide=BOTH` للـOne-Way.
- CCXT **4.5.92** (الإصدار المثبّت في `requirements.txt`): تأكيد `fetch_open_orders` ->
  `swapV2PrivateGetTradeOpenOrders`، وتطبيع `STOP_MARKET` إلى النوع الموحّد `market` مع بقاء النوع
  الأصلي في `info.type`، وتوفّر `swapV1PrivatePostTradeCancelReplace`.

لم يُخترع أي Endpoint أو Parameter. لم يُنقل كود من GitHub دون فهم واختبار.

---

## 8. مشاكل متبقية / قيود

1. **لم يُختبر على BingX Demo/LIVE** — لا مفاتيح في بيئة البناء. هذا القيد جوهري.
2. **استمرارية تحريك Native SL:** خطوة دنيا (`entry*0.0005`) لتقليل عدد `cancelReplace`؛ لا يوجد
   Trailing أصلي مستمر على المنصة (مقصود، لتفادي مخالفة قواعد المنصة أثناء التنفيذ).
3. **حالة سباق محتملة** بين إغلاق TP1 وتحريك الوقف: يُدار بـ`_CLOSE_ORDER_LOCK` و`_TRADE_LOCK`،
   لكن يحتاج تأكيدًا على المنصة الحقيقية.
4. **تعدد المراكز:** النظام يدعم مركزًا واحدًا (حسب التصميم). وجود أكثر من مركز => Hold ومراجعة يدوية.
5. **الاستثناءات في `_restore_trade_from_journal`** تُغلق fail-closed إلى Hold، وهو مقصود.
6. **لا ضمان ربح** ولا ضمان تشغيل 24/7 من نجاح الاختبارات.

---

## 9. تعليمات التشغيل والتحديث (دون حذف SQLite أو سجل الصفقات)

1. **خُذ نسخة احتياطية** من `data/ggp_trade_journal.sqlite3` (والملفات `-wal`/`-shm` إن وُجدت).
2. فك ZIP في مجلد جديد (لا تستبدل المجلد العامل في مكانه أثناء تشغيل البوت).
3. `pip install -r requirements.txt` (ccxt==4.5.92).
4. انسخ `.env.example` إلى `.env` وأدخل مفاتيحك بنفسك. **لا تُشارك `.env`.**
5. **لا تحذف ولا تُعِد إنشاء** `data/ggp_trade_journal.sqlite3`؛ النظام يُرحّل الأعمدة تلقائيًا
   (`ALTER TABLE ... ADD COLUMN`) دون فقدان صفوف أو أحداث. حافظ على قيمة `TRADE_JOURNAL_PATH`.
6. شغّل `python -m pytest -q` ثم `python preflight.py` قبل التشغيل.
7. ابدأ بـPAPER، ثم BingX Demo/VST، وراقب `recovery_reason` و`[NATIVE_SL_MOVE]` قبل أي LIVE.
8. **لا تمسح `RECOVERY_HOLD` يدويًا.** إذا ظهر، افحص `recovery_reason` المحدد والمركز على BingX.

---

## 10. تأكيد عدم تضمين الأسرار

- لا يوجد ملف `.env` حقيقي في المشروع (تم التحقق: `ls .env` ⇒ غير موجود).
- `.env.example` قالب فارغ فقط، بلا أي مفتاح.
- لا مفاتيح API في أي ملف مصدر أو اختبار أو تقرير.
- الاختبارات تستخدم Mock Exchange حصريًا، ولا تتصل بأي حساب.
- ZIP النهائي يستثني `.env` وأي `*.sqlite3-wal`/`*.sqlite3-shm` ومجلدات الكاش.

---

## إعلان صريح

تم إصلاح جذر RECOVERY_HOLD محليًا، وأصبحت حالة الصفقة قابلة للاستعادة والمصالحة دون تخمين،
ويعمل مدير الأرباح والوقف والتحقق من الإغلاق بصورة متسقة، وتعمل سياسة TP1/TP2 بنسبة 50/50،
وتخصيص الهامش 20%/30% من Available Balance عند رافعة 20x، ولم تتغير استراتيجية الدخول.

**هذا لا يعني أن النظام جاهز للتداول الحقيقي ولا أنه يحقق أرباحًا.** نجاح Native SL لم يُعلن
نهائيًا قبل التحقق منه على حساب BingX فعلي.


## سياسة الوقف الثابت — تحديث 2026-11-11

وقف BingX الأصلي ثابت بعد الدخول. تم تعطيل مسار تحريك/استبدال Native SL بعد الدخول، بما في ذلك Breakeven وTP1 وTrailing. مستويات BE والتريلينج تُدار داخليًا فقط، والإغلاق الفعلي يمر عبر مسار reduce-only والتحقق الصارم. فشل إنشاء/التحقق من الوقف الأصلي يظل حالة حماية حرجة ولا يتم تجاهلها.
