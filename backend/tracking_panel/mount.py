import win32com.client

class Telescope:
    def __init(self):
        self.mount = None
        self.connected = False
    

    def connect(self):
        if not self.connected:
            try:
                self.mount = win32com.client.Dispatch("ASCOM.ASIMount.Telescope")
                self.mount.Connected = True
                self.connected = True
                print("Mount Connected")
            except Exception as e:
                print(f"Failed to connect to mount: {e}")

    
    def disconnect(self):
        if self.connected:
            self.mount.Connected = False
            self.connected = False
            print("Disconnected from mount")


    def slew(self, ra_hours, da_deg):
        self.connect()
        self.mount.SlewToCoordinatesAsync(ra_hours, da_deg)
        print(f"Slewed to RA:{ra_hours}h, Dec:{da_deg}°")

    def get_current_coordinates(self):
        self.connect()
        ra = self.mount.RightAscension
        dec = self.mount.Declination
        print(f"Current Coordinates - RA: {ra}h, Dec: {dec}°")
        return ra, dec