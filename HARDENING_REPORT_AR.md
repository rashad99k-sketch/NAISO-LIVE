# تقرير التحصين النهائي — GGP Release 9 FINAL HARDENED

**التاريخ:** 2026-11-10
**Commit:** `d29d1daaa384dce8d493b2e94dbec921b7be4a18`
**الفرع:** `fix/recovery-hold-native-sl-release-9`
**الحالة:** تحصين مُختبَر محليًا (Mock Exchange). **ليس تصريح LIVE** — يلزم اختبار BingX Demo.

النطاق: مسارات Recovery وTP1 وNative SL فقط. لم تُمسّ الاستراتيجية ولا قواعد الدخول ولا Scanner ولا
Dashboard ولا Telegram ولا Flask Routes ولا Threading ولا المؤشرات. سياسة رأس المال كما هي:
Medium=20%، Strong=30% من Available Balance، رافعة 20x، TP1/TP2 = 50%/50%، وحد مخاطرة الوقف مستقل لم يُرفع.

---

## 1. السبب الجذري لكل عيب

### العيب 1 — هوية مركز TP1 (CRITICAL)
في `_tp1_fill_verified_on_exchange` كان الشرط:
```python
if order_position_side and expected_position_side and order_position_side != expected_position_side:
    continue
```
يقبل دليل TP1 إذا كان `positionSide` **مفقودًا** (لأن الطرف الأول يصبح falsy فيُتخطى الشرط)، حتى في
Hedge Mode. النتيجة: أمر إغلاق لا تُثبت ساقه قد يُحتسب كـTP1 لمركز مختلف على نفس الرمز.

### العيب 2 — دليل TP1 لا ينجو من Restart (CRITICAL)
`_tp1_fill_verified_on_exchange` كان يقرأ معرّفات الإغلاق من `STATE` العامة فقط:
`STATE["bot_close_order_ids"]`, `STATE["pending_close_order_id"]`, `STATE["last_close_order_id"]`.
وفي مسار الاستعادة تُستدعى قبل نقل `saved_state` (Journal snapshot) إلى `STATE`. لذلك بعد Restart لا ترى
الدالة المعرّفات المحفوظة، فيُرفض دليل TP1 المشروع ويبقى المركز في Hold.

### العيب 3 — ذرية استبدال Native SL (CRITICAL)
مسار الاستبدال كان يعتمد على:
- نتيجة `cancelReplace` مباشرة دون قراءة تأكيد جديدة؛
- `restore_previous_protection()` الذي يستنتج "الاحتفاظ" من `place_verified` الذي كان يعتمد أي وقف نشط؛
- عدم التحقق من أن الوقف القديم أُلغي فعلًا قبل إرسال الجديد.
النتيجة: احتمال إرجاع `RETAINED` بناءً على غموض إلغاء أو رد API قديم، أو إرسال وقف جديد قبل تأكيد الإلغاء
(تعارض/رفض من المنصة)، دون PRE/POST state واضحة.

---

## 2. الملفات والدوال التي تغيّرت

| الملف | الدوال | التغيير |
|---|---|---|
| `ggpbot.py` | `_tp1_fill_verified_on_exchange` | هوية `positionSide` إلزامية حسب الوضع؛ معرّفات/وضع/وقت صريحة |
| `ggpbot.py` | `_collect_tp1_close_order_ids` (جديدة) | قراءة معرّفات الإغلاق من Journal snapshot + row |
| `ggpbot.py` | `_restore_trade_from_journal` | تمرير `close_order_ids`/`position_mode`/`trade_entry_ts` من Journal |
| `bingx_execution.py` | `StopMoveResult` | إضافة حقل `reason` |
| `bingx_execution.py` | `_precheck_native_stop` (جديدة) | PRECHECK: مركز/هوية/كمية/سعر السوق/الوقف القديم |
| `bingx_execution.py` | `_verified_retained` (جديدة) | إثبات الاحتفاظ من قراءة جديدة فقط |
| `bingx_execution.py` | `_count_matching_stops` (جديدة) | كشف تعارض/تكرار الأوامر |
| `bingx_execution.py` | `replace_native_stop` | PRECHECK→REPLACEMENT→POSTCHECK بحالات MOVED/RETAINED/LOST |
| `bingx_execution.py` | `_postcheck` (جديدة) | تأكيد الوقف الجديد من قراءة جديدة |
| `test_hardening_release9.py` (جديد) | — | 31 اختبارًا جديدًا |

---

## 3. Git Diff

الفرق بين آخر إصدار معتمد (`7d98441`) وهذا التحصين (`d29d1da`):
`RELEASE_9_HARDENING_DIFF.patch` (1206 سطرًا، 4 ملفات، +915/−120).
متاح للتنزيل عبر رابط الـZIP أدناه بجوار الملف.

أبرز التغييرات:
- شرط الهوية الجديد في `_tp1_fill_verified_on_exchange`.
- إضافة `_collect_tp1_close_order_ids` واستدعاؤها في `_restore_trade_from_journal`.
- إعادة بناء `replace_native_stop` إلى ثلاث مراحل + دوال مساعدة.

---

## 4. نتائج الاختبارات

| الأمر | النتيجة |
|---|---|
| `python -m pytest -q` | **229 passed, 0 failed** |
| `python -m compileall -q .` | exit 0 |
| `python preflight.py` | `PREFLIGHT OK` |
| `test_hardening_release9.py` | 39 passed |

### تغطية متطلبات الاختبار

**TP1 (12+):** Hedge LONG صحيح؛ Hedge SHORT صحيح؛ رفض Position Side مفقود؛ رفض متعارض؛ One-Way BOTH/غائب؛
رفض ساق Hedge في One-Way؛ رفض الرمز غير المطابق؛ رفض Close Side؛ رفض الكمية؛ رفض أمر غير مرتبط بالصفقة؛
رفض الوضع UNKNOWN؛ رفض أمر موسوم قبل الدخول زمنيًا؛ قبول موسوم بعد الدخول؛ **استعادة TP1 من Journal بعد
Restart دون معرّفات في STATE**؛ عدم تحويل انخفاض الكمية إلى TP1؛ عدم تكرار TP1 عند إعادة الاستعادة.

**Native SL (14):** MOVED مع read-back؛ فشل الإلغاء مع إثبات بقاء الوقف؛ نجاح الإلغاء وفشل الإنشاء؛
Timeout بعد الإلغاء؛ Timeout في cancelReplace؛ إلغاء غامض لا يثبت RETAINED؛ RETAINED بعد قراءة جديدة فقط؛
رفض وقف كميته أقل من المركز؛ رفض سعر غير مطابق؛ رفض Position Side غير مطابق؛ منع Duplicate Stop؛
LOST عند فقدان الوقف؛ عدم فتح مركز جديد أثناء الاستبدال (PRECHECK)؛ رفض سعر وقف في الجانب الخطأ.

---

## 5. ما تم اختباره على Mock

كل ما سبق، بالكامل على Mock Exchange (لا شبكة ولا مفاتيح). يشمل دورة PRECHECK/REPLACEMENT/POSTCHECK
وحالات MOVED/RETAINED/LOST، ومسار استعادة TP1 من Journal بعد Restart، ورفض الهوية/الرمز/الجانب/الكمية.

## 6. ما لم يتم اختباره على BingX Demo

- لا اتصال فعلي بحساب BingX (لا مفاتيح في بيئة البناء).
- السلوك الفعلي لـ`POST /openApi/swap/v1/trade/cancelReplace` على كل نوع عقد.
- الشكل الفعلي لحقول `positionSide`/`stopPrice`/`origQty` في `GET /openApi/swap/v2/trade/openOrders`.
- دقة الكمية/السعر و`tradeMinQuantity`/`tradeMinUSDT` لكل عقد.
- Partial fills الحقيقية أثناء استبدال الوقف، وسلوك Reduce-Only في One-Way على المنصة.
- اختبار Restart حقيقي على مركز حي.

**لا يمكن اعتبار Native SL أو استعادة TP1 ناجحين نهائيًا قبل التحقق على حساب BingX.**

---

## 7. حالة Git

- **Commit Hash:** `d29d1daaa384dce8d493b2e94dbec921b7be4a18`
- **Branch Name:** `fix/recovery-hold-native-sl-release-9`
- **Push:** **لم يتم.** رمز GitHub المتاح هو GitHub App installation token للـ`openhands-ai` بصلاحية
  قراءة فقط (403 `Resource not accessible by integration`). الـcommit محلي فقط.

## 8. ZIP النهائي

- **الاسم:** `GGP_RELEASE_9_FINAL_HARDENED.zip`
- **بُني بـ:** `git archive HEAD` من `d29d1da` (مطابق بايت-بايت عبر `diff -r`).
- **الحجم:** 306,457 بايت | **عدد المدخلات:** 75 | **CRC:** سليم.
- **SHA-256:** `d9d69c9aebb210264b4fd89d636c6b2904216ebbbefeef7df4f17968b7223487`
- لا يحتوي `.env` أو API Keys أو SQLite أو `-wal`/`-shm` أو Cache أو Virtual Environment.

**رابط التنزيل:**
`https://work-1-wdxdupvoiwholvgk.prod-runtime.all-hands.dev/GGP_RELEASE_9_FINAL_HARDENED.zip`
(بديل: `https://work-2-wdxdupvoiwholvgk.prod-runtime.all-hands.dev/GGP_RELEASE_9_FINAL_HARDENED.zip`)
وملف الفروق: `.../RELEASE_9_HARDENING_DIFF.patch`

---

## إعلان صريح

تم تحصين مسارات TP1 وRecovery وNative SL محليًا، وأصبحت هوية TP1 تُثبت حسب وضع الحساب، ودليله ينجو من
Restart، واستبدال الوقف يعمل كآلة حالات واضحة لا تُثبت حالة بلا قراءة جديدة.

**هذا لا يعني أن النظام آمن للتداول LIVE.** نجاح Native SL وTP1 لم يُعلن نهائيًا قبل اختبار BingX Demo فعلي.

---

## 9. جولة التصحيح الأخيرة (تحديث د) — `5c20a3f`

ثلاثة تشديدات صغيرة على نفس المسارات:

1. **إلزامية الرمز والهوية في دليل TP1:** أصبح الرمز إلزاميًا (أمر بلا رمز يُرفض)، وفي Hedge يُشترط وجود
   ساق متوقعة معروفة (LONG/SHORT)؛ فإذا كان `positionSide` المتوقع مجهولًا يُرفض الدليل بدل قبوله.
2. **إلزامية `positionSide` في Hedge PRECHECK:** سجل المركز الحيّ يجب أن يحمل `positionSide` صريحًا؛ غيابه
   يُجهض الاستبدال (`precheck_hedge_position_side_missing`) بدل تخطّي الشرط.
3. **التحقق من نتيجة الإلغاء قبل إرسال بديل:** دالة جديدة `_is_stop_id_active` تؤكد المعرّف القديم بقراءة
   جديدة. الإلغاء الغامض لا يُطلق إنشاءً جديدًا: إن كان الوقف القديم نشطًا يُحتفظ به؛ وإن كان الغموض قائمًا
   دون إثبات وقف مغطٍّ تصبح النتيجة `LOST` (Hold). وفي مسار `cancelReplace` يُعامل خطأ النقل كغموض
   (تحقق ثم لا إنشاء على حالة غير مثبتة)، والرفض الصريح يُبقي الوقف القديم المثبت.

**الاختبارات بعد الجولة:** 225 passed (+4 جديدة)، compileall وpreflight ناجحان.

---

## 10. جولة إصلاح إضافية (تحديث هـ) — `ca3fba2`

**العيب:** في `place_new()` داخل `replace_native_stop()` كان إلغاء وقف قديم غير مطابق ينتقل مباشرة إلى
`create_order()` بعد أن يعود `cancel_order()` دون استثناء، بلا قراءة جديدة تُثبت اختفاء المعرّف القديم.
نجاح استدعاء الإلغاء لا يساوي إثبات الإلغاء على المنصة (تأخر انعكاس الحالة/عدم تزامن)، ما قد يُنشئ وقفًا
بديلًا والوقف القديم ما زال نشطًا (تكرار/تجاوز الكمية).

**الإصلاح:**
- `_read_open_stop_rows()` جديدة تُعيد `(rows, ok)`؛ `ok=False` تعني فشل مسار القراءة ⇒ **UNKNOWN**،
  ولا تُفسَّر أبدًا كـ"لا توجد أوامر".
- `_stop_id_presence()` جديدة تُعيد `ACTIVE` / `GONE` / `UNKNOWN` لمعرّف واحد بعينه؛ `GONE` لا تُعاد إلا بعد
  قراءة ناجحة لا يتضمن فيها المعرّف، وفشل القراءة ⇒ `UNKNOWN`.
- `place_new()` الآن: (أ) ترفض الإنشاء إذا كانت حالة الأوامر غير مقروءة (UNKNOWN)؛ (ب) بعد إلغاء وقف قديم
  تشترط `_stop_id_presence == "GONE"` قبل إنشاء البديل، وإلا تتوقف دون إنشاء.

**الاختبارات:** 229 passed (+4): إلغاء يعود بنجاح لكن الوقف ما زال مُدرجًا ⇒ `RETAINED` دون تكرار؛ قراءة
الأوامر فاشلة ⇒ `LOST` دون إنشاء؛ وقف قديم ما زال مُدرجًا في `place_new` ⇒ `LOST` دون إنشاء؛ وقف قديم أُزيل
فعلًا ⇒ `MOVED`. اختبار `place_new` الجديد يفشل على الكود قبل الإصلاح (يؤكد أنه يلتقط الثغرة).


