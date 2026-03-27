from django.urls import path
from . import views

# Routes will be added in subsequent phases
urlpatterns = [
    path('', views.index, name='index'),
]
