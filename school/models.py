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
import re
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
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


def next_month(value):
    """First day of the next month: 2026-12-01 -> 2027-01-01."""
    return value.replace(year=value.year + (value.month == 12), month=value.month % 12 + 1, day=1)


def months_between(start, end):
    """List of first days of all months from start to end (both included).
    2026-09-08 .. 2026-11-30 -> [2026-09-01, 2026-10-01, 2026-11-01]"""
    result = []
    month = first_day_of_month(start)
    while month <= end:
        result.append(month)
        month = next_month(month)
    return result


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
    # The login of this teacher on the website (can be empty: not every teacher uses the system).
    # SET_NULL: if the login is deleted, the teacher and his lessons and salaries stay.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="teacher_profile", verbose_name="Обліковий запис",
        help_text="Логін, під яким викладач входить у систему. Він бачитиме лише свої групи.",
    )
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
        # "Intensive" was removed: it was easy to confuse with the tariff name

    # A short name inside the subject, for example "група 1".
    # The full name "Математика · група 1" is built in full_name,
    # so we do not repeat the subject in this field.
    name = models.CharField("Назва групи", max_length=50, help_text="Наприклад: група 1")
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
        # Sort by subject first: "група 1" exists in every subject
        ordering = ["subject__name", "name"]
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
        return self.full_name

    @property
    def full_name(self):
        """Clear name for lists and journals: "Математика · група 1".
        Tip: load groups with select_related("subject"), otherwise every name is one more SQL query."""
        return f"{self.subject.name} · {self.name}"

    @property
    def schedule_text(self):
        """Schedule in one line: "Пн, Ср 18:00–19:30".
        Days with the same time are joined: Mon and Wed at 18:00 -> "Пн, Ср 18:00–19:30".
        Tip: load groups with prefetch_related("schedule") for lists."""
        slots = sorted(self.schedule.all(), key=lambda s: (s.weekday, s.start_time))
        if not slots:
            return "розклад не задано"
        by_time = {}  # (start, end) -> ["Пн", "Ср"]
        for slot in slots:
            by_time.setdefault((slot.start_time, slot.end_time), []).append(slot.short_day)
        parts = [f"{', '.join(days)} {start:%H:%M}–{end:%H:%M}" for (start, end), days in by_time.items()]
        return "; ".join(parts)

    def current_students(self):
        """Students of the group = students with an active contract for this group."""
        return Student.objects.filter(
            contracts__group=self, contracts__status=Contract.Status.ACTIVE
        ).distinct()

    @property
    def free_places(self):
        return self.max_students - self.current_students().count()


class GroupSchedule(models.Model):
    """Regular weekly schedule of a group: one row = one lesson in the week.
    Example: group 1, Monday, 18:00-19:30."""

    # Numbers like in Python date.isoweekday(): Monday = 1 ... Sunday = 7
    class Weekday(models.IntegerChoices):
        MONDAY = 1, "Понеділок"
        TUESDAY = 2, "Вівторок"
        WEDNESDAY = 3, "Середа"
        THURSDAY = 4, "Четвер"
        FRIDAY = 5, "П'ятниця"
        SATURDAY = 6, "Субота"
        SUNDAY = 7, "Неділя"

    SHORT_DAYS = {1: "Пн", 2: "Вт", 3: "Ср", 4: "Чт", 5: "Пт", 6: "Сб", 7: "Нд"}

    # CASCADE: if a group is deleted, its schedule is deleted too
    group = models.ForeignKey(
        StudyGroup, on_delete=models.CASCADE, related_name="schedule", verbose_name="Група"
    )
    weekday = models.PositiveSmallIntegerField("День тижня", choices=Weekday.choices)
    start_time = models.TimeField("Початок")
    end_time = models.TimeField("Кінець")

    class Meta:
        db_table = "group_schedule"
        verbose_name = "Розклад групи"
        verbose_name_plural = "Розклад груп"
        ordering = ["group", "weekday", "start_time"]
        constraints = [
            # A group cannot have two lessons on the same day at the same time
            models.UniqueConstraint(
                fields=["group", "weekday", "start_time"],
                name="group_schedule_one_slot",
                violation_error_message="У цієї групи вже є заняття в цей день о цій годині.",
            ),
            models.CheckConstraint(
                condition=Q(weekday__gte=1, weekday__lte=7),
                name="group_schedule_weekday_1_7",
                violation_error_message="День тижня має бути від 1 (понеділок) до 7 (неділя).",
            ),
            models.CheckConstraint(
                condition=Q(end_time__gt=F("start_time")),
                name="group_schedule_end_after_start",
                violation_error_message="Заняття має закінчуватися пізніше, ніж починається.",
            ),
        ]

    def __str__(self):
        return f"{self.group.full_name}: {self.short_day} {self.start_time:%H:%M}–{self.end_time:%H:%M}"

    @property
    def short_day(self):
        return self.SHORT_DAYS.get(self.weekday, "?")

    def clean(self):
        # Two lessons of one group must not overlap: 18:00-19:30 and 19:00-20:30 on the same day.
        # (The database rule above only stops exactly the same start time.)
        if not (self.group_id and self.weekday and self.start_time and self.end_time):
            return
        overlap = (
            GroupSchedule.objects
            .filter(group_id=self.group_id, weekday=self.weekday,
                    start_time__lt=self.end_time, end_time__gt=self.start_time)
            .exclude(pk=self.pk)
            .exists()
        )
        if overlap:
            raise ValidationError("Цей час перетинається з іншим заняттям групи в цей день.")


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
        return f"{self.group.full_name} — {when}"

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


# ───────────────────────── NMT conversion tables ─────────────────────────

def parse_conversion_text(text):
    """Read a pasted table: every line has pairs "test score  NMT score".
    All of these work:
        5 100
        5;100
        5 → 100
        8 100   27 154      (two columns copied from a PDF: two pairs in one line)
    Returns (pairs, errors): pairs = [(5, 100), ...]."""
    pairs, errors = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        values = [int(v) for v in re.findall(r"\d+", line)]
        if not values:
            continue  # an empty line or a line with words only (a header)
        if len(values) % 2:
            errors.append(f"Рядок {number}: «{line.strip()}» — потрібні пари чисел «тестовий бал — бал 100–200».")
            continue
        pairs.extend(zip(values[0::2], values[1::2]))
    return pairs, errors


def check_conversion_rows(threshold_raw, max_raw, pairs):
    """Check a full table. Returns a list of errors (an empty list = the table is good).
    Rules: one row for EVERY test score from the threshold to the maximum,
    NMT scores 100-200, a bigger test score never gives a smaller NMT score."""
    errors = []
    scale = {}
    for raw, scaled in pairs:
        if raw in scale:
            errors.append(f"Тестовий бал {raw} записано двічі.")
        scale[raw] = scaled
    if threshold_raw is None or max_raw is None:
        return errors
    for raw, scaled in sorted(scale.items()):
        if raw < threshold_raw or raw > max_raw:
            errors.append(f"Тестовий бал {raw} поза межами {threshold_raw}–{max_raw} (поріг — максимум).")
        if not 100 <= scaled <= 200:
            errors.append(f"Для тестового бала {raw}: бал {scaled} має бути від 100 до 200.")
    missing = [raw for raw in range(threshold_raw, max_raw + 1) if raw not in scale]
    if missing:
        shown = ", ".join(str(m) for m in missing[:10]) + (" …" if len(missing) > 10 else "")
        errors.append(f"Немає рядків для тестових балів: {shown}. Учень з таким балом не отримав би результату.")
    previous = None
    for raw in range(threshold_raw, max_raw + 1):
        if raw in scale:
            if previous is not None and scale[raw] < previous[1]:
                errors.append(
                    f"Більший тестовий бал дає менший результат: {previous[0]} → {previous[1]}, але {raw} → {scale[raw]}."
                )
            previous = (raw, scale[raw])
    return errors


class ConversionTable(models.Model):
    """Official table of the УЦОЯО for one subject and one year:
    how to change the test score (for example, 17 of 32) into the NMT score 100-200.
    The table changes every year, so we keep every year as a new record and never change the code."""

    subject = models.ForeignKey(
        Subject, on_delete=models.PROTECT, related_name="conversion_tables", verbose_name="Предмет"
    )
    year = models.PositiveSmallIntegerField("Рік НМТ")
    max_raw = models.PositiveSmallIntegerField("Максимальний тестовий бал")
    threshold_raw = models.PositiveSmallIntegerField(
        "Пороговий тестовий бал", help_text="Найменший тестовий бал, що дає 100. Нижче — «не склав»."
    )
    source = models.URLField("Джерело", blank=True, help_text="Посилання на офіційну таблицю (УЦОЯО, МОН)")

    class Meta:
        db_table = "conversion_tables"
        verbose_name = "Таблиця переведення балів"
        verbose_name_plural = "Таблиці переведення балів"
        ordering = ["subject__name", "-year"]
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "year"], name="conversion_tables_one_per_year",
                violation_error_message="Для цього предмета таблиця за цей рік уже є.",
            ),
            models.CheckConstraint(
                condition=Q(threshold_raw__gte=1, threshold_raw__lte=F("max_raw")),
                name="conversion_tables_threshold_within_max",
                violation_error_message="Поріг має бути від 1 до максимального тестового бала.",
            ),
            models.CheckConstraint(
                condition=Q(year__gte=2022, year__lte=2100),
                name="conversion_tables_year_valid",
                violation_error_message="Рік має бути від 2022 (перше НМТ).",
            ),
        ]

    def __str__(self):
        return f"{self.subject} — {self.year} (поріг {self.threshold_raw}, макс. {self.max_raw})"

    @classmethod
    def for_test(cls, subject_id, day):
        """The table for a test: the newest year that is not later than the test year.
        Example: a trial test in November 2026 -> table 2026 (the table 2027 does not exist yet)."""
        if not subject_id or not day:
            return None
        return (
            cls.objects.filter(subject_id=subject_id, year__lte=day.year)
            .prefetch_related("rows").order_by("-year").first()
        )

    @property
    def scale(self):
        """{test score: NMT score} — read once, then used from memory."""
        if not hasattr(self, "_scale"):
            self._scale = {row.raw_score: row.scaled_score for row in self.rows.all()}
        return self._scale

    def convert(self, raw):
        """Test score -> NMT score. None if the threshold is not passed (or there is no row)."""
        if raw is None or raw < self.threshold_raw:
            return None
        return self.scale.get(raw)

    def problems(self):
        """Errors of the saved table (an empty list = the table is complete)."""
        return check_conversion_rows(self.threshold_raw, self.max_raw, list(self.scale.items()))


class ConversionRow(models.Model):
    table = models.ForeignKey(
        ConversionTable, on_delete=models.CASCADE, related_name="rows", verbose_name="Таблиця"
    )
    raw_score = models.PositiveSmallIntegerField("Тестовий бал")
    scaled_score = models.PositiveSmallIntegerField("Бал 100–200")

    class Meta:
        db_table = "conversion_rows"
        verbose_name = "Рядок таблиці переведення"
        verbose_name_plural = "Рядки таблиці переведення"
        ordering = ["table", "raw_score"]
        constraints = [
            models.UniqueConstraint(
                fields=["table", "raw_score"], name="conversion_rows_one_per_raw",
                violation_error_message="Цей тестовий бал уже є в таблиці.",
            ),
            models.CheckConstraint(
                condition=Q(scaled_score__gte=100, scaled_score__lte=200),
                name="conversion_rows_scaled_100_200",
                violation_error_message="Бал має бути від 100 до 200.",
            ),
        ]

    def __str__(self):
        return f"{self.raw_score} → {self.scaled_score}"


# ───────────────────────── Tests ─────────────────────────

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

    # Tests of these types use the official NMT scale (100-200).
    # Topic tests use percent (0-100), because "150 points" for one topic means nothing.
    NMT_SCALE_KINDS = [Kind.DIAGNOSTIC, Kind.TRIAL, Kind.FINAL]

    class Meta:
        db_table = "tests"
        verbose_name = "Тест"
        verbose_name_plural = "Тести"
        ordering = ["-held_at"]

    def __str__(self):
        return f"{self.title} ({self.held_at:%d.%m.%Y})"

    @property
    def uses_nmt_scale(self):
        return self.kind in self.NMT_SCALE_KINDS

    @property
    def conversion_table(self):
        """The conversion table for this test (None for topic tests or if there is no table).
        We keep it in memory, so a test with 30 results reads the table only once."""
        if not self.uses_nmt_scale:
            return None
        key = (self.subject_id, self.held_at.year if self.held_at else None)
        if getattr(self, "_table_key", None) != key:
            self._table_key = key
            self._table = ConversionTable.for_test(self.subject_id, self.held_at)
        return self._table

    def score_range(self):
        """Allowed score for this test: (min, max)."""
        return (100, 200) if self.uses_nmt_scale else (0, 100)

    def scale_label(self):
        """Short text about the scale, for error messages and pages."""
        return "від 100 до 200 (шкала НМТ)" if self.uses_nmt_scale else "від 0 до 100 (%)"

    def clean(self):
        if self.group_id and self.subject_id and self.group.subject_id != self.subject_id:
            raise ValidationError({"group": "Предмет групи не збігається з предметом тесту."})

        # If somebody changes the type of a test that already has results,
        # the old scores can be wrong for the new scale (for example, 70% -> "70 NMT points").
        if self.pk:
            low, high = self.score_range()
            results = self.results.all()
            wrong = results.filter(score__isnull=False).exclude(score__gte=low, score__lte=high).count()
            if not self.uses_nmt_scale:
                # A topic test needs a percent for every student
                wrong += results.filter(score__isnull=True).count()
            if wrong:
                raise ValidationError({
                    "kind": f"Для цього типу бал має бути {self.scale_label()}, "
                            f"але {wrong} результат(ів) не підходять. Спершу виправте бали."
                })

    def recalculate_results(self):
        """Convert the test scores of all results again (for example, after a fix in the table).
        Returns how many results changed."""
        table = self.conversion_table
        if table is None:
            return 0
        changed = 0
        for result in self.results.filter(raw_score__isnull=False):
            new_score = table.convert(result.raw_score)
            if new_score != result.score:
                result.score = new_score
                result.save(update_fields=["score"])
                changed += 1
        return changed


class TestResult(models.Model):
    test = models.ForeignKey(
        Test, on_delete=models.CASCADE, related_name="results", verbose_name="Тест"
    )
    student = models.ForeignKey(
        Student, on_delete=models.CASCADE, related_name="test_results", verbose_name="Учень"
    )
    # Test score = points for correct answers (for example, 17 of 32).
    # For NMT tests the teacher writes only this number, the system finds the score 100-200.
    raw_score = models.PositiveSmallIntegerField(
        "Тестовий бал", null=True, blank=True,
        help_text="Для НМТ-тестів: кількість тестових балів (наприклад, 17 з 32).",
    )
    # NMT tests: calculated from raw_score (empty = the threshold is not passed).
    # Topic tests: percent 0-100, written by the teacher.
    score = models.IntegerField(
        "Бал", null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(200)],
        help_text="Для НМТ-тестів рахується автоматично з тестового бала. Для тематичних — відсоток.",
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
            # An empty result makes no sense: we need a score or at least a test score
            models.CheckConstraint(
                condition=Q(score__isnull=False) | Q(raw_score__isnull=False),
                name="test_results_has_some_score",
                violation_error_message="Вкажіть бал або тестовий бал.",
            ),
        ]

    def __str__(self):
        return f"{self.student} — {self.test.title}: {self.score_display()}"

    @property
    def threshold_failed(self):
        """True if the student did not pass the threshold of an NMT test."""
        return self.test.uses_nmt_scale and self.score is None and self.raw_score is not None

    def score_display(self):
        """Score with its unit: "70%" for topic tests, "165" for the NMT scale."""
        if self.threshold_failed:
            return "поріг не подолано"
        if self.score is None:
            return "—"
        return str(self.score) if self.test.uses_nmt_scale else f"{self.score}%"

    def raw_display(self):
        """Test score with the maximum: "17 з 32" (or "17" if there is no table)."""
        if self.raw_score is None:
            return "—"
        table = self.test.conversion_table
        return f"{self.raw_score} з {table.max_raw}" if table else str(self.raw_score)

    def _get_test(self):
        # When you add a new test with results on one admin page,
        # the test is not saved yet: test_id is empty, but self.test already exists.
        # So we take the object, not only the id.
        try:
            return self.test
        except Test.DoesNotExist:
            return None

    def _fill_defaults(self):
        test = self._get_test()
        # NMT test with a table: the score 100-200 always comes from the table
        if test is not None and self.raw_score is not None and test.conversion_table is not None:
            self.score = test.conversion_table.convert(self.raw_score)

    def clean(self):
        test = self._get_test()
        if test is None:
            return
        table = test.conversion_table

        if not test.uses_nmt_scale:
            # Topic test: only percent
            if self.raw_score is not None:
                raise ValidationError({"raw_score": "Для тематичного тесту тестовий бал не потрібен — вкажіть відсоток."})
            if self.score is None:
                raise ValidationError({"score": "Вкажіть результат у відсотках (0–100)."})
        elif table is not None:
            # NMT test with a table: the test score is required, the score is calculated
            if self.raw_score is None:
                raise ValidationError({"raw_score": f"Вкажіть тестовий бал (0–{table.max_raw}): бал 100–200 порахується сам."})
            if self.raw_score > table.max_raw:
                raise ValidationError({"raw_score": f"Максимум для цього предмета — {table.max_raw} тестових балів."})
            self._fill_defaults()
        else:
            # NMT test, but there is no table for this subject: the score is written by hand
            if self.score is None:
                raise ValidationError({
                    "score": "Для цього предмета немає таблиці переведення: вкажіть бал 100–200 вручну "
                             "(або додайте таблицю в розділі «Таблиці переведення балів»)."
                })

        # The score must fit the scale of this test type
        low, high = test.score_range()
        if self.score is not None and not (low <= self.score <= high):
            raise ValidationError({
                "score": f"Для тесту типу «{test.get_kind_display()}» бал має бути {test.scale_label()}."
            })

        # The student must study in the group (or the subject) of this test
        if not self.student_id:
            return
        contracts = Contract.objects.filter(student_id=self.student_id)
        if test.group_id:
            # Group test: the student must study in this group
            ok = contracts.filter(group_id=test.group_id).exists()
        else:
            # School-wide test: the student must study this subject in any group
            ok = contracts.filter(group__subject_id=test.subject_id).exists()
        if not ok:
            raise ValidationError({"student": "Цей учень не навчається в групі (або на предметі) цього тесту."})

    def save(self, *args, **kwargs):
        self._fill_defaults()  # also works when results are created in code
        super().save(*args, **kwargs)


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

    class PaymentPlan(models.TextChoices):
        MONTHLY = "monthly", "Щомісяця"
        PREPAID = "prepaid", "Повністю наперед"

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
    # Filled automatically with the end date of the group.
    # Change it by hand only if the contract is terminated early.
    end_date = models.DateField(
        "Дата завершення", null=True, blank=True,
        help_text="Якщо не вказати — візьметься дата завершення групи. "
                  "Вручну — лише при достроковому розірванні.",
    )
    discount_pct = models.DecimalField(
        "Знижка, %", default=Decimal("0"), validators=PERCENT_VALIDATORS, **PERCENT
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.ACTIVE)
    payment_plan = models.CharField(
        "План оплати", max_length=20, choices=PaymentPlan.choices, default=PaymentPlan.MONTHLY,
        help_text="Щомісяця — рахунок на кожен місяць до 1-го числа. "
                  "Повністю наперед — один рахунок на весь курс.",
    )
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
        return f"Договір №{self.pk} — {self.student} ({self.group.full_name})"

    def _fill_defaults(self):
        if not self.group_id:
            return
        # 1. The contract ends when the group ends (if nobody wrote another date)
        if self.end_date is None:
            self.end_date = self.group.end_date
        # 2. Early termination: if the date is still in the future, the contract ends today
        if self.status == self.Status.TERMINATED:
            today = timezone.localdate()
            if self.end_date is None or self.end_date > today:
                self.end_date = max(today, self.start_date) if self.start_date else today

    def clean(self):
        self._fill_defaults()

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

        if self.group_id and self.start_date:
            group = self.group
            # The student cannot start before the group starts
            if self.start_date < group.start_date:
                raise ValidationError({"start_date": f"Група починає навчання {group.start_date:%d.%m.%Y}."})
            # The contract cannot be longer than the group
            if group.end_date and self.end_date and self.end_date > group.end_date:
                raise ValidationError({"end_date": f"Група завершує навчання {group.end_date:%d.%m.%Y}."})

        # For full prepayment we must know how many months to pay for
        if self.payment_plan == self.PaymentPlan.PREPAID and not self.end_date:
            raise ValidationError({
                "payment_plan": "Для оплати наперед у групи має бути дата завершення "
                                "(інакше невідомо, за скільки місяців рахувати)."
            })

        # The plan cannot change after the first payment:
        # the money is already linked to the old invoices
        if self.pk:
            old_plan = Contract.objects.filter(pk=self.pk).values_list("payment_plan", flat=True).first()
            if old_plan != self.payment_plan and Payment.objects.filter(invoice__contract=self).exists():
                raise ValidationError({"payment_plan": "За договором уже є оплати, план оплати змінити не можна."})

    def save(self, *args, **kwargs):
        self._fill_defaults()
        super().save(*args, **kwargs)
        # After every save the invoices follow the contract (dates, plan, discount)
        self.sync_invoices()

    def monthly_amount(self):
        """Price for one month with the discount."""
        price = self.tariff.monthly_price
        return money_round(price * (Decimal("100") - self.discount_pct) / Decimal("100"))

    def study_months(self):
        """All months of studying: from the start month to the end month.
        If the end is unknown, we plan only until the current month."""
        last_day = self.end_date or max(timezone.localdate(), self.start_date)
        return months_between(self.start_date, last_day)

    def planned_invoices(self):
        """The invoice plan for this contract: {month: (amount, due date)}.

        Monthly: one invoice for every month, pay before the 1st day of the month
                 (the first month: not earlier than the start of studying).
        Prepaid: one invoice for the whole course, pay before the start of studying."""
        months = self.study_months()
        if self.payment_plan == self.PaymentPlan.PREPAID:
            total = self.monthly_amount() * len(months)
            return {months[0]: (total, self.start_date)}
        return {month: (self.monthly_amount(), max(month, self.start_date)) for month in months}

    def sync_invoices(self):
        """Make the invoices of this contract match the plan.

        - creates the missing invoices;
        - updates the amount and the due date of invoices WITHOUT payments
          (for example, the discount changed);
        - deletes invoices WITHOUT payments that are not in the plan anymore
          (for example, the contract was terminated in December -> January..May are not needed).
        Invoices that already have payments are never changed: this is real money.
        Returns (created, updated, deleted)."""
        plan = self.planned_invoices()
        created = updated = deleted = 0
        existing = {inv.period_month: inv for inv in self.invoices.annotate(pay_count=models.Count("payments"))}

        for month, inv in existing.items():
            if inv.pay_count:
                continue  # it has payments: do not touch
            if month not in plan:
                inv.delete()
                deleted += 1
                continue
            amount, due = plan[month]
            if inv.amount != amount or inv.due_date != due:
                inv.amount, inv.due_date = amount, due
                inv.save(update_fields=["amount", "due_date"])
                updated += 1

        for month, (amount, due) in plan.items():
            if month not in existing:
                Invoice.objects.create(contract=self, period_month=month, amount=amount, due_date=due)
                created += 1
        return created, updated, deleted


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
            # Take the amount from the contract plan (monthly price or the whole course)
            planned = self.contract.planned_invoices().get(self.period_month)
            self.amount = planned[0] if planned else self.contract.monthly_amount()

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