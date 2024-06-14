from django.urls import include, path

from utilities.urls import get_model_urls
from . import views

urlpatterns = [
    # Branches
    path('branchs/', views.BranchListView.as_view(), name='branch_list'),
    path('branchs/add/', views.BranchEditView.as_view(), name='branch_add'),
    path('branchs/import/', views.BranchBulkImportView.as_view(), name='branch_import'),
    path('branchs/edit/', views.BranchBulkEditView.as_view(), name='branch_bulk_edit'),
    path('branchs/delete/', views.BranchBulkDeleteView.as_view(), name='branch_bulk_delete'),
    path('branchs/<int:pk>/', include(get_model_urls('netbox_vcs', 'branch'))),

    # Change diffs
    path('changes/', views.ChangeDiffListView.as_view(), name='changediff_list'),
]
