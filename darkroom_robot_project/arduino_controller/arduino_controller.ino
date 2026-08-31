#include <Adafruit_NeoPixel.h>

// 조명 전용 보드 (FTDI). 서보는 다른 아두이노(servo_controller)가 맡는다.
// 명령: B:30 / ALL:30 / R1:n / OFF
#define LED_PIN 8
#define LED_COUNT 96
#define MAX_BRIGHTNESS 80
#define RING_SIZE 24

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);

int clampBright(int value) {
  if (value < 0) {
    return 0;
  }
  if (value > MAX_BRIGHTNESS) {
    return MAX_BRIGHTNESS;
  }
  return value;
}

void setAll(int value) {
  uint32_t color = strip.Color(value, value, value);
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, color);
  }
  strip.show();
}

void setRing(int ring, int value) {
  if (ring < 1 || ring > 4) {
    Serial.println("ERR RING");
    return;
  }
  uint32_t color = strip.Color(value, value, value);
  int start = (ring - 1) * RING_SIZE;
  for (int i = 0; i < RING_SIZE; i++) {
    strip.setPixelColor(start + i, color);
  }
  strip.show();
  Serial.print("OK R");
  Serial.print(ring);
  Serial.print(":");
  Serial.println(value);
}

void setup() {
  Serial.begin(9600);
  strip.begin();
  setAll(0);
  Serial.println("READY NeoPixel96 D8");
}

void loop() {
  if (Serial.available() <= 0) {
    return;
  }

  String input = Serial.readStringUntil('\n');
  input.trim();
  if (input.length() == 0) {
    return;
  }

  if (input == "OFF") {
    setAll(0);
    Serial.println("OK OFF");
    return;
  }

  if (input.startsWith("B:") || input.startsWith("ALL:")) {
    int value = clampBright(input.substring(input.indexOf(':') + 1).toInt());
    setAll(value);
    Serial.print("OK ALL:");
    Serial.println(value);
    return;
  }

  if (input.charAt(0) == 'R' && input.indexOf(':') == 2) {
    int ring = input.substring(1, 2).toInt();
    int value = clampBright(input.substring(3).toInt());
    setRing(ring, value);
    return;
  }

  Serial.println("ERR CMD");
}
