"""
URLs of the school application.
app_name gives a "namespace": in templates we write {% url 'school:dashboard' %}.
"""
from django.urls import path

from . import views

app_name = "school"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]