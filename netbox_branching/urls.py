from django.urls import include, path

from utilities.urls import get_model_urls
from . import views

urlpatterns = [
    # Branches
    path('branches/', views.BranchListView.as_view(), name='branch_list'),
    path('branches/add/', views.BranchEditView.as_view(), name='branch_add'),
    path('branches/import/', views.BranchBulkImportView.as_view(), name='branch_bulk_import'),
    path('branches/edit/', views.BranchBulkEditView.as_view(), name='branch_bulk_edit'),
    path('branches/delete/', views.BranchBulkDeleteView.as_view(), name='branch_bulk_delete'),
    path('branches/<int:pk>/', include(get_model_urls('netbox_branching', 'branch'))),

    # Change diffs
    path('changes/', views.ChangeDiffListView.as_view(), name='changediff_list'),
]
