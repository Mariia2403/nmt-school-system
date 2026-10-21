"""
Models for the NMT preparation school information system.

The structure follows the agreed database schema.
Table names (db_table) are the same as in the schema.

We protect data on two levels:
1. CheckConstraint / UniqueConstraint - rules inside PostgreSQL.
   They always work, even for scripts and bulk imports.
2. clean() methods - more complex checks with clear error messages.
   They work in forms (Django Admin and our own pages).

Some fields are calculated automatically if you leave them empty
(invoice amount, salary, reserve). We use the method _fill_defaults() for this.
Note: do not name it _prepare() - Django already uses this name inside models.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q, Sum
from django.utils import timezone

# Common settings for money and percent fields
MONEY = {"max_digits": 10, "decimal_places": 2}      # up to 99 999 999.99
PERCENT = {"max_digits": 5, "decimal_places": 2}     # we limit it to 0-100
PERCENT_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]
NMT_SCORE_VALIDATORS = [MinValueValidator(100), MaxValueValidator(200)]  # official NMT scale


def first_day_of_month(value):
    """Return the first day of the month: 2026-10-17 -> 2026-10-01."""
    return value.replace(day=1) if value else value


def money_round(value):
    """Round money to 2 digits after the point: 123.456 -> 123.46."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ───────────────────────── Reference data ─────────────────────────

class Subject(models.Model):
    name = models.CharField("Назва", max_length=100)

    class Meta:
        db_table = "subjects"
        verbose_name = "Предмет"
        verbose_name_plural = "Предмети"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Teacher(models.Model):
    last_name = models.CharField("Прізвище", max_length=50)
    first_name = models.CharField("Ім'я", max_length=50)
    phone = models.CharField("Телефон", max_length=20, blank=True)
    rate_per_lesson = models.DecimalField("Ставка за заняття, грн", **MONEY)
    is_active = models.BooleanField("Працює", default=True)

    class Meta:
        db_table = "teachers"
        verbose_name = "Викладач"
        verbose_name_plural = "Викладачі"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(rate_per_lesson__gte=0),
                name="teachers_rate_not_negative",
                violation_error_message="Ставка не може бути від'ємною.",
            ),
        ]

    def __str__(self):
        return f"{self.last_name} {self.first_name}"


class StudentQuerySet(models.QuerySet):
    def active(self):
        """Students who have at least one active contract.
        This is the ONLY definition of an "active student" in the project."""
        return self.filter(contracts__status=Contract.Status.ACTIVE).distinct()


class Student(models.Model):
    last_name = models.CharField("Прізвище", max_length=50)
    first_name = models.CharField("Ім'я", max_length=50)
    phone = models.CharField("Телефон", max_length=20, blank=True)
    grade = models.IntegerField(
        "Клас", validators=[MinValueValidator(1), MaxValueValidator(11)]
    )
    parent_name = models.CharField("ПІБ батьків", max_length=150, blank=True)
    parent_phone = models.CharField("Телефон батьків", max_length=20, blank=True)
    registered_at = models.DateField("Дата реєстрації", default=timezone.localdate)

    # Now we can write Student.objects.active()
    objects = StudentQuerySet.as_manager()

    class Meta:
        db_table = "students"
        verbose_name = "Учень"
        verbose_name_plural = "Учні"
        ordering = ["last_name", "first_name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(grade__gte=1, grade__lte=11),
                name="students_grade_1_11",
                violation_error_message="Клас має бути від 1 до 11.",
            ),
        ]

    def __str__(self):
        return f"{self.last_name} {self.first_name}"

    @property
    def has_active_contract(self):
        return self.contracts.filter(status=Contract.Status.ACTIVE).exists()


# ───────────────────────── Studying ─────────────────────────

class StudyGroup(models.Model):
    # The value on the left is saved in the database,
    # the text on the right is shown to the user.
    class Level(models.TextChoices):
        BASIC = "basic", "Базовий"
        MEDIUM = "medium", "Середній"
        ADVANCED = "advanced", "Поглиблений"
        INTENSIVE = "intensive", "Інтенсив"

    name = models.CharField("Назва групи", max_length=50)
    # PROTECT: you cannot delete a subject or a teacher if a group uses it
    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="groups", verbose_name="Предмет"
    )
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="groups", verbose_name="Викладач"
    )
    level = models.CharField("Рівень", max_length=20, choices=Level.choices, default=Level.MEDIUM)
    start_date = models.DateField("Дата початку")
    end_date = models.DateField("Дата завершення", null=True, blank=True)  # can be unknown
    max_students = models.PositiveIntegerField("Макс. учнів", default=12)

    class Meta:
        db_table = "study_groups"
        verbose_name = "Група"
        verbose_name_plural = "Групи"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="study_groups_end_after_start",
                violation_error_message="Дата завершення не може бути раніше дати початку.",
            ),
            models.CheckConstraint(
                condition=Q(max_students__gte=1),
                name="study_groups_max_students_positive",
                violation_error_message="У групі має бути хоча б одне місце.",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.subject})"

    def current_students(self):
        """Students of the group = students with an active contract for this group."""
        return Student.objects.filter(
            contracts__group=self, contracts__status=Contract.Status.ACTIVE
        ).distinct()

    @property
    def free_places(self):
        return self.max_students - self.current_students().count()


class Lesson(models.Model):
    class Status(models.TextChoices):
        PLANNED = "planned", "Заплановане"
        HELD = "held", "Проведене"
        CANCELLED = "cancelled", "Скасоване"
        RESCHEDULED = "rescheduled", "Перенесене"

    # CASCADE: if a group is deleted, its lessons are deleted too
    group = models.ForeignKey(
        StudyGroup, on_delete=models.CASCADE, related_name="lessons", verbose_name="Група"
    )
    # The real teacher of this lesson (for example, a substitute teacher).
    # If it is empty, we take the group teacher automatically.
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="lessons", verbose_name="Викладач",
        blank=True, help_text="Якщо не вказати — підставиться викладач групи",
    )
    starts_at = models.DateTimeField("Дата і час початку")
    topic = models.CharField("Тема", max_length=200, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PLANNED)

    class Meta:
        db_table = "lessons"
        verbose_name = "Заняття"
        verbose_name_plural = "Заняття"
        ordering = ["-starts_at"]  # newest lessons first

    def __str__(self):
        # Show the time in Kyiv time zone, not in UTC
        when = timezone.localtime(self.starts_at).strftime("%d.%m.%Y %H:%M") if self.starts_at else "—"
        return f"{self.group.name} — {when}"

    def _fill_defaults(self):
        if self.teacher_id is None and self.group_id is not None:
            self.teacher_id = self.group.teacher_id

    def clean(self):
        self._fill_defaults()

    def save(self, *args, **kwargs):
        self._fill_defaults()  # also works when we create lessons in code
        super().save(*args, **kwargs)


class Attendance(models.Model):
    class Status(models.TextChoices):
        PRESENT = "present", "Присутній"
        ABSENT = "absent", "Відсутній"
        LATE = "late", "Запізнився"
        EXCUSED = "excused", "Відсутній (поважна причина)"

    lesson = models.ForeignKey(
        Lesson, on_delete=models.CASCADE, related_name="attendance", verbose_name="Заняття"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="attendance", verbose_name="Учень"
    )
    status = models.CharField("Статус", max_length=10, choices=Status.choices, default=Status.PRESENT)

    class Meta:
        db_table = "attendance"
        verbose_name = "Відвідуваність"
        verbose_name_plural = "Відвідуваність"
        # Django uses its own id, so we add this rule instead of a composite key:
        # one student can have only one attendance record for one lesson.
        constraints = [
            models.UniqueConstraint(fields=["lesson", "student"], name="unique_attendance_lesson_student"),
        ]

    def __str__(self):
        return f"{self.student} — {self.lesson}: {self.get_status_display()}"

    def clean(self):
        # The student must have a contract (any status) for the group of this lesson.
        # We allow old contracts too, so history stays correct.
        if self.lesson_id and self.student_id:
            in_group = Contract.objects.filter(
                student_id=self.student_id, group_id=self.lesson.group_id
            ).exists()
            if not in_group:
                raise ValidationError({"student": "Цей учень не навчається в групі цього заняття."})


class Test(models.Model):
    class Kind(models.TextChoices):
        DIAGNOSTIC = "diagnostic", "Діагностичний (вхідний)"
        TOPIC = "topic", "Тематичний"
        TRIAL = "trial", "Пробне НМТ"
        FINAL = "final", "Підсумковий"

    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="tests", verbose_name="Предмет"
    )
    # Empty group = a school-wide test for all groups of this subject
    group = models.ForeignKey(
        StudyGroup, on_delete=models.CASCADE, related_name="tests", verbose_name="Група",
        null=True, blank=True, help_text="Залиште порожнім для тесту всіх груп предмета",
    )
    title = models.CharField("Назва", max_length=150)
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices, default=Kind.TOPIC)
    held_at = models.DateField("Дата проведення")

    class Meta:
        db_table = "tests"
        verbose_name = "Тест"
        verbose_name_plural = "Тести"
        ordering = ["-held_at"]

    def __str__(self):
        return f"{self.title} ({self.held_at:%d.%m.%Y})"

    def clean(self):
        if self.group_id and self.subject_id and self.group.subject_id != self.subject_id:
            raise ValidationError({"group": "Предмет групи не збігається з предметом тесту."})


class TestResult(models.Model):
    test = models.ForeignKey(
        Test, on_delete=models.CASCADE, related_name="results", verbose_name="Тест"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="test_results", verbose_name="Учень"
    )
    # 0-200 because school tests can use their own scale, not only the NMT scale
    score = models.IntegerField(
        "Бал", validators=[MinValueValidator(0), MaxValueValidator(200)]
    )

    class Meta:
        db_table = "test_results"
        verbose_name = "Результат тесту"
        verbose_name_plural = "Результати тестів"
        constraints = [
            # One student can have only one result for one test
            models.UniqueConstraint(fields=["test", "student"], name="unique_test_result_test_student"),
            models.CheckConstraint(
                condition=Q(score__gte=0, score__lte=200),
                name="test_results_score_0_200",
                violation_error_message="Бал має бути від 0 до 200.",
            ),
        ]

    def __str__(self):
        return f"{self.student} — {self.test.title}: {self.score}"

    def clean(self):
        if not (self.test_id and self.student_id):
            return
        contracts = Contract.objects.filter(student_id=self.student_id)
        if self.test.group_id:
            # Group test: the student must study in this group
            ok = contracts.filter(group_id=self.test.group_id).exists()
        else:
            # School-wide test: the student must study this subject in any group
            ok = contracts.filter(group__subject_id=self.test.subject_id).exists()
        if not ok:
            raise ValidationError({"student": "Цей учень не навчається в групі (або на предметі) цього тесту."})


# ───────────────────────── Contracts and finance ─────────────────────────

class Tariff(models.Model):
    name = models.CharField("Назва", max_length=100)
    monthly_price = models.DecimalField("Ціна за місяць, грн", **MONEY)
    lessons_per_month = models.PositiveIntegerField("Занять на місяць")
    has_guarantee = models.BooleanField("Гарантія результату", default=False)
    # Required only if the tariff has a guarantee
    guarantee_min_score = models.IntegerField(
        "Гарантований мін. бал НМТ", null=True, blank=True, validators=NMT_SCORE_VALIDATORS
    )
    # How much money the school returns if the student does not get the guaranteed score.
    # We also use this percent to calculate the reserve in every payment.
    refund_pct = models.DecimalField(
        "Відсоток повернення", default=Decimal("0"), validators=PERCENT_VALIDATORS, **PERCENT
    )
    is_active = models.BooleanField("Діючий", default=True)

    class Meta:
        db_table = "tariffs"
        verbose_name = "Тариф"
        verbose_name_plural = "Тарифи"
        ordering = ["monthly_price"]
        constraints = [
            models.CheckConstraint(
                condition=Q(monthly_price__gte=0),
                name="tariffs_price_not_negative",
                violation_error_message="Ціна не може бути від'ємною.",
            ),
            models.CheckConstraint(
                condition=Q(refund_pct__gte=0, refund_pct__lte=100),
                name="tariffs_refund_pct_0_100",
                violation_error_message="Відсоток повернення має бути від 0 до 100.",
            ),
            models.CheckConstraint(
                condition=Q(has_guarantee=False)
                | Q(guarantee_min_score__gte=100, guarantee_min_score__lte=200),
                name="tariffs_guarantee_needs_score",
                violation_error_message="Для тарифу з гарантією вкажіть мінімальний бал НМТ (100–200).",
            ),
        ]

    def __str__(self):
        return f"{self.name} — {self.monthly_price} грн/міс"


class Contract(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Діючий"
        SUSPENDED = "suspended", "Призупинений"
        COMPLETED = "completed", "Завершений"
        TERMINATED = "terminated", "Розірваний"

    # Statuses when a student "takes a place" in a group
    OPEN_STATUSES = [Status.ACTIVE, Status.SUSPENDED]

    # PROTECT everywhere: financial data must not disappear by accident
    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="contracts", verbose_name="Учень"
    )
    group = models.ForeignKey(
        StudyGroup, on_delete=models.PROTECT, related_name="contracts", verbose_name="Група"
    )
    tariff = models.ForeignKey(
        Tariff, on_delete=models.PROTECT, related_name="contracts", verbose_name="Тариф"
    )
    start_date = models.DateField("Дата початку")
    end_date = models.DateField("Дата завершення", null=True, blank=True)
    discount_pct = models.DecimalField(
        "Знижка, %", default=Decimal("0"), validators=PERCENT_VALIDATORS, **PERCENT
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.ACTIVE)
    # We know this score only after the student takes the NMT
    final_nmt_score = models.IntegerField(
        "Підсумковий бал НМТ", null=True, blank=True, validators=NMT_SCORE_VALIDATORS
    )

    class Meta:
        db_table = "contracts"
        verbose_name = "Договір"
        verbose_name_plural = "Договори"
        ordering = ["-start_date"]
        constraints = [
            # A student cannot have two open contracts for the same group
            models.UniqueConstraint(
                fields=["student", "group"],
                condition=Q(status__in=["active", "suspended"]),
                name="contracts_one_open_per_student_group",
                violation_error_message="У цього учня вже є діючий договір у цій групі.",
            ),
            models.CheckConstraint(
                condition=Q(discount_pct__gte=0, discount_pct__lte=100),
                name="contracts_discount_0_100",
                violation_error_message="Знижка має бути від 0 до 100%.",
            ),
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="contracts_end_after_start",
                violation_error_message="Дата завершення не може бути раніше дати початку.",
            ),
            models.CheckConstraint(
                condition=Q(final_nmt_score__isnull=True)
                | Q(final_nmt_score__gte=100, final_nmt_score__lte=200),
                name="contracts_final_score_100_200",
                violation_error_message="Бал НМТ має бути від 100 до 200.",
            ),
        ]

    def __str__(self):
        return f"Договір №{self.pk} — {self.student} ({self.group.name})"

    def clean(self):
        # A new contract cannot use an old (inactive) tariff
        if self._state.adding and self.tariff_id and not self.tariff.is_active:
            raise ValidationError({"tariff": "Цей тариф більше не діє."})

        # Check that the group is not full
        if self.group_id and self.status in self.OPEN_STATUSES:
            taken = (
                Contract.objects.filter(group_id=self.group_id, status__in=self.OPEN_STATUSES)
                .exclude(pk=self.pk)
                .count()
            )
            if taken >= self.group.max_students:
                raise ValidationError({"group": "У групі немає вільних місць."})

    def monthly_amount(self):
        """Price for one month with the discount."""
        price = self.tariff.monthly_price
        return money_round(price * (Decimal("100") - self.discount_pct) / Decimal("100"))


class Invoice(models.Model):
    contract = models.ForeignKey(
        Contract, on_delete=models.PROTECT, related_name="invoices", verbose_name="Договір"
    )
    # We always save the first day of the month, for example 2026-10-01
    period_month = models.DateField("Місяць", help_text="Будь-яка дата місяця — збережеться 1-ше число")
    # A "snapshot": the amount stays the same even if the tariff price changes later
    amount = models.DecimalField(
        "Сума, грн", blank=True, **MONEY,
        help_text="Якщо не вказати — порахується з тарифу і знижки",
    )
    due_date = models.DateField("Сплатити до")

    class Meta:
        db_table = "invoices"
        verbose_name = "Рахунок"
        verbose_name_plural = "Рахунки"
        ordering = ["-period_month"]
        constraints = [
            # Only one invoice for one contract for one month
            models.UniqueConstraint(fields=["contract", "period_month"], name="invoices_one_per_month"),
            models.CheckConstraint(
                condition=Q(amount__gte=0),
                name="invoices_amount_not_negative",
                violation_error_message="Сума не може бути від'ємною.",
            ),
        ]

    def __str__(self):
        return f"Рахунок №{self.pk} — {self.contract.student}, {self.period_month:%m.%Y}"

    def _fill_defaults(self):
        self.period_month = first_day_of_month(self.period_month)
        if self.amount is None and self.contract_id:
            self.amount = self.contract.monthly_amount()

    def clean(self):
        self._fill_defaults()

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)

    @property
    def paid_total(self):
        """How much money was paid for this invoice."""
        return self.payments.aggregate(total=Sum("amount"))["total"] or Decimal("0")

    @property
    def debt(self):
        """How much money is still not paid (0 if everything is paid)."""
        return max(self.amount - self.paid_total, Decimal("0"))


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "cash", "Готівка"
        CARD = "card", "Картка (термінал)"
        TRANSFER = "transfer", "Банківський переказ"
        ONLINE = "online", "Онлайн-оплата"

    # One invoice can have several payments (for example, a student pays in two parts)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="payments", verbose_name="Рахунок"
    )
    amount = models.DecimalField("Сума, грн", **MONEY)
    paid_at = models.DateTimeField("Дата і час оплати", default=timezone.now)
    method = models.CharField("Спосіб оплати", max_length=20, choices=Method.choices, default=Method.CARD)
    # Reserve = money the school keeps "just in case":
    # if the student does not get the guaranteed NMT score, the school returns part of the money,
    # and the teachers' salary for this student is still covered.
    reserve_amount = models.DecimalField(
        "Сума в резерв, грн", blank=True, **MONEY,
        help_text="Резерв під можливе повернення за гарантією. Якщо не вказати — "
                  "порахується: сума оплати × відсоток повернення тарифу (для тарифів без гарантії — 0)",
    )

    class Meta:
        db_table = "payments"
        verbose_name = "Оплата"
        verbose_name_plural = "Оплати"
        ordering = ["-paid_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="payments_amount_positive",
                violation_error_message="Сума оплати має бути більшою за 0.",
            ),
            models.CheckConstraint(
                condition=Q(reserve_amount__gte=0, reserve_amount__lte=F("amount")),
                name="payments_reserve_within_amount",
                violation_error_message="Резерв має бути від 0 до суми оплати.",
            ),
        ]

    def __str__(self):
        return f"{self.amount} грн — {self.invoice.contract.student} ({timezone.localtime(self.paid_at):%d.%m.%Y})"

    def calculate_reserve(self):
        """Reserve = payment amount × refund percent of the tariff (only for guarantee tariffs)."""
        tariff = self.invoice.contract.tariff
        if not tariff.has_guarantee:
            return Decimal("0")
        return money_round(self.amount * tariff.refund_pct / Decimal("100"))

    def _fill_defaults(self):
        if self.reserve_amount is None and self.invoice_id and self.amount is not None:
            self.reserve_amount = self.calculate_reserve()

    def clean(self):
        self._fill_defaults()

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)


class Refund(models.Model):
    class Reason(models.TextChoices):
        GUARANTEE = "guarantee", "Гарантія (бал НМТ нижче гарантованого)"
        EARLY_TERMINATION = "early_termination", "Дострокове розірвання договору"
        OVERPAYMENT = "overpayment", "Переплата"
        OTHER = "other", "Інше"

    class Status(models.TextChoices):
        PENDING = "pending", "Очікує розгляду"
        APPROVED = "approved", "Схвалено"
        REJECTED = "rejected", "Відхилено"
        PAID = "paid", "Виплачено"

    contract = models.ForeignKey(
        Contract, on_delete=models.PROTECT, related_name="refunds", verbose_name="Договір"
    )
    reason = models.CharField("Причина", max_length=30, choices=Reason.choices)
    amount = models.DecimalField("Сума, грн", **MONEY)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateField("Дата заявки", default=timezone.localdate)
    paid_at = models.DateField("Дата виплати", null=True, blank=True)  # empty until the money is paid

    class Meta:
        db_table = "refunds"
        verbose_name = "Повернення коштів"
        verbose_name_plural = "Повернення коштів"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="refunds_amount_positive",
                violation_error_message="Сума повернення має бути більшою за 0.",
            ),
            # If the status is "paid", the payment date is required
            models.CheckConstraint(
                condition=~Q(status="paid") | Q(paid_at__isnull=False),
                name="refunds_paid_needs_date",
                violation_error_message="Для статусу «Виплачено» вкажіть дату виплати.",
            ),
        ]

    def __str__(self):
        return f"Повернення {self.amount} грн — {self.contract.student} ({self.get_status_display()})"


class Expense(models.Model):
    class Category(models.TextChoices):
        RENT = "rent", "Оренда"
        UTILITIES = "utilities", "Комунальні послуги"
        MATERIALS = "materials", "Навчальні матеріали"
        MARKETING = "marketing", "Реклама і маркетинг"
        EQUIPMENT = "equipment", "Обладнання"
        SOFTWARE = "software", "Програмне забезпечення"
        OTHER = "other", "Інше"

    category = models.CharField("Категорія", max_length=30, choices=Category.choices)
    amount = models.DecimalField("Сума, грн", **MONEY)
    spent_at = models.DateField("Дата", default=timezone.localdate)
    comment = models.TextField("Коментар", blank=True)

    class Meta:
        db_table = "expenses"
        verbose_name = "Витрата"
        verbose_name_plural = "Витрати"
        ordering = ["-spent_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="expenses_amount_positive",
                violation_error_message="Сума витрати має бути більшою за 0.",
            ),
        ]

    def __str__(self):
        return f"{self.get_category_display()} — {self.amount} грн ({self.spent_at:%d.%m.%Y})"


class Salary(models.Model):
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="salaries", verbose_name="Викладач"
    )
    period_month = models.DateField("Місяць", help_text="Будь-яка дата місяця — збережеться 1-ше число")
    # A "snapshot" at the moment of calculation,
    # because the teacher's rate can change later.
    lessons_count = models.PositiveIntegerField("Кількість занять", default=0)
    base_amount = models.DecimalField(
        "Базова сума, грн", blank=True, **MONEY,
        help_text="Якщо не вказати — порахується: проведені заняття × ставка",
    )
    bonus = models.DecimalField("Премія, грн", default=Decimal("0"), **MONEY)
    paid_at = models.DateField("Дата виплати", null=True, blank=True)

    class Meta:
        db_table = "salaries"
        verbose_name = "Зарплата"
        verbose_name_plural = "Зарплати"
        ordering = ["-period_month"]
        constraints = [
            # Only one salary record for one teacher for one month
            models.UniqueConstraint(fields=["teacher", "period_month"], name="salaries_one_per_month"),
            models.CheckConstraint(
                condition=Q(base_amount__gte=0, bonus__gte=0),
                name="salaries_amounts_not_negative",
                violation_error_message="Суми не можуть бути від'ємними.",
            ),
        ]

    def __str__(self):
        return f"{self.teacher} — {self.period_month:%m.%Y}"

    def calculate_from_lessons(self):
        """Count held lessons of the teacher in this month and multiply by the rate."""
        self.lessons_count = Lesson.objects.filter(
            teacher_id=self.teacher_id,
            status=Lesson.Status.HELD,
            starts_at__year=self.period_month.year,
            starts_at__month=self.period_month.month,
        ).count()
        self.base_amount = self.teacher.rate_per_lesson * self.lessons_count

    def _fill_defaults(self):
        self.period_month = first_day_of_month(self.period_month)
        if self.base_amount is None and self.teacher_id and self.period_month:
            self.calculate_from_lessons()

    def clean(self):
        self._fill_defaults()

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)

    @property
    def total(self):
        """Total amount to pay: base amount + bonus."""
        return (self.base_amount or 0) + (self.bonus or 0)