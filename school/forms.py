"""
Forms of the website (not the admin).
StudentForm    - data of a new student and parents.
EnrollmentForm - "Записати в групу": one contract (group, tariff, plan, discount, start date).
DebtCallForm   - "Записати дзвінок" on the page "До дзвінка".
TeacherForm    - "Новий викладач" on the page "Доступ".
Django forms check the data AND call the model checks (Contract.clean): free places,
old tariff, start before the group, etc. So the rules are the same as in the admin.
"""
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils import timezone

from .models import Contract, DebtCall, Student, StudyGroup, Tariff, Teacher, Test


class BootstrapMixin:
    """Add Bootstrap classes to every field, so we do not write them in the template."""

    def add_bootstrap(self):
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class StudentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Student
        fields = ["last_name", "first_name", "grade", "phone", "parent_name", "parent_phone"]
        widgets = {
            "grade": forms.NumberInput(attrs={"min": 1, "max": 11, "placeholder": "випускник"}),
            "phone": forms.TextInput(attrs={"placeholder": "067 123 45 67", "inputmode": "tel"}),
            "parent_phone": forms.TextInput(attrs={"placeholder": "067 123 45 67", "inputmode": "tel"}),
            "parent_name": forms.TextInput(attrs={"placeholder": "Прізвище Ім'я По батькові"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["grade"].initial = 11
        self.add_bootstrap()

    def clean(self):
        data = super().clean()
        # The same student twice? Same last name + first name + parents' phone -> warn.
        # Example: two admins enter the same child -> two cards, debts are split.
        last, first, phone = data.get("last_name"), data.get("first_name"), data.get("parent_phone")
        if last and first and phone:
            same = Student.objects.filter(
                last_name__iexact=last.strip(), first_name__iexact=first.strip(),
                parent_phone=Student.normalize_phone(phone),
            ).first()
            if same:
                raise forms.ValidationError(
                    f"Такий учень уже є: {same} (картка №{same.pk}). Відкрийте його картку і натисніть "
                    "«Записати в групу», щоб не було двох карток."
                )
        return data


class GroupChoiceField(forms.ModelChoiceField):
    """Groups in the list show free places: "Математика · група 1 — Бондар Олег — вільно 3 з 12"."""

    def label_from_instance(self, group):
        free = group.max_students - group.taken
        places = f"вільно {free} з {group.max_students}" if free > 0 else "немає місць"
        return f"{group.full_name} — {group.teacher} — {places}"


class TariffChoiceField(forms.ModelChoiceField):
    """Tariffs in the list show the price:
    "Стандарт — 2 400 ₴/міс" or "Пакет «3 предмети» — 6 000 ₴/міс за 3 предмети"."""

    def label_from_instance(self, tariff):
        price = f"{tariff.monthly_price:,.0f}".replace(",", " ")
        what = f" за {tariff.subjects_count} предмети" if tariff.is_package else ""
        guarantee = " · з гарантією" if tariff.has_guarantee else ""
        return f"{tariff.name} — {price} ₴/міс{what}{guarantee}"


# Field names of groups: a package of 4 subjects needs 4 groups
GROUP_FIELDS = ["group", "group_2", "group_3", "group_4"]


class EnrollmentForm(BootstrapMixin, forms.Form):
    """"Записати в групу": ONE tariff and its groups.
    A tariff for one subject -> 1 group. A package of 3 (4) subjects -> 3 (4) groups of DIFFERENT subjects.
    Every group becomes its own contract (journal, pauses, debts work per group as before).
    If the student already studies a subject in the SAME group (for example, Math alone) and the
    package includes it, this contract moves into the package (Contract.switch_tariff).

    student: if we already know the student (card of the student), there is no student field.
    """

    student = forms.ModelChoiceField(queryset=Student.objects.none(), label="Учень")
    tariff = TariffChoiceField(queryset=Tariff.objects.none(), label="Тариф")
    group = GroupChoiceField(queryset=StudyGroup.objects.none(), label="Група")
    group_2 = GroupChoiceField(queryset=StudyGroup.objects.none(), label="Група — предмет 2", required=False)
    group_3 = GroupChoiceField(queryset=StudyGroup.objects.none(), label="Група — предмет 3", required=False)
    group_4 = GroupChoiceField(queryset=StudyGroup.objects.none(), label="Група — предмет 4", required=False)
    payment_plan = forms.ChoiceField(choices=Contract.PaymentPlan.choices, label="Оплата",
                                     initial=Contract.PaymentPlan.MONTHLY)
    discount_pct = forms.DecimalField(label="Знижка, %", min_value=0, max_value=100, decimal_places=2,
                                      initial=0, widget=forms.NumberInput(attrs={"step": "0.01"}))
    start_date = forms.DateField(
        label="Дата початку",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Якщо група стартує пізніше — візьметься дата старту групи. "
                  "Перший неповний місяць рахується за заняттями.",
    )

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fixed_student = student
        if student is not None:
            # The student is known: no field, we use him ourselves
            del self.fields["student"]
        else:
            self.fields["student"].queryset = Student.objects.order_by("last_name", "first_name")

        # Only groups that have not finished yet. "taken" = students who keep a place.
        groups = (
            StudyGroup.objects.filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .select_related("subject", "teacher")
            .annotate(taken=Count("contracts", filter=Q(contracts__status__in=Contract.OPEN_STATUSES)))
            .order_by("subject__name", "name")
        )
        for name in GROUP_FIELDS:
            self.fields[name].queryset = groups
        self.fields["tariff"].queryset = Tariff.objects.filter(is_active=True).order_by("subjects_count", "monthly_price")
        self.fields["start_date"].initial = today
        self.planned = []  # [(contract, contract_it_replaces or None)] after is_valid()
        self.add_bootstrap()

    def chosen_groups(self):
        """[(field name, group)] for the groups this tariff needs."""
        tariff = self.cleaned_data.get("tariff")
        count = tariff.subjects_count if tariff else 1
        return [(name, self.cleaned_data.get(name)) for name in GROUP_FIELDS[:count]]

    def clean(self):
        data = super().clean()
        tariff = data.get("tariff")
        student = self.fixed_student or data.get("student")
        start = data.get("start_date")
        if not tariff or not start or self.errors:
            return data

        # 1. Every subject of the package has a group, and the subjects are different
        subjects = {}
        for name, group in self.chosen_groups():
            if group is None:
                self.add_error(name, f"Оберіть групу: у тарифі {tariff.subjects_count} предмети.")
            elif group.subject_id in subjects:
                self.add_error(name, f"«{group.subject.name}» уже вибрано. У пакеті — різні предмети.")
            else:
                subjects[group.subject_id] = group
        if self.errors:
            return data

        # 2. Build the contracts and check them with the model rules (free places, dates, one subject once)
        self.planned = []
        for name, group in self.chosen_groups():
            replaces = None
            if student is not None and student.pk:
                replaces = Contract.objects.filter(
                    student=student, group=group, status__in=Contract.OPEN_STATUSES
                ).select_related("tariff").first()
                if replaces is not None and replaces.tariff_id == tariff.pk:
                    self.add_error(name, f"{student} уже навчається в цій групі за цим тарифом.")
                    continue
            contract = Contract(
                group=group, tariff=tariff, payment_plan=data["payment_plan"],
                discount_pct=data["discount_pct"],
                # A group can start later than the chosen day: then we take the group start
                start_date=max(start, group.start_date),
            )
            if student is not None and student.pk:
                contract.student = student
            if replaces is not None:
                contract._replaces = replaces  # Math alone -> Math in the package (same group)
            # Without "student": a new student is not saved yet, and when a contract replaces
            # the old one in the same group, the database rule "one open contract per group"
            # would see the old one (it closes only when we save). Contract.clean still checks
            # free places and "one subject once" with the student.
            exclude = ["student"] if (replaces is not None or not (student and student.pk)) else None
            try:
                contract.full_clean(exclude=exclude)
            except ValidationError as error:
                for field, messages in error.message_dict.items():
                    self.add_error(name if field in ("group", "start_date", "end_date", "__all__") else
                                   (field if field in self.fields else None), messages)
                continue
            self.planned.append((contract, replaces))
        return data

    def subject_ids(self):
        """Subjects of this block: to check that two blocks do not repeat a subject."""
        return {contract.group.subject_id for contract, _ in self.planned}

    def save(self, student):
        """Save the contracts (call inside transaction.atomic). Returns the new contracts."""
        saved = []
        for contract, replaces in self.planned:
            if replaces is not None:
                # The same group, another tariff: old price before the start, new price after
                saved.append(replaces.switch_tariff(contract.tariff, contract.start_date, contract.discount_pct))
            else:
                contract.student = student
                contract.save()  # save() also creates the invoices
                saved.append(contract)
        return saved


class SwitchTariffForm(BootstrapMixin, forms.Form):
    """A package broke (the student left one subject): the other subjects of the package
    move to a tariff for one subject from the chosen day (rule "Б", 11.11.2026)."""

    tariff = TariffChoiceField(queryset=Tariff.objects.none(), label="Новий тариф для решти предметів")
    from_day = forms.DateField(
        label="З якої дати", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Зазвичай — 1-ше число наступного місяця: поточний місяць лишається за ціною пакета.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tariff"].queryset = Tariff.objects.filter(is_active=True, subjects_count=1)
        self.add_bootstrap()

class TestForm(BootstrapMixin, forms.ModelForm):
    """"Новий тест" on the page of a group. The subject comes from the group.
    "Для всіх груп предмета" = a school-wide test (for example, a trial NMT for everybody)."""

    for_all_groups = forms.BooleanField(
        label="Для всіх груп предмета", required=False,
        help_text="Наприклад, пробне НМТ для всієї школи: тест буде видно в кожній групі цього предмета.",
    )

    class Meta:
        model = Test
        fields = ["title", "kind", "max_score", "held_at"]
        widgets = {
            "held_at": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "title": forms.TextInput(attrs={"placeholder": "Наприклад: Пробне НМТ №2"}),
            "max_score": forms.NumberInput(attrs={"placeholder": "Наприклад: 20", "min": 1}),
        }

    def __init__(self, *args, group, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group
        self.instance.subject = group.subject
        self.fields["held_at"].initial = timezone.localdate()
        self.fields["kind"].initial = Test.Kind.TRIAL
        self.add_bootstrap()
        self.fields["for_all_groups"].widget.attrs["class"] = "form-check-input"

    def clean(self):
        cleaned = super().clean()
        # The field "Кількість завдань" is hidden for NMT tests (JavaScript),
        # so a number left there from a topic test must not give an error
        if cleaned.get("kind") != Test.Kind.TOPIC:
            cleaned["max_score"] = None
            self.instance.max_score = None
        return cleaned

    def save(self, commit=True):
        self.instance.group = None if self.cleaned_data.get("for_all_groups") else self.group
        return super().save(commit)


class DebtCallForm(BootstrapMixin, forms.ModelForm):
    """"Записати дзвінок": the result of a call to parents.
    The student, the time and who called are set by the view, not by the form."""

    class Meta:
        model = DebtCall
        fields = ["result", "promised_date", "comment"]
        widgets = {
            "result": forms.RadioSelect,
            "promised_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "comment": forms.TextInput(attrs={"placeholder": "Наприклад: тато заплатить після зарплати"}),
        }

    def __init__(self, *args, student=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.student = student
        self.instance.user = user
        self.add_bootstrap()
        self.fields["result"].widget.attrs["class"] = "form-check-input"

class TeacherForm(BootstrapMixin, forms.ModelForm):
    """"Новий викладач" on the page "Доступ": the teacher and (if a password is given) his login.
    Without a login the teacher still can be chosen for groups; the login can be added later."""

    username = forms.CharField(label="Логін", required=False,
                               help_text="Порожньо — система запропонує прізвище латиницею.")
    password = forms.CharField(label="Пароль", required=False, widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                               help_text="Порожньо — без логіну (можна створити пізніше в таблиці вище).")

    class Meta:
        model = Teacher
        fields = ["last_name", "first_name", "phone", "rate_per_lesson"]
        widgets = {
            "phone": forms.TextInput(attrs={"placeholder": "067 123 45 67", "inputmode": "tel"}),
            "rate_per_lesson": forms.NumberInput(attrs={"min": 0, "step": 10, "placeholder": "400"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_bootstrap()

    def clean(self):
        data = super().clean()
        last, first = data.get("last_name"), data.get("first_name")
        if last and first and Teacher.objects.filter(last_name__iexact=last.strip(), first_name__iexact=first.strip()).exists():
            raise forms.ValidationError(f"Викладач {last} {first} уже є. Створіть йому логін у таблиці вище.")
        if data.get("username") and not data.get("password"):
            self.add_error("password", "Вкажіть пароль для логіну (або залиште порожнім і логін).")
        return data
