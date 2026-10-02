from django.urls import path
from . import views
app_name='monitoring'
urlpatterns=[path('',views.index,name='index'),path('refresh/',views.refresh,name='refresh'),path('settings/',views.settings_view,name='settings')]
