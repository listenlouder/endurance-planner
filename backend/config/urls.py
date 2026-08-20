from django.http import HttpResponse
from django.urls import path, include

handler403 = 'events.views.permission_denied_view'
handler404 = 'events.views.not_found_view'
handler500 = 'events.views.server_error_view'


def healthz(request):
    """
    Railway's healthcheck target. Deliberately touches neither the database
    nor a template: pointed at '/' it rendered the full homepage on every
    probe, and both logging middlewares skip this path so continuous probe
    traffic cannot drown the log stream or the activity table.
    """
    return HttpResponse('ok', content_type='text/plain')


urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('accounts/', include('allauth.urls')),
    path('', include('events.urls')),
]
