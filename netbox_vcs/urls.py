from django.urls import include, path

from utilities.urls import get_model_urls
from . import views

urlpatterns = [
    # Contexts
    path('contexts/', views.ContextListView.as_view(), name='context_list'),
    path('contexts/add/', views.ContextEditView.as_view(), name='context_add'),
    path('contexts/import/', views.ContextBulkImportView.as_view(), name='context_import'),
    path('contexts/edit/', views.ContextBulkEditView.as_view(), name='context_bulk_edit'),
    path('contexts/delete/', views.ContextBulkDeleteView.as_view(), name='context_bulk_delete'),
    path('contexts/<int:pk>/', include(get_model_urls('netbox_vcs', 'context'))),

    # Change diffs
    path('changes/', views.ChangeDiffListView.as_view(), name='changediff_list'),
]
