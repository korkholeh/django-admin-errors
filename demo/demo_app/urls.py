from django.urls import path

from demo_app import views

urlpatterns = [
    path("", views.index, name="index"),
    path("boom/", views.boom, name="boom"),
    path("boom/<int:n>/", views.boom_n, name="boom_n"),
    path("keyerror/<slug:key>/", views.keyerror, name="keyerror"),
    path("nested/", views.nested, name="nested"),
    path("logged/", views.logged, name="logged"),
    path("warning/", views.warning, name="warning"),
    path("sensitive/", views.sensitive, name="sensitive"),
    path("storm/", views.storm, name="storm"),
    path("unique-storm/", views.unique_storm, name="unique_storm"),
    path("task/", views.task, name="task"),
    path("async-boom/", views.async_boom, name="async_boom"),
    path("404/", views.not_found, name="not_found"),
]
