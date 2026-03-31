from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('create/', views.event_create, name='event_create'),
    path('lookup/', views.event_lookup_by_id, name='event_lookup_by_id'),
    path('<uuid:event_id>/view/', views.view_event, name='view_event'),
    path('<uuid:event_id>/signup/', views.signup, name='signup'),
    path('<uuid:event_id>/signup/<uuid:driver_id>/edit/', views.signup_edit, name='signup_edit'),
    path('<uuid:event_id>/signup/<uuid:driver_id>/success/', views.signup_success, name='signup_success'),
    path('<uuid:event_id>/signup/<uuid:driver_id>/delete/', views.driver_delete, name='driver_delete'),
    # Phase 4: admin sub-routes must come BEFORE the <str:admin_key> entry point,
    # otherwise Django matches the literal segments (e.g. "edit-field") as the key.
    path('set-timezone/', views.set_timezone, name='set_timezone'),
    path('<uuid:event_id>/admin/update-field/', views.admin_edit_field, name='admin_update_field'),
    path('<uuid:event_id>/admin/edit-field/', views.admin_edit_field, name='admin_edit_field'),
    path('<uuid:event_id>/admin/remove-driver/<uuid:driver_id>/', views.admin_remove_driver, name='admin_remove_driver'),
    path('<uuid:event_id>/admin/create-stints/', views.create_stints, name='create_stints'),
    path('<uuid:event_id>/admin/<str:admin_key>/', views.admin_page, name='admin_page'),
]
