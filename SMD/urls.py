"""
URL configuration for SMD project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a path() to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a path() to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a path() to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from VisionInspection import ui_views

urlpatterns = [
    path("favicon.ico", ui_views.favicon, name="favicon"),
    path("vision-inspection/", include("VisionInspection.urls")),
    # Jazzmin ("jazzmin" listed before admin in INSTALLED_APPS) transparently
    # themes the default admin UI; keep the standard admin site URLs.
    path("admin/", admin.site.urls),
]
