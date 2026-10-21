"""
Views (page logic) of the school website.
Every page is only for logged-in users (@login_required).
"""
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone

from .models import Expense, Invoice, Lesson, Payment, Student, StudyGroup

ZERO = Value(Decimal("0"))
MONEY_FIELD = DecimalField(max_digits=12, decimal_places=2)


def invoices_with_paid():
    """Invoices with an extra field paid_sum (how much was paid), calculated in SQL."""
    return (
        Invoice.objects
        .select_related("contract__student", "contract__group")
        .annotate(paid_sum=Coalesce(Sum("payments__amount"), ZERO, output_field=MONEY_FIELD))
    )


def overdue_invoices(today):
    """Invoices where the due date has passed and the full amount is not paid yet."""
    return [inv for inv in invoices_with_paid().filter(due_date__lt=today) if inv.paid_sum < inv.amount]


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

    # Debts: sum of unpaid parts of overdue invoices, grouped by student
    overdue = overdue_invoices(today)
    debt_by_student = {}
    for inv in overdue:
        student = inv.contract.student
        debt_by_student[student] = debt_by_student.get(student, Decimal("0")) + (inv.amount - inv.paid_sum)
    total_debt = sum(debt_by_student.values(), Decimal("0"))
    top_debtors = sorted(debt_by_student.items(), key=lambda item: item[1], reverse=True)[:5]

    # Lessons for today and the next lessons
    today_lessons = (
        Lesson.objects.filter(starts_at__date=today)
        .select_related("group", "group__subject", "teacher")
        .order_by("starts_at")
    )
    next_lessons = (
        Lesson.objects.filter(starts_at__gt=now, status=Lesson.Status.PLANNED)
        .exclude(starts_at__date=today)
        .select_related("group", "teacher")
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
        "debtors_count": len(debt_by_student),
        "top_debtors": top_debtors,
        "today_lessons": today_lessons,
        "next_lessons": next_lessons,
    }
    return render(request, "school/dashboard.html", context)