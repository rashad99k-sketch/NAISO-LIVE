# سجل التغييرات — JGB/GGP

## ترقية 2026-11-10 (تحديث ج) — تحصين TP1 وNative SL (Release 9 FINAL HARDENED)
- **هوية TP1 حسب وضع الحساب:** في Hedge يجب وجود `positionSide` ومطابقته (LONG/SHORT)؛ القيمة المفقودة أو
  المتعارضة تُرفض. في One-Way يجب أن يكون غائبًا أو BOTH، وساق Hedge تُرفض. الوضع غير المعروف يُرفض.
- **ربط الأمر بالصفقة:** يتطلب معرّف أمر صرف + تطابق الرمز بعد التطبيع + جانب الإغلاق المعاكس + حالة
  تعبئة مع `filled>0` + كمية = 50% من الأصل، مع ربط بمعرّف مسجّل أو وسم TP/Partial + وقت الأمر ≥ وقت الدخول.
- **نجاة الدليل بعد Restart:** `_collect_tp1_close_order_ids()` يقرأ معرّفات الإغلاق من Journal
  (snapshot + row) لا من `STATE`؛ ودالة الإثبات تستقبل `close_order_ids` و`position_mode` و`trade_entry_ts` صراحة.
- **Native SL State Machine:** PRECHECK (قراءة المركز + الهوية + الكمية + صلاحية سعر الوقف) ثم
  REPLACEMENT (cancelReplace/إلغاء مؤكد) ثم POSTCHECK (قراءة جديدة). `RETAINED` لا تُعتمد إلا بعد قراءة
  جديدة تُثبت أن الوقف القديم نشط ويغطي الكمية، ولا تُستنتج من غموض إلغاء أو حالة محلية أو رد API قديم.
  `LOST` تعني عدم إمكانية إثبات أي وقف ⇒ Hold.
- الاختبارات المحلية: 229 passed (39 في `test_hardening_release9.py`).

## ترقية 2026-11-10 (تحديث ب) — تحصين TP1 وفشل cancelReplace
- لم يعد انخفاض كمية المركز وحده يُسجَّل كـTP1. لا يُرقّى TP1 إلا إذا كان السجل قد حفظ `tp1_hit=True`
  قبل التعطل أو أثبتت المنصة وجود أمر إغلاق جزئي صادر من البوت وتعبّأ فعلًا؛ وإلا يبقى Hold صريح
  (`unverified_position_reduction_requires_tp1_fill_evidence`). هذا يمنع تسمية إغلاق يدوي/خارجي كـTP1.
- `replace_native_stop` أصبح يعيد `MOVED`/`RETAINED`/`LOST`: عند فشل التحريك يُتحقق من بقاء الوقف
  القديم أو يُعاد إنشاؤه عند مستواه الأصلي، ولا يُنشأ وقف مكرر، ولا يدخل Hold إلا عند فقدان الحماية فعلًا.
- مطابقة دقيقة للهوية والكمية: `RETAINED` تُعتمد فقط بعد قراءة BingX المطابقة (رمز/جانب/`positionSide`/سعر/تغطية كمية)؛
  و`place_verified` يرفض الوقف المتباين ويُلغي القديم ثم يُنشئ وقفًا صحيح الحجم؛ ودليل TP1 يطابق
  جانب الإغلاق و`positionSide` والكمية المتوقعة.
- الاختبارات المحلية: 190 passed (38 منها في `test_recovery_lifecycle.py`).

## ترقية 2026-11-10 — إصلاح جذر RECOVERY_HOLD + تحريك Native SL فعليًا
- إصلاح السبب الجذري لـ`RECOVERY_HOLD`: كانت أعلام الدخول/الإغلاق العابرة (`ENTRY_PENDING`،
  `close_reconciliation_required`، `external_close_detected`) وأي انخفاض في الكمية تبقى شروط Hold
  دائمة، فيبقى المركز عالقًا رغم أن BingX يثبت الحالة. الآن:
  - يُعاد اشتقاق كمية التعبئة الأصلية من سجل أمر الدخول على BingX عند فقدان الـcheckpoint
    (`ggp_recovery.reconcile_entry_fill`)، ولا تُخترع من `requested_qty`.
  - يُعتمد انخفاض الكمية المثبت من BingX (إغلاق جزئي قبل الـcheckpoint) كحقيقة، ويُشتق TP1 من
    سياسة 50/50 فلا يُنفّذ TP1 مرتين بعد Restart.
  - الزيادة غير المبرّرة في الكمية (لا يمكن أن تحدث لمركز أحادي Reduce-Only) تبقى Hold صريحًا.
- إضافة `TradeExecutionService.replace_native_stop` لتحريك أمر الوقف الأصلي فعليًا على BingX عبر
  `POST /openApi/swap/v1/trade/cancelReplace` (STOP_ON_FAILURE) مع read-back، وبديل Cancel+Place
  يرفض الإرسال عند غياب تأكيد الإلغاء.
- ربط Breakeven وTrailing Stop وTP1 بتحريك Native SL على المنصة بدل تحديث محلي فقط
  (`move_native_stop_on_exchange`). عند فشل التحريك يبقى المركز في RECOVERY_HOLD بدل ادّعاء حماية غير موجودة.
- تثبيت `entry_execution_state=FILLED` بعد دخول ناجح حتى لا يُعاد اشتقاق Hold زائف بعد Restart.
- إصلاح مساري الاختبار المعطوبين (`test_main_engine_compatibility`، `test_dual_scanner_dashboard`)
  ليكونا متوافقين مع المسار المسطّح أو مجلد tests/، وإضافة ملف `.env.example` المفقود.
- إضافة `test_recovery_lifecycle.py`: 35 سيناريو تكاملي (Restart، Native SL، Hedge/One-Way،
  Partial TP1، Timeout، تعديل SL، حدود التخصيص 20/30 وحد المخاطرة).
- الاختبارات المحلية: 180 passed؛ compilation وpreflight ناجحان.
- لم يجرِ اختبار اتصال فعلي على BingX Demo/LIVE أو اختبار تشغيل 24/7.

## ترقية 2026-11-09 — 20x / 30%-40% / Dynamic Profit Protection
- رفع سياسة الرافعة للدخول الجديد إلى 20x مع set/read-back verification.
- 20% هدف تخصيص هامش للفرص المتوسطة، و30% هدف للفرص القوية المعتمدة فقط.
- إبقاء حد مخاطرة الوقف 1% افتراضيًا؛ حجم الصفقة قد يكون أقل من سقف الهامش إذا فرضت مسافة الوقف ذلك.
- توسيع التحقق من Native SL ليشمل `triggerOrderId`، مع اشتراط حالة أمر نشطة ومطابقة الرمز والاتجاه والجانب والسعر والكمية.
- إضافة EMA20/EMA50 context من الشموع المغلقة، Peak ROE/Drawdown-from-peak، وAdaptive ATR Trail مستلهم من RORO إلى مدير GGP المركزي.
- الإبقاء على TP1 50% وTP2 للمتبقي وعلى بوابة التحقق من التعبئة.
- الاختبارات المحلية: 133 passed؛ compilation ناجح.
- لم يجرِ اختبار اتصال فعلي على BingX Demo/LIVE أو اختبار تشغيل 24/7.


## سياسة الوقف الثابت — تحديث 2026-11-11

وقف BingX الأصلي ثابت بعد الدخول. تم تعطيل مسار تحريك/استبدال Native SL بعد الدخول، بما في ذلك Breakeven وTP1 وTrailing. مستويات BE والتريلينج تُدار داخليًا فقط، والإغلاق الفعلي يمر عبر مسار reduce-only والتحقق الصارم. فشل إنشاء/التحقق من الوقف الأصلي يظل حالة حماية حرجة ولا يتم تجاهلها.
