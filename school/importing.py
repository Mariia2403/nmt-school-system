"""
Import of students from Excel (.xlsx) or CSV.

How it works:
1. read_table()  - reads the file into a list of rows {column key: text}.
2. check_rows()  - checks every row with the SAME rules as the site forms
                   (StudentForm, EnrollmentForm), nothing is saved yet. The admin sees a preview.
3. import_rows() - saves the good rows in one transaction. Every row is checked again,
                   because the database could change between the preview and the import.

Columns of the file (the first row = headers, the order does not matter):
Прізвище, Ім'я, Клас, Телефон учня, ПІБ батьків, Телефон батьків - the student;
Група, Тариф, Дата початку - optional: enroll the student at once.
Several groups in one cell: "Математика · група 1; Історія України · група 2".
"""
import csv
import io
import re
from datetime import date, datetime

from django.db import transaction
from django.utils import timezone

from .forms import GROUP_FIELDS, EnrollmentForm, StudentForm
from .models import Contract, Student, StudyGroup, Tariff

MAX_ROWS = 1000
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB is enough for thousands of students

# Column header (lower case, without spaces at the ends) -> key in a row
COLUMNS = {
    "прізвище": "last_name",
    "ім'я": "first_name", "імя": "first_name", "ім’я": "first_name",
    "клас": "grade",
    "телефон учня": "phone", "телефон": "phone",
    "піб батьків": "parent_name", "батьки": "parent_name",
    "телефон батьків": "parent_phone",
    "група": "groups", "групи": "groups",
    "тариф": "tariff",
    "дата початку": "start_date",
}
# Headers for the template file, in this order
TEMPLATE_HEADERS = ["Прізвище", "Ім'я", "Клас", "Телефон учня", "ПІБ батьків", "Телефон батьків",
                    "Група", "Тариф", "Дата початку"]


class ImportFileError(Exception):
    """The file cannot be read at all (wrong type, no headers, too big)."""


def has_excel_support():
    """True if the library openpyxl is installed (needed only for .xlsx)."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return False
    return True


def cell_text(value):
    """A cell -> clean text. Excel gives numbers and dates, not only text:
    11.0 -> "11", a date -> "07.09.2026", a phone 380671234567 -> "380671234567"."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def rows_from_table(table):
    """A list of rows (lists of cells) -> list of dicts by COLUMNS.
    The first non-empty row is the header row. Empty rows are skipped."""
    rows = [[cell_text(c) for c in row] for row in table]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise ImportFileError("Файл порожній.")
    header = [COLUMNS.get(h.lower().strip()) for h in rows[0]]
    if "last_name" not in header or "first_name" not in header:
        raise ImportFileError(
            "У першому рядку мають бути назви колонок, щонайменше «Прізвище» та «Ім'я». "
            "Завантажте шаблон і заповніть його."
        )
    result = []
    for number, row in enumerate(rows[1:], start=2):  # number = row number in Excel
        data = {key: (row[i] if i < len(row) else "") for i, key in enumerate(header) if key}
        data["row"] = number
        result.append(data)
    if len(result) > MAX_ROWS:
        raise ImportFileError(f"Забагато рядків: {len(result)}. За один раз — до {MAX_ROWS}.")
    return result


def read_table(uploaded):
    """Read an uploaded .xlsx or .csv file into rows."""
    if uploaded.size > MAX_FILE_SIZE:
        raise ImportFileError("Файл завеликий (більше 2 МБ).")
    name = uploaded.name.lower()
    content = uploaded.read()
    if name.endswith(".xlsx"):
        if not has_excel_support():
            raise ImportFileError("Для файлів .xlsx потрібна бібліотека openpyxl (pip install openpyxl). "
                                  "Або збережіть таблицю як CSV.")
        from openpyxl import load_workbook
        try:
            sheet = load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
        except Exception:
            raise ImportFileError("Не вдалося відкрити файл Excel. Перевірте, що це справжній .xlsx.")
        return rows_from_table(sheet.iter_rows(values_only=True))
    if name.endswith(".csv"):
        # Excel saves CSV in Windows-1251 or UTF-8; columns with ";" or ","
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ImportFileError("Не вдалося прочитати текст файлу (кодування).")
        delimiter = ";" if text.split("\n", 1)[0].count(";") >= text.split("\n", 1)[0].count(",") else ","
        return rows_from_table(csv.reader(io.StringIO(text), delimiter=delimiter))
    raise ImportFileError("Потрібен файл .xlsx або .csv.")


def normalize_name(text):
    """"Математика · група 1", "математика група 1", "Математика - група 1" -> "математика група 1"."""
    return re.sub(r"[\s·\-–—_]+", " ", text.lower()).strip()


def parse_date(text):
    """"07.09.2026" or "2026-09-07" -> date; empty -> None; wrong -> ValueError."""
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(text)


class Lookup:
    """Groups and tariffs by name, read once for the whole file."""

    def __init__(self):
        today = timezone.localdate()
        self.groups = {
            normalize_name(g.full_name): g
            for g in StudyGroup.objects.filter(end_date__gte=today).select_related("subject")
        }
        self.tariffs = {t.name.lower().strip(): t for t in Tariff.objects.filter(is_active=True)}


def enrollment_forms(row, lookup, errors):
    """Build EnrollmentForm(s) for the "Група / Тариф / Дата початку" of a row.
    A tariff for one subject + 2 groups -> 2 forms; a package of 3 + 3 groups -> 1 form."""
    names = [n.strip() for n in re.split(r"[;\n]", row.get("groups", "")) if n.strip()]
    if not names:
        if row.get("tariff"):
            errors.append("Тариф вказано, а групу — ні.")
        return []
    groups = []
    for name in names:
        group = lookup.groups.get(normalize_name(name))
        if group is None:
            errors.append(f"Немає групи «{name}» (або вона вже завершилась). Назва — як на сайті: «Математика · група 1».")
        else:
            groups.append(group)
    tariff = lookup.tariffs.get(row.get("tariff", "").lower().strip())
    if not row.get("tariff"):
        errors.append("Вкажіть тариф для групи.")
    elif tariff is None:
        errors.append(f"Немає діючого тарифу «{row['tariff']}».")
    try:
        start = parse_date(row.get("start_date", "")) or timezone.localdate()
    except ValueError:
        errors.append(f"Дата «{row['start_date']}» — потрібен формат 07.09.2026.")
        start = timezone.localdate()
    if errors:
        return []

    def form_for(chosen):
        data = {"tariff": tariff.pk, "payment_plan": Contract.PaymentPlan.MONTHLY,
                "discount_pct": "0", "start_date": start.isoformat()}
        for field, group in zip(GROUP_FIELDS, chosen):
            data[field] = group.pk
        return EnrollmentForm(data, student=Student())

    if tariff.is_package:
        if len(groups) != tariff.subjects_count:
            errors.append(f"У тарифі «{tariff.name}» {tariff.subjects_count} предмети — вкажіть стільки ж груп через «;».")
            return []
        return [form_for(groups)]
    subjects = [g.subject_id for g in groups]
    if len(subjects) != len(set(subjects)):
        errors.append("Два рази один предмет. Один предмет — лише один раз.")
        return []
    return [form_for([g]) for g in groups]


def form_errors(form):
    """All error texts of a Django form in one list."""
    return [str(e) for errors in form.errors.values() for e in errors]


def grade_value(text):
    """The column "Клас": "11" -> "11"; empty or "випускник" -> "" (a graduate of past years)."""
    text = str(text or "").strip()
    return "" if text.lower().startswith("вип") else text


def check_row(row, lookup, seen, taken):
    """Check one row. Returns a dict for the preview:
    status "ok" (will be added), "skip" (this student already exists) or "error".
    seen  - students already met in this file (to skip duplicates inside the file);
    taken - places in groups that earlier rows of the file will take."""
    result = {"row": row["row"], "data": row, "status": "ok", "messages": [], "groups": []}
    student_form = StudentForm({
        "last_name": row.get("last_name", ""), "first_name": row.get("first_name", ""),
        "grade": grade_value(row.get("grade", "")), "phone": row.get("phone", ""),
        "parent_name": row.get("parent_name", ""), "parent_phone": row.get("parent_phone", ""),
    })
    if not student_form.is_valid():
        if "уже є" in " ".join(student_form.non_field_errors()):
            result["status"] = "skip"
            result["messages"] = ["Такий учень уже є в системі — пропустимо."]
            return result
        result["status"] = "error"
        labels = {name: field.label for name, field in student_form.fields.items()}
        result["messages"] = [f"{labels.get(f, f)}: {' '.join(str(e) for e in errs)}"
                              for f, errs in student_form.errors.items()]
        return result

    key = (row["last_name"].strip().lower(), row["first_name"].strip().lower(),
           Student.normalize_phone(row.get("parent_phone", "")))
    if key in seen:
        result["status"] = "skip"
        result["messages"] = [f"Той самий учень уже є в рядку {seen[key]} файлу — пропустимо."]
        return result
    seen[key] = row["row"]

    errors = []
    forms = enrollment_forms(row, lookup, errors)
    for form in forms:
        if not form.is_valid():
            errors.extend(form_errors(form))
            continue
        for contract, _ in form.planned:
            group = contract.group
            free = group.max_students - Contract.objects.filter(
                group=group, status__in=Contract.OPEN_STATUSES).count() - taken.get(group.pk, 0)
            if free <= 0:
                errors.append(f"У групі «{group.full_name}» немає місць (з урахуванням рядків вище).")
            else:
                result["groups"].append(group.full_name)
    if errors:
        result["status"] = "error"
        result["messages"] = errors
        result["groups"] = []
        return result
    for form in forms:
        for contract, _ in form.planned:
            taken[contract.group_id] = taken.get(contract.group_id, 0) + 1
    if forms:
        result["messages"] = [f"Записати: {', '.join(result['groups'])} ({forms[0].cleaned_data['tariff'].name})"]
    return result


def check_rows(rows):
    """Check all rows (nothing is saved). Returns (results, counts)."""
    lookup, seen, taken = Lookup(), {}, {}
    results = [check_row(row, lookup, seen, taken) for row in rows]
    counts = {status: sum(1 for r in results if r["status"] == status) for status in ("ok", "skip", "error")}
    return results, counts


def import_rows(rows):
    """Save the good rows. Every row is checked again right before saving
    (the free places are counted from the database, with the rows saved just before).
    Returns counts {"added", "enrolled", "skip", "error"} and the errors [(row, text)]."""
    lookup, seen = Lookup(), {}
    counts = {"added": 0, "enrolled": 0, "skip": 0, "error": 0}
    errors = []
    with transaction.atomic():
        for row in rows:
            result = check_row(row, lookup, seen, taken={})
            if result["status"] != "ok":
                counts["skip" if result["status"] == "skip" else "error"] += 1
                if result["status"] == "error":
                    errors.append((row["row"], " ".join(result["messages"])))
                continue
            # One row = its own savepoint: if this row fails, the rows before it stay
            try:
                with transaction.atomic():
                    student_data = {k: row.get(k, "") for k in
                                    ("last_name", "first_name", "phone", "parent_name", "parent_phone")}
                    student_data["grade"] = grade_value(row.get("grade", ""))
                    student_form = StudentForm(student_data)
                    student_form.is_valid()
                    student = student_form.save()
                    enrolled = 0
                    for form in enrollment_forms(row, lookup, []):
                        form.is_valid()
                        enrolled += len(form.save(student))
            except Exception as error:  # a rare case: the data changed right now
                counts["error"] += 1
                errors.append((row["row"], f"Не збережено: {error}"))
                continue
            counts["added"] += 1
            counts["enrolled"] += enrolled
    return counts, errors


def template_workbook():
    """The template file: sheet "Учні" (headers + 2 examples) and sheet "Довідка"
    with the exact names of groups and tariffs to copy."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    lookup = Lookup()
    book = Workbook()
    sheet = book.active
    sheet.title = "Учні"
    sheet.append(TEMPLATE_HEADERS)
    groups = sorted(lookup.groups.values(), key=lambda g: (g.subject.name, g.name))
    single = next((t for t in lookup.tariffs.values() if not t.is_package), None)
    today = timezone.localdate().strftime("%d.%m.%Y")
    sheet.append(["Приклад", "Оксана", 11, "067 123 45 67", "Приклад Ірина Петрівна", "050 765 43 21",
                  groups[0].full_name if groups else "", single.name if single else "", today])
    sheet.append(["Прикладенко", "Тарас", 10, "", "Прикладенко Олег Іванович", "093 000 11 22", "", "", ""])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
    for column, width in zip("ABCDEFGHI", [16, 12, 7, 16, 28, 18, 34, 22, 14]):
        sheet.column_dimensions[column].width = width

    info = book.create_sheet("Довідка")
    info.append(["Групи (копіюйте назву в колонку «Група»; кілька груп — через «;»)", "Викладач", "Вільно місць"])
    for group in groups:
        free = group.max_students - group.contracts.filter(status__in=Contract.OPEN_STATUSES).count()
        info.append([group.full_name, str(group.teacher), free])
    info.append([])
    info.append(["Тарифи (колонка «Тариф»)", "Ціна за місяць", "Предметів"])
    for tariff in lookup.tariffs.values():
        info.append([tariff.name, float(tariff.monthly_price), tariff.subjects_count])
    info.append([])
    info.append(["Клас — число 1–11 (порожньо або «випускник» — випускник минулих років). Телефони — у будь-якому вигляді. Дата — 07.09.2026 (порожньо = сьогодні)."])
    info.append(["Рядок без групи — учень додається без запису в групу."])
    for row in (1, len(groups) + 3):
        for cell in info[row]:
            cell.font = Font(bold=True)
    info.column_dimensions["A"].width = 60
    info.column_dimensions["B"].width = 22
    info.column_dimensions["C"].width = 14
    return book