import zwoasi

class Camera:
    self.camera = None
    self.connected = False


    def connect(self):
        dll_path = os.path.join(os.path.dirname(__file__), 'ZWO_Trigger', 'ZWO_ASI_LIB', 'lib', 'x64', 'ASICamera2.dll')
        if not os.path.exists(dll_path):
            print(f"dll not found at: {dll_path}")
        else:
            zwoasi.init(dll_path)
            print("Dll loaded successfully")
            num_cameras = zwoasi.get_num_cameras()
            if num_cameras == 0:
                print("No ZWO cameras detected")
            else:
                print(f"Detected {num_cameras} camera(s)")

        self.camera = zwoasi.Camera(0)
        self.connected = True
        print("Camera Connected")
        # Setting default camera settings

        self.camera.set_control_value(zwoasi.ASI_BANDWIDTHOVERLOAD, self.camera.get_controls()['BandWidth']['MinValue'])
        self.camera.disable_dark_subtract()
        self.camera.set_control_value(zwoasi.ASI_GAIN, 150)
        self.camera.set_control_value(zwoasi.ASI_EXPOSURE, 30000)
        self.camera.set_control_value(zwoasi.ASI_WB_B, 99)
        self.camera.set_control_value(zwoasi.ASI_WB_R, 75)
        self.camera.set_control_value(zwoasi.ASI_GAMMA, 50)
        self.camera.set_control_value(zwoasi.ASI_BRIGHTNESS, 50)
        self.camera.set_control_value(zwoasi.ASI_FLIP, 0)

    def _save_control_values(self, filename, settings):
        try:
            settings_filename = filename + '.txt'
            with open(settings_filename, 'w') as f:
                for k in sorted(settings.keys()):
                    f.write('%s: %s\n' % (k, str(settings[k])))
            print('Camera settings saved to %s' % settings_filename)
        except Exception as e:
            print(f'Error saving camera settings: {e}')            
    
    def capture_photo(self, filename, gain, exposure):
        self.camera.stop_video_capture()
        self.camera.stop_exposure()
        self.camera.set_image_type(zwoasi.ASI_IMG_RGB24)
        self.camera.set_control_value(zwoasi.ASI_GAIN, gain)
        self.camera.set_control_value(zwoasi.ASI_EXPOSURE, exposure)
        self.camera.capture(filename=filename)
        self._save_control_values(filename=filename, settings=self.camera.get_control_values())
        print(f"Captured image and saved to : {filename}")