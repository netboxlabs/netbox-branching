from netbox.api.routers import NetBoxRouter
from . import views

router = NetBoxRouter()
router.APIRootView = views.RootView
router.register('branches', views.BranchViewSet)
router.register('changes', views.ChangeDiffViewSet)

urlpatterns = router.urls