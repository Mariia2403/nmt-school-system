"""
Views (page logic) of the school website.
Every page is only for logged-in users with a role (see access.py):
@staff_page  - administrator and teachers (a teacher sees only his own data),
@admin_page  - only the administrator (money and debts).
"""
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, DecimalField, Exists, OuterRef, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .access import (
    admin_page, get_teacher, groups_for, is_admin, lessons_for, staff_page, students_for, subjects_for,
)
from .models import (
    Attendance, Contract, Expense, Invoice, Lesson, Payment, Refund, Salary, Student, StudyGroup, Subject,
    Teacher, Test, TestResult, months_between, next_month,
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

    # Lessons for today and the next lessons
    today_lessons = (
        Lesson.objects.filter(starts_at__date=today)
        .select_related("group__subject", "teacher")
        .order_by("starts_at")
    )
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
    }
    return render(request, "school/dashboard.html", context)


def teacher_dashboard(request, teacher):
    """Main page of a teacher: lessons today, forgotten journals, his groups."""
    today = timezone.localdate()
    now = timezone.now()
    lessons = lessons_for(request.user).select_related("group__subject", "teacher")
    today_lessons = with_marks(lessons.filter(starts_at__date=today)).order_by("starts_at")
    for lesson in today_lessons:
        lesson.needs_marks = (lesson.starts_at <= now and lesson.marked == 0
                              and lesson.status in [Lesson.Status.PLANNED, Lesson.Status.HELD])
    unmarked = with_marks(lessons.filter(
        starts_at__lt=now, starts_at__gte=now - timedelta(days=30),
        status__in=[Lesson.Status.PLANNED, Lesson.Status.HELD],
    )).filter(marked=0).order_by("starts_at")
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
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    grade = request.GET.get("grade", "")
    debt_filter = request.GET.get("debt", "")

    active_contract = Contract.objects.filter(student=OuterRef("pk"), status=Contract.Status.ACTIVE)
    students = (
        # A teacher sees only students of his groups
        Student.objects.filter(pk__in=students_for(request.user))
        .annotate(has_active=Exists(active_contract))
        # Load active contracts with groups in ONE extra query (not one query per student)
        .prefetch_related(Prefetch(
            "contracts",
            queryset=Contract.objects.filter(status=Contract.Status.ACTIVE).select_related("group__subject"),
            to_attr="active_contracts",
        ))
    )

    # Search: every word must be found in one of the fields.
    # "Шевченко Анна" finds Anna Shevchenko.
    for word in q.split():
        students = students.filter(
            Q(last_name__icontains=word) | Q(first_name__icontains=word) | Q(phone__icontains=word)
            | Q(parent_name__icontains=word) | Q(parent_phone__icontains=word)
        )
    if status == "active":
        students = students.filter(has_active=True)
    elif status == "inactive":
        students = students.filter(has_active=False)
    if grade.isdigit():
        students = students.filter(grade=int(grade))

    # Debts are calculated once and used for the filter and for the table
    debts = debt_by_student(today)
    if debt_filter == "overdue":
        students = students.filter(pk__in=[s.pk for s in debts])

    total_found = students.count()
    page = Paginator(students, 20).get_page(request.GET.get("page"))

    # Add the debt to every student on this page
    for student in page:
        student.debt = debts.get(student, Decimal("0"))

    # Keep search and filters when the user goes to another page
    params = request.GET.copy()
    params.pop("page", None)

    context = {
        "page": page,
        "total_found": total_found,
        "q": q,
        "status": status,
        "grade": grade,
        "debt_filter": debt_filter,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
        "grades": students_for(request.user).values_list("grade", flat=True).distinct().order_by("grade"),
        "querystring": params.urlencode(),
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
        .prefetch_related("group__schedule")
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
        "contracts": contracts,
        "attendance": attendance[:30],
        "attendance_total": stats["total"],
        "attendance_percent": attendance_percent,
        "results": results,
        "avg_score": avg_score,
        "nmt_results_count": results.filter(test__kind__in=Test.NMT_SCALE_KINDS).count(),
        "invoices": invoices,
        "overdue_debt": overdue_debt,
        "unpaid_total": unpaid_total,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/student_detail.html", context)


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
    for c in contracts:
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
    }
    return render(request, "school/group_detail.html", context)


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
    ids = set(
        Contract.objects
        .filter(group_id=lesson.group_id, start_date__lte=day)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=day))
        .exclude(status=Contract.Status.SUSPENDED)
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


@staff_page
def journal(request):
    today = timezone.localdate()
    now = timezone.now()
    day = parse_day(request.GET.get("date"), today)

    lessons = with_marks(
        lessons_for(request.user).filter(starts_at__date=day)
        .select_related("group__subject", "group__teacher", "teacher")
    ).order_by("starts_at")
    for lesson in lessons:
        lesson.is_future = lesson.starts_at > now
        # A lesson has started, it was not cancelled, but nobody marked the students
        lesson.needs_marks = (not lesson.is_future and lesson.marked == 0
                              and lesson.status in [Lesson.Status.PLANNED, Lesson.Status.HELD])

    # Lessons of the last 30 days without any marks: the teacher forgot the journal
    unmarked = with_marks(
        lessons_for(request.user).filter(
            starts_at__lt=now, starts_at__gte=now - timedelta(days=30),
            status__in=[Lesson.Status.PLANNED, Lesson.Status.HELD],
        ).select_related("group__subject", "teacher")
    ).filter(marked=0).order_by("starts_at")

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
    for student in students:
        student.mark = marks.get(student.pk)
        student.debt = debts.get(student.pk, Decimal("0"))

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
        .filter(Q(created_at__gte=month, created_at__lt=end) | Q(paid_at__gte=month, paid_at__lt=end))
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