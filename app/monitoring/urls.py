from django.urls import path
from . import views, restore_views
app_name='monitoring'
urlpatterns=[path('',views.index,name='index'),path('refresh/',views.refresh,name='refresh'),path('settings/',views.settings_view,name='settings')]
urlpatterns += [path('tasks/', views.tasks, name='tasks')]
urlpatterns += [path('restore-verifications/', restore_views.restore_verifications, name='restore_verifications')]
