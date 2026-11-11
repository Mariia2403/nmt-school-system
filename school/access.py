"""
Roles and access rules of the school website.

There are four roles (Django groups):
- "Адміністратор" (director): sees and does everything.
- "Менеджер": students, enrollment and import, groups and lessons (schedule), journal, tests,
  calls to parents, payments. No finance page, no tariffs, no logins.
- "Бухгалтер": finance, payments, calls to parents; students and groups only to look at.
- "Викладач": only HIS groups, their students, the journal of his lessons and test results.
  He does not see money: no finance, no amounts of debts.
Administrator, manager and accountant are "office" roles: they see ALL students and groups.

A superuser is always an administrator.
A teacher is a user who is linked to a Teacher record (Teacher.user).

Pages check ABILITIES, not role names: @role_page("payments") means "who may enter payments".
So to change what a role can do, we change only ROLE_ABILITIES below.
All pages take data ONLY through groups_for, lessons_for, students_for,
so a teacher cannot open a strange group even if he types its address by hand.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import Lesson, Student, StudyGroup, Subject, Teacher

ADMIN_GROUP = "Адміністратор"
MANAGER_GROUP = "Менеджер"
ACCOUNTANT_GROUP = "Бухгалтер"
TEACHER_GROUP = "Викладач"

# Office roles: group name -> short role code. The order matters: the first one wins
# (a user in two groups gets the stronger role).
OFFICE_ROLES = {ADMIN_GROUP: "admin", MANAGER_GROUP: "manager", ACCOUNTANT_GROUP: "accountant"}
ROLE_LABELS = {"admin": "адміністратор", "manager": "менеджер", "accountant": "бухгалтер"}

# What every office role can do. The names of abilities:
#   office    - sees all students, groups and lessons (not only his own like a teacher)
#   money     - sees amounts: debts, invoices, tariffs of students
#   enroll    - new students, enrollment, import, pause / leave, package changes
#   schedule  - lessons: cancel / move / another teacher, "update lessons by schedule"
#   groups    - finish or delete a group (money and contracts change)
#   journal   - marks attendance in any lesson (a teacher - only in his lessons)
#   tests     - creates tests and enters scores in any group
#   calls     - the page "До дзвінка" and "Записати дзвінок"
#   payments  - "Внести оплату"
#   finance   - the page "Фінанси" (income, salaries, profit)
#   tariffs   - the page "Тарифи"
#   access    - the page "Доступ" (logins)
ALL_ABILITIES = {"office", "money", "enroll", "schedule", "groups", "journal", "tests",
                 "calls", "payments", "finance", "tariffs", "access"}
ROLE_ABILITIES = {
    "admin": ALL_ABILITIES,
    "manager": {"office", "money", "enroll", "schedule", "journal", "tests", "calls", "payments"},
    "accountant": {"office", "money", "calls", "payments", "finance"},
}


def _role(user):
    """Calculate the role once per request and keep it on the user object.
    Returns (office role code or None, teacher or None)."""
    if not user.is_authenticated:
        return None, None
    if not hasattr(user, "_school_role"):
        if user.is_superuser:
            role = "admin"
        else:
            names = set(user.groups.values_list("name", flat=True))
            role = next((code for name, code in OFFICE_ROLES.items() if name in names), None)
        teacher = Teacher.objects.filter(user=user, is_active=True).first()
        user._school_role = (role, teacher)
    return user._school_role


def office_role(user):
    """"admin", "manager", "accountant" or None (a teacher or a user without a role)."""
    return _role(user)[0]


def can(user, ability):
    """True if the user may do this (see ROLE_ABILITIES). A teacher has no office abilities."""
    return ability in ROLE_ABILITIES.get(office_role(user), set())


def is_admin(user):
    """The director: may do everything."""
    return office_role(user) == "admin"


def is_office(user):
    """Administrator, manager or accountant: sees all school data."""
    return can(user, "office")


def get_teacher(user):
    """The Teacher record of this user (None if the user is not a teacher)."""
    return _role(user)[1]


# ───────────────────────── Decorators for views ─────────────────────────

def staff_page(view):
    """A page for every role: office workers AND teachers.
    A logged-in user without any role gets the page "403 Access denied"."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not (office_role(request.user) or get_teacher(request.user)):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def role_page(ability):
    """A page only for roles with this ability, for example @role_page("finance")."""
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not can(request.user, ability):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def admin_page(view):
    """A page only for the director (the same as @role_page with the "access" ability)."""
    return role_page("access")(view)


# ───────────────────────── What a user can see ─────────────────────────

def groups_for(user):
    """Groups: all for office workers, only his own groups for a teacher."""
    if is_office(user):
        return StudyGroup.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return StudyGroup.objects.none()
    return StudyGroup.objects.filter(teacher=teacher)


def lessons_for(user):
    """Lessons: a teacher sees lessons of his groups AND lessons where he replaced a colleague."""
    if is_office(user):
        return Lesson.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return Lesson.objects.none()
    return Lesson.objects.filter(Q(group__teacher=teacher) | Q(teacher=teacher))


def students_for(user):
    """Students: a teacher sees students who study (or studied) in his groups."""
    if is_office(user):
        return Student.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return Student.objects.none()
    return Student.objects.filter(contracts__group__teacher=teacher).distinct()


def subjects_for(user):
    """Subjects: a teacher sees subjects of his groups (for tests)."""
    if is_office(user):
        return Subject.objects.all()
    return Subject.objects.filter(groups__in=groups_for(user)).distinct()


# ───────────────────────── Creating roles and logins ─────────────────────────
# Used by the page "Доступ" (staff_accounts), the command "setup_roles" and seed_data.

# What a teacher may do in the admin: only tests and their results
TEACHER_PERMISSIONS = [
    "view_test", "add_test", "change_test",
    "view_testresult", "add_testresult", "change_testresult", "delete_testresult",
]

UKRAINIAN_LETTERS = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh",
    "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "’": "",
}
# At the start of a word some letters are written differently: Юлія -> Yuliia
FIRST_LETTERS = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}


def transliterate(text):
    """Ukrainian -> Latin letters for a login: "Гриценко" -> "hrytsenko"."""
    text = text.strip().lower()
    result = []
    for i, letter in enumerate(text):
        if i == 0 and letter in FIRST_LETTERS:
            result.append(FIRST_LETTERS[letter])
        else:
            result.append(UKRAINIAN_LETTERS.get(letter, letter if letter.isascii() and letter.isalnum() else ""))
    return "".join(result)


# What the manager and the accountant may do in the admin (Django permissions).
# The website itself checks ROLE_ABILITIES; the admin checks these permissions.
def _perms(actions, models):
    return [f"{action}_{model}" for model in models for action in actions]


MANAGER_PERMISSIONS = (
    # Groups and their schedule are edited in the admin ("Редагувати групу і розклад")
    _perms(["view", "add", "change"], ["studygroup", "groupschedule", "lesson", "attendance", "student", "contract", "contractpause"])
    + _perms(["view", "add", "change", "delete"], ["test", "testresult"])
    + _perms(["view"], ["subject", "teacher", "tariff", "invoice", "payment", "debtcall"])
    + _perms(["add"], ["payment", "debtcall"])
)
ACCOUNTANT_PERMISSIONS = (
    # Money: refunds are paid and expenses / salaries are entered in the admin
    _perms(["view", "add", "change"], ["refund", "expense", "salary"])
    + _perms(["view"], ["invoice", "payment", "contract", "student", "studygroup", "tariff", "teacher", "debtcall"])
    + _perms(["add"], ["payment", "debtcall"])
)


def setup_role_groups():
    """Create (or update) the four role groups with their admin permissions.
    It is safe to run many times. Returns {group name: group}."""
    from django.contrib.auth.models import Group, Permission

    school = Permission.objects.filter(content_type__app_label="school")
    groups = {}
    for name, perms in [
        # The administrator can do everything with school data and can create logins
        (ADMIN_GROUP, school | Permission.objects.filter(content_type__app_label="auth",
                                                          codename__in=["view_user", "add_user", "change_user"])),
        (MANAGER_GROUP, school.filter(codename__in=MANAGER_PERMISSIONS)),
        (ACCOUNTANT_GROUP, school.filter(codename__in=ACCOUNTANT_PERMISSIONS)),
        (TEACHER_GROUP, school.filter(codename__in=TEACHER_PERMISSIONS)),
    ]:
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set(perms)
        groups[name] = group
    return groups


class LoginError(Exception):
    """A clear error for the page "Доступ" (for example, the login is taken)."""


def suggest_username(teacher):
    """A login suggestion: surname in Latin letters ("kovalenko").
    If another teacher already uses it, we add the first letter of the name ("kovalenko.i")."""
    base = transliterate(teacher.last_name) or f"teacher{teacher.pk}"
    if Teacher.objects.filter(user__username=base).exclude(pk=teacher.pk).exists():
        return f"{base}.{transliterate(teacher.first_name)[:1]}"
    return base


def check_username(username):
    """Only Latin letters, digits and @ . + - _ (a Cyrillic login is hard to type on some phones)."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9@.+_-]{3,150}", username or ""):
        raise LoginError("Логін: від 3 символів, лише латинські літери, цифри та символи @ . + - _")


def check_password(password, user=None):
    """Django rules for passwords (length, not too simple, not only digits). Raises LoginError."""
    from django.contrib.auth.password_validation import validate_password
    from django.core.exceptions import ValidationError

    try:
        validate_password(password, user=user)
    except ValidationError as error:
        raise LoginError(" ".join(error.messages))


def create_teacher_login(teacher, password, username=None, check=False):
    """Create a login for a teacher (if he has none) and link it to the Teacher record.
    username: empty -> suggest_username(). check=True: check the password by Django rules.
    A free old login (its teacher was deleted, for example by seed_data --clear) is used again.
    Returns (user, created)."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    User = get_user_model()
    if teacher.user_id:
        return teacher.user, False
    setup_role_groups()  # the groups must exist; it is safe to run many times

    username = (username or suggest_username(teacher)).strip()
    check_username(username)
    existing = User.objects.filter(username=username).first()
    if existing and (Teacher.objects.filter(user=existing).exists() or existing.is_superuser):
        raise LoginError(f"Логін «{username}» уже зайнятий.")

    user = existing or User(username=username, first_name=teacher.first_name, last_name=teacher.last_name)
    if check:
        check_password(password, user)
    created = existing is None
    if created:
        user.set_password(password)
    # is_staff = True: the teacher can open the admin, but he sees only tests (his permissions)
    user.is_staff = True
    user.is_active = True
    user.save()
    user.groups.add(Group.objects.get(name=TEACHER_GROUP))
    teacher.user = user
    teacher.save(update_fields=["user"])
    return user, created


def create_office_login(username, password, group_name, first_name=""):
    """Create a login for an office worker: administrator, manager or accountant (page "Доступ")."""
    from django.contrib.auth import get_user_model

    if group_name not in OFFICE_ROLES:
        raise LoginError("Оберіть роль: адміністратор, менеджер або бухгалтер.")
    User = get_user_model()
    groups = setup_role_groups()
    username = username.strip()
    check_username(username)
    if User.objects.filter(username=username).exists():
        raise LoginError(f"Логін «{username}» уже зайнятий.")
    # is_staff = True: the admin opens, and Django permissions of the group decide what is there
    user = User(username=username, first_name=first_name.strip(), is_staff=True)
    check_password(password, user)
    user.set_password(password)
    user.save()
    user.groups.add(groups[group_name])
    return user


def create_admin_login(username, password, first_name=""):
    """Create a new administrator login (kept for the command "setup_roles")."""
    return create_office_login(username, password, ADMIN_GROUP, first_name)
