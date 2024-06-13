from netbox.api.routers import NetBoxRouter
from . import views

router = NetBoxRouter()
router.APIRootView = views.VCSRootView
router.register('contexts', views.ContextViewSet)
router.register('changes', views.ChangeDiffViewSet)

urlpatterns = router.urls