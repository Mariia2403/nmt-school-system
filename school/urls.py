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
    path("debts/call-list/", views.call_list, name="call_list"),
]