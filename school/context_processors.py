"""
Variables for ALL templates (registered in settings.py -> TEMPLATES -> context_processors).
Thanks to this, base.html knows which menu items to show.

can.finance, can.enroll, can.money ... - what the user may do (see access.ROLE_ABILITIES).
is_school_admin - the director (may do everything).
is_office       - administrator, manager or accountant (sees all students and groups).
"""
from .access import ALL_ABILITIES, ROLE_LABELS, can, get_teacher, is_admin, office_role


def roles(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_school_admin": False, "is_office": False, "current_teacher": None,
                "can": {}, "role_label": ""}
    role = office_role(user)
    teacher = get_teacher(user)
    return {
        "is_school_admin": is_admin(user),
        "is_office": role is not None,
        "current_teacher": teacher,
        "can": {ability: can(user, ability) for ability in ALL_ABILITIES},
        # Text for the badge near the name in the menu
        "role_label": ROLE_LABELS.get(role) or ("викладач" if teacher else "без ролі"),
    }
