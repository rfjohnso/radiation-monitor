/*
  CosmicWatch Desktop Muon Detector - Serial Only

  Minimal sketch: detects muon events and outputs to serial.
  No SD card or OLED required.

  Based on CosmicWatch Desktop Muon Detector v2 by Spencer N. Axani (saxani@mit.edu)

  Output format (space-delimited):
    count timestamp_ms adc sipm_mV deadtime_ms temperature_C
*/

#include <EEPROM.h>

const int SIGNAL_THRESHOLD = 50;
const int RESET_THRESHOLD  = 25;
const int LED_BRIGHTNESS   = 255;

// Calibration polynomial for ADC -> SiPM voltage (mV)
const long double cal[] = {
  -9.085681659276021e-27, 4.6790804314609205e-23, -1.0317125207013292e-19,
  1.2741066484319192e-16, -9.684460759517656e-14, 4.6937937442284284e-11,
  -1.4553498837275352e-08, 2.8216624998078298e-06, -0.000323032620672037,
  0.019538631135788468, -0.3774384056850066, 12.324891083404246
};

char detector_name[40];
unsigned long count             = 0;
unsigned long time_stamp        = 0;
unsigned long measurement_deadtime = 0;
int           start_time        = 0;
long int      total_deadtime    = 0;
unsigned long measurement_t1;
float         temperatureC;

boolean get_detector_name(char* det_name) {
  byte ch;
  int bytesRead = 0;
  ch = EEPROM.read(bytesRead);
  if (ch == 0xFF) return false;
  while (ch != 0x00 && bytesRead < 40) {
    det_name[bytesRead] = ch;
    bytesRead++;
    ch = EEPROM.read(bytesRead);
  }
  det_name[bytesRead] = '\0';
  return true;
}

float get_sipm_voltage(float adc_value) {
  float voltage = 0;
  for (int i = 0; i < (sizeof(cal) / sizeof(float)); i++) {
    voltage += cal[i] * pow(adc_value, (sizeof(cal) / sizeof(float) - i - 1));
  }
  return voltage;
}

void setup() {
  Serial.begin(9600);
  analogReference(INTERNAL);
  pinMode(3, OUTPUT);
  pinMode(6, OUTPUT);
  digitalWrite(6, LOW);

  if (!get_detector_name(detector_name)) {
    strcpy(detector_name, "CosmicWatch");
  }

  Serial.println(F("##########################################################################################"));
  Serial.println(F("### CosmicWatch: The Desktop Muon Detector (Serial Only)"));
  Serial.println(F("### Event Ardn_time[ms] ADC[0-1023] SiPM[mV] Deadtime[ms] Temp[C]"));
  Serial.println(F("##########################################################################################"));
  Serial.print(F("Device ID: "));
  Serial.println(detector_name);

  analogRead(A0);
  start_time = millis();
}

void loop() {
  if (analogRead(A0) > SIGNAL_THRESHOLD) {
    int adc = analogRead(A0);
    count++;

    measurement_deadtime = total_deadtime;
    time_stamp = millis() - start_time;
    measurement_t1 = micros();

    temperatureC = (((analogRead(A3) + analogRead(A3) + analogRead(A3)) / 3.0 * (3300.0 / 1024.0)) - 500.0) / 10.0;

    analogWrite(3, LED_BRIGHTNESS);

    // Use Serial.print to avoid String memory fragmentation
    Serial.print(count);
    Serial.print(' ');
    Serial.print(time_stamp);
    Serial.print(' ');
    Serial.print(adc);
    Serial.print(' ');
    Serial.print(get_sipm_voltage(adc));
    Serial.print(' ');
    Serial.print(measurement_deadtime);
    Serial.print(' ');
    Serial.println(temperatureC);

    digitalWrite(3, LOW);

    while (analogRead(A0) > RESET_THRESHOLD) { continue; }

    total_deadtime += (micros() - measurement_t1) / 1000.0;
  }
}
