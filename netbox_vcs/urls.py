from django.urls import include, path

from utilities.urls import get_model_urls
from . import views

urlpatterns = [
    path('contexts/', views.ContextListView.as_view(), name='context_list'),
    path('contexts/add/', views.ContextEditView.as_view(), name='context_add'),
    path('contexts/<int:pk>/', include(get_model_urls('netbox_vcs', 'context'))),
]
