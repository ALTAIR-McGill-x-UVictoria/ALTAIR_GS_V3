/*
 * UART <-> USB Serial Passthrough — Seeed XIAO ESP32C3
 * -------------------------------------------------------
 * Relays raw bytes bidirectionally between the ESP32C3's native
 * USB-CDC port (Serial, over the USB-C connector) and a hardware
 * UART (Serial1) wired to an external device.
 *
 *   UART_TX_PIN (this board) -> external device RX
 *   UART_RX_PIN (this board) <- external device TX
 *
 * No parsing, no buffering logic, no framing — just a raw pipe in
 * both directions.
 *
 * REQUIRED Arduino IDE setting:
 *   Tools > USB CDC On Boot: "Enabled"
 *   (without this, Serial won't come up over USB on the C3)
 */

#include <Arduino.h>

// ---- Configuration — adjust to match your wiring/baud ----
#define UART_TX_PIN   D6
#define UART_RX_PIN   D7
#define UART_BAUD     9600   // must match the external device
#define USB_BAUD      9600   // ignored by native USB-CDC, harmless to set

HardwareSerial UartPort(1);    // use UART peripheral #1

void setup() {
  Serial.begin(USB_BAUD);
  UartPort.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);

  // Uncomment to block until the host opens the USB CDC port:
  // while (!Serial) { delay(10); }
}

void loop() {
  // UART -> USB
  while (UartPort.available()) {
    Serial.write(UartPort.read());
  }

  // USB -> UART
  while (Serial.available()) {
    UartPort.write(Serial.read());
  }
}
