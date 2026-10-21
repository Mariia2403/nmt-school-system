"""
Main URL settings of the project.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Ready login/logout views from Django. If a user is already logged in,
    # the login page sends them straight to the dashboard.
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # All pages of our school application
    path("", include("school.urls")),
]