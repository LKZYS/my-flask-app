import csv
import os
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash

app = Flask(__name__)
app.secret_key = "change-this-secret-key"  # مهم تغيّرها لاحقًا

IT_STAFF_CODE = "IT-2026"  # الرمز السري لإنشاء حساب فريق الدعم الفني — غيّره لرمز خاص فيك

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(BASE_DIR, "users.csv")
TICKETS_FILE = os.path.join(BASE_DIR, "tickets.csv")


def init_file():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["full_name", "birthdate", "email", "password", "role", "training_number", "last_updated"])  # صف العناوين

    if not os.path.exists(TICKETS_FILE):
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "email", "title", "description", "status", "created_at", "edited", "resolved_at"])  # صف العناوين


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


@app.route("/")
def home():
    if "user" in session:
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


def get_ticket_stats():
    """يرجع (عدد البلاغات المحلولة هالشهر، عدد البلاغات الجارية اللي فُتحت هالشهر، إجمالي البلاغات المفتوحة حاليًا بغض النظر عن الشهر).
    الإحصائيات الشهرية ترتست تلقائيًا أول كل شهر ميلادي جديد."""
    now = datetime.now()
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    resolved = 0
    ongoing = 0
    open_queue = 0
    for row in rows:
        while len(row) < 8:
            row.append("")
        status = row[4]
        if status == "تم حل الطلب" and row[7]:
            try:
                resolved_time = datetime.strptime(row[7], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if resolved_time.year == now.year and resolved_time.month == now.month:
                resolved += 1
        elif status in ("قيد التنفيذ", "جاري العمل عليه"):
            open_queue += 1
            try:
                created_time = datetime.strptime(row[5], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if created_time.year == now.year and created_time.month == now.month:
                ongoing += 1

    return resolved, ongoing, open_queue


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
    resolved_count, ongoing_count, open_queue = get_ticket_stats()
    student_count = get_student_count()
    return render_template(
        "it_dashboard.html",
        user=session["user"],
        resolved_count=resolved_count,
        ongoing_count=ongoing_count,
        open_queue=open_queue,
        student_count=student_count,
    )


@app.route("/it-tickets")
def it_tickets():
    if session.get("role") != "it":
        return redirect(url_for("it_login"))
    students = get_all_tickets()
    return render_template("it_tickets.html", user=session["user"], students=students, statuses=TICKET_STATUSES)


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
def update_ticket_status_route(ticket_id):
    if session.get("role") != "it":
        return redirect(url_for("it_login"))

    status = request.form.get("status", "")
    update_ticket_status(ticket_id, status)
    return redirect(url_for("it_tickets"))


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


def save_ticket(email, title, description):
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    next_id = 1000 + len(rows)  # يبدأ الترقيم من #1001 ليبدو كرقم بلاغ حقيقي

    with open(TICKETS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            next_id, email, title, description, "قيد التنفيذ",
            datetime.now().strftime("%Y-%m-%d %H:%M"), "0", ""
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
    tickets = [row for row in tickets if is_ticket_visible(row)]
    tickets.reverse()  # الأحدث أولًا
    return tickets


def get_all_tickets():
    """يجيب كل البلاغات من جميع الطلاب مجمّعة حسب كل طالب (اسمه، رقمه التدريبي، وبلاغاته)."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)
        rows = [row for row in reader if row]

    for row in rows:
        while len(row) < 7:
            row.append("0")  # صف قديم بدون عمود edited
        while len(row) < 8:
            row.append("")  # صف قديم بدون عمود resolved_at

    rows = [row for row in rows if is_ticket_visible(row)]
    rows.reverse()  # الأحدث أولًا

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


def cancel_ticket(ticket_id, email):
    """يغيّر حالة بلاغ معيّن إلى (ملغى)، فقط إذا كان يخص نفس المستخدم."""
    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[0] == str(ticket_id) and row[1] == email:
            row[4] = "ملغى"
            updated = True

    if updated:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


TICKET_STATUSES = ["قيد التنفيذ", "جاري العمل عليه", "تم حل الطلب"]
RESOLVED_HIDE_AFTER = timedelta(days=30)  # مدة بقاء البلاغ المحلول ظاهرًا قبل ما يختفي من القوائم (البيانات تبقى محفوظة بالملف)


def is_ticket_visible(row):
    """يرجع False إذا كان البلاغ (تم حل الطلب) ومرّ على حله أكثر من RESOLVED_HIDE_AFTER.
    البلاغ يبقى محفوظًا بالملف دائمًا، بس نخفيه من العرض فقط."""
    if row[4] != "تم حل الطلب":
        return True
    resolved_at = row[7] if len(row) > 7 else ""
    if not resolved_at:
        return True
    try:
        resolved_time = datetime.strptime(resolved_at, "%Y-%m-%d %H:%M")
    except ValueError:
        return True
    return datetime.now() - resolved_time <= RESOLVED_HIDE_AFTER


def update_ticket_status(ticket_id, status):
    """يغيّر حالة بلاغ معيّن (يستخدمها فريق الدعم الفني). لا يمكن تغيير بلاغ ملغى."""
    if status not in TICKET_STATUSES:
        return False

    with open(TICKETS_FILE, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    updated = False
    for row in rows[1:]:  # تخطي صف العناوين
        if row and row[0] == str(ticket_id) and row[4] != "ملغى":
            while len(row) < 8:
                row.append("")  # صف قديم بدون عمود resolved_at
            row[4] = status
            if status == "تم حل الطلب":
                row[7] = datetime.now().strftime("%Y-%m-%d %H:%M")
            else:
                row[7] = ""
            updated = True

    if updated:
        with open(TICKETS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

    return updated


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
    if "user" not in session:
        return redirect(url_for("login"))
    total_tickets, open_tickets, resolved_tickets = get_student_ticket_summary(session.get("email", ""))
    return render_template(
        "dashboard.html",
        user=session["user"],
        email=session.get("email", ""),
        total_tickets=total_tickets,
        open_tickets=open_tickets,
        resolved_tickets=resolved_tickets,
    )


@app.route("/about-college")
def about_college():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("about_college.html", user=session["user"])


@app.route("/intro-meeting")
def intro_meeting():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("intro_meeting.html", user=session["user"])


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


@app.route("/new-ticket", methods=["GET", "POST"])
def new_ticket():
    if "user" not in session:
        return redirect(url_for("login"))

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
    if "user" not in session:
        return redirect(url_for("login"))

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
    if "user" not in session:
        return redirect(url_for("login"))

    tickets = get_tickets_by_email(session["email"])
    return render_template("my_tickets.html", tickets=tickets)


@app.route("/tickets/<int:ticket_id>/cancel", methods=["POST"])
def cancel_ticket_route(ticket_id):
    if "user" not in session:
        return redirect(url_for("login"))

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
