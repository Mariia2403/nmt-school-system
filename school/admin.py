"""
Django Admin settings for all tables of the school.

For every model we set:
- list_display  - columns in the list
- list_filter   - filters on the right side
- search_fields - search box at the top
- inlines       - related records inside one page (for example, payments inside an invoice)
"""
from decimal import Decimal

from django.contrib import admin
from django.db.models import Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce

from .models import (
    Attendance, Contract, Expense, GroupSchedule, Invoice, Lesson, Payment, Refund, Salary,
    Student, StudyGroup, Subject, Tariff, Teacher, Test, TestResult,
)

# Titles of the admin site
admin.site.site_header = "Школа підготовки до НМТ"
admin.site.site_title = "Школа НМТ"
admin.site.index_title = "Адміністрування"


# ───────────────────────── Inlines ─────────────────────────
# An inline shows related records on the page of the "parent" record.

class ContractInline(admin.TabularInline):
    model = Contract
    extra = 0
    fields = ["group", "tariff", "payment_plan", "status", "start_date", "end_date", "discount_pct"]
    autocomplete_fields = ["group", "tariff"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("group__subject", "tariff")
    show_change_link = True  # link to open the full contract page


class GroupScheduleInline(admin.TabularInline):
    """Weekly schedule on the group page: one row = one lesson in the week."""
    model = GroupSchedule
    extra = 1  # one empty row, ready for a new day
    fields = ["weekday", "start_time", "end_time"]


class LessonInline(admin.TabularInline):
    model = Lesson
    extra = 0
    fields = ["starts_at", "teacher", "topic", "status"]
    show_change_link = True


class AttendanceInline(admin.TabularInline):
    model = Attendance
    extra = 0
    fields = ["student", "status"]

    def get_formset(self, request, obj=None, **kwargs):
        # Save the current lesson, so we can show only students of its group
        request._current_lesson = obj
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "student":
            lesson = getattr(request, "_current_lesson", None)
            if lesson is None:
                # A new lesson: the group is not saved yet, so we show nobody
                kwargs["queryset"] = Student.objects.none()
            else:
                # Students who have (or had) a contract for this group
                kwargs["queryset"] = Student.objects.filter(contracts__group=lesson.group).distinct()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class TestResultInline(admin.TabularInline):
    model = TestResult
    extra = 0
    fields = ["student", "score"]

    def get_formset(self, request, obj=None, **kwargs):
        request._current_test = obj
        return super().get_formset(request, obj, **kwargs)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "student":
            test = getattr(request, "_current_test", None)
            if test is None:
                kwargs["queryset"] = Student.objects.none()
            elif test.group_id:
                kwargs["queryset"] = Student.objects.filter(contracts__group_id=test.group_id).distinct()
            else:
                kwargs["queryset"] = Student.objects.filter(
                    contracts__group__subject_id=test.subject_id
                ).distinct()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class InvoiceInline(admin.TabularInline):
    """Invoices of a contract. They are created automatically from the payment plan,
    so here we only look at them (to change one invoice, open it with the link)."""
    model = Invoice
    extra = 0
    fields = ["period_month", "amount", "due_date"]
    readonly_fields = ["period_month", "amount", "due_date"]
    show_change_link = True
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False  # new invoices come from the plan, not by hand


class RefundInline(admin.TabularInline):
    model = Refund
    extra = 0
    fields = ["reason", "amount", "status", "created_at", "paid_at"]


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 1  # one empty row, ready for a new payment
    fields = ["amount", "paid_at", "method", "reserve_amount"]


# ───────────────────────── Reference data ─────────────────────────

@admin.register(Subject)
class SubjectAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ["last_name", "first_name", "phone", "rate_per_lesson", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["last_name", "first_name", "phone"]
    list_editable = ["is_active"]  # change it right in the list


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ["last_name", "first_name", "grade", "phone", "parent_phone", "is_active", "registered_at"]
    list_filter = ["grade", "registered_at"]
    search_fields = ["last_name", "first_name", "phone", "parent_name", "parent_phone"]
    inlines = [ContractInline]

    def get_queryset(self, request):
        # One SQL query for the "active" column instead of one query per student
        active = Contract.objects.filter(student=OuterRef("pk"), status=Contract.Status.ACTIVE)
        return super().get_queryset(request).annotate(has_active=Exists(active))

    @admin.display(description="Активний", boolean=True, ordering="has_active")
    def is_active(self, obj):
        return obj.has_active


@admin.register(Tariff)
class TariffAdmin(admin.ModelAdmin):
    list_display = [
        "name", "monthly_price", "lessons_per_month", "has_guarantee",
        "guarantee_min_score", "refund_pct", "is_active",
    ]
    list_filter = ["has_guarantee", "is_active"]
    search_fields = ["name"]
    list_editable = ["is_active"]


# ───────────────────────── Studying ─────────────────────────

@admin.register(StudyGroup)
class StudyGroupAdmin(admin.ModelAdmin):
    list_display = [
        "full_name", "teacher", "level", "schedule", "students_count", "max_students", "start_date", "end_date",
    ]
    list_filter = ["subject", "level", "teacher"]
    search_fields = ["name", "subject__name", "teacher__last_name"]
    autocomplete_fields = ["subject", "teacher"]
    # The schedule goes first: it is short and it is needed more often than the list of lessons
    inlines = [GroupScheduleInline, LessonInline]

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("subject", "teacher")
            .prefetch_related("schedule")  # schedule of all groups in ONE extra query
            .annotate(
                students_count=Count(
                    "contracts", filter=Q(contracts__status=Contract.Status.ACTIVE), distinct=True
                )
            )
        )

    @admin.display(description="Група", ordering="subject__name")
    def full_name(self, obj):
        return obj.full_name

    @admin.display(description="Розклад")
    def schedule(self, obj):
        return obj.schedule_text

    @admin.display(description="Учнів", ordering="students_count")
    def students_count(self, obj):
        return obj.students_count


@admin.register(GroupSchedule)
class GroupScheduleAdmin(admin.ModelAdmin):
    """The schedule of the whole school in one list (it is easy to find free rooms and hours)."""
    list_display = ["weekday", "start_time", "end_time", "group", "teacher"]
    list_filter = ["weekday", "group__subject", "group__teacher"]
    search_fields = ["group__name", "group__subject__name", "group__teacher__last_name"]
    autocomplete_fields = ["group"]
    ordering = ["weekday", "start_time"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("group__subject", "group__teacher")

    @admin.display(description="Викладач", ordering="group__teacher__last_name")
    def teacher(self, obj):
        return obj.group.teacher


@admin.register(Lesson)
class LessonAdmin(admin.ModelAdmin):
    list_display = ["starts_at", "group", "teacher", "topic", "status"]
    list_filter = ["status", "group__subject", "group", "teacher"]
    search_fields = ["group__name", "topic"]
    date_hierarchy = "starts_at"  # quick navigation by year / month / day
    autocomplete_fields = ["group", "teacher"]
    inlines = [AttendanceInline]
    actions = ["mark_all_present"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("group__subject", "teacher")

    @admin.action(description="Відмітити всіх учнів групи присутніми")
    def mark_all_present(self, request, queryset):
        # Create attendance records only where they do not exist yet
        created = 0
        for lesson in queryset.select_related("group"):
            for student in lesson.group.current_students():
                _, was_created = Attendance.objects.get_or_create(
                    lesson=lesson, student=student,
                    defaults={"status": Attendance.Status.PRESENT},
                )
                created += was_created
        self.message_user(request, f"Створено записів відвідуваності: {created}")


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ["lesson", "student", "status"]
    list_filter = ["status", "lesson__group"]
    search_fields = ["student__last_name", "student__first_name", "lesson__group__name"]
    autocomplete_fields = ["lesson", "student"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("lesson__group__subject", "student")


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    list_display = ["title", "subject", "group", "kind", "held_at", "results_count"]
    list_filter = ["kind", "subject", "group"]
    search_fields = ["title"]
    date_hierarchy = "held_at"
    autocomplete_fields = ["subject", "group"]
    inlines = [TestResultInline]

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("subject", "group__subject")
            .annotate(results_count=Count("results"))
        )

    @admin.display(description="Результатів", ordering="results_count")
    def results_count(self, obj):
        return obj.results_count


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    list_display = ["test", "student", "score"]
    list_filter = ["test__kind", "test__subject"]
    search_fields = ["student__last_name", "student__first_name", "test__title"]
    autocomplete_fields = ["test", "student"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("test", "student")


# ───────────────────────── Contracts and finance ─────────────────────────

@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = [
        "id", "student", "group", "tariff", "payment_plan", "status", "start_date", "end_date",
        "discount_pct", "final_nmt_score",
    ]
    list_filter = ["status", "payment_plan", "tariff", "group__subject", "group"]
    search_fields = ["student__last_name", "student__first_name", "group__name"]
    autocomplete_fields = ["student", "group", "tariff"]
    inlines = [InvoiceInline, RefundInline]
    actions = ["sync_invoices"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("student", "group__subject", "tariff")

    @admin.action(description="Оновити графік рахунків за планом оплати")
    def sync_invoices(self, request, queryset):
        # Normally the invoices are updated when a contract is saved.
        # This action is useful for groups without an end date (new months appear over time)
        # and for old contracts created before the automatic schedule.
        created = updated = deleted = 0
        for contract in queryset.select_related("group", "tariff"):
            c, u, d = contract.sync_invoices()
            created, updated, deleted = created + c, updated + u, deleted + d
        self.message_user(request, f"Рахунків створено: {created}, оновлено: {updated}, видалено: {deleted}")


class DebtFilter(admin.SimpleListFilter):
    """Filter invoices by payment state."""
    title = "Стан оплати"
    parameter_name = "debt"

    def lookups(self, request, model_admin):
        return [("debt", "Є борг"), ("paid", "Оплачено повністю")]

    def queryset(self, request, queryset):
        if self.value() == "debt":
            return queryset.filter(paid_sum__lt=F("amount"))
        if self.value() == "paid":
            return queryset.filter(paid_sum__gte=F("amount"))
        return queryset


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ["id", "student", "period_month", "amount", "paid", "debt", "due_date"]
    list_filter = [DebtFilter, "period_month", "contract__group"]
    search_fields = ["contract__student__last_name", "contract__student__first_name"]
    autocomplete_fields = ["contract"]
    inlines = [PaymentInline]

    def get_queryset(self, request):
        # Calculate the paid sum in SQL for all invoices at once
        return (
            super().get_queryset(request)
            .select_related("contract__student", "contract__group__subject")
            .annotate(
                paid_sum=Coalesce(
                    Sum("payments__amount"), Value(Decimal("0")),
                    output_field=DecimalField(max_digits=10, decimal_places=2),
                )
            )
        )

    @admin.display(description="Учень", ordering="contract__student__last_name")
    def student(self, obj):
        return obj.contract.student

    @admin.display(description="Сплачено", ordering="paid_sum")
    def paid(self, obj):
        return obj.paid_sum

    @admin.display(description="Борг")
    def debt(self, obj):
        return max(obj.amount - obj.paid_sum, Decimal("0"))


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["paid_at", "student", "invoice", "amount", "reserve_amount", "method"]
    list_filter = ["method", "paid_at"]
    search_fields = ["invoice__contract__student__last_name", "invoice__contract__student__first_name"]
    date_hierarchy = "paid_at"
    autocomplete_fields = ["invoice"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("invoice__contract__student")

    @admin.display(description="Учень", ordering="invoice__contract__student__last_name")
    def student(self, obj):
        return obj.invoice.contract.student


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ["created_at", "contract", "reason", "amount", "status", "paid_at"]
    list_filter = ["status", "reason"]
    search_fields = ["contract__student__last_name", "contract__student__first_name"]
    autocomplete_fields = ["contract"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("contract__student", "contract__group__subject")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["spent_at", "category", "amount", "comment"]
    list_filter = ["category"]
    search_fields = ["comment"]
    date_hierarchy = "spent_at"


@admin.register(Salary)
class SalaryAdmin(admin.ModelAdmin):
    list_display = ["period_month", "teacher", "lessons_count", "base_amount", "bonus", "total_amount", "paid_at"]
    list_filter = ["teacher", "period_month"]
    search_fields = ["teacher__last_name", "teacher__first_name"]
    autocomplete_fields = ["teacher"]
    actions = ["recalculate"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("teacher")

    @admin.display(description="Разом, грн")
    def total_amount(self, obj):
        return obj.total

    @admin.action(description="Перерахувати з проведених занять")
    def recalculate(self, request, queryset):
        for salary in queryset.select_related("teacher"):
            salary.calculate_from_lessons()
            salary.save()
        self.message_user(request, f"Перераховано записів: {queryset.count()}")