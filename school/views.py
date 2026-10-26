"""
Views (page logic) of the school website.
Every page is only for logged-in users (@login_required).
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, DecimalField, Exists, OuterRef, Prefetch, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .models import (
    Attendance, Contract, Expense, Invoice, Lesson, Payment, Student, StudyGroup, Test,
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

@login_required
def dashboard(request):
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


# ───────────────────────── Students ─────────────────────────

@login_required
def student_list(request):
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    grade = request.GET.get("grade", "")
    debt_filter = request.GET.get("debt", "")

    active_contract = Contract.objects.filter(student=OuterRef("pk"), status=Contract.Status.ACTIVE)
    students = (
        Student.objects
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
        "grades": Student.objects.values_list("grade", flat=True).distinct().order_by("grade"),
        "querystring": params.urlencode(),
    }
    return render(request, "school/student_list.html", context)


@login_required
def student_detail(request, pk):
    today = timezone.localdate()
    # If there is no student with this id, Django shows the "404 Not found" page
    student = get_object_or_404(Student, pk=pk)

    contracts = (
        student.contracts
        .select_related("group__subject", "group__teacher", "tariff")
        .prefetch_related("group__schedule")
        .order_by("-start_date")
    )

    # Attendance: statistics for all lessons, the list shows the last 30
    attendance = student.attendance.select_related("lesson__group__subject").order_by("-lesson__starts_at")
    stats = attendance.aggregate(
        total=Count("id"),
        present=Count("id", filter=Q(status__in=[Attendance.Status.PRESENT, Attendance.Status.LATE])),
    )
    attendance_percent = round(stats["present"] * 100 / stats["total"]) if stats["total"] else None

    # Test results
    results = student.test_results.select_related("test__subject", "test__group").order_by("-test__held_at")
    # Average only for tests on the NMT scale (100-200).
    # If we mix them with topic tests in %, "165" and "70%" give a meaningless "117".
    avg_score = results.filter(test__kind__in=Test.NMT_SCALE_KINDS).aggregate(avg=Avg("score"))["avg"]

    # Invoices with payments inside
    invoices = list(
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
        "invoices": invoices,
        "overdue_debt": overdue_debt,
        "unpaid_total": unpaid_total,
        "overdue_days": settings.DEBT_OVERDUE_DAYS,
    }
    return render(request, "school/student_detail.html", context)


# ───────────────────────── Debts ─────────────────────────

@login_required
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