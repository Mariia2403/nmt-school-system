"""
Views (page logic) of the school website.
Every page is only for logged-in users with a role (see access.py):
@staff_page  - administrator and teachers (a teacher sees only his own data),
@admin_page  - only the administrator (money and debts).
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Avg, Count, DecimalField, Exists, F, Func, OuterRef, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import EnrollmentForm, StudentForm
from .access import (
    ADMIN_GROUP, LoginError, admin_page, check_password, create_admin_login, create_teacher_login,
    get_teacher, groups_for, is_admin, lessons_for, staff_page, students_for, subjects_for, suggest_username,
)
from .models import (
    Attendance, Contract, ContractPause, Expense, Invoice, Lesson, Payment, Refund, Salary, Student, StudyGroup, Subject,
    Tariff, Teacher, Test, TestResult, months_between, next_month,
)

ZERO = Value(Decimal("0"))
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


# ───────────────────────── Helpers ─────────────────────────

def invoices_with_paid():
    """Invoices with an extra field paid_sum (how much was paid), calculated in SQL."""
    return (
        Invoice.objects
        .select_related("contract__student", "contract__group__subject")
        .annotate(paid_sum=Coalesce(Sum("payments__amount"), ZERO, output_field=MONEY_FIELD))
    )


def overdue_limit(today):
    """The last due date that is already "overdue".
    Example with 7 days: today is 26.10 -> invoices with due date before 19.10 are overdue.
    The number of days is in settings.py (DEBT_OVERDUE_DAYS)."""
    return today - timedelta(days=settings.DEBT_OVERDUE_DAYS)


def is_overdue(invoice, today):
    """One rule for one invoice: there is a debt AND more than N days passed after the due date.
    Works for invoices from invoices_with_paid() (they have paid_sum)."""
    return invoice.paid_sum < invoice.amount and invoice.due_date < overdue_limit(today)


def overdue_invoices(today):
    """All overdue invoices. Every invoice gets debt_value (how much is not paid)."""
    result = []
    # The filter in SQL makes the list short, is_overdue() checks the payments
    for inv in invoices_with_paid().filter(due_date__lt=overdue_limit(today)):
        if is_overdue(inv, today):
            inv.debt_value = inv.amount - inv.paid_sum
            result.append(inv)
    return result


def debt_by_student(today):
    """Overdue debt of every student: {student: amount}.
    We use this function everywhere, so the debt is always calculated the same way."""
    debts = {}
    for inv in overdue_invoices(today):
        student = inv.contract.student
        debts[student] = debts.get(student, Decimal("0")) + inv.debt_value
    return debts


def no_access_ids(today):
    """IDs of students who must NOT come to lessons ("Без допуску"):
    they have an unpaid invoice (any group) that is more than ACCESS_BLOCK_DAYS days late.
    One SQL query. Used by the Journal, the group page, the student card and lists."""
    limit = today - timedelta(days=settings.ACCESS_BLOCK_DAYS)
    return set(
        Invoice.objects
        .annotate(paid_sum=Coalesce(Sum("payments__amount"), ZERO, output_field=MONEY_FIELD))
        .filter(due_date__lt=limit, paid_sum__lt=F("amount"))
        .values_list("contract__student_id", flat=True)
    )


def debtors_to_call(today):
    """Students with an overdue debt, with everything the administrator needs for a call.
    The longest overdue goes first: these parents must be called first."""
    rows = {}
    for inv in overdue_invoices(today):
        student = inv.contract.student
        row = rows.setdefault(student.pk, {
            "student": student,
            "debt": Decimal("0"),
            "oldest_due": inv.due_date,
            "invoices": 0,
            "groups": set(),
        })
        row["debt"] += inv.debt_value
        row["oldest_due"] = min(row["oldest_due"], inv.due_date)
        row["invoices"] += 1
        row["groups"].add(inv.contract.group.full_name)

    result = []
    for row in rows.values():
        row["days_overdue"] = (today - row["oldest_due"]).days
        row["groups"] = sorted(row["groups"])
        result.append(row)
    result.sort(key=lambda r: (r["days_overdue"], r["debt"]), reverse=True)
    return result


# ───────────────────────── Dashboard ─────────────────────────

@staff_page
def dashboard(request):
    # A teacher has his own main page: his lessons and groups, no money
    if not is_admin(request.user):
        return teacher_dashboard(request, get_teacher(request.user))

    today = timezone.localdate()
    now = timezone.now()

    # Groups that are studying now (started and not finished yet)
    active_groups = StudyGroup.objects.filter(start_date__lte=today).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    )

    # Money for the current month
    month_payments = Payment.objects.filter(paid_at__year=today.year, paid_at__month=today.month)
    payments_sum = month_payments.aggregate(s=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD))["s"]
    expenses_sum = Expense.objects.filter(spent_at__year=today.year, spent_at__month=today.month).aggregate(
        s=Coalesce(Sum("amount"), ZERO, output_field=MONEY_FIELD)
    )["s"]
    reserve_total = Payment.objects.aggregate(
        s=Coalesce(Sum("reserve_amount"), ZERO, output_field=MONEY_FIELD)
    )["s"]

    # Debts
    debts = debt_by_student(today)
    total_debt = sum(debts.values(), Decimal("0"))
    top_debtors = sorted(debts.items(), key=lambda item: item[1], reverse=True)[:5]

    # Lessons for today: with marks, to show "не відмічено" for lessons that already started
    today_lessons = mark_lesson_state(list(with_marks(
        Lesson.objects.filter(starts_at__date=today).select_related("group__subject", "teacher")
    ).order_by("starts_at")), now)
    # All teachers: lessons of the last 30 days without marks (the admin reminds the teachers)
    unmarked = unmarked_lessons(Lesson.objects.all(), now)
    next_lessons = (
        Lesson.objects.filter(starts_at__gt=now, status=Lesson.Status.PLANNED)
        .exclude(starts_at__date=today)
        .select_related("group__subject", "teacher")
        .order_by("starts_at")[:5]
    )

    context = {
        "today": today,
        "active_students": Student.objects.active().count(),
        "active_groups": active_groups.count(),
        "payments_sum": payments_sum,
        "payments_count": month_payments.count(),
        "expenses_sum": expenses_sum,
        "reserve_total": reserve_total,
        "total_debt": total_debt,
        "debtors_count": len(debts),
        "top_debtors": top_debtors,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        "today_lessons": today_lessons,
        "next_lessons": next_lessons,
        "unmarked": unmarked[:10],
        "unmarked_count": unmarked.count(),
    }
    return render(request, "school/dashboard.html", context)


def teacher_dashboard(request, teacher):
    """Main page of a teacher: lessons today, forgotten journals, his groups."""
    today = timezone.localdate()
    now = timezone.now()
    lessons = lessons_for(request.user).select_related("group__subject", "teacher")
    today_lessons = mark_lesson_state(list(with_marks(lessons.filter(starts_at__date=today)).order_by("starts_at")), now)
    unmarked = unmarked_lessons(lessons, now)
    next_lessons = (
        lessons.filter(starts_at__gt=now, status=Lesson.Status.PLANNED)
        .exclude(starts_at__date=today).order_by("starts_at")[:5]
    )
    groups = (
        groups_for(request.user).filter(pk__in=running_groups(today))
        .select_related("subject").prefetch_related("schedule")
        .annotate(taken=Count("contracts", filter=Q(contracts__status__in=Contract.OPEN_STATUSES), distinct=True))
    )
    context = {
        "today": today,
        "teacher": teacher,
        "today_lessons": today_lessons,
        "unmarked": unmarked,
        "next_lessons": next_lessons,
        "groups": groups,
    }
    return render(request, "school/dashboard_teacher.html", context)


# ───────────────────────── Students ─────────────────────────

@staff_page
def student_list(request):
    """List of students with search and filters.
    Two views: "Списком" (20 students on a page) and "За групами" (blocks by group)."""
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    grade = request.GET.get("grade", "")
    debt_filter = request.GET.get("debt", "")
    subject_id = request.GET.get("subject", "")
    group_id = request.GET.get("group", "")
    tariff_id = request.GET.get("tariff", "")
    view_mode = "groups" if request.GET.get("view") == "groups" else "list"
    is_school_admin = is_admin(request.user)

    # Open contracts = the student studies now or is on pause (both keep a place in the group)
    open_contracts = Contract.objects.filter(status__in=Contract.OPEN_STATUSES)
    if not is_school_admin:
        # A teacher sees only his groups, also inside a student row
        open_contracts = open_contracts.filter(group__in=groups_for(request.user))
    has_active = Exists(Contract.objects.filter(student=OuterRef("pk"), status=Contract.Status.ACTIVE))
    has_suspended = Exists(Contract.objects.filter(student=OuterRef("pk"), status=Contract.Status.SUSPENDED))
    students = (
        # A teacher sees only students of his groups
        Student.objects.filter(pk__in=students_for(request.user))
        .annotate(has_active=has_active, has_suspended=has_suspended)
        # Open contracts with groups and tariffs in ONE extra query (not one query per student)
        .prefetch_related(Prefetch(
            "contracts",
            queryset=open_contracts.select_related("group__subject", "tariff").order_by("group__subject__name", "group__name"),
            to_attr="open_contracts",
        ))
    )

    # Search. A phone is searched by digits only: "067-123-45-67", "(067) 123 45 67"
    # and "0671234567" find the same "+380671234567".
    digits = re.sub(r"\D", "", q)
    if q and len(digits) >= 5 and not re.search(r"[^\d\s()+\-.]", q):
        students = students.annotate(
            phone_digits=Func(F("phone"), Value(r"\D"), Value(""), Value("g"), function="regexp_replace"),
            parent_digits=Func(F("parent_phone"), Value(r"\D"), Value(""), Value("g"), function="regexp_replace"),
        ).filter(Q(phone_digits__contains=digits) | Q(parent_digits__contains=digits))
    else:
        # Every word must be found in one of the fields: "Шевченко Анна" finds Anna Shevchenko
        for word in q.split():
            students = students.filter(
                Q(last_name__icontains=word) | Q(first_name__icontains=word) | Q(phone__icontains=word)
                | Q(parent_name__icontains=word) | Q(parent_phone__icontains=word)
            )

    # Status: the same three words everywhere
    if status == "active":          # Навчається: at least one active contract
        students = students.filter(has_active=True)
    elif status == "paused":        # На паузі: only paused contracts
        students = students.filter(has_active=False, has_suspended=True)
    elif status == "inactive":      # Не навчається: no open contracts
        students = students.filter(has_active=False, has_suspended=False)
    if grade.isdigit():
        students = students.filter(grade=int(grade))

    # Subject / group / tariff: the student has an OPEN contract with it
    contract_filter = Q()
    if subject_id.isdigit():
        contract_filter &= Q(group__subject_id=int(subject_id))
    if group_id.isdigit():
        contract_filter &= Q(group_id=int(group_id))
    if tariff_id.isdigit() and is_school_admin:
        contract_filter &= Q(tariff_id=int(tariff_id))
    if contract_filter:
        students = students.filter(pk__in=open_contracts.filter(contract_filter).values("student_id"))

    # Debts are calculated once and used for the filter and for the table
    debts = debt_by_student(today)
    blocked = no_access_ids(today)
    if debt_filter == "overdue":
        students = students.filter(pk__in=[s.pk for s in debts])
    elif debt_filter == "noaccess":
        students = students.filter(pk__in=blocked)

    def add_marks(student):
        student.debt = debts.get(student, Decimal("0"))
        student.no_access = student.pk in blocked
        return student

    total_found = students.count()
    page = None
    blocks = []
    if view_mode == "groups":
        # Blocks by group. A student in two groups is shown in both blocks.
        found = {s.pk: add_marks(s) for s in students}
        rows = (
            open_contracts.filter(student_id__in=found.keys()).filter(contract_filter)
            .select_related("group__subject", "group__teacher", "tariff")
            .order_by("group__subject__name", "group__name", "student__last_name", "student__first_name")
        )
        by_group = {}
        for contract in rows:
            block = by_group.setdefault(contract.group_id, {"group": contract.group, "rows": []})
            block["rows"].append({"student": found[contract.student_id], "contract": contract})
        blocks = list(by_group.values())
        in_groups = {row["student"].pk for block in blocks for row in block["rows"]}
        without_group = [s for pk, s in found.items() if pk not in in_groups]
    else:
        page = Paginator(students, 20).get_page(request.GET.get("page"))
        for student in page:
            add_marks(student)
        without_group = []

    # Keep search and filters when the user goes to another page or changes the view
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("view", None)

    groups = groups_for(request.user).select_related("subject").order_by("subject__name", "name")
    context = {
        "page": page,
        "blocks": blocks,
        "without_group": without_group,
        "view_mode": view_mode,
        "total_found": total_found,
        "q": q,
        "status": status,
        "grade": grade,
        "debt_filter": debt_filter,
        "subject_id": subject_id,
        "group_id": group_id,
        "tariff_id": tariff_id,
        "subjects": subjects_for(request.user),
        "groups": groups,
        "tariffs": Tariff.objects.all() if is_school_admin else Tariff.objects.none(),
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        "grades": students_for(request.user).values_list("grade", flat=True).distinct().order_by("grade"),
        "querystring": params.urlencode(),
        "today": today,
    }
    return render(request, "school/student_list.html", context)


@staff_page
def student_detail(request, pk):
    today = timezone.localdate()
    admin = is_admin(request.user)
    # If there is no student with this id (or it is a student of another teacher),
    # Django shows the "404 Not found" page
    student = get_object_or_404(students_for(request.user), pk=pk)
    my_groups = groups_for(request.user)

    contracts = (
        student.contracts
        .filter(group__in=my_groups)  # a teacher sees only contracts in his groups
        .select_related("group__subject", "group__teacher", "tariff")
        # pauses and refund requests are shown under every contract
        .prefetch_related("group__schedule", "pauses", "refunds")
        .order_by("-start_date")
    )

    # Attendance: statistics for all lessons, the list shows the last 30
    attendance = (
        student.attendance.filter(lesson__group__in=my_groups)
        .select_related("lesson__group__subject").order_by("-lesson__starts_at")
    )
    stats = attendance.aggregate(
        total=Count("id"),
        present=Count("id", filter=Q(status__in=[Attendance.Status.PRESENT, Attendance.Status.LATE])),
    )
    attendance_percent = round(stats["present"] * 100 / stats["total"]) if stats["total"] else None

    # Test results
    results = (
        student.test_results.filter(test__subject__in=subjects_for(request.user))
        .select_related("test__subject", "test__group").order_by("-test__held_at")
    )
    # Average only for tests on the NMT scale (100-200).
    # If we mix them with topic tests in %, "165" and "70%" give a meaningless "117".
    avg_score = results.filter(test__kind__in=Test.NMT_SCALE_KINDS).aggregate(avg=Avg("score"))["avg"]

    # Invoices with payments inside (a teacher does not see money: the list is empty)
    invoices = [] if not admin else list(
        invoices_with_paid()
        .filter(contract__student=student)
        .prefetch_related("payments")
        .order_by("-period_month")
    )
    for inv in invoices:
        inv.debt_value = max(inv.amount - inv.paid_sum, Decimal("0"))
        # The same rule as on all other pages
        inv.is_overdue = is_overdue(inv, today)
        # A planned invoice for a future month: nobody has to pay it yet
        inv.is_future = inv.due_date > today
        # Not paid yet, but the student still has time (less than N days after the due date)
        inv.is_waiting = inv.debt_value > 0 and not inv.is_overdue and not inv.is_future
    overdue_debt = sum((inv.debt_value for inv in invoices if inv.is_overdue), Decimal("0"))
    # Only invoices whose due date has come (future months are not a debt)
    unpaid_total = sum((inv.debt_value for inv in invoices if not inv.is_future), Decimal("0"))

    context = {
        "student": student,
        "chart": nmt_chart_data(results, contracts),
        "contracts": contracts,
        "attendance": attendance[:30],
        "attendance_total": stats["total"],
        "attendance_percent": attendance_percent,
        "results": results,
        "avg_score": avg_score,
        "nmt_results_count": results.filter(test__kind__in=Test.NMT_SCALE_KINDS).count(),
        "invoices": invoices,
        "overdue_debt": overdue_debt,
        # "Без допуску": the same rule as in the Journal (settings.ACCESS_BLOCK_DAYS)
        "no_access": student.pk in no_access_ids(today),
        "access_block_days": settings.ACCESS_BLOCK_DAYS,
        "unpaid_total": unpaid_total,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        "today": today,  # default date in the pause / leave forms
    }
    return render(request, "school/student_detail.html", context)


def enrollment_summary(contract):
    """Short text about a new contract for the success message:
    "Математика · група 1 (Стандарт, 2 400 ₴/міс)"."""
    if contract.payment_plan == Contract.PaymentPlan.PREPAID:
        total = contract.invoices.aggregate(s=Sum("amount"))["s"] or Decimal("0")
        money = f"весь курс {total:,.0f} ₴".replace(",", " ")
    else:
        money = f"{contract.monthly_amount():,.0f} ₴/міс".replace(",", " ")
    return f"{contract.group.full_name} ({contract.tariff.name}, {money})"


def enrollment_prices():
    """Data for the live "До сплати" line in the form (JavaScript):
    tariff prices and group dates. Without amounts of any real person."""
    tariffs = {str(t.pk): float(t.monthly_price) for t in Tariff.objects.filter(is_active=True)}
    groups = {
        str(g.pk): {"start": g.start_date.isoformat(), "end": g.end_date.isoformat() if g.end_date else ""}
        for g in StudyGroup.objects.filter(end_date__gte=timezone.localdate())
    }
    return {"tariffs": tariffs, "groups": groups}


def enrollment_initial(group_id):
    """Start values for "Записати в групу": the group from the link and its start date."""
    today = timezone.localdate()
    initial = {"start_date": today}
    group = StudyGroup.objects.filter(pk=group_id).first() if str(group_id).isdigit() else None
    if group:
        initial["group"] = group.pk
        initial["start_date"] = max(today, group.start_date)
    return initial


@admin_page
def student_create(request):
    """Page "Новий учень": the student, the parents and 1-2 groups in ONE form.
    Before: the admin panel, the start date by hand, full and finished groups in the list."""
    group_id = request.GET.get("group", "")
    if request.method == "POST":
        student_form = StudentForm(request.POST)
        first = EnrollmentForm(request.POST, prefix="g1", student=Student())
        # The second group is not required: if nothing was changed in it, Django skips it
        second = EnrollmentForm(request.POST, prefix="g2", student=Student(), empty_permitted=True,
                                use_required_attribute=False, initial=enrollment_initial(""))
        forms_ok = all([student_form.is_valid(), first.is_valid(), second.is_valid()])
        # The same group in both blocks: show it at once, together with other errors
        first_group = first.cleaned_data.get("group") if hasattr(first, "cleaned_data") else None
        second_group = second.cleaned_data.get("group") if hasattr(second, "cleaned_data") else None
        if second.has_changed() and first_group and first_group == second_group:
            second.add_error("group", "Ця група вже вибрана вище.")
            forms_ok = False
        if forms_ok:
            # atomic: the student and the contracts are saved together, or nothing is saved
            with transaction.atomic():
                student = student_form.save()
                lines = []
                for form in [first, second]:
                    if form is second and not second.has_changed():
                        continue
                    contract = form.save(commit=False)
                    contract.student = student
                    contract.save()  # save() also creates the invoices
                    lines.append(enrollment_summary(contract))
            messages.success(request, f"Учня {student} додано і записано: " + "; ".join(lines) + ".")
            return redirect("school:student_detail", pk=student.pk)
        messages.error(request, "Перевірте поля, позначені червоним.")
    else:
        student_form = StudentForm()
        first = EnrollmentForm(prefix="g1", student=Student(), initial=enrollment_initial(group_id))
        second = EnrollmentForm(prefix="g2", student=Student(), empty_permitted=True,
                                use_required_attribute=False, initial=enrollment_initial(""))

    context = {
        "student_form": student_form,
        "enroll_forms": [first, second],
        "prices": enrollment_prices(),
    }
    return render(request, "school/student_create.html", context)


@admin_page
def enroll(request):
    """Page "Записати в групу" for a student who already exists.
    From the student card (?student=5) the student is known, from a group page (?group=3) the group is known."""
    student = Student.objects.filter(pk=request.GET.get("student") or 0).first()
    group_id = request.GET.get("group", "")
    if request.method == "POST":
        form = EnrollmentForm(request.POST, student=student)
        if form.is_valid():
            with transaction.atomic():
                contract = form.save()  # save() also creates the invoices
            messages.success(request, f"{contract.student} записано: {enrollment_summary(contract)}.")
            return redirect("school:student_detail", pk=contract.student_id)
        messages.error(request, "Перевірте поля, позначені червоним.")
    else:
        form = EnrollmentForm(student=student, initial=enrollment_initial(group_id))

    group = StudyGroup.objects.select_related("subject").filter(pk=group_id).first() if group_id.isdigit() else None
    context = {
        "form": form,
        "student": student,
        "group": group,
        "prices": enrollment_prices(),
    }
    return render(request, "school/enroll.html", context)


@admin_page
def contract_action(request, pk, action):
    """Buttons of a contract in the student card (only POST):
    pause  - "Поставити на паузу" (first day without lessons + reason),
    resume - "Повернути з паузи" (first day of lessons after the pause),
    leave  - "Учень пішов" (last day of studying).
    The money is recalculated in the model (Contract.pause / resume / terminate)."""
    contract = get_object_or_404(Contract.objects.select_related("student", "group"), pk=pk)
    back = redirect("school:student_detail", pk=contract.student_id)
    if request.method != "POST":
        return back

    try:
        day = date.fromisoformat(request.POST.get("day", ""))
    except ValueError:
        messages.error(request, "Вкажіть дату.")
        return back

    group = contract.group.full_name
    try:
        with transaction.atomic():  # all changes or nothing
            if action == "pause":
                refund = contract.pause(day, request.POST.get("reason", "").strip()[:200])
                text = f"«{group}»: пауза з {day:%d.%m.%Y}. Рахунки за місяці паузи не створюються."
            elif action == "resume":
                contract.resume(day)
                refund = 0
                text = f"«{group}»: учень повертається {day:%d.%m.%Y}. Рахунки на наступні місяці створено."
            elif action == "leave":
                refund = contract.terminate(day)
                text = (f"«{group}»: договір розірвано, останній день — {day:%d.%m.%Y}. "
                        "Неоплачені рахунки після цієї дати прибрано.")
            else:
                return back
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return back

    if refund:
        text += (f" Батьки вже заплатили за заняття, яких не буде, — створено заявку на повернення "
                 f"{refund:.0f} грн («Очікує розгляду», Фінанси → Повернення).")
    messages.success(request, text)
    return back


# Colors of subjects on the chart: very different colors, so lines are easy to tell apart
SUBJECT_COLORS = ["#2563eb", "#16a34a", "#9333ea", "#ea580c", "#0891b2", "#be185d", "#ca8a04", "#4b5563"]


def nmt_chart_data(results, contracts):
    """Data for the chart "Динаміка балів НМТ" in the student card (Chart.js).
    Only tests on the NMT scale (diagnostic, trial, final). Topic tests are in %,
    so they are not on this chart. "Поріг не подолано" = an empty point (a gap in the line).
    Returns None if there is nothing to draw."""
    nmt = [r for r in results if r.test.kind in Test.NMT_SCALE_KINDS]
    if not nmt:
        return None
    nmt.sort(key=lambda r: r.test.held_at)
    days = sorted({r.test.held_at for r in nmt})
    labels = [day.strftime("%d.%m") for day in days]

    datasets = []
    subjects = sorted({r.test.subject for r in nmt}, key=lambda s: s.name)
    # The place of the subject in the list of ALL subjects -> the same subject has the same color for every student
    all_subjects = list(Subject.objects.order_by("name").values_list("pk", flat=True))
    for subject in subjects:
        by_day = {r.test.held_at: r for r in nmt if r.test.subject_id == subject.pk}
        color = SUBJECT_COLORS[all_subjects.index(subject.pk) % len(SUBJECT_COLORS)]
        datasets.append({
            "label": subject.name,
            "data": [by_day[d].score if d in by_day else None for d in days],
            # Text for the tooltip: test name and "поріг не подолано"
            "notes": [
                (by_day[d].test.title + (" — поріг не подолано" if by_day[d].threshold_failed else ""))
                if d in by_day else "" for d in days
            ],
            "borderColor": color,
            "backgroundColor": color,
            "spanGaps": True,  # join the points over days without this subject
            "tension": 0.25,
        })

    # The guaranteed score of the tariff (for example, 170): a dashed line
    guarantees = sorted({c.tariff.guarantee_min_score for c in contracts
                         if c.tariff.has_guarantee and c.tariff.guarantee_min_score})
    for score in guarantees:
        datasets.append({
            "label": f"Гарантія {score}",
            "data": [score] * len(days),
            "notes": [""] * len(days),
            "borderColor": "#6b7280",
            "backgroundColor": "#6b7280",
            "borderDash": [6, 4],
            "pointRadius": 0,
            "borderWidth": 1.5,
            "isGuarantee": True,  # the legend shows it as a short line, not a box
        })

    # Progress of every subject: the last score minus the first score ("Математика +12")
    changes = []
    for subject in subjects:
        scores = [r.score for r in nmt if r.test.subject_id == subject.pk and r.score is not None]
        if len(scores) >= 2:
            changes.append({"subject": subject.name, "delta": scores[-1] - scores[0]})
    failed = sum(1 for r in nmt if r.threshold_failed)
    return {"labels": labels, "datasets": datasets, "changes": changes, "failed": failed, "count": len(nmt)}


# ───────────────────────── Groups ─────────────────────────

def overdue_by_group(today):
    """Overdue debts split by group: {group id: {student id: amount}}.
    It uses the same overdue_invoices() as all other pages."""
    result = {}
    for inv in overdue_invoices(today):
        students = result.setdefault(inv.contract.group_id, {})
        students[inv.contract.student_id] = students.get(inv.contract.student_id, Decimal("0")) + inv.debt_value
    return result


def running_groups(today):
    """Groups that study now: already started and not finished yet."""
    return StudyGroup.objects.filter(start_date__lte=today).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    )


@staff_page
def group_list(request):
    today = timezone.localdate()
    now = timezone.now()
    subject = request.GET.get("subject", "")
    teacher = request.GET.get("teacher", "")
    show = request.GET.get("show", "")  # "" = only groups that study now, "all" = all groups

    groups = (
        groups_for(request.user)
        .select_related("subject", "teacher")
        .prefetch_related("schedule")
        .annotate(
            active_count=Count("contracts", filter=Q(contracts__status=Contract.Status.ACTIVE), distinct=True),
            # A suspended student still keeps the place in the group
            taken=Count("contracts", filter=Q(contracts__status__in=Contract.OPEN_STATUSES), distinct=True),
        )
    )
    if show != "all":
        groups = groups.filter(pk__in=running_groups(today))
    if subject.isdigit():
        groups = groups.filter(subject_id=int(subject))
    if teacher.isdigit():
        groups = groups.filter(teacher_id=int(teacher))

    groups = list(groups)
    debts = overdue_by_group(today)
    # The next lesson of every group in ONE query: we take the planned lessons from now
    next_lessons = {}
    for lesson in Lesson.objects.filter(
        group__in=groups, starts_at__gte=now, status=Lesson.Status.PLANNED
    ).order_by("starts_at"):
        next_lessons.setdefault(lesson.group_id, lesson)  # keep only the first (the nearest)

    for group in groups:
        group_debts = debts.get(group.id, {})
        group.debtors_count = len(group_debts)
        group.debt_total = sum(group_debts.values(), Decimal("0"))
        group.free = group.max_students - group.taken
        group.fill_pct = round(group.taken * 100 / group.max_students) if group.max_students else 0
        group.next_lesson = next_lessons.get(group.id)

    context = {
        "groups": groups,
        "subjects": Subject.objects.filter(groups__in=groups_for(request.user)).distinct(),
        "teachers": Teacher.objects.filter(groups__in=groups_for(request.user)).distinct(),
        "subject": subject,
        "teacher": teacher,
        "show": show,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/group_list.html", context)


@staff_page
def group_detail(request, pk):
    today = timezone.localdate()
    now = timezone.now()
    group = get_object_or_404(
        groups_for(request.user).select_related("subject", "teacher").prefetch_related("schedule"), pk=pk
    )

    # Students of the group = open contracts (active + suspended)
    contracts = list(
        group.contracts
        .filter(status__in=Contract.OPEN_STATUSES)
        .select_related("student", "tariff")
        .order_by("student__last_name", "student__first_name")
    )

    # Attendance of every student in THIS group, in one query
    attendance = {
        row["student"]: row
        for row in Attendance.objects.filter(lesson__group=group)
        .values("student")
        .annotate(
            total=Count("id"),
            present=Count("id", filter=Q(status__in=[Attendance.Status.PRESENT, Attendance.Status.LATE])),
        )
    }
    # The last result on the NMT scale (100-200) of every student in this subject
    last_scores = {}
    for result in (
        TestResult.objects
        .filter(test__subject=group.subject, test__kind__in=Test.NMT_SCALE_KINDS,
                student__in=[c.student_id for c in contracts])
        .select_related("test")
        .order_by("-test__held_at")
    ):
        last_scores.setdefault(result.student_id, result)  # the first one is the newest

    debts = overdue_by_group(today).get(group.id, {})
    blocked = no_access_ids(today)
    for c in contracts:
        c.no_access = c.student_id in blocked
        stats = attendance.get(c.student_id)
        c.attendance_pct = round(stats["present"] * 100 / stats["total"]) if stats and stats["total"] else None
        c.last_score = last_scores.get(c.student_id)
        c.debt = debts.get(c.student_id, Decimal("0"))

    active_count = sum(c.status == Contract.Status.ACTIVE for c in contracts)
    next_lessons = group.lessons.filter(starts_at__gte=now).exclude(
        status=Lesson.Status.CANCELLED
    ).select_related("teacher").order_by("starts_at")[:6]
    past_lessons = (
        group.lessons.filter(starts_at__lt=now)
        .select_related("teacher")
        .annotate(
            marked=Count("attendance"),
            present=Count("attendance", filter=Q(
                attendance__status__in=[Attendance.Status.PRESENT, Attendance.Status.LATE]
            )),
        )
        .order_by("-starts_at")[:10]
    )
    held_count = group.lessons.filter(status=Lesson.Status.HELD).count()

    context = {
        "group": group,
        "contracts": contracts,
        "suspended_count": len(contracts) - active_count,
        "taken": len(contracts),
        "free": group.max_students - len(contracts),
        "debtors_count": len(debts),
        "debt_total": sum(debts.values(), Decimal("0")),
        "next_lessons": next_lessons,
        "past_lessons": past_lessons,
        "held_count": held_count,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        # Start date for the "Записати учня" button: today, but not before the group starts
        "enroll_start": max(timezone.localdate(), group.start_date),
        # For the "Керування групою" block
        "is_finished": bool(group.end_date and group.end_date < today),
        "money_records": group.money_records_count(),
        "invoices_count": Invoice.objects.filter(contract__group=group).count(),
        "all_contracts_count": group.contracts.count(),
        "lessons_count": group.lessons.count(),
        "finish_default": max(today, group.start_date),
    }
    return render(request, "school/group_detail.html", context)


@admin_page
def group_sync_lessons(request, pk):
    """Button "Оновити заняття за розкладом" on the group page (only POST).
    Creates the missing lessons until the end of the group and removes planned lessons
    that are not in the schedule anymore. Marked and held lessons are never touched."""
    group = get_object_or_404(StudyGroup, pk=pk)
    if request.method == "POST":
        if not group.end_date:
            messages.error(request, "Спершу вкажіть дату завершення групи («Редагувати групу і розклад»).")
        elif not group.schedule.exists():
            messages.error(request, "У групи немає розкладу. Додайте дні й час у «Редагувати групу і розклад».")
        else:
            created, removed = group.sync_lessons_with_schedule()
            if created or removed:
                text = f"Заняття оновлено: створено {created}"
                if removed:
                    text += f", прибрано {removed} (їх більше немає в розкладі)"
                messages.success(request, text + f". Заняття заплановано до {group.end_date:%d.%m.%Y}.")
            else:
                messages.info(request, "Усі заняття вже відповідають розкладу — нічого змінювати не треба.")
    # Post/Redirect/Get: F5 after the button does not repeat the action
    return redirect("school:group_detail", pk=group.pk)


@admin_page
def group_finish(request, pk):
    """Button "Завершити групу" (only POST): the group stops studying on the chosen day.
    Nothing is deleted from the money history: the group just goes to the finished groups."""
    group = get_object_or_404(StudyGroup, pk=pk)
    if request.method != "POST":
        return redirect("school:group_detail", pk=group.pk)

    try:
        on_day = date.fromisoformat(request.POST.get("on_day", ""))
    except ValueError:
        messages.error(request, "Вкажіть дату завершення.")
        return redirect("school:group_detail", pk=group.pk)

    if on_day < group.start_date:
        messages.error(
            request,
            f"Група почалася {group.start_date:%d.%m.%Y} — завершити раніше не можна. "
            "Якщо групу створили помилково, використайте «Видалити групу».",
        )
        return redirect("school:group_detail", pk=group.pk)

    with transaction.atomic():  # all changes or nothing
        finished, removed, refunds_total = group.finish(on_day)
    text = (
        f"Групу завершено {on_day:%d.%m.%Y}. Договорів завершено: {finished}, "
        f"майбутніх занять прибрано: {removed}. Оплати, борги й журнал збережено."
    )
    if refunds_total:
        text += (f" Батьки вже заплатили за заняття після цієї дати — створено заявки на повернення "
                 f"на {refunds_total:.0f} грн (Фінанси → Повернення).")
    messages.success(request, text)
    return redirect("school:group_detail", pk=group.pk)


@admin_page
def group_delete(request, pk):
    """Button "Видалити групу" (only POST): deletes a group created by mistake
    together with its contracts, invoices and lessons — only if there is no money in it."""
    group = get_object_or_404(StudyGroup, pk=pk)
    if request.method != "POST":
        return redirect("school:group_detail", pk=group.pk)

    name = group.full_name
    try:
        with transaction.atomic():  # if something fails, nothing is deleted
            counts = group.delete_with_contracts()
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
        return redirect("school:group_detail", pk=group.pk)

    messages.success(
        request,
        f"Групу «{name}» видалено разом з договорами ({counts['contracts']}), "
        f"рахунками ({counts['invoices']}) і заняттями ({counts['lessons']}).",
    )
    return redirect("school:group_list")


# ───────────────────────── Journal ─────────────────────────

# Buttons in the journal: (value, short text, Bootstrap color)
ATTENDANCE_BUTTONS = [
    (Attendance.Status.PRESENT, "Присутній", "success"),
    (Attendance.Status.LATE, "Запізнився", "warning"),
    (Attendance.Status.ABSENT, "Відсутній", "danger"),
    (Attendance.Status.EXCUSED, "Поважна", "secondary"),
]
# Statuses that can be chosen for a lesson that has already started
PAST_LESSON_STATUSES = [Lesson.Status.HELD, Lesson.Status.CANCELLED, Lesson.Status.RESCHEDULED]


def parse_day(value, today):
    """"2026-10-30" from the address -> date(2026, 10, 30). A wrong value -> today."""
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return today


def lesson_students(lesson):
    """Students who must be in the journal of this lesson:
    everybody whose contract for the group was valid on the lesson day
    (suspended students do not come), plus anybody who already has a mark
    (so old marks never disappear from the form)."""
    day = timezone.localtime(lesson.starts_at).date()
    # A pause that covers the lesson day: such a student does not come to this lesson
    on_pause = ContractPause.objects.filter(
        contract=OuterRef("pk"), start_date__lte=day
    ).filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
    ids = set(
        Contract.objects
        .filter(group_id=lesson.group_id, start_date__lte=day)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
        .exclude(Exists(on_pause))
        .values_list("student_id", flat=True)
    )
    ids |= set(lesson.attendance.values_list("student_id", flat=True))
    return Student.objects.filter(pk__in=ids).order_by("last_name", "first_name")


def with_marks(lessons):
    """Add to lessons: marked (how many marks) and present (present + late)."""
    return lessons.annotate(
        marked=Count("attendance"),
        present=Count("attendance", filter=Q(
            attendance__status__in=[Attendance.Status.PRESENT, Attendance.Status.LATE]
        )),
    )


def unmarked_lessons(lessons, now, days=30):
    """Lessons of the last `days` days that already started, were not cancelled,
    but have no marks at all: the teacher forgot the journal.
    The same rule for the Journal, the teacher main page and the admin main page."""
    return with_marks(
        lessons.filter(
            starts_at__lt=now, starts_at__gte=now - timedelta(days=days),
            status__in=[Lesson.Status.PLANNED, Lesson.Status.HELD],
        ).select_related("group__subject", "teacher")
    ).filter(marked=0).order_by("starts_at")


def mark_lesson_state(lessons, now):
    """Add lesson.needs_marks: the lesson has started, it was not cancelled, but nobody
    marked the students. Templates show it as a red "не відмічено" badge."""
    for lesson in lessons:
        lesson.is_future = lesson.starts_at > now
        lesson.needs_marks = (not lesson.is_future and lesson.marked == 0
                              and lesson.status in [Lesson.Status.PLANNED, Lesson.Status.HELD])
    return lessons


@staff_page
def journal(request):
    today = timezone.localdate()
    now = timezone.now()
    day = parse_day(request.GET.get("date"), today)

    lessons = with_marks(
        lessons_for(request.user).filter(starts_at__date=day)
        .select_related("group__subject", "group__teacher", "teacher")
    ).order_by("starts_at")
    mark_lesson_state(lessons, now)
    unmarked = unmarked_lessons(lessons_for(request.user), now)

    context = {
        "day": day,
        "today": today,
        "previous_day": (day - timedelta(days=1)).isoformat(),
        "next_day": (day + timedelta(days=1)).isoformat(),
        "lessons": lessons,
        "unmarked": unmarked,
    }
    return render(request, "school/journal.html", context)


@staff_page
def journal_lesson(request, pk):
    """Attendance form of one lesson. GET shows the form, POST saves it."""
    lesson = get_object_or_404(
        lessons_for(request.user).select_related("group__subject", "group__teacher", "teacher"), pk=pk
    )
    now = timezone.now()
    is_future = lesson.starts_at > now
    students = list(lesson_students(lesson))

    if request.method == "POST":
        # You cannot mark a lesson that has not started yet
        if is_future:
            messages.error(request, "Заняття ще не почалося — відмічати відвідуваність зарано.")
            return redirect("school:journal_lesson", pk=lesson.pk)

        new_status = request.POST.get("lesson_status")
        if new_status not in PAST_LESSON_STATUSES:
            messages.error(request, "Оберіть статус заняття.")
            return redirect("school:journal_lesson", pk=lesson.pk)

        # atomic: the lesson and all marks are saved together, or nothing is saved
        with transaction.atomic():
            lesson.topic = request.POST.get("topic", "").strip()[:200]
            lesson.status = new_status
            lesson.save(update_fields=["topic", "status"])

            if new_status != Lesson.Status.HELD:
                # Nobody can be "present" at a cancelled lesson
                deleted, _ = lesson.attendance.all().delete()
                text = f"Заняття позначено як «{lesson.get_status_display()}»."
                if deleted:
                    text += f" Видалено відміток: {deleted}."
                messages.info(request, text)
            else:
                saved, missing = 0, []
                # We read marks ONLY for students of this lesson,
                # so a changed form cannot add a stranger to the journal
                for student in students:
                    value = request.POST.get(f"student_{student.pk}")
                    if value not in Attendance.Status.values:
                        missing.append(str(student))
                        continue
                    Attendance.objects.update_or_create(
                        lesson=lesson, student=student, defaults={"status": value},
                    )
                    saved += 1
                messages.success(request, f"Журнал збережено: відмічено {saved} учн.")
                if missing:
                    messages.warning(request, "Не відмічено: " + ", ".join(missing))
        return redirect("school:journal_lesson", pk=lesson.pk)

    # GET: show the form
    marks = dict(lesson.attendance.values_list("student_id", "status"))
    debts = overdue_by_group(timezone.localdate()).get(lesson.group_id, {})
    blocked = no_access_ids(timezone.localdate())
    for student in students:
        student.mark = marks.get(student.pk)
        student.debt = debts.get(student.pk, Decimal("0"))
        student.no_access = student.pk in blocked

    # Neighbour lessons of the same group: quick moving through the journal
    group_lessons = lessons_for(request.user).filter(group_id=lesson.group_id)
    previous_lesson = group_lessons.filter(starts_at__lt=lesson.starts_at).order_by("-starts_at").first()
    next_lesson = group_lessons.filter(starts_at__gt=lesson.starts_at).order_by("starts_at").first()

    # For a planned lesson that has already started we suggest "held"
    if lesson.status == Lesson.Status.PLANNED and not is_future:
        form_status = Lesson.Status.HELD
    else:
        form_status = lesson.status

    context = {
        "lesson": lesson,
        "students": students,
        "is_future": is_future,
        "buttons": ATTENDANCE_BUTTONS,
        "status_choices": [(s, s.label) for s in PAST_LESSON_STATUSES],
        "form_status": form_status,
        "marked_count": len(marks),
        "debtors_count": sum(1 for s in students if s.debt),
        "no_access_count": sum(1 for s in students if s.no_access),
        "previous_lesson": previous_lesson,
        "next_lesson": next_lesson,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/journal_lesson.html", context)


# ───────────────────────── Debts ─────────────────────────

@admin_page
def call_list(request):
    """The list "to call parents": students with an overdue debt."""
    today = timezone.localdate()
    rows = debtors_to_call(today)
    blocked = no_access_ids(today)
    for row in rows:
        row["no_access"] = row["student"].pk in blocked
    context = {
        "today": today,
        "rows": rows,
        "total_debt": sum((r["debt"] for r in rows), Decimal("0")),
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/call_list.html", context)


# ───────────────────────── Finance ─────────────────────────

def money_sum(queryset, field):
    """Sum of one money field in SQL (0 if there are no rows)."""
    return queryset.aggregate(s=Coalesce(Sum(field), ZERO, output_field=MONEY_FIELD))["s"]


def parse_month(value, today):
    """"2026-10" from the address -> date(2026, 10, 1). A wrong value -> the current month."""
    try:
        year, month = (int(part) for part in value.split("-"))
        return date(year, month, 1)
    except (AttributeError, ValueError):
        return today.replace(day=1)


def school_year_months(month):
    """Months of the school year (September - May) that contains this month."""
    year = month.year if month.month >= 9 else month.year - 1
    return months_between(date(year, 9, 1), date(year + 1, 5, 1))


def month_summary(month):
    """All money numbers of one month. The same function is used for the cards and for the year table,
    so the numbers on the page are always the same.

    plan      = invoices with the due date in this month (what we expect to get);
    collected = how much was paid for THESE invoices (on any day);
    income    = all money that came in this month (also late payments for old months);
    profit    = income - expenses - salaries - refunds."""
    end = next_month(month)

    plan_invoices = Invoice.objects.filter(due_date__gte=month, due_date__lt=end)
    plan = money_sum(plan_invoices, "amount")
    collected = money_sum(Payment.objects.filter(invoice__in=plan_invoices), "amount")

    payments = Payment.objects.filter(paid_at__date__gte=month, paid_at__date__lt=end)
    income = money_sum(payments, "amount")
    reserve = money_sum(payments, "reserve_amount")

    expenses = money_sum(Expense.objects.filter(spent_at__gte=month, spent_at__lt=end), "amount")
    # Salary is a cost of the month when the lessons were held (it is paid later)
    month_salaries = Salary.objects.filter(period_month=month)
    salaries = money_sum(month_salaries, "base_amount") + money_sum(month_salaries, "bonus")
    refunds = money_sum(
        Refund.objects.filter(status=Refund.Status.PAID, paid_at__gte=month, paid_at__lt=end), "amount"
    )

    return {
        "month": month,
        "plan": plan,
        "collected": collected,
        # None if there is no plan: we cannot divide by 0
        "collected_pct": round(collected * 100 / plan) if plan else None,
        "income": income,
        "reserve": reserve,
        "expenses": expenses,
        "salaries": salaries,
        "refunds": refunds,
        "costs": expenses + salaries + refunds,
        "profit": income - expenses - salaries - refunds,
    }


@admin_page
def finance(request):
    today = timezone.localdate()
    month = parse_month(request.GET.get("month"), today)
    end = next_month(month)
    summary = month_summary(month)

    # Invoices of the month with their state
    invoices = list(
        invoices_with_paid()
        .filter(due_date__gte=month, due_date__lt=end)
        .select_related("contract__tariff")
        .order_by("due_date", "contract__student__last_name")
    )
    for inv in invoices:
        inv.debt_value = max(inv.amount - inv.paid_sum, Decimal("0"))
        if inv.debt_value == 0:
            inv.state, inv.state_color = "Сплачено", "success"
        elif inv.due_date > today:
            inv.state, inv.state_color = "Очікується", "secondary"
        elif is_overdue(inv, today):
            inv.state, inv.state_color = "Прострочено", "danger"
        elif inv.paid_sum > 0:
            inv.state, inv.state_color = "Частково", "warning"
        else:
            inv.state, inv.state_color = "Не сплачено", "warning"

    payments = (
        Payment.objects.filter(paid_at__date__gte=month, paid_at__date__lt=end)
        .select_related("invoice__contract__student", "invoice__contract__group__subject")
        .order_by("-paid_at")
    )
    expenses = Expense.objects.filter(spent_at__gte=month, spent_at__lt=end).order_by("-spent_at")
    # Expenses by category: [("Оренда", 18000), ...], the biggest first
    labels = dict(Expense.Category.choices)
    expenses_by_category = [
        (labels.get(row["category"], row["category"]), row["total"])
        for row in expenses.values("category").annotate(total=Sum("amount")).order_by("-total")
    ]
    salaries = Salary.objects.filter(period_month=month).select_related("teacher").order_by("teacher__last_name")
    refunds = (
        Refund.objects
        .filter(
            Q(created_at__gte=month, created_at__lt=end)
            | Q(paid_at__gte=month, paid_at__lt=end)
            # Requests that still wait for a decision are shown in every month, so they are not forgotten
            | Q(status__in=[Refund.Status.PENDING, Refund.Status.APPROVED])
        )
        .select_related("contract__student")
        .order_by("-created_at")
    )

    # Debtors: always "now", not for the chosen month
    debtors = debtors_to_call(today)

    # Table for the whole school year. Months in the future have only a plan.
    current_month = today.replace(day=1)
    year_rows = [month_summary(m) for m in school_year_months(month)]
    for row in year_rows:
        row["is_future"] = row["month"] > current_month
    past_rows = [row for row in year_rows if not row["is_future"]]
    year_total = {
        key: sum((row[key] for row in past_rows), Decimal("0"))
        for key in ["plan", "income", "expenses", "salaries", "refunds", "profit"]
    }

    previous = (month - timedelta(days=1)).replace(day=1)
    context = {
        "today": today,
        "month": month,
        "is_current_month": month == current_month,
        "previous_month": previous.strftime("%Y-%m"),
        "next_month": end.strftime("%Y-%m"),
        "s": summary,
        "invoices": invoices,
        "payments": payments,
        "expenses": expenses,
        "expenses_by_category": expenses_by_category,
        "salaries": salaries,
        "refunds": refunds,
        "debtors": debtors[:10],
        "debtors_count": len(debtors),
        "debtors_total": sum((d["debt"] for d in debtors), Decimal("0")),
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        "year_rows": year_rows,
        "year_total": year_total,
    }
    return render(request, "school/finance.html", context)


# ───────────────────────── Payments ─────────────────────────

def unpaid_invoices(student):
    """Invoices of the student that are not fully paid, oldest first.
    Every invoice gets debt_value (how much is still not paid)."""
    result = []
    for inv in (
        invoices_with_paid().filter(contract__student=student)
        .order_by("due_date", "period_month", "pk")
    ):
        inv.debt_value = inv.amount - inv.paid_sum
        if inv.debt_value > 0:
            result.append(inv)
    return result


def allocate_payment(invoices, amount):
    """Split the money between invoices: first the oldest debt, then the next one, and so on.
    Example: debts 2040 (September) and 2040 (October), the parent pays 3000
             -> 2040 to September, 960 to October.
    Returns (plan, rest): plan = [(invoice, part), ...], rest = money that did not fit."""
    plan, rest = [], amount
    for inv in invoices:
        if rest <= 0:
            break
        part = min(inv.debt_value, rest)
        plan.append((inv, part))
        rest -= part
    return plan, rest


@admin_page
def student_payment(request, pk):
    """Page "Внести оплату": the administrator writes only the amount,
    the system puts it on the invoices (oldest debts first)."""
    today = timezone.localdate()
    student = get_object_or_404(Student, pk=pk)
    invoices = unpaid_invoices(student)
    for inv in invoices:
        inv.is_overdue = is_overdue(inv, today)
        inv.is_future = inv.due_date > today
    total_now = sum((inv.debt_value for inv in invoices if not inv.is_future), Decimal("0"))
    total_all = sum((inv.debt_value for inv in invoices), Decimal("0"))

    form = {"amount": "", "method": Payment.Method.CASH, "paid_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M")}
    if request.method == "POST":
        form.update({key: request.POST.get(key, "").strip() for key in form})
        errors = []
        try:
            amount = Decimal(form["amount"].replace(",", ".").replace(" ", ""))
        except Exception:
            amount = None
        if amount is None or amount <= 0:
            errors.append("Вкажіть суму більшу за 0.")
        elif amount != amount.quantize(Decimal("0.01")):
            errors.append("Сума — до копійок (два знаки після коми).")
        if form["method"] not in Payment.Method.values:
            errors.append("Оберіть спосіб оплати.")
        try:
            paid_at = timezone.make_aware(datetime.fromisoformat(form["paid_at"]))
            if paid_at > timezone.now() + timedelta(minutes=5):
                errors.append("Дата оплати не може бути в майбутньому.")
        except ValueError:
            errors.append("Вкажіть дату й час оплати.")
        if not errors:
            plan, rest = allocate_payment(invoices, amount)
            if rest > 0:
                errors.append(
                    f"Сума більша за всі рахунки учня на {rest:.2f} грн (усього до сплати, з наперед: {total_all:.2f} грн). "
                    "Якщо батьки переплатили, оформіть «Повернення коштів» з причиною «Переплата»."
                )
        if errors:
            for error in errors:
                messages.error(request, error)
        else:
            # atomic: all parts of the payment are saved together, or nothing is saved
            with transaction.atomic():
                lines = []
                for inv, part in plan:
                    payment = Payment(invoice=inv, amount=part, paid_at=paid_at, method=form["method"])
                    payment.full_clean()  # the same checks as in the admin
                    payment.save()
                    when = "весь курс" if inv.contract.payment_plan == Contract.PaymentPlan.PREPAID else f"{inv.period_month:%m.%Y}"
                    lines.append(f"{inv.contract.group.full_name}, {when}: {part:.2f} грн")
            messages.success(request, f"Оплату {amount:.2f} грн внесено. Зараховано: " + "; ".join(lines) + ".")
            return redirect("school:student_detail", pk=student.pk)

    context = {
        "student": student,
        "invoices": invoices,
        "total_now": total_now,
        "total_all": total_all,
        "overdue_total": sum((inv.debt_value for inv in invoices if inv.is_overdue), Decimal("0")),
        "methods": Payment.Method.choices,
        "form": form,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/student_payment.html", context)


# ───────────────────────── Access (logins) ─────────────────────────

@admin_page
def staff_accounts(request):
    """Page "Доступ": the administrator creates logins for teachers, changes passwords,
    turns access off and adds new administrators. No console is needed."""
    from django.contrib.auth import get_user_model

    User = get_user_model()

    if request.method == "POST":
        action = request.POST.get("action")
        password = request.POST.get("password", "")
        try:
            if action == "create_admin":
                user = create_admin_login(request.POST.get("username", ""), password, request.POST.get("first_name", ""))
                messages.success(request, f"Адміністратора «{user.username}» створено.")
            else:
                teacher = get_object_or_404(Teacher, pk=request.POST.get("teacher"))
                if action == "create":
                    if teacher.user_id:
                        raise LoginError(f"У викладача {teacher} уже є логін «{teacher.user.username}».")
                    user, _ = create_teacher_login(teacher, password, request.POST.get("username"), check=True)
                    messages.success(request, f"Логін «{user.username}» для {teacher} створено. Передайте викладачу логін і пароль.")
                elif action == "password" and teacher.user:
                    check_password(password, teacher.user)
                    teacher.user.set_password(password)
                    teacher.user.save()
                    messages.success(request, f"Пароль для «{teacher.user.username}» змінено.")
                elif action == "toggle" and teacher.user:
                    # Turning off is safer than deleting: history (who marked the journal) stays
                    teacher.user.is_active = not teacher.user.is_active
                    teacher.user.save(update_fields=["is_active"])
                    state = "увімкнено" if teacher.user.is_active else "вимкнено"
                    messages.info(request, f"Доступ для «{teacher.user.username}» {state}.")
                else:
                    messages.error(request, "Невідома дія.")
        except LoginError as error:
            messages.error(request, str(error))
        # Post/Redirect/Get: F5 after saving does not send the form again
        return redirect("school:staff_accounts")

    teachers = list(
        Teacher.objects.select_related("user")
        .annotate(groups_count=Count("groups", distinct=True))
        .order_by("-is_active", "last_name", "first_name")
    )
    for teacher in teachers:
        teacher.suggested_username = suggest_username(teacher)
    admins = (
        User.objects.filter(Q(is_superuser=True) | Q(groups__name=ADMIN_GROUP))
        .distinct().order_by("username")
    )
    context = {"teachers": teachers, "admins": admins}
    return render(request, "school/staff_accounts.html", context)