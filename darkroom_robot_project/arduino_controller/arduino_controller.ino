#include <Adafruit_NeoPixel.h>

// 조명 전용 보드 (FTDI). 서보는 다른 아두이노(servo_controller)가 맡는다.
// 데이터는 D7 한 줄(R1→R4). 5V/GND는 링 2개씩 두 갈래.
// 명령: B:30 / ALL:30 / R1:n / C:r,g,b / OFF
// C 는 색온도(톤) 조절용이다. B 는 r=g=b 인 C 와 같다.
// NeoPixel 흰색은 낮은 밝기에서 파랗게 치우친다. 그 보정은 카메라 화이트밸런스로
// 밀지 말고 여기서 채널비로 잡는 편이 노이즈 면에서 유리하다.
#define LED_PIN 7
#define LED_COUNT 96
#define MAX_BRIGHTNESS 80
#define RING_SIZE 24
// LED 하나가 쓸 수 있는 채널 합. 흰색 B:80 이 80*3 = 240 이라 그 전류를 그대로 예산으로 쓴다.
// 톤을 맞추려고 G·B 를 깎으면 합이 남는다. 그 여유를 R 에 돌려주면 검증된 전류 안에서
// 광량을 더 뽑을 수 있다. 채널 하나는 이 예산 안에서 80 을 넘어도 된다.
#define MAX_TOTAL 240

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

int clampChannel(int value) {
  if (value < 0) {
    return 0;
  }
  if (value > 255) {
    return 255;
  }
  return value;
}

// 합이 예산을 넘으면 세 채널을 같은 비율로 줄인다. 한 채널만 자르면 톤이 틀어진다.
void fitBudget(int *r, int *g, int *b) {
  long total = (long)(*r) + (long)(*g) + (long)(*b);
  if (total <= MAX_TOTAL) {
    return;
  }
  *r = (int)((long)(*r) * MAX_TOTAL / total);
  *g = (int)((long)(*g) * MAX_TOTAL / total);
  *b = (int)((long)(*b) * MAX_TOTAL / total);
}

void setAllRgb(int r, int g, int b) {
  uint32_t color = strip.Color(r, g, b);
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, color);
  }
  strip.show();
}

void setAll(int value) {
  setAllRgb(value, value, value);
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
  Serial.println("READY NeoPixel96 D7");
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

  if (input.startsWith("C:")) {
    String rest = input.substring(2);
    int first = rest.indexOf(',');
    int second = rest.indexOf(',', first + 1);
    if (first < 0 || second < 0) {
      Serial.println("ERR CMD");
      return;
    }
    int r = clampChannel(rest.substring(0, first).toInt());
    int g = clampChannel(rest.substring(first + 1, second).toInt());
    int b = clampChannel(rest.substring(second + 1).toInt());
    fitBudget(&r, &g, &b);
    setAllRgb(r, g, b);
    Serial.print("OK C:");
    Serial.print(r);
    Serial.print(",");
    Serial.print(g);
    Serial.print(",");
    Serial.println(b);
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
