from django.urls import path, include
from rest_framework import routers
from .views import PowerSystemViewSet, SensorDataViewSet, monitoring_dashboard, reset_count, PowerCommandView, latest_data, activate_override

router = routers.DefaultRouter()
router.register(r'powersystem', PowerSystemViewSet)
router.register(r'sensordata', SensorDataViewSet)

urlpatterns = [
    path('', monitoring_dashboard, name='dashboard'),  
    path('api/', include(router.urls)),                 
    path('api/resetcount/', reset_count, name='reset_count'),
    path('api/power-command/', PowerCommandView.as_view(), name='power_command'),
    path('api/latest-data/', latest_data, name='latest_data'),
    path('api/activate-override/', activate_override, name='activate_override'),  # Endpoint baru untuk override
]