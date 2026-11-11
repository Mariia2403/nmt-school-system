"""
Fill the database with realistic test data.

Usage:
    python manage.py seed_data          # only if the database is empty
    python manage.py seed_data --clear  # delete all school data and create it again

Dates are calculated from "today", so the data always looks fresh:
the school year starts on 1 September, past lessons are "held",
future lessons are "planned".

The size is like a real school: about 300 students, 30 groups, 10 teachers,
all NMT subjects. Most families pay on time, about 10% are late.
"""
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from school.access import ACCOUNTANT_GROUP, ADMIN_GROUP, MANAGER_GROUP, create_teacher_login, setup_role_groups
from school.models import (
    Attendance, Contract, ContractPause, ConversionRow, ConversionTable, DebtCall, Expense, GroupSchedule, Invoice, Lesson,
    Payment, Refund, Salary, Student, StudyGroup, Subject, Tariff, Teacher, Test, TestResult,
)

LESSON_MINUTES = 90  # one lesson lasts 1.5 hours

STUDENTS_COUNT = 300

# Surnames without gender forms, so they fit both boys and girls
LAST_NAMES = [
    "Шевченко", "Коваленко", "Бондаренко", "Ткаченко", "Кравченко", "Олійник", "Шевчук",
    "Поліщук", "Лисенко", "Марченко", "Савченко", "Руденко", "Мороз", "Павленко",
    "Кузьменко", "Петренко", "Ковальчук", "Гончаренко", "Литвиненко", "Тимошенко",
    "Мельничук", "Романюк", "Кириленко", "Ярошенко", "Гуменюк", "Нестеренко",
    "Остапенко", "Василенко", "Захарченко", "Левченко", "Карпенко", "Онищенко", "Сидоренко",
    "Клименко", "Приходько", "Гаврилюк", "Мартинюк", "Пономаренко", "Дорошенко", "Юрченко",
    "Головко", "Білоус", "Гордієнко", "Тарасенко", "Яковенко", "Семенюк", "Зінченко",
    "Давиденко", "Костенко", "Філоненко", "Москаленко", "Кушнір", "Самойленко", "Харченко",
    "Ігнатенко", "Литвин", "Щербак", "Панченко", "Мисник", "Король",
]
BOY_NAMES = ["Олександр", "Максим", "Артем", "Дмитро", "Назар", "Богдан", "Іван", "Матвій", "Денис",
             "Андрій", "Тимофій", "Марко", "Євген", "Владислав", "Ярослав", "Остап", "Кирило", "Роман"]
GIRL_NAMES = ["Софія", "Анна", "Марія", "Вікторія", "Дарина", "Анастасія", "Поліна", "Олена", "Юлія",
              "Катерина", "Злата", "Соломія", "Мілана", "Діана", "Ангеліна", "Христина", "Ірина", "Валерія"]
MOTHER_NAMES = ["Олена Петрівна", "Наталія Іванівна", "Ірина Василівна", "Тетяна Миколаївна", "Світлана Олегівна",
                "Оксана Вікторівна", "Людмила Сергіївна", "Юлія Андріївна", "Галина Петрівна"]
FATHER_NAMES = ["Сергій Іванович", "Андрій Петрович", "Олег Васильович", "Віктор Миколайович", "Ігор Степанович",
                "Роман Олексійович", "Володимир Іванович"]

# All NMT subjects. Ukrainian, Math and History are required for everybody,
# the others are "one by choice" (the 4th subject).
REQUIRED = ["Українська мова", "Математика", "Історія України"]
BY_CHOICE = ["Англійська мова", "Біологія", "Хімія", "Фізика", "Географія", "Українська література"]
# How often a family chooses a subject when it takes 1-2 subjects (not a package)
SUBJECT_WEIGHTS = {
    "Математика": 30, "Українська мова": 25, "Історія України": 15, "Англійська мова": 15,
    "Біологія": 5, "Хімія": 3, "Фізика": 3, "Географія": 2, "Українська література": 2,
}
# The 4th subject of the package "4 предмети"
CHOICE_WEIGHTS = {
    "Англійська мова": 50, "Біологія": 18, "Хімія": 8, "Фізика": 8, "Географія": 8, "Українська література": 8,
}

TOPICS = {
    "Українська мова": ["Фонетика", "Орфоепія", "Лексикологія", "Будова слова", "Іменник", "Прикметник",
                        "Дієслово", "Дієприкметник", "Синтаксис речення", "Пунктуація", "Пряма мова"],
    "Математика": ["Відсотки", "Лінійні рівняння", "Квадратні рівняння", "Нерівності", "Функції",
                   "Прогресії", "Тригонометрія", "Планіметрія", "Стереометрія", "Логарифми", "Похідна"],
    "Історія України": ["Київська Русь", "Галицько-Волинська держава", "Козацька доба", "Гетьманщина",
                        "Українська революція 1917–1921", "Голодомор", "Друга світова війна", "Незалежність"],
    "Англійська мова": ["Present Tenses", "Past Tenses", "Future Forms", "Passive Voice", "Conditionals",
                        "Reported Speech", "Modal Verbs", "Reading Strategies", "Use of English"],
    "Біологія": ["Клітина", "Обмін речовин", "Генетика", "Еволюція", "Рослини", "Тварини", "Людина", "Екологія"],
    "Хімія": ["Будова атома", "Хімічний зв'язок", "Розчини", "Окисно-відновні реакції", "Метали",
              "Неметали", "Органічні сполуки", "Розрахункові задачі"],
    "Фізика": ["Кінематика", "Динаміка", "Закони збереження", "Молекулярна фізика", "Електростатика",
               "Постійний струм", "Магнетизм", "Оптика", "Атомна фізика"],
    "Географія": ["Картографія", "Літосфера", "Атмосфера", "Гідросфера", "Населення світу",
                  "Економіка України", "Регіони світу"],
    "Українська література": ["Усна народна творчість", "Давня література", "Шевченко", "Франко",
                              "Леся Українка", "Розстріляне відродження", "Сучасна література"],
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


# Reasons for cancelled and moved lessons (lessons.note)
CANCEL_REASONS = ["Хвороба викладача", "Свято", "Відключення світла", "Технічні причини"]
MOVE_REASONS = ["Викладач на семінарі", "Збіг із пробним НМТ у школах", "Прохання батьків"]


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
        self.create_lesson_changes()
        self.create_tests()
        self.create_invoices_and_payments()
        self.create_refunds()
        self.create_salaries()
        self.create_expenses()
        self.create_demo_logins()
        self.create_debt_calls()
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
        self.subjects = {name: Subject.objects.create(name=name) for name in REQUIRED + BY_CHOICE}

        # (last name, first name, rate per lesson, subjects). The first 4 teachers keep their
        # demo logins (kovalenko, bondar, melnyk, hrytsenko).
        teachers_data = [
            ("Коваленко", "Ірина", "400", ["Українська мова", "Українська література"]),
            ("Бондар", "Олег", "420", ["Математика"]),
            ("Мельник", "Тетяна", "380", ["Історія України"]),
            ("Гриценко", "Марина", "450", ["Англійська мова"]),
            ("Савчук", "Оксана", "400", ["Українська мова"]),
            ("Литвиненко", "Андрій", "430", ["Математика"]),
            ("Федоренко", "Галина", "380", ["Історія України"]),
            ("Кравець", "Олена", "420", ["Англійська мова"]),
            ("Дмитренко", "Наталія", "400", ["Біологія", "Хімія"]),
            ("Ковтун", "Віктор", "410", ["Фізика", "Географія"]),
        ]
        self.teachers = []
        self.teachers_for = {}  # subject name -> teachers who teach it
        for last_name, first_name, rate, subjects in teachers_data:
            teacher = Teacher.objects.create(
                last_name=last_name, first_name=first_name, phone=phone(), rate_per_lesson=Decimal(rate),
            )
            self.teachers.append(teacher)
            for subject in subjects:
                self.teachers_for.setdefault(subject, []).append(teacher)

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
        # Packages: one price for 3 or 4 subjects, the family chooses the subjects (about 15% of families).
        self.package_3 = Tariff.objects.create(
            name="Пакет «3 предмети»", monthly_price=Decimal("6000"), lessons_per_month=8, subjects_count=3,
        )
        Tariff.objects.create(
            name="Пакет «4 предмети» (повний НМТ)", monthly_price=Decimal("7600"), lessons_per_month=8, subjects_count=4,
        )
        # An old tariff, not used for new contracts (to show the "is_active" flag)
        Tariff.objects.create(name="Базовий (архів)", monthly_price=Decimal("2000"),
                              lessons_per_month=8, is_active=False)

    def create_groups(self):
        start = self.year_start + timedelta(days=(7 - self.year_start.weekday()) % 7)  # first Monday
        end = date(self.year_start.year + 1, 5, 31)
        # How many groups every subject has (more groups for popular subjects): 30 groups
        groups_count = {
            "Українська мова": 7, "Математика": 7, "Історія України": 6, "Англійська мова": 4,
            "Біологія": 2, "Хімія": 1, "Фізика": 1, "Географія": 1, "Українська література": 1,
        }
        levels = [StudyGroup.Level.BASIC, StudyGroup.Level.MEDIUM, StudyGroup.Level.ADVANCED, StudyGroup.Level.MEDIUM]
        # Free time slots of a teacher: two days a week + start time. One teacher never has
        # two groups at the same time.
        slots = [
            ([1, 4], time(16, 0)), ([2, 5], time(16, 0)), ([1, 4], time(17, 40)), ([2, 5], time(17, 40)),
            ([3, 6], time(10, 0)), ([3, 6], time(12, 0)), ([1, 3], time(19, 20)),
        ]
        used = {teacher.id: 0 for teacher in self.teachers}  # how many slots a teacher has used
        self.groups = []
        for subject_name, count in groups_count.items():
            teachers = self.teachers_for[subject_name]
            for number in range(1, count + 1):
                # The teacher of this subject who has the fewest groups now
                teacher = min(teachers, key=lambda t: used[t.id])
                days, at = slots[used[teacher.id] % len(slots)]
                used[teacher.id] += 1
                group = StudyGroup.objects.create(
                    name=f"група {number}", subject=self.subjects[subject_name], teacher=teacher,
                    level=levels[(number - 1) % len(levels)],
                    start_date=start, end_date=end, max_students=20,
                )
                # The schedule is saved in the database (table group_schedule),
                # and lessons are created from it below
                for day in days:
                    GroupSchedule.objects.create(
                        group=group, weekday=day, start_time=at, end_time=plus_minutes(at, LESSON_MINUTES),
                    )
                group.slot_keys = {(day, at) for day in days}  # to avoid two groups of one student at one time
                self.groups.append(group)

    def create_students(self):
        self.students = []
        self.skill = {}  # student id -> "level" of the student, used for test scores
        for _ in range(STUDENTS_COUNT):
            last_name = random.choice(LAST_NAMES)
            is_girl = random.random() < 0.55
            first_name = random.choice(GIRL_NAMES if is_girl else BOY_NAMES)
            if random.random() < 0.7:
                parent = f"{last_name} {random.choice(MOTHER_NAMES)}"
            else:
                parent = f"{last_name} {random.choice(FATHER_NAMES)}"
            student = Student.objects.create(
                last_name=last_name, first_name=first_name, phone=phone(),
                # About 6% are graduates of past years: the class is empty
                grade=None if random.random() < 0.06 else random.choice([10, 11, 11, 11, 11]),
                parent_name=parent, parent_phone=phone(),
                registered_at=self.year_start - timedelta(days=random.randint(0, 25)),
            )
            self.students.append(student)
            self.skill[student.id] = random.randint(120, 185)

    def free_group(self, subject_name, taken, busy):
        """A group of the subject with a free place, at a time when the student is free.
        The group with the most free places is chosen, so groups fill evenly."""
        options = [
            g for g in self.groups
            if g.subject.name == subject_name and taken[g.id] < g.max_students and not (g.slot_keys & busy)
        ]
        return max(options, key=lambda g: g.max_students - taken[g.id]) if options else None

    def enroll(self, student, groups, tariff, taken, plan=None, discount=None, start=None):
        """Create contracts (invoices are created in Contract.save())."""
        plan = plan or random.choices(list(Contract.PaymentPlan), weights=[85, 15])[0]
        discount = Decimal(random.choice([0, 0, 0, 0, 0, 0, 10, 15])) if discount is None else discount
        for group in groups:
            contract = Contract.objects.create(
                student=student, group=group, tariff=tariff, payment_plan=plan, discount_pct=discount,
                start_date=start or group.start_date + timedelta(days=random.choice([0, 0, 0, 0, 3, 7, 14, 21, 35])),
            )
            self.contracts.append(contract)
            taken[group.id] += 1

    def create_contracts(self):
        self.contracts = []
        taken = {g.id: 0 for g in self.groups}
        standard, guarantee = self.tariffs
        package_4 = Tariff.objects.get(subjects_count=4)

        # A demo student for the defense: the package "3 предмети" (Ukrainian, Math, History)
        demo = Student.objects.create(
            last_name="Демченко", first_name="Олена", grade=11, phone="+380671110022",
            parent_name="Демченко Наталія Петрівна", parent_phone="+380501110022",
            registered_at=self.year_start,
        )
        self.students.insert(0, demo)
        self.skill[demo.id] = 165
        self.demo_ids = {demo.id}
        busy = set()
        demo_groups = []
        for subject_name in REQUIRED:
            group = self.free_group(subject_name, taken, busy)
            demo_groups.append(group)
            busy |= group.slot_keys
        self.enroll(demo, demo_groups, self.package_3, taken, plan=Contract.PaymentPlan.MONTHLY,
                    discount=Decimal("0"), start=demo_groups[0].start_date)

        for student in self.students[1:]:
            r = random.random()
            if r < 0.10:    # package of 3 required subjects
                wanted, tariff = list(REQUIRED), self.package_3
            elif r < 0.15:  # package of 4: required + one by choice
                choice = random.choices(list(CHOICE_WEIGHTS), weights=list(CHOICE_WEIGHTS.values()))[0]
                wanted, tariff = REQUIRED + [choice], package_4
            else:           # 1 or 2 separate subjects
                count = 1 if random.random() < 0.65 else 2
                names = list(SUBJECT_WEIGHTS)
                wanted = []
                while len(wanted) < count:
                    name = random.choices(names, weights=[SUBJECT_WEIGHTS[n] for n in names])[0]
                    if name not in wanted:
                        wanted.append(name)
                # About 60% "Standard", 40% "Guarantee 170+"
                tariff = random.choices([standard, guarantee], weights=[6, 4])[0]

            busy, groups = set(), []
            for subject_name in wanted:
                group = self.free_group(subject_name, taken, busy)
                if group is None:
                    break
                groups.append(group)
                busy |= group.slot_keys
            if len(groups) < len(wanted):
                if tariff.is_package:
                    continue  # no room for the whole package: this family did not come
                if not groups:
                    continue
            self.enroll(student, groups, tariff, taken)

        # Students without any contract (no room) are deleted: every student studies something
        with_contracts = {c.student_id for c in self.contracts}
        Student.objects.exclude(pk__in=with_contracts).delete()
        self.students = [s for s in self.students if s.id in with_contracts]

        # A few contracts with other statuses, to make the data realistic.
        # The model methods do it like the admin: invoices change, extra money -> refund requests.
        candidates = [
            c for c in self.contracts
            if not c.tariff.is_package and c.student_id not in self.demo_ids
            and c.payment_plan == Contract.PaymentPlan.MONTHLY and c.start_date == c.group.start_date
        ]
        random.shuffle(candidates)
        for contract in candidates[:8]:  # about 3%: the student left in October
            contract.terminate(date(self.year_start.year, 10, random.randint(5, 28)))
        for contract in candidates[8:14]:  # about 2%: on pause now (illness, olympiad, ...)
            contract.pause(self.today - timedelta(days=random.randint(3, 20)),
                           random.choice(["хворіє", "олімпіада", "сімейні обставини", ""]))
        for contract in candidates[14:17]:  # a pause in October, the student came back
            contract.pause(date(self.year_start.year, 10, 6), "хворіла")
            contract.refresh_from_db()
            contract.resume(date(self.year_start.year, 10, 27))
        for contract in self.contracts:
            contract.refresh_from_db()

    def members_on(self, group, day):
        """Students who studied in the group on this day (not on a pause)."""
        result = []
        for c in self.by_group.get(group.id, []):
            if c.start_date <= day and (c.end_date is None or c.end_date >= day) and not self.on_pause(c, day):
                result.append(c.student)
        return result

    def on_pause(self, contract, day):
        return any(p.start_date <= day and (p.end_date is None or p.end_date >= day)
                   for p in self.pauses.get(contract.id, []))

    def create_lessons_and_attendance(self):
        # Contracts by group and pauses by contract: read once, used for every lesson
        self.by_group = {}
        for c in Contract.objects.select_related("student"):
            self.by_group.setdefault(c.group_id, []).append(c)
        self.pauses = {}
        for p in ContractPause.objects.all():
            self.pauses.setdefault(p.contract_id, []).append(p)
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
                        # A few lessons of the last week are not marked yet: the teacher forgot the journal
                        if status == Lesson.Status.HELD and now - starts_at < timedelta(days=7) and random.random() < 0.15:
                            status = Lesson.Status.PLANNED
                    else:
                        status = Lesson.Status.PLANNED
                    # Sometimes another teacher replaces the group teacher
                    teacher = group.teacher
                    others = [t for t in self.teachers_for[group.subject.name] if t != group.teacher]
                    if others and random.random() < 0.03:
                        teacher = random.choice(others)
                    note = random.choice(CANCEL_REASONS) if status == Lesson.Status.CANCELLED else ""
                    lesson = Lesson(group=group, teacher=teacher, starts_at=starts_at,
                                    topic=topics[number % len(topics)], status=status, note=note)
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

    def create_lesson_changes(self):
        """Future changes, so the pages show them: a few moved lessons, a cancelled lesson
        and a substitute teacher. We use the model methods (move_to, cancel, replace_teacher),
        so all checks work: a lesson is moved only to a time when everybody is free."""
        now = timezone.now()
        future = list(
            Lesson.objects.filter(status=Lesson.Status.PLANNED, starts_at__gt=now + timedelta(days=1),
                                  starts_at__lt=now + timedelta(days=14))
            .select_related("group__subject", "teacher").order_by("?")
        )
        moved = cancelled = replaced = 0
        for lesson in future:
            if moved < 4:
                # Same time one or two days later; if somebody is busy, try the next lesson
                for days in (1, 2):
                    try:
                        lesson.move_to(lesson.starts_at + timedelta(days=days), random.choice(MOVE_REASONS))
                    except ValidationError:
                        continue
                    moved += 1
                    break
                else:
                    continue
            elif cancelled < 2:
                lesson.cancel(random.choice(CANCEL_REASONS))
                cancelled += 1
            elif replaced < 3:
                for teacher in self.teachers_for[lesson.group.subject.name]:
                    try:
                        lesson.replace_teacher(teacher)
                    except ValidationError:
                        continue
                    replaced += 1
                    break
            else:
                break
        self.stdout.write(f"  Перенесено занять: {moved}, скасовано наперед: {cancelled}, заміни: {replaced}")

    def score_for(self, student, test_day):
        """NMT scale score (100-200): student level + progress over time + some luck.
        Progress: about +5 points a month, luck: -6..+6 (so most students grow, some do not)."""
        progress = (test_day - self.year_start).days // 6
        return max(100, min(200, self.skill[student.id] + progress + random.randint(-6, 6)))

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
        if test.counts_answers:
            # Topic test with tasks: correct answers -> percent (17 of 20 -> 85%)
            raw = round(self.percent_for(student, test_day) * test.max_score / 100)
            return TestResult(test=test, student=student, raw_score=raw, score=test.convert_raw(raw))
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
            # (title, type, day, number of tasks): test №1 in percent, test №2 with 20 tasks
            plan = [
                ("Вхідна діагностика", Test.Kind.DIAGNOSTIC, group.start_date + timedelta(days=3), None),
                ("Тематичний тест №1", Test.Kind.TOPIC, group.start_date + timedelta(days=35), None),
                ("Тематичний тест №2", Test.Kind.TOPIC, group.start_date + timedelta(days=56), 20),
            ]
            for title, kind, day, tasks in plan:
                if day > self.today:
                    continue
                test = Test.objects.create(subject=group.subject, group=group, max_score=tasks,
                                           title=f"{title} ({group.full_name})", kind=kind, held_at=day)
                for student in self.members_on(group, day):
                    # Diagnostic -> test score + NMT score, topic test -> percent (or correct answers)
                    results.append(self.make_result(test, student, day))

        # School-wide trial NMT for every subject (group is empty) every 3 weeks.
        # Scores grow a little over time, so the chart in the student card shows progress.
        subjects_with_groups = {g.subject_id: g.subject for g in self.groups}
        for number, days in enumerate([25, 46, 67, 88, 109, 130], start=1):
            trial_day = self.year_start + timedelta(days=days)
            if trial_day > self.today:
                break
            for subject in subjects_with_groups.values():
                if subject.name == "Українська мова" and days == 46:
                    title = "Пробне НМТ: українська мова"  # the test from the earlier steps keeps its name
                else:
                    title = f"Пробне НМТ №{number}: {subject.name}"
                test = Test.objects.create(subject=subject, group=None, title=title,
                                           kind=Test.Kind.TRIAL, held_at=trial_day)
                # All students of the subject on this day (one student can be in two groups)
                participants = {}
                for group in self.groups:
                    if group.subject_id == subject.id:
                        for student in self.members_on(group, trial_day):
                            participants[student.id] = student
                # In this one test the weakest participant does not pass the threshold
                fail_id = None
                if title == "Пробне НМТ: українська мова" and participants:
                    fail_id = min(participants, key=lambda sid: self.skill[sid])
                for student_id, student in participants.items():
                    results.append(self.make_result(test, student, trial_day, fail=student_id == fail_id))
        TestResult.objects.bulk_create(results)

    def create_invoices_and_payments(self):
        """The invoices already exist (Contract.save() creates them). Here we only add payments
        for invoices whose due date has come; future invoices stay unpaid.
        Every family has its own habit: most pay on time, about 10% are late, a few pay a part."""
        habit = {}
        for student in self.students:
            if student.id in self.demo_ids:
                habit[student.id] = "good"
            else:
                habit[student.id] = random.choices(["good", "late", "partial"], weights=[90, 7, 3])[0]
        for contract in self.contracts:
            due = [i for i in contract.invoices.order_by("period_month") if i.due_date <= self.today]
            for number, invoice in enumerate(due):
                is_last = number == len(due) - 1
                kind = habit[contract.student_id]
                if kind == "late" and is_last:
                    continue  # the last invoice is not paid yet -> overdue, "Без допуску"
                part = Decimal("0.5") if kind == "partial" and is_last else Decimal("1")
                self.pay(invoice, part)

    def pay(self, invoice, part):
        # Most families pay a few days before or after the due date
        day = invoice.due_date + timedelta(days=random.choice([-3, -2, -1, 0, 0, 1, 2, 3, 5]))
        day = min(day, self.today)
        method = random.choices(list(Payment.Method), weights=[2, 4, 2, 3])[0]
        # reserve_amount is calculated automatically for guarantee tariffs
        Payment.objects.create(invoice=invoice, amount=money(invoice.amount * part),
                               paid_at=aware(day, time(random.randint(9, 19), random.choice([0, 15, 30, 45]))),
                               method=method)

    def create_refunds(self):
        # Two families who left in October got money back for the unused part of a month
        left = [c for c in self.contracts if c.status == Contract.Status.TERMINATED]
        for contract in left[:2]:
            paid_day = contract.end_date + timedelta(days=5)
            Refund.objects.create(
                contract=contract, reason=Refund.Reason.EARLY_TERMINATION,
                amount=money(contract.monthly_amount() / 4),
                status=Refund.Status.PAID, created_at=contract.end_date, paid_at=paid_day,
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
                (Expense.Category.RENT, "45000", 1, "Оренда приміщення (4 класи)"),
                (Expense.Category.UTILITIES, str(random.randint(6000, 9000)), 15, "Світло, вода, інтернет"),
                (Expense.Category.MARKETING, str(random.randint(12000, 20000)), 5, "Реклама в Instagram і TikTok"),
                (Expense.Category.MATERIALS, str(random.randint(5000, 9000)), 8, "Друк збірників і пробних тестів"),
            ]
            if month == self.year_start:
                items.append((Expense.Category.EQUIPMENT, "60000", 3, "Проєктори і дошки в 4 класи"))
                items.append((Expense.Category.SOFTWARE, "1200", 3, "Підписка на сервіс онлайн-тестів"))
            for category, amount, day, comment in items:
                spent = month.replace(day=day)
                if spent <= self.today:
                    Expense.objects.create(category=category, amount=Decimal(amount), spent_at=spent, comment=comment)
            month = add_month(month)

    def create_demo_logins(self):
        """Roles + demo logins: "director" (administrator), "manager", "accountant"
        and one login for every teacher.
        Logins are not deleted by --clear: they are linked again to the new teachers."""
        groups = setup_role_groups()
        User = get_user_model()
        self.logins = []
        for username, first_name, group_name in [
            (DEMO_ADMIN, "Директор", ADMIN_GROUP),
            ("manager", "Олена (менеджер)", MANAGER_GROUP),
            ("accountant", "Ірина (бухгалтер)", ACCOUNTANT_GROUP),
        ]:
            user, _ = User.objects.get_or_create(username=username, defaults={"first_name": first_name})
            user.is_staff = True  # the admin opens; the group permissions decide what is there
            user.is_active = True
            user.set_password(DEMO_PASSWORD)
            user.save()
            user.groups.set([groups[group_name]])
            self.logins.append(username)
        for teacher in self.teachers:
            user, _ = create_teacher_login(teacher, DEMO_PASSWORD)
            user.set_password(DEMO_PASSWORD)  # the same demo password every time
            user.save()
            self.logins.append(user.username)

    def create_debt_calls(self):
        """Calls to parents of debtors (page "До дзвінка"), so the page shows every case:
        not called yet, no answer, called today, promised to pay, a broken promise."""
        from school.views import debtors_to_call  # the same list as on the page

        director = get_user_model().objects.get(username=DEMO_ADMIN)
        now = timezone.now()
        calls = []
        for row in debtors_to_call(self.today):
            first_day = max(row["oldest_due"], self.today - timedelta(days=10))
            days_ago = random.randint(0, max(0, (self.today - first_day).days))
            called_at = timezone.make_aware(datetime.combine(self.today - timedelta(days=days_ago), time(random.randint(10, 18), random.choice([0, 15, 30, 45]))))
            if called_at > now:
                called_at = now - timedelta(minutes=random.randint(5, 60))
            call = DebtCall(student=row["student"], called_at=called_at, user=director)
            luck = random.random()
            if luck < 0.3:
                continue  # not called yet
            elif luck < 0.55:
                call.result = DebtCall.Result.NO_ANSWER
            elif luck < 0.8:
                call.result = DebtCall.Result.PROMISED
                call.promised_date = self.today + timedelta(days=random.randint(1, 7))
                call.called_at = now - timedelta(days=random.randint(0, 2), hours=1)
                call.comment = random.choice(["Сплатять після зарплати", "Тато переведе на картку", ""])
            elif luck < 0.9:
                # Promised, the day passed, but no money: the student is back at the top
                call.result = DebtCall.Result.PROMISED
                call.called_at = now - timedelta(days=6)
                call.promised_date = self.today - timedelta(days=2)
                call.comment = "Обіцяли до кінця тижня"
            else:
                call.result = DebtCall.Result.REACHED
                call.comment = "Нагадали про оплату, перезвонять"
            calls.append(call)
        DebtCall.objects.bulk_create(calls)  # bulk_create: clean() is not called, the data is checked above

    def print_summary(self):
        rows = [
            ("Предмети", Subject), ("Таблиці переведення", ConversionTable), ("Викладачі", Teacher), ("Тарифи", Tariff), ("Учні", Student),
            ("Групи", StudyGroup), ("Розклад (занять на тиждень)", GroupSchedule),
            ("Договори", Contract), ("Заняття", Lesson),
            ("Відвідуваність", Attendance), ("Тести", Test), ("Результати тестів", TestResult),
            ("Рахунки", Invoice), ("Оплати", Payment), ("Повернення", Refund),
            ("Зарплати", Salary), ("Витрати", Expense), ("Дзвінки боржникам", DebtCall),
        ]
        self.stdout.write(self.style.SUCCESS("Тестові дані створено:"))
        for label, model in rows:
            self.stdout.write(f"  {label}: {model.objects.count()}")
        self.stdout.write(self.style.SUCCESS(
            f"Демо-логіни (пароль «{DEMO_PASSWORD}»): {', '.join(self.logins)}"
        ))
        self.stdout.write("  director — адміністратор, manager — менеджер, accountant — бухгалтер; інші — викладачі (свої групи).")