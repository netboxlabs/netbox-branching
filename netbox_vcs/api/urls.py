from netbox.api.routers import NetBoxRouter
from .views import ContextViewSet

router = NetBoxRouter()
router.register('context', ContextViewSet)
urlpatterns = router.urls