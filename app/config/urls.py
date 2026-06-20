from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("dashboard.urls")),
    path("family/", include("family_core.urls")),
    path("portfolio/", include("portfolio.urls")),
    path("ledger/", include("ledger.urls")),
    path("ipo/", include("ipo.urls")),
    path("notes/", include("notes.urls")),
    path("macro/", include("macro.urls")),
    path("ai/", include("ai_analysis.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
