"""
Forms of the website (not the admin).
StudentForm    - data of a new student and parents.
EnrollmentForm - "Записати в групу": one contract (group, tariff, plan, discount, start date).
Django forms check the data AND call the model checks (Contract.clean): free places,
old tariff, start before the group, etc. So the rules are the same as in the admin.
"""
from django import forms
from django.db.models import Count, Q
from django.utils import timezone

from .models import Contract, Student, StudyGroup, Tariff


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
            "grade": forms.NumberInput(attrs={"min": 1, "max": 11}),
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
    """Groups in the list show free places: "Математика · група 1 — вільно 3 з 12"."""

    def label_from_instance(self, group):
        free = group.max_students - group.taken
        places = f"вільно {free} з {group.max_students}" if free > 0 else "немає місць"
        return f"{group.full_name} — {group.teacher} — {places}"


class TariffChoiceField(forms.ModelChoiceField):
    """Tariffs in the list show the price: "Стандарт — 2 400 ₴/міс, 8 занять"."""

    def label_from_instance(self, tariff):
        price = f"{tariff.monthly_price:,.0f}".replace(",", " ")
        guarantee = " · з гарантією" if tariff.has_guarantee else ""
        return f"{tariff.name} — {price} ₴/міс, {tariff.lessons_per_month} занять{guarantee}"


class EnrollmentForm(BootstrapMixin, forms.ModelForm):
    """One contract = the student goes to one group.
    student: if we already know the student (card of the student), the field is hidden.
    """

    group = GroupChoiceField(queryset=StudyGroup.objects.none(), label="Група")
    tariff = TariffChoiceField(queryset=Tariff.objects.none(), label="Тариф")

    class Meta:
        model = Contract
        fields = ["student", "group", "tariff", "payment_plan", "discount_pct", "start_date"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "discount_pct": forms.NumberInput(attrs={"min": 0, "max": 100, "step": "0.01"}),
        }
        labels = {"discount_pct": "Знижка, %"}

    def __init__(self, *args, student=None, **kwargs):
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fixed_student = student
        if student is not None:
            # The student is known: no field, we set it ourselves
            del self.fields["student"]
            self.instance.student = student
        else:
            self.fields["student"].queryset = Student.objects.order_by("last_name", "first_name")
            self.fields["student"].label = "Учень"

        # Only groups that have not finished yet. "taken" = students who keep a place.
        self.fields["group"].queryset = (
            StudyGroup.objects.filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
            .select_related("subject", "teacher")
            .annotate(taken=Count("contracts", filter=Q(contracts__status__in=Contract.OPEN_STATUSES)))
            .order_by("subject__name", "name")
        )
        self.fields["tariff"].queryset = Tariff.objects.filter(is_active=True)
        self.fields["start_date"].initial = today
        self.fields["start_date"].help_text = "Не раніше старту групи. Перший місяць рахується за заняттями з цієї дати."
        self.fields["payment_plan"].help_text = ""
        self.add_bootstrap()

    def clean(self):
        data = super().clean()
        student = self.fixed_student or data.get("student")
        group = data.get("group")
        # A student cannot be in the same group twice
        if student and student.pk and group and Contract.objects.filter(
            student=student, group=group, status__in=Contract.OPEN_STATUSES
        ).exists():
            raise forms.ValidationError({"group": f"{student} уже навчається в цій групі."})
        return data