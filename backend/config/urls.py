from django.contrib import admin
from django.urls import path, include

handler403 = 'events.views.permission_denied_view'
handler404 = 'events.views.not_found_view'
handler500 = 'events.views.server_error_view'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('allauth.urls')),
    path('', include('events.urls')),
]
