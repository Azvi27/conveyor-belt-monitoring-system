from rest_framework import viewsets, status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.shortcuts import render
from django.http import JsonResponse
import requests
import logging
from datetime import datetime, timedelta
from threading import Timer
from django.db.models import Count, Case, When, IntegerField
from .models import PowerSystem, SensorData
from .serializers import PowerSystemSerializer, SensorDataSerializer, PowerCommandSerializer, ProductCountSerializer

# URL dan timeout Raspberry Pi
RASPBERRY_PI_URL = "http://192.168.148.187:8000"
POWER_CONTROL_URL = f"{RASPBERRY_PI_URL}/control-power"
RESET_COUNTER_URL = f"{RASPBERRY_PI_URL}/reset-counter"
TIMEOUT = 5

# === Override Mode Setup ===
override_mode = False
override_timestamp = None
# Track timer thread
auto_resume_timer = None

# Timestamp reset terakhir untuk perhitungan counter
last_reset_timestamp = None

# Viewsets untuk administrasi - tidak diubah
class PowerSystemViewSet(viewsets.ModelViewSet):
    queryset = PowerSystem.objects.all()
    serializer_class = PowerSystemSerializer

class SensorDataViewSet(viewsets.ModelViewSet):
    queryset = SensorData.objects.all()
    serializer_class = SensorDataSerializer

# Dashboard - tidak diubah
def monitoring_dashboard(request):
    context = {
        "sensor": SensorData.objects.last(),
        "power": PowerSystem.objects.last(),
    }
    return render(request, 'monitoring/dashboard.html', context)

# Fungsi untuk auto resume override
def auto_resume_override():
    global override_mode, override_timestamp
    
    print("[OVERRIDE] Timer expired, deactivating override mode.")
    override_mode = False
    override_timestamp = None
    
    # Kirim sinyal ke Raspberry Pi untuk resume operasi
    try:
        response = requests.post(
            POWER_CONTROL_URL,
            json={"status": 1},  # Resume
            timeout=TIMEOUT
        )
        if response.status_code == 200:
            print("[RASPI] Resume signal sent successfully.")
            
            # Buat data PowerSystem baru dengan status aktif
            PowerSystem.objects.create(
                timestamp=timezone.now(),
                status=True,
                voltage=0,
                vibration=True,
                current=0,
                power_consumption=0
            )
            print("[DB] Created new PowerSystem record with status=True")
        else:
            print(f"[RASPI] Resume failed, status {response.status_code}")
    except Exception as e:
        print(f"[RASPI] Resume error: {e}")

# Fungsi untuk menghitung jumlah produk baik/buruk
def get_product_counts():
    global last_reset_timestamp
    
    # Query dasar untuk SensorData
    query = SensorData.objects
    
    # Jika ada reset timestamp, hanya hitung data setelah timestamp tersebut
    if last_reset_timestamp:
        query = query.filter(timestamp__gte=last_reset_timestamp)
    
    # Hitung produk baik dan buruk
    product_counts = query.aggregate(
        good_product=Count(Case(
            When(status_product=True, then=1),
            output_field=IntegerField()
        )),
        bad_product=Count(Case(
            When(status_product=False, then=1),
            output_field=IntegerField()
        ))
    )
    
    return product_counts

# API untuk data sensor dan power
@api_view(['POST'])
def create_data(request, data_type):
    if data_type == 'sensor':
        # Ekstrak dan persiapkan data untuk SensorData
        sensor_data = {
            'timestamp': request.data.get('timestamp', timezone.now()),
            'mass': request.data.get('mass', 0),
            'brightness': request.data.get('brightness', 0),
            'status_product': request.data.get('status_product', True)
        }
        
        # Konversi status_product menjadi boolean jika dikirim sebagai 0/1
        if 'status_product' in sensor_data and not isinstance(sensor_data['status_product'], bool):
            sensor_data['status_product'] = bool(int(sensor_data['status_product']))
        
        sensor_serializer = SensorDataSerializer(data=sensor_data)
        
        # Persiapkan data untuk PowerSystem
        power_data = {
            'timestamp': request.data.get('timestamp', timezone.now()),
            'status': request.data.get('status', True),
            'voltage': request.data.get('voltage', 0),
            'current': request.data.get('current', 0),
            'power_consumption': request.data.get('power_consumption', 0),
        }
        
        # Handle vibration status
        if 'vibration_level' in request.data:
            vibration_level = float(request.data.get('vibration_level', 0))
            power_data['vibration'] = vibration_level < 5  # True jika aman, False jika tidak
        
        power_serializer = PowerSystemSerializer(data=power_data)
        
        # Validasi dan simpan data
        if sensor_serializer.is_valid() and power_serializer.is_valid():
            sensor_serializer.save()
            power_serializer.save()
            
            # Dapatkan jumlah produk setelah penyimpanan
            product_counts = get_product_counts()
            
            return Response({
                "message": "Sensor data saved successfully",
                "sensor": sensor_serializer.data,
                "power": power_serializer.data,
                "product_counts": product_counts
            }, status=status.HTTP_201_CREATED)
        
        # Jika terjadi error, berikan detail error
        errors = {}
        if not sensor_serializer.is_valid():
            errors['sensor'] = sensor_serializer.errors
        if not power_serializer.is_valid():
            errors['power'] = power_serializer.errors
        
        return Response({"errors": errors}, status=status.HTTP_400_BAD_REQUEST)
    
    else:  # power
        # Langsung gunakan PowerSystemSerializer
        serializer = PowerSystemSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response({"message": "Power system status saved successfully", "data": serializer.data}, 
                           status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# API untuk mengirim perintah ke Raspberry Pi - dimodifikasi untuk menangani override
class PowerCommandView(APIView):
    def post(self, request):
        global override_mode, override_timestamp
        
        serializer = PowerCommandSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        status_value = serializer.validated_data['status']
        
        # Jika mencoba mengaktifkan sistem (status=1) saat override aktif, tolak
        if status_value == 1 and override_mode:
            return Response(
                {"error": "Cannot activate system during override mode. Wait for override mode to expire."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            # Kirim perintah ke Raspberry Pi
            response = requests.post(
                POWER_CONTROL_URL,
                json={"status": status_value},
                timeout=TIMEOUT
            )
            
            # Jika berhasil, simpan status
            if response.status_code == 200:
                # Dapatkan atau atur nilai voltage, current dan power_consumption
                voltage = request.data.get('voltage', 0)
                current = request.data.get('current', 0)
                power_consumption = request.data.get('power_consumption', 0)
                vibration = request.data.get('vibration', True)
                
                PowerSystem.objects.create(
                    timestamp=timezone.now(),
                    status=bool(status_value),
                    voltage=voltage,
                    vibration=vibration,
                    current=current,
                    power_consumption=power_consumption
                )
                return Response({"message": "Power command sent successfully"}, 
                               status=status.HTTP_200_OK)
            else:
                return Response(
                    {"error": f"Failed to send command: {response.text}"}, 
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            return Response(
                {"error": f"Communication error: {str(e)}"}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

# API untuk mendapatkan data terbaru
def latest_data(request):
    # Log untuk debugging
    print("[DEBUG] latest_data() called")
    print(f"[DEBUG] Current override status: mode={override_mode}, timestamp={override_timestamp}")
    
    try:
        sensor = SensorData.objects.latest('timestamp')
        power = PowerSystem.objects.latest('timestamp')
        
        # Hitung jumlah produk baik dan buruk
        product_counts = get_product_counts()
        
        data = {
            "sensor": {
                "id": sensor.id,
                "mass": sensor.mass,
                "brightness": sensor.brightness,
                "status_product": sensor.status_product,
                "timestamp": sensor.timestamp.isoformat(),
            },
            "power": {
                "id": power.id,
                "status": power.status,
                "voltage": power.voltage,
                "vibration": power.vibration,
                "current": power.current,
                "power_consumption": power.power_consumption,
                "timestamp": power.timestamp.isoformat(),
                "override_active": override_mode  # Tambahkan flag override
            },
            "product_counts": {
                "good_product": product_counts['good_product'],
                "bad_product": product_counts['bad_product']
            }
        }
        return JsonResponse(data)
    except Exception as e:
        print(f"[ERROR] latest_data error: {e}")
        return JsonResponse({"error": str(e)}, status=500)

# API untuk reset counter
@api_view(['POST'])
def reset_count(request):
    global last_reset_timestamp
    
    try:
        latest = SensorData.objects.last()
        if not latest:
            return Response({"error": "No sensor data available"}, 
                           status=status.HTTP_404_NOT_FOUND)
        
        # Set timestamp reset untuk filter produk
        last_reset_timestamp = timezone.now()
        
        # Buat instance baru dengan timestamp reset
        SensorData.objects.create(
            timestamp=last_reset_timestamp,
            mass=latest.mass,
            brightness=latest.brightness,
            status_product=True  # Default baik
        )
        
        # Tambahan kode untuk reset counter di Raspberry Pi
        try:
            # Kirim perintah reset ke Raspberry Pi
            pi_response = requests.post(
                RESET_COUNTER_URL,
                json={"reset": True},
                timeout=TIMEOUT
            )
            
            if pi_response.status_code != 200:
                return Response(
                    {"warning": "Counters reset in database but failed to reset on Raspberry Pi"}, 
                    status=status.HTTP_200_OK
                )
                
        except Exception as e:
            return Response(
                {"warning": f"Counters reset in database but failed to communicate with Raspberry Pi: {str(e)}"}, 
                status=status.HTTP_200_OK
            )
        
        # Get product counts setelah reset (seharusnya 1 good, 0 bad)
        product_counts = get_product_counts()
        
        return Response({
            "message": "Counters reset successfully on database and Raspberry Pi",
            "product_counts": product_counts
        }, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"error": str(e)}, 
                       status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# API baru untuk mengaktifkan mode override
@api_view(['POST'])
def activate_override(request):
    global override_mode, override_timestamp, auto_resume_timer
    
    # Periksa data yang diperlukan
    required_fields = ['voltage', 'current']
    if not all(field in request.data for field in required_fields):
        return Response(
            {"error": f"Missing required fields: {', '.join(required_fields)}"}, 
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Ekstrak data yang dikirim
    voltage = float(request.data.get('voltage', 0))
    current = float(request.data.get('current', 0))
    
    # Batalkan timer yang masih berjalan (jika ada)
    if auto_resume_timer and auto_resume_timer.is_alive():
        auto_resume_timer.cancel()
    
    # Aktifkan mode override
    override_mode = True
    override_timestamp = datetime.now()
    print(f"[OVERRIDE] Activated at {override_timestamp}")
    
    # Jadwalkan auto resume dengan timer thread
    auto_resume_timer = Timer(10.0, auto_resume_override)
    auto_resume_timer.daemon = True
    auto_resume_timer.start()
    print("[OVERRIDE] Auto-resume scheduled in 10 seconds")
    
    # Kirim perintah ke Raspberry Pi untuk mematikan
    try:
        response = requests.post(
            POWER_CONTROL_URL,
            json={"status": 0},  # Matikan
            timeout=TIMEOUT
        )
        
        if response.status_code != 200:
            print(f"[RASPI] Warning: Failed to stop, status {response.status_code}")
            return Response(
                {"warning": "Override mode activated but failed to stop Raspberry Pi"}, 
                status=status.HTTP_200_OK
            )
        
        print("[RASPI] Stop command sent successfully")
            
    except Exception as e:
        print(f"[RASPI] Error sending stop command: {e}")
        return Response(
            {"warning": f"Override mode activated but failed to communicate with Raspberry Pi: {str(e)}"}, 
            status=status.HTTP_200_OK
        )
    
    # Simpan status mesin ke database
    try:
        power_data = PowerSystem.objects.create(
            timestamp=timezone.now(),
            status=False,  # Override selalu mematikan mesin
            voltage=voltage,
            vibration=True,  # Dianggap aman
            current=current,
            power_consumption=0
        )
        print(f"[DB] Created new PowerSystem record id={power_data.id} with status=False")
    except Exception as e:
        print(f"[DB] Error creating PowerSystem record: {e}")
        return Response(
            {"warning": f"Override mode activated but failed to save to database: {str(e)}"}, 
            status=status.HTTP_200_OK
        )
    
    return Response({
        "message": "Override mode activated successfully (will auto-deactivate after 10 seconds)",
        "data": PowerSystemSerializer(power_data).data,
        "debug_info": {
            "override_mode": override_mode,
            "override_timestamp": override_timestamp.isoformat() if override_timestamp else None,
            "timer_active": auto_resume_timer.is_alive() if auto_resume_timer else False
        }
    }, status=status.HTTP_200_OK)