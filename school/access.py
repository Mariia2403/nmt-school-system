"""
Roles and access rules of the school website.

There are two roles (Django groups):
- "Адміністратор": sees everything (students, groups, journal, finance, debts).
- "Викладач": sees only HIS groups, their students, the journal of his lessons
  and enters test results. He does not see money: no finance, no amounts of debts.

A superuser is always an administrator.
A teacher is a user who is linked to a Teacher record (Teacher.user).

All pages take data ONLY through the functions below (groups_for, lessons_for, students_for).
So a teacher cannot open a strange group even if he types its address by hand.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import Lesson, Student, StudyGroup, Subject, Teacher

ADMIN_GROUP = "Адміністратор"
TEACHER_GROUP = "Викладач"


def _role(user):
    """Calculate the role once per request and keep it on the user object.
    Returns (is_admin, teacher or None)."""
    if not user.is_authenticated:
        return False, None
    if not hasattr(user, "_school_role"):
        admin = user.is_superuser or user.groups.filter(name=ADMIN_GROUP).exists()
        teacher = Teacher.objects.filter(user=user, is_active=True).first()
        user._school_role = (admin, teacher)
    return user._school_role


def is_admin(user):
    return _role(user)[0]


def get_teacher(user):
    """The Teacher record of this user (None if the user is not a teacher)."""
    return _role(user)[1]


# ───────────────────────── Decorators for views ─────────────────────────

def staff_page(view):
    """A page for the administrator AND for teachers.
    A logged-in user without any role gets the page "403 Access denied"."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not (is_admin(request.user) or get_teacher(request.user)):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def admin_page(view):
    """A page only for the administrator (money, debts)."""
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


# ───────────────────────── What a user can see ─────────────────────────

def groups_for(user):
    """Groups: all for the administrator, only his own groups for a teacher."""
    if is_admin(user):
        return StudyGroup.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return StudyGroup.objects.none()
    return StudyGroup.objects.filter(teacher=teacher)


def lessons_for(user):
    """Lessons: a teacher sees lessons of his groups AND lessons where he replaced a colleague."""
    if is_admin(user):
        return Lesson.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return Lesson.objects.none()
    return Lesson.objects.filter(Q(group__teacher=teacher) | Q(teacher=teacher))


def students_for(user):
    """Students: a teacher sees students who study (or studied) in his groups."""
    if is_admin(user):
        return Student.objects.all()
    teacher = get_teacher(user)
    if teacher is None:
        return Student.objects.none()
    return Student.objects.filter(contracts__group__teacher=teacher).distinct()


def subjects_for(user):
    """Subjects: a teacher sees subjects of his groups (for tests)."""
    if is_admin(user):
        return Subject.objects.all()
    return Subject.objects.filter(groups__in=groups_for(user)).distinct()


# ───────────────────────── Creating roles and logins ─────────────────────────
# Used by the command "python manage.py setup_roles" and by seed_data.

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


def setup_role_groups():
    """Create (or update) the groups "Адміністратор" and "Викладач" with their permissions.
    It is safe to run many times."""
    from django.contrib.auth.models import Group, Permission

    admin_group, _ = Group.objects.get_or_create(name=ADMIN_GROUP)
    # The administrator can do everything with school data and can create logins for teachers
    admin_group.permissions.set(
        Permission.objects.filter(content_type__app_label="school")
        | Permission.objects.filter(content_type__app_label="auth", codename__in=["view_user", "add_user", "change_user"])
    )
    teacher_group, _ = Group.objects.get_or_create(name=TEACHER_GROUP)
    teacher_group.permissions.set(
        Permission.objects.filter(content_type__app_label="school", codename__in=TEACHER_PERMISSIONS)
    )
    return admin_group, teacher_group


def create_teacher_login(teacher, password):
    """Create a login for a teacher (if he has none) and link it to the Teacher record.
    Login = surname in Latin letters, for example "kovalenko". Returns (user, created)."""
    from django.contrib.auth import get_user_model
    from django.contrib.auth.models import Group

    User = get_user_model()
    if teacher.user_id:
        return teacher.user, False
    base = transliterate(teacher.last_name) or f"teacher{teacher.pk}"
    username = base
    # The login is taken by ANOTHER teacher (two teachers with one surname) -> add the first letter of the name.
    # A free old login (its teacher was deleted) is used again.
    if Teacher.objects.filter(user__username=username).exclude(pk=teacher.pk).exists():
        username = f"{base}.{transliterate(teacher.first_name)[:1]}"
    user, created = User.objects.get_or_create(
        username=username,
        defaults={"first_name": teacher.first_name, "last_name": teacher.last_name},
    )
    if created:
        user.set_password(password)
    # is_staff = True: the teacher can open the admin, but he sees only tests (his permissions)
    user.is_staff = True
    user.save()
    user.groups.add(Group.objects.get(name=TEACHER_GROUP))
    teacher.user = user
    teacher.save(update_fields=["user"])
    return user, created