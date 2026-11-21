# تقرير دمج استراتيجية EMA9 / EMA15 + VWAP + Pullback

## القرار المعماري

أصبحت استراتيجية EMA9/EMA15 + Session VWAP + Pullback هي بوابة الدخول النهائية المشتركة. لا تمنح إشارات RF أو SNIPER أو Scanner أو أي مسار قديم صلاحية إرسال أمر بمفردها؛ جميعها مصادر اكتشاف/ترشيح فقط، بينما يظل execute_entry مسار الدخول الوحيد.

## شروط السماح بالدخول

1. بيانات OHLCV مكتملة وصحيحة، والشموع المستخدمة مغلقة وحديثة.
2. تقاطع EMA9 مع EMA15 حديث وفي اتجاه الصفقة.
3. استمرار علاقة EMA9/EMA15 في الاتجاه نفسه.
4. توافق EMA9 والسعر المغلق مع Session VWAP.
5. حدوث Pullback لاحق للتقاطع إلى نطاق EMA9/EMA15، دون كسر عميق للنطاق.
6. شمعة رفض اتجاهية مغلقة بعد الـPullback، مع دليل Wick في اتجاه الرفض.
7. إغلاق شمعة التأكيد خارج قمة/قاع الشمعة السابقة في اتجاه الصفقة.
8. رفض السوق المتذبذب إذا تكرر تقاطع EMA9/VWAP أو EMA9/EMA15 ثلاث مرات أو أكثر ضمن آخر 8 شموع مغلقة.
9. عدم مطاردة السعر إذا ابتعد السعر الفعلي عن EMA9 بأكثر من 1.5 ATR.
10. صلاحية الحدث مرتبطة بهوية الشموع: cross timestamp + pullback timestamp + continuation candle timestamp.

أي نقص في البيانات أو غياب أحد الشروط ينتج WAIT/BLOCK، ولا توجد موافقة افتراضية.

## نقاط الدمج

- أضيفت دالة pure validator باسم validate_ema915_vwap_pullback_entry_gate داخل roro_confluence.py.
- استبدلت execute_entry بوابة EMA9/VWAP القديمة ببوابة EMA9/EMA15 + VWAP + Pullback.
- أعيد التحقق من الإشارة داخل open_position باستخدام آخر بيانات متاحة وسعر ticker الفعلي قبل إرسال أمر BingX، ويجب أن تطابق هوية الحدث التي اجتازت بوابة execute_entry.
- أضيفت البوابة إلى scale-in؛ لا يمكن إضافة تعرض جديد دون حدث Pullback مستقل جديد. تحفظ هوية حدث scale-in في TradeJournal بعد نجاح الأمر، وتدخل حالة recovery hold إذا تعذر تثبيت الحدث بعد قبول التعرض.
- احتفظت قيمة dual_ema_vwap_gate كحقل توافق للأنظمة التشخيصية القديمة، لكنها تحمل الآن snapshot البوابة الأساسية الجديدة.
- بقيت ema9_vwap_proximity وEMA50 diagnostics أدوات اكتشاف/سياق فقط وليست سلطة دخول.

## ما لم يتغير

لم يتم تعديل:
- Position sizing أو leverage أو risk caps.
- هندسة SL/TP وإدارة الأهداف الاتجاهية، وfallback الخاص بصفقات REVERSAL/RANGE. لم يتضمن هذا الدمج أي تعديل على حسابات أو مستويات SL/TP في كود الإنتاج، وظل تقسيم TP1/TP2 بنسبة 50/50 كما هو.
- TradeExecutionService أو التحقق من fills أو Native SL.
- TradeManagementBoard أو LiveTradeManager أو Profit Brain.
- منطق TP1/TP2 وتقسيم 50/50.
- Exchange synchronization أو dashboard routes أو scanner scheduling.

## الاختبارات المضافة

أضيف test_ema915_vwap_pullback_gate.py ويغطي:
- نجاح BUY وSELL على بيانات اختبارية مغلقة.
- رفض غياب التقاطع.
- رفض تعارض VWAP.
- رفض غياب Pullback.
- رفض غياب كسر الاستمرارية.
- رفض مطاردة السعر.
- رفض تكرار تقاطعات VWAP في السوق المتذبذب.
- رفض الشموع القديمة.
- تحقق ساكن من فرض البوابة في execute_entry وopen_position وscale-in.

## نتائج CI وحدود الاعتماد

- GitHub Actions: `python -m compileall -q .` — ناجح.
- Runtime preflight — ناجح.
- آخر تشغيل كامل مكتمل على نفس كود الإنتاج: **543 passed, 1 warning** خلال 83.34 ثانية.
- أُعيد تشغيل CI على أحدث commit بعد تعديل test fixture لمنع تكرار هوية الحدث بين اختبارات sizing؛ نتيجة التشغيل الأخير ما زالت قيد الانتظار عند تحديث هذا التقرير.
- لم يتم تنفيذ PAPER أو BingX Demo أو LIVE من بيئة البناء. نجاح CI لا يثبت سلامة fills أو Native SL أو الربحية.

لا تعتبر هذه الدفعة اعتمادًا للتداول الحي. يلزم إكمال إعادة CI، ثم PAPER، ثم Demo مع التحقق من تطابق الحدث، الكمية، fills، Native SL، وسلوك الاستعادة.
