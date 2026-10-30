"""
URLs of the school application.
app_name gives a "namespace": in templates we write {% url 'school:dashboard' %}.
"""
from django.urls import path

from . import views

app_name = "school"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("students/", views.student_list, name="student_list"),
    # <int:pk> takes the number from the address, for example /students/5/ -> pk=5
    path("students/<int:pk>/", views.student_detail, name="student_detail"),
    # Students with an overdue debt: the administrator calls their parents
    path("groups/", views.group_list, name="group_list"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    # Journal: lessons of one day (?date=2026-10-30) and the attendance form of one lesson
    path("journal/", views.journal, name="journal"),
    path("journal/lesson/<int:pk>/", views.journal_lesson, name="journal_lesson"),
    path("debts/call-list/", views.call_list, name="call_list"),
    # Money of the school. The month is in the address: /finance/?month=2026-10
    path("finance/", views.finance, name="finance"),
    # Logins of teachers and administrators (only for the administrator)
    path("access/", views.staff_accounts, name="staff_accounts"),
]