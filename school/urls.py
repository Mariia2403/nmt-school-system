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
    # A new student with 1-2 groups in one form
    path("students/new/", views.student_create, name="student_create"),
    # "Записати в групу" for a student who already exists (?student=5 or ?group=3)
    path("enroll/", views.enroll, name="enroll"),
    # <int:pk> takes the number from the address, for example /students/5/ -> pk=5
    path("students/<int:pk>/", views.student_detail, name="student_detail"),
    # The administrator enters money from parents; the system splits it between invoices
    path("students/<int:pk>/payment/", views.student_payment, name="student_payment"),
    # Students with an overdue debt: the administrator calls their parents
    path("groups/", views.group_list, name="group_list"),
    # Buttons of a contract in the student card: pause / resume / leave
    path("contracts/<int:pk>/<str:action>/", views.contract_action, name="contract_action"),
    path("groups/<int:pk>/", views.group_detail, name="group_detail"),
    # Button on the group page: create lessons from the weekly schedule
    path("groups/<int:pk>/sync-lessons/", views.group_sync_lessons, name="group_sync_lessons"),
    # Buttons in the "Керування групою" block: finish the group / delete a group created by mistake
    path("groups/<int:pk>/finish/", views.group_finish, name="group_finish"),
    path("groups/<int:pk>/delete/", views.group_delete, name="group_delete"),
    # Journal: lessons of one day (?date=2026-10-30) and the attendance form of one lesson
    path("journal/", views.journal, name="journal"),
    path("journal/lesson/<int:pk>/", views.journal_lesson, name="journal_lesson"),
    path("debts/call-list/", views.call_list, name="call_list"),
    # Money of the school. The month is in the address: /finance/?month=2026-10
    path("finance/", views.finance, name="finance"),
    # Logins of teachers and administrators (only for the administrator)
    path("access/", views.staff_accounts, name="staff_accounts"),
]