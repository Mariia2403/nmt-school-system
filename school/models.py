"""
Models for the NMT preparation school information system.

The structure follows the agreed database schema.
Table names (db_table) are the same as in the schema.
"""
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

# Common settings for money and percent fields
MONEY = {"max_digits": 10, "decimal_places": 2}      # up to 99 999 999.99
PERCENT = {"max_digits": 5, "decimal_places": 2}     # up to 999.99, but we limit it to 0-100
PERCENT_VALIDATORS = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))]
NMT_SCORE_VALIDATORS = [MinValueValidator(100), MaxValueValidator(200)]  # official NMT scale


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

    def __str__(self):
        return f"{self.last_name} {self.first_name}"


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

    class Meta:
        db_table = "students"
        verbose_name = "Учень"
        verbose_name_plural = "Учні"
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name} {self.first_name}"


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

    def __str__(self):
        return f"{self.name} ({self.subject})"


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
    # The real teacher of this lesson (can be different from the group teacher)
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="lessons", verbose_name="Викладач"
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


class Test(models.Model):
    class Kind(models.TextChoices):
        DIAGNOSTIC = "diagnostic", "Діагностичний (вхідний)"
        TOPIC = "topic", "Тематичний"
        TRIAL = "trial", "Пробне НМТ"
        FINAL = "final", "Підсумковий"

    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="tests", verbose_name="Предмет"
    )
    group = models.ForeignKey(
        StudyGroup, on_delete=models.CASCADE, related_name="tests", verbose_name="Група"
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
        # One student can have only one result for one test
        constraints = [
            models.UniqueConstraint(fields=["test", "student"], name="unique_test_result_test_student"),
        ]

    def __str__(self):
        return f"{self.student} — {self.test.title}: {self.score}"


# ───────────────────────── Contracts and finance ─────────────────────────

class Tariff(models.Model):
    name = models.CharField("Назва", max_length=100)
    monthly_price = models.DecimalField("Ціна за місяць, грн", **MONEY)
    lessons_per_month = models.PositiveIntegerField("Занять на місяць")
    has_guarantee = models.BooleanField("Гарантія результату", default=False)
    # Empty if the tariff has no guarantee
    guarantee_min_score = models.IntegerField(
        "Гарантований мін. бал НМТ", null=True, blank=True, validators=NMT_SCORE_VALIDATORS
    )
    refund_pct = models.DecimalField(
        "Відсоток повернення", default=Decimal("0"), validators=PERCENT_VALIDATORS, **PERCENT
    )
    is_active = models.BooleanField("Діючий", default=True)

    class Meta:
        db_table = "tariffs"
        verbose_name = "Тариф"
        verbose_name_plural = "Тарифи"
        ordering = ["monthly_price"]

    def __str__(self):
        return f"{self.name} — {self.monthly_price} грн/міс"


class Contract(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Діючий"
        SUSPENDED = "suspended", "Призупинений"
        COMPLETED = "completed", "Завершений"
        TERMINATED = "terminated", "Розірваний"

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

    def __str__(self):
        return f"Договір №{self.pk} — {self.student} ({self.group.name})"


class Invoice(models.Model):
    contract = models.ForeignKey(
        Contract, on_delete=models.PROTECT, related_name="invoices", verbose_name="Договір"
    )
    # We save the first day of the month, for example 2026-10-01
    period_month = models.DateField("Місяць", help_text="Перше число місяця, за який рахунок")
    amount = models.DecimalField("Сума, грн", **MONEY)
    due_date = models.DateField("Сплатити до")

    class Meta:
        db_table = "invoices"
        verbose_name = "Рахунок"
        verbose_name_plural = "Рахунки"
        ordering = ["-period_month"]

    def __str__(self):
        return f"Рахунок №{self.pk} — {self.contract.student}, {self.period_month:%m.%Y}"


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
    reserve_amount = models.DecimalField(
        "Сума в резерв, грн", default=Decimal("0"), **MONEY,
        help_text="Частина оплати, відкладена в резерв (наприклад, під можливе повернення за гарантією)",
    )

    class Meta:
        db_table = "payments"
        verbose_name = "Оплата"
        verbose_name_plural = "Оплати"
        ordering = ["-paid_at"]

    def __str__(self):
        return f"{self.amount} грн — {self.invoice.contract.student} ({timezone.localtime(self.paid_at):%d.%m.%Y})"


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

    def __str__(self):
        return f"{self.get_category_display()} — {self.amount} грн ({self.spent_at:%d.%m.%Y})"


class Salary(models.Model):
    teacher = models.ForeignKey(
        Teacher, on_delete=models.PROTECT, related_name="salaries", verbose_name="Викладач"
    )
    period_month = models.DateField("Місяць", help_text="Перше число місяця")
    # We save these numbers at the moment of calculation,
    # because the teacher's rate can change later.
    lessons_count = models.PositiveIntegerField("Кількість занять", default=0)
    base_amount = models.DecimalField("Базова сума, грн", **MONEY)
    bonus = models.DecimalField("Премія, грн", default=Decimal("0"), **MONEY)
    paid_at = models.DateField("Дата виплати", null=True, blank=True)

    class Meta:
        db_table = "salaries"
        verbose_name = "Зарплата"
        verbose_name_plural = "Зарплати"
        ordering = ["-period_month"]

    def __str__(self):
        return f"{self.teacher} — {self.period_month:%m.%Y}"

    @property
    def total(self):
        """Total amount to pay: base amount + bonus."""
        return (self.base_amount or 0) + (self.bonus or 0)