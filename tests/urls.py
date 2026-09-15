from django.contrib import admin
from django.urls import path

from tests import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("boom/", views.boom),
    path("nested/", views.nested),
    path("notfound/", views.notfound),
    path("denied/", views.denied),
    path("log-and-raise/", views.log_and_raise),
    path("log-only/", views.log_only),
    path("sensitive-post/", views.sensitive_post),
    path("sensitive-vars/", views.sensitive_vars),
    path("unrepresentable/", views.unrepresentable),
    path("broken-user/", views.broken_user),
    path("aboom/", views.aboom),
]
