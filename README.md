# SmartPOS 1.1.0-rc1

> **Windows packaging (1.1.0-rc2):** run `.\build_windows.ps1` on Windows or trigger the *Windows release build* GitHub Action. See `docs/BUILD_WINDOWS.md`. The EXE had not been built on Windows when this archive was created.


> **Release-candidate status — not certified for production.** Business logic, security and migrations are covered
> by 88 automated service-level tests. The Qt GUI, printing, barcode scanning and the Windows EXE have **not** been
> run since the changes (no PySide6/Windows available when this was produced). Read `docs/BUILD_WINDOWS.md` for the
> mandatory smoke-test list and `AUDIT_REMEDIATION.md` for the open items.

Documentation: `docs/` (ARCHITECTURE, SECURITY, ACCOUNTING_RULES, INVENTORY_COSTING, MIGRATIONS, DATABASE_SCHEMA,
BACKUP_AND_RESTORE, TESTING, BUILD_WINDOWS, INSTALLATION) · `CHANGELOG.md` · `AUDIT_REMEDIATION.md`.

---

# SmartPOS — Repaired Source Build

نسخة مصدرية مُصلحة من SmartPOS (Python + PySide6 + SQLite)، بواجهة عربية RTL.

## ما الذي تم إصلاحه

- لا يتم تنفيذ أي ملف `smartpos_restore_pending.db` عند بدء التشغيل. يتم عزله إن وُجد، ولا توجد قاعدة بيانات تشغيلية داخل حزمة المصدر أو ملف البناء.
- النسخ والاستعادة يتطلبان مديرًا نشطًا؛ الاستعادة تتطلب إعادة إدخال كلمة المرور، وتتحقق من SQLite قبل الاستبدال، وتحفظ نسخة رجوع قبل التفعيل.
- تخزين قاعدة البيانات والإعدادات والنسخ والسجلات أصبح في مجلدات المستخدم لا بجانب EXE أو داخل مجلد التثبيت.
- checkout يدعم نقدي، آجل، ونقدي + آجل. الآجل يسجل مديونية عميل، والتحصيل يسجل حركة خزنة، والمرتجع يمكن رده نقدًا أو كرصيد عميل.
- تسوية الوردية تخصم المرتجعات النقدية المسجلة فقط. المرتجعات القديمة مجهولة طريقة الرد تبقى `unknown`، ويجب على المدير تصنيفها من سجل فعلي؛ لا يتم افتراض أنها نقدية.
- التصنيف النقدي لمرتجع تاريخي يعيد حساب الرصيد المتوقع والفرق في الوردية القديمة ويكتب حدثًا في سجل التدقيق.
- التقارير تعتمد business date من 04:00 إلى 04:00، وترتيب المنتجات يطرح المرتجعات المسجلة خلال فترة التقرير.
- تكلفة شراء المنتج تستخدم moving weighted average في الاستلام الجديد، مع سجل حركات مخزون append-only.
- هناك قواعد قاعدة بيانات تمنع مجموع فاتورة غير صحيح، وكمية مرتجع تتجاوز المباع، وتعديل/حذف سجلات مالية أو حركات مخزون حساسة.
- الجلسات الحساسة تتحقق من الدور وحالة المستخدم في قاعدة البيانات الفعلية، لا من بيانات الواجهة القديمة فقط.
- أضيفت اختبارات خدمة وقاعدة بيانات وملفات متطلبات وتعليمات بناء.

## تنبيه بخصوص البيانات التاريخية

حزمة `SmartPOS_Data_Recovery_Pack_2026-10-31.zip` المرفقة منفصلة عن البرنامج، وتحتوي قواعد البيانات الأصلية كما وصلت من مجلدات `data`, `build_backup`, و`dist`. بعضها غير متطابق في عدد الفواتير والمنتجات. **لا تستورد أكثر من لقطة واحدة، ولا تختَر الأحدث بالاسم وحده.** راجع `RECOVERY_MANIFEST.md` داخل حزمة الاستعادة وحدد مع صاحب المتجر أي ملف يمثل آخر بيانات صحيحة.

يجب استيراد القاعدة المختارة قبل أول تشغيل من خلال:

```powershell
python import_legacy_database.py "C:\\path\\to\\recovery\\SmartPOS_Test_Build\\data\\smartpos.db"
```

الأداة ترفض استبدال قاعدة تشغيل تحتوي بيانات. إذا بدأت البرنامج وأنشأت حساب المدير أو أدخلت فواتير، لن تستبدل البيانات بهذه الأداة؛ استخدم الاستعادة الموثقة داخل البرنامج بعد نسخة أمان.

## أول تشغيل على Windows

1. ثبّت Python 3.10 أو أحدث (يفضل 3.11/3.12) وحدد Add Python to PATH.
2. افتح PowerShell داخل مجلد المشروع:

```powershell
py -3 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

3. (اختياري) استورد لقطة قاعدة البيانات التي اخترتها كما هو موضح أعلاه **قبل تشغيل التطبيق لأول مرة**.
4. شغّل:

```powershell
python app.py
```

أول تشغيل لقاعدة جديدة يعرض إعداد حساب المدير. لا توجد كلمات مرور افتراضية مضمّنة.

## مسارات البيانات

- Windows database/backups/logs: `%LOCALAPPDATA%\\SmartPOS\\data`
- Windows configuration: `%APPDATA%\\SmartPOS`
- Linux database/backups/logs: `~/.local/share/smartpos/data` (مع دعم `XDG_DATA_HOME`)
- Linux configuration: `~/.config/smartpos` (مع دعم `XDG_CONFIG_HOME`)
- يمكن تحديد مجلد بيانات بديل عبر `SMARTPOS_DATA_DIR` قبل تشغيل التطبيق.

## الاختبارات

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

الاختبارات لا تحتاج PySide6 GUI، وتغطي عمليات الدفع والآجل والتحصيل والمرتجعات والتسوية والتكلفة المرجحة والتقارير والترحيل والنسخ والاستعادة والصلاحيات.

## بناء Windows EXE

نفّذ من PowerShell على Windows بعد تثبيت متطلبات التطوير:

```powershell
.\\build_windows.ps1
```

الناتج `dist\\SmartPOS\\SmartPOS.exe`. ملف `SmartPOS.spec` لا يضمّن قاعدة بيانات أو ملفات استعادة داخل التوزيعة. **لا تنسخ قاعدة البيانات إلى مجلد EXE**؛ سيستخدم البرنامج مجلد بيانات المستخدم. لم يتم توليد EXE داخل بيئة الفحص الحالية؛ يجب تنفيذ البناء واختبار نافذة البرنامج والطباعة على Windows قبل النشر.

## سياسات محاسبية وحدود لا يمكن استنتاجها من قاعدة قديمة

- المرتجعات التاريخية لا تحتوي tender موثوقًا؛ لا يمكن معرفة إن كان المبلغ قد خرج نقدًا أو تحول إلى رصيد عميل من الملف وحده. تصنيفها يتطلب مراجعة إيصالات أو سجل فعلي.
- وحدات البيع القديمة التي لم تحفظ تكلفة الوحدة تظل بتكلفة تاريخية مجهولة، ويظهر التحذير في التقرير بدل اختلاق COGS.
- قيمة `purchase_price` الموجودة عند تفعيل سجل الحركة تُستخدم كنقطة تكلفة افتتاحية لاحتساب WAC للاستلامات اللاحقة؛ لا تمثل إعادة بناء دقيقة لتكلفة تاريخية لم تحفظ أصلًا.
- موديل المجموعات يربط صفوف الـSKU حسب اسم المنتج لتجميع المقاسات/الألوان؛ إنشاء كتالوج Parent Product متكامل، إدارة التلف/الهالك المتقدمة، والجرد الدوري الموجّه تحتاج مرحلة منفصلة.

## Architecture

- `app.py`: نقطة تشغيل وتجميع واجهات PySide6 الحالية.
- `services/`: منطق المبيعات، المرتجعات، العملاء، المخزون، التقارير، المحاسبة، النسخ والأمان.
- `database.py`: اتصال SQLite والترحيلات والـtriggers.
- `tests/`: اختبارات الخدمات والتكامل مع قاعدة SQLite مؤقتة.

تم فصل قواعد العمل الأساسية إلى خدمات قابلة للاختبار، لكن `app.py` ما زال ملف واجهة كبيرًا؛ النقل الكامل لكل screen إلى modules/repositories خطوة صيانة لاحقة، ولا ندّعي أن هذا التفكيك اكتمل.
