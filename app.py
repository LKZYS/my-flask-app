import csv
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "change-this-secret-key"  # مهم تغيّرها لاحقًا

IT_STAFF_CODE = "IT-2026"  # الرمز السري لإنشاء حساب فريق الدعم الفني — غيّره لرمز خاص فيك

STATS_RESET_AFTER = timedelta(days=30)  # نافذة إنجاز الموظف الفني (بلاغات حلّها) — متحركة يوم بيوم، مو رتست ثابت أول الشهر
TRAINING_LINK_VALID_DAYS = 14  # صلاحية رابط المنشأة (قبول/رفض أو تقييم) بالأيام

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.csv")
TICKETS_FILE = os.path.join(BASE_DIR, "tickets.csv")
TRAINING_FILE = os.path.join(BASE_DIR, "training_requests.csv")
NOTIF_DISMISS_FILE = os.path.join(BASE_DIR, "notification_dismissals.csv")  # تنبيهات الفنيين اللي تم تجاهلها (لكل موظف على حدة)
COOP_SETTINGS_FILE = os.path.join(BASE_DIR, "coop_settings.json")  # بيانات منسّق التدريب التعاوني اللي تظهر للمنشآت
DATE_COLUMNS = ["id", "email", "title", "kind", "course", "date", "time", "note", "done", "created_at"]
DATES_FILE = os.path.join(BASE_DIR, "academic_dates.csv")  # المواعيد الدراسية المهمة اللي يضيفها الطالب بنفسه (اختبارات، تسليمات...)
SUPPORT_SETTINGS_FILE = os.path.join(BASE_DIR, "support_settings.json")  # بيانات التواصل مع الدعم الفني اللي يعبّيها الموظفون وتظهر للطلاب

TRAINING_DOCS_DIR = os.path.join(BASE_DIR, "static", "coop_docs")
ALLOWED_DOC_EXT = {"pdf", "doc", "docx", "jpg", "jpeg", "png"}
MAX_DOC_SIZE = 8 * 1024 * 1024  # 8 ميجابايت لكل ملف
MAX_DOC_FILES = 5  # أقصى عدد مستندات لكل طلب تدريب

STATUS_AWAITING = "بانتظار إصدار الخطاب"        # الطالب قدّم الطلب والكلية ما أصدرت الخطاب بعد
STATUS_COMPANY_PENDING = "قيد المراجعة عند المنشأة"  # الخطاب صدر والرابط عند المنشأة
STATUS_COLLEGE_REJECTED = "مرفوض من الكلية"      # الكلية رفضت طلب الطالب قبل إصدار الخطاب
CLOSED_TRAINING_STATUSES = ("تم القبول", "مرفوض", STATUS_COLLEGE_REJECTED)  # حالات مقفولة (قرار نهائي)
TRAINING_COLUMNS = 27  # 25 عمود أساسي + last_edited_by + last_edited_at (سجل آخر تعديل من الموظف)
EDITABLE_TRAINING_STATUSES = (STATUS_COMPANY_PENDING, "تم القبول", "مرفوض")  # الحالات اللي يقدر الموظف يعدّل بياناتها (الخطاب صدر)
TRAINING_HIDE_AFTER = timedelta(hours=24)  # مدة بقاء الطلب المقفول ظاهرًا قبل ما يختفي وينتقل للأرشيف (يبقى محفوظًا بالملف)
MAX_OPEN_STUDENT_REQUESTS = 3  # أقصى عدد طلبات "بانتظار الإصدار" لنفس الطالب بنفس الوقت

COMPLETION_PENDING = "قيد المراجعة"    # الطالب رفع شهادة إتمام وبانتظار مراجعة الكلية لها
COMPLETION_APPROVED = "معتمدة"        # الكلية اعتمدت إتمام التدريب
COMPLETION_REJECTED = "مرفوضة"        # الكلية رفضت الشهادة المرفوعة، والطالب يقدر يعيد الرفع

AVATAR_DIR = os.path.join(BASE_DIR, "static", "avatars")
ALLOWED_AVATAR_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
MAX_AVATAR_SIZE = 3 * 1024 * 1024  # 3 ميجابايت


def avatar_key(email):
    """يولّد اسم ملف ثابت وآمن للصورة الشخصية بناءً على البريد الإلكتروني."""
    return hashlib.md5(email.strip().lower().encode("utf-8")).hexdigest()


def get_avatar_filename(email):
    """يرجع اسم ملف الصورة الشخصية الحالي لهذا البريد لو موجود، وإلا None."""
    if not email or not os.path.isdir(AVATAR_DIR):
        return None
    key = avatar_key(email)
    for ext in ALLOWED_AVATAR_EXT:
        candidate = f"{key}.{ext}"
        if os.path.exists(os.path.join(AVATAR_DIR, candidate)):
            return candidate
    return None


def delete_avatar(email):
    """يحذف الصورة الشخصية الحالية لهذا البريد لو موجودة."""
    filename = get_avatar_filename(email)
    if filename:
        try:
            os.remove(os.path.join(AVATAR_DIR, filename))
        except OSError:
            pass


def fix_duplicate_ticket_ids():
    """يصلح أي أرقام بلاغات مكرّرة في الملف (من أثر طريقة الترقيم القديمة).
    البلاغ الأقدم يحتفظ برقمه، والمكرّر بعده ياخذ رقمًا جديدًا فوق أكبر رقم موجود."""
    if not os.path.exists(TICKETS_FILE):
        return 0

    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        return 0

    seen = set()
    max_id = 1000
    for row in rows[1:]:
        if not row:
            continue
        try:
            max_id = max(max_id, int(row[0]))
        except (ValueError, IndexError):
            continue

    fixed = 0
    for row in rows[1:]:  # تخطي صف العناوين
        if not row:
            continue
        ticket_id = row[0]
        if ticket_id in seen:
            max_id += 1
            row[0] = str(max_id)
            fixed += 1
        seen.add(row[0])

    if fixed:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return fixed


def _upgrade_training_header():
    """يضيف عنوانَي last_edited_by / last_edited_at لصف العناوين في ملفات قديمة (لا يمس بقية الصفوف)."""
    try:
        with open(TRAINING_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except OSError:
        return
    if rows and len(rows[0]) < TRAINING_COLUMNS and rows[0][:1] == ["id"]:
        rows[0] = rows[0] + ["last_edited_by", "last_edited_at"][: TRAINING_COLUMNS - len(rows[0])]
        with open(TRAINING_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)


def init_file():
    os.makedirs(AVATAR_DIR, exist_ok=True)
    os.makedirs(TRAINING_DOCS_DIR, exist_ok=True)

    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["full_name", "birthdate", "email", "password", "role", "training_number", "last_updated"])  # صف العناوين

    if not os.path.exists(TICKETS_FILE):
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "email", "title", "description", "status", "created_at", "edited", "resolved_at", "handled_by"])  # صف العناوين

    if not os.path.exists(TRAINING_FILE):
        with open(TRAINING_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "id", "student_email", "student_name", "training_number",
                "company_name", "company_email", "position_title",
                "status", "token", "token_expires_at", "created_at",
                "supervisor_name", "start_date", "decided_at", "issued_by",
                "documents", "student_notes", "college_reject_reason",
                "end_date",
                "completion_doc", "completion_status", "completion_submitted_at",
                "completion_reviewed_at", "completion_reject_reason", "final_certificate_doc",
                "last_edited_by", "last_edited_at",
            ])  # صف العناوين
    else:
        _upgrade_training_header()  # ملفات قديمة: نضيف عناوين أعمدة سجل التعديل

    if not os.path.exists(NOTIF_DISMISS_FILE):
        with open(NOTIF_DISMISS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["staff_email", "key", "dismissed_at"])  # صف العناوين

    if not os.path.exists(DATES_FILE):
        with open(DATES_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(DATE_COLUMNS)  # صف العناوين

    fix_duplicate_ticket_ids()  # ينظّف أي تكرار قديم بالأرقام عند تشغيل التطبيق


init_file()  # يشتغل عند استيراد الملف، عشان يضمن التهيئة حتى لو شغّلت التطبيق عبر gunicorn


def email_exists(email):
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # تخطي صف العناوين
        for row in reader:
            if row and row[2] == email:
                return True
    return False


def training_number_exists(training_number):
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # تخطي صف العناوين
        for row in reader:
            if len(row) >= 6 and row[5] == training_number:
                return True
    return False


def save_user(full_name, birthdate, email, password, role="student", training_number=""):
    with open(USERS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([full_name, birthdate, email, password, role, training_number, ""])


def check_login(email, password, required_role=None):
    """يتحقق من بيانات الدخول. لو حددت required_role، يرفض أي حساب من نوع مختلف."""
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 5:
                continue  # صف قديم بدون عمود role
            if row[2] == email and row[3] == password:
                if required_role and row[4] != required_role:
                    return None
                return {"full_name": row[0], "role": row[4]}
    return None


def reset_password(email, new_password):
    """يحدّث كلمة المرور لحساب معيّن عن طريق إعادة كتابة الملف كاملًا."""
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[2] == email:
            row[3] = new_password
            updated = True

    if updated:
        with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


def update_user_profile(email, full_name, birthdate):
    """يحدّث الاسم الكامل وتاريخ الميلاد وتاريخ آخر تحديث لحساب معيّن (طالب أو فني).
    البريد الإلكتروني لا يتغيّر لأنه المفتاح المستخدم لربط الحساب ببلاغاته."""
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[2] == email:
            while len(row) < 7:
                row.append("")  # صف قديم بدون عمود last_updated
            row[0] = full_name
            row[1] = birthdate
            row[6] = datetime.now().strftime("%Y-%m-%d %H:%M")
            updated = True

    if updated:
        with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


@app.context_processor
def inject_avatar():
    """يوفّر current_avatar_url تلقائيًا لكل القوالب — رابط صورة المستخدم الحالي إن وُجدت."""
    email = session.get("email")
    filename = get_avatar_filename(email) if email else None
    avatar_url = url_for("static", filename=f"avatars/{filename}") if filename else None
    return {"current_avatar_url": avatar_url}


def get_coop_contact():
    """بيانات منسّق التدريب التعاوني اللي يعبّيها فريق الدعم من صفحة الإعدادات.
    تُرجع {name, email, phone} — وتكون فاضية لو ما انحفظ شي بعد."""
    empty = {"name": "", "email": "", "phone": ""}
    try:
        with open(COOP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {k: str(data.get(k) or "").strip() for k in empty}


def save_coop_contact(name, email, phone):
    tmp_path = COOP_SETTINGS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "email": email, "phone": phone}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, COOP_SETTINGS_FILE)  # كتابة ذرّية: ما يتلف الملف لو انقطع الحفظ


@app.context_processor
def inject_coop_contact():
    """يوفّر coop_contact() لكل القوالب (تُقرأ البيانات فقط إذا استُدعيت داخل القالب)."""
    return {"coop_contact": get_coop_contact}


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[0-9\s\-()]{7,20}$")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


@app.route("/coop/settings", methods=["GET", "POST"])
def coop_settings():
    """صفحة يعبّي فيها فريق الدعم بيانات منسّق التدريب التعاوني (اسم + بريد و/أو جوال)،
    وتظهر هذي البيانات للمنشآت في صفحة الرابط (coop_verify)."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    contact = get_coop_contact()
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip().translate(_ARABIC_DIGITS)
        contact = {"name": name, "email": email, "phone": phone}
        if not name:
            error = "اكتب اسم المنسّق"
        elif len(name) > 80:
            error = "الاسم طويل، الحد الأقصى 80 حرفًا"
        elif not email and not phone:
            error = "اكتب بريدًا إلكترونيًا أو رقم جوال على الأقل ليتمكن المنشآت من التواصل"
        elif email and (len(email) > 120 or not _EMAIL_RE.match(email)):
            error = "صيغة البريد الإلكتروني غير صحيحة"
        elif phone and not _PHONE_RE.match(phone):
            error = "رقم الجوال غير صحيح (أرقام فقط، ويمكن أن يبدأ بـ +)"
        else:
            save_coop_contact(name, email, phone)
            flash("تم حفظ بيانات المنسّق، وستظهر للمنشآت في صفحة الرابط.", "success")
            return redirect(url_for("coop_settings"))
    return render_template("coop_settings.html", contact=contact, error=error)


SUPPORT_FIELDS = ("phone", "whatsapp", "email", "location", "hours", "note")
SUPPORT_DEFAULT_HOURS = "الأحد – الخميس، من الساعة 8 صباحًا إلى 4 مساءً"


def get_support_contact():
    """بيانات التواصل مع الدعم الفني اللي يعبّيها فريق الدعم من صفحة الإعدادات.
    تُرجع dict فيه (phone, whatsapp, email, location, hours, note) — وتكون فاضية لو ما انحفظ شي بعد."""
    empty = {k: "" for k in SUPPORT_FIELDS}
    try:
        with open(SUPPORT_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {**empty, "hours": SUPPORT_DEFAULT_HOURS}  # أول مرة: نعبّي الدوام المعتاد كقيمة مبدئية
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {k: str(data.get(k) or "").strip() for k in empty}


def save_support_contact(contact):
    tmp_path = SUPPORT_SETTINGS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({k: contact.get(k, "") for k in SUPPORT_FIELDS}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, SUPPORT_SETTINGS_FILE)  # كتابة ذرّية: ما يتلف الملف لو انقطع الحفظ


def whatsapp_link(number):
    """يحوّل رقم الواتساب لرابط wa.me (يفترض رقم سعودي لو بدأ بـ 0 أو 5)."""
    digits = re.sub(r"\D", "", number or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "966" + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = "966" + digits
    return f"https://wa.me/{digits}"


def phone_href(number):
    """رابط tel: نظيف (يحافظ على + في البداية ويشيل المسافات والشرطات)."""
    number = (number or "").strip()
    if not number:
        return ""
    plus = "+" if number.startswith("+") else ""
    return "tel:" + plus + re.sub(r"\D", "", number)


@app.route("/support/settings", methods=["GET", "POST"])
def support_settings():
    """صفحة يعبّي فيها فريق الدعم بيانات التواصل (جوال / واتساب / بريد / موقع المكتب / أوقات الدوام)،
    وتظهر للطلاب في صفحة الدعم الفني (/support) اللي يفتحها زر \"الدعم الفني\" من لوحة الطالب."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    contact = get_support_contact()
    error = None
    if request.method == "POST":
        contact = {
            "phone": request.form.get("phone", "").strip().translate(_ARABIC_DIGITS),
            "whatsapp": request.form.get("whatsapp", "").strip().translate(_ARABIC_DIGITS),
            "email": request.form.get("email", "").strip(),
            "location": request.form.get("location", "").strip(),
            "hours": request.form.get("hours", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        if not (contact["phone"] or contact["whatsapp"] or contact["email"]):
            error = "اكتب رقم جوال أو واتساب أو بريدًا إلكترونيًا على الأقل ليتمكن الطلاب من التواصل"
        elif contact["phone"] and not _PHONE_RE.match(contact["phone"]):
            error = "رقم الجوال غير صحيح (أرقام فقط، ويمكن أن يبدأ بـ +)"
        elif contact["whatsapp"] and not _PHONE_RE.match(contact["whatsapp"]):
            error = "رقم الواتساب غير صحيح (أرقام فقط، ويمكن أن يبدأ بـ +)"
        elif contact["email"] and (len(contact["email"]) > 120 or not _EMAIL_RE.match(contact["email"])):
            error = "صيغة البريد الإلكتروني غير صحيحة"
        elif len(contact["location"]) > 120:
            error = "وصف الموقع طويل، الحد الأقصى 120 حرفًا"
        elif len(contact["hours"]) > 100:
            error = "أوقات الدوام طويلة، الحد الأقصى 100 حرف"
        elif len(contact["note"]) > 200:
            error = "الملاحظة طويلة، الحد الأقصى 200 حرف"
        else:
            save_support_contact(contact)
            flash("تم حفظ بيانات الدعم الفني، وستظهر للطلاب في صفحة الدعم الفني.", "success")
            return redirect(url_for("support_settings"))
    return render_template(
        "support_settings.html",
        contact=contact,
        error=error,
        wa_link=whatsapp_link(contact["whatsapp"]),
        phone_href=phone_href(contact["phone"]),
    )


def require_student():
    """يتأكد إن الجلسة الحالية تخص حساب طالب فعليًا، مو حساب فريق الدعم الفني.
    يرجّع استجابة تحويل لو الوصول غير مسموح، أو None لو الوصول سليم ويقدر الراوت يكمل عمله."""
    if "user" not in session:
        return redirect(url_for("login"))
    if session.get("role") != "student":
        return redirect(url_for("it_dashboard"))
    return None


@app.route("/")
def home():
    if "user" in session:
        if session.get("role") == "it":
            return redirect(url_for("it_dashboard"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        result = check_login(email, password, required_role="student")
        if result:
            session["user"] = result["full_name"]
            session["email"] = email
            session["role"] = "student"
            return redirect(url_for("dashboard"))
        else:
            flash("البريد الإلكتروني أو كلمة المرور غير صحيحة")
            return redirect(url_for("login"))

    return render_template("login.html")


def calculate_age(birth_date):
    today = date.today()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


@app.route("/register", methods=["GET", "POST"])
def register():
    today_str = date.today().isoformat()
    max_birthdate_str = date(date.today().year - 18, date.today().month, date.today().day).isoformat()

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        training_number = request.form.get("training_number", "").strip()
        birthdate = request.form.get("birthdate", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name or not training_number or not birthdate or not email or not password or not confirm_password:
            flash("الرجاء تعبئة جميع الحقول")
            return redirect(url_for("register"))

        if len(training_number) > 9:
            flash("الرقم التدريبي يجب ألا يتجاوز 9 أرقام")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("كلمتا المرور غير متطابقتين")
            return redirect(url_for("register"))

        try:
            birth_date_obj = datetime.strptime(birthdate, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ الميلاد غير صحيح")
            return redirect(url_for("register"))

        if birth_date_obj > date.today():
            flash("تاريخ الميلاد غير صحيح")
            return redirect(url_for("register"))

        if calculate_age(birth_date_obj) < 18:
            flash("يجب أن يكون عمرك 18 سنة أو أكثر للتسجيل")
            return redirect(url_for("register"))

        if email_exists(email):
            flash("هذا البريد الإلكتروني مسجّل بالفعل")
            return redirect(url_for("register"))

        if training_number_exists(training_number):
            flash("هذا الرقم التدريبي مسجّل بالفعل")
            return redirect(url_for("register"))

        save_user(full_name, birthdate, email, password, role="student", training_number=training_number)
        flash("تم إنشاء الحساب بنجاح، سجّل دخولك الآن", "success")
        return redirect(url_for("login"))

    return render_template("register.html", today=today_str, max_birthdate=max_birthdate_str)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not email or not new_password or not confirm_password:
            flash("الرجاء تعبئة جميع الحقول")
            return redirect(url_for("forgot_password"))

        if new_password != confirm_password:
            flash("كلمتا المرور غير متطابقتين")
            return redirect(url_for("forgot_password"))

        if not email_exists(email):
            flash("لا يوجد حساب مسجّل بهذا البريد الإلكتروني")
            return redirect(url_for("forgot_password"))

        reset_password(email, new_password)
        flash("تم تغيير كلمة المرور بنجاح، سجّل دخولك الآن", "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/it-login", methods=["GET", "POST"])
def it_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        result = check_login(email, password, required_role="it")
        if result:
            session["user"] = result["full_name"]
            session["email"] = email
            session["role"] = "it"
            return redirect(url_for("it_dashboard"))
        else:
            flash("البيانات غير صحيحة، أو هذا الحساب ليس حساب فريق الدعم الفني")
            return redirect(url_for("it_login"))

    return render_template("it_login.html")


@app.route("/it-register", methods=["GET", "POST"])
def it_register():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        birthdate = request.form.get("birthdate", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        staff_code = request.form.get("staff_code", "").strip()

        if not full_name or not birthdate or not email or not password or not confirm_password or not staff_code:
            flash("الرجاء تعبئة جميع الحقول")
            return redirect(url_for("it_register"))

        if password != confirm_password:
            flash("كلمتا المرور غير متطابقتين")
            return redirect(url_for("it_register"))

        if staff_code != IT_STAFF_CODE:
            flash("رمز فريق الدعم الفني غير صحيح")
            return redirect(url_for("it_register"))

        if email_exists(email):
            flash("هذا البريد الإلكتروني مسجّل بالفعل")
            return redirect(url_for("it_register"))

        save_user(full_name, birthdate, email, password, role="it")
        flash("تم إنشاء حساب فريق الدعم الفني بنجاح، سجّل دخولك الآن", "success")
        return redirect(url_for("it_login"))

    return render_template("it_register.html")


def get_ticket_stats(handler_email):
    """يرجع (عدد البلاغات اللي حلّها هذا الموظف تحديدًا خلال آخر 30 يوم، عدد البلاغات اللي يشتغل عليها حاليًا).
    كل موظف فني يشوف إنجازه الشخصي فقط — بلاغ حلّه موظف ثاني ما يُحتسب له.
    عدّاد المحلولة يعتمد على نافذة متحركة من 30 يوم (مو الشهر الميلادي)، فيرتست تدريجيًا يوم بيوم
    بدل ما يترست فجأة أول كل شهر — والبلاغ يبقى محسوب بالإحصائيات حتى لو اختفى من قائمة البلاغات بعد 24 ساعة."""
    now = datetime.now()
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    resolved = 0
    ongoing = 0
    for row in rows:
        while len(row) < 9:
            row.append("")
        status = row[4]
        handled_by = row[8]
        if handled_by != handler_email:
            continue
        if status == "تم حل الطلب" and row[7]:
            try:
                resolved_time = datetime.strptime(row[7], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if now - resolved_time <= STATS_RESET_AFTER:
                resolved += 1
        elif status == "جاري العمل عليه":
            ongoing += 1

    return resolved, ongoing


def get_open_queue_count():
    """يرجع إجمالي عدد البلاغات المفتوحة حاليًا (قيد التنفيذ أو جاري العمل عليها) لكل الفريق، بغض النظر عمّن يتابعها."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    return sum(1 for row in rows if len(row) > 4 and row[4] in ("قيد التنفيذ", "جاري العمل عليه"))


def get_archive_count():
    """يرجع إجمالي عدد البلاغات المغلقة (محلولة أو ملغاة) من كل الأوقات، لعرضه بجانب رابط الأرشيف."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    return sum(1 for row in rows if len(row) > 4 and row[4] in ("تم حل الطلب", "ملغى"))


def format_duration_ar(total_seconds):
    """يحوّل عدد ثواني لنص مدة مقروء بالعربي (يوم/ساعة/دقيقة)."""
    total_minutes = int(total_seconds // 60)
    days, rem_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem_minutes, 60)
    if days > 0:
        return f"{days} يوم {hours} ساعة" if hours else f"{days} يوم"
    if hours > 0:
        return f"{hours} ساعة {minutes} دقيقة" if minutes else f"{hours} ساعة"
    return f"{minutes} دقيقة"


def get_avg_resolution_time():
    """يرجع متوسط الوقت من فتح البلاغ لحين حله (نص جاهز للعرض)، محسوب على كل البلاغات
    المحلولة من كل الأوقات. يرجع None لو ما فيه بلاغات محلولة بعد."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    diffs = []
    for row in rows:
        while len(row) < 9:
            row.append("")
        if row[4] != "تم حل الطلب" or not row[7] or not row[5]:
            continue
        try:
            created = datetime.strptime(row[5], "%Y-%m-%d %H:%M")
            resolved = datetime.strptime(row[7], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        seconds = (resolved - created).total_seconds()
        if seconds >= 0:
            diffs.append(seconds)

    if not diffs:
        return None
    return format_duration_ar(sum(diffs) / len(diffs))


def get_today_ticket_count():
    """يرجع عدد البلاغات اللي وصلت اليوم (بغض النظر عن حالتها)، لكل الفريق."""
    today_str = date.today().isoformat()
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    return sum(1 for row in rows if len(row) > 5 and row[5].startswith(today_str))


def get_top_staff_this_month():
    """يرجع (اسم الموظف، عدد البلاغات) لأكثر موظف فني حلّ بلاغات خلال آخر 30 يوم (STATS_RESET_AFTER)،
    أو None لو ما فيه أي بلاغ محلول ضمن هذي النافذة."""
    now = datetime.now()
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    counts = {}
    for row in rows:
        while len(row) < 9:
            row.append("")
        if row[4] != "تم حل الطلب" or not row[8] or not row[7]:
            continue
        try:
            resolved_time = datetime.strptime(row[7], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if now - resolved_time <= STATS_RESET_AFTER:
            counts[row[8]] = counts.get(row[8], 0) + 1

    if not counts:
        return None

    top_email = max(counts, key=lambda e: counts[e])
    names = get_it_staff_names()
    return names.get(top_email, top_email), counts[top_email]


def get_it_staff_names():
    """يرجع قاموس (إيميل -> الاسم الكامل) لكل موظفي فريق الدعم الفني، لعرض اسم من يتابع/حلّ كل بلاغ."""
    names = {}
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 5 and row[4] == "it":
                names[row[2]] = row[0]
    return names


def get_student_count():
    """يرجع عدد الطلاب المسجّلين (حسابات role=student)."""
    count = 0
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 5 and row[4] == "student":
                count += 1
    return count


@app.route("/it-dashboard")
def it_dashboard():
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    resolved_count, ongoing_count = get_ticket_stats(session.get("email", ""))
    open_queue = get_open_queue_count()
    student_count = get_student_count()
    archive_count = get_archive_count()
    avg_resolution_time = get_avg_resolution_time()
    today_ticket_count = get_today_ticket_count()
    top_staff = get_top_staff_this_month()
    coop_new_count = sum(1 for r in get_all_training_requests() if r[7] == STATUS_AWAITING)
    coop_archive_count = len(get_archived_training_requests())
    coop_notifications = get_coop_notifications(session.get("email", ""))
    return render_template(
        "it_dashboard.html",
        user=session["user"],
        resolved_count=resolved_count,
        ongoing_count=ongoing_count,
        open_queue=open_queue,
        student_count=student_count,
        archive_count=archive_count,
        avg_resolution_time=avg_resolution_time,
        today_ticket_count=today_ticket_count,
        top_staff_name=top_staff[0] if top_staff else None,
        top_staff_count=top_staff[1] if top_staff else 0,
        coop_new_count=coop_new_count,
        coop_archive_count=coop_archive_count,
        coop_notifications=coop_notifications,
    )


@app.route("/it-tickets")
def it_tickets():
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    students = get_all_tickets()
    staff_names = get_it_staff_names()
    return render_template(
        "it_tickets.html",
        user=session["user"],
        students=students,
        statuses=TICKET_STATUSES,
        staff_names=staff_names,
        current_staff_email=session.get("email", ""),
    )


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def update_ticket_status_route(ticket_id):
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    status = request.form.get("status", "")
    result = update_ticket_status(ticket_id, status, session.get("email", ""))

    if result == "locked":
        flash("هذا البلاغ تم حلّه ولا يمكن تغيير حالته بعد ذلك")
    elif result == "cancelled":
        flash("لا يمكن تغيير حالة بلاغ ملغى")
    elif result == "ok":
        flash("تم تحديث حالة البلاغ", "success")

    return redirect(url_for("it_tickets"))


@app.route("/it-archive")
def it_archive():
    """أرشيف كامل للبلاغات المغلقة (محلولة أو ملغاة) من كل الأوقات، بدون نافذة الـ24 ساعة.
    البلاغات هنا للعرض فقط، ما فيه تعديل على حالتها."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    students = get_archived_tickets()
    staff_names = get_it_staff_names()
    return render_template(
        "it_archive.html",
        user=session["user"],
        students=students,
        staff_names=staff_names,
    )


def get_all_students():
    """يرجع كل حسابات الطلاب (الاسم، الإيميل، الرقم التدريبي) لعرضهم بنموذج إصدار خطاب تدريب."""
    students = []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 5 and row[4] == "student":
                students.append({
                    "full_name": row[0],
                    "email": row[2],
                    "training_number": row[5] if len(row) > 5 else "",
                })
    return students


@app.route("/coop")
def coop_dashboard():
    """لوحة التدريب التعاوني — يديرها فريق الدعم الفني بنفس حسابهم الحالي.
    تعرض كل طلبات التدريب وحالتها (قيد المراجعة عند المنشأة / مقبول / مرفوض)."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    requests_list = get_visible_training_requests()
    requests_list.sort(key=lambda r: r[7] != STATUS_AWAITING)  # الطلبات الجديدة فوق (الترتيب الداخلي يبقى الأحدث أولًا)
    return render_template(
        "coop_dashboard.html",
        user=session["user"],
        requests=requests_list,
        archive_count=len(get_archived_training_requests()),
    )


@app.route("/coop/archive")
def coop_archive():
    """أرشيف طلبات التدريب المقفولة (قبول/رفض) اللي مرّ على قرارها أكثر من 24 ساعة وفعليًا
    اختفت من لوحة المتابعة الرئيسية — ما فيه تكرار بين اللوحتين. الطلبات هنا للعرض فقط، ما فيه تعديل على حالتها."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    requests_list = get_archived_training_requests()
    return render_template(
        "coop_archive.html",
        user=session["user"],
        requests=requests_list,
    )


@app.route("/coop/new", methods=["GET", "POST"])
def coop_new_request():
    """نموذج إصدار خطاب تدريب جديد لطالب معيّن، وتوليد رابط آمن يُرسل للمنشأة."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    if request.method == "POST":
        student_email = request.form.get("student_email", "").strip()
        company_name = request.form.get("company_name", "").strip()
        company_email = request.form.get("company_email", "").strip()
        position_title = request.form.get("position_title", "").strip()

        if not student_email or not company_name:
            flash("لازم تعبّي الطالب واسم المنشأة")
            return redirect(url_for("coop_new_request"))

        next_id = get_next_training_id()
        uploaded_files = request.files.getlist("documents")
        saved_docs = save_training_documents(next_id, uploaded_files)
        if uploaded_files and any(f and f.filename for f in uploaded_files) and not saved_docs:
            flash("ما تم قبول أي مستند — تأكد من الصيغة (PDF, Word, صورة) وأن الحجم أقل من 8 ميجابايت")

        result = create_training_request(
            student_email, company_name, company_email, position_title,
            session.get("email", ""), documents=saved_docs, request_id=next_id,
        )
        if not result:
            flash("ما قدرنا نلقى هذا الطالب")
            return redirect(url_for("coop_new_request"))

        verify_link = url_for("coop_verify", token=result["token"], _external=True)
        flash(f"تم إصدار الخطاب — رابط المنشأة: {verify_link}", "success")
        return redirect(url_for("coop_dashboard"))

    students = get_all_students()
    return render_template("coop_new.html", user=session["user"], students=students)


@app.route("/coop/verify/<token>", methods=["GET", "POST"])
def coop_verify(token):
    """صفحة عامة بدون تسجيل دخول — تفتحها المنشأة عبر رابطها الآمن لقبول أو رفض الطالب."""
    training_request = get_training_request_by_token(token)
    if not training_request:
        return render_template("coop_verify.html", state="notfound")

    if is_training_link_expired(training_request) and training_request[7] == "قيد المراجعة عند المنشأة":
        return render_template("coop_verify.html", state="expired", req=training_request)

    if training_request[7] != "قيد المراجعة عند المنشأة":
        return render_template("coop_verify.html", state="decided", req=training_request)

    if request.method == "POST":
        decision = request.form.get("decision", "")
        supervisor_name = request.form.get("supervisor_name", "").strip()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()

        if decision not in ("accept", "reject"):
            return render_template("coop_verify.html", state="pending", req=training_request, error="فضلًا اختر القبول أو الرفض")
        if decision == "accept" and not supervisor_name:
            return render_template("coop_verify.html", state="pending", req=training_request, error="لازم تكتب اسم المشرف الميداني")
        if decision == "accept" and start_date and end_date and end_date < start_date:
            return render_template("coop_verify.html", state="pending", req=training_request, error="تاريخ نهاية التدريب لازم يكون بعد تاريخ البداية")

        result = decide_training_request(token, decision, supervisor_name, start_date, end_date)
        if result == "ok":
            return render_template("coop_verify.html", state="submitted", decision=decision)
        elif result == "expired":
            return render_template("coop_verify.html", state="expired", req=training_request)
        elif result == "already_decided":
            return render_template("coop_verify.html", state="decided", req=training_request)
        else:
            return render_template("coop_verify.html", state="notfound")

    return render_template("coop_verify.html", state="pending", req=training_request)


def _valid_email(value):
    value = (value or "").strip()
    return len(value) <= 120 and "@" in value and "." in value.split("@")[-1] and " " not in value


@app.route("/coop/request", methods=["GET", "POST"])
def coop_student_request():
    """الطالب يقدّم طلب تدريب تعاوني ويحدد الجهة اللي يبيها، عشان الكلية تصدر له خطاب موجّه لها."""
    guard = require_student()
    if guard:
        return guard

    if request.method == "POST":
        company_name = " ".join(request.form.get("company_name", "").split())
        position_title = " ".join(request.form.get("position_title", "").split())
        company_email = request.form.get("company_email", "").strip()
        notes = " ".join(request.form.get("notes", "").split())

        error = None
        if not company_name:
            error = "لازم تكتب اسم الجهة"
        elif len(company_name) > 100 or len(position_title) > 100:
            error = "اسم الجهة أو المسمى طويل زيادة (الحد 100 حرف)"
        elif company_email and not _valid_email(company_email):
            error = "صيغة إيميل الجهة غير صحيحة"
        elif len(notes) > 500:
            error = "الملاحظات طويلة زيادة (الحد 500 حرف)"

        if not error:
            result = create_student_training_request(
                session.get("email", ""), company_name, company_email, position_title, notes,
            )
            if "id" in result:
                flash("وصل طلبك للكلية — بنصدر لك الخطاب الموجّه للجهة، وتتابع حالته من هنا", "success")
                return redirect(url_for("my_coop"))
            if result["error"] == "duplicate":
                error = "عندك طلب نشط لنفس الجهة بالفعل، تابعه من صفحة الخدمة"
            elif result["error"] == "limit":
                error = f"وصلت للحد الأقصى ({MAX_OPEN_STUDENT_REQUESTS} طلبات) بانتظار إصدار الخطاب — انتظر الكلية تعالجها أول"
            else:
                error = "ما قدرنا نسجّل الطلب، حاول مرة ثانية"
        flash(error)
        return render_template("coop_request.html", user=session["user"], form=request.form)

    return render_template("coop_request.html", user=session["user"], form={})


@app.route("/coop/<int:req_id>/issue", methods=["GET", "POST"])
def coop_issue_letter(req_id):
    """الموظف يراجع طلب الطالب، ويكمّل بيانات الجهة، ويصدر الخطاب (يولّد رابط المنشأة)."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    row = get_training_request_by_id(req_id)
    if not row or row[7] != STATUS_AWAITING:
        flash("هذا الطلب مو متاح للإصدار (ممكن انصدر أو انرفض قبل)")
        return redirect(url_for("coop_dashboard"))

    if request.method == "POST":
        if request.form.get("edit_version", "") != (row[26] or ""):
            flash("الطالب عدّل بيانات طلبه وأنت تراجعه — حدّثنا البيانات لك، راجعها وأرفق المستندات ثم أصدر الخطاب")
            return render_template("coop_issue.html", user=session["user"], req=row, form={})

        company_name = " ".join(request.form.get("company_name", "").split())
        company_email = request.form.get("company_email", "").strip()
        position_title = " ".join(request.form.get("position_title", "").split())

        if not company_name:
            flash("لازم تكتب اسم الجهة")
            return render_template("coop_issue.html", user=session["user"], req=row, form=request.form)
        if company_email and not _valid_email(company_email):
            flash("صيغة إيميل الجهة غير صحيحة")
            return render_template("coop_issue.html", user=session["user"], req=row, form=request.form)

        uploaded_files = request.files.getlist("documents")
        if not any(f and f.filename for f in uploaded_files):
            flash("لازم ترفق مستند واحد على الأقل (خطاب رسمي، سيرة ذاتية...)")
            return render_template("coop_issue.html", user=session["user"], req=row, form=request.form)
        saved_docs = save_training_documents(req_id, uploaded_files)
        if not saved_docs:
            flash("ما تم قبول أي مستند — تأكد من الصيغة (PDF, Word, صورة) وأن الحجم أقل من 8 ميجابايت")
            return render_template("coop_issue.html", user=session["user"], req=row, form=request.form)

        result = issue_training_letter(
            req_id, company_name, company_email, position_title,
            session.get("email", ""), documents=saved_docs, expected_edit_at=row[26] or "",
        )
        if isinstance(result, dict) and result.get("conflict"):
            flash("الطالب عدّل طلبه قبل لحظات — راجع البيانات المحدّثة وأعد المحاولة")
            return redirect(url_for("coop_issue_letter", req_id=req_id))
        if not result:
            flash("ما قدرنا نصدر الخطاب — الطلب تغيّرت حالته")
            return redirect(url_for("coop_dashboard"))

        verify_link = url_for("coop_verify", token=result["token"], _external=True)
        flash(f"تم إصدار الخطاب — رابط المنشأة: {verify_link}", "success")
        return redirect(url_for("coop_dashboard"))

    return render_template("coop_issue.html", user=session["user"], req=row, form={})


@app.route("/coop/<int:req_id>/reject", methods=["POST"])
def coop_college_reject(req_id):
    """الموظف يرفض طلب الطالب (قبل إصدار الخطاب) مع سبب يظهر للطالب."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    reason = " ".join(request.form.get("reason", "").split())
    if not reason or len(reason) > 300:
        flash("اكتب سبب الرفض (بحد أقصى 300 حرف) عشان يظهر للطالب")
        return redirect(url_for("coop_dashboard"))
    if reject_student_training_request(req_id, reason):
        flash("تم رفض الطلب وإشعار الطالب بالسبب", "success")
    else:
        flash("ما قدرنا نرفض الطلب — ممكن انصدر أو انرفض قبل")
    return redirect(url_for("coop_dashboard"))


@app.route("/coop/<int:req_id>/edit", methods=["GET", "POST"])
def coop_edit_request(req_id):
    """الموظف يعدّل طلب تدريب صدر له خطاب، لو الطالب أو الجهة سجّلوا شي غلط (اسم الجهة، الإيميل، المسمى،
    وبعد قبول الجهة: المشرف وتواريخ التدريب). كل تعديل يُسجَّل باسم الموظف."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    row = get_training_request_by_id(req_id)
    if not row or row[7] not in EDITABLE_TRAINING_STATUSES:
        flash("هذا الطلب ما يمكن تعديله (يعدّل الطلب بعد إصدار الخطاب فقط)")
        return redirect(url_for("coop_dashboard"))

    is_accepted = row[7] == "تم القبول"
    can_reopen = row[7] != STATUS_COMPANY_PENDING and not (row[19] or row[20] or row[24])

    if request.method == "POST":
        company_name = " ".join(request.form.get("company_name", "").split())
        company_email = request.form.get("company_email", "").strip()
        position_title = " ".join(request.form.get("position_title", "").split())
        supervisor_name = start_date = end_date = None
        error = None

        if not company_name:
            error = "لازم تكتب اسم الجهة"
        elif len(company_name) > 100 or len(position_title) > 100:
            error = "اسم الجهة أو المسمى طويل زيادة (الحد 100 حرف)"
        elif company_email and not _valid_email(company_email):
            error = "صيغة إيميل الجهة غير صحيحة"
        elif is_accepted:
            supervisor_name = " ".join(request.form.get("supervisor_name", "").split())
            start_date = request.form.get("start_date", "").strip()
            end_date = request.form.get("end_date", "").strip()
            if not supervisor_name:
                error = "لازم تكتب اسم المشرف الميداني"
            elif len(supervisor_name) > 100:
                error = "اسم المشرف طويل زيادة (الحد 100 حرف)"
            elif not _valid_date(start_date) or not _valid_date(end_date):
                error = "صيغة التاريخ غير صحيحة"
            elif start_date and end_date and end_date < start_date:
                error = "تاريخ نهاية التدريب لازم يكون بعد تاريخ البداية"

        if error:
            flash(error)
            return render_template("coop_edit.html", user=session["user"], req=row, form=request.form,
                                   is_accepted=is_accepted, can_reopen=can_reopen)

        if update_training_details(req_id, session.get("email", ""), company_name, company_email, position_title,
                                   supervisor_name, start_date, end_date):
            flash("تم حفظ التعديلات", "success")
        else:
            flash("ما قدرنا نحفظ — تغيّرت حالة الطلب")
        return redirect(url_for("coop_edit_request", req_id=req_id))

    return render_template("coop_edit.html", user=session["user"], req=row, form={},
                           is_accepted=is_accepted, can_reopen=can_reopen)


@app.route("/coop/<int:req_id>/reopen", methods=["POST"])
def coop_reopen_request(req_id):
    """تجديد رابط المنشأة، أو إعادة فتح قرارها لو قبلت/رفضت بالغلط — يتولّد رابط جديد والقديم يوقف."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    result = reopen_training_request(req_id, session.get("email", ""))
    if not result:
        flash("ما قدرنا نعيد فتح الطلب — تغيّرت حالته")
        return redirect(url_for("coop_dashboard"))
    if result.get("error") == "completion":
        flash("ما نقدر نعيد فتح القرار لأن الطالب رفع شهادة إتمام لهذا الطلب — تقدر تعدّل البيانات فقط")
        return redirect(url_for("coop_edit_request", req_id=req_id))

    verify_link = url_for("coop_verify", token=result["token"], _external=True)
    label = "تم تجديد رابط المنشأة" if result["kind"] == "renewed" else "تمت إعادة فتح الطلب للمنشأة"
    flash(f"{label} — الرابط الجديد: {verify_link} (الرابط القديم ما عاد يشتغل)", "success")
    return redirect(url_for("coop_dashboard"))


@app.route("/my-coop")
def my_coop():
    """صفحة الطالب لمتابعة حالة طلبات التدريب التعاوني الخاصة به."""
    guard = require_student()
    if guard:
        return guard
    my_requests = get_training_requests_by_student(session.get("email", ""))
    for r in my_requests:
        token = r.pop("token")
        r["link"] = url_for("coop_verify", token=token, _external=True) if token else ""
    return render_template("my_coop.html", user=session["user"], requests=my_requests)


@app.route("/my-coop/<int:req_id>/edit", methods=["GET", "POST"])
def coop_student_edit(req_id):
    """الطالب يعدّل طلبه (اسم الجهة، المسمى، الإيميل، الملاحظات) طالما الكلية ما أصدرت الخطاب بعد."""
    guard = require_student()
    if guard:
        return guard

    email = session.get("email", "")
    row = get_training_request_by_id(req_id)
    if not row or row[1].strip().lower() != email.strip().lower():
        flash("ما قدرنا نلقى هذا الطلب")
        return redirect(url_for("my_coop"))
    if row[7] != STATUS_AWAITING:
        flash("ما تقدر تعدّل الطلب بعد ما تعالجه الكلية — تواصل معها لو فيه خطأ")
        return redirect(url_for("my_coop"))

    if request.method == "POST":
        company_name = " ".join(request.form.get("company_name", "").split())
        position_title = " ".join(request.form.get("position_title", "").split())
        company_email = request.form.get("company_email", "").strip()
        notes = " ".join(request.form.get("notes", "").split())

        error = None
        if not company_name:
            error = "لازم تكتب اسم الجهة"
        elif len(company_name) > 100 or len(position_title) > 100:
            error = "اسم الجهة أو المسمى طويل زيادة (الحد 100 حرف)"
        elif company_email and not _valid_email(company_email):
            error = "صيغة إيميل الجهة غير صحيحة"
        elif len(notes) > 500:
            error = "الملاحظات طويلة زيادة (الحد 500 حرف)"
        elif find_duplicate_active_request(email, company_name, exclude_id=req_id):
            error = "عندك طلب نشط لنفس الجهة بالفعل، تابعه من صفحة الخدمة"
        if error:
            flash(error)
            return render_template("coop_request.html", user=session["user"], form=request.form, editing=True, req_id=req_id)

        unchanged = (
            company_name == row[4] and position_title == row[6]
            and company_email == row[5] and notes == row[16]
        )
        if unchanged:
            flash("ما تغيّر شي في الطلب", "success")
        elif update_student_training_request(req_id, email, company_name, company_email, position_title, notes):
            flash("تم تعديل طلبك، ويوصل التحديث لمسؤول التدريب", "success")
        else:
            flash("ما قدرنا نحفظ التعديل — الكلية عالجت الطلب قبل لحظات")
        return redirect(url_for("my_coop"))

    current = {"company_name": row[4], "position_title": row[6], "company_email": row[5], "notes": row[16]}
    return render_template("coop_request.html", user=session["user"], form=current, editing=True, req_id=req_id)


@app.route("/coop/<int:req_id>/complete", methods=["POST"])
def coop_submit_completion(req_id):
    """الطالب يرفع شهادة إتمام التدريب من المنشأة بعد نهاية فترة التدريب، وتروح لمراجعة الكلية."""
    guard = require_student()
    if guard:
        return guard

    row = get_training_request_by_id(req_id)
    if not row or row[1].strip().lower() != session.get("email", "").strip().lower():
        flash("ما قدرنا نلقى هذا الطلب")
        return redirect(url_for("my_coop"))
    if row[7] != "تم القبول":
        flash("هذا الطلب مو بحالة تسمح برفع شهادة إتمام")
        return redirect(url_for("my_coop"))

    stored = save_single_training_file(req_id, request.files.get("completion_cert"), prefix="cert_")
    if not stored:
        flash("ما تم قبول الملف — تأكد من الصيغة (PDF, Word, صورة) وأن الحجم أقل من 8 ميجابايت")
        return redirect(url_for("my_coop"))

    if not submit_completion_certificate(req_id, session.get("email", ""), stored):
        flash("ما قدرنا نسجّل الشهادة — حاول مرة ثانية")
        return redirect(url_for("my_coop"))

    flash("تم رفع شهادة الإتمام، بانتظار مراجعة الكلية لها", "success")
    return redirect(url_for("my_coop"))


@app.route("/coop/<int:req_id>/complete/review", methods=["POST"])
def coop_review_completion(req_id):
    """الموظف يعتمد أو يرفض شهادة إتمام التدريب اللي رفعها الطالب، ويقدر يرفق الشهادة الرسمية عند الاعتماد."""
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    decision = request.form.get("decision", "")
    if decision == "approve":
        cert_file = request.files.get("final_certificate")
        if not cert_file or not cert_file.filename:
            flash("لازم ترفق شهادة الطالب عشان تعتمد إتمام التدريب")
            return redirect(url_for("coop_dashboard"))
        final_cert = save_single_training_file(req_id, cert_file, prefix="finalcert_")
        if not final_cert:
            flash("ما تم قبول ملف الشهادة — تأكد من الصيغة (PDF, Word, صورة) وأن الحجم أقل من 8 ميجابايت")
            return redirect(url_for("coop_dashboard"))
        if review_completion_certificate(req_id, "approve", final_cert=final_cert):
            flash("تم اعتماد إتمام التدريب وإرفاق الشهادة للطالب", "success")
        else:
            flash("ما قدرنا نعتمد — تأكد إن فيه شهادة بانتظار المراجعة لهذا الطلب")
    elif decision == "reject":
        reason = " ".join(request.form.get("reason", "").split())
        if not reason or len(reason) > 300:
            flash("اكتب سبب رفض الشهادة (بحد أقصى 300 حرف) عشان يظهر للطالب")
        elif review_completion_certificate(req_id, "reject", reason=reason):
            flash("تم رفض الشهادة وإشعار الطالب بالسبب", "success")
        else:
            flash("ما قدرنا نرفض — تأكد إن فيه شهادة بانتظار المراجعة لهذا الطلب")
    else:
        flash("قرار غير صحيح")
    return redirect(url_for("coop_dashboard"))


def get_user_by_email(email):
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) < 5:
                continue
            if row[2] == email:
                return {
                    "full_name": row[0],
                    "birthdate": row[1],
                    "email": row[2],
                    "role": row[4],
                    "training_number": row[5] if len(row) > 5 else "",
                    "last_updated": row[6] if len(row) > 6 else "",
                }
    return None


def get_next_ticket_id():
    """يرجع رقم البلاغ التالي = أكبر رقم موجود + 1، ويبدأ من #1001.
    الاعتماد على أكبر رقم بدل عدد الصفوف يمنع تكرار الأرقام لو انحذف صف أو تغيّر ترتيب الملف."""
    max_id = 1000
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # تخطي صف العناوين
        for row in reader:
            if not row:
                continue
            try:
                max_id = max(max_id, int(row[0]))
            except (ValueError, IndexError):
                continue  # صف تالف أو رقم غير صحيح — نتجاهله
    return max_id + 1


def save_ticket(email, title, description):
    next_id = get_next_ticket_id()

    with open(TICKETS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            next_id, email, title, description, "قيد التنفيذ",
            datetime.now().strftime("%Y-%m-%d %H:%M"), "0", "", ""
        ])


def get_tickets_by_email(email):
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        tickets = [row for row in reader if row and row[1] == email]
    for row in tickets:
        while len(row) < 7:
            row.append("0")  # صف قديم بدون عمود edited
        while len(row) < 8:
            row.append("")  # صف قديم بدون عمود resolved_at
        while len(row) < 9:
            row.append("")  # صف قديم بدون عمود handled_by
    tickets = [row for row in tickets if is_ticket_visible(row)]
    tickets.reverse()  # الأحدث أولًا
    return tickets


def _group_tickets_by_student(rows):
    """يجمّع صفوف بلاغات (بعد إكمال أعمدتها) حسب كل طالب، بنفس ترتيبها المُمرَّر."""
    students = {}
    order = []
    for row in rows:
        email = row[1]
        if email not in students:
            user_data = get_user_by_email(email)
            students[email] = {
                "email": email,
                "full_name": user_data["full_name"] if user_data else email,
                "training_number": user_data["training_number"] if user_data else "—",
                "tickets": [],
            }
            order.append(email)
        students[email]["tickets"].append(row)

    return [students[email] for email in order]


def get_all_tickets():
    """يجيب كل البلاغات الظاهرة حاليًا من جميع الطلاب مجمّعة حسب كل طالب (اسمه، رقمه التدريبي، وبلاغاته)."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    for row in rows:
        while len(row) < 7:
            row.append("0")  # صف قديم بدون عمود edited
        while len(row) < 8:
            row.append("")  # صف قديم بدون عمود resolved_at
        while len(row) < 9:
            row.append("")  # صف قديم بدون عمود handled_by

    rows = [row for row in rows if is_ticket_visible(row)]
    rows.reverse()  # الأحدث أولًا

    return _group_tickets_by_student(rows)


def get_archived_tickets():
    """يجيب كل البلاغات المغلقة (محلولة أو ملغاة) من كل الأوقات، بغض النظر عن نافذة الـ24 ساعة —
    هذي أرشيف كامل لسجل البلاغات، يستخدمها فريق الدعم الفني للرجوع لأي بلاغ قديم."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    for row in rows:
        while len(row) < 7:
            row.append("0")
        while len(row) < 8:
            row.append("")
        while len(row) < 9:
            row.append("")

    rows = [row for row in rows if row[4] in CLOSED_STATUSES]
    rows.reverse()  # الأحدث أولًا

    return _group_tickets_by_student(rows)


def cancel_ticket(ticket_id, email):
    """يغيّر حالة بلاغ معيّن إلى (ملغى)، فقط إذا كان يخص نفس المستخدم."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[0] == str(ticket_id) and row[1] == email:
            while len(row) < 9:
                row.append("")  # صف قديم بدون عمود resolved_at / handled_by
            row[4] = "ملغى"
            row[7] = datetime.now().strftime("%Y-%m-%d %H:%M")  # وقت الإلغاء، يُستخدم لإخفائه بعد 24 ساعة
            updated = True

    if updated:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


TICKET_STATUSES = ["قيد التنفيذ", "جاري العمل عليه", "تم حل الطلب"]
CLOSED_STATUSES = ("تم حل الطلب", "ملغى")
CLOSED_HIDE_AFTER = timedelta(hours=24)  # مدة بقاء البلاغ المحلول أو الملغى ظاهرًا قبل ما يختفي من القوائم (البيانات تبقى محفوظة بالملف)


def is_ticket_visible(row):
    """يرجع False إذا كان البلاغ (محلول أو ملغى) ومرّ على إغلاقه أكثر من CLOSED_HIDE_AFTER.
    البلاغ يبقى محفوظًا بالملف دائمًا، بس نخفيه من العرض فقط."""
    if row[4] not in CLOSED_STATUSES:
        return True
    closed_at = row[7] if len(row) > 7 else ""
    if not closed_at:
        return True
    try:
        closed_time = datetime.strptime(closed_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return datetime.now() - closed_time <= CLOSED_HIDE_AFTER


# ============================ التدريب التعاوني ============================

TRAINING_STATUSES = ("قيد المراجعة عند المنشأة", "تم القبول", "مرفوض")


def get_training_requests_by_student(email):
    """يرجع طلبات التدريب التعاوني الخاصة بطالب واحد (الأحدث أولًا) بدون أي بيانات حساسة —
    الرمز السري (token) وإيميل المنشأة ما تنعرض للطالب أبدًا.
    خطاب التدريب (المستندات المرفقة وقت الإصدار) يظهر للطالب بمجرد إصدار الخطاب، عشان يقدر يطبعه."""
    email = (email or "").strip().lower()
    result = []
    for row in get_all_training_requests():
        if row[1].strip().lower() != email:
            continue
        if not is_training_request_visible(row):
            continue
        if row[7] == "تم القبول":
            state = "accepted"
        elif row[7] == "مرفوض":
            state = "rejected"
        elif row[7] == STATUS_AWAITING:
            state = "awaiting"
        elif row[7] == STATUS_COLLEGE_REJECTED:
            state = "college_rejected"
        elif row[7] == STATUS_COMPANY_PENDING and is_training_link_expired(row):
            state = "expired"
        else:
            state = "pending"

        # الخطاب موجود فقط بعد الإصدار (كل الحالات إلا انتظار الإصدار أو رفض الكلية قبل الإصدار)
        documents = []
        if state not in ("awaiting", "college_rejected") and row[15]:
            for stored_name in row[15].split(","):
                stored_name = stored_name.strip()
                if not stored_name:
                    continue
                ext = stored_name.rsplit(".", 1)[-1].lower() if "." in stored_name else ""
                documents.append({
                    "url": url_for("static", filename=f"coop_docs/{stored_name}"),
                    "name": display_doc_name(stored_name),
                    "is_image": ext in ("jpg", "jpeg", "png"),
                })

        training_ended = False
        if row[18]:
            try:
                training_ended = date.today() >= datetime.strptime(row[18], "%Y-%m-%d").date()
            except ValueError:
                training_ended = False

        result.append({
            "id": row[0],
            "company_name": row[4],
            "position_title": row[6],
            "status": row[7],
            "state": state,
            "expires_at": row[9],
            "created_at": row[10],
            "supervisor_name": row[11],
            "start_date": row[12],
            "end_date": row[18],
            "training_ended": training_ended,
            "decided_at": row[13],
            "college_reject_reason": row[17],
            "documents": documents,
            "completion_status": row[20],
            "completion_cert_url": url_for("static", filename=f"coop_docs/{row[19]}") if row[19] else "",
            "completion_cert_name": display_doc_name(row[19]) if row[19] else "",
            "completion_reject_reason": row[23],
            "final_certificate_url": url_for("static", filename=f"coop_docs/{row[24]}") if row[24] else "",
            "final_certificate_name": display_doc_name(row[24]) if row[24] else "",
            # رمز رابط المنشأة يُعرض للطالب فقط طالما الرابط صالح، عشان يسلّمه للجهة
            "token": row[8] if state == "pending" else "",
        })
    return result


def get_next_training_id():
    """يرجع رقم الطلب التالي = أكبر رقم موجود + 1، نفس منطق ترقيم البلاغات."""
    max_id = 0
    if os.path.exists(TRAINING_FILE):
        with open(TRAINING_FILE, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if not row:
                    continue
                try:
                    max_id = max(max_id, int(row[0]))
                except (ValueError, IndexError):
                    continue
    return max_id + 1


def create_training_request(student_email, company_name, company_email, position_title, issued_by, documents=None, request_id=None):
    """ينشئ طلب تدريب تعاوني جديد لطالب معيّن، ويولّد رمز وصول آمن (token) للمنشأة
    صالح لمدة TRAINING_LINK_VALID_DAYS يوم، ترسله الكلية للمنشأة عشان تقبل/ترفض الطالب من دون حساب.
    documents: أسماء الملفات المخزّنة (بعد حفظها بمجلد TRAINING_DOCS_DIR) لإرفاقها بالطلب."""
    student = get_user_by_email(student_email)
    if not student or student["role"] != "student":
        return None

    next_id = request_id if request_id is not None else get_next_training_id()
    token = secrets.token_urlsafe(24)
    now = datetime.now()
    expires_at = now + timedelta(days=TRAINING_LINK_VALID_DAYS)
    documents_str = ",".join(documents) if documents else ""

    with open(TRAINING_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            next_id, student_email, student["full_name"], student["training_number"],
            company_name, company_email, position_title,
            "قيد المراجعة عند المنشأة", token, expires_at.strftime("%Y-%m-%d %H:%M"),
            now.strftime("%Y-%m-%d %H:%M"),
            "", "", "", issued_by, documents_str, "", "",
        ])

    return {"id": next_id, "token": token}


def _pad_training_row(row):
    while len(row) < TRAINING_COLUMNS:
        row.append("")
    return row


def display_doc_name(stored_name):
    """يشيل بادئة التخزين (رقم الطلب + رمز عشوائي) ويرجّع اسم الملف الأصلي للعرض على الموظف."""
    parts = stored_name.split("_", 2)
    return parts[2] if len(parts) == 3 else stored_name


def save_training_documents(request_id, files):
    """يحفظ مستندات مرفقة (خطاب رسمي، سيرة ذاتية...) بمجلد ثابت، ويرجّع أسماء الملفات المخزّنة.
    يتجاهل الملفات بصيغة غير مسموحة أو تتجاوز الحجم الأقصى بدل ما يوقف العملية كاملة."""
    saved = []
    os.makedirs(TRAINING_DOCS_DIR, exist_ok=True)
    for file in files[:MAX_DOC_FILES]:
        if not file or not file.filename:
            continue
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_DOC_EXT:
            continue
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(0)
        if size > MAX_DOC_SIZE:
            continue
        safe_name = secure_filename(file.filename) or f"file.{ext}"
        stored_name = f"{request_id}_{secrets.token_hex(3)}_{safe_name}"
        file.save(os.path.join(TRAINING_DOCS_DIR, stored_name))
        saved.append(stored_name)
    return saved


def save_single_training_file(request_id, file, prefix=""):
    """يحفظ ملف واحد (شهادة إتمام تدريب مثلًا) بنفس قيود مستندات التدريب.
    يرجّع اسم الملف المخزّن، أو None لو الملف غير موجود أو الصيغة/الحجم غير مقبولة."""
    if not file or not file.filename:
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_DOC_EXT:
        return None
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_DOC_SIZE:
        return None
    os.makedirs(TRAINING_DOCS_DIR, exist_ok=True)
    safe_name = secure_filename(file.filename) or f"file.{ext}"
    stored_name = f"{prefix}{request_id}_{secrets.token_hex(3)}_{safe_name}"
    file.save(os.path.join(TRAINING_DOCS_DIR, stored_name))
    return stored_name


def submit_completion_certificate(req_id, student_email, stored_name):
    """الطالب يرفع شهادة إتمام تدريب من المنشأة بعد نهاية فترة التدريب، وتروح لمراجعة الكلية.
    يُسمح بإعادة الرفع لو الشهادة السابقة انرفضت. يرجّع True/None."""
    def updater(row):
        if row[1].strip().lower() != (student_email or "").strip().lower():
            return None
        if row[7] != "تم القبول":
            return None
        if row[20] == COMPLETION_APPROVED:
            return None
        row[19] = stored_name
        row[20] = COMPLETION_PENDING
        row[21] = datetime.now().strftime("%Y-%m-%d %H:%M")
        row[22] = ""
        row[23] = ""
        return True
    return _update_training_row(req_id, updater)


def review_completion_certificate(req_id, decision, reason="", final_cert=None):
    """الكلية تعتمد أو ترفض شهادة الإتمام اللي رفعها الطالب. يرجّع True/None."""
    def updater(row):
        if row[20] != COMPLETION_PENDING:
            return None
        row[22] = datetime.now().strftime("%Y-%m-%d %H:%M")
        if decision == "approve":
            row[20] = COMPLETION_APPROVED
            if final_cert:
                row[24] = final_cert
        else:
            row[20] = COMPLETION_REJECTED
            row[23] = reason
        return True
    return _update_training_row(req_id, updater)


def get_all_training_requests():
    """يرجع كل طلبات التدريب التعاوني، الأحدث أولًا، لعرضها بلوحة فريق الدعم الفني."""
    if not os.path.exists(TRAINING_FILE):
        return []
    with open(TRAINING_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [_pad_training_row(row) for row in reader if row]
    rows.reverse()
    return rows


def is_training_request_visible(row):
    """يرجع False إذا كان الطلب مقفول (قبول/رفض المنشأة أو رفض الكلية) ومرّ على قراره أكثر
    من TRAINING_HIDE_AFTER. الطلب يبقى محفوظًا بالملف دائمًا، بس نخفيه من اللوحة والأرشيف يتكفّل فيه."""
    if row[7] not in CLOSED_TRAINING_STATUSES:
        return True
    if row[7] == "تم القبول" and row[20] == COMPLETION_PENDING:
        return True  # فيه شهادة إتمام بانتظار مراجعة الكلية — يبقى ظاهرًا باللوحة حتى لو مرّ أكثر من TRAINING_HIDE_AFTER
    decided_at = row[13] if len(row) > 13 else ""
    if not decided_at:
        return True
    try:
        decided_time = datetime.strptime(decided_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return datetime.now() - decided_time <= TRAINING_HIDE_AFTER


def get_visible_training_requests():
    """كل طلبات التدريب الظاهرة حاليًا بلوحة فريق الدعم الفني (تستبعد المقفولة اللي مرّ على قرارها
    أكثر من TRAINING_HIDE_AFTER — هذي تروح للأرشيف بدالها)."""
    return [row for row in get_all_training_requests() if is_training_request_visible(row)]


def get_archived_training_requests():
    """يجيب طلبات التدريب المقفولة (قبول/رفض) اللي مرّ على قرارها أكثر من TRAINING_HIDE_AFTER —
    يعني اللي فعليًا اختفت من لوحة المتابعة الرئيسية. ما فيه تكرار بين اللوحة والأرشيف:
    الطلب يكون بأحدهما بس، مو بالاثنين بنفس الوقت."""
    rows = [
        row for row in get_all_training_requests()
        if row[7] in CLOSED_TRAINING_STATUSES and not is_training_request_visible(row)
    ]
    return rows


def get_dismissed_notification_keys(staff_email):
    """مفاتيح التنبيهات اللي هذا الموظف تجاهلها."""
    keys = set()
    if not os.path.exists(NOTIF_DISMISS_FILE):
        return keys
    email = (staff_email or "").strip().lower()
    with open(NOTIF_DISMISS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 2 and row[0].strip().lower() == email:
                keys.add(row[1])
    return keys


def dismiss_notifications(staff_email, keys):
    """يسجّل تجاهل مجموعة تنبيهات لهذا الموظف (ما يكرر المفتاح لو كان متجاهَلًا من قبل)."""
    already = get_dismissed_notification_keys(staff_email)
    new_keys = [k for k in keys if k and k not in already]
    if not new_keys:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(NOTIF_DISMISS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for k in new_keys:
            writer.writerow([staff_email, k, now])
    return len(new_keys)


def get_coop_notifications(staff_email=None, include_dismissed=False):
    """تنبيهات فريق الدعم الفني بالتدريب التعاوني — تُحسب من حالة الطلبات الحالية،
    فأي تنبيه يختفي تلقائيًا لما الموظف يعالج الطلب (يصدر الخطاب / يراجع الشهادة).
    الموظف يقدر يتجاهل التنبيه يدويًا؛ والمفتاح يشمل وقت الحدث، فلو الطالب رفع شهادة جديدة
    (بعد رفض الأولى مثلًا) يرجع التنبيه يظهر من جديد.
    كل عنصر: {key, req_id, kind, text, time}. الأحدث أولًا."""
    dismissed = set() if include_dismissed else get_dismissed_notification_keys(staff_email)
    items = []
    for r in get_all_training_requests():
        if r[7] == "تم القبول" and r[20] == COMPLETION_PENDING:
            kind, text, when = "certificate", f"الطالب {r[2]} أرفق شهادة الإتمام", (r[21] or r[13])
        elif r[7] == STATUS_AWAITING and r[26]:
            kind, text, when = "edited_request", f"الطالب {r[2]} عدّل بيانات طلب التدريب", r[26]
        elif r[7] == STATUS_AWAITING:
            kind, text, when = "new_request", f"الطالب {r[2]} قدّم طلب تدريب جديد", r[10]
        else:
            continue
        key = f"{r[0]}|{kind}|{when}"
        if key in dismissed:
            continue
        items.append({"key": key, "req_id": r[0], "kind": kind, "text": text, "time": when})
    items.sort(key=lambda n: n["time"] or "", reverse=True)
    return items


@app.route("/coop/notifications/dismiss", methods=["POST"])
def coop_dismiss_notifications():
    """تجاهل تنبيه واحد (key) أو كل التنبيهات الحالية (all=1) — يرجّع JSON عشان الواجهة تحدّث نفسها."""
    if session.get("role") != "it":
        return {"ok": False}, 403
    email = session.get("email", "")
    if request.form.get("all") == "1":
        keys = [n["key"] for n in get_coop_notifications(email)]
    else:
        keys = [request.form.get("key", "").strip()]
    dismiss_notifications(email, keys)
    return {"ok": True, "remaining": len(get_coop_notifications(email))}


def get_training_request_by_id(req_id):
    """يرجع صف طلب التدريب برقمه، أو None لو ما وجد."""
    for row in get_all_training_requests():
        if row[0] == str(req_id):
            return row
    return None


def _normalize_company(name):
    return " ".join((name or "").split()).lower()


def create_student_training_request(student_email, company_name, company_email, position_title, notes):
    """ينشئ طلب تدريب قدّمه الطالب بنفسه (بدون رمز)، بحالة "بانتظار إصدار الخطاب".
    يرجّع {"id": ...} عند النجاح، أو {"error": "nostudent" | "duplicate" | "limit"}."""
    student = get_user_by_email(student_email)
    if not student or student["role"] != "student":
        return {"error": "nostudent"}

    email_key = student_email.strip().lower()
    company_key = _normalize_company(company_name)
    awaiting = 0
    for row in get_all_training_requests():
        if row[1].strip().lower() != email_key:
            continue
        if row[7] == STATUS_AWAITING:
            awaiting += 1
        active = (
            row[7] in (STATUS_AWAITING, "تم القبول")
            or (row[7] == STATUS_COMPANY_PENDING and not is_training_link_expired(row))
        )
        if active and _normalize_company(row[4]) == company_key:
            return {"error": "duplicate"}
    if awaiting >= MAX_OPEN_STUDENT_REQUESTS:
        return {"error": "limit"}

    next_id = get_next_training_id()
    with open(TRAINING_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            next_id, student_email, student["full_name"], student["training_number"],
            company_name, company_email, position_title,
            STATUS_AWAITING, "", "", datetime.now().strftime("%Y-%m-%d %H:%M"),
            "", "", "", "", "", notes, "",
        ])
    return {"id": next_id}


def find_duplicate_active_request(student_email, company_name, exclude_id=None):
    """هل عند الطالب طلب نشط ثاني لنفس الجهة؟ (نفس قاعدة منع التكرار وقت التقديم، مع استثناء الطلب اللي يعدّله)."""
    email_key = student_email.strip().lower()
    company_key = _normalize_company(company_name)
    for row in get_all_training_requests():
        if row[1].strip().lower() != email_key or (exclude_id is not None and row[0] == str(exclude_id)):
            continue
        active = (
            row[7] in (STATUS_AWAITING, "تم القبول")
            or (row[7] == STATUS_COMPANY_PENDING and not is_training_link_expired(row))
        )
        if active and _normalize_company(row[4]) == company_key:
            return True
    return False


def update_student_training_request(req_id, student_email, company_name, company_email, position_title, notes):
    """الطالب يعدّل طلبه قبل ما تصدر الكلية الخطاب فقط (بانتظار الإصدار). يسجّل وقت التعديل عشان يوصل تنبيه للموظف
    ونكشف التعارض لو الموظف كان فاتح نموذج الإصدار. يرجّع True أو None لو الطلب مو للطالب أو تغيّرت حالته."""
    def updater(row):
        if row[7] != STATUS_AWAITING or row[1].strip().lower() != student_email.strip().lower():
            return None
        row[4] = company_name
        row[5] = company_email
        row[6] = position_title
        row[16] = notes
        row[25] = student_email
        row[26] = datetime.now().strftime("%Y-%m-%d %H:%M")
        return True
    return _update_training_row(req_id, updater)


def _update_training_row(req_id, updater):
    """يقرأ الملف كاملًا ويطبّق updater على صف الطلب المطلوب فقط ثم يحفظ.
    updater يستقبل الصف (بعد تعبئته للطول الكامل) ويعدّله، ويرجّع نتيجة العملية.
    يرجّع None لو ما لقى الطلب."""
    if not os.path.exists(TRAINING_FILE):
        return None
    with open(TRAINING_FILE, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    result = None
    for i, row in enumerate(rows):
        if i == 0 or not row or row[0] != str(req_id):
            continue
        row = _pad_training_row(row)
        rows[i] = row
        result = updater(row)
        break
    if result is not None:
        with open(TRAINING_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
    return result


def issue_training_letter(req_id, company_name, company_email, position_title, issued_by, documents=None, expected_edit_at=None):
    """يحوّل طلب الطالب من "بانتظار إصدار الخطاب" إلى "قيد المراجعة عند المنشأة"،
    ويولّد رمز الوصول للمنشأة. يرجّع {"token": ...} أو None لو الطلب ما عاد متاح،
    أو {"conflict": True} لو الطالب عدّل طلبه بعد ما فتح الموظف نموذج الإصدار (expected_edit_at = آخر تعديل شافه الموظف)."""
    def updater(row):
        if row[7] != STATUS_AWAITING:
            return None
        if expected_edit_at is not None and (row[26] or "") != expected_edit_at:
            return {"conflict": True}
        now = datetime.now()
        token = secrets.token_urlsafe(24)
        row[4] = company_name
        row[5] = company_email
        row[6] = position_title
        row[7] = STATUS_COMPANY_PENDING
        row[8] = token
        row[9] = (now + timedelta(days=TRAINING_LINK_VALID_DAYS)).strftime("%Y-%m-%d %H:%M")
        row[10] = now.strftime("%Y-%m-%d %H:%M")
        row[14] = issued_by
        row[15] = ",".join(documents) if documents else ""
        row[25] = ""  # نصفّر سجل "آخر تعديل" (كان تعديل الطالب قبل الإصدار) عشان ما يظهر كأنه تعديل موظف
        row[26] = ""
        return {"token": token}
    return _update_training_row(req_id, updater)


def reject_student_training_request(req_id, reason):
    """الكلية ترفض طلب الطالب قبل إصدار الخطاب، مع سبب يظهر للطالب."""
    def updater(row):
        if row[7] != STATUS_AWAITING:
            return None
        row[7] = STATUS_COLLEGE_REJECTED
        row[13] = datetime.now().strftime("%Y-%m-%d %H:%M")
        row[17] = reason
        return True
    return _update_training_row(req_id, updater)


def is_training_link_expired(row):
    """يتحقق هل انتهت صلاحية رابط المنشأة لهذا الطلب."""
    expires_at = row[9]
    if not expires_at:
        return False
    try:
        expiry = datetime.strptime(expires_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return datetime.now() > expiry


def get_training_request_by_token(token):
    """يرجع صف طلب التدريب المطابق للرمز، أو None لو ما وجد."""
    if not token or not os.path.exists(TRAINING_FILE):
        return None  # الطلبات اللي ما صدر لها خطاب بعد رمزها فاضي، ما نطابقها أبدًا
    with open(TRAINING_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            row = _pad_training_row(row)
            if row[8] == token:
                return row
    return None


def decide_training_request(token, decision, supervisor_name="", start_date="", end_date=""):
    """يسجّل قرار المنشأة (قبول/رفض) عبر رابطها الآمن. يرفض العملية لو الرمز منتهي
    أو الطلب تم البت فيه مسبقًا، عشان ما ينفتح نفس الرابط لتغيير قرار قديم."""
    if not os.path.exists(TRAINING_FILE):
        return "notfound"

    with open(TRAINING_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    result = "notfound"
    updated = False
    for row in rows[1:]:
        if not row or row[8] != token:
            continue
        row = _pad_training_row(row)

        if row[7] != "قيد المراجعة عند المنشأة":
            result = "already_decided"
            continue
        if is_training_link_expired(row):
            result = "expired"
            continue

        row[7] = "تم القبول" if decision == "accept" else "مرفوض"
        row[11] = supervisor_name
        row[12] = start_date
        row[13] = datetime.now().strftime("%Y-%m-%d %H:%M")
        row[18] = end_date
        updated = True
        result = "ok"

    if updated:
        with open(TRAINING_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return result


def _valid_date(value):
    """تاريخ بصيغة YYYY-MM-DD أو فاضي."""
    if not value:
        return True
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def update_training_details(req_id, editor_email, company_name, company_email, position_title,
                            supervisor_name=None, start_date=None, end_date=None):
    """الموظف يصحّح بيانات طلب صدر له خطاب (غلط من الطالب أو من الجهة). بيانات الجهة تتعدّل بكل الحالات القابلة
    للتعديل، والمشرف والتواريخ بس لو الجهة قبلت الطالب. يسجّل مين عدّل ومتى. يرجّع True أو None لو الحالة ما تسمح."""
    def updater(row):
        if row[7] not in EDITABLE_TRAINING_STATUSES:
            return None
        row[4] = company_name
        row[5] = company_email
        row[6] = position_title
        if row[7] == "تم القبول" and supervisor_name is not None:
            row[11] = supervisor_name
            row[12] = start_date or ""
            row[18] = end_date or ""
        row[25] = editor_email
        row[26] = datetime.now().strftime("%Y-%m-%d %H:%M")
        return True
    return _update_training_row(req_id, updater)


def reopen_training_request(req_id, editor_email):
    """يولّد رابطًا جديدًا للمنشأة ويلغي القديم.
    - الطلب قيد المراجعة عند المنشأة (رابط منتهي أو انرسل لجهة غلط): تجديد الرابط.
    - الجهة قبلت/رفضت بالغلط: إعادة فتح القرار (يرجع "قيد المراجعة عند المنشأة" وتُمسح بيانات القرار السابق).
      ما تنفع لو الطالب رفع شهادة إتمام، لأن القرار صار مبني عليه شي بعده.
    يرجّع {"token", "kind"} أو {"error": "completion"} أو None لو الحالة ما تسمح."""
    def updater(row):
        status = row[7]
        if status not in EDITABLE_TRAINING_STATUSES:
            return None
        if status != STATUS_COMPANY_PENDING and (row[19] or row[20] or row[24]):
            return {"error": "completion"}
        now = datetime.now()
        kind = "renewed" if status == STATUS_COMPANY_PENDING else "reopened"
        token = secrets.token_urlsafe(24)
        row[7] = STATUS_COMPANY_PENDING
        row[8] = token
        row[9] = (now + timedelta(days=TRAINING_LINK_VALID_DAYS)).strftime("%Y-%m-%d %H:%M")
        if kind == "reopened":
            row[11] = ""   # supervisor_name
            row[12] = ""   # start_date
            row[13] = ""   # decided_at
            row[18] = ""   # end_date
        row[25] = editor_email
        row[26] = now.strftime("%Y-%m-%d %H:%M")
        return {"token": token, "kind": kind}
    return _update_training_row(req_id, updater)


def update_ticket_status(ticket_id, status, handler_email=""):
    """يغيّر حالة بلاغ معيّن (يستخدمها فريق الدعم الفني). لا يمكن تغيير بلاغ ملغى.
    البلاغ المحلول يُقفل نهائيًا على الجميع، حتى الموظف اللي حلّه بنفسه —
    ما فيه رجوع له إلا لو انفتح بطريقة ثانية (مستقبلًا لو احتجتوها).
    يسجّل أي موظف فني هو من قام بالتحديث، عشان الإنجاز يُنسب للشخص الصحيح فقط."""
    if status not in TICKET_STATUSES:
        return "invalid"

    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    result = "notfound"
    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if not row or row[0] != str(ticket_id):
            continue

        while len(row) < 9:
            row.append("")  # صف قديم بدون عمود resolved_at / handled_by

        if row[4] == "ملغى":
            result = "cancelled"
            continue

        if row[4] == "تم حل الطلب":
            result = "locked"  # بلاغ محلول — مقفل نهائيًا على الجميع
            continue

        row[4] = status
        if status == "تم حل الطلب":
            row[7] = datetime.now().strftime("%Y-%m-%d %H:%M")
            row[8] = handler_email
        elif status == "جاري العمل عليه":
            row[7] = ""
            row[8] = handler_email
        else:  # قيد التنفيذ — البلاغ يرجع لقائمة الانتظار العامة بدون موظف مسؤول عنه
            row[7] = ""
            row[8] = ""
        updated = True
        result = "ok"

    if updated:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return result


def get_ticket_by_id(ticket_id, email):
    """يجيب بلاغ معيّن، فقط إذا كان يخص نفس المستخدم."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if row and row[0] == str(ticket_id) and row[1] == email:
                while len(row) < 7:
                    row.append("0")  # صف قديم بدون عمود edited
                while len(row) < 8:
                    row.append("")  # صف قديم بدون عمود resolved_at
                while len(row) < 9:
                    row.append("")  # صف قديم بدون عمود handled_by
                return row
    return None


def update_ticket(ticket_id, email, title, description):
    """يحدّث عنوان ووصف بلاغ معيّن ويعلّمه كمعدّل، فقط إذا كان يخص نفس المستخدم."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[0] == str(ticket_id) and row[1] == email:
            row[2] = title
            row[3] = description
            while len(row) < 7:
                row.append("0")
            while len(row) < 8:
                row.append("")
            while len(row) < 9:
                row.append("")
            row[6] = "1"
            updated = True

    if updated:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


def get_student_ticket_summary(email):
    """يرجع (إجمالي البلاغات الظاهرة، عدد البلاغات المفتوحة، عدد البلاغات المحلولة) لطالب معيّن."""
    tickets = get_tickets_by_email(email)
    open_count = sum(1 for t in tickets if t[4] in ("قيد التنفيذ", "جاري العمل عليه"))
    resolved_count = sum(1 for t in tickets if t[4] == "تم حل الطلب")
    return len(tickets), open_count, resolved_count


@app.route("/dashboard")
def dashboard():
    guard = require_student()
    if guard:
        return guard
    total_tickets, open_tickets, resolved_tickets = get_student_ticket_summary(session.get("email", ""))
    return render_template(
        "dashboard.html",
        user=session["user"],
        email=session.get("email", ""),
        total_tickets=total_tickets,
        open_tickets=open_tickets,
        resolved_tickets=resolved_tickets,
        support_hours=get_support_contact()["hours"],
        next_date=get_next_student_date(session.get("email", "")),
    )


@app.route("/about-college")
def about_college():
    guard = require_student()
    if guard:
        return guard
    return render_template("about_college.html", user=session["user"])


@app.route("/intro-meeting")
def intro_meeting():
    guard = require_student()
    if guard:
        return guard
    return render_template("intro_meeting.html", user=session["user"])


@app.route("/support")
def support():
    guard = require_student()
    if guard:
        return guard
    contact = get_support_contact()
    return render_template(
        "support.html",
        user=session["user"],
        contact=contact,
        wa_link=whatsapp_link(contact["whatsapp"]),
        phone_href=phone_href(contact["phone"]),
        has_contact=any(contact[k] for k in ("phone", "whatsapp", "email", "location")),
    )


# ===================== المواعيد الدراسية =====================
DATE_KINDS = {
    "exam":  {"label": "اختبار",       "icon": "📝"},
    "work":  {"label": "تسليم / مشروع", "icon": "📚"},
    "admin": {"label": "موعد إداري",   "icon": "🗓️"},
    "other": {"label": "أخرى",         "icon": "⭐"},
}
MAX_STUDENT_DATES = 100  # أقصى عدد مواعيد لكل طالب
WEEKDAYS_AR = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]  # ترتيب weekday() في بايثون


def _read_all_dates():
    rows = []
    with open(DATES_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            while len(row) < len(DATE_COLUMNS):
                row.append("")
            rows.append(row)
    return rows


def _write_all_dates(rows):
    with open(DATES_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(DATE_COLUMNS)
        writer.writerows(rows)


def _next_date_id(rows):
    max_id = 0
    for r in rows:
        try:
            max_id = max(max_id, int(r[0]))
        except (ValueError, IndexError):
            continue
    return max_id + 1


def _format_time_ar(value):
    """13:30 -> 1:30 م"""
    if not value:
        return ""
    try:
        t = datetime.strptime(value, "%H:%M")
    except ValueError:
        return ""
    suffix = "ص" if t.hour < 12 else "م"
    hour = t.hour % 12 or 12
    return f"{hour}:{t.minute:02d} {suffix}"


def _countdown_ar(days_left):
    if days_left == 0:
        return "اليوم"
    if days_left == 1:
        return "غدًا"
    if days_left == 2:
        return "بعد يومين"
    if 3 <= days_left <= 10:
        return f"بعد {days_left} أيام"
    if days_left > 10:
        return f"بعد {days_left} يومًا"
    ago = -days_left
    if ago == 1:
        return "أمس"
    if ago == 2:
        return "قبل يومين"
    if 3 <= ago <= 10:
        return f"قبل {ago} أيام"
    return f"قبل {ago} يومًا"


def _row_to_date_item(row, today):
    """يحوّل صف CSV إلى قاموس جاهز للعرض (مع العد التنازلي والحالة)."""
    try:
        d = datetime.strptime(row[5], "%Y-%m-%d").date()
    except ValueError:
        return None
    kind = row[3] if row[3] in DATE_KINDS else "other"
    done = row[8] == "1"
    days_left = (d - today).days
    if done:
        state = "done"
    elif days_left < 0:
        state = "overdue"
    elif days_left == 0:
        state = "today"
    elif days_left <= 3:
        state = "soon"
    else:
        state = "later"
    return {
        "id": int(row[0]), "title": row[2], "kind": kind,
        "kind_label": DATE_KINDS[kind]["label"], "icon": DATE_KINDS[kind]["icon"],
        "course": row[4], "date": row[5], "time": row[6], "time_label": _format_time_ar(row[6]),
        "note": row[7], "done": done, "days_left": days_left,
        "countdown": _countdown_ar(days_left), "state": state,
        "weekday": WEEKDAYS_AR[d.weekday()], "day": d.day,
        "month": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو",
                  "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"][d.month - 1],
        "sort_key": (row[5], row[6] or "99:99"),
    }


def get_student_dates(email):
    """كل مواعيد الطالب كقواميس جاهزة للعرض."""
    today = date.today()
    items = []
    for row in _read_all_dates():
        if row[1] != email:
            continue
        item = _row_to_date_item(row, today)
        if item:
            items.append(item)
    return items


def get_student_date(date_id, email):
    for row in _read_all_dates():
        if row[0] == str(date_id) and row[1] == email:
            return row
    return None


def build_week_strip(items):
    """أيام الأسبوع الحالي (أحد → سبت) ومعها مواعيد كل يوم."""
    today = date.today()
    start = today - timedelta(days=(today.weekday() + 1) % 7)  # آخر أحد
    strip = []
    for i in range(7):
        d = start + timedelta(days=i)
        iso = d.strftime("%Y-%m-%d")
        strip.append({
            "name": WEEKDAYS_AR[d.weekday()], "day": d.day, "is_today": d == today,
            "events": [x for x in items if x["date"] == iso and not x["done"]],
        })
    return strip


def get_next_student_date(email):
    """أقرب موعد قادم غير منتهي (لعرضه على لوحة الطالب)."""
    upcoming = [x for x in get_student_dates(email) if not x["done"] and x["days_left"] >= 0]
    upcoming.sort(key=lambda x: x["sort_key"])
    return upcoming[0] if upcoming else None


def _validate_date_form(form):
    """يرجّع (قيم منظّفة، رسالة خطأ أو None)."""
    title = form.get("title", "").strip()
    kind = form.get("kind", "exam")
    course = form.get("course", "").strip()
    date_val = form.get("date", "").strip()
    time_val = form.get("time", "").strip()
    note = form.get("note", "").strip()
    values = {"title": title, "kind": kind, "course": course, "date": date_val, "time": time_val, "note": note}
    if not title:
        return values, "اكتب عنوان الموعد"
    if len(title) > 80 or len(course) > 50 or len(note) > 200:
        return values, "النص طويل، اختصر العنوان أو المادة أو الملاحظة"
    if kind not in DATE_KINDS:
        return values, "نوع الموعد غير صحيح"
    if not date_val or not _valid_date(date_val):
        return values, "اختر تاريخًا صحيحًا"
    if time_val:
        try:
            datetime.strptime(time_val, "%H:%M")
        except ValueError:
            return values, "الوقت غير صحيح"
    return values, None


@app.route("/my-dates", methods=["GET", "POST"])
def my_dates():
    guard = require_student()
    if guard:
        return guard
    email = session["email"]
    form_values = {"title": "", "kind": "exam", "course": "", "date": "", "time": "", "note": ""}

    if request.method == "POST":
        form_values, error = _validate_date_form(request.form)
        if not error:
            rows = _read_all_dates()
            if sum(1 for r in rows if r[1] == email) >= MAX_STUDENT_DATES:
                error = f"وصلت للحد الأقصى ({MAX_STUDENT_DATES} موعد)، احذف مواعيد قديمة أولًا"
        if error:
            flash(error)
        else:
            rows.append([
                _next_date_id(rows), email, form_values["title"], form_values["kind"],
                form_values["course"], form_values["date"], form_values["time"],
                form_values["note"], "0", datetime.now().strftime("%Y-%m-%d %H:%M"),
            ])
            _write_all_dates(rows)
            flash("تمت إضافة الموعد", "success")
            return redirect(url_for("my_dates"))

    items = get_student_dates(email)
    upcoming = sorted([x for x in items if not x["done"] and x["days_left"] >= 0], key=lambda x: x["sort_key"])
    past = sorted([x for x in items if x["done"] or x["days_left"] < 0], key=lambda x: x["sort_key"], reverse=True)
    return render_template(
        "my_dates.html", user=session["user"], kinds=DATE_KINDS,
        upcoming=upcoming, past=past, week=build_week_strip(items),
        next_item=upcoming[0] if upcoming else None,
        exam_count=sum(1 for x in upcoming if x["kind"] == "exam"),
        today_iso=date.today().strftime("%Y-%m-%d"),
        editing=None, form=form_values,
    )


@app.route("/my-dates/<int:date_id>/edit", methods=["GET", "POST"])
def edit_my_date(date_id):
    guard = require_student()
    if guard:
        return guard
    email = session["email"]
    row = get_student_date(date_id, email)
    if not row:
        flash("الموعد غير موجود")
        return redirect(url_for("my_dates"))

    form_values = {"title": row[2], "kind": row[3], "course": row[4], "date": row[5], "time": row[6], "note": row[7]}
    if request.method == "POST":
        form_values, error = _validate_date_form(request.form)
        if error:
            flash(error)
        else:
            rows = _read_all_dates()
            for r in rows:
                if r[0] == str(date_id) and r[1] == email:
                    r[2], r[3], r[4] = form_values["title"], form_values["kind"], form_values["course"]
                    r[5], r[6], r[7] = form_values["date"], form_values["time"], form_values["note"]
            _write_all_dates(rows)
            flash("تم حفظ التعديل", "success")
            return redirect(url_for("my_dates"))

    items = get_student_dates(email)
    upcoming = sorted([x for x in items if not x["done"] and x["days_left"] >= 0], key=lambda x: x["sort_key"])
    past = sorted([x for x in items if x["done"] or x["days_left"] < 0], key=lambda x: x["sort_key"], reverse=True)
    return render_template(
        "my_dates.html", user=session["user"], kinds=DATE_KINDS,
        upcoming=upcoming, past=past, week=build_week_strip(items),
        next_item=upcoming[0] if upcoming else None,
        exam_count=sum(1 for x in upcoming if x["kind"] == "exam"),
        today_iso=date.today().strftime("%Y-%m-%d"),
        editing=date_id, form=form_values,
    )


@app.route("/my-dates/<int:date_id>/done", methods=["POST"])
def toggle_my_date_done(date_id):
    guard = require_student()
    if guard:
        return guard
    rows = _read_all_dates()
    for r in rows:
        if r[0] == str(date_id) and r[1] == session["email"]:
            r[8] = "0" if r[8] == "1" else "1"
    _write_all_dates(rows)
    return redirect(url_for("my_dates"))


@app.route("/my-dates/<int:date_id>/delete", methods=["POST"])
def delete_my_date(date_id):
    guard = require_student()
    if guard:
        return guard
    rows = _read_all_dates()
    kept = [r for r in rows if not (r[0] == str(date_id) and r[1] == session["email"])]
    if len(kept) != len(rows):
        _write_all_dates(kept)
        flash("تم حذف الموعد", "success")
    return redirect(url_for("my_dates"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user" not in session:
        return redirect(url_for("login"))

    user_data = get_user_by_email(session.get("email", ""))
    if not user_data:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        birthdate = request.form.get("birthdate", "").strip()

        if not first_name or not birthdate:
            flash("الرجاء تعبئة الاسم الأول وتاريخ الميلاد")
            return redirect(url_for("profile"))

        try:
            birth_date_obj = datetime.strptime(birthdate, "%Y-%m-%d").date()
        except ValueError:
            flash("تاريخ الميلاد غير صحيح")
            return redirect(url_for("profile"))

        if birth_date_obj > date.today():
            flash("تاريخ الميلاد غير صحيح")
            return redirect(url_for("profile"))

        new_full_name = f"{first_name} {last_name}".strip() if last_name else first_name
        update_user_profile(user_data["email"], new_full_name, birthdate)
        session["user"] = new_full_name  # عشان الاسم يتحدث فورًا بالقائمة العلوية
        flash("تم حفظ التعديلات بنجاح", "success")
        return redirect(url_for("profile"))

    full_name = user_data["full_name"].strip()
    parts = full_name.split(" ", 1)
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    return render_template(
        "profile.html",
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        email=user_data["email"],
        birthdate=user_data["birthdate"],
        last_updated=user_data.get("last_updated", ""),
        today=date.today().isoformat(),
        back_url=url_for("it_dashboard") if session.get("role") == "it" else url_for("dashboard"),
    )


@app.route("/profile/avatar", methods=["POST"])
def upload_avatar():
    if "user" not in session:
        return redirect(url_for("login"))

    email = session.get("email", "")
    file = request.files.get("avatar")

    if not file or file.filename == "":
        flash("الرجاء اختيار صورة")
        return redirect(url_for("profile"))

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_AVATAR_EXT:
        flash("صيغة الصورة غير مدعومة (png, jpg, jpeg, webp, gif)")
        return redirect(url_for("profile"))

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_AVATAR_SIZE:
        flash("حجم الصورة يتجاوز 3 ميجابايت")
        return redirect(url_for("profile"))

    os.makedirs(AVATAR_DIR, exist_ok=True)
    delete_avatar(email)  # يحذف أي صورة قديمة بصيغة مختلفة قبل حفظ الجديدة

    filename = f"{avatar_key(email)}.{ext}"
    file.save(os.path.join(AVATAR_DIR, filename))

    flash("تم تحديث الصورة الشخصية بنجاح", "success")
    return redirect(url_for("profile"))


@app.route("/profile/avatar/delete", methods=["POST"])
def delete_avatar_route():
    if "user" not in session:
        return redirect(url_for("login"))

    delete_avatar(session.get("email", ""))
    flash("تم إزالة الصورة الشخصية", "success")
    return redirect(url_for("profile"))


@app.route("/new-ticket", methods=["GET", "POST"])
def new_ticket():
    guard = require_student()
    if guard:
        return guard

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if not title or not description:
            flash("الرجاء تعبئة عنوان ووصف المشكلة")
            return redirect(url_for("new_ticket"))

        save_ticket(session["email"], title, description)
        flash("تم إرسال البلاغ بنجاح", "success")
        return redirect(url_for("my_tickets"))

    return render_template("new_ticket.html")


@app.route("/tickets/<int:ticket_id>/edit", methods=["GET", "POST"])
def edit_ticket(ticket_id):
    guard = require_student()
    if guard:
        return guard

    ticket = get_ticket_by_id(ticket_id, session["email"])
    if not ticket:
        flash("البلاغ غير موجود")
        return redirect(url_for("my_tickets"))

    if ticket[4] == "ملغى":
        flash("لا يمكن تعديل بلاغ ملغى")
        return redirect(url_for("my_tickets"))

    if ticket[4] == "تم حل الطلب":
        flash("لا يمكن تعديل بلاغ تم حله")
        return redirect(url_for("my_tickets"))

    if ticket[6] == "1":
        flash("لا يمكن تعديل البلاغ أكثر من مرة واحدة")
        return redirect(url_for("my_tickets"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if not title or not description:
            flash("الرجاء تعبئة عنوان ووصف المشكلة")
            return redirect(url_for("edit_ticket", ticket_id=ticket_id))

        update_ticket(ticket_id, session["email"], title, description)
        flash("تم تعديل البلاغ بنجاح", "success")
        return redirect(url_for("my_tickets"))

    return render_template("edit_ticket.html", ticket=ticket)


@app.route("/my-tickets")
def my_tickets():
    guard = require_student()
    if guard:
        return guard

    tickets = get_tickets_by_email(session["email"])
    return render_template("my_tickets.html", tickets=tickets)


@app.route("/tickets/<int:ticket_id>/cancel", methods=["POST"])
def cancel_ticket_route(ticket_id):
    guard = require_student()
    if guard:
        return guard

    ticket = get_ticket_by_id(ticket_id, session["email"])
    if ticket and ticket[4] == "تم حل الطلب":
        flash("لا يمكن إلغاء بلاغ تم حله")
        return redirect(url_for("my_tickets"))

    cancel_ticket(ticket_id, session["email"])
    flash("تم إلغاء البلاغ", "success")
    return redirect(url_for("my_tickets"))


@app.route("/logout")
def logout():
    session.pop("user", None)
    session.pop("email", None)
    session.pop("role", None)
    return redirect(url_for("login"))


if __name__ == "__main__":
    init_file()
    app.run(debug=True)
