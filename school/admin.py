"""
Django Admin settings for all tables of the school.

For every model we set:
- list_display  - columns in the list
- list_filter   - filters on the right side
- search_fields - search box at the top
- inlines       - related records inside one page (for example, payments inside an invoice)
"""
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.db.models import Count, DecimalField, Exists, F, OuterRef, Q, Sum, Value
from django.db.models.functions import Coalesce

from .access import groups_for, is_office, students_for, subjects_for
from .models import (
    Attendance, Contract, ContractPause, ConversionRow, ConversionTable, DebtCall, Expense, GroupSchedule, Invoice, Lesson,
    Payment, Refund, Salary, Student, StudyGroup, Subject, Tariff, Teacher, Test, TestResult,
    check_conversion_rows, parse_conversion_text,
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
    # NMT tests: write the test score, the score 100-200 is calculated.
    # Topic tests: write only the percent in "Бал".
    fields = ["student", "raw_score", "score"]

    def get_formset(self, request, obj=None, **kwargs):
        request._current_test = obj
        return super().get_formset(request, obj, **kwargs)

    def get_queryset(self, request):
        # A teacher sees results only of his own students
        # (a school-wide test can have students of other teachers)
        qs = super().get_queryset(request)
        if not is_office(request.user):
            qs = qs.filter(student__in=students_for(request.user))
        return qs

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
            if not is_office(request.user):
                kwargs["queryset"] = kwargs["queryset"].filter(pk__in=students_for(request.user))
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


class ContractPauseInline(admin.TabularInline):
    """Pauses of a contract. Usually they are made with the buttons in the student card."""
    model = ContractPause
    extra = 0
    fields = ["start_date", "end_date", "reason"]


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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["last_name", "first_name", "phone", "rate_per_lesson", "user", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["last_name", "first_name", "phone"]
    autocomplete_fields = ["user"]  # choose the login from the list of users
    list_editable = ["is_active"]  # change it right in the list


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = [
        "name", "monthly_price", "subjects_count", "lessons_per_month", "has_guarantee",
        "guarantee_min_score", "refund_pct", "is_active",
    ]
    list_filter = ["has_guarantee", "is_active"]
    search_fields = ["name"]
    list_editable = ["is_active"]


# ───────────────────────── Studying ─────────────────────────

@admin.register(StudyGroup)
class StudyGroupAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = [
        "full_name", "teacher", "level", "schedule", "students_count", "max_students", "start_date", "end_date",
    ]
    list_filter = ["subject", "level", "teacher"]
    search_fields = ["name", "subject__name", "teacher__last_name"]
    autocomplete_fields = ["subject", "teacher"]
    # Only the schedule here. Lessons are created from the schedule automatically
    # (up to ~70 per group), so a list of lessons made this page very long.
    # Lessons are marked in the Journal and edited in "Заняття".
    inlines = [GroupScheduleInline]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # The name is not required in the form: if it is empty, StudyGroup.clean() gives "група N".
        # A user who may only look (the accountant) gets a form without editable fields.
        name_field = form.base_fields.get("name")
        if name_field is not None:
            name_field.required = False
            name_field.help_text = "Можна залишити порожнім — система сама дасть наступний номер: «група 1», «група 2»…"
        return form

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related("subject", "teacher")
            .prefetch_related("schedule")  # schedule of all groups in ONE extra query
            .annotate(
                # The same rule as on the site: active AND suspended students take a place
                students_count=Count(
                    "contracts", filter=Q(contracts__status__in=Contract.OPEN_STATUSES), distinct=True
                )
            )
        )

    def save_related(self, request, form, formsets, change):
        # The schedule (inline) is saved here, after the group itself.
        # Only now we can create the lessons from the new schedule.
        super().save_related(request, form, formsets, change)
        group = form.instance
        created, removed = group.sync_lessons_with_schedule()
        if created or removed:
            text = f"Заняття оновлено за розкладом (до {group.end_date:%d.%m.%Y}): створено {created}"
            if removed:
                text += f", прибрано {removed} запланованих, яких більше немає в розкладі"
            self.message_user(request, text + ".")
        elif not group.schedule.exists():
            self.message_user(
                request, "У групи ще немає розкладу — додайте дні й час, і заняття створяться самі.",
                level=messages.WARNING,
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["starts_at", "group", "teacher", "topic", "status"]
    list_filter = ["status", "group__subject", "group", "teacher"]
    # "математика група 1" finds lessons of "Математика · група 1"
    search_fields = ["group__name", "group__subject__name", "topic"]
    date_hierarchy = "starts_at"  # quick navigation by year / month / day
    autocomplete_fields = ["group", "teacher"]
    # "Перенесено з": only the id with a search button, not a list of thousands of lessons
    raw_id_fields = ["moved_from"]
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["lesson", "student", "status"]
    list_filter = ["status", "lesson__group"]
    search_fields = ["student__last_name", "student__first_name", "lesson__group__name", "lesson__group__subject__name"]
    autocomplete_fields = ["lesson", "student"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("lesson__group__subject", "student")


@admin.register(Test)
class TestAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["title", "subject", "group", "kind", "held_at", "table_used", "results_count"]
    list_filter = ["kind", "subject", "group"]
    search_fields = ["title"]
    date_hierarchy = "held_at"
    autocomplete_fields = ["subject", "group"]
    inlines = [TestResultInline]
    actions = ["recalculate_scores"]

    @admin.display(description="Таблиця переведення")
    def table_used(self, obj):
        if not obj.uses_nmt_scale:
            return "— (відсотки)"
        table = obj.conversion_table
        return f"{table.year} р." if table else "немає (бал вручну)"

    @admin.action(description="Перерахувати бали 100–200 за таблицею переведення")
    def recalculate_scores(self, request, queryset):
        # Scores are a "snapshot": if somebody fixes a row in the table later,
        # old results do not change by themselves. This action updates them.
        changed = sum(test.recalculate_results() for test in queryset.select_related("subject"))
        self.message_user(request, f"Змінено результатів: {changed}")

    def get_queryset(self, request):
        qs = (
            super().get_queryset(request)
            .select_related("subject", "group__subject")
            .annotate(results_count=Count("results"))
        )
        if not is_office(request.user):
            # A teacher: tests of his groups + school-wide tests of his subjects
            qs = qs.filter(
                Q(group__in=groups_for(request.user))
                | Q(group__isnull=True, subject__in=subjects_for(request.user))
            )
        return qs

    # For a teacher we use simple lists instead of autocomplete:
    # autocomplete would need access to ALL groups and subjects
    def get_autocomplete_fields(self, request):
        return self.autocomplete_fields if is_office(request.user) else []

    def get_list_filter(self, request):
        return self.list_filter if is_office(request.user) else ["kind"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if not is_office(request.user):
            if db_field.name == "group":
                kwargs["queryset"] = groups_for(request.user).select_related("subject")
            elif db_field.name == "subject":
                kwargs["queryset"] = subjects_for(request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Результатів", ordering="results_count")
    def results_count(self, obj):
        return obj.results_count


@admin.register(TestResult)
class TestResultAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["test", "student", "raw_score", "result"]
    list_filter = ["test__kind", "test__subject"]
    search_fields = ["student__last_name", "student__first_name", "test__title"]
    autocomplete_fields = ["test", "student"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("test", "student")

    @admin.display(description="Бал", ordering="score")
    def result(self, obj):
        return obj.score_display()

    # This list is only for office workers with the permissions of their role.
    # Teachers enter results inside a test (TestResultInline has its own permissions).
    def has_module_permission(self, request):
        return is_office(request.user) and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return is_office(request.user) and super().has_view_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return is_office(request.user) and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return is_office(request.user) and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return is_office(request.user) and super().has_delete_permission(request, obj)


# ───────────────────────── NMT conversion tables ─────────────────────────

class ConversionRowFormSet(BaseInlineFormSet):
    """Checks the WHOLE table, not only one row: every test score from the threshold
    to the maximum must have a row, and the scores must not go down."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return  # first the user must fix errors in single rows
        # If the table was pasted as text, the rows below will be replaced:
        # the text is checked in ConversionTableForm, so here we do nothing
        if self.data.get("rows_text", "").strip():
            return
        pairs = [
            (form.cleaned_data["raw_score"], form.cleaned_data["scaled_score"])
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        errors = check_conversion_rows(self.instance.threshold_raw, self.instance.max_raw, pairs)
        if errors:
            raise ValidationError(errors)


class ConversionRowInline(admin.TabularInline):
    model = ConversionRow
    formset = ConversionRowFormSet
    extra = 0
    fields = ["raw_score", "scaled_score"]


class ConversionTableForm(forms.ModelForm):
    # Not a database field: a text box to paste the whole table at once
    rows_text = forms.CharField(
        label="Вставити таблицю текстом", required=False,
        widget=forms.Textarea(attrs={"rows": 12, "cols": 40, "placeholder": "5 100\n6 108\n7 115\n…\n32 200"}),
        help_text="Кожен рядок — «тестовий бал  бал 100–200». Можна вставити й два стовпці з PDF "
                  "(«8 100   27 154»). Якщо поле заповнене, воно ЗАМІНИТЬ усі рядки таблиці нижче.",
    )

    class Meta:
        model = ConversionTable
        fields = ["subject", "year", "max_raw", "threshold_raw", "source"]

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("rows_text", "").strip()
        if not text:
            return cleaned
        pairs, errors = parse_conversion_text(text)
        if not errors:
            errors = check_conversion_rows(cleaned.get("threshold_raw"), cleaned.get("max_raw"), pairs)
        if errors:
            raise ValidationError({"rows_text": errors})
        # Remember the rows: they are saved after the table (in save_related)
        self.instance._pasted_rows = pairs
        return cleaned


@admin.register(ConversionTable)
class ConversionTableAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    form = ConversionTableForm
    list_display = ["subject", "year", "threshold_raw", "max_raw", "rows_count", "is_complete", "source"]
    list_filter = ["subject", "year"]
    autocomplete_fields = ["subject"]
    inlines = [ConversionRowInline]
    fieldsets = [
        (None, {"fields": ["subject", "year", "max_raw", "threshold_raw", "source"]}),
        ("Швидке введення", {"fields": ["rows_text"]}),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("subject").prefetch_related("rows")

    @admin.display(description="Рядків")
    def rows_count(self, obj):
        return len(obj.scale)

    @admin.display(description="Повна", boolean=True)
    def is_complete(self, obj):
        return not obj.problems()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        pairs = getattr(form.instance, "_pasted_rows", None)
        if pairs:
            # Replace all rows with the pasted table
            form.instance.rows.all().delete()
            ConversionRow.objects.bulk_create(
                ConversionRow(table=form.instance, raw_score=raw, scaled_score=scaled) for raw, scaled in pairs
            )
            self.message_user(request, f"Таблицю замінено: {len(pairs)} рядків.")


# ───────────────────────── Contracts and finance ─────────────────────────

@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = [
        "id", "student", "group", "tariff", "payment_plan", "status", "start_date", "end_date",
        "discount_pct", "final_nmt_score",
    ]
    list_filter = ["status", "payment_plan", "tariff", "group__subject", "group"]
    search_fields = ["student__last_name", "student__first_name", "group__name", "group__subject__name"]
    autocomplete_fields = ["student", "group", "tariff"]
    inlines = [ContractPauseInline, InvoiceInline, RefundInline]
    actions = ["sync_invoices"]

    def save_related(self, request, form, formsets, change):
        # Pauses (inline) are saved after the contract: only now the invoices can follow them
        super().save_related(request, form, formsets, change)
        form.instance.sync_invoices()

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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["id", "student", "period_month", "amount", "paid", "debt", "due_date"]
    list_filter = [DebtFilter, "period_month", "contract__group"]
    # "Шевченко математика" finds the invoices of Shevchenko for maths
    search_fields = ["contract__student__last_name", "contract__student__first_name", "contract__group__subject__name"]
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
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
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["created_at", "contract", "reason", "amount", "status", "paid_at"]
    list_filter = ["status", "reason"]
    search_fields = ["contract__student__last_name", "contract__student__first_name"]
    autocomplete_fields = ["contract"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("contract__student", "contract__group__subject")


@admin.register(DebtCall)
class DebtCallAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["called_at", "student", "result", "promised_date", "user", "comment"]
    list_filter = ["result", "user"]
    search_fields = ["student__last_name", "student__first_name", "comment"]
    date_hierarchy = "called_at"
    autocomplete_fields = ["student"]

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("student", "user")


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
    list_display = ["spent_at", "category", "amount", "comment"]
    list_filter = ["category"]
    search_fields = ["comment"]
    date_hierarchy = "spent_at"


@admin.register(Salary)
class SalaryAdmin(admin.ModelAdmin):
    save_on_top = True  # «Зберегти» buttons also at the top: no need to scroll down
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