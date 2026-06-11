from picamera2 import Picamera2

picam2 = Picamera2()

for i, mode in enumerate(picam2.sensor_modes):
    print(f"\nmode {i}")
    for k, v in mode.items():
        print(f"  {k}: {v}")