"""
Fill the database with realistic test data.

Usage:
    python manage.py seed_data          # only if the database is empty
    python manage.py seed_data --clear  # delete all school data and create it again

Dates are calculated from "today", so the data always looks fresh:
the school year starts on 1 September, past lessons are "held",
future lessons are "planned".
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from school.access import ADMIN_GROUP, create_teacher_login, setup_role_groups
from school.models import (
    Attendance, Contract, ConversionRow, ConversionTable, Expense, GroupSchedule, Invoice, Lesson,
    Payment, Refund, Salary, Student, StudyGroup, Subject, Tariff, Teacher, Test, TestResult,
)

LESSON_MINUTES = 90  # one lesson lasts 1.5 hours

# Surnames without gender forms, so they fit both boys and girls
LAST_NAMES = [
    "Шевченко", "Коваленко", "Бондаренко", "Ткаченко", "Кравченко", "Олійник", "Шевчук",
    "Поліщук", "Лисенко", "Марченко", "Савченко", "Руденко", "Мороз", "Павленко",
    "Кузьменко", "Петренко", "Ковальчук", "Гончаренко", "Литвиненко", "Тимошенко",
    "Мельничук", "Романюк", "Кириленко", "Демченко", "Ярошенко", "Гуменюк", "Нестеренко",
    "Остапенко", "Василенко", "Захарченко",
]
BOY_NAMES = ["Олександр", "Максим", "Артем", "Дмитро", "Назар", "Богдан", "Іван", "Матвій", "Денис", "Андрій"]
GIRL_NAMES = ["Софія", "Анна", "Марія", "Вікторія", "Дарина", "Анастасія", "Поліна", "Олена", "Юлія", "Катерина"]
MOTHER_NAMES = ["Олена Петрівна", "Наталія Іванівна", "Ірина Василівна", "Тетяна Миколаївна", "Світлана Олегівна"]
FATHER_NAMES = ["Сергій Іванович", "Андрій Петрович", "Олег Васильович", "Віктор Миколайович", "Ігор Степанович"]

TOPICS = {
    "Українська мова": ["Фонетика", "Орфоепія", "Лексикологія", "Будова слова", "Іменник", "Прикметник",
                        "Дієслово", "Дієприкметник", "Синтаксис речення", "Пунктуація", "Пряма мова"],
    "Математика": ["Відсотки", "Лінійні рівняння", "Квадратні рівняння", "Нерівності", "Функції",
                   "Прогресії", "Тригонометрія", "Планіметрія", "Стереометрія", "Логарифми", "Похідна"],
    "Історія України": ["Київська Русь", "Галицько-Волинська держава", "Козацька доба", "Гетьманщина",
                        "Українська революція 1917–1921", "Голодомор", "Друга світова війна", "Незалежність"],
    "Англійська мова": ["Present Tenses", "Past Tenses", "Future Forms", "Passive Voice", "Conditionals",
                        "Reported Speech", "Modal Verbs", "Reading Strategies", "Use of English"],
}


# Official NMT 2026 conversion tables (Додаток 5 до Порядку прийому 2026).
# subject: (max test score, threshold, [NMT score for threshold, threshold + 1, ..., max])
NMT_TABLES_2026 = {
    "Українська мова": (45, 8, [
        100, 105, 110, 120, 125, 130, 134, 136, 138, 140, 142, 143, 144, 145, 146, 148, 149, 150, 152,
        154, 156, 157, 159, 160, 162, 163, 165, 167, 170, 172, 175, 177, 180, 183, 186, 191, 195, 200,
    ]),
    "Математика": (32, 5, [
        100, 108, 115, 123, 131, 134, 137, 140, 143, 145, 147, 148, 149, 150,
        151, 152, 155, 159, 163, 167, 170, 173, 176, 180, 184, 189, 194, 200,
    ]),
    "Історія України": (54, 9, [
        100, 105, 110, 115, 120, 125, 130, 132, 134, 136, 138, 140, 141, 142, 143, 144, 145, 146, 147,
        148, 149, 150, 151, 152, 154, 156, 158, 160, 163, 166, 168, 169, 170, 172, 173, 175, 177, 179,
        181, 183, 185, 188, 191, 194, 197, 200,
    ]),
    "Англійська мова": (32, 5, [
        100, 109, 118, 125, 131, 134, 137, 140, 143, 145, 147, 148, 149, 150,
        151, 152, 153, 155, 157, 159, 162, 166, 169, 173, 179, 185, 191, 200,
    ]),
}
# Demo logins for checking the roles (only for test data on your computer!)
DEMO_PASSWORD = "nmt-demo-2026"
DEMO_ADMIN = "director"

NMT_TABLES_SOURCE = "https://osvita.ua/doc/files/news/978/97839/d5_1.pdf"


def phone():
    return "+380" + random.choice(["50", "63", "66", "67", "68", "73", "93", "97", "99"]) + f"{random.randint(0, 9999999):07d}"


def aware(day, at):
    """Join a date and a time into a datetime with the Kyiv time zone."""
    return timezone.make_aware(datetime.combine(day, at))


def add_month(day):
    """Return the first day of the next month."""
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def plus_minutes(at, minutes):
    """Add minutes to a time: 18:00 + 90 -> 19:30."""
    return (datetime.combine(date.today(), at) + timedelta(minutes=minutes)).time()


def money(value):
    return Decimal(value).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Заповнює базу даних реалістичними тестовими даними"

    def add_arguments(self, parser):
        parser.add_argument("--clear", action="store_true", help="Видалити всі дані школи перед заповненням")
        parser.add_argument("--seed", type=int, default=42, help="Число для генератора (ті самі дані щоразу)")

    @transaction.atomic  # if something fails, nothing is saved
    def handle(self, *args, **options):
        random.seed(options["seed"])

        if options["clear"]:
            self.clear()
        elif Student.objects.exists():
            self.stdout.write(self.style.WARNING(
                "У базі вже є дані. Запустіть з --clear, щоб очистити і заповнити заново."
            ))
            return

        self.today = timezone.localdate()
        # The school year starts on 1 September
        year = self.today.year if self.today.month >= 9 else self.today.year - 1
        self.year_start = date(year, 9, 1)

        self.create_subjects_and_teachers()
        self.create_conversion_tables()
        self.create_tariffs()
        self.create_groups()
        self.create_students()
        self.create_contracts()
        self.create_lessons_and_attendance()
        self.create_tests()
        self.create_invoices_and_payments()
        self.create_refunds()
        self.create_salaries()
        self.create_expenses()
        self.create_demo_logins()
        self.print_summary()

    # ───────────────────────── Steps ─────────────────────────

    def clear(self):
        # Order is important: first records that depend on others (because of PROTECT)
        for model in [Payment, Refund, Invoice, Attendance, TestResult, Test, Lesson,
                      Contract, Salary, Expense, GroupSchedule, StudyGroup, Tariff, Student,
                      Teacher, ConversionRow, ConversionTable, Subject]:
            model.objects.all().delete()
        self.stdout.write("Старі дані видалено.")

    def create_subjects_and_teachers(self):
        names = ["Українська мова", "Математика", "Історія України", "Англійська мова", "Біологія"]
        self.subjects = {name: Subject.objects.create(name=name) for name in names}

        teachers_data = [
            ("Коваленко", "Ірина", "400"),
            ("Бондар", "Олег", "420"),
            ("Мельник", "Тетяна", "380"),
            ("Гриценко", "Марина", "450"),
        ]
        self.teachers = [
            Teacher.objects.create(last_name=ln, first_name=fn, phone=phone(), rate_per_lesson=Decimal(rate))
            for ln, fn, rate in teachers_data
        ]

    def create_conversion_tables(self):
        """Official tables 2026. Biology has no table on purpose:
        it shows how the system works when the score is written by hand."""
        for subject_name, (max_raw, threshold, scaled) in NMT_TABLES_2026.items():
            table = ConversionTable.objects.create(
                subject=self.subjects[subject_name], year=2026,
                max_raw=max_raw, threshold_raw=threshold, source=NMT_TABLES_SOURCE,
            )
            ConversionRow.objects.bulk_create(
                ConversionRow(table=table, raw_score=threshold + i, scaled_score=value)
                for i, value in enumerate(scaled)
            )

    def create_tariffs(self):
        # Only two tariffs for new contracts.
        # The "Intensive" tariff was removed: students paid for 12 lessons a month,
        # but groups have only 8 lessons a month.
        self.tariffs = [
            Tariff.objects.create(name="Стандарт", monthly_price=Decimal("2400"), lessons_per_month=8),
            Tariff.objects.create(
                name="Гарантія 170+", monthly_price=Decimal("3800"), lessons_per_month=8,
                has_guarantee=True, guarantee_min_score=170, refund_pct=Decimal("30"),
            ),
        ]
        # An old tariff, not used for new contracts (to show the "is_active" flag)
        Tariff.objects.create(name="Базовий (архів)", monthly_price=Decimal("2000"),
                              lessons_per_month=8, is_active=False)

    def create_groups(self):
        start = self.year_start + timedelta(days=(7 - self.year_start.weekday()) % 7)  # first Monday
        end = date(self.year_start.year + 1, 5, 31)
        t = self.teachers
        # (short name, subject, teacher, level, weekdays (1 = Monday ... 7 = Sunday), start time)
        # The full name is built automatically: "Математика · група 1"
        data = [
            ("група 1", "Українська мова", t[0], StudyGroup.Level.MEDIUM, [1, 4], time(16, 0)),
            ("група 2", "Українська мова", t[0], StudyGroup.Level.ADVANCED, [2, 5], time(17, 30)),
            ("група 1", "Математика", t[1], StudyGroup.Level.BASIC, [1, 3], time(18, 0)),
            ("група 2", "Математика", t[1], StudyGroup.Level.ADVANCED, [2, 4], time(16, 0)),
            ("група 1", "Історія України", t[2], StudyGroup.Level.MEDIUM, [3, 6], time(10, 0)),
            ("група 1", "Англійська мова", t[3], StudyGroup.Level.MEDIUM, [5, 6], time(12, 0)),
        ]
        self.groups = []
        for name, subject, teacher, level, days, at in data:
            group = StudyGroup.objects.create(
                name=name, subject=self.subjects[subject], teacher=teacher, level=level,
                start_date=start, end_date=end, max_students=12,
            )
            # The schedule is saved in the database (table group_schedule),
            # and lessons are created from it below
            for day in days:
                GroupSchedule.objects.create(
                    group=group, weekday=day, start_time=at, end_time=plus_minutes(at, LESSON_MINUTES),
                )
            self.groups.append(group)

    def create_students(self):
        self.students = []
        self.skill = {}  # student id -> "level" of the student, used for test scores
        for last_name in LAST_NAMES:
            is_girl = random.random() < 0.55
            first_name = random.choice(GIRL_NAMES if is_girl else BOY_NAMES)
            if random.random() < 0.7:
                parent = f"{last_name} {random.choice(MOTHER_NAMES)}"
            else:
                parent = f"{last_name} {random.choice(FATHER_NAMES)}"
            student = Student.objects.create(
                last_name=last_name, first_name=first_name, phone=phone(),
                grade=random.choice([10, 11, 11, 11]),
                parent_name=parent, parent_phone=phone(),
                registered_at=self.year_start - timedelta(days=random.randint(0, 25)),
            )
            self.students.append(student)
            self.skill[student.id] = random.randint(120, 180)

    def create_contracts(self):
        self.contracts = []
        taken = {g.id: 0 for g in self.groups}
        active_tariffs = self.tariffs  # only active tariffs

        for student in self.students:
            # Every student studies 1 or 2 subjects
            wanted = 1 if random.random() < 0.5 else 2
            subjects_used = set()
            for group in random.sample(self.groups, len(self.groups)):
                if wanted == 0:
                    break
                if group.subject_id in subjects_used or taken[group.id] >= group.max_students:
                    continue
                start = group.start_date + timedelta(days=random.choice([0, 0, 0, 3, 7, 14, 21, 30]))
                # Invoices for the whole course are created automatically in Contract.save()
                contract = Contract.objects.create(
                    student=student, group=group,
                    # About 20% of families pay for the whole course at once
                    payment_plan=random.choices(list(Contract.PaymentPlan), weights=[8, 2])[0],
                    # One weight for each tariff (2 tariffs -> 2 weights).
                    # About 60% "Standard", 40% "Guarantee 170+".
                    tariff=random.choices(active_tariffs, weights=[6, 4])[0],
                    start_date=start,
                    discount_pct=Decimal(random.choice([0, 0, 0, 0, 0, 10, 15])),
                )
                self.contracts.append(contract)
                taken[group.id] += 1
                subjects_used.add(group.subject_id)
                wanted -= 1

        # A few contracts with other statuses, to make the data realistic
        early = [c for c in self.contracts if c.start_date == c.group.start_date]
        monthly = [c for c in early if c.payment_plan == Contract.PaymentPlan.MONTHLY]
        for contract in random.sample(monthly, 2):
            contract.status = Contract.Status.TERMINATED
            contract.end_date = contract.start_date + timedelta(days=random.randint(20, 35))
            contract.save()  # the invoices after the end date are deleted automatically
        suspended = random.choice([c for c in self.contracts if c.status == Contract.Status.ACTIVE])
        suspended.status = Contract.Status.SUSPENDED
        suspended.save()

    def members_on(self, group, day):
        """Students who studied in the group on this day."""
        return [
            c.student for c in self.contracts
            if c.group_id == group.id and c.start_date <= day
            and (c.end_date is None or c.end_date >= day)
            and c.status != Contract.Status.SUSPENDED
        ]

    def create_lessons_and_attendance(self):
        lessons, attendance = [], []
        last_day = self.today + timedelta(days=40)
        now = timezone.now()
        statuses = [Attendance.Status.PRESENT] * 16 + [Attendance.Status.LATE, Attendance.Status.ABSENT,
                                                      Attendance.Status.ABSENT, Attendance.Status.EXCUSED]
        for group in self.groups:
            # Read the schedule from the database: {weekday: start time}
            slots = {slot.weekday: slot.start_time for slot in group.schedule.all()}
            topics = TOPICS[group.subject.name]
            day, number = group.start_date, 0
            while day <= last_day:
                if day.isoweekday() in slots:  # isoweekday(): Monday = 1, like in the table
                    starts_at = aware(day, slots[day.isoweekday()])
                    if starts_at < now:
                        status = Lesson.Status.CANCELLED if random.random() < 0.04 else Lesson.Status.HELD
                    else:
                        status = Lesson.Status.PLANNED
                    # Sometimes another teacher replaces the group teacher
                    teacher = group.teacher
                    if random.random() < 0.03:
                        teacher = random.choice([t for t in self.teachers if t != group.teacher])
                    lesson = Lesson(group=group, teacher=teacher, starts_at=starts_at,
                                    topic=topics[number % len(topics)], status=status)
                    lessons.append(lesson)
                    number += 1
                day += timedelta(days=1)

        Lesson.objects.bulk_create(lessons)  # one big INSERT instead of hundreds

        for lesson in lessons:
            if lesson.status != Lesson.Status.HELD:
                continue
            for student in self.members_on(lesson.group, lesson.starts_at.date()):
                attendance.append(Attendance(lesson=lesson, student=student, status=random.choice(statuses)))
        Attendance.objects.bulk_create(attendance)

    def score_for(self, student, test_day):
        """NMT scale score (100-200): student level + progress over time + some luck."""
        progress = (test_day - self.year_start).days // 15
        return max(100, min(200, self.skill[student.id] + progress + random.randint(-12, 12)))

    def percent_for(self, student, test_day):
        """Topic test score in percent (0-100).
        A strong student (level 180) gets about 90%, a weak one (level 120) about 40%."""
        progress = (test_day - self.year_start).days // 15
        value = 40 + (self.skill[student.id] - 120) * 0.8 + progress + random.randint(-10, 10)
        return max(0, min(100, round(value)))

    def raw_for(self, table, target, fail=False):
        """The test score that gives the NMT score closest to the target.
        fail=True: a score below the threshold (the student did not pass)."""
        if fail:
            return random.randint(max(0, table.threshold_raw - 4), table.threshold_raw - 1)
        return min(table.scale, key=lambda raw: abs(table.scale[raw] - target))

    def make_result(self, test, student, test_day, fail=False):
        """A result with the right scale for the test type.
        bulk_create() does not call save(), so here we convert the score ourselves."""
        if not test.uses_nmt_scale:
            return TestResult(test=test, student=student, score=self.percent_for(student, test_day))
        table = test.conversion_table
        if table is None:  # no table for the subject: the score is written by hand
            return TestResult(test=test, student=student, score=self.score_for(student, test_day))
        raw = self.raw_for(table, self.score_for(student, test_day), fail=fail)
        return TestResult(test=test, student=student, raw_score=raw, score=table.convert(raw))

    def create_tests(self):
        results = []
        for group in self.groups:
            plan = [
                ("Вхідна діагностика", Test.Kind.DIAGNOSTIC, group.start_date + timedelta(days=3)),
                ("Тематичний тест №1", Test.Kind.TOPIC, group.start_date + timedelta(days=35)),
            ]
            for title, kind, day in plan:
                if day > self.today:
                    continue
                test = Test.objects.create(subject=group.subject, group=group,
                                           title=f"{title} ({group.full_name})", kind=kind, held_at=day)
                for student in self.members_on(group, day):
                    # Diagnostic -> test score + NMT score, topic test -> percent
                    results.append(self.make_result(test, student, day))

        # School-wide trial NMT in Ukrainian for all groups of the subject (group is empty)
        trial_day = self.year_start + timedelta(days=46)
        if trial_day <= self.today:
            subject = self.subjects["Українська мова"]
            test = Test.objects.create(subject=subject, group=None, title="Пробне НМТ: українська мова",
                                       kind=Test.Kind.TRIAL, held_at=trial_day)
            # All students of the subject on this day (one student can be in two groups)
            participants = {}
            for group in self.groups:
                if group.subject_id == subject.id:
                    for student in self.members_on(group, trial_day):
                        participants[student.id] = student
            # The weakest participant does not pass the threshold: the pages show this case too
            weakest_id = min(participants, key=lambda sid: self.skill[sid]) if participants else None
            for student_id, student in participants.items():
                results.append(self.make_result(test, student, trial_day, fail=student_id == weakest_id))
        TestResult.objects.bulk_create(results)

    def create_invoices_and_payments(self):
        """The invoices already exist (Contract.save() creates them). Here we only add payments
        for invoices whose due date has come; future invoices stay unpaid."""
        for contract in self.contracts:
            for invoice in contract.invoices.order_by("period_month"):
                if invoice.due_date > self.today:
                    continue  # it is too early to pay
                is_current = invoice.period_month == self.today.replace(day=1)
                self.pay(invoice, is_current, prepaid=contract.payment_plan == Contract.PaymentPlan.PREPAID)

    def pay(self, invoice, is_current, prepaid=False):
        r = random.random()
        if prepaid:
            # The whole course is paid at once, before the start
            part = 1
        elif is_current:
            # Current month: some pay fully, some partly, some have not paid yet
            part = 1 if r < 0.45 else (0.5 if r < 0.65 else 0)
        else:
            # Past months: most students paid, a few have a debt
            part = 1 if r < 0.88 else 0.5
        if part == 0:
            return
        # Most families pay a few days before or after the due date, a few are late
        day = invoice.due_date + timedelta(days=random.choice([-3, -2, -1, 0, 0, 1, 2, 3, 5, 10]))
        day = min(day, self.today)
        method = random.choices(list(Payment.Method), weights=[2, 4, 2, 3])[0]
        # reserve_amount is calculated automatically for guarantee tariffs
        Payment.objects.create(invoice=invoice, amount=money(invoice.amount * Decimal(part)),
                               paid_at=aware(day, time(random.randint(9, 19), random.choice([0, 15, 30, 45]))),
                               method=method)

    def create_refunds(self):
        for contract in self.contracts:
            if contract.status != Contract.Status.TERMINATED:
                continue
            paid_day = contract.end_date + timedelta(days=5)
            is_paid = paid_day <= self.today
            Refund.objects.create(
                contract=contract, reason=Refund.Reason.EARLY_TERMINATION,
                amount=money(contract.monthly_amount() / 2),
                status=Refund.Status.PAID if is_paid else Refund.Status.APPROVED,
                created_at=contract.end_date, paid_at=paid_day if is_paid else None,
            )
        # One small request that is still waiting
        contract = random.choice([c for c in self.contracts if c.status == Contract.Status.ACTIVE])
        Refund.objects.create(contract=contract, reason=Refund.Reason.OVERPAYMENT, amount=Decimal("200"),
                              created_at=self.today - timedelta(days=2))

    def create_salaries(self):
        current_month = self.today.replace(day=1)
        month = self.year_start
        while month <= current_month:
            for teacher in self.teachers:
                # base_amount is empty -> it is calculated from held lessons
                paid_at = None if month == current_month else add_month(month) + timedelta(days=4)
                if paid_at and paid_at > self.today:
                    paid_at = None
                Salary.objects.create(teacher=teacher, period_month=month,
                                      bonus=Decimal(random.choice([0, 0, 0, 500, 1000])), paid_at=paid_at)
            month = add_month(month)

    def create_expenses(self):
        month = self.year_start
        while month <= self.today:
            items = [
                (Expense.Category.RENT, "18000", 1, "Оренда приміщення"),
                (Expense.Category.UTILITIES, str(random.randint(2500, 4000)), 15, "Світло, вода, інтернет"),
                (Expense.Category.MARKETING, str(random.randint(3000, 7000)), 5, "Реклама в Instagram і TikTok"),
                (Expense.Category.MATERIALS, str(random.randint(1500, 3500)), 8, "Друк збірників і пробних тестів"),
            ]
            if month == self.year_start:
                items.append((Expense.Category.EQUIPMENT, "24000", 3, "Проєктор і дошка"))
                items.append((Expense.Category.SOFTWARE, "1200", 3, "Підписка на сервіс онлайн-тестів"))
            for category, amount, day, comment in items:
                spent = month.replace(day=day)
                if spent <= self.today:
                    Expense.objects.create(category=category, amount=Decimal(amount), spent_at=spent, comment=comment)
            month = add_month(month)

    def create_demo_logins(self):
        """Roles + demo logins: "director" (administrator) and one login for every teacher.
        Logins are not deleted by --clear: they are linked again to the new teachers."""
        setup_role_groups()
        User = get_user_model()
        director, _ = User.objects.get_or_create(username=DEMO_ADMIN, defaults={"first_name": "Директор"})
        director.is_staff = True
        director.set_password(DEMO_PASSWORD)
        director.save()
        director.groups.add(Group.objects.get(name=ADMIN_GROUP))
        self.logins = [DEMO_ADMIN]
        for teacher in self.teachers:
            user, _ = create_teacher_login(teacher, DEMO_PASSWORD)
            user.set_password(DEMO_PASSWORD)  # the same demo password every time
            user.save()
            self.logins.append(user.username)

    def print_summary(self):
        rows = [
            ("Предмети", Subject), ("Таблиці переведення", ConversionTable), ("Викладачі", Teacher), ("Тарифи", Tariff), ("Учні", Student),
            ("Групи", StudyGroup), ("Розклад (занять на тиждень)", GroupSchedule),
            ("Договори", Contract), ("Заняття", Lesson),
            ("Відвідуваність", Attendance), ("Тести", Test), ("Результати тестів", TestResult),
            ("Рахунки", Invoice), ("Оплати", Payment), ("Повернення", Refund),
            ("Зарплати", Salary), ("Витрати", Expense),
        ]
        self.stdout.write(self.style.SUCCESS("Тестові дані створено:"))
        for label, model in rows:
            self.stdout.write(f"  {label}: {model.objects.count()}")
        self.stdout.write(self.style.SUCCESS(
            f"Демо-логіни (пароль «{DEMO_PASSWORD}»): {', '.join(self.logins)}"
        ))
        self.stdout.write("  director — адміністратор; інші — викладачі (бачать лише свої групи).")