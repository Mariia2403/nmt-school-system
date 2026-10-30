"""
Variables for ALL templates (registered in settings.py -> TEMPLATES -> context_processors).
Thanks to this, base.html knows whether to show the menu items "Фінанси" and "До дзвінка".
"""
from .access import get_teacher, is_admin


def roles(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"is_school_admin": False, "current_teacher": None}
    return {"is_school_admin": is_admin(user), "current_teacher": get_teacher(user)}