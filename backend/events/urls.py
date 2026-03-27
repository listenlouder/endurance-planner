from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('create/', views.event_create, name='event_create'),
    path('lookup/', views.event_lookup_by_id, name='event_lookup_by_id'),
    path('<uuid:event_id>/lookup/', views.event_lookup, name='event_lookup'),
]
