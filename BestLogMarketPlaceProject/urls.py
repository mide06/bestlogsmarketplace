"""
URL configuration for BestLogMarketPlaceProject project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include 
from django.conf import settings
from django.conf.urls.static import static
from BestLogMarketPlaceApp import views as app_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include("BestLogMarketPlaceApp.urls")),
    path('temporary-data-import/upload/', app_views.temporary_data_import_upload, name='temporary_data_import_upload'),
    path('temporary-data-import/run/', app_views.temporary_data_import_run, name='temporary_data_import_run'),
]

urlpatterns += static(settings.MEDIA_URL,document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL,document_root=settings.STATIC_ROOT)